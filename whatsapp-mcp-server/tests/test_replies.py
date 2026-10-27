import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch
import replies

TARGET = {"chat_jid": "15550000001@s.whatsapp.net", "message_id": "original",
          "sender_jid": "99900002@lid", "text": "Synthetic message", "fingerprint": "hash"}

class ReplyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"WHATSAPP_STORE_DIR": directory.name,
            "WHATSAPP_SEND_ENABLED": "true", "WHATSAPP_SEND_ALLOWLIST": "",
            "WHATSAPP_DO_NOT_CONTACT": ""})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.target = dict(TARGET)
        mock = patch.object(replies, "_bridge_api_request", side_effect=self.bridge)
        mock.start()
        self.addCleanup(mock.stop)

    def bridge(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET":
            return {"success": True, "data": dict(self.target)}
        return {"success": True, "message_id": "sent-id", "reply_to_message_id": "original"}

    def send(self, message="Synthetic reply"):
        return replies.reply_message(TARGET["chat_jid"], "original", message, "request-reply-0001")

    def test_direct_reply_preserves_quote_and_replay_never_sends_again(self):
        self.assertTrue(self.send()["success"])
        self.assertEqual(self.calls[-1][2]["payload"], {
            "chat_jid": TARGET["chat_jid"], "message_id": "original",
            "message": "Synthetic reply", "fingerprint": "hash"})
        self.assertTrue(self.send()["replayed"])
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_changed_reply_with_same_id_is_rejected(self):
        self.send()
        self.assertEqual(self.send("Changed")["code"], "request_id_conflict")
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_preview_failure_and_wrong_target_never_send(self):
        self.target["chat_jid"] = "15550000002@s.whatsapp.net"
        self.assertFalse(self.send()["success"])
        self.assertFalse(any(c[0] == "POST" for c in self.calls))

    def test_mcp_catalogue_has_request_ids_and_no_confirmation_tokens(self):
        import main
        tools = asyncio.run(main.mcp.list_tools())
        for tool in tools:
            self.assertNotIn("confirmation_token", tool.inputSchema.get("properties", {}))
            if tool.name in {"send_message", "send_file", "send_audio_message", "reply_message"}:
                self.assertIn("request_id", tool.inputSchema["required"])

    def test_disabled_reply_does_not_even_read_quote(self):
        with patch.dict(os.environ, {"WHATSAPP_SEND_ENABLED": "false"}):
            self.assertEqual(self.send()["code"], "sending_disabled")
        self.assertEqual(self.calls, [])
