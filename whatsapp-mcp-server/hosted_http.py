"""Authenticated remote MCP behind a trusted HTTPS tunnel/reverse proxy."""
import os
import secrets
from urllib.parse import urlsplit

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings


class HostedApp:
    def __init__(self, app, public_url, token):
        url = urlsplit(public_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in ("", "/") or url.query or url.fragment
                or url.port not in (None, 443)):
            raise ValueError("A canonical HTTPS public origin is required")
        if len(token) < 32 or not token.isascii():
            raise ValueError("A separate MCP bearer credential of at least 32 ASCII characters is required")
        self.app = app
        self.hosts = {url.hostname.lower(), url.hostname.lower() + ":443"}
        self.authorization = ("Bearer " + token).encode("ascii")

    @staticmethod
    async def reject(send, status):
        headers = [(b"content-type", b"text/plain"), (b"cache-control", b"no-store")]
        if status == 401:
            headers.append((b"www-authenticate", b'Bearer realm="whatsapp-secondary"'))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"Request refused"})

    async def __call__(self, scope, receive, send):
        original_send = send

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in (b"cache-control", b"strict-transport-security")]
                headers += [(b"cache-control", b"no-store"),
                            (b"strict-transport-security", b"max-age=31536000")]
                message = {**message, "headers": headers}
            await original_send(message)

        send = private_send
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        values = {}
        for key, value in scope.get("headers", []):
            values.setdefault(key.lower(), []).append(value)
        if (len(values.get(b"host", [])) != 1
                or values[b"host"][0].decode("ascii", "replace").lower() not in self.hosts
                or b"origin" in values or b"forwarded" in values):
            await self.reject(send, 403)
            return
        auth = values.get(b"authorization", [])
        if len(auth) != 1 or not secrets.compare_digest(auth[0], self.authorization):
            await self.reject(send, 401)
            return
        if scope.get("path") not in ("/mcp", "/mcp/") or scope.get("query_string"):
            await self.reject(send, 404)
            return
        # Buffer at most 1 MiB before MCP parsing, including chunked requests.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > 1024 * 1024:
                await self.reject(send, 413)
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        # Auth is consumed only at this boundary; it is never forwarded to WhatsApp.
        await self.app(scope, bounded_receive, send)


def run_hosted_http(mcp, port):
    public_url = os.environ.get("WHATSAPP_MCP_PUBLIC_URL", "")
    token = os.environ.get("WHATSAPP_MCP_TOKEN", "")
    app = HostedApp(None, public_url, token)
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=sorted(app.hosts), allowed_origins=[])
    # Configure before constructing the SDK's lazily cached session manager.
    app.app = mcp.streamable_http_app()
    bind_host = os.environ.get("WHATSAPP_MCP_BIND_HOST", "127.0.0.1")
    if bind_host not in ("127.0.0.1", "0.0.0.0"):
        raise ValueError("Unsupported bind host")
    uvicorn.run(app, host=bind_host, port=port, log_level="warning", access_log=False,
                proxy_headers=False, limit_concurrency=32, timeout_keep_alive=5)
