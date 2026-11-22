"""Synthetic regression tests. No real account, service or WhatsApp transport."""
import asyncio
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

import requests
from transfer_detection import account_clues, classify
from transfer_detector import State, Detector, bounded_notification, status_server, Webhook
from transfer_card import NameEnquiry
from transfer_telemetry import record, report, milliseconds, COLUMNS
from transfer_detector_status import record_intake_stage, get_transfer_latency_report
from test_transfer_detector import FakeBridge, FakeWebhook, message, PHONE, NOW


class LatencyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.state = State(self.root)
        self.bridge, self.hook = FakeBridge(), FakeWebhook()
        self.detector = Detector(self.state, self.bridge, self.hook, 'synthetic-instance', '15550000001')
        self.detector.initialize()
        self.state.set(go_live=(NOW-timedelta(minutes=30)).isoformat())

    def add(self, row):
        self.bridge.messages.append(row)
        self.bridge.context.append(row)
        self.detector.drain()
        return self.state.due()

    def pending(self):
        with self.state.db() as db:
            return [json.loads(x[0]) for x in db.execute('SELECT payload FROM outbox WHERE payload IS NOT NULL ORDER BY rowid')]

    def test_wallet_phone_shaped_account_and_plain_phone_negatives(self):
        # Same 11-digit prefix/shape as reported regression, with invented digits.
        for word in ('OPay', 'PalmPay', 'Moniepoint', 'Kuda', 'acct', 'account'):
            text = '09100000000 / ' + word + ' / Demo Beneficiary'
            self.assertEqual(account_clues(text), ['09100000000'])
            self.assertEqual(classify(message(text=text))['account_number_candidates'], ['09100000000'])
        for text in ('09100000000 / Demo Beneficiary', 'Call 09100000000', '+2349100000000 OPay',
                     '0 910 000 0000 OPay', '09100000000 GTB', 'NGN 5000 OPay'):
            self.assertEqual(account_clues(text), [])
            self.assertIsNone(classify(message(text=text)))

    def test_card_complete_saved_contact_and_account_string(self):
        get = self.bridge.get
        def saved(path, params=None):
            result = get(path, params)
            if path == 'contact/aliases':
                result.update(requester_is_saved_contact=True, saved_contact_name='Saved Demo')
            return result
        with patch.object(self.bridge, 'get', side_effect=saved):
            payload = self.add(message(text='Please send NGN 5000 for plumbing. 09100000000 OPay'))[1]
        card = payload['card']
        self.assertEqual(card['requester_display_name'], 'Saved Demo')
        self.assertEqual(card['account_number'], '09100000000')
        self.assertEqual(card['amount'], {'value': '5000', 'currency': 'NGN'})
        self.assertEqual(card['purpose'], 'plumbing')
        self.assertTrue(payload['requester_is_saved_contact'])
        self.assertEqual(card['saved_contact_phone'], PHONE)
        self.assertIsNone(card['account_name'])
        self.assertEqual(card['account_name_reason'], 'name_enquiry_disabled')
        self.assertFalse(payload['payment_authorized'])

    def test_unknown_amount_and_unsaved_contact_are_explicit(self):
        payload = self.add(message(text='09100000000 OPay Demo'))[1]
        self.assertFalse(payload['requester_is_saved_contact'])
        self.assertIsNone(payload['card']['saved_contact_name'])
        self.assertIsNone(payload['card']['saved_contact_phone'])
        self.assertIsNone(payload['card']['amount'])
        self.assertEqual(self.detector.hold_until(message(), payload), 0)

    def test_multibyte_cap_integrity_and_card_never_dropped(self):
        payload = self.add(message(text='Please send NGN 5000 for rent 09100000000 OPay'))[1]
        payload['context'] = [{'content': '界'*4000}]*20
        payload['source']['content'] = '界'*2000
        bounded = bounded_notification(payload)
        body = json.dumps(bounded, ensure_ascii=False, separators=(',', ':')).encode()
        self.assertLessEqual(len(body), 3500)
        self.assertEqual(bounded['payload_bytes'], len(body))
        self.assertEqual(bounded['payload_end'], bounded['event_id'])
        self.assertEqual(bounded['card'], payload['card'])
        self.assertTrue(bounded['integrity']['card_fields_preserved'])
        self.assertEqual(bounded, bounded_notification(payload))

    def test_backoff_one_two_four_eight_capped_and_warning_once(self):
        key = self.add(message())[0]
        self.hook.fail = True
        for attempt in range(9):
            with self.state.db() as db:
                db.execute('UPDATE outbox SET next_attempt=0 WHERE id=?', (key,))
            with patch('transfer_detector.time.time', return_value=1000):
                self.detector.deliver_one()
            with self.state.db() as db:
                count, next_at = db.execute('SELECT attempts,next_attempt FROM outbox WHERE id=?', (key,)).fetchone()
            self.assertEqual(count, attempt+1)
            self.assertEqual(next_at-1000, min(60, 2**attempt))
        warnings = [p for p in self.pending() if p['event_type'] == 'detector_health_warning']
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]['failed_event_id'], key)
        self.assertEqual({call[0] for call in self.hook.calls}, {key})

    def test_webhook_idempotency_header_and_real_http_status(self):
        webhook = Webhook('https://example.invalid/wake', 'X-Owner-Key', 'synthetic-key')
        with patch.object(webhook.session, 'post', return_value=Mock(status_code=202)) as post:
            for _ in range(2):
                self.assertEqual(webhook.send('synthetic-event', {'payment_authorized': False}), (True, None))
            self.assertEqual(post.call_args.kwargs['headers']['Idempotency-Key'], 'synthetic-event')
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
            self.assertEqual(webhook.last_status, 202)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args.kwargs['timeout'], (3, 5))
            self.assertFalse(webhook.session.trust_env)

    def test_telemetry_attempts_persist_and_end_to_end_requires_owner_stage(self):
        stamp = milliseconds()
        key = self.add(message(timestamp=datetime.fromtimestamp((stamp-7000)/1000, timezone.utc).isoformat(),
                               ingested_at=stamp-6000))[0]
        self.assertTrue(self.detector.deliver_one())
        self.assertEqual(report(self.state)['hops'].get('message_to_owner'), None)
        self.assertEqual(len(report(self.state)['awaiting_owner_stage']), 1)
        record(self.state, key, 'card_created')
        record(self.state, key, 'owner_alerted')
        result = report(self.state)
        self.assertIn(key, result['event_ids_over_5s'])
        self.assertGreater(result['hops']['message_to_owner']['p90_ms'], 5000)
        with self.state.db() as db:
            row = db.execute('SELECT msg_ts,ingested_at,observed_at,classify_ms,post_started_at,http_status,http_ms,delivered_at,attempts FROM outbox WHERE id=?', (key,)).fetchone()
        self.assertTrue(all(x is not None for x in row))
        self.assertEqual(row[-1], 1)
        self.assertEqual(State(self.root).status()['last_24h_p90_ms'], result['hops']['message_to_owner']['p90_ms'])

    def test_percentiles_exact_and_append_only_stages(self):
        key = self.add(message())[0]
        with self.state.db() as db:
            db.execute('UPDATE outbox SET msg_ts=1000,ingested_at=2000,observed_at=3000,classify_ms=7,http_ms=8 WHERE id=?', (key,))
        with patch('transfer_telemetry.milliseconds', return_value=7000):
            record(self.state, key, 'card_created')
        value = report(self.state)['hops']['message_to_owner']
        self.assertEqual([value[x] for x in ('count','median_ms','p90_ms','max_ms')], [1,6000,6000,6000])
        with self.state.db() as db:
            for sql in ('UPDATE stages SET at=0', 'DELETE FROM stages'):
                with self.assertRaises(sqlite3.IntegrityError): db.execute(sql)
        for event, stage in [('missing','card_created'), (key,'arbitrary text')]:
            with self.assertRaises(ValueError):record(self.state, event, stage)
        with self.assertRaises(ValueError):report(self.state, 0)

    def test_owner_tools_refuse_unauthenticated_calls(self):
        self.assertEqual(record_intake_stage('synthetic', 'card_created')['code'], 'owner_auth_required')
        self.assertEqual(get_transfer_latency_report()['code'], 'owner_auth_required')
        self.assertEqual(get_transfer_latency_report(100)['code'], 'invalid_days')

    def test_status_and_stage_api_auth_and_content_free(self):
        key = self.add(message())[0]
        server = status_server(self.state, 'synthetic-key', port=0)
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        self.assertEqual(requests.post(base+'/stages', json={'event_id':key,'stage':'card_created'}).status_code, 401)
        auth = {'Authorization':'Bearer synthetic-key'}
        self.assertEqual(requests.post(base+'/stages', json={'event_id':key,'stage':'card_created'},headers=auth).status_code,200)
        self.assertEqual(requests.post(base+'/stages',json={'event_id':key,'stage':'card_created','at':0},headers=auth).status_code,400)
        response = requests.get(base+'/latency?days=14',headers=auth)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('content',response.text)
        self.assertNotIn('0123456789',response.text)

    def configured_enquiry(self):
        env = patch.dict(os.environ, {'WHATSAPP_TRANSFER_NAME_ENQUIRY_URL':'https://example.invalid/enquiry'})
        env.start(); self.addCleanup(env.stop)
        self.add(message(text='09100000000 OPay Demo'))
        return NameEnquiry(self.state, 'https://example.invalid/enquiry')

    def test_name_enquiry_timeout_never_blocks_card_and_logs_no_content(self):
        enquiry = self.configured_enquiry()
        entered, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set();release.wait(3);return None
        output = io.StringIO()
        with patch.object(enquiry, 'lookup', side_effect=blocked), contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            worker = threading.Thread(target=enquiry.one);worker.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertTrue(self.detector.deliver_one())
            self.assertLess(time.monotonic()-started, .5)
            self.assertIsNone(self.hook.calls[0][1]['card']['account_name'])
            release.set();worker.join(3)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(len(self.pending()),0)

    def test_name_enquiry_success_emits_revision_and_receipt_cannot_resurrect(self):
        enquiry = self.configured_enquiry()
        first = self.state.due()[1]
        self.detector.deliver_one()
        with patch.object(enquiry, 'lookup', return_value='Verified Demo'):
            self.assertTrue(enquiry.one())
        updated = self.state.due()[1]
        self.assertEqual(updated['event_type'],'candidate_updated')
        self.assertEqual(updated['request_key'],first['request_key'])
        self.assertEqual(updated['previous_event_id'],first['event_id'])
        self.assertEqual(updated['card']['account_name'],'Verified Demo')
        self.assertEqual(updated['changed_fields'], ['account_name'])
        self.state.cancel_held(first['source']['chat_jid'])
        self.state.enriched(updated, updated['event_id'])
        self.assertEqual([p['event_type'] for p in self.pending()],['candidate_withdrawn'])

    def test_changed_fields_survive_delivered_payload_cleanup_and_restart(self):
        first = self.add(message())[1]
        self.detector.deliver_one()
        self.state = State(self.root)
        self.detector.state = self.state
        self.add(message(2, 'Please send NGN 15,000 for plumbing. Bank: Demo Bank. Account 0123456789.', id='message-1'))
        updated = self.state.due()[1]
        self.assertEqual(updated['previous_event_id'], first['event_id'])
        self.assertEqual(updated['changed_fields'], ['amount'])

    def test_migration_releases_legacy_hold_without_changing_claim(self):
        key = self.add(message())[0]
        claim = self.state.attempt_started(key)
        with self.state.db() as db:
            db.execute('UPDATE outbox SET held=1,next_attempt=9999999999 WHERE id=?',(key,))
        migrated = State(self.root)
        with migrated.db() as db:
            held, next_at, actual_claim = db.execute('SELECT held,next_attempt,first_attempt FROM outbox').fetchone()
            columns = {x[1] for x in db.execute('PRAGMA table_info(outbox)')}
        self.assertEqual((held,next_at,actual_claim),(0,0,claim))
        self.assertTrue(set(COLUMNS) <= columns)
        self.assertTrue({'push_sent_at','push_status'}.isdisjoint(columns))

    def test_event_overlay_fanout_updates_and_withdrawals_have_no_private_fields(self):
        from mcp_events import EventStore, KINDS
        events = EventStore(self.root,'whatsapp-main','synthetic-instance')
        self.state.events = events
        events.subscribe('owner',{'source_account':'whatsapp-main','event_types':list(KINDS)},'https://example.invalid', 'synthetic-unused',time.time()+300)
        first=self.add(message(text='09100000000 OPay Demo'))[1]
        self.add(message(2,'5,000'))
        self.state.cancel_held(first['source']['chat_jid'])
        with events.db() as db:
            refs = db.execute('SELECT kind FROM event_refs ORDER BY seq').fetchall()
            payloads=[x[0] for x in db.execute('SELECT payload FROM event_deliveries WHERE payload IS NOT NULL')]
        self.assertEqual([x[0] for x in refs], ['transfer_request_candidate','candidate_updated','candidate_withdrawn'])
        for payload in payloads:
            self.assertNotIn('09100000000',payload)
            self.assertNotIn('content',payload)
        self.assertTrue(json.loads(payloads[-1])['data']['candidate_withdrawn'])

    def test_original_source_edit_after_followup_withdraws_latest_revision(self):
        first = self.add(message(text='09100000000 OPay Demo'))[1]
        self.detector.deliver_one()
        self.add(message(2, '5,000'))
        updated = self.state.due()[1]
        self.add(message(3, 'OTP do not share 000000', id='message-1'))
        pending = self.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['previous_event_id'], updated['event_id'])
        self.assertEqual(pending[0]['request_key'], first['request_key'])
        self.assertTrue(pending[0]['candidate_withdrawn'])

    def test_warning_failure_does_not_spawn_recursive_warnings(self):
        self.state.enqueue_system({'event_id':'synthetic-health','event_type':'detector_health_warning',
                                  'source_account':'whatsapp-main','payment_authorized':False})
        self.hook.fail=True
        for _ in range(4):
            with self.state.db() as db:db.execute('UPDATE outbox SET next_attempt=0')
            self.detector.deliver_one()
        self.assertEqual(len(self.pending()),1)
        self.assertIn('payload_bytes',self.pending()[0])

    def test_large_card_text_truncates_deterministically_preserving_keys_and_digits(self):
        payload=self.add(message(text='09100000000 OPay Demo'))[1]
        for key in ('purpose','requester_display_name','saved_contact_name','account_name'):
            payload['card'][key]='界'*240
        payload['source']['filename']='界'*200
        result=bounded_notification(payload)
        self.assertEqual(set(result['card']),set(payload['card']))
        self.assertEqual(result['card']['account_number'],'09100000000')
        self.assertTrue(result['integrity']['card_text_truncated'])
        self.assertLessEqual(result['payload_bytes'],3500)

    def test_plan_only_script_does_not_import_or_call_network_client(self):
        import importlib.util
        from pathlib import Path
        spec=importlib.util.spec_from_file_location('latency_script',Path(__file__).parents[2]/'scripts/e2e_latency.py')
        script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
        with patch('sys.argv',['e2e_latency.py']),patch.object(script,'live') as live,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(script.main(),0)
        live.assert_not_called()

    def test_stale_due_snapshot_cannot_bypass_another_workers_backoff(self):
        key=self.add(message())[0]
        stale=self.state.due()
        claim=self.state.attempt_started(key)
        self.state.failed(key,stale[2],'webhook_network_error',claim)
        self.assertIsNone(self.state.attempt_started(key))

    def test_multiple_samples_percentiles_and_receiver_hops(self):
        for n in range(1,11):
            key=self.add(message(n,'Please send NGN 500 for gas',id='sample-'+str(n)))[0]
            self.detector.deliver_one()
            with self.state.db() as db:
                db.execute('UPDATE outbox SET msg_ts=1000,post_started_at=1100 WHERE id=?',(key,))
                db.executemany('INSERT INTO stages VALUES(?,?,?)',[(key,'intake_received',1200),
                    (key,'card_created',1200+n*100),(key,'owner_alerted',1300+n*100)])
        hops=report(self.state)['hops']
        self.assertEqual(hops['intake_to_card']['median_ms'],550)
        self.assertEqual(hops['intake_to_card']['p90_ms'],900)
        self.assertEqual(hops['intake_to_card']['max_ms'],1000)
        self.assertEqual(hops['post_to_intake']['p90_ms'],100)
        self.assertEqual(hops['card_to_alert']['p90_ms'],100)


class OwnerTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_rpc_propagates_authenticated_owner_to_sdk_tools(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.auth.provider import AccessToken
        from mcp.server.auth.middleware.auth_context import get_access_token
        from mcp_events_http import EventRPC
        class Provider:
            async def load_access_token(self, token):
                return AccessToken(token=token,client_id='synthetic',scopes=['whatsapp'],subject='owner')
            async def event_owner(self, token):return 'owner'
            def event_owner_active(self, owner):return True
        mcp=FastMCP('synthetic')
        @mcp.tool()
        def owner_probe() -> dict:
            return {'owner':get_access_token().subject}
        rpc=EventRPC(mcp,Mock(),Provider())
        status,response=await rpc.handle(json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'owner_probe','arguments':{}}}).encode(),[b'Bearer synthetic'])
        self.assertEqual(status,200)
        self.assertEqual(json.loads(response['result']['content'][0]['text']),{'owner':'owner'})
        self.assertIsNone(get_access_token())

    async def test_owner_intake_tool_passes_only_safe_arguments(self):
        from mcp.server.auth.provider import AccessToken
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        context=auth_context_var.set(AuthenticatedUser(AccessToken(token='synthetic',client_id='test',scopes=['whatsapp'],subject='owner')))
        try:
            with patch.dict(os.environ,{'WHATSAPP_BRIDGE_READ_TOKEN':'synthetic-read-token-'+'x'*32}),patch('transfer_detector_status.requests.request') as request:
                request.return_value.json.return_value={'event_id':'synthetic','stage':'card_created','at':1}
                self.assertEqual(record_intake_stage('synthetic','card_created')['at'],1)
                self.assertEqual(request.call_args.kwargs['json'],{'event_id':'synthetic','stage':'card_created'})
        finally:auth_context_var.reset(context)

