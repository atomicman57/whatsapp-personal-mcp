"""Owner card evidence. All values are untrusted candidates, never approval."""
import re
import json
import http.client
import socket
from concurrent.futures import TimeoutError as FutureTimeout
import threading
import time
from urllib.parse import urlsplit

from mcp_events import RESOLVER


def card_for(row, clues, aliases, rows):
    accounts = clues.get('account_number_candidates', [])
    banks = clues.get('bank_name_candidates', [])
    amounts = clues.get('amount_candidates', [])
    saved = bool(aliases.get('requester_is_saved_contact'))
    saved_name = (aliases.get('saved_contact_name') or '')[:120] if saved else None
    purpose = None
    for item in rows + [row]:
        if item['id'] not in clues.get('source_message_ids', []):
            continue
        text = item.get('content') or ''
        match = re.search(r'\bfor\s+(.{1,240})', text, re.I)
        if match:
            purpose = re.split(r'[.;\n]|\b(?:bank|acct|account)\b|\d{10,}', match[1], maxsplit=1, flags=re.I)[0].strip()[:240] or None
        elif re.match(r'^(?:waybill|drinks|delivery|transport|gas|fuel|rent|school fees|plumbing)\b', text, re.I):
            purpose = text[:240]
    return {'requester_display_name': saved_name or (row.get('chat_name') or 'Unknown requester')[:120],
            'amount': ({'value': amounts[0]['amount'], 'currency': amounts[0].get('currency_hint')}
                       if amounts and not clues.get('amount_ambiguous') else None),
            'bank': banks[0][:80] if len(banks) == 1 else None,
            'account_number': accounts[0] if len(accounts) == 1 else None,
            'account_name': None, 'account_name_reason': 'name_enquiry_disabled', 'purpose': purpose,
            'requester_is_saved_contact': saved, 'saved_contact_name': saved_name,
            'saved_contact_phone': aliases.get('phone_number') if saved else None}


class NameEnquiry:
    """Single background worker, durable jobs, no work on the card delivery path."""
    def __init__(self, state, url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query or parsed.port not in (None, 443):
            raise ValueError('invalid_name_enquiry_url')
        self.state, self.url = state, url
        self.stop = threading.Event()
        with state.lock, state.db() as db:
            db.execute("UPDATE name_enquiries SET status='pending' WHERE status='running'")

    def one(self):
        with self.state.lock, self.state.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT event_id,payload FROM name_enquiries WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return False
            db.execute("UPDATE name_enquiries SET status='running' WHERE event_id=?", (row[0],))
        from transfer_detection import digest
        payload = json.loads(row[1])
        card = payload['card']
        name = self.lookup(card)
        # A late success emits a revision; never modifies a payload already in flight.
        if name:
            card.update(account_name=name, account_name_reason=None)
            payload.update(event_id=digest(row[0], 'name-enquiry', name), event_type='candidate_updated',
                           previous_event_id=row[0], revision_hint='replace_card_for_request_key',
                           changed_fields=['account_name'])
            self.state.enriched(payload, row[0])
        with self.state.lock, self.state.db() as db:
            db.execute("UPDATE name_enquiries SET status=?,payload=NULL WHERE event_id=?",
                       ('resolved' if name else 'unavailable', row[0]))
        return True

    def lookup(self, card):
        """Two-second wall-clock budget includes DNS, TLS, headers and bounded body."""
        parsed = urlsplit(self.url)
        host, port = parsed.hostname, parsed.port or 443
        deadline = time.monotonic() + 2
        lookup = RESOLVER.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        connection = None
        sock = None
        tls_sock = None
        timer = None
        try:
            answers = lookup.result(timeout=max(.001, deadline-time.monotonic()))
            family, kind, proto, _, address = answers[0]
            sock = socket.socket(family, kind, proto)
            sock.settimeout(max(.001, deadline-time.monotonic()))
            connection = http.client.HTTPSConnection(host, port, timeout=max(.001, deadline-time.monotonic()))
            def expire():
                target = tls_sock if tls_sock is not None else sock
                try:
                    target.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                target.close()
            timer = threading.Timer(max(.001, deadline-time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            sock.connect(address)
            # Pin the resolved socket; TLS still verifies the configured host.
            tls_sock = connection._context.wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
            connection.sock = tls_sock
            tls_sock.do_handshake()
            body = json.dumps({'account_number':card['account_number'], 'bank':card['bank']}).encode()
            connection.request('POST', parsed.path or '/', body=body, headers={'Content-Type':'application/json'})
            response = connection.getresponse()
            if response.status != 200:
                return None
            data = response.read(8193)
            if len(data)>8192 or time.monotonic()>deadline:
                return None
            value = json.loads(data).get('account_name')
            if isinstance(value,str) and value.strip():
                return value.strip()[:120]
        except (OSError, ValueError, TypeError, AttributeError, IndexError, http.client.HTTPException, FutureTimeout):
            return None
        finally:
            lookup.cancel()
            if timer is not None:timer.cancel()
            if connection is not None:connection.close()
            if tls_sock is not None:tls_sock.close()
            if sock is not None:sock.close()
        return None

    def run(self):
        while not self.stop.is_set():
            try:
                if not self.one():
                    self.stop.wait(.1)
            except Exception:
                self.stop.wait(1)
