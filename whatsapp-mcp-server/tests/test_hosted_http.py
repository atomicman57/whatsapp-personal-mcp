import asyncio
import json
import os
import socket
import subprocess
import sys
import unittest

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from hosted_http import HostedApp

TOKEN = 'synthetic-hosted-token-never-a-real-credential'


class HostedTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_and_network_boundaries(self):
        reached = []

        async def downstream(scope, receive, send):
            reached.append(await receive())
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        app = HostedApp(downstream, 'https://mcp.example.test', TOKEN)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://mcp.example.test') as client:
            for headers, path, data, status in [
                ({}, '/mcp', b'{}', 401),
                ({'Authorization': 'Bearer wrong'}, '/mcp', b'{}', 401),
                ({'Authorization': 'Bearer ' + TOKEN, 'Host': 'attacker.test'}, '/mcp', b'{}', 403),
                ({'Authorization': 'Bearer ' + TOKEN, 'Origin': 'https://attacker.test'}, '/mcp', b'{}', 403),
                ({'Authorization': 'Bearer ' + TOKEN}, '/api/send', b'{}', 404),
                ({'Authorization': 'Bearer ' + TOKEN}, '/mcp?access_token=secret', b'{}', 404),
                ({'Authorization': 'Bearer ' + TOKEN}, '/mcp', b'x' * (1024 * 1024 + 1), 413),
                ([(b'Authorization', ('Bearer ' + TOKEN).encode()), (b'Authorization', b'Bearer second')], '/mcp', b'{}', 401),
            ]:
                response = await client.post(path, headers=headers, content=data)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertEqual(response.headers['strict-transport-security'], 'max-age=31536000')
            self.assertEqual(reached, [])
            accepted = await client.post('/mcp', headers={'Authorization': 'Bearer ' + TOKEN}, content=b'{}')
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.headers['cache-control'], 'no-store')
            self.assertEqual(accepted.headers['strict-transport-security'], 'max-age=31536000')
            self.assertEqual(len(reached), 1)
            self.assertEqual(reached[0]['body'], b'{}')

    def test_configuration_fails_closed(self):
        for url, token in [('http://mcp.example.test', TOKEN), ('https://u:p@mcp.example.test', TOKEN),
                           ('https://mcp.example.test/path', TOKEN), ('https://mcp.example.test', 'short')]:
            with self.assertRaises(ValueError):
                HostedApp(None, url, token)

    async def test_real_mcp_initialization_with_public_host_and_token(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        script = '''from mcp.server.fastmcp import FastMCP
from hosted_http import run_hosted_http
m=FastMCP('hosted-protocol-test')
@m.tool()
def probe() -> dict:
    return {'ok': True}
run_hosted_http(m, PORT)
'''.replace('PORT', str(port))
        process = subprocess.Popen([sys.executable, '-c', script], env={**os.environ,
            'WHATSAPP_MCP_PUBLIC_URL': 'https://mcp.example.test', 'WHATSAPP_MCP_TOKEN': TOKEN,
            'WHATSAPP_MCP_BIND_HOST': '127.0.0.1'}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            async with httpx.AsyncClient() as http:
                for _ in range(100):
                    try:
                        response = await http.get(f'http://127.0.0.1:{port}/mcp', headers={'Host': 'mcp.example.test'})
                        self.assertEqual(response.status_code, 401)
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(.05)
                else:
                    self.fail('Hosted MCP did not become ready')
            async with streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=httpx.AsyncClient(
                    headers={'Host': 'mcp.example.test', 'Authorization': 'Bearer ' + TOKEN})) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    result = await session.call_tool('probe', {})
                    self.assertFalse(result.isError)
                    data = result.structuredContent or json.loads(result.content[0].text)
                    self.assertEqual(data, {'ok': True})
        finally:
            process.terminate()
            process.wait(timeout=10)