class NameEnquiryDeadlineTests(unittest.TestCase):
    def test_dns_timeout_is_two_seconds_and_never_creates_a_socket(self):
        with tempfile.TemporaryDirectory() as root:
            enquiry=NameEnquiry(State(root),'https://example.invalid/enquiry')
            future=Mock()
            from concurrent.futures import TimeoutError
            future.result.side_effect=TimeoutError()
            with patch('transfer_card.RESOLVER.submit',return_value=future),patch('transfer_card.socket.socket') as socket_:
                self.assertIsNone(enquiry.lookup({'account_number':'09100000000','bank':'OPay'}))
                self.assertLessEqual(future.result.call_args.kwargs['timeout'],2)
                socket_.assert_not_called()
            future.cancel.assert_called_once()

    def test_total_deadline_interrupts_a_trickling_http_body(self):
        with tempfile.TemporaryDirectory() as root:
            enquiry=NameEnquiry(State(root),'https://example.invalid/enquiry')
            future=Mock()
            future.result.return_value=[(2,1,6,'',('192.0.2.1',443))]
            sock,tls,connection=Mock(),Mock(),Mock()
            connection._context.wrap_socket.return_value=tls
            response=Mock(status=200)
            released=threading.Event()
            tls.shutdown.side_effect=lambda *args:released.set()
            def trickle(*args):
                released.wait(4)
                raise OSError('synthetic timeout')
            response.read.side_effect=trickle
            connection.getresponse.return_value=response
            with patch('transfer_card.RESOLVER.submit',return_value=future),patch('transfer_card.socket.socket',return_value=sock),patch('transfer_card.http.client.HTTPSConnection',return_value=connection):
                started=time.monotonic()
                self.assertIsNone(enquiry.lookup({'account_number':'09100000000','bank':'OPay'}))
                self.assertLess(time.monotonic()-started,2.8)
            tls.shutdown.assert_called_once()
            tls.close.assert_called()

    def test_lookup_success_full_account_string_and_bounded_read(self):
        with tempfile.TemporaryDirectory() as root:
            enquiry=NameEnquiry(State(root),'https://example.invalid/enquiry')
            future=Mock();future.result.return_value=[(2,1,6,'',('192.0.2.1',443))]
            sock,connection,tls=Mock(),Mock(),Mock()
            connection._context.wrap_socket.return_value=tls
            response=Mock(status=200)
            response.read.return_value=b'{"account_name":"Verified Demo"}'
            connection.getresponse.return_value=response
            with patch('transfer_card.RESOLVER.submit',return_value=future),patch('transfer_card.socket.socket',return_value=sock),patch('transfer_card.http.client.HTTPSConnection',return_value=connection):
                self.assertEqual(enquiry.lookup({'account_number':'09100000000','bank':'OPay'}),'Verified Demo')
            body=json.loads(connection.request.call_args.kwargs['body'])
            self.assertEqual(body['account_number'],'09100000000')
            response.read.assert_called_once_with(8193)
