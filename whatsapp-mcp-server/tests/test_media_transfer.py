"""Remote file capabilities: bounded streaming, isolation and replay protection."""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from hosted_http import HostedApp
from media_transfer import MediaError, MediaTransfer, MAX_BYTES


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.media=MediaTransfer(self.directory.name,'https://media.example.test')
        self.app=HostedApp(None,'https://media.example.test','synthetic-secret-of-at-least-32-characters',media=self.media)

    def upload(self,data=b'synthetic receipt',request='receipt-0001'):
        return self.media.create_upload('receipt.pdf',len(data),hashlib.sha256(data).hexdigest(),request)

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(self.app),base_url=self.media.origin)

    async def test_large_upload_download_and_completed_retry(self):
        data=b'x'*(1024*1024+17);grant=self.upload(data)
        async with self.client() as client:
            response=await client.put(grant['upload_url'],headers=grant['upload_headers'],content=data)
            self.assertEqual(response.status_code,200)
            self.assertEqual(Path(grant['media_path']).read_bytes(),data)
            self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertIn('strict-transport-security',response.headers)
            replay=await client.put(grant['upload_url'],headers=grant['upload_headers'],content=data)
            self.assertEqual(replay.status_code,409)
            self.assertTrue(self.upload(data)['ready'])
            download=self.media.download(grant['media_path'])
            received=await client.get(download['download_url'],headers=download['download_headers'])
            self.assertEqual(received.content,data)
            self.assertEqual(hashlib.sha256(received.content).hexdigest(),download['sha256'])
            self.assertEqual((await client.get(download['download_url'],headers=download['download_headers'])).status_code,409)

    async def test_invalid_capabilities_never_read_body(self):
        grant=self.upload();messages=[]
        async def receive():
            self.fail('Unauthorized upload read its body')
        async def send(message):messages.append(message)
        path=urlsplit(grant['upload_url']).path
        for extra,status in [([],401),([(b'authorization',b'Bearer synthetic-secret-of-at-least-32-characters')],401),
                             ([(b'authorization',b'Media '+b'x'*40)],401),
                             ([(b'authorization',grant['upload_headers']['Authorization'].encode())]*2,401)]:
            messages.clear()
            await self.app({'type':'http','method':'PUT','path':path,'query_string':b'',
                'headers':[(b'host',b'media.example.test')]+extra},receive,send)
            self.assertEqual(messages[0]['status'],status)
        for headers in [{'Host':'evil.test'},{'Origin':'https://evil.test'}]:
            async with self.client() as client:
                response=await client.put(grant['upload_url'],headers={**grant['upload_headers'],**headers},content=b'synthetic receipt')
                self.assertEqual(response.status_code,403)

    async def test_rotated_expired_cross_account_and_query_caps(self):
        old=self.upload();new=self.upload()
        async with self.client() as client:
            self.assertEqual((await client.put(old['upload_url'],headers=old['upload_headers'],content=b'synthetic receipt')).status_code,401)
            self.assertEqual((await client.put(new['upload_url']+'?token=anything',headers=new['upload_headers'],content=b'synthetic receipt')).status_code,404)
            with self.media.db() as db:db.execute('UPDATE uploads SET expires=?',(time.time()-1,))
            self.assertEqual((await client.put(new['upload_url'],headers=new['upload_headers'],content=b'synthetic receipt')).status_code,401)
        with tempfile.TemporaryDirectory() as other:
            isolated=MediaTransfer(other,self.media.origin)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(isolated),base_url=self.media.origin) as client:
                self.assertEqual((await client.put(new['upload_url'],headers=new['upload_headers'],content=b'synthetic receipt')).status_code,401)

    async def test_mismatch_cleanup_and_retry(self):
        grant=self.upload()
        async with self.client() as client:
            response=await client.put(grant['upload_url'],headers=grant['upload_headers'],content=b'wrong same length')
            self.assertEqual(response.status_code,400)
            self.assertFalse(Path(grant['media_path']).exists())
            self.assertEqual(list(self.media.temporary.iterdir()),[])
            grant=self.upload()
            self.assertEqual((await client.put(grant['upload_url'],headers=grant['upload_headers'],content=b'synthetic receipt')).status_code,200)
        with self.assertRaises(MediaError):self.upload(b'different')

    async def test_disconnect_and_length_bounds(self):
        grant=self.upload();sent=[]
        async def receive():return {'type':'http.disconnect'}
        async def send(message):sent.append(message)
        await self.media({'type':'http','method':'PUT','path':urlsplit(grant['upload_url']).path,
            'headers':[(b'authorization',grant['upload_headers']['Authorization'].encode())]},receive,send)
        self.assertEqual(sent[0]['status'],400)
        self.assertFalse(Path(grant['media_path']).exists())
        grant=self.upload()
        async with self.client() as client:
            self.assertEqual((await client.put(grant['upload_url'],headers=grant['upload_headers'],content=b'x'*(MAX_BYTES+1))).status_code,400)

    async def test_download_change_and_path_jail(self):
        path=self.media.outbox/'synthetic.pdf';path.write_bytes(b'old')
        grant=self.media.download(str(path));path.write_bytes(b'changed')
        async with self.client() as client:
            self.assertEqual((await client.get(grant['download_url'],headers=grant['download_headers'])).status_code,409)
        for invalid in [str(self.media.database),'/etc/passwd',str(self.media.root/'missing.pdf')]:
            with self.assertRaises(MediaError):self.media.download(invalid)
        with tempfile.TemporaryDirectory() as outside:
            escaped=Path(outside)/'outside.pdf';escaped.write_bytes(b'private')
            link=self.media.outbox/'link.pdf';link.symlink_to(escaped)
            with self.assertRaises(MediaError):self.media.download(str(link))

    def test_metadata_and_pending_quota(self):
        for name,size,sha,key in [('../receipt.pdf',1,'a'*64,'valid-key'),('secret.sqlite',1,'a'*64,'valid-key'),
                                 ('receipt.pdf',MAX_BYTES+1,'a'*64,'valid-key'),('receipt.pdf',1,'x'*64,'valid-key')]:
            with self.assertRaises(MediaError):self.media.create_upload(name,size,sha,key)
        for i in range(32):self.upload(request=f'quota-key-{i:03}')
        with self.assertRaises(MediaError):self.upload(request='quota-overflow')
        with self.media.db() as db:db.execute('UPDATE uploads SET expires=?',(time.time()-1,))
        self.assertFalse(self.upload(request='quota-reclaimed')['ready'])
