import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from intake_alerts import (AlertClient, AlertWorker, delete_intake_alert, format_alert,
                           list_intake_alerts, queue)
from transfer_card import card_for, claimed_name
from transfer_detector import State


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'INTAKE_SELF_ALERT_ENABLED': 'true',
            'INTAKE_SELF_ALERT_TEST_ENABLED': '', 'INTAKE_SELF_ALERT_MAIN_URL': 'http://127.0.0.1:8753/api',
            'INTAKE_SELF_ALERT_TOKEN': 'synthetic-alert-token-never-a-real-credential'})
        self.env.start()
        self.addCleanup(self.env.stop)
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.state = State(root.name)
        self.client = Mock()
        self.worker = AlertWorker(self.state, self.client)

    def payload(self, **extra):
        return {'event_id': 'event-one', 'request_key': 'request-one', 'revision': 1,
                'event_type': 'transfer_request_candidate', 'card': {
                    'requester_display_name': 'Demo Contact', 'claimed_name': 'Ada',
                    'amount': {'value': '12500', 'currency': 'NGN'}, 'bank': 'Wema',
                    'account_number': '0000012345'}, **extra}

    def put(self, payload):
        with self.state.db() as db:
            queue(db, payload)

    def test_format_and_privacy(self):
        text = format_alert(self.payload()['card'])
        self.assertEqual(text, 'Demo Contact (claims to be Ada): NGN 12,500; Wema; account ending 2345. Approve in Transfer Request Bot')
        self.assertNotIn('0000012345', text)
        self.assertEqual(format_alert({}), 'unknown sender: amount not given; bank not given; account not given. Approve in Transfer Request Bot')
        card = self.payload()['card'] | {'requester_display_name': 'Demo 0000012345\n', 'bank': 'Wema 0000012345', 'claimed_name': 'Ada 0000012345'}
        self.assertNotIn('0000012345', format_alert(card))
        self.assertNotIn('\n', format_alert(card))
        card['amount'] = {'value': '1000.05', 'currency': None}
        self.assertIn('1,000.05', format_alert(card))
        self.assertNotIn('NGN', format_alert(card))
        card['account_number'] = 12345
        self.assertIn('account not given', format_alert(card))

    def test_flags_and_synthetic_events(self):
        with patch.dict(os.environ, {'INTAKE_SELF_ALERT_ENABLED': ''}):
            self.put(self.payload())
        self.assertFalse(self.worker.one())
        for marker in ({'dry_run': True}, {'test': True}, {'source': {'content': 'TRANSFER_DETECTOR_TEST: synthetic'}}):
            self.put(self.payload(**marker))
            self.assertFalse(self.worker.one())
        with patch.dict(os.environ, {'INTAKE_SELF_ALERT_TEST_ENABLED': 'true'}):
            self.put(self.payload(dry_run=True))
        self.assertTrue(self.worker.one())
        self.assertTrue(self.client.send.call_args.args[0]['test'])

    def test_idempotent_jobs_and_revision_selection(self):
        for _ in range(2):
            self.put(self.payload())
        self.assertTrue(self.worker.one())
        self.assertFalse(self.worker.one())
        self.client.send.assert_called_once()
        for revision, fields in enumerate((['account_name'], ['amount'], ['bank'], ['account_number']), 2):
            self.put(self.payload(event_id=f'event-{revision}', revision=revision,
                                  event_type='candidate_updated', changed_fields=fields))
            self.worker.one()
            job = self.client.send.call_args.args[0]
            self.assertEqual(job['suppress'], fields == ['account_name'])
        self.put(self.payload(event_id='withdrawn', candidate_withdrawn=True, previous_event_id='event-one'))
        self.worker.one()
        self.client.delete.assert_called_once_with('event-one')

    def test_no_replay_or_send_retry(self):
        # Existing webhook outbox payloads never become alert jobs on upgrade.
        with patch.dict(os.environ, {'INTAKE_SELF_ALERT_ENABLED': ''}):
            self.state.enqueue_system(self.payload())
        self.state = State(self.state.path.parent)
        self.worker = AlertWorker(self.state, self.client)
        self.assertFalse(self.worker.one())
        self.put(self.payload())
        self.client.send.side_effect = RuntimeError('synthetic error must not be logged')
        self.assertTrue(self.worker.one())
        self.assertFalse(self.worker.one())
        self.put(self.payload(event_id='event-after-crash'))
        restarted = AlertWorker(self.state, self.client)
        self.assertFalse(restarted.one())
        with self.state.db() as db:
            rows = db.execute('SELECT status,payload FROM intake_alert_jobs ORDER BY rowid').fetchall()
        self.assertEqual(rows, [('unconfirmed', None), ('abandoned', None)])

    def test_detector_transaction_creates_revision_jobs(self):
        first = self.payload(source={'chat_jid': '999001111@lid'})
        self.state.consume(1, 'source-one', 'digest-one', first)
        second = self.payload(event_id='event-two', source={'chat_jid': '999001111@lid'})
        second['card'] = second['card'] | {'amount': {'value': '15000', 'currency': 'NGN'}}
        self.state.consume(2, 'source-two', 'digest-two', second)
        with self.state.db() as db:
            jobs = [json.loads(x[0]) for x in db.execute('SELECT payload FROM intake_alert_jobs ORDER BY rowid')]
        self.assertEqual([job['revision'] for job in jobs], [1, 2])
        self.assertEqual([job['suppress'] for job in jobs], [False, False])
        self.assertIn('15,000', jobs[1]['text'])
        self.assertNotIn('0000012345', json.dumps(jobs))
        self.assertEqual(self.state.get('cursor'), 2)

    def test_worker_is_off_detector_path_and_wakes_promptly(self):
        import threading
        reached, release = threading.Event(), threading.Event()
        def blocked(job):
            reached.set()
            release.wait(2)
        self.client.send.side_effect = blocked
        thread = threading.Thread(target=self.worker.run)
        thread.start()
        try:
            started = time.monotonic()
            self.put(self.payload())
            self.assertTrue(reached.wait(1))
            self.assertLess(time.monotonic() - started, 1)
            self.state.set(cursor=5)
            self.assertEqual(self.state.get('cursor'), 5)
        finally:
            release.set()
            self.worker.stop.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_main_identity_and_cross_process_request(self):
        client = AlertClient()
        client.request = Mock(side_effect=[{'data': {'jid': '15550001755@s.whatsapp.net'}}, {'success': True}])
        job = {'event_id': 'event-secondary', 'text': 'synthetic'}
        self.assertTrue(client.send(job)['success'])
        self.assertEqual(client.request.call_args.args,
            ('POST', 'intake-alerts', {**job, 'chat_jid': '15550001755@s.whatsapp.net'}))
        client.request = Mock(return_value={'data': {'jid': '15550000002@s.whatsapp.net'}})
        with self.assertRaises(ValueError):
            client.send(job)
        client.request.assert_called_once()
        with patch.dict(os.environ, {'INTAKE_SELF_ALERT_MAIN_URL': 'http://remote.example/api'}):
            with self.assertRaises(ValueError):
                AlertClient()

    def test_delete_and_list_tools_auth_validation_and_forwarding(self):
        self.assertEqual(delete_intake_alert('event-one')['code'], 'owner_auth_required')
        self.assertEqual(list_intake_alerts('event-one')['code'], 'owner_auth_required')
        self.assertEqual(delete_intake_alert('!')['code'], 'invalid_event_id')
        self.assertEqual(delete_intake_alert('event-one', 'free text')['code'], 'invalid_reason')
        with patch('intake_alerts._owner_alert', return_value={'success': True, 'data': []}) as call:
            self.assertTrue(delete_intake_alert('event-one', 'edited')['success'])
            call.assert_called_with('POST', 'intake-alerts/delete', {'event_id': 'event-one', 'reason': 'edited'})
            self.assertTrue(list_intake_alerts('event-one')['success'])
            call.assert_called_with('GET', 'intake-alerts', params={'event_id': 'event-one'})

    def test_claimed_names_are_explicit_and_separate(self):
        for text, expected in [('this is Tunde, please send to 0000012345 Wema', 'Tunde'),
                ('my name is Ada Okafor. Please transfer NGN 5000', 'Ada Okafor'),
                ('from Ada', 'Ada'), ('send from Ada account', None),
                ('account name is Ada', None), ('this is my bank account', None),
                ('this is Tunde, my name is Ada.', None)]:
            with self.subTest(text=text):
                self.assertEqual(claimed_name(text), expected)
        row = {'id': 'message-one', 'content': 'this is Tunde, please pay NGN 5000', 'chat_name': 'Push Demo'}
        clues = {'source_message_ids': ['message-one']}
        card = card_for(row, clues, {'requester_is_saved_contact': True,
                        'saved_contact_name': 'Saved Demo', 'push_name': 'Push Demo'}, [])
        self.assertEqual(card['claimed_name'], 'Tunde')
        self.assertEqual(card['whatsapp_contact_name'], 'Saved Demo')
        self.assertEqual(card['whatsapp_push_name'], 'Push Demo')
        self.assertIn('Saved Demo (claims to be Tunde)', format_alert(card))
