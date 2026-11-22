"""Synthetic intake fixtures only; no external connections."""
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'trb_intake_post.py'
sys.path.insert(0, str(SCRIPT.parent))
import trb_intake_post as intake


def payload(event='event-1', key='request-1', **extra):
    return {'event_id': event, 'request_key': key, 'event_type': 'transfer_request_candidate',
            'source_account': 'synthetic', 'source': {'chat_jid': 'invented@lid', 'content': 'PRIVATE MESSAGE'},
            'card': {'amount': {'value': '40000', 'currency': 'NGN'},
                     'account_number': '0000012345', 'account_name': None}, **extra}


def call_intake(args):
    return intake.intake(*args)


class IntakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_repeat_upsert_related_and_private_compact_output(self):
        first = intake.intake(self.root, payload())
        self.assertEqual(first['request']['last4'], '2345')
        self.assertTrue(intake.intake(self.root, payload())['duplicate'])
        updated = payload('event-2', previous_event_id='event-1', event_type='candidate_updated', changed_fields=['amount'])
        updated['card']['amount']['value'] = '50000'
        intake.intake(self.root, updated)
        rows = intake.read_lines(self.root / 'requests.jsonl')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['amount'], '50000')
        result = intake.intake(self.root, payload('event-3', 'request-2'))
        self.assertEqual(result['related_requests'], [
            {'request_key': 'request-1', 'event_id': 'event-2', 'state': 'pending', 'amount': '50000', 'last4': '2345'}])
        output = intake.compact(result)
        self.assertNotIn('0000012345', output)
        self.assertNotIn('PRIVATE MESSAGE', output)
        events = (self.root / 'events.jsonl').read_text()
        self.assertNotIn('PRIVATE MESSAGE', events)
        self.assertNotIn('0000012345', events)
        self.assertEqual(list(json.loads(events.splitlines()[0])),
                         ['event_id', 'request_key', 'event_type', 'previous_event_id', 'at', 'applied', 'changed_fields'])
        self.assertEqual((self.root / 'requests.jsonl').stat().st_mode & 0o777, 0o600)

    def test_resend_after_handled_or_completed_is_new_request_with_context(self):
        for status in ('handled', 'completed'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                intake.intake(root, payload())
                rows = intake.read_lines(root / 'requests.jsonl')
                rows[0]['state'] = status
                intake.commit(root, {'requests.jsonl': rows})
                resent = payload('new-message-event', 'new-message-request')
                result = intake.intake(root, resent)
                self.assertTrue(result['applied'])
                self.assertFalse(result['duplicate'])
                self.assertEqual(result['related_requests'][0]['request_key'], 'request-1')
                self.assertEqual(result['related_requests'][0]['state'], status)
                self.assertEqual(len(intake.read_lines(root / 'requests.jsonl')), 2)
                self.assertTrue(intake.intake(root, resent)['duplicate'])

    def test_related_matches_requester_or_account_but_only_recent(self):
        intake.intake(self.root, payload())
        second = payload('event-2', 'request-2')
        second['source']['chat_jid'] = 'other@lid'
        self.assertEqual(len(intake.intake(self.root, second)['related_requests']), 1)
        third = payload('event-3', 'request-3')
        third['card']['account_number'] = '1111122222'
        self.assertEqual(len(intake.intake(self.root, third)['related_requests']), 1)
        with patch.object(intake, 'now', return_value=intake.now() + timedelta(days=15)):
            self.assertEqual(intake.intake(self.root, payload('event-4', 'request-4'))['related_requests'], [])

    def test_out_of_order_and_withdrawal(self):
        intake.intake(self.root, payload('event-2', previous_event_id='event-1', event_type='candidate_updated'))
        self.assertFalse(intake.intake(self.root, payload())['applied'])
        intake.intake(self.root, payload('event-3', previous_event_id='event-2', event_type='candidate_withdrawn'))
        result = intake.intake(self.root, payload('event-4', previous_event_id='event-1', event_type='candidate_updated'))
        self.assertFalse(result['applied'])
        self.assertEqual(result['request']['state'], 'withdrawn')

    def test_missing_predecessor_never_overwrites_newer_revision(self):
        intake.intake(self.root, payload('event-4', previous_event_id='event-3', event_type='candidate_updated'))
        for revision in (payload(), payload('event-2', previous_event_id='event-1', event_type='candidate_updated')):
            result = intake.intake(self.root, revision)
            self.assertFalse(result['applied'])
            self.assertTrue(result['review_required'])
            self.assertEqual(result['request']['event_id'], 'event-4')

    def test_archive_crash_recovers_moves_without_losing_or_duplicating_lines(self):
        old = intake.now() - timedelta(days=20)
        with patch.object(intake, 'now', return_value=old):
            intake.intake(self.root, payload())
        original = intake.atomic_write
        def fail_archive(path, content):
            if path.parent.name == 'archive':
                raise OSError('synthetic archive crash')
            return original(path, content)
        with patch.object(intake, 'atomic_write', side_effect=fail_archive):
            with self.assertRaises(OSError):
                intake.archive(self.root)
        self.assertEqual(intake.archive(self.root)['archived'], {'requests': 0, 'events': 0})
        self.assertEqual(len(intake.history(self.root, 'events')), 1)
        self.assertTrue(intake.intake(self.root, payload())['duplicate'])

    def test_concurrent_process_callers_dedupe_and_retain_distinct_requests(self):
        arguments = [(str(self.root), payload())] * 8 + [
            (str(self.root), payload('unique-' + str(i), 'key-' + str(i))) for i in range(8)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(call_intake, arguments))
        self.assertEqual(sum(row['duplicate'] for row in results), 7)
        self.assertEqual(len(intake.read_lines(self.root / 'events.jsonl')), 9)
        self.assertEqual(len(intake.read_lines(self.root / 'requests.jsonl')), 9)

    def test_archive_preserves_old_pending_and_dedupe_then_is_idempotent(self):
        old = intake.now() - timedelta(days=20)
        with patch.object(intake, 'now', return_value=old):
            intake.intake(self.root, payload())
            intake.intake(self.root, payload('event-2', 'request-2'))
        intake.intake(self.root, payload('event-3', 'request-2', event_type='candidate_withdrawn', previous_event_id='event-2'))
        result = intake.archive(self.root)
        self.assertEqual(result['archived'], {'requests': 1, 'events': 2})
        self.assertEqual([row['request_key'] for row in intake.read_lines(self.root / 'requests.jsonl')], ['request-1'])
        self.assertTrue(intake.intake(self.root, payload('event-2', 'request-2'))['duplicate'])
        self.assertEqual(intake.archive(self.root)['archived'], {'requests': 0, 'events': 0})

    def test_archive_moves_old_nonopen_state_and_keeps_recent_nonterminal(self):
        intake.intake(self.root, payload())
        rows = intake.read_lines(self.root / 'requests.jsonl')
        rows[0].update(state='deferred', updated_at=(intake.now()-timedelta(days=20)).isoformat())
        intake.commit(self.root, {'requests.jsonl': rows})
        self.assertEqual(intake.archive(self.root)['archived']['requests'], 1)

    def test_crash_between_file_replacements_recovers_without_duplicate(self):
        original = intake.atomic_write
        def fail_events(path, content):
            if path.name == 'events.jsonl':
                raise OSError('synthetic crash')
            return original(path, content)
        with patch.object(intake, 'atomic_write', side_effect=fail_events):
            with self.assertRaises(OSError):
                intake.intake(self.root, payload())
        self.assertTrue(intake.intake(self.root, payload())['duplicate'])
        self.assertEqual(len(intake.read_lines(self.root / 'requests.jsonl')), 1)
        self.assertEqual(len(intake.read_lines(self.root / 'events.jsonl')), 1)

    def test_numeric_account_is_rejected_without_writes(self):
        value = payload()
        value['card']['account_number'] = 1111122222
        with self.assertRaises(ValueError):
            intake.intake(self.root, value)
        self.assertFalse((self.root / 'events.jsonl').exists())

    def test_cli_never_echoes_input_on_error_and_archive(self):
        command = [sys.executable, str(SCRIPT), '--state-dir', str(self.root)]
        result = subprocess.run(command, input=json.dumps(payload()), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('0000012345', result.stdout)
        self.assertNotIn('PRIVATE MESSAGE', result.stdout)
        result = subprocess.run(command, input='PRIVATE MESSAGE 0000012345', text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '{"error":"intake_failed"}\n')
        self.assertEqual(result.stderr, '')
        result = subprocess.run(command + ['--archive'], text=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout)['archived'], {'requests': 0, 'events': 0})
