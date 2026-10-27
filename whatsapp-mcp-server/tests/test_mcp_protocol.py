"""Synthetic end-to-end MCP/HTTP tests. Never connect a WhatsApp account."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_tools_to_http_and_durable_retries(self):
        dispatches = []
        authentication = []

        class Bridge(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                authentication.append(self.headers.get("Authorization"))
                if self.path.startswith("/api/reply/preview"):
                    self.answer({"success": True, "data": {"chat_jid": "15550000001@s.whatsapp.net",
                        "message_id": "original", "fingerprint": "synthetic-fingerprint"}})
                elif self.path.startswith("/api/chats"):
                    self.answer({"success": True, "data": [{"jid": "15550000001@s.whatsapp.net", "unread_count": 2}]})
                else:
                    self.answer({"success": True, "connected": True, "logged_in": True,
                        "reads_may_be_stale": False, "coverage": "synthetic_fixture_only"})

            def do_POST(self):
                authentication.append(self.headers.get("Authorization"))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "media_path" in body:
                    body["frozen_bytes"] = Path(body["media_path"]).read_bytes().hex()
                dispatches.append((self.path, body))
                self.answer({"success": True, "message_id": "synthetic-" + str(len(dispatches))})

            def answer(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as root:
            outbox = Path(root) / "outbox"
            outbox.mkdir()
            attachment = outbox / "synthetic.png"
            attachment.write_bytes(b"synthetic-receipt")
            env = {**os.environ, "WHATSAPP_STORE_DIR": root,
                "WHATSAPP_BRIDGE_URL": f"http://127.0.0.1:{server.server_port}/api",
                "WHATSAPP_BRIDGE_TOKEN": "synthetic-test-token-never-a-real-credential",
                "WHATSAPP_SEND_ENABLED": "true", "WHATSAPP_MUTATIONS_ENABLED": "false",
                "WHATSAPP_SEND_ALLOWLIST": "15550000001", "WHATSAPP_DO_NOT_CONTACT": ""}
            params = StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).resolve().parents[1] / "main.py")], env=env)

            async def run_client(first):
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertGreaterEqual(len(tools.tools), 30)
                        for tool in tools.tools:
                            self.assertNotIn("confirmation_token", tool.inputSchema.get("properties", {}))

                        async def call(name, args):
                            result = await session.call_tool(name, args)
                            self.assertFalse(result.isError, result)
                            data = result.structuredContent or json.loads(result.content[0].text)
                            return data.get("result", data)

                        sent = await call("send_message", {"recipient": "15550000001",
                            "message": "Synthetic protocol test", "request_id": "protocol-text-0001"})
                        self.assertTrue(sent["success"])
                        if not first:
                            self.assertTrue(sent["replayed"])
                            return
                        replay = await call("send_message", {"recipient": "15550000001",
                            "message": "Synthetic protocol test", "request_id": "protocol-text-0001"})
                        self.assertTrue(replay["replayed"])
                        conflict = await call("send_message", {"recipient": "15550000001",
                            "message": "Changed", "request_id": "protocol-text-0001"})
                        self.assertEqual(conflict["code"], "request_id_conflict")
                        denied = await call("send_message", {"recipient": "15550000002",
                            "message": "Must not dispatch", "request_id": "protocol-denied-0001"})
                        self.assertEqual(denied["code"], "recipient_denied")
                        reply = await call("reply_message", {"chat_jid": "15550000001@s.whatsapp.net",
                            "message_id": "original", "message": "Synthetic quoted reply",
                            "request_id": "protocol-reply-0001"})
                        self.assertTrue(reply["success"])
                        media = await call("send_file", {"recipient": "15550000001",
                            "media_path": str(attachment), "request_id": "protocol-file-0001", "caption": ""})
                        self.assertTrue(media["success"])
                        chats = await call("list_chat_state", {})
                        self.assertEqual(chats["data"][0]["unread_count"], 2)

            await run_client(True)
            await run_client(False)  # actual MCP process restart, same persistent journal
            self.assertEqual(len(dispatches), 3)
            self.assertEqual(dispatches[1][0], "/api/reply")
            self.assertEqual(dispatches[1][1]["fingerprint"], "synthetic-fingerprint")
            self.assertEqual(dispatches[2][1]["frozen_bytes"], b"synthetic-receipt".hex())
            self.assertTrue(all(v == "Bearer synthetic-test-token-never-a-real-credential" for v in authentication))
