"""Content-free, millisecond intake measurements. Unknown hops remain unknown."""
import math
import time
import sqlite3
from datetime import datetime, timezone

STAGES = ('intake_received', 'card_created', 'owner_alerted')
COLUMNS = {'msg_ts': 'INTEGER', 'ingested_at': 'INTEGER', 'sse_wake_at': 'INTEGER',
           'observed_at': 'INTEGER', 'classify_ms': 'REAL', 'post_started_at': 'INTEGER',
           'http_status': 'INTEGER', 'http_ms': 'REAL', 'delivered_at': 'INTEGER'}


def milliseconds(value=None):
    if value is None:
        return int(time.time() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)
    except (ValueError, TypeError, AttributeError):
        return None


def migrate(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(outbox)')}
    for name, kind in COLUMNS.items():
        if name not in columns:
            db.execute(f'ALTER TABLE outbox ADD COLUMN {name} {kind}')
    db.executescript('''CREATE TABLE IF NOT EXISTS stages(event_id TEXT NOT NULL, stage TEXT NOT NULL, at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS stages_event ON stages(event_id);
        CREATE TRIGGER IF NOT EXISTS stages_no_update BEFORE UPDATE ON stages BEGIN SELECT RAISE(ABORT,'append_only'); END;
        CREATE TRIGGER IF NOT EXISTS stages_no_delete BEFORE DELETE ON stages BEGIN SELECT RAISE(ABORT,'append_only'); END;''')
    # Release legacy destination holds on upgrade, but retain active send claims.
    db.execute('UPDATE outbox SET held=0,next_attempt=0 WHERE held=1 AND delivered IS NULL')


def record(state, event_id, stage):
    if not isinstance(event_id, str) or not 0 < len(event_id) <= 128 or stage not in STAGES:
        raise ValueError('invalid_intake_stage')
    with state.lock, state.db() as db:
        if not db.execute('SELECT 1 FROM outbox WHERE id=?', (event_id,)).fetchone():
            raise ValueError('unknown_event')
        at = milliseconds()
        db.execute('INSERT INTO stages VALUES(?,?,?)', (event_id, stage, at))
    return {'event_id': event_id, 'stage': stage, 'at': at}


def report(state, days=14):
    if type(days) not in (int, float) or not 0 < days <= 90:
        raise ValueError('invalid_days')
    now = milliseconds()
    with state.lock, state.db() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT * FROM outbox WHERE created>=?',
                          (datetime.fromtimestamp((now-days*86400000)/1000, timezone.utc).isoformat(),)).fetchall()
        stages = {}
        for row in db.execute('SELECT event_id,stage,MIN(at) AS at FROM stages GROUP BY event_id,stage'):
            stages.setdefault(row['event_id'], {})[row['stage']] = row['at']
    hops = {}
    slow = []
    incomplete = []
    for row in rows:
        values = dict(row)
        values.update(stages.get(row['id'], {}))
        values['owner_card_at'] = values.get('owner_alerted') or values.get('card_created')
        pairs = [('message_to_ingest','msg_ts','ingested_at'), ('ingest_to_sse','ingested_at','sse_wake_at'),
                 ('sse_to_observe','sse_wake_at','observed_at'), ('ingest_to_observe','ingested_at','observed_at'),
                 ('observe_to_post','observed_at','post_started_at'), ('webhook_to_card','delivered_at','card_created'),
                 ('post_to_intake','post_started_at','intake_received'), ('intake_to_card','intake_received','card_created'),
                 ('card_to_alert','card_created','owner_alerted'),
                 ('message_to_owner','msg_ts','owner_card_at'), ('ingest_to_owner','ingested_at','owner_card_at')]
        for name, start, end in pairs:
            a, b = values.get(start), values.get(end)
            if a is not None and b is not None and b >= a:
                hops.setdefault(name, []).append((b-a, row['id']))
        for name in ('classify_ms', 'http_ms'):
            if values.get(name) is not None:
                hops.setdefault(name, []).append((values[name], row['id']))
        start = values.get('msg_ts')
        end = values.get('owner_card_at')
        if start is not None and end is not None and end-start > 5000:
            slow.append(row['id'])
        if start is not None and end is None:
            incomplete.append({'event_id': row['id'], 'age_ms': max(0, now-start), 'over_5s': now-start > 5000})
    result = {}
    for name, points in hops.items():
        points.sort()
        ordered = [point[0] for point in points]
        n = len(points)
        result[name] = {'count': n, 'median_ms': (ordered[(n-1)//2]+ordered[n//2])/2,
                        'p90_ms': ordered[math.ceil(.9*n)-1], 'max_ms': ordered[-1],
                        'event_ids_over_5s': [key for value, key in points if value > 5000]}
    return {'days': days, 'hops': result, 'event_ids_over_5s': slow, 'awaiting_owner_stage': incomplete,
            'owner_stage_required': True, 'payment_execution': False}
