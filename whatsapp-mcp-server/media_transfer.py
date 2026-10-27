"""Bounded media transport for remote MCP computers, with per-file capabilities."""
import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

MAX_BYTES=16*1024*1024
EXTENSIONS={'.pdf','.png','.jpg','.jpeg','.webp','.gif','.mp4','.ogg','.mp3','.m4a','.wav'}
CAP_SECONDS=300
RETAIN_SECONDS=86400

class MediaError(Exception):
    pass

class MediaTransfer:
    def __init__(self,root,origin):
        url=urlsplit(origin)
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.port not in (None,443) or url.path not in ('','/') or url.query or url.fragment:
            raise MediaError('media_hosting_unconfigured')
        self.origin=origin.rstrip('/');self.root=Path(root).expanduser().resolve()
        self.outbox=self.root/'outbox';self.temporary=self.root/'media-upload-tmp';self.database=self.root/'media-transfer.sqlite'
        for path in [self.root,self.outbox,self.temporary]:
            if path.is_symlink():raise MediaError('invalid_media_storage')
            path.mkdir(parents=True,mode=0o700,exist_ok=True);path.chmod(0o700)
        if self.database.is_symlink():raise MediaError('invalid_media_storage')
        fd=os.open(self.database,os.O_CREAT|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600);os.close(fd);self.database.chmod(0o600)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS uploads(
             id TEXT PRIMARY KEY,request_id TEXT UNIQUE,filename TEXT,size INTEGER,sha TEXT,
             token_hash TEXT,expires REAL,status TEXT,created REAL,lease REAL DEFAULT 0,path TEXT);
             CREATE TABLE IF NOT EXISTS downloads(
             id TEXT PRIMARY KEY,path TEXT,token_hash TEXT,expires REAL,used INTEGER DEFAULT 0,
             size INTEGER,sha TEXT,device INTEGER,inode INTEGER,mtime INTEGER);''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.database,timeout=10);db.row_factory=sqlite3.Row
        try:
            yield db;db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def prune(self,db):
        now=time.time()
        for row in db.execute("SELECT id FROM uploads WHERE status='uploading' AND lease<?",(now,)).fetchall():
            for path in self.temporary.glob(row['id']+'.*.part'):path.unlink(missing_ok=True)
        db.execute("UPDATE uploads SET status='failed',lease=0 WHERE (status='pending' AND expires<?) OR (status='uploading' AND lease<?)",(now,now))
        for row in db.execute('SELECT id,path FROM uploads WHERE created<? AND lease<?',(now-RETAIN_SECONDS,now)).fetchall():
            path=Path(row['path'])
            if path.parent==self.outbox and path.name.startswith('upload-'+row['id']):path.unlink(missing_ok=True)
            db.execute('DELETE FROM uploads WHERE id=?',(row['id'],))
        db.execute('DELETE FROM downloads WHERE expires<?',(now,))
        for path in self.temporary.glob('*.part'):
            if path.stat().st_mtime<now-RETAIN_SECONDS:path.unlink(missing_ok=True)

    def create_upload(self,filename,size,sha,request_id):
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',filename) or Path(filename).suffix.lower() not in EXTENSIONS
                or type(size) is not int or size<1 or size>MAX_BYTES or not re.fullmatch('[a-f0-9]{64}',sha)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}',request_id)):
            raise MediaError('invalid_media_upload')
        token=secrets.token_urlsafe(32);now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self.prune(db)
            row=db.execute('SELECT * FROM uploads WHERE request_id=?',(request_id,)).fetchone()
            if row and (row['filename'],row['size'],row['sha'])!=(filename,size,sha):raise MediaError('upload_request_conflict')
            if row and row['status']=='done' and Path(row['path']).is_file():
                return {'success':True,'ready':True,'media_path':row['path'],'size_bytes':size,'sha256':sha}
            if row and row['status']=='uploading' and row['lease']>now:raise MediaError('upload_in_progress')
            count=db.execute('SELECT count(*) FROM uploads').fetchone()[0]
            active,total=db.execute("SELECT count(*),coalesce(sum(size),0) FROM uploads WHERE request_id!=? AND status IN ('pending','uploading','done')",(request_id,)).fetchone()
            pending=db.execute("SELECT count(*) FROM uploads WHERE request_id!=? AND status IN ('pending','uploading')",(request_id,)).fetchone()[0]
            if (not row and count>=512) or total+size>256*1024*1024 or pending>=32:raise MediaError('media_staging_quota')
            if not row:
                identifier=secrets.token_hex(16);path=str(self.outbox/('upload-'+identifier+Path(filename).suffix.lower()))
                db.execute('INSERT INTO uploads(id,request_id,filename,size,sha,created,path) VALUES(?,?,?,?,?,?,?)',
                           (identifier,request_id,filename,size,sha,now,path))
            else:identifier,path=row['id'],row['path']
            db.execute('UPDATE uploads SET token_hash=?,expires=?,status=?,lease=0 WHERE id=?',
                       (hashlib.sha256(token.encode()).hexdigest(),now+CAP_SECONDS,'pending',identifier))
        return {'success':True,'ready':False,'upload_id':identifier,'media_path':path,'upload_method':'PUT',
                'upload_url':self.origin+'/media/upload/'+identifier,'upload_headers':{'Authorization':'Media '+token},
                'expires_in_seconds':CAP_SECONDS,'size_bytes':size,'sha256':sha}

    def download(self,media_path):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self.prune(db)
        path=Path(media_path).expanduser().resolve()
        if not path.is_relative_to(self.root) or path.suffix.lower() not in EXTENSIONS or not path.is_file():raise MediaError('invalid_media_path')
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        try:
            stat=os.fstat(fd)
            if stat.st_size<1 or stat.st_size>MAX_BYTES:raise MediaError('invalid_media_size')
            digest=hashlib.sha256()
            while chunk:=os.read(fd,65536):digest.update(chunk)
        finally:os.close(fd)
        identifier=secrets.token_hex(16);token=secrets.token_urlsafe(32);sha=digest.hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM downloads').fetchone()[0]>=128:raise MediaError('media_download_quota')
            db.execute('INSERT INTO downloads(id,path,token_hash,expires,size,sha,device,inode,mtime) VALUES(?,?,?,?,?,?,?,?,?)',
                       (identifier,str(path),hashlib.sha256(token.encode()).hexdigest(),time.time()+CAP_SECONDS,
                        stat.st_size,sha,stat.st_dev,stat.st_ino,stat.st_mtime_ns))
        return {'success':True,'download_url':self.origin+'/media/download/'+identifier,
                'download_headers':{'Authorization':'Media '+token},'filename':path.name,'size_bytes':stat.st_size,
                'sha256':sha,'expires_in_seconds':CAP_SECONDS,'read_only':True}

    @staticmethod
    async def respond(send,status,body):
        data=json.dumps(body).encode()
        await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json'),
                    (b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff')]})
        await send({'type':'http.response.body','body':data})

    async def __call__(self,scope,receive,send):
        match=re.fullmatch(r'/media/(upload|download)/([a-f0-9]{32})',scope.get('path',''))
        if not match or scope.get('query_string'):
            await self.respond(send,404,{'success':False,'code':'media_route_not_found'});return
        kind,identifier=match.groups();method='PUT' if kind=='upload' else 'GET'
        if scope.get('method')!=method:
            await self.respond(send,405,{'success':False,'code':'media_method_refused'});return
        auth=[v for k,v in scope.get('headers',[]) if k.lower()==b'authorization']
        if len(auth)!=1 or not auth[0].startswith(b'Media ') or not 32<=len(auth[0][6:])<=128:
            await self.respond(send,401,{'success':False,'code':'media_capability_required'});return
        digest=hashlib.sha256(auth[0][6:]).hexdigest();table='uploads' if kind=='upload' else 'downloads'
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM '+table+' WHERE id=?',(identifier,)).fetchone()
            if not row or row['expires']<time.time() or not secrets.compare_digest(row['token_hash'],digest):
                await self.respond(send,401,{'success':False,'code':'media_capability_invalid'});return
            if (kind=='upload' and row['status']!='pending') or (kind=='download' and row['used']):
                await self.respond(send,409,{'success':False,'code':'media_capability_used'});return
            if kind=='upload':
                lengths=[v for k,v in scope.get('headers',[]) if k.lower()==b'content-length']
                try:valid_length=len(lengths)<=1 and (not lengths or int(lengths[0])==row['size'])
                except ValueError:valid_length=False
                if not valid_length:
                    await self.respond(send,400,{'success':False,'code':'media_length_mismatch'});return
                db.execute("UPDATE uploads SET status='uploading',lease=? WHERE id=?",(time.time()+180,identifier))
            else:db.execute('UPDATE downloads SET used=1 WHERE id=?',(identifier,))
        if kind=='download':
            try:
                path=Path(row['path'])
                if path.resolve()!=path or not path.is_relative_to(self.root):raise MediaError('media_changed')
                fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0));stat=os.fstat(fd)
                if (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)!=(row['device'],row['inode'],row['size'],row['mtime']):
                    os.close(fd);raise MediaError('media_changed')
            except (OSError,MediaError):
                await self.respond(send,409,{'success':False,'code':'media_changed'});return
            try:
                await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/octet-stream'),
                    (b'content-length',str(row['size']).encode()),(b'content-disposition',b'attachment'),
                    (b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),(b'referrer-policy',b'no-referrer')]})
                while chunk:=os.read(fd,65536):await send({'type':'http.response.body','body':chunk,'more_body':True})
                await send({'type':'http.response.body','body':b'','more_body':False})
            finally:os.close(fd)
            return
        temporary=self.temporary/(identifier+'.'+secrets.token_hex(8)+'.part');done=False;installed=False
        try:
            fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
            size=0;sha=hashlib.sha256()
            try:
                async with asyncio.timeout(120):
                    while True:
                        message=await receive()
                        if message['type']=='http.disconnect':raise MediaError('media_upload_disconnected')
                        chunk=message.get('body',b'');size+=len(chunk)
                        if size>row['size'] or size>MAX_BYTES:raise MediaError('media_upload_too_large')
                        sha.update(chunk)
                        with memoryview(chunk) as remaining:
                            while remaining:
                                written=os.write(fd,remaining)
                                if written<=0:raise OSError('media_write_failed')
                                remaining=remaining[written:]
                        if not message.get('more_body',False):break
                os.fsync(fd)
            finally:os.close(fd)
            if size!=row['size'] or sha.hexdigest()!=row['sha']:raise MediaError('media_upload_checksum_mismatch')
            os.replace(temporary,row['path'])
            installed=True
            with self.db() as db:db.execute("UPDATE uploads SET status='done',lease=0 WHERE id=?",(identifier,))
            done=True
            await self.respond(send,200,{'success':True,'ready':True,'media_path':row['path'],'size_bytes':size,'sha256':row['sha']})
        except (MediaError,OSError,sqlite3.Error,TimeoutError):
            await self.respond(send,400,{'success':False,'code':'media_upload_failed'})
        finally:
            temporary.unlink(missing_ok=True)
            if not done:
                if installed:Path(row['path']).unlink(missing_ok=True)
                with self.db() as db:db.execute("UPDATE uploads SET status='failed',lease=0 WHERE id=?",(identifier,))

