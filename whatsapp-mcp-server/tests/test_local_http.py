import unittest
from local_http import LocalOnlyApp


class LocalHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_origin_forwarded_host_and_dns_rebinding_refused(self):
        reached = []

        async def app(scope, receive, send):
            reached.append(True)

        protected = LocalOnlyApp(app, 8744)
        for headers in (
            [(b"host", b"attacker.invalid:8744")],
            [(b"host", b"127.0.0.1:8744"), (b"origin", b"https://example.invalid")],
            [(b"host", b"127.0.0.1:8744"), (b"forwarded", b"for=example.invalid")],
        ):
            responses = []

            async def send(message):
                responses.append(message)

            await protected({"type": "http", "headers": headers}, None, send)
            self.assertEqual(responses[0]["status"], 403)
        self.assertEqual(reached, [])
        await protected({"type": "http", "headers": [(b"host", b"127.0.0.1:8744")]}, None, None)
        self.assertEqual(reached, [True])
