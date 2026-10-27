import concurrent.futures
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import direct_sending
from outbound_policy import canonical_recipient, perform_send


class AutomaticSendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {
            "WHATSAPP_SEND_ENABLED": "true", "WHATSAPP_STORE_DIR": self.directory.name,
            "WHATSAPP_SEND_ALLOWLIST": "", "WHATSAPP_DO_NOT_CONTACT": "",
        })
        env.start()
        self.addCleanup(env.stop)
        self.execute = Mock(return_value={"success": True, "message_id": "synthetic-id"})

    def send(self, request_id="request-0001", message="Synthetic test", recipient="15550000001"):
        return perform_send("text", recipient, request_id, {"message": message}, self.execute)

    def test_direct_send_and_replay_after_new_connection(self):
        self.assertTrue(self.send()["success"])
        self.assertTrue(self.send()["replayed"])
        self.execute.assert_called_once_with("15550000001@s.whatsapp.net")

    def test_conflicting_payload_and_recipient_never_send_again(self):
        self.send()
        self.assertEqual(self.send(message="Changed")["code"], "request_id_conflict")
        self.assertEqual(self.send(recipient="15550000002")["code"], "request_id_conflict")
        self.assertEqual(self.execute.call_count, 1)

    def test_concurrent_same_id_dispatches_at_most_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.send(), range(4)))
        self.assertEqual(self.execute.call_count, 1)
        self.assertTrue(any(r.get("success") for r in results))

    def test_timeout_leaves_durable_unknown_and_never_retries(self):
        self.execute.side_effect = TimeoutError("must not leak content")
        self.assertEqual(self.send()["code"], "outcome_unknown")
        self.execute.side_effect = None
        self.assertEqual(self.send()["code"], "outcome_unknown")
        self.assertEqual(self.execute.call_count, 1)

    def test_failure_does_not_authorize_automatic_retry(self):
        self.execute.return_value = {"success": False, "message": "connection failed"}
        self.send()
        self.assertEqual(self.send()["code"], "outcome_unknown")
        self.assertEqual(self.execute.call_count, 1)

    def test_read_only_does_not_create_journal(self):
        with patch.dict(os.environ, {"WHATSAPP_SEND_ENABLED": "false"}):
            self.assertEqual(self.send()["code"], "sending_disabled")
        self.assertFalse((Path(self.directory.name) / "outbound-journal.sqlite").exists())
        self.execute.assert_not_called()

    def test_phone_alias_block_and_allowlist(self):
        with patch.dict(os.environ, {"WHATSAPP_DO_NOT_CONTACT": "+15550000001"}):
            self.assertEqual(self.send()["code"], "recipient_denied")
        with patch.dict(os.environ, {"WHATSAPP_SEND_ALLOWLIST": "15550000002"}):
            self.assertEqual(self.send()["code"], "recipient_denied")
        self.execute.assert_not_called()

    def test_unknown_lid_and_group_fail_closed_with_denylist(self):
        with patch.dict(os.environ, {"WHATSAPP_DO_NOT_CONTACT": "15550000002"}):
            for recipient in ("99900001@lid", "99900001@g.us"):
                self.assertEqual(self.send(recipient=recipient)["code"], "recipient_identity_unverified")
        self.execute.assert_not_called()

    def test_invalid_policy_name_or_request_id(self):
        self.assertEqual(self.send(recipient="Someone")["code"], "invalid_recipient_policy")
        self.assertEqual(self.send(request_id="bad")["code"], "invalid_request_id")
        with patch.dict(os.environ, {"WHATSAPP_DO_NOT_CONTACT": "not-a-phone"}):
            self.assertEqual(self.send()["code"], "invalid_recipient_policy")
        self.execute.assert_not_called()

    def test_journal_excludes_message_and_phone(self):
        self.send(message="Synthetic private content")
        with sqlite3.connect(Path(self.directory.name) / "outbound-journal.sqlite") as conn:
            row = conn.execute("SELECT * FROM outbound_requests").fetchone()
        self.assertNotIn("Synthetic private content", str(row))
        self.assertNotIn("15550000001", str(row))
        self.assertEqual(json.loads(row[2])["message_id"], "synthetic-id")

    def test_direct_file_freezes_bytes_and_preserves_caption(self):
        outbox = Path(self.directory.name) / "outbox"
        outbox.mkdir()
        source = outbox / "receipt.png"
        source.write_bytes(b"synthetic-image")

        def bridge(method, path, payload, **kwargs):
            self.assertEqual(payload["message"], "test caption")
            self.assertNotEqual(payload["media_path"], str(source))
            self.assertEqual(Path(payload["media_path"]).read_bytes(), b"synthetic-image")
            source.write_bytes(b"changed")
            return {"success": True, "message_id": "attachment-id"}

        with patch.object(direct_sending, "_bridge_api_request", side_effect=bridge) as api:
            result = direct_sending.send_file("15550000001", str(source), "attachment-0001", "test caption")
            self.assertTrue(result["success"])
            self.assertEqual(direct_sending.send_file("15550000001", str(source), "attachment-0001", "test caption")["code"], "request_id_conflict")
            self.assertEqual(api.call_count, 1)
        self.assertFalse(any(p.is_dir() and p.name.startswith("outbound-")
                             for p in Path(self.directory.name).iterdir()))

    def test_file_outside_outbox_and_escaping_symlink_are_refused(self):
        outside = Path(self.directory.name) / "outside.pdf"
        outside.write_bytes(b"synthetic")
        outbox = Path(self.directory.name) / "outbox"
        outbox.mkdir()
        link = outbox / "link.pdf"
        link.symlink_to(outside)
        with patch.object(direct_sending, "_bridge_api_request") as api:
            for source in (outside, link):
                self.assertEqual(direct_sending.send_file("15550000001", str(source), "attachment-0002")["code"], "invalid_outbox_file")
            api.assert_not_called()

    def test_canonical_names_are_never_guessed(self):
        self.assertEqual(canonical_recipient("+15550000001"), "15550000001@s.whatsapp.net")
        with self.assertRaises(ValueError):
            canonical_recipient("Test Person")
