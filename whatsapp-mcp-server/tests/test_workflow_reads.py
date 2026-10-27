import sqlite3
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import whatsapp
import workflow_reads


class StructuredReadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "messages.db"
        with sqlite3.connect(self.database) as db:
            db.executescript("""CREATE TABLE messages(id TEXT,chat_jid TEXT,sender TEXT,content TEXT,
                timestamp TEXT,is_from_me BOOLEAN,media_type TEXT,filename TEXT,status TEXT);
                CREATE TABLE message_quotes(id TEXT,chat_jid TEXT,reply_to_message_id TEXT,reply_to_sender_jid TEXT,forwarded BOOLEAN);
                CREATE TABLE receipt_events(chat_jid TEXT,message_id TEXT,recipient_jid TEXT,status TEXT,timestamp TEXT);""")
            for message_id in ("a", "b", "c"):
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)", (
                    message_id, "15550000001@s.whatsapp.net", "15550000001", "Synthetic text",
                    "2026-01-01 00:00:00+00:00", True, "", "", "sent"))
            db.execute("INSERT INTO message_quotes VALUES(?,?,?,?,?)", (
                "c", "15550000001@s.whatsapp.net", "a", "15550000001@s.whatsapp.net", None))
            db.execute("INSERT INTO receipt_events VALUES(?,?,?,?,?)", (
                "15550000001@s.whatsapp.net", "c", "15550000001@s.whatsapp.net", "read", "2026-01-01T00:00:10Z"))
        override = patch.object(whatsapp, "MESSAGES_DB_PATH", str(self.database))
        override.start()
        self.addCleanup(override.stop)

    def test_keyset_export_same_timestamp_no_duplicates_and_no_writes(self):
        before = self.database.read_bytes()
        first = workflow_reads.export_chat("15550000001@s.whatsapp.net", limit=2)
        second = workflow_reads.export_chat("15550000001@s.whatsapp.net", first["next_cursor"], limit=2)
        records = first["messages"] + second["messages"]
        self.assertEqual([r["id"] for r in records], ["c", "b", "a"])
        self.assertEqual(len({r["stable_id"] for r in records}), 3)
        self.assertIsNone(second["next_cursor"])
        self.assertIsNone(records[0]["forwarded"])
        self.assertEqual(records[0]["reply_to_message_id"], "a")
        self.assertEqual(self.database.read_bytes(), before)

    def test_cursor_cannot_change_chat_or_be_invalid(self):
        cursor = workflow_reads.export_chat("15550000001@s.whatsapp.net", limit=1)["next_cursor"]
        self.assertEqual(workflow_reads.export_chat("15550000002@s.whatsapp.net", cursor)["code"], "invalid_cursor")
        self.assertEqual(workflow_reads.export_chat("15550000001@s.whatsapp.net", "invalid")["code"], "invalid_cursor")

    def test_feed_pages_identical_timestamps_without_skipping(self):
        first = workflow_reads.list_new_messages("2025-12-31T23:59:59Z", limit=2)
        second = workflow_reads.list_new_messages("2025-12-31T23:59:59Z", first["next_cursor"], limit=2)
        self.assertEqual([r["id"] for r in first["messages"] + second["messages"]], ["a", "b", "c"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(workflow_reads.list_new_messages("2026-01-01T00:00:00Z")["messages"], [])
        self.assertEqual(workflow_reads.list_new_messages("2026-01-01")["code"], "invalid_feed_position")

    def test_receipt_beats_late_sent_cache_and_preserves_timestamp(self):
        result = workflow_reads.get_message_status("15550000001@s.whatsapp.net", "c")
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["receipts"][0]["timestamp"], "2026-01-01T00:00:10Z")
        self.assertEqual(workflow_reads.get_message_status("15550000001@s.whatsapp.net", "missing")["code"], "message_not_found")

    def test_missing_database_not_created_and_not_reported_as_empty_history(self):
        missing = self.database.parent / "missing.db"
        with patch.object(whatsapp, "MESSAGES_DB_PATH", str(missing)):
            self.assertEqual(workflow_reads.export_chat("15550000001@s.whatsapp.net")["code"], "history_unavailable")
        self.assertFalse(missing.exists())

    def test_profile_photo_never_fetches_untrusted_url(self):
        with patch.object(workflow_reads, "get_contact_details", return_value={"success": True,
                "data": {"photo_url": "http://127.0.0.1/private"}}), patch.dict(os.environ,
                {"WHATSAPP_STORE_DIR": str(self.database.parent)}), patch.object(workflow_reads.requests, "get") as fetch:
            self.assertEqual(workflow_reads.download_profile_photo("15550000001")["code"], "untrusted_photo_url")
            fetch.assert_not_called()

    def test_profile_photo_download_is_private_and_never_sends_bridge_token(self):
        url = "https://pps.whatsapp.net/synthetic-photo"
        response = MagicMock()
        response.status_code = 200
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"\x89PNG\r\n\x1a\nsynthetic"]
        with patch.object(workflow_reads, "get_contact_details", return_value={"success": True,
                "data": {"photo_url": url}}), patch.dict(os.environ,
                {"WHATSAPP_STORE_DIR": str(self.database.parent), "WHATSAPP_BRIDGE_TOKEN": "synthetic-bridge-secret"}), patch.object(workflow_reads.requests, "get", return_value=response) as fetch:
            result = workflow_reads.download_profile_photo("15550000001")
            self.assertTrue(result["success"])
            self.assertEqual(Path(result["path"]).stat().st_mode & 0o777, 0o600)
            fetch.assert_called_once_with(url, stream=True, allow_redirects=False, timeout=15)
