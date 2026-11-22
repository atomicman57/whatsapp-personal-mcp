"""Private event-driven WhatsApp intake. Read-only bridge; authenticated wake only."""
import hmac
import json
import os
import re
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import requests
from transfer_telemetry import migrate, milliseconds, record, report
from transfer_card import card_for, NameEnquiry
from transfer_detection import (DEFAULT_EXCLUDED_CHAT_PHRASES,LINK_WINDOW_SECONDS,TEST_PREFIX,classify,digest,
    parse_excluded_chat_phrases,parse_link_window,safe_context,sensitive,settled,source_digest)

DEFAULT_SOURCE_ACCOUNT='whatsapp-main'

WEBHOOK_TIMEOUT=(3,5)  # connect, read seconds; used by Webhook.send
# An outbox claim older than this is treated as abandoned (crash mid-send) and may be reclaimed.
# It must exceed the longest send (WEBHOOK_TIMEOUT) with margin so a live send is never reclaimed.
CLAIM_LEASE_SECONDS=120
assert CLAIM_LEASE_SECONDS>sum(WEBHOOK_TIMEOUT)*2

def utcnow():return datetime.now(timezone.utc).isoformat()
def claim_cutoff():return (datetime.now(timezone.utc)-timedelta(seconds=CLAIM_LEASE_SECONDS)).isoformat()
def parsed_time(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('timezone required')
    return result

def bounded_notification(payload,max_bytes=3500):
    payload=json.loads(json.dumps(payload))
    # Some wake adapters truncate larger inputs. Keep control fields first,
    # fit a complete JSON document, and give the receiver an end marker.
    payload={'schema_version':1,'event_id':payload['event_id'],
             'event_type':payload.get('event_type'),'dry_run':bool(payload.get('dry_run',False)),
             'payment_authorized':False,**payload}
    payload['payment_authorized']=False
    payload['payload_end']=payload['event_id']
    payload['payload_bytes']=0
    payload['integrity']={'complete_json':True,'card_fields_preserved':True}
    for _ in range(5):payload['payload_bytes']=len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())
    def size():return len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())
    while size()>max_bytes and payload.get('context'):
        payload['context'].pop(0);payload['context_truncated']=True
    if size()>max_bytes and payload.get('source',{}).get('content'):
        payload['source']['content']=payload['source']['content'][:512];payload['context_truncated']=True
    if size()>max_bytes and payload.get('candidate'):
        payload['candidate']={'classification':'heuristic_candidate','details_omitted':True}
        payload['candidate_truncated']=True;payload['context_may_be_incomplete']=True
    if size()>max_bytes and payload.get('source'):
        payload['source'].pop('content',None)
        payload['context_truncated']=True
    if size()>max_bytes and payload.get('source'):
        for key in ('chat_name','filename'):payload['source'].pop(key,None)
    # Preserve every card key and all account digits. Only presentation text shrinks.
    for limit in (80,40,20):
        if size()<=max_bytes:break
        for key in ('purpose','requester_display_name','saved_contact_name','account_name'):
            value=payload.get('card',{}).get(key)
            if isinstance(value,str) and len(value)>limit:
                payload['card'][key]=value[:limit]
                payload['integrity']['card_text_truncated']=True
    if size()>max_bytes:raise ValueError('notification_too_large')
    for _ in range(5):payload['payload_bytes']=size()
    if size()>max_bytes:raise ValueError('notification_too_large')
    payload['payload_end']=payload.pop('payload_end')
    return payload

