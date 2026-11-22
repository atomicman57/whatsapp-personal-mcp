"""Sanitized replay scripts, run through real detector state with no wall-clock sleeps.

Media descriptions document the source scenario; only captions enter the detector.
The text-only detector does not OCR images or transcribe audio.
"""
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_transfer_detector import FakeBridge, FakeWebhook, message
from transfer_detector import Detector, State
from transfer_detection import classify
from transfer_card import card_for

REPLAYS = json.loads(Path(__file__).with_name('fixtures').joinpath('transfer_replays.json').read_text())
WEMA = '0000012345'
GTBANK = '1111122222'


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.start = self.clock
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        clock = patch('transfer_detector.utcnow', side_effect=lambda: self.clock.isoformat())
        clock.start()
        self.addCleanup(clock.stop)
        self.state = State(self.directory.name)
        self.bridge = FakeBridge()
        self.detector = Detector(self.state, self.bridge, FakeWebhook(), 'replay-instance', '15550000001')
        self.detector.initialize()
        self.events = []
        self.steps = []

    def replay(self, number, day_shift=0):
        base = self.start + timedelta(days=day_shift)
        for entry in REPLAYS[str(number)]['messages']:
            self.clock = base + timedelta(seconds=entry['offset_seconds'])
            row = message(len(self.bridge.messages)+1, entry['content'], timestamp=self.clock.isoformat(),
                          is_from_me=entry['owner'], media_type=entry['media_type'])
            self.bridge.messages.append(row)
            self.bridge.context.append(row)
            self.detector.drain()
            with self.state.db() as db:
                self.events = [json.loads(x[0]) for x in db.execute('SELECT payload FROM outbox ORDER BY rowid')]
            self.steps.append(list(self.events))
        return self.events

    def latest(self):
        return {event['request_key']: event for event in self.events}

    def destinations(self):
        return {event['card']['account_number']: event for event in self.latest().values()
                if event['card']['account_number']}

    def amount(self, event):
        amount = event['card']['amount']
        return amount['value'] if amount else None

    def assert_update(self, before, after, amount):
        self.assertEqual(after['request_key'], before['request_key'])
        self.assertEqual(after['event_type'], 'candidate_updated')
        self.assertEqual(after['revision'], 2)
        self.assertEqual(after['previous_event_id'], before['event_id'])
        self.assertEqual(self.amount(after), amount)

    def test_replay_01_compact_amount_and_phone(self):
        event, = self.replay(1)
        self.assertEqual(event['card']['account_number'], WEMA)
        self.assertEqual(event['card']['bank'], 'Wema')
        self.assertEqual(self.amount(event), '7000')
        self.assertEqual(event['revision'], 1)
        self.assertEqual(self.steps[1], [])

    def test_replay_02_next_day_split_destination(self):
        self.replay(2)
        self.assertEqual(len(self.latest()), 2)
        old, new = self.destinations()[GTBANK], self.destinations()[WEMA]
        self.assertEqual(self.amount(old), '8000')
        self.assertIsNone(self.amount(new))
        self.assertEqual(new['revision'], 2)
        self.assertEqual(new['card']['bank'], 'Wema')
        account = self.steps[5][-1]
        self.assertEqual(account['revision'], 1)
        self.assertIsNone(account['card']['bank'])
        self.assertIsNone(self.amount(account))
        self.assertEqual(new['previous_event_id'], account['event_id'])

    def test_replay_03_new_destination_then_bare_amount(self):
        self.replay(3)
        self.assertEqual(len(self.latest()), 2)
        self.assertIsNone(self.amount(self.destinations()[GTBANK]))
        before = self.steps[4][-1]
        after = self.steps[5][-1]
        self.assertEqual(before['card']['account_number'], WEMA)
        self.assertIsNone(self.amount(before))
        self.assert_update(before, after, '2500')
        self.assertEqual(after['changed_fields'], ['amount'])
        self.assertEqual(self.amount(self.destinations()[WEMA]), '2500')

    def feed(self, text, seconds=0, chat='99900001@lid'):
        self.clock=self.start+timedelta(seconds=seconds)
        row=message(len(self.bridge.messages)+1,text,timestamp=self.clock.isoformat(),chat_jid=chat)
        self.bridge.messages.append(row)
        self.bridge.context.append(row)
        self.detector.drain()
        with self.state.db() as db:
            self.events=[json.loads(x[0]) for x in db.execute('SELECT payload FROM outbox ORDER BY rowid')]
        return self.events

    def test_request_sentence_amounts_reach_cards(self):
        cases=(('abeg 2,500 to 0123456789 Opay','2500'),
               ('send 2,500 to 0123456789 Opay','2500'),
               ('2,500 to 0123456789 Opay','2500'),
               ('pls send 25,000 to 0123456789 Opay','25000'),
               ('₦2,500 to 0123456789 Opay','2500'),
               ('N2,500 to 0123456789 Opay','2500'),
               ('2,500 naira to 0123456789 Opay','2500'),
               ('SPEED1 send 1k 0123456789 Gt','1000'),
               ('SPEED2 0123456789 Access',None),
               ('SPEED3 abeg 2,500 to 0123456789 Opay','2500'),
               ('SPEED4 0123456789 GTB 3k transport','3000'),
               ('SPEED5 0123456789 Gt',None))
        for index,(text,amount) in enumerate(cases):
            with self.subTest(text=text):
                chat=f'sentence-{index}@lid'
                self.feed(text,chat=chat)
                event=self.events[-1]
                self.assertEqual(event['source']['chat_jid'],chat)
                self.assertEqual(event['card']['account_number'],'0123456789')
                self.assertEqual(self.amount(event),amount)
                self.assertEqual([x['amount'] for x in event['candidate']['amount_candidates']],
                                 [amount] if amount else [])
                if amount:
                    self.assertEqual(event['candidate']['amount_candidates'][0]['source_message_id'],
                                     event['candidate']['source_message_ids'][-1])

    def test_label_numbers_never_supply_amount_alone_or_with_account(self):
        labels=('retest 5','test 8','item 2','no 3','#4','number 2500','arbitrary 2500',
                'Please send 2500','2500 for parts','retest 2500')
        for index,label in enumerate(labels):
            with self.subTest(case=index):
                chat=f'label-{index}@lid'
                self.feed('Please send to '+WEMA+' Wema Demo '+label,chat=chat)
                self.assertIsNone(self.amount(self.events[-1]))
                before=list(self.events)
                self.feed(label,12,chat=chat)
                self.assertEqual(self.events,before)
                self.feed(label,13,chat=f'isolated-{index}@lid')
                self.assertEqual(self.events,before)

    def test_standalone_amount_formats_update_empty_key_revision_two(self):
        for index,text in enumerate(('2500','2,500','₦2500','N2500','2500 naira',
                                     'For parts\n2500','For parts\n2,500','For parts\n₦2500')):
            with self.subTest(case=index):
                chat=f'format-{index}@lid'
                self.feed(WEMA+' Wema',chat=chat)
                before=self.events[-1]
                self.feed(text,900,chat=chat)
                self.assert_update(before,self.events[-1],'2500')
                self.assertIn('amount',self.events[-1]['changed_fields'])

    def test_standalone_amount_formats_require_immediate_same_chat_account(self):
        for index,text in enumerate(('2500','2,500','₦2500','N2500','2500 naira','For parts\n2500')):
            with self.subTest(case=index):
                chat=f'context-{index}@lid'
                self.feed('Please send money',chat=chat)
                before=list(self.events)
                self.feed(text,12,chat=chat)
                self.assertEqual(self.events,before)
                self.feed(WEMA+' Wema',20,chat=chat)
                before=list(self.events)
                self.feed(text,21,chat=f'other-format-{index}@lid')
                self.assertEqual(self.events,before)
                self.feed('How is the weather?',22,chat=chat)
                before=list(self.events)
                self.feed(text,23,chat=chat)
                self.assertEqual(self.events,before)

    def test_standalone_amount_formats_do_not_replace_completed_or_inactive_key(self):
        for index,text in enumerate(('2500','2,500','₦2500','N2500','2500 naira','For parts\n2500')):
            for active in (True,False):
                with self.subTest(case=index,active=active):
                    chat=f'key-{index}-{active}@lid'
                    self.feed(WEMA+' Wema'+(' 4k' if active else ''),chat=chat)
                    if not active:
                        with self.state.db() as db:
                            db.execute('UPDATE requests_latest SET active=0 WHERE chat_jid=?',(chat,))
                    before=list(self.events)
                    self.feed(text,12,chat=chat)
                    self.assertEqual(self.events,before)

    def test_standalone_amount_formats_expire_after_900_seconds(self):
        for index,text in enumerate(('2500','2,500','₦2500','N2500','2500 naira','For parts\n2500')):
            with self.subTest(case=index):
                chat=f'expired-format-{index}@lid'
                self.feed(WEMA+' Wema',chat=chat)
                before=list(self.events)
                self.feed(text,901,chat=chat)
                self.assertEqual(self.events,before)

    def test_standalone_amount_write_rechecks_empty_active_key(self):
        for index,text in enumerate(('2500','2,500','₦2500','N2500','2500 naira','For parts\n2500')):
            for change in ('amount','inactive'):
                with self.subTest(case=index,change=change):
                    chat=f'race-{index}-{change}@lid'
                    self.feed(WEMA+' Wema',chat=chat)
                    initial=self.events[-1]
                    self.clock=self.start+timedelta(seconds=12)
                    row=message(len(self.bridge.messages)+1,text,timestamp=self.clock.isoformat(),chat_jid=chat)
                    self.bridge.context=[self.bridge.messages[-1],row]
                    key,body_hash,payload=self.detector.build(row)
                    self.assertIsNotNone(payload)
                    with self.state.db() as db:
                        if change=='inactive':
                            db.execute('UPDATE requests_latest SET active=0 WHERE request_key=?',(initial['request_key'],))
                        else:
                            card=dict(initial['card'])
                            card['amount']={'value':'4000','currency':'NGN'}
                            db.execute('UPDATE requests_latest SET card=? WHERE request_key=?',
                                       (json.dumps(card),initial['request_key']))
                        count=db.execute('SELECT count(*) FROM outbox').fetchone()[0]
                    with self.assertRaisesRegex(RuntimeError,'bare_amount_request_no_longer_empty'):
                        self.state.consume(row['sequence'],key,body_hash,payload)
                    with self.state.db() as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],count)

    def test_small_bare_numbers_and_malformed_grouping_stay_unset(self):
        for index,text in enumerate(('5','99','N99','0','25,00','2,50','2500.','2500!')):
            with self.subTest(case=index):
                chat=f'invalid-format-{index}@lid'
                self.feed(WEMA+' Wema',chat=chat)
                before=list(self.events)
                self.feed(text,12,chat=chat)
                self.assertEqual(self.events,before)

    def test_explicit_amounts_on_account_messages_still_work(self):
        for index,(text,amount) in enumerate((('5k','5000'),('₦5,000','5000'),('send 6k','6000'))):
            with self.subTest(case=index):
                self.feed(WEMA+' Wema Demo '+text,chat=f'explicit-account-{index}@lid')
                self.assertEqual(self.amount(self.events[-1]),amount)

    def test_explicit_small_currency_and_k_amounts_still_work(self):
        for index,(text,amount) in enumerate((('₦5','5'),('5 naira','5'),('5k','5000'),
                                            ('₦5,000','5000'),('send 6k','6000'))):
            with self.subTest(case=index):
                chat=f'explicit-format-{index}@lid'
                self.feed(WEMA+' Wema',chat=chat)
                before=self.events[-1]
                self.feed(text,12,chat=chat)
                self.assert_update(before,self.events[-1],amount)

    def test_bare_number_without_preceding_account_is_ignored(self):
        self.feed('Please send 8k')
        before=list(self.events)
        self.feed('2500',12)
        self.assertEqual(self.events,before)
        self.assertEqual(self.feed('2500',13,chat='other@lid'),before)

    def test_bare_number_after_completed_account_is_ignored(self):
        self.feed(WEMA+' Wema 4k')
        before=list(self.events)
        self.assertEqual(self.feed('2500',12),before)
        self.assertEqual(self.amount(self.events[0]),'4000')

    def test_bare_number_does_not_replace_previously_attached_amount(self):
        self.feed(WEMA+' Wema')
        self.feed('6k',6)
        # Omitted bridge context cannot hide the already persisted amount.
        self.bridge.context=self.bridge.context[:1]
        completed=list(self.events)
        self.assertEqual(self.feed('2500',8),completed)
        self.feed(WEMA+' Wema',10)
        # Repeated details open a new empty key; only that key can receive the reply.
        before=self.events[-1]
        self.feed('2500',12)
        self.assert_update(before,self.events[-1],'2500')
        self.assertEqual(self.amount(self.events[1]),'6000')
        self.assertNotEqual(self.events[1]['request_key'],self.events[-1]['request_key'])
        before=list(self.events)
        self.assertEqual(self.feed('5000',13),before)

    def test_bare_number_at_901_seconds_is_ignored(self):
        self.feed(WEMA+' Wema')
        before=list(self.events)
        self.assertEqual(self.feed('2500',901),before)
        self.assertIsNone(self.amount(self.events[0]))

    def test_long_bare_numbers_and_phones_never_supply_amount(self):
        for index,text in enumerate(('1000000','11111111','111111111','08000000001','23480000001')):
            with self.subTest(length=len(text)):
                chat=f'long-{index}@lid'
                self.feed(WEMA+' Wema',chat=chat)
                before=list(self.events)
                self.assertEqual(self.feed(text,12,chat=chat),before)

    def test_bare_number_after_unrelated_message_is_ignored(self):
        for index,text in enumerate(('How is the weather?', 'Price is 5k in the market', 'Your access code: 123456')):
            with self.subTest(kind=index):
                chat=f'chatter-{index}@lid'
                self.feed(WEMA+' Wema',chat=chat)
                self.feed(text,6,chat=chat)
                before=list(self.events)
                self.assertEqual(self.feed('2500',12,chat=chat),before)

    def test_three_to_six_digit_bare_amounts_and_window_boundary(self):
        for index,text in enumerate(('500','2500','2026','12345','123456')):
            with self.subTest(length=len(text)):
                chat=f'positive-{index}@lid'
                self.feed(WEMA+' Wema',chat=chat)
                before=self.events[-1]
                self.feed(text,900,chat=chat)
                self.assert_update(before,self.events[-1],text)
                self.assertEqual(self.events[-1]['changed_fields'],['amount'])

    def test_bare_amount_needs_an_emitted_active_key(self):
        self.feed(WEMA+' Wema')
        with self.state.db() as db:
            db.execute('UPDATE requests_latest SET active=0')
        before=list(self.events)
        self.assertEqual(self.feed('2500',12),before)

    def test_bare_amount_and_source_survive_restart_and_outbox_cleanup(self):
        self.feed(WEMA+' Wema')
        self.feed('2500',12)
        original=self.events[-1]
        with self.state.db() as db:
            db.execute('DELETE FROM outbox')
        self.detector=Detector(State(self.directory.name),self.bridge,FakeWebhook(),'replay-instance','15550000001')
        self.detector.initialize()
        self.feed('For coil and plug',20)
        after,=self.events
        self.assertEqual(after['request_key'],original['request_key'])
        self.assertEqual(after['revision'],3)
        self.assertEqual(after['changed_fields'],['purpose'])
        self.assertEqual(self.amount(after),'2500')
        self.assertEqual(after['candidate']['amount_candidates'][0]['source_message_id'],'message-2')

    @unittest.expectedFailure
    def test_replay_04_caption_bank_then_late_amount(self):
        """xfail: caption alone lacks the bank/request context required for an initial candidate."""
        self.replay(4)
        self.assertEqual(len(self.steps[0]), 1)
        self.assertIsNone(self.amount(self.steps[0][0]))
        self.assertEqual(len(self.latest()), 1)
        self.assert_update(self.steps[2][-1], self.events[-1], '8500')

    def test_replay_04_bare_amount_after_owner_prompt_stays_unset(self):
        self.replay(4)
        self.assertEqual(len(self.latest()), 1)
        before = self.steps[2][-1]
        self.assertIsNone(self.amount(before))
        self.assertEqual(before['card']['account_number'], WEMA)
        self.assertEqual(before['card']['bank'], 'Wema')
        self.assertEqual(before, self.events[-1])
        self.assertIsNone(self.amount(self.events[-1]))

    def test_replay_05_itemized_bill_with_misspelled_total(self):
        self.replay(5)
        self.assertEqual(len(self.latest()), 1)
        initial = self.steps[1][-1]
        self.assertEqual(initial['revision'], 1)
        self.assertEqual(initial['card']['account_number'], WEMA)
        self.assertEqual(self.amount(initial), '6000')
        self.assertEqual(self.amount(self.events[-1]), '6000')

    def test_replay_06_previous_day_cost_not_inherited(self):
        self.replay(6)
        for event in self.steps[2]:
            self.assertIsNone(event['card']['account_number'])
        self.assertEqual(len(self.destinations()), 1)
        today = self.destinations()[WEMA]
        self.assertIsNone(self.amount(today))
        today_keys = {event['request_key'] for event in self.events
                      if event['source']['timestamp'] >= (self.start+timedelta(hours=17)).isoformat()}
        self.assertEqual(today_keys, {today['request_key']})

    @unittest.expectedFailure
    def test_replay_07_approximate_then_any_amount(self):
        """xfail: no Day-0 candidate conflicts with supported destination-less explicit loan requests."""
        self.replay(7)
        self.assertIsNone(self.amount(self.destinations()[WEMA]))
        self.assertEqual(self.steps[4], [])
        event, = self.latest().values()
        self.assertEqual(event['card']['account_number'], WEMA)
        self.assertIsNone(self.amount(event))

    def test_replay_08_held_balance_is_not_a_request(self):
        self.assertEqual(self.replay(8), [])

    def test_replay_09_product_images_are_not_totals(self):
        self.replay(9)
        self.assertEqual(len(self.latest()), 1)
        for event in self.events:
            self.assertIsNone(self.amount(event))
            self.assertEqual(event['card']['account_number'], WEMA)
        self.assertEqual(self.destinations()[WEMA]['card']['bank'], 'Wema')

    def test_replay_10_quotation_separate_from_prior_payment(self):
        self.replay(10)
        self.assertEqual(len(self.latest()), 2)
        self.assertEqual(self.amount(self.destinations()[GTBANK]), '8000')
        self.assertIsNone(self.amount(self.destinations()[WEMA]))

    def test_multi_day_history_does_not_supply_amount(self):
        self.replay(1)
        original = self.events[0]
        self.replay(9, day_shift=3)
        self.assertEqual(len(self.latest()), 2)
        self.assertEqual(self.events[0], original)
        self.assertIsNone(self.amount(self.events[-1]))

    def test_replay_03_bare_amount_only_updates_latest_destination(self):
        self.replay(3)
        self.assertEqual(len(self.latest()), 2)
        self.assertEqual(set(self.destinations()), {WEMA, GTBANK})
        self.assertIsNone(self.amount(self.destinations()[GTBANK]))
        self.assertEqual(self.amount(self.destinations()[WEMA]), '2500')
        self.assertNotEqual(self.destinations()[WEMA]['request_key'], self.destinations()[GTBANK]['request_key'])

    def test_replay_07_next_day_any_amount_stays_unset(self):
        self.replay(7)
        self.assertTrue(all(self.amount(event) is None for event in self.events))
        self.assertEqual(len(self.destinations()), 1)
        today = self.destinations()[WEMA]
        self.assertIsNone(self.amount(today))
        self.assertEqual(today['revision'], 1)
        self.assertNotIn('message-4', today['candidate']['source_message_ids'])
        self.assertEqual(self.steps[7], self.steps[6])

    def test_replay_identifiers_are_obviously_synthetic(self):
        for replay in REPLAYS.values():
            for entry in replay['messages']:
                numbers = re.findall(r'\d{10,}', entry['content']+' '+entry.get('media_description', ''))
                self.assertTrue(set(numbers) <= {WEMA, GTBANK, '2348000000001'})

    def test_approximate_amount_never_supplies_nearby_destination(self):
        ask = message(1, 'I need like 8k from you sir, sa a loan from you please')
        account = message(2, WEMA+' Wema', timestamp=(self.clock+timedelta(seconds=30)).isoformat())
        for source, context in ((account, [ask]), (ask, [message(0, WEMA+' Wema',
                                  timestamp=(self.clock-timedelta(seconds=30)).isoformat())])):
            with self.subTest(source=source['id']):
                self.assertEqual(classify(source, context)['amount_candidates'], [])

    def test_itemized_list_without_total_keeps_amount_null(self):
        for text in ('Screw-1k\npeg-1k\nsilicon-2k\nworkmanship/tp-2k', 'Screw-1k\npeg-1k'):
            with self.subTest(text=text):
                row = message(1, text+'\n'+WEMA+' Wema')
                clues = classify(row)
                self.assertTrue(clues['amount_ambiguous'])
                self.assertIsNone(card_for(row, clues, {}, [row])['amount'])
                account = message(2, WEMA+' Wema')
                clues = classify(account, [message(1, text)])
                self.assertTrue(clues['amount_ambiguous'])
                self.assertIsNone(card_for(account, clues, {}, [account])['amount'])

    def test_held_balance_and_quotation_allow_explicit_payment_requests(self):
        for text in ('I still have your balance of 5k. Please send 6k',
                     'QUOTATION FOR AC WORK\nGrand Total: ₦7,200\nPlease pay'):
            with self.subTest(text=text):
                self.assertIsNotNone(classify(message(1, text)))
