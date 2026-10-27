"""Real MCP/cache regression for the advertised structured read schemas."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class StructuredReadProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_contact_chat_and_message_schemas_against_real_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            chat = '15555550001@s.whatsapp.net'
            stamp = '2026-10-27T10:00:00+00:00'
            with sqlite3.connect(Path(directory) / 'messages.db') as db:
                db.execute('CREATE TABLE chats(jid TEXT PRIMARY KEY,name TEXT,last_message_time TEXT)')
                db.execute('CREATE TABLE messages(id TEXT,chat_jid TEXT,sender TEXT,content TEXT,timestamp TEXT,is_from_me INTEGER,media_type TEXT)')
                db.execute('INSERT INTO chats VALUES(?,?,?)', (chat, 'Synthetic contact', stamp))
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', ('synthetic-id', chat, '15555550001', 'Synthetic content', stamp, 1, ''))
                for lid in ['99900000000001@lid', '99900000000002@lid']:
                    db.execute('INSERT INTO chats VALUES(?,?,?)', (lid, 'Ambiguous profile', stamp))
            env = {**os.environ, 'WHATSAPP_STORE_DIR': directory, 'WHATSAPP_CONTACTS_HELPER': '/nonexistent/synthetic-helper', 'WHATSAPP_SEND_ENABLED': 'false'}
            params = StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).resolve().parents[1] / 'main.py')], env=env)
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()

                    async def call(name, args):
                        result = await session.call_tool(name, args)
                        self.assertFalse(result.isError, result.content)
                        data = result.structuredContent or json.loads(result.content[0].text)
                        return data.get('result', data) if isinstance(data, dict) else data

                    contacts = await call('search_contacts', {'query': 'Synthetic'})
                    self.assertEqual(contacts[0]['phone_number'], '15555550001')
                    ambiguous = await call('search_contacts', {'query': 'Ambiguous'})
                    self.assertEqual(len(ambiguous), 2)
                    self.assertTrue(all(item['phone_number'] is None and item['jid'].endswith('@lid') for item in ambiguous))
                    unified = await call('search_contacts_chats_and_groups', {'query': 'Ambiguous'})
                    self.assertEqual(len(unified), 2)
                    self.assertTrue(all(item['phone_number'] is None for item in unified))
                    for include in [True, False]:
                        chats = await call('list_chats', {'query': 'Synthetic', 'include_last_message': include})
                        self.assertEqual(chats[0]['jid'], chat)
                        self.assertEqual(chats[0]['last_message_time'], stamp)
                        self.assertEqual(chats[0]['last_message'], 'Synthetic content' if include else None)
                        result = await call('get_chat', {'chat_jid': chat, 'include_last_message': include})
                        self.assertEqual(result['jid'], chat)
                        self.assertEqual(result['last_message'], 'Synthetic content' if include else None)
                    direct = await call('get_direct_chat_by_contact', {'sender_phone_number': '15555550001'})
                    self.assertEqual(direct['jid'], chat)
                    self.assertIsNone(await call('get_direct_chat_by_contact', {'sender_phone_number': '555550001'}))
                    self.assertIsNone(await call('get_direct_chat_by_contact', {'sender_phone_number': '99900000000001@lid'}))
                    matching = await call('get_contact_chats', {'jid': chat})
                    self.assertEqual(matching[0]['jid'], chat)
                    for context in [True, False]:
                        messages = await call('list_messages', {'chat_jid': chat, 'include_context': context})
                        self.assertEqual(messages[0]['id'], 'synthetic-id')
                        self.assertEqual(messages[0]['timestamp'], stamp)
                    around = await call('get_message_context', {'message_id': 'synthetic-id'})
                    self.assertEqual(around['message']['id'], 'synthetic-id')
                    self.assertEqual(around['before'], [])
                    self.assertIsNone(await call('get_chat', {'chat_jid': '15555559999@s.whatsapp.net'}))
                    self.assertIsNone(await call('get_direct_chat_by_contact', {'sender_phone_number': '15555559999'}))
