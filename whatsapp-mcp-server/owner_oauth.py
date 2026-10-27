"""Owner-approved OAuth using the MCP SDK's PKCE, token and DCR handlers.

Only the owner's existing high-entropy maintenance key can approve a client.
Authorization codes and bearer/refresh tokens are stored as SHA-256 digests.
"""
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse

SCOPE = 'whatsapp'
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class OwnerOAuthProvider:
    def __init__(self, public_url, owner_key, store_dir):
        parsed = urlsplit(public_url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError('OAuth requires a canonical HTTPS origin')
        if len(owner_key) < 32 or not owner_key.isascii():
            raise ValueError('A private high-entropy owner connection key is required')
        self.origin = public_url.rstrip('/')
        self.resource = self.origin + '/mcp'
        self.owner_key = owner_key
        root = Path(store_dir)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database = root / 'oauth.sqlite'
        if self.database.is_symlink():
            raise ValueError('OAuth state must not be a symlink')
        fd = os.open(self.database, os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        self.database.chmod(0o600)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY, data TEXT NOT NULL, created INTEGER NOT NULL, approved INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS grants(kind TEXT NOT NULL, digest TEXT PRIMARY KEY, client TEXT NOT NULL, data TEXT NOT NULL, expires INTEGER NOT NULL, family TEXT NOT NULL DEFAULT "", active INTEGER NOT NULL DEFAULT 1)')
            db.execute('CREATE INDEX IF NOT EXISTS grants_family ON grants(family)')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def clean(self, db):
        db.execute('DELETE FROM grants WHERE expires < ?', (int(time.time()),))

    def put(self, db, kind, raw, client, data, expires, family=''):
        db.execute('INSERT INTO grants(kind,digest,client,data,expires,family) VALUES(?,?,?,?,?,?)',
                   (kind, digest(raw), client, json.dumps(data), expires, family))

    def row(self, db, kind, raw):
        return db.execute('SELECT * FROM grants WHERE kind=? AND digest=?', (kind, digest(raw))).fetchone()

    async def get_client(self, client_id):
        with self.db() as db:
            row = db.execute('SELECT data FROM clients WHERE id=?', (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row['data']) if row else None

    async def register_client(self, client_info):
        if not client_info.redirect_uris or len(client_info.redirect_uris) > 10:
            raise RegistrationError('invalid_redirect_uri', 'One to ten exact redirect URIs are required')
        for value in client_info.redirect_uris:
            url = urlsplit(str(value))
            if (url.username or url.password or url.fragment or not url.hostname or
                    not (url.scheme == 'https' or (url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')))):
                raise RegistrationError('invalid_redirect_uri', 'Use HTTPS or a loopback callback')
        if client_info.scope and set(client_info.scope.split()) - {SCOPE}:
            raise RegistrationError('invalid_client_metadata', 'Unsupported scope')
        with self.db() as db:
            self.clean(db)
            # Preserve approved clients. Bound anonymous DCR storage without giving
            # unapproved clients any access to the WhatsApp tools.
            old = db.execute('SELECT id FROM clients WHERE approved=0 ORDER BY created DESC LIMIT -1 OFFSET 127').fetchall()
            for row in old:
                db.execute('DELETE FROM grants WHERE client=?', (row['id'],))
                db.execute('DELETE FROM clients WHERE id=?', (row['id'],))
            db.execute('INSERT INTO clients(id,data,created) VALUES(?,?,?)',
                       (client_info.client_id, client_info.model_dump_json(), int(time.time())))

    async def authorize(self, client, params):
        if params.resource not in (None, self.resource):
            raise AuthorizeError('invalid_request', 'Unexpected resource audience')
        if set(params.scopes or [SCOPE]) - {SCOPE}:
            raise AuthorizeError('invalid_scope', 'Unsupported scope')
        request_id = secrets.token_urlsafe(32)
        with self.db() as db:
            self.clean(db)
            db.execute('DELETE FROM grants WHERE digest IN (SELECT digest FROM grants WHERE kind="request" ORDER BY expires DESC LIMIT -1 OFFSET 255)')
            self.put(db, 'request', request_id, client.client_id, params.model_dump(mode='json'), int(time.time()) + 600)
        return self.origin + '/owner/authorize?request=' + request_id

    def csrf(self, request_id):
        return hmac.new(self.owner_key.encode(), ('owner-consent:' + request_id).encode(), hashlib.sha256).hexdigest()

    async def owner_authorize(self, request):
        if request.method == 'POST':
            if request.headers.get('origin') != self.origin:
                return HTMLResponse('Request refused', status_code=403)
            form = await request.form()
            request_id = str(form.get('request_id', ''))
            if not secrets.compare_digest(str(form.get('csrf', '')), self.csrf(request_id)):
                return HTMLResponse('Request refused', status_code=403)
            key = str(form.get('owner_key', ''))
            if not secrets.compare_digest(digest(key), digest(self.owner_key)) or form.get('authorize') != 'yes':
                return HTMLResponse('Connection key or authorization missing', status_code=403)
            with self.db() as db:
                row = self.row(db, 'request', request_id)
                if not row or not row['active'] or row['expires'] < time.time():
                    return HTMLResponse('This connection request has expired. Start a new connection.', status_code=410)
                params = AuthorizationParams.model_validate_json(row['data'])
                code = secrets.token_urlsafe(32)
                expires = int(time.time()) + 120
                authorization = AuthorizationCode(code=code, client_id=row['client'], scopes=params.scopes or [SCOPE],
                    expires_at=expires, code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=self.resource, subject='owner')
                db.execute('UPDATE grants SET active=0 WHERE digest=?', (row['digest'],))
                db.execute('UPDATE clients SET approved=1 WHERE id=?', (row['client'],))
                self.put(db, 'code', code, row['client'], authorization.model_dump(mode='json', exclude={'code'}), expires)
            return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                                    status_code=303, headers={'Referrer-Policy': 'no-referrer'})
        request_id = request.query_params.get('request', '')
        with self.db() as db:
            row = self.row(db, 'request', request_id)
        if not row or not row['active'] or row['expires'] < time.time():
            return HTMLResponse('Connection request missing or expired', status_code=410)
        client = await self.get_client(row['client'])
        if client is None:
            return HTMLResponse('Connection request expired', status_code=410)
        params = AuthorizationParams.model_validate_json(row['data'])
        nonce = secrets.token_urlsafe(18)
        body = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Second WhatsApp</title><style nonce="NONCE">body{font:16px system-ui;max-width:650px;margin:10vh auto;padding:24px;line-height:1.5}input,button{font:inherit;padding:12px;box-sizing:border-box;width:100%;margin:8px 0}code{overflow-wrap:anywhere}button{cursor:pointer}</style>
<h1>Connect Second WhatsApp</h1><p>Client: <strong>CLIENT</strong></p><p>Return address: <code>CALLBACK</code></p>
<p>This connector can read contacts, messages and media, and send under the server's configured recipient policy. Sends have no per-message approval prompts.</p>
<form method="post" action="/owner/authorize"><input type="hidden" name="request_id" value="REQUEST"><input type="hidden" name="csrf" value="CSRF">
<label for="owner_key">Private owner connection key</label><input id="owner_key" type="password" name="owner_key" autocomplete="off" required>
<p>The key is sent only to this server. Keep it out of assistant chats.</p><button name="authorize" value="yes">Authorize this connector</button></form></html>'''
        replacements = {'NONCE': nonce, 'CLIENT': html.escape(client.client_name or 'MCP client'),
                        'CALLBACK': html.escape(str(params.redirect_uri)), 'REQUEST': html.escape(request_id),
                        'CSRF': self.csrf(request_id)}
        for key, value in replacements.items():
            body = body.replace(key, value)
        return HTMLResponse(body, headers={'Content-Security-Policy': "default-src 'none'; style-src 'nonce-" + nonce + "'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            # HTML form navigation under no-referrer sends Origin:null. Keep
            # same-origin POSTs compatible with the strict Origin/CSRF checks,
            # while withholding the consent URL from cross-origin navigation.
            'Referrer-Policy': 'same-origin', 'X-Content-Type-Options': 'nosniff'})

    async def load_authorization_code(self, client, authorization_code):
        with self.db() as db:
            row = self.row(db, 'code', authorization_code)
        if not row or not row['active'] or row['expires'] < time.time() or row['client'] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **json.loads(row['data']))

    def issue(self, db, client_id, scopes, family):
        self.clean(db)
        now = int(time.time())
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(48)
        access_info = AccessToken(token=access, client_id=client_id, scopes=scopes, expires_at=now + ACCESS_SECONDS,
                                  resource=self.resource, subject='owner')
        refresh_info = RefreshToken(token=refresh, client_id=client_id, scopes=scopes,
                                    expires_at=now + REFRESH_SECONDS, subject='owner')
        self.put(db, 'access', access, client_id, access_info.model_dump(mode='json', exclude={'token'}), now + ACCESS_SECONDS, family)
        self.put(db, 'refresh', refresh, client_id, refresh_info.model_dump(mode='json', exclude={'token'}), now + REFRESH_SECONDS, family)
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=ACCESS_SECONDS, scope=' '.join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        with self.db() as db:
            row = self.row(db, 'code', authorization_code.code)
            if not row or not row['active'] or row['expires'] < time.time() or row['client'] != client.client_id:
                raise TokenError('invalid_grant', 'Authorization code already used or expired')
            db.execute('UPDATE grants SET active=0 WHERE digest=?', (row['digest'],))
            return self.issue(db, client.client_id, authorization_code.scopes, secrets.token_hex(16))

    async def load_refresh_token(self, client, refresh_token):
        with self.db() as db:
            row = self.row(db, 'refresh', refresh_token)
            if row and row['client'] == client.client_id and not row['active'] and row['expires'] >= time.time():
                # Reuse of a rotated refresh token revokes the entire grant family.
                db.execute('UPDATE grants SET active=0 WHERE family=?', (row['family'],))
                return None
        if not row or not row['active'] or row['expires'] < time.time() or row['client'] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **json.loads(row['data']))

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self.db() as db:
            row = self.row(db, 'refresh', refresh_token.token)
            if not row or not row['active'] or row['expires'] < time.time() or row['client'] != client.client_id:
                raise TokenError('invalid_grant', 'Refresh token already used or expired')
            original = json.loads(row['data'])['scopes']
            if set(scopes) - set(original):
                raise TokenError('invalid_scope', 'Scope escalation is not allowed')
            db.execute('UPDATE grants SET active=0 WHERE family=?', (row['family'],))
            return self.issue(db, client.client_id, scopes, row['family'])

    async def load_access_token(self, token):
        if secrets.compare_digest(digest(token), digest(self.owner_key)):
            return AccessToken(token=token, client_id='owner-maintenance', scopes=[SCOPE], resource=self.resource, subject='owner')
        with self.db() as db:
            row = self.row(db, 'access', token)
        if not row or not row['active'] or row['expires'] < time.time():
            return None
        value = AccessToken(token=token, **json.loads(row['data']))
        return value if value.resource == self.resource else None

    async def revoke_token(self, token):
        with self.db() as db:
            row = db.execute('SELECT family FROM grants WHERE digest=? AND kind IN ("access","refresh")', (digest(token.token),)).fetchone()
            if row:
                db.execute('UPDATE grants SET active=0 WHERE family=?', (row['family'],))


def configure_oauth():
    provider = OwnerOAuthProvider(os.environ.get('WHATSAPP_MCP_PUBLIC_URL', ''),
                                  os.environ.get('WHATSAPP_MCP_TOKEN', ''), os.environ['WHATSAPP_STORE_DIR'])
    settings = AuthSettings(issuer_url=provider.origin, resource_server_url=provider.resource,
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True), required_scopes=[SCOPE])
    return provider, settings