def configured_media():
    root=os.environ.get('WHATSAPP_STORE_DIR');origin=os.environ.get('WHATSAPP_MCP_PUBLIC_URL')
    if not root or not origin:raise MediaError('media_hosting_unconfigured')
    return MediaTransfer(root,origin)

def create_media_upload(filename:str,size_bytes:int,sha256:str,request_id:str)->dict:
    """Stage a file FROM YOUR COMPUTER for hosted send_file/send_audio_message.

    Compute its SHA-256 and byte count locally. Reuse request_id for retries.
    If ready=false, upload the binary file with HTTP PUT to upload_url using
    upload_headers from your computer, then pass media_path to send_file.
    Five-minute capability, 16 MiB maximum, 24-hour staging retention. Headers
    are private: never print them in chat. No WhatsApp message is sent here.
    """
    try:return configured_media().create_upload(filename,size_bytes,sha256,request_id)
    except MediaError as error:return {'success':False,'code':str(error)}
    except (OSError,sqlite3.Error):return {'success':False,'code':'media_storage_unavailable'}

def get_media_download(media_path:str)->dict:
    """Get a five-minute, one-use media download for your remote computer.

    Use a server file_path returned by download_media/download_profile_photo,
    or a staged media_path. Fetch download_url with download_headers; verify
    size_bytes/sha256. This never sends or marks a WhatsApp message read.
    Keep the returned authorization header private and out of chat.
    """
    try:return configured_media().download(media_path)
    except MediaError as error:return {'success':False,'code':str(error)}
    except (OSError,sqlite3.Error):return {'success':False,'code':'media_storage_unavailable'}
