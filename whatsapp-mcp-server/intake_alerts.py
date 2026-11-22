"""Self-chat alerts only. No payment, read receipt, historical replay, or send retry."""
import json
import os
import re
import threading
from urllib.parse import urlsplit

import requests
from transfer_telemetry import is_test_event


def enabled():
    return os.environ.get('INTAKE_SELF_ALERT_ENABLED', '').lower() == 'true'


def label(value, fallback):
    # Names/banks are untrusted. Remove digits, control characters and line breaks
    # so a contact name can never leak an account or inject a second instruction.
    value = re.sub(r'[^\w .\'-]', '', str(value or ''), flags=re.UNICODE)
    value = ''.join(c for c in value if not c.isdigit())
    return ' '.join(value.split())[:70] or fallback


def format_alert(card):
    name = label(card.get('requester_display_name'), 'unknown sender')
    if name.casefold() in ('unknown requester', 'unknown sender'):
        name = 'unknown sender'
    claimed = card.get('claimed_name')
    if claimed:
        name += ' (claims to be ' + label(claimed, 'unknown sender') + ')'
    amount = card.get('amount') or {}
    value = str(amount.get('value', ''))
    if re.fullmatch(r'\d{1,13}(?:\.\d{1,2})?', value):
        whole, dot, fraction = value.partition('.')
        # Group digits without rounding or inventing a currency.
        money = f'{int(whole):,}' + (dot + fraction if dot else '')
        currency = amount.get('currency')
        if currency in ('NGN', 'USD', 'EUR', 'GBP'):
            money = currency + ' ' + money
    else:
        money = 'amount not given'
    account = card.get('account_number')
    tail = account[-4:] if isinstance(account, str) and re.fullmatch(r'\d{10,11}', account) else None
    return (f'{name}: {money}; {label(card.get("bank"), "bank not given")}; '
            f'{"account ending " + tail if tail else "account not given"}. '
            'Approve in Transfer Request Bot')


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS intake_alert_jobs (
        event_id TEXT PRIMARY KEY, payload TEXT, status TEXT NOT NULL DEFAULT 'pending')''')


def queue(db, payload):
    """Called only for newly consumed events, in the detector write transaction."""
    if not enabled():
        return
    test = is_test_event(payload)
    if test and os.environ.get('INTAKE_SELF_ALERT_TEST_ENABLED', '').lower() != 'true':
        return
    event = payload.get('event_type')
    if payload.get('candidate_withdrawn'):
        job = {'delete_event_id': payload.get('previous_event_id')}
    elif event in ('transfer_request_candidate', 'candidate_updated') and payload.get('card'):
        changed = set(payload.get('changed_fields', []))
        job = {'event_id': payload['event_id'], 'request_key': payload['request_key'],
               'revision': payload.get('revision', 1), 'text': format_alert(payload['card']),
               'test': test, 'suppress': event == 'candidate_updated' and not bool(
                   changed & {'amount', 'bank', 'account_number'})}
    else:
        return
    db.execute('INSERT OR IGNORE INTO intake_alert_jobs(event_id,payload) VALUES(?,?)',
               (payload['event_id'], json.dumps(job)))


class AlertClient:
    def __init__(self):
        # Both account processes explicitly address main. No secondary fallback.
        self.base = os.environ['INTAKE_SELF_ALERT_MAIN_URL'].rstrip('/')
        url = urlsplit(self.base)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or
                url.password or url.query or url.fragment or url.path != '/api'):
            raise ValueError('invalid_main_alert_url')
        token = os.environ['INTAKE_SELF_ALERT_TOKEN']
        if len(token) < 32:
            raise ValueError('invalid_alert_token')
        if url.scheme == 'http' and url.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('remote_alerts_require_https_or_loopback_tunnel')
        self.headers = {'Authorization': 'Bearer ' + token}

    def request(self, method, path, body=None, params=None):
        response = requests.request(method, self.base + '/' + path, json=body, params=params,
                                    headers=self.headers, timeout=(2, 12), allow_redirects=False)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or 'success' not in result:
            raise ValueError('alert_operation_failed')
        return result

    def send(self, job):
        identity = self.request('GET', 'identity')['data']
        jid = identity.get('jid', '')
        if not jid.endswith('1755@s.whatsapp.net'):
            raise ValueError('main_identity_required')
        return self.request('POST', 'intake-alerts', {**job, 'chat_jid': jid})

    def delete(self, event_id, reason='edited'):
        return self.request('POST', 'intake-alerts/delete', {'event_id': event_id, 'reason': reason})


class AlertWorker:
    def __init__(self, state, client):
        self.state, self.client = state, client
        self.stop = threading.Event()
        # Startup never replays queued work from a previous detector process.
        with state.lock, state.db() as db:
            db.execute("UPDATE intake_alert_jobs SET status='abandoned',payload=NULL WHERE status='pending'")

    def one(self):
        with self.state.lock, self.state.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT event_id,payload FROM intake_alert_jobs WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return False
            # Never reclaim on restart: an interrupted HTTP send is ambiguous.
            db.execute("UPDATE intake_alert_jobs SET status='claimed',payload=NULL WHERE event_id=?", (row[0],))
        try:
            job = json.loads(row[1])
            if 'delete_event_id' in job:
                result = self.client.delete(job['delete_event_id'])
            else:
                result = self.client.send(job)
            if isinstance(result, dict) and result.get('success') is False:
                raise ValueError('alert_operation_failed')
            status = 'completed'
        except Exception:
            status = 'unconfirmed'  # Never log exception text or request contents.
        with self.state.lock, self.state.db() as db:
            db.execute('UPDATE intake_alert_jobs SET status=? WHERE event_id=?', (status, row[0]))
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                if not self.one():
                    self.stop.wait(.05)
            except Exception:
                self.stop.wait(1)


def _owner_alert(method, path, body=None, params=None):
    from mcp.server.auth.middleware.auth_context import get_access_token
    access = get_access_token()
    if not access or getattr(access, 'subject', None) != 'owner' or 'whatsapp' not in access.scopes:
        return {'success': False, 'code': 'owner_auth_required'}
    try:
        return AlertClient().request(method, path, body, params)
    except (KeyError, ValueError, requests.RequestException):
        return {'success': False, 'code': 'intake_alert_unavailable'}


def delete_intake_alert(event_id: str, reason: str = 'approved') -> dict:
    """Owner-only delete all live alerts for this event's request. No payment approval.

    Returns message IDs, deleted_at and delete_mode (revoke+delete_for_me or
    delete_for_me_only). reason is
    a short workflow label such as approved, rejected, edited or withdrawn.
    Failed deletes remain live and may be explicitly retried with this tool.
    """
    if not isinstance(event_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', event_id):
        return {'success': False, 'code': 'invalid_event_id'}
    if not isinstance(reason, str) or not re.fullmatch(r'[a-z_]{1,32}', reason):
        return {'success': False, 'code': 'invalid_reason'}
    return _owner_alert('POST', 'intake-alerts/delete', {'event_id': event_id, 'reason': reason})


def list_intake_alerts(event_id: str) -> dict:
    """Owner-only alert metadata for every revision of this event's request."""
    if not isinstance(event_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', event_id):
        return {'success': False, 'code': 'invalid_event_id'}
    return _owner_alert('GET', 'intake-alerts', params={'event_id': event_id})


def cleanup_intake_alert_tombstones(since: str | None = None, dry_run: bool = True) -> dict:
    """Owner-only delete-for-me cleanup of deleted/revoked ledger alerts in self-chat.

    Defaults to dry-run: returns count and eligible rows without WhatsApp changes.
    since filters alert sent_at inclusively, as an ISO timestamp with timezone.
    Completed rows are skipped on retries. No non-ledger messages are touched.
    WhatsApp acknowledgment does not prove phone/linked-device UI synchronization.
    """
    from datetime import datetime
    if not isinstance(dry_run, bool):
        return {'success': False, 'code': 'invalid_dry_run'}
    body = {'dry_run': dry_run}
    if since is not None:
        try:
            if not isinstance(since, str):
                raise ValueError('invalid_since')
            parsed = datetime.fromisoformat(since.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('timezone_required')
            body['since'] = parsed.isoformat()
        except ValueError:
            return {'success': False, 'code': 'invalid_since'}
    return _owner_alert('POST', 'intake-alerts/cleanup', body)
