"""Synthetic full OAuth/PKCE lifecycle against a real hosted MCP process."""
import asyncio
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ORIGIN = 'https://mcp.example.test'
KEY = 'synthetic-private-owner-key-never-a-real-credential'
CALLBACK = 'https://client.example.test/oauth/callback'


def callback_url(response):
    return html.unescape(re.search(r'<a href="([^"]+)"', response.text).group(1))


class OAuthProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        script = '''import main
from hosted_http import run_hosted_http
@main.mcp.tool()
def probe() -> dict[str, bool]:
    return {'ok': True}
run_hosted_http(main.mcp, PORT)
'''.replace('PORT', str(port))
        self.process = subprocess.Popen([sys.executable, '-c', script], env={**os.environ,
            'WHATSAPP_STORE_DIR': self.directory.name, 'WHATSAPP_MCP_PUBLIC_URL': ORIGIN,
            'WHATSAPP_MCP_TOKEN': KEY, 'WHATSAPP_MCP_OAUTH_ENABLED': 'true',
            'WHATSAPP_MCP_BIND_HOST': '127.0.0.1'}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.http = httpx.AsyncClient(base_url=self.url, headers={'Host': 'mcp.example.test'}, timeout=10)
        for _ in range(100):
            try:
                response = await self.http.get('/.well-known/oauth-authorization-server')
                if response.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            await asyncio.sleep(.05)
        else:
            await self.asyncTearDown()
            self.fail('OAuth MCP did not become ready')

    async def asyncTearDown(self):
        await self.http.aclose()
        self.process.terminate()
        self.process.wait(timeout=10)
        self.directory.cleanup()

    async def register(self, name='Synthetic MCP client', method='none', redirect=CALLBACK):
        response = await self.http.post('/register', json={'client_name': name,
            'redirect_uris': [redirect], 'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'], 'token_endpoint_auth_method': method, 'scope': 'whatsapp'})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def prepare(self, client, resource=ORIGIN + '/mcp'):
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        response = await self.http.get('/authorize', params={'client_id': client['client_id'],
            'redirect_uri': CALLBACK, 'response_type': 'code', 'scope': 'whatsapp', 'state': 'synthetic-state',
            'code_challenge': challenge, 'code_challenge_method': 'S256', 'resource': resource})
        self.assertEqual(response.status_code, 302, response.text)
        location = urlsplit(response.headers['location'])
        form = await self.http.get(location.path + '?' + location.query)
        self.assertEqual(form.status_code, 200, form.text)
        values = {key: re.search(r'name="' + key + r'" value="([^"]+)"', form.text).group(1)
                  for key in ['request_id', 'csrf']}
        return verifier, values, form

    async def approve(self, values, key=KEY, origin=ORIGIN):
        return await self.http.post('/owner/authorize', data={**values, 'owner_key': key, 'authorize': 'yes'},
                                    headers={'Origin': origin})

    async def exchange(self, client, code, verifier, redirect=CALLBACK):
        data = {'client_id': client['client_id'], 'grant_type': 'authorization_code', 'code': code,
                'code_verifier': verifier, 'redirect_uri': redirect}
        if client.get('client_secret'):
            data['client_secret'] = client['client_secret']
        return await self.http.post('/token', data=data)

    async def grant(self, client):
        verifier, values, _ = await self.prepare(client)
        approved = await self.approve(values)
        self.assertEqual(approved.status_code, 200)
        result = parse_qs(urlsplit(callback_url(approved)).query)
        self.assertEqual(result['state'], ['synthetic-state'])
        token = await self.exchange(client, result['code'][0], verifier)
        self.assertEqual(token.status_code, 200, token.text)
        return token.json()

    async def probe(self, token):
        async with httpx.AsyncClient(headers={'Host': 'mcp.example.test', 'Authorization': 'Bearer ' + token}) as client:
            async with streamable_http_client(self.url + '/mcp', http_client=client) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    result = await session.call_tool('probe', {})
                    self.assertFalse(result.isError)
                    self.assertEqual(result.structuredContent, {'ok': True})

    async def refused(self, token):
        response = await self.http.get('/mcp', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 401)

    async def test_discovery_consent_pkce_and_one_time_codes(self):
        missing = await self.http.get('/mcp')
        self.assertEqual(missing.status_code, 401)
        self.assertIn('resource_metadata=', missing.headers['www-authenticate'])
        metadata = (await self.http.get('/.well-known/oauth-protected-resource/mcp')).json()
        self.assertEqual(metadata['resource'], ORIGIN + '/mcp')
        client = await self.register(name='<img src=x onerror=alert(1)>')
        verifier, values, form = await self.prepare(client)
        self.assertNotIn('<img', form.text)
        self.assertIn('&lt;img', form.text)
        self.assertIn('type="password"', form.text)
        self.assertEqual(form.headers['referrer-policy'], 'same-origin')
        self.assertIn("frame-ancestors 'none'", form.headers['content-security-policy'])
        self.assertNotIn(KEY, form.text)
        self.assertEqual((await self.approve(values, key='wrong')).status_code, 403)
        self.assertEqual((await self.approve(values, origin='https://attacker.test')).status_code, 403)
        self.assertEqual((await self.approve(values, origin='null')).status_code, 403)
        self.assertEqual((await self.approve({**values, 'csrf': 'wrong'})).status_code, 403)
        approved = await self.approve(values)
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.headers['referrer-policy'], 'no-referrer')
        self.assertIn("form-action 'none'", approved.headers['content-security-policy'])
        code = parse_qs(urlsplit(callback_url(approved)).query)['code'][0]
        self.assertEqual((await self.approve(values)).status_code, 410)
        self.assertEqual((await self.exchange(client, code, 'x' * 43)).status_code, 400)
        self.assertEqual((await self.exchange(client, code, verifier, redirect='https://attacker.test')).status_code, 400)
        other = await self.register()
        self.assertEqual((await self.exchange(other, code, verifier)).status_code, 400)
        response = await self.exchange(client, code, verifier)
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()
        self.assertEqual((await self.exchange(client, code, verifier)).status_code, 400)
        await self.probe(token['access_token'])
        await self.probe(KEY)  # Existing private maintenance access still works.
        raw_database = (Path(self.directory.name) / 'oauth.sqlite').read_bytes()
        for value in [KEY, code, token['access_token'], token['refresh_token']]:
            self.assertNotIn(value.encode(), raw_database)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['strict-transport-security'], 'max-age=31536000')

    async def test_refresh_rotation_replay_revocation_expiry_and_confidential_client(self):
        client = await self.register(method='client_secret_post')
        first = await self.grant(client)
        data = {'client_id': client['client_id'], 'client_secret': client['client_secret'],
                'grant_type': 'refresh_token', 'refresh_token': first['refresh_token']}
        escalated = await self.http.post('/token', data={**data, 'scope': 'whatsapp administrator'})
        self.assertEqual(escalated.status_code, 400)
        refreshed = await self.http.post('/token', data=data)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        second = refreshed.json()
        self.assertNotEqual(first['access_token'], second['access_token'])
        self.assertNotEqual(first['refresh_token'], second['refresh_token'])
        await self.refused(first['access_token'])
        await self.probe(second['access_token'])
        # Replaying a rotated refresh token invalidates the entire grant family.
        self.assertEqual((await self.http.post('/token', data=data)).status_code, 400)
        await self.refused(second['access_token'])
        third = await self.grant(client)
        missing_secret = await self.http.post('/revoke', data={'client_id': client['client_id'],
                                                              'token': third['refresh_token']})
        self.assertEqual(missing_secret.status_code, 401)
        await self.probe(third['access_token'])
        revoked = await self.http.post('/revoke', data={'client_id': client['client_id'],
            'client_secret': client['client_secret'], 'token': third['refresh_token']})
        self.assertEqual(revoked.status_code, 200)
        await self.refused(third['access_token'])
        fourth = await self.grant(client)
        with sqlite3.connect(Path(self.directory.name) / 'oauth.sqlite') as db:
            db.execute('UPDATE grants SET expires=0 WHERE digest=?', (hashlib.sha256(fourth['access_token'].encode()).hexdigest(),))
        await self.refused(fourth['access_token'])

    async def test_unsafe_redirects_unknown_paths_and_resource_audience(self):
        for uri in ['javascript:alert(1)', 'http://attacker.test/callback', 'https://u:p@client.example.test/callback', CALLBACK + '#fragment']:
            response = await self.http.post('/register', json={'redirect_uris': [uri],
                'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
                'token_endpoint_auth_method': 'none'})
            self.assertEqual(response.status_code, 400)
        client = await self.register()
        response = await self.http.get('/authorize', params={'client_id': client['client_id'],
            'redirect_uri': CALLBACK, 'response_type': 'code', 'scope': 'whatsapp',
            'code_challenge': 'x' * 43, 'code_challenge_method': 'S256', 'resource': 'https://attacker.test/mcp'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(response.headers['location']).query)['error'], ['invalid_request'])
        self.assertEqual((await self.http.get('/api/send')).status_code, 404)
        self.assertEqual((await self.http.get('/owner/authorize?request=unknown')).status_code, 410)

    async def test_parallel_code_exchange_and_expired_owner_request(self):
        client = await self.register()
        verifier, values, _ = await self.prepare(client)
        approved = await self.approve(values)
        code = parse_qs(urlsplit(callback_url(approved)).query)['code'][0]
        responses = await asyncio.gather(self.exchange(client, code, verifier), self.exchange(client, code, verifier))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 400])
        winner = next(response.json() for response in responses if response.status_code == 200)
        await self.probe(winner['access_token'])
        revoked = await self.http.post('/revoke', data={'client_id': client['client_id'],
                                                       'token': winner['refresh_token']})
        self.assertEqual(revoked.status_code, 200)
        await self.refused(winner['access_token'])
        _, expired, _ = await self.prepare(client)
        with sqlite3.connect(Path(self.directory.name) / 'oauth.sqlite') as db:
            db.execute('UPDATE grants SET expires=0 WHERE digest=?', (hashlib.sha256(expired['request_id'].encode()).hexdigest(),))
        self.assertEqual((await self.approve(expired)).status_code, 410)
