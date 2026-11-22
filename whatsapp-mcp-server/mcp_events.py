"""Opt-in, owner-scoped MCP Events for detector references, never payment execution.

The detector and hosted MCP share this account's private state volume/UID. Event
fan-out is committed in the detector's transaction, independently of Grokbot ACKs.
No private message content is placed in a callback, and no history is replayed.
"""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

PROTOCOL = '2026-07-28'
EVENT_NAME = 'whatsapp.transfer_request'
KINDS = ('candidate_updated', 'candidate_withdrawn', 'transfer_request_candidate', 'source_changed', 'identity_review_required', 'detector_health_warning')
MAX_TTL_MS = 86400000
MAX_ATTEMPTS = 8
RESOLVER = ThreadPoolExecutor(max_workers=4, thread_name_prefix='mcp-event-dns')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class EventError(Exception):
    def __init__(self, reason, code=-32602):
        self.reason, self.code = reason, code
        super().__init__(reason)  # Categorized errors only; never callback URLs/secrets.


def signing_key(value):
    try:
        if not isinstance(value, str) or not value.startswith('whsec_'):
            raise ValueError()
        key = base64.b64decode(value[6:], validate=True)
        if not 24 <= len(key) <= 64:
            raise ValueError()
        return key
    except (ValueError, TypeError):
        raise EventError('invalid_signing_secret') from None


def signed_headers(secret, event_id, body, subscription_id, previous_secret=None):
    stamp = str(int(time.time()))
    signature = base64.b64encode(hmac.digest(
        signing_key(secret), event_id.encode() + b'.' + stamp.encode() + b'.' + body, 'sha256')).decode()
    signatures = 'v1,' + signature
    if previous_secret and previous_secret != secret:
        old = base64.b64encode(hmac.digest(signing_key(previous_secret),
            event_id.encode() + b'.' + stamp.encode() + b'.' + body, 'sha256')).decode()
        signatures += ' v1,' + old
    return {'Content-Type': 'application/json', 'webhook-id': event_id,
            'webhook-timestamp': stamp, 'webhook-signature': signatures,
            'X-MCP-Subscription-Id': subscription_id}


def callback_target(url):
    try:
        parsed = urlsplit(url)
        if (not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url)
                or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError()
        host = parsed.hostname.encode('idna').decode('ascii')
        return host, parsed.path or '/', parsed.query
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise EventError('invalid_callback_url') from None