class State:
    def __init__(self,root,source_account=DEFAULT_SOURCE_ACCOUNT,events=None):
        self.source_account=source_account
        self.events=events
        root=Path(root);root.mkdir(parents=True,mode=0o700,exist_ok=True)
        self.path=root/'transfer-detector.sqlite'
        if events is not None and events.path.resolve()!=self.path.resolve():
            raise ValueError('Events must use the detector transaction database')
        if self.path.is_symlink():raise ValueError('state symlink refused')
        fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600);os.close(fd)
        self.path.chmod(0o600);self.lock=threading.RLock()
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS seen(source TEXT PRIMARY KEY,digest TEXT NOT NULL,event TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,source TEXT NOT NULL,payload TEXT,
                  attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
                  delivered TEXT,created TEXT NOT NULL,observed TEXT,processing_ms REAL,first_attempt TEXT,
                  request_key TEXT,held INTEGER NOT NULL DEFAULT 0);''')
            # Databases created before these columns existed.
            columns={x[1] for x in db.execute('PRAGMA table_info(outbox)')}
            if 'request_key' not in columns:db.execute('ALTER TABLE outbox ADD COLUMN request_key TEXT')
            if 'held' not in columns:db.execute('ALTER TABLE outbox ADD COLUMN held INTEGER NOT NULL DEFAULT 0')
            migrate(db)
            db.executescript('''CREATE TABLE IF NOT EXISTS requests_latest(request_key TEXT PRIMARY KEY,event_id TEXT NOT NULL,chat_jid TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS name_enquiries(event_id TEXT PRIMARY KEY,payload TEXT,status TEXT NOT NULL DEFAULT 'pending');''')
            if 'card' not in {x[1] for x in db.execute('PRAGMA table_info(requests_latest)')}:
                db.execute('ALTER TABLE requests_latest ADD COLUMN card TEXT')
            for key,event_id,body in db.execute('SELECT request_key,id,payload FROM outbox WHERE request_key IS NOT NULL AND payload IS NOT NULL ORDER BY rowid'):
                value=json.loads(body)
                chat=(value.get('source') or {}).get('chat_jid')
                if chat:
                    db.execute('INSERT OR IGNORE INTO requests_latest(request_key,event_id,chat_jid,active) VALUES(?,?,?,1)',(key,event_id,chat))
            # Claims are not cleared here: another worker may be mid-send. A claim stranded by a crash
            # becomes due again once older than CLAIM_LEASE_SECONDS (see due/attempt_started).
    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.path,timeout=10)
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('PRAGMA secure_delete=ON')
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:connection.close()
    def get(self,key,default=None):
        with self.lock,self.db() as db:
            row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self,**values):
        with self.lock,self.db() as db:
            for key,value in values.items():db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,json.dumps(value)))
    def seen(self,source):
        with self.lock,self.db() as db:
            return db.execute('SELECT digest,event FROM seen WHERE source=?',(source,)).fetchone()
    def _queue(self,db,source,payload):
        payload=bounded_notification(payload)
        if db.execute('SELECT count(*) FROM outbox WHERE delivered IS NULL').fetchone()[0]>=10000:
            raise RuntimeError('outbox_capacity')
        db.execute('INSERT OR IGNORE INTO outbox(id,source,payload,created,observed,processing_ms,request_key,held,next_attempt,msg_ts,ingested_at,sse_wake_at,observed_at,classify_ms) VALUES(?,?,?,?,?,?,?,0,0,?,?,?,?,?)',
            (payload['event_id'],source,json.dumps(payload),utcnow(),payload.get('detector_observed_at'),payload.get('detector_processing_ms'),payload.get('request_key'),
             milliseconds(payload.get('source',{}).get('timestamp')) if payload.get('source',{}).get('timestamp') else None,
             payload.get('ingested_at'),payload.get('sse_wake_at'),milliseconds(payload.get('detector_observed_at')) if payload.get('detector_observed_at') else None,payload.get('detector_processing_ms')))
        if self.events is not None:self.events.publish(db,payload,0)
        return payload
    def consume(self,sequence,source,body_digest,payload,hold_until=0):
        with self.lock,self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if payload:
                request_key=payload.get('request_key')
                if payload.get('candidate_withdrawn') and payload.get('previous_event_id'):
                    previous=payload['previous_event_id']
                    latest=db.execute('SELECT r.request_key,r.event_id FROM requests_latest r JOIN outbox o ON o.request_key=r.request_key WHERE o.id=? AND r.active=1',(previous,)).fetchone()
                    if latest:
                        request_key=payload['request_key']=latest[0]
                        payload['previous_event_id']=latest[1]
                        db.execute('UPDATE requests_latest SET active=0 WHERE request_key=?',(request_key,))
                        stale=db.execute('SELECT id FROM outbox WHERE request_key=? AND delivered IS NULL AND first_attempt IS NULL',(request_key,)).fetchall()
                        if self.events is not None:self.events.cancel_pending(db,[x[0] for x in stale])
                        db.executemany('DELETE FROM outbox WHERE id=?',stale)
                    else:
                        db.execute('DELETE FROM outbox WHERE id=? AND delivered IS NULL AND first_attempt IS NULL',(previous,))
                        if self.events is not None:self.events.cancel_pending(db,[previous])
                if request_key and not payload.get('candidate_withdrawn'):
                    previous=db.execute('SELECT event_id,card FROM requests_latest WHERE request_key=? AND active=1',(request_key,)).fetchone()
                    if previous and previous[0]!=payload['event_id']:
                        old_card=json.loads(previous[1]) if previous[1] else {}
                        new_card=payload.get('card',{})
                        payload.update(event_type='candidate_updated',previous_event_id=previous[0],revision_hint='replace_card_for_request_key',
                            changed_fields=sorted(k for k in old_card.keys()|new_card.keys() if old_card.get(k)!=new_card.get(k)))
                    db.execute('INSERT OR REPLACE INTO requests_latest VALUES(?,?,?,1,?)',(request_key,payload['event_id'],payload['source']['chat_jid'],json.dumps(payload.get('card',{}))))
                payload=self._queue(db,source,payload)
                if payload.get('card',{}).get('account_name_reason')=='pending_nonblocking':
                    db.execute('INSERT OR IGNORE INTO name_enquiries(event_id,payload) VALUES(?,?)',(payload['event_id'],json.dumps(payload)))
                db.execute('INSERT OR REPLACE INTO seen VALUES(?,?,?)',(source,body_digest,payload['event_id']))
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('cursor',json.dumps(sequence)))
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('last_event',json.dumps(utcnow())))
    def enriched(self,payload,original):
        with self.lock,self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            latest=db.execute('SELECT event_id FROM requests_latest WHERE request_key=? AND active=1',(payload['request_key'],)).fetchone()
            # Do not resurrect withdrawn requests or replace a newer amount/destination revision.
            if not latest or latest[0]!=original:return
            self._queue(db,'name_enquiry',payload)
            db.execute('UPDATE requests_latest SET event_id=?,card=? WHERE request_key=?',(payload['event_id'],json.dumps(payload.get('card',{})),payload['request_key']))
    def cancel_held(self,chat_jid):
        """Cancel unsent candidates and emit one withdrawal per active request, atomically."""
        with self.lock,self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            requests_=db.execute('SELECT request_key,event_id FROM requests_latest WHERE chat_jid=? AND active=1',(chat_jid,)).fetchall()
            for request_key,previous in requests_:
                rows=db.execute('SELECT id FROM outbox WHERE request_key=? AND delivered IS NULL AND first_attempt IS NULL',(request_key,)).fetchall()
                if self.events is not None:self.events.cancel_pending(db,[row[0] for row in rows])
                db.executemany('DELETE FROM outbox WHERE id=?',rows)
                db.execute("UPDATE name_enquiries SET status='cancelled',payload=NULL WHERE event_id=?",(previous,))
                self._queue(db,'receipt',{'event_id':digest(previous,'withdrawn'),'event_type':'candidate_withdrawn',
                    'request_key':request_key,'previous_event_id':previous,'revision_hint':'withdraw_card_for_request_key',
                    'candidate_withdrawn':True,'source_account':self.source_account,'payment_authorized':False,
                    'reason':'Receipt observed; candidate withdrawn. No payment authorized.'})
                db.execute('UPDATE requests_latest SET active=0 WHERE request_key=?',(request_key,))
        return len(requests_)
    def due(self):
        with self.lock,self.db() as db:
            row=db.execute('SELECT id,payload,attempts FROM outbox WHERE delivered IS NULL AND (first_attempt IS NULL OR first_attempt<?) AND next_attempt<=? ORDER BY rowid LIMIT 1',
                           (claim_cutoff(),time.time())).fetchone()
        return (row[0],json.loads(row[1]),row[2]) if row else None
    def delivered(self,key):
        with self.lock,self.db() as db:
            # Once acknowledged, keep dedupe metadata but no extra context copy.
            db.execute('UPDATE outbox SET payload=NULL,delivered=?,delivered_at=? WHERE id=?',(utcnow(),milliseconds(),key))
        self.set(last_webhook_accepted=utcnow(),delivery_error=None)
    def attempt_started(self,key):
        """Exclusively claim a due row (unclaimed, or claim older than the lease); returns the claim
        timestamp, or None if another worker holds a live claim or it was delivered or cancelled since `due()`."""
        claim=utcnow()
        with self.lock,self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            claimed=db.execute('UPDATE outbox SET first_attempt=?,post_started_at=?,attempts=attempts+1 WHERE id=? AND delivered IS NULL AND next_attempt<=? AND (first_attempt IS NULL OR first_attempt<?)',
                               (claim,milliseconds(),key,time.time(),claim_cutoff())).rowcount==1
        return claim if claimed else None
    def enqueue_system(self,payload):
        with self.lock,self.db() as db:
            self._queue(db,'system',payload)
    def failed(self,key,attempts,code,claim):
        with self.lock,self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT attempts,source FROM outbox WHERE id=? AND first_attempt=?',(key,claim)).fetchone()
            if not current:return
            count,source=current
            db.execute('UPDATE outbox SET next_attempt=?,first_attempt=NULL WHERE id=? AND first_attempt=?',
                       (time.time()+min(60,2**min(max(0,count-1),6)),key,claim))
            if count==3 and source!='system':
                self._queue(db,'system',{'event_id':digest(key,'webhook-failed-3'),'event_type':'detector_health_warning',
                    'source_account':self.source_account,'failed_event_id':key,'reason':'Webhook failed three times; retries continue.',
                    'payment_authorized':False})
        self.set(delivery_error=code)
    def status(self):
        with self.lock,self.db() as db:
            pending=db.execute('SELECT count(*) FROM outbox WHERE delivered IS NULL').fetchone()[0]
            sent=db.execute('SELECT count(*) FROM outbox WHERE delivered IS NOT NULL').fetchone()[0]
        latency=report(self,1)
        p90=latency['hops'].get('message_to_owner',{}).get('p90_ms')
        return {'last_24h_p90_ms':p90,'last_24h_latency':latency,'enabled':True,'source_account':self.source_account,'queued_notifications':pending,
                'accepted_notifications':sent,**{key:self.get(key) for key in
                ['go_live','stream_connected','last_event','last_webhook_accepted','read_error','delivery_error','gap_detected']},
                'delivery_semantics':'at-least-once webhook; stable event IDs require receiver deduplication',
                'payment_execution':False}

class BridgeReader:
    def __init__(self,base,token):
        url=urlsplit(base)
        if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost') or url.username or url.password:
            raise ValueError('private loopback bridge required')
        if len(token)<32:raise ValueError('read credential required')
        self.base=base.rstrip('/');self.headers={'Authorization':'Bearer '+token}
    def get(self,path,params=None):
        with requests.get(self.base+'/'+path,params=params,headers=self.headers,timeout=(5,15),stream=True) as response:
            response.raise_for_status();body=bytearray()
            for block in response.iter_content(65536):
                body.extend(block)
                if len(body)>4*1024*1024:raise ValueError('bridge_response_too_large')
        result=json.loads(body)
        if not result.get('success'):raise RuntimeError('bridge_read_failed')
        return result.get('data',result)
    def events(self,stop,wake,state):
        while not stop.is_set():
            try:
                # SSE is a wake signal. Durable changes, not the bounded memory
                # replay buffer, own the processing cursor and restart recovery.
                with requests.get(self.base+'/events',headers=self.headers,stream=True,timeout=(5,40)) as response:
                    response.raise_for_status();state.set(stream_connected=True);wake.set()
                    for line in response.iter_lines(chunk_size=1):
                        if stop.is_set():break
                        if line.startswith((b'event: message',b'event: reset')):
                            state.set(sse_wake_at=milliseconds());wake.set()
            except (requests.RequestException,ValueError):
                state.set(stream_connected=False)
            finally:state.set(stream_connected=False)
            if not stop.wait(2):wake.set()

class Webhook:
    def __init__(self,url,header,value):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('HTTPS webhook required')
        if not re.fullmatch(r'[A-Za-z0-9-]+',header) or header.lower() in ('host','content-type','content-length','connection'):
            raise ValueError('invalid webhook authorization header')
        if not value or '\n' in value or '\r' in value:raise ValueError('webhook authorization required')
        self.url=url;self.headers={header:value}
        self.session=requests.Session()
        self.session.trust_env=False
    def send(self,event_id,payload):
        try:
            body=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
            response=self.session.post(self.url,data=body,headers={**self.headers,'Content-Type':'application/json','Idempotency-Key':event_id},
                                   timeout=WEBHOOK_TIMEOUT,allow_redirects=False)
            self.last_status=response.status_code
            response.close()
            # Acceptance is not evidence that the receiving bot has processed it.
            if 200<=response.status_code<300:return True,None
            return False,'webhook_http_'+str(response.status_code)
        except requests.RequestException:
            self.last_status=None
            return False,'webhook_network_error'

class Detector:
    def __init__(self,state,bridge,webhook,account,expected_phone,blocked_phones=(),blocked_names=(),test_sender='',
                 source_account=DEFAULT_SOURCE_ACCOUNT,link_window=LINK_WINDOW_SECONDS,exclusions=DEFAULT_EXCLUDED_CHAT_PHRASES):
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',source_account or ''):raise ValueError('invalid source account')
        self.state=state;self.bridge=bridge;self.webhook=webhook
        self.source_account=state.source_account=source_account
        self.link_window=link_window;self.exclusions=tuple(exclusions)
        self.account=account;self.expected_phone=expected_phone
        self.blocked=set(blocked_phones);self.names=[x.casefold() for x in blocked_names]
        self.test_sender=test_sender;self.stop=threading.Event();self.wake=threading.Event()
    def initialize(self):
        identity=self.bridge.get('identity')
        if identity.get('phone_number')!=self.expected_phone:raise RuntimeError('account_identity_mismatch')
        page=self.bridge.get('message-changes',{'after_sequence':0,'limit':1})
        if self.state.get('cursor') is None:
            self.state.set(cursor=page['max_sequence'],go_live=utcnow(),account=self.account)
        if self.state.get('account')!=self.account:raise RuntimeError('state_account_mismatch')
    def build(self,row):
        started=time.perf_counter();observed=utcnow()
        source_key=digest(self.account,row['chat_jid'],row['id'])
        body_hash=source_digest(row);prior=self.state.seen(source_key)
        if prior and prior[0]==body_hash:return source_key,body_hash,None
        base={'event_id':digest(source_key,body_hash),'source_account':self.source_account,'source_instance':self.account,
              'source':{key:row.get(key) for key in ('chat_jid','id','timestamp','is_from_me','chat_name','media_type','filename')},
              'untrusted_evidence':True,'payment_authorized':False,
              'ingested_at':row.get('ingested_at'),'sse_wake_at':self.state.get('sse_wake_at')}
        base['source']['chat_name']=(row.get('chat_name') or '')[:120]
        base['source']['filename']=(row.get('filename') or '')[:200]
        def timed(payload):
            return {**payload,'detector_observed_at':observed,'detector_processing_ms':round((time.perf_counter()-started)*1000,3)}
        def skip():
            if prior:
                return source_key,body_hash,timed({**base,'event_type':'source_changed','previous_event_id':prior[1],
                    'candidate_withdrawn':True,'context':[],'reason':'Source changed; invalidate any unexecuted approval and review. No payment authorized.'})
            return source_key,body_hash,None
        if (len(row['id'])>256 or len(row['chat_jid'])>128 or row.get('deleted') or row.get('is_from_me')
                or not row['chat_jid'].endswith(('@lid','@s.whatsapp.net'))):return skip()
        try:
            timestamp=parsed_time(row['timestamp'])
            if timestamp<parsed_time(self.state.get('go_live')):return skip()
        except (ValueError,TypeError):return skip()
        if sensitive(row.get('content','')):return skip()
        if any(name in (row.get('chat_name') or '').casefold() for name in self.names):return skip()
        aliases=self.bridge.get('contact/aliases',{'jid':row['chat_jid']})
        phone=aliases.get('phone_number','')
        if phone in self.blocked or any(x.removesuffix('@s.whatsapp.net') in self.blocked for x in aliases.get('aliases',[])):
            return skip()
        dry_run=(row.get('content') or '').startswith(TEST_PREFIX) and phone==self.test_sender and bool(self.test_sender)
        if (row.get('content') or '').startswith(TEST_PREFIX) and not dry_run:return skip()
        # Unresolved identities get a metadata-only review notification, never a
        # context fetch that could open a specifically excluded person's chat.
        if not phone:
            clues=classify(row,(),dry_run,self.link_window,self.exclusions)
            if not clues:return skip()
            return source_key,body_hash,timed({**base,'event_type':'identity_review_required','identity_unresolved':True,
                'source':{key:base['source'].get(key) for key in ('chat_jid','id','timestamp')},'context':[],
                'context_withheld_for_identity_verification':True,'dry_run':dry_run})
        context=self.bridge.get('messages',{'chat_jid':row['chat_jid'],'limit':50})
        rows=[]
        for item in context:
            try:
                dt=parsed_time(item['timestamp'])
                if timestamp-timedelta(hours=48)<=dt<=timestamp:rows.append(item)
            except (ValueError,TypeError,KeyError):continue
        rows.sort(key=lambda x:(parsed_time(x['timestamp']),x['id']))
        rows=rows[-20:]
        clues=classify(row,rows,dry_run,self.link_window,self.exclusions)
        if not clues:return skip()
        cleaned,truncated=safe_context(rows)
        base['source']['content']=(row.get('content') or '')[:2000]
        base['source']['phone_number']=phone
        card=card_for(row,clues,aliases,rows)
        if os.environ.get('WHATSAPP_TRANSFER_NAME_ENQUIRY_URL'):
            card['account_name_reason']='pending_nonblocking' if card['account_number'] and card['bank'] else 'destination_missing_or_ambiguous'
        return source_key,body_hash,timed({**base,'event_type':'transfer_request_candidate','candidate':clues,
            'card':card,'requester_is_saved_contact':card['requester_is_saved_contact'],
            'request_key':digest(self.account,row['chat_jid'],clues['anchor_message_id']),
            'context':cleaned,'context_truncated':truncated or len(row.get('content',''))>2000,
            'context_may_be_incomplete':len(context)>=50 or not any(x['id']==row['id'] for x in rows),
            'dry_run':dry_run,'previous_event_id':prior[1] if prior else None})
    def hold_until(self,row,payload):
        """The link window correlates revisions; it never delays an owner card."""
        return 0
    def settles(self,row):
        """A live receipt/"already paid" from either side settles the chat's pending split."""
        text=row.get('content') or ''
        if row.get('deleted') or not settled(text) or sensitive(text) or text.startswith(TEST_PREFIX):return False
        try:return parsed_time(row['timestamp'])>=parsed_time(self.state.get('go_live'))
        except (ValueError,TypeError,KeyError,AttributeError):return False
    def drain(self):
        for batch in range(100):
            cursor=self.state.get('cursor')
            page=self.bridge.get('message-changes',{'after_sequence':cursor,'limit':25})
            if page['gap_detected']:
                self.state.set(gap_detected=True,read_error='change_feed_gap_requires_review')
                self.state.enqueue_system({'event_id':digest(self.account,'feed-gap',cursor),
                    'event_type':'detector_health_warning','source_account':self.source_account,
                    'reason':'Durable feed retention/cursor gap; automatic intake stopped for owner review.',
                    'payment_authorized':False,'untrusted_evidence':True})
                return
            for row in page['messages']:
                if self.settles(row):self.state.cancel_held(row['chat_jid'])
                key,body_hash,payload=self.build(row)
                self.state.consume(row['sequence'],key,body_hash,payload,self.hold_until(row,payload))
            self.state.set(read_error=None,gap_detected=False)
            if not page['has_more']:return
        self.wake.set()
    def deliver_one(self):
        item=self.state.due()
        if not item:return False
        key,payload,attempts=item
        claim=self.state.attempt_started(key)
        if not claim:return True
        started=time.perf_counter()
        success,code=self.webhook.send(key,payload)
        status=200 if success else int(code.removeprefix('webhook_http_')) if code and code.startswith('webhook_http_') else None
        with self.state.lock,self.state.db() as db:
            db.execute('UPDATE outbox SET http_status=?,http_ms=? WHERE id=?',(getattr(self.webhook,'last_status',status),round((time.perf_counter()-started)*1000,3),key))
        if success:self.state.delivered(key)
        else:self.state.failed(key,attempts,code,claim)
        return True
    def delivery_loop(self):
        while not self.stop.is_set():
            try:
                if not self.deliver_one():self.stop.wait(.05)
            except Exception:
                self.state.set(delivery_error='delivery_state_error');self.stop.wait(2)
    def run(self):
        while not self.stop.is_set():
            try:self.initialize();break
            except Exception:self.state.set(read_error='bridge_identity_or_feed_unavailable');self.stop.wait(3)
        threading.Thread(target=self.bridge.events,args=(self.stop,self.wake,self.state),daemon=True).start()
        threading.Thread(target=self.delivery_loop,daemon=True).start()
        self.wake.set()
        while not self.stop.is_set():
            # This cheap catch-up heartbeat is server-local; no AI bot is polled.
            self.wake.wait(1);self.wake.clear()
            try:self.drain()
            except Exception:self.state.set(read_error='bridge_or_processing_error');self.stop.wait(2);self.wake.set()

def status_server(state,token,port=8755):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def authorized(self):
            auth=self.headers.get_all('Authorization') or []
            if len(auth)!=1 or not hmac.compare_digest(auth[0],'Bearer '+token):
                self.send_error(401);return False
            return True
        def answer(self,value):
            content=json.dumps(value).encode();self.send_response(200)
            self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
        def do_GET(self):
            if not self.authorized():return
            from urllib.parse import parse_qs
            url=urlsplit(self.path)
            if url.path=='/status':self.answer(state.status())
            elif url.path=='/latency':
                try:self.answer(report(state,int(parse_qs(url.query).get('days',['14'])[0])))
                except (ValueError,TypeError):self.send_error(400)
            else:self.send_error(404)
        def do_POST(self):
            if not self.authorized():return
            if self.path!='/stages':self.send_error(404);return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=1024:raise ValueError()
                value=json.loads(self.rfile.read(length))
                if not isinstance(value,dict) or set(value)!={'event_id','stage'}:raise ValueError()
                self.answer(record(state,value['event_id'],value['stage']))
            except (ValueError,TypeError):self.send_error(400)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server

def main():
    os.umask(0o077)
    token=os.environ['WHATSAPP_BRIDGE_READ_TOKEN']
    source_account=os.environ.get('WHATSAPP_TRANSFER_SOURCE_ACCOUNT','').strip() or DEFAULT_SOURCE_ACCOUNT
    state_dir=os.environ.get('WHATSAPP_TRANSFER_STATE_DIR','/state')
    events=None
    if os.environ.get('WHATSAPP_MCP_EVENTS_ENABLED','').lower()=='true':
        from mcp_events import EventStore
        events=EventStore(state_dir,source_account,os.environ['WHATSAPP_BRIDGE_INSTANCE_ID'])
    state=State(state_dir,source_account,events=events)
    bridge=BridgeReader(os.environ.get('WHATSAPP_BRIDGE_URL','http://127.0.0.1:8753/api'),token)
    webhook=Webhook(os.environ['WHATSAPP_TRANSFER_WEBHOOK_URL'],os.environ['WHATSAPP_TRANSFER_WEBHOOK_AUTH_HEADER'],os.environ['WHATSAPP_TRANSFER_WEBHOOK_AUTH_VALUE'])
    detector=Detector(state,bridge,webhook,os.environ['WHATSAPP_BRIDGE_INSTANCE_ID'],os.environ['WHATSAPP_TRANSFER_EXPECTED_PHONE'],
        [x.strip().lstrip('+') for x in os.environ.get('WHATSAPP_TRANSFER_EXCLUDED_PHONES','').split(',') if x.strip()],
        [x.strip() for x in os.environ.get('WHATSAPP_TRANSFER_EXCLUDED_NAMES','').split(',') if x.strip()],
        os.environ.get('WHATSAPP_TRANSFER_TEST_SENDER',''),source_account,
        parse_link_window(os.environ.get('WHATSAPP_TRANSFER_LINK_WINDOW_SECONDS')),
        parse_excluded_chat_phrases(os.environ.get('WHATSAPP_TRANSFER_EXCLUDED_CHAT_PHRASES')))
    enquiry=None
    if os.environ.get('WHATSAPP_TRANSFER_NAME_ENQUIRY_URL'):
        enquiry=NameEnquiry(state,os.environ['WHATSAPP_TRANSFER_NAME_ENQUIRY_URL'])
        threading.Thread(target=enquiry.run,daemon=True).start()
    server=status_server(state,token)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:(detector.stop.set(),detector.wake.set()))
    try:detector.run()
    finally:
        if enquiry:enquiry.stop.set()
        server.shutdown();server.server_close()

if __name__=='__main__':main()
