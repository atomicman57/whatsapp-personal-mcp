"""Loopback-only MCP transport for a desktop client's local-device connection."""
import uvicorn


class LocalOnlyApp:
    def __init__(self, app, port):
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            host = headers.get(b"host", b"").decode("ascii", "replace")
            if (host not in self.hosts or b"origin" in headers or
                    b"forwarded" in headers or b"x-forwarded-for" in headers):
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Local client requests only"})
                return
        await self.app(scope, receive, send)


def run_local_http(mcp, port):
    uvicorn.run(LocalOnlyApp(mcp.streamable_http_app(), port),
                host="127.0.0.1", port=port, log_level="warning", access_log=False)