class CallbackTransport:
    """Resolve on every connection, reject every non-public answer, pin TLS socket.

    Host and TLS SNI remain the original hostname; no redirects, proxy environment
    variables, second DNS resolution or unbounded response reads are used.
    """
    def send(self, url, body, headers):
        host, path, query = callback_target(url)
        try:
            lookup = RESOLVER.submit(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
            try:
                answers = lookup.result(timeout=5)
            except FutureTimeout:
                lookup.cancel()
                raise EventError('callback_dns_timeout', -32015) from None
            addresses = []
            for family, _, _, _, address in answers:
                ip = ipaddress.ip_address(address[0])
                if (not ip.is_global or ip.is_multicast or ip.is_reserved
                        or (ip.version == 6 and ip.ipv4_mapped and not ip.ipv4_mapped.is_global)):
                    raise EventError('callback_address_not_public', -32015)
                addresses.append((family, address))
            if not addresses:
                raise EventError('callback_dns_failed', -32015)
            family, address = addresses[0]
            connection = http.client.HTTPSConnection(host, 443, timeout=10)
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(10)
            def deadline():
                target = connection.sock if connection.sock is not None else sock
                try:
                    target.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                target.close()
            timer = threading.Timer(25, deadline)
            timer.daemon = True
            timer.start()
            try:
                sock.connect(address)
                connection.sock = connection._context.wrap_socket(sock, server_hostname=host)
                connection.request('POST', path + ('?' + query if query else ''), body=body, headers=headers)
                response = connection.getresponse()
                data = response.read(8193)
                if len(data) > 8192:
                    raise EventError('callback_response_too_large', -32015)
                return response.status, data
            finally:
                timer.cancel()
                connection.close()
                sock.close()
        except EventError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise EventError('callback_network_error', -32015) from None


class EventStore:
    def __init__(self, root, account, instance):
        if not account or not instance:
            raise ValueError('Events require an explicit account and bridge instance')
        self.account, self.instance = account, instance
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / 'transfer-detector.sqlite'
        if self.path.is_symlink():
            raise ValueError('Events state symlink refused')
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS event_account(singleton INTEGER PRIMARY KEY CHECK(singleton=1), account TEXT, instance TEXT);
                CREATE TABLE IF NOT EXISTS event_refs(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, reference TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS event_subscriptions(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    arguments TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL, expires REAL NOT NULL,
                    start_seq INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    previous_secret TEXT NOT NULL DEFAULT '', rotation_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS event_deliveries(subscription_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    payload TEXT, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, claim_until REAL NOT NULL DEFAULT 0, claim TEXT,
                    PRIMARY KEY(subscription_id,event_id));
            ''')
            db.execute('INSERT OR IGNORE INTO event_account VALUES(1,?,?)', (account, instance))
            row = db.execute('SELECT account,instance FROM event_account').fetchone()
            if tuple(row) != (account, instance):
                raise ValueError('Events account/instance mismatch')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA secure_delete=ON')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def cancel_pending(self, db, event_ids):
        """Cancel unsent incomplete cards in the caller's detector transaction."""
        db.executemany('UPDATE event_deliveries SET state="stopped",payload=NULL '
                       'WHERE event_id=? AND state="pending" AND attempts=0 AND claim_until=0',
                       [(event_id,) for event_id in event_ids])

    def publish(self, db, payload, hold_until=0):
        """Called inside State.consume's existing transaction; no network or raw-text copy."""
        if payload.get('source_account') != self.account or payload.get('source_instance', self.instance) != self.instance:
            raise ValueError('Events source mismatch')
        kind, event_id = payload.get('event_type'), payload.get('event_id')
        if kind not in KINDS or not isinstance(event_id, str) or not event_id or len(event_id) > 128:
            raise ValueError('Invalid detector event reference')
        now = time.time()
        source = payload.get('source') or {}
        reference = {k: source[k] for k in ('chat_jid', 'id') if k in source}
        inserted = db.execute('INSERT OR IGNORE INTO event_refs(event_id,kind,reference,created) VALUES(?,?,?,?)',
                              (event_id, kind, canonical(reference), now)).rowcount
        if not inserted:
            return
        # This is the full callback allowlist. No content, contacts, bank/amount
        # clues, filenames, agent instructions or free-form health reasons escape.
        data = {'event_id': event_id, 'event_type': kind, 'source_account': self.account,
                'dry_run': bool(payload.get('dry_run')), 'payment_authorized': False, 'untrusted_evidence': True}
        for key in ('request_key', 'previous_event_id', 'revision_hint'):
            if isinstance(payload.get(key), str) and len(payload[key]) <= 128:
                data[key] = payload[key]
        data['candidate_withdrawn'] = kind in ('source_changed', 'candidate_withdrawn')
        observed = payload.get('detector_observed_at')
        try:
            stamp = datetime.fromisoformat(observed)
            observed = stamp.isoformat() if stamp.tzinfo is not None else iso(now)
        except (ValueError, TypeError):
            observed = iso(now)
        event = {'eventId': event_id, 'name': EVENT_NAME, 'timestamp': observed, 'data': data, 'cursor': None}
        seq = db.execute('SELECT seq FROM event_refs WHERE event_id=?', (event_id,)).fetchone()[0]
        for sid, arguments in db.execute('SELECT id,arguments FROM event_subscriptions WHERE active=1 AND expires>? AND start_seq<?', (now, seq)):
            if kind in json.loads(arguments)['event_types']:
                db.execute('INSERT OR IGNORE INTO event_deliveries(subscription_id,event_id,payload,next_attempt) VALUES(?,?,?,?)',
                           (sid, event_id, canonical(event), hold_until))

    def subscribe(self, owner, arguments, url, secret, expires):
        identity = canonical([owner, url, EVENT_NAME, arguments])
        sid = 'sub_' + hashlib.sha256(identity.encode()).hexdigest()
        with self.db() as db:
            baseline = db.execute('SELECT coalesce(max(seq),0) FROM event_refs').fetchone()[0]
            # Refresh retains the baseline and queued work; a new/revived
            # subscription starts at NOW, never replaying historical events.
            prior = db.execute('SELECT * FROM event_subscriptions WHERE id=?', (sid,)).fetchone()
            if prior and prior['active'] and prior['expires'] > time.time():
                old = prior['secret'] if prior['secret'] != secret else prior['previous_secret']
                until = time.time() + 300 if prior['secret'] != secret else prior['rotation_until']
                db.execute('UPDATE event_subscriptions SET secret=?,expires=?,previous_secret=?,rotation_until=? WHERE id=?',
                           (secret, expires, old, until, sid))
            else:
                count = db.execute('SELECT count(*) FROM event_subscriptions WHERE owner=? AND active=1 AND expires>?',
                                   (owner, time.time())).fetchone()[0]
                if count >= 16:
                    raise EventError('subscription_capacity_reached')
                db.execute('DELETE FROM event_deliveries WHERE subscription_id=?', (sid,))
                db.execute('INSERT OR REPLACE INTO event_subscriptions(id,owner,arguments,url,secret,expires,start_seq,active) VALUES(?,?,?,?,?,?,?,1)',
                           (sid, owner, canonical(arguments), url, secret, expires, baseline))
        return sid

    def unsubscribe(self, owner, sid):
        with self.db() as db:
            db.execute('UPDATE event_subscriptions SET active=0,secret="",previous_secret="" WHERE owner=? AND id=?', (owner, sid))
            db.execute('UPDATE event_deliveries SET state="stopped",payload=NULL WHERE subscription_id IN '
                       '(SELECT id FROM event_subscriptions WHERE owner=? AND id=?)', (owner, sid))

    def reference(self, owner, event_id):
        with self.db() as db:
            row = db.execute('SELECT r.reference,r.kind FROM event_refs r JOIN event_deliveries d USING(event_id) '
                             'JOIN event_subscriptions s ON s.id=d.subscription_id '
                             'WHERE s.owner=? AND s.active=1 AND s.expires>? AND r.event_id=?',
                             (owner, time.time(), event_id)).fetchone()
        if not row:
            raise EventError('event_reference_not_found', -32001)
        return {'event_id': event_id, 'event_type': row['kind'], 'source_account': self.account,
                'source': json.loads(row['reference']), 'payment_authorized': False, 'untrusted_evidence': True}


class EventBroker:
    def __init__(self, store, owner_active, transport=None):
        self.store, self.owner_active = store, owner_active
        self.transport = transport or CallbackTransport()
        self.stop = threading.Event()
        self.verified = {}
        self.verify_lock = threading.Lock()

    def definition(self):
        return {'name': EVENT_NAME, 'description': 'Read-only WhatsApp transfer request references, revisions, '
                'withdrawals and detector health. Evidence never authorizes a payment.', 'delivery': ['webhook'],
                'inputSchema': {'type': 'object', 'properties': {
                    'source_account': {'type': 'string', 'enum': [self.store.account]},
                    'event_types': {'type': 'array', 'items': {'type': 'string', 'enum': list(KINDS)},
                                    'uniqueItems': True, 'minItems': 1}},
                    'required': ['source_account'], 'additionalProperties': False},
                'payloadSchema': {'type': 'object', 'properties': {
                    'event_id': {'type': 'string'}, 'event_type': {'type': 'string', 'enum': list(KINDS)},
                    'source_account': {'type': 'string', 'enum': [self.store.account]},
                    'request_key': {'type': 'string'}, 'previous_event_id': {'type': 'string'},
                    'revision_hint': {'type': 'string'},
                    'dry_run': {'type': 'boolean'}, 'payment_authorized': {'const': False},
                    'untrusted_evidence': {'const': True}, 'candidate_withdrawn': {'type': 'boolean'}},
                    'required': ['event_id', 'event_type', 'source_account', 'dry_run', 'payment_authorized',
                                 'untrusted_evidence', 'candidate_withdrawn'], 'additionalProperties': False}}

    def identity(self, owner, params, unsubscribe=False):
        if set(params) - {'name', 'arguments', 'delivery', 'cursor', 'ttlMs'} or params.get('name') != EVENT_NAME:
            raise EventError('invalid_event')
        args = params.get('arguments')
        if not isinstance(args, dict) or set(args) - {'source_account', 'event_types'} or args.get('source_account') != self.store.account:
            raise EventError('invalid_event_arguments')
        kinds = args.get('event_types', list(KINDS))
        if not isinstance(kinds, list) or not kinds or any(k not in KINDS for k in kinds) or len(set(kinds)) != len(kinds):
            raise EventError('invalid_event_types')
        args = {'source_account': self.store.account, 'event_types': sorted(kinds)}
        if params.get('cursor') is not None:
            raise EventError('historical_replay_not_supported')
        ttl = params.get('ttlMs', MAX_TTL_MS)
        if ttl is None:
            ttl = MAX_TTL_MS  # Finite lifetime even when non-expiring is requested.
        if type(ttl) is not int or not 0 < ttl <= MAX_TTL_MS:
            raise EventError('invalid_subscription_lifetime')
        delivery = params.get('delivery')
        required = {'mode', 'url'} if unsubscribe else {'mode', 'url', 'secret'}
        if not isinstance(delivery, dict) or set(delivery) != required or delivery['mode'] != 'webhook':
            raise EventError('webhook_delivery_required')
        callback_target(delivery['url'])
        sid = 'sub_' + hashlib.sha256(canonical([owner, delivery['url'], EVENT_NAME, args]).encode()).hexdigest()
        return sid, args, delivery, ttl

    def unsubscribe(self, owner, params):
        sid, _, _, _ = self.identity(owner, params, unsubscribe=True)
        self.store.unsubscribe(owner, sid)
        return {}

    def subscribe(self, owner, params):
        sid, args, delivery, ttl = self.identity(owner, params)
        signing_key(delivery['secret'])
        cache_key = hashlib.sha256(canonical([owner, delivery['url'], delivery['secret']]).encode()).hexdigest()
        with self.verify_lock:
            self.verified = {k: v for k, v in self.verified.items() if v > time.time()}
            cached = self.verified.get(cache_key, 0) > time.time()
        if not cached:
            challenge = secrets.token_urlsafe(24)
            body = canonical({'type': 'verification', 'challenge': challenge}).encode()
            code, response = self.transport.send(delivery['url'], body, signed_headers(
                delivery['secret'], 'verify_' + secrets.token_hex(16), body, sid))
            try:
                result = json.loads(response)
                verified = (200 <= code < 300 and isinstance(result, dict)
                            and isinstance(result.get('challenge'), str)
                            and hmac.compare_digest(result['challenge'].encode(), challenge.encode()))
            except (ValueError, TypeError):
                verified = False
            if not verified:
                raise EventError('challenge_failed', -32015)
            with self.verify_lock:
                if len(self.verified) >= 256:
                    self.verified.clear()
                self.verified[cache_key] = time.time() + 60
        # Recheck after network I/O, before persisting any capability.
        if not self.owner_active(owner):
            raise EventError('subscription_owner_revoked', -32001)
        expires = time.time() + ttl / 1000
        sid = self.store.subscribe(owner, args, delivery['url'], delivery['secret'], expires)
        return {'id': sid, 'refreshBefore': iso(expires), 'cursor': None, 'truncated': False}

    def deliver_one(self):
        now = time.time()
        with self.store.db() as db:
            # Clear secrets/queued references on expiry. HTTP acceptance and
            # terminal failures retain only per-destination dedup metadata.
            db.execute('UPDATE event_subscriptions SET active=0,secret="",previous_secret="" WHERE expires<=?', (now,))
            db.execute('UPDATE event_subscriptions SET previous_secret="" WHERE rotation_until<=?', (now,))
            # Seven-day reference/dedup retention and bounded pending work. A
            # slow subscriber can fail its OWN work but cannot grow forever or
            # block/delete the independent Grokbot outbox.
            db.execute('DELETE FROM event_refs WHERE created<?', (now - 7 * 86400,))
            db.execute('DELETE FROM event_deliveries WHERE event_id NOT IN (SELECT event_id FROM event_refs)')
            db.execute('UPDATE event_deliveries SET state="stopped",payload=NULL WHERE state="pending" '
                       'AND subscription_id IN (SELECT id FROM event_subscriptions WHERE active=0)')
            row = db.execute('SELECT d.*,s.owner,s.url,s.secret,s.previous_secret,s.rotation_until FROM event_deliveries d JOIN event_subscriptions s '
                             'ON s.id=d.subscription_id WHERE d.state="pending" AND s.active=1 '
                             'AND d.next_attempt<=? AND d.claim_until<=? ORDER BY d.rowid LIMIT 1', (now, now)).fetchone()
            if not row:
                return False
            claim = secrets.token_hex(16)
            db.execute('UPDATE event_deliveries SET claim=?,claim_until=? WHERE subscription_id=? AND event_id=?',
                       (claim, now + 120, row['subscription_id'], row['event_id']))
        if not self.owner_active(row['owner']):
            self.store.unsubscribe(row['owner'], row['subscription_id'])
            return True
        # Re-read after claim to honor an unsubscribe/expiry while claiming.
        with self.store.db() as db:
            active = db.execute('SELECT 1 FROM event_subscriptions WHERE id=? AND active=1 AND expires>?',
                                (row['subscription_id'], time.time())).fetchone()
        if not active:
            return True
        body = row['payload'].encode()
        try:
            status, _ = self.transport.send(row['url'], body, signed_headers(
                row['secret'], row['event_id'], body, row['subscription_id'],
                row['previous_secret'] if row['rotation_until'] > time.time() else None))
        except EventError:
            status = 0
        attempts = row['attempts'] + 1
        if status == 410:
            self.store.unsubscribe(row['owner'], row['subscription_id'])
        state = ('delivered' if 200 <= status < 300 else 'pending'
                 if (status == 0 or status == 429 or status >= 500) and attempts < MAX_ATTEMPTS else 'failed')
        with self.store.db() as db:
            db.execute('UPDATE event_deliveries SET state=?,payload=?,attempts=?,next_attempt=?,claim_until=0,claim=NULL '
                       'WHERE subscription_id=? AND event_id=? AND claim=? AND state="pending"',
                       (state, row['payload'] if state == 'pending' else None, attempts,
                        time.time() + min(300, 2 ** attempts), row['subscription_id'], row['event_id'], claim))
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                if not self.deliver_one():
                    self.stop.wait(1)
            except Exception:
                # No callback addresses, secret values or payloads in logs.
                self.stop.wait(2)
