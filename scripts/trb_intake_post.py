#!/usr/bin/env python3
"""Private, locked JSONL intake. Reads one webhook JSON object from stdin."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import sys

TERMINAL = {'completed', 'paid', 'rejected', 'cancelled', 'withdrawn', 'closed'}
OPEN = {'open', 'pending', 'review', 'approved', 'processing'}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone_required')
    return parsed


def read_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.intake-')
    try:
        with os.fdopen(fd, 'w') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def recover(root):
    journal = root / '.transaction.json'
    if journal.exists():
        for name, content in json.loads(journal.read_text()).items():
            atomic_write(root / name, content)
        journal.unlink()


def commit(root, files):
    # A durable redo journal recovers a crash between replacements on the next call.
    atomic_write(root / '.transaction.json', compact({
        name: ''.join(compact(row) + '\n' for row in rows) for name, rows in files.items()
    }))
    recover(root)


@contextmanager
def locked(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.intake.lock').open('a+b') as file:
        os.chmod(file.name, 0o600)
        if os.name == 'nt':
            import msvcrt
            file.seek(0)
            file.write(b'\0')
            file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(file, fcntl.LOCK_EX)
        try:
            recover(root)
            yield root
        finally:
            if os.name == 'nt':
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)


def history(root, kind):
    return read_lines(root / (kind + '.jsonl')) + [
        row for path in sorted((root / 'archive').glob(kind + '-*.jsonl'))
        for row in read_lines(path)
    ]


def summary(row):
    return {'request_key': row['request_key'], 'event_id': row['event_id'],
            'state': row['state'], 'amount': row.get('amount'),
            'last4': (row.get('account_number') or '')[-4:] or None}


def intake(root, payload, days=14):
    event_id = payload.get('event_id')
    if not isinstance(event_id, str) or not event_id or len(event_id) > 256:
        raise ValueError('invalid_event_id')
    if payload.get('payload_end', event_id) != event_id:
        raise ValueError('incomplete_payload')
    with locked(root) as root:
        events = history(root, 'events')
        requests = read_lines(root / 'requests.jsonl')
        # Live rows precede archive snapshots; report each logical request once.
        all_requests = list({row['request_key']: row for row in reversed(history(root, 'requests'))}.values())
        key = payload.get('request_key')
        if key is not None and (not isinstance(key, str) or not key or len(key) > 256):
            raise ValueError('invalid_request_key')
        current = next((row for row in all_requests if row['request_key'] == key), None)
        duplicate = any(row['event_id'] == event_id for row in events)
        card = payload.get('card') or {}
        source = payload.get('source') or {}
        requester = [payload.get('source_account'), source.get('chat_jid')]
        account = card.get('account_number')
        if account is not None and (not isinstance(account, str) or not account.isascii()
                                    or not account.isdigit() or len(account) not in (10, 11)):
            raise ValueError('invalid_account_number')
        cutoff = now() - timedelta(days=days)
        related = [summary(row) for row in all_requests
                   if row['request_key'] != key and timestamp(row['updated_at']) >= cutoff
                   and ((requester[1] and row.get('requester') == requester)
                        or (account and row.get('account_number') == account))]
        if duplicate:
            return {'duplicate': True, 'applied': False, 'request': summary(current) if current else None,
                    'related_requests': related}
        predecessor = payload.get('previous_event_id')
        stale = any(row.get('previous_event_id') == event_id for row in events)
        conflict = bool(current and current['event_id'] != event_id and predecessor != current['event_id'])
        # Never replace a known revision with an initial event or an unproven branch.
        # Missing predecessors require receiver review rather than guessing arrival order.
        stale = stale or conflict
        kind = payload.get('event_type')
        withdrawn = kind == 'candidate_withdrawn' or payload.get('candidate_withdrawn') is True
        applied = bool(key and not stale and (withdrawn or kind in ('transfer_request_candidate', 'candidate_updated')))
        at = now().isoformat()
        if applied:
            amount = card.get('amount') or {}
            row = {**(current or {}), 'request_key': key, 'event_id': event_id,
                   'state': 'withdrawn' if withdrawn else (current or {}).get('state', 'pending'),
                   'updated_at': at}
            if not withdrawn:
                row.update(requester=requester, account_number=account, bank=card.get('bank'),
                           amount=amount.get('value'), currency=amount.get('currency'),
                           account_name=card.get('account_name'))
            requests = [item for item in requests if item['request_key'] != key] + [row]
            current = row
        # Fixed compact event layout excludes source text, phones and full accounts.
        event = {'event_id': event_id, 'request_key': key, 'event_type': kind,
                 'previous_event_id': predecessor, 'at': at, 'applied': applied,
                 'changed_fields': payload.get('changed_fields', [])}
        commit(root, {'requests.jsonl': requests,
                      'events.jsonl': read_lines(root / 'events.jsonl') + [event]})
        return {'duplicate': False, 'applied': applied, 'review_required': conflict, 'request': summary(current) if current else None,
                'related_requests': related}


def archive(root, days=14):
    if days < 1:
        raise ValueError('invalid_days')
    cutoff = now() - timedelta(days=days)
    with locked(root) as root:
        files = {}
        counts = {}
        for kind in ('requests', 'events'):
            keep, moved = [], []
            for row in read_lines(root / (kind + '.jsonl')):
                if kind == 'requests':
                    state = row['state']
                    eligible = state in TERMINAL or (state not in OPEN and timestamp(row['updated_at']) < cutoff)
                else:
                    eligible = timestamp(row['at']) < cutoff
                (moved if eligible else keep).append(row)
            name = 'archive/' + kind + '-' + now().date().isoformat() + '.jsonl'
            files[kind + '.jsonl'] = keep
            files[name] = read_lines(root / name) + moved
            counts[kind] = len(moved)
        commit(root, files)
        return {'archived': counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--archive', action='store_true')
    parser.add_argument('--days', type=int, default=14)
    args = parser.parse_args()
    try:
        if args.days < 1:
            raise ValueError('invalid_days')
        result = archive(args.state_dir, args.days) if args.archive else intake(
            args.state_dir, json.load(sys.stdin), args.days)
        print(compact(result))
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        # Do not echo input, filesystem contents, message text or raw exceptions.
        print('{"error":"intake_failed"}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
