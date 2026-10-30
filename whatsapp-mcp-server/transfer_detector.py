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
from transfer_detection import (DEFAULT_EXCLUDED_CHAT_PHRASES,LINK_WINDOW_SECONDS,RECEIPT,TEST_PREFIX,classify,digest,
    parse_excluded_chat_phrases,parse_link_window,safe_context,sensitive,source_digest)

DEFAULT_SOURCE_ACCOUNT='whatsapp-main'

def utcnow():return datetime.now(timezone.utc).isoformat()
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
    def size():return len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())
    while size()>max_bytes and payload.get('context'):
        payload['context'].pop(0);payload['context_truncated']=True
    if size()>max_bytes and payload.get('source',{}).get('content'):
        payload['source']['content']=payload['source']['content'][:512];payload['context_truncated']=True
    if size()>max_bytes and payload.get('candidate'):
        payload['candidate']={'classification':'heuristic_candidate','details_omitted':True}
        payload['candidate_truncated']=True;payload['context_may_be_incomplete']=True
    if size()>max_bytes:raise ValueError('notification_too_large')
    payload['payload_end']=payload.pop('payload_end')
    return payload

class State:
    def __init__(self,root,source_account=DEFAULT_SOURCE_ACCOUNT):
        self.source_account=source_account
        root=Path(root);root.mkdir(parents=True,mode=0o700,exist_ok=True)
        self.path=root/'transfer-detector.sqlite'
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
    def consume(self,sequence,source,body_digest,payload,hold_until=0):
        """`hold_until` defers an incomplete card; a later card for its request replaces it unsent."""
        with self.lock,self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if payload:
                payload=bounded_notification(payload)
                request_key=payload.get('request_key')
                db.execute('DELETE FROM outbox WHERE held=1 AND delivered IS NULL AND first_attempt IS NULL AND id!=? AND (request_key=? OR id=?)',
                           (payload['event_id'],request_key,payload.get('previous_event_id')))
                count=db.execute('SELECT count(*) FROM outbox WHERE delivered IS NULL').fetchone()[0]
                if count>=10000:raise RuntimeError('outbox_capacity')
                db.execute('INSERT OR IGNORE INTO outbox(id,source,payload,created,observed,processing_ms,request_key,held,next_attempt) VALUES(?,?,?,?,?,?,?,?,?)',
                           (payload['event_id'],source,json.dumps(payload),utcnow(),payload.get('detector_observed_at'),payload.get('detector_processing_ms'),
                            request_key,int(hold_until>0),hold_until))
                db.execute('INSERT OR REPLACE INTO seen VALUES(?,?,?)',(source,body_digest,payload['event_id']))
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('cursor',json.dumps(sequence)))
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('last_event',json.dumps(utcnow())))
    def cancel_held(self,chat_jid):
        """Drop this chat's unsent held cards; a receipt means the request is already settled."""
        with self.lock,self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT id,payload FROM outbox WHERE held=1 AND delivered IS NULL AND first_attempt IS NULL').fetchall()
            stale=[key for key,payload in rows if payload and (json.loads(payload).get('source') or {}).get('chat_jid')==chat_jid]
            db.executemany('DELETE FROM outbox WHERE id=?',[(key,) for key in stale])
        return len(stale)
    def due(self):
        with self.lock,self.db() as db:
            row=db.execute('SELECT id,payload,attempts FROM outbox WHERE delivered IS NULL AND next_attempt<=? ORDER BY rowid LIMIT 1',(time.time(),)).fetchone()
        return (row[0],json.loads(row[1]),row[2]) if row else None
    def delivered(self,key):
        with self.lock,self.db() as db:
            # Once acknowledged, keep dedupe metadata but no extra context copy.
            db.execute('UPDATE outbox SET payload=NULL,delivered=? WHERE id=?',(utcnow(),key))
        self.set(last_webhook_accepted=utcnow(),delivery_error=None)
    def attempt_started(self,key):
        with self.lock,self.db() as db:
            db.execute('UPDATE outbox SET first_attempt=COALESCE(first_attempt,?) WHERE id=?',(utcnow(),key))
    def enqueue_system(self,payload):
        with self.lock,self.db() as db:
            db.execute('INSERT OR IGNORE INTO outbox(id,source,payload,created) VALUES(?,?,?,?)',
                       (payload['event_id'],'system',json.dumps(payload),utcnow()))
    def failed(self,key,attempts,code):
        with self.lock,self.db() as db:
            db.execute('UPDATE outbox SET attempts=?,next_attempt=? WHERE id=?',
                       (attempts+1,time.time()+min(300,2**min(attempts+1,9)),key))
        self.set(delivery_error=code)
    def status(self):
        with self.lock,self.db() as db:
            pending=db.execute('SELECT count(*) FROM outbox WHERE delivered IS NULL').fetchone()[0]
            sent=db.execute('SELECT count(*) FROM outbox WHERE delivered IS NOT NULL').fetchone()[0]
        return {'enabled':True,'source_account':self.source_account,'queued_notifications':pending,
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
                    for line in response.iter_lines(chunk_size=1024):
                        if stop.is_set():break
                        if line.startswith((b'event: message',b'event: reset')):wake.set()
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
    def send(self,event_id,payload):
        try:
            body=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
            response=requests.post(self.url,data=body,headers={**self.headers,'Content-Type':'application/json','Idempotency-Key':event_id},
                                   timeout=(5,20),allow_redirects=False)
            # Acceptance is not evidence that the receiving bot has processed it.
            if 200<=response.status_code<300:return True,None
            return False,'webhook_http_'+str(response.status_code)
        except requests.RequestException:return False,'webhook_network_error'

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
              'untrusted_evidence':True,'payment_authorized':False}
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
        return source_key,body_hash,timed({**base,'event_type':'transfer_request_candidate','candidate':clues,
            'request_key':digest(self.account,row['chat_jid'],clues['anchor_message_id']),
            'context':cleaned,'context_truncated':truncated or len(row.get('content',''))>2000,
            'context_may_be_incomplete':len(context)>=50 or not any(x['id']==row['id'] for x in rows),
            'dry_run':dry_run,'previous_event_id':prior[1] if prior else None})
    def hold_until(self,row,payload):
        """Destination-only cards wait out the link window for a split amount to complete them."""
        if not payload or payload.get('event_type')!='transfer_request_candidate':return 0
        candidate=payload.get('candidate') or {}
        if not (candidate.get('amount_missing') and candidate.get('account_number_candidates')):return 0
        now=time.time()
        try:expires=parsed_time(row['timestamp']).timestamp()+self.link_window
        except (ValueError,TypeError,KeyError):expires=now+self.link_window
        return min(expires,now+self.link_window) if expires>now else 0
    def settles(self,row):
        """A live receipt/"already paid" from either side settles the chat's pending split."""
        text=row.get('content') or ''
        if row.get('deleted') or not RECEIPT.search(text) or sensitive(text) or text.startswith(TEST_PREFIX):return False
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
        self.state.attempt_started(key)
        success,code=self.webhook.send(key,payload)
        if success:self.state.delivered(key)
        else:self.state.failed(key,attempts,code)
        return True
    def delivery_loop(self):
        while not self.stop.is_set():
            try:
                if not self.deliver_one():self.stop.wait(1)
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
            self.wake.wait(30);self.wake.clear()
            try:self.drain()
            except Exception:self.state.set(read_error='bridge_or_processing_error');self.stop.wait(2);self.wake.set()

def status_server(state,token,port=8755):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path!='/status':self.send_error(404);return
            auth=self.headers.get_all('Authorization') or []
            if len(auth)!=1 or not hmac.compare_digest(auth[0],'Bearer '+token):self.send_error(401);return
            content=json.dumps(state.status()).encode();self.send_response(200)
            self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server

def main():
    os.umask(0o077)
    token=os.environ['WHATSAPP_BRIDGE_READ_TOKEN']
    source_account=os.environ.get('WHATSAPP_TRANSFER_SOURCE_ACCOUNT','').strip() or DEFAULT_SOURCE_ACCOUNT
    state=State(os.environ.get('WHATSAPP_TRANSFER_STATE_DIR','/state'),source_account)
    bridge=BridgeReader(os.environ.get('WHATSAPP_BRIDGE_URL','http://127.0.0.1:8753/api'),token)
    webhook=Webhook(os.environ['WHATSAPP_TRANSFER_WEBHOOK_URL'],os.environ['WHATSAPP_TRANSFER_WEBHOOK_AUTH_HEADER'],os.environ['WHATSAPP_TRANSFER_WEBHOOK_AUTH_VALUE'])
    detector=Detector(state,bridge,webhook,os.environ['WHATSAPP_BRIDGE_INSTANCE_ID'],os.environ['WHATSAPP_TRANSFER_EXPECTED_PHONE'],
        [x.strip().lstrip('+') for x in os.environ.get('WHATSAPP_TRANSFER_EXCLUDED_PHONES','').split(',') if x.strip()],
        [x.strip() for x in os.environ.get('WHATSAPP_TRANSFER_EXCLUDED_NAMES','').split(',') if x.strip()],
        os.environ.get('WHATSAPP_TRANSFER_TEST_SENDER',''),source_account,
        parse_link_window(os.environ.get('WHATSAPP_TRANSFER_LINK_WINDOW_SECONDS')),
        parse_excluded_chat_phrases(os.environ.get('WHATSAPP_TRANSFER_EXCLUDED_CHAT_PHRASES')))
    server=status_server(state,token)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:(detector.stop.set(),detector.wake.set()))
    try:detector.run()
    finally:server.shutdown();server.server_close()

if __name__=='__main__':main()
