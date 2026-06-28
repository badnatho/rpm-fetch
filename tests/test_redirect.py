"""Regression tests for redirect handling against presigned-URL backends.

Artifactory 302-redirects artifact/metadata GETs to a presigned S3 URL whose
auth lives in the query string; if the client re-sends its own ``Authorization``
header, S3 returns ``400 InvalidArgument`` (two auth mechanisms). The client must
therefore strip credentials when a redirect crosses to a different host, while
keeping them on same-host redirects.

Two loopback addresses stand in for the two hosts: 127.0.0.1 (the "Artifactory")
and 127.0.0.2 (the "S3"). Both are loopback on Linux, so no DNS is involved, yet
``urlsplit().hostname`` sees them as different hosts — exactly the condition the
stripping logic keys on.
"""

import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import _pathsetup  # noqa: F401  (sys.path side effect)
from rpm_fetch.http import Auth, HttpClient


def _make_s3_handler(record, lock):
    class S3Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            auth = self.headers.get("Authorization")
            with lock:
                record["s3_auth"] = auth
            if auth is not None:
                # Mimic S3 rejecting a request that carries two auth mechanisms.
                self.send_error(400, "two auth mechanisms")
                return
            body = b"PRIMARY-DATA"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return S3Handler


def _make_artifactory_handler(s3_url, record, lock):
    class ArtifactoryHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/metadata":  # cross-host redirect to "S3"
                self.send_response(302)
                self.send_header("Location", s3_url)
                self.end_headers()
            elif self.path == "/same-redirect":  # same-host redirect
                self.send_response(302)
                self.send_header("Location", "/landing")
                self.end_headers()
            elif self.path == "/landing":
                with lock:
                    record["landing_auth"] = self.headers.get("Authorization")
                body = b"SAME-HOST-DATA"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

    return ArtifactoryHandler


class RedirectAuthStrippingTests(unittest.TestCase):
    def setUp(self):
        self.record: dict[str, str | None] = {}
        self.lock = threading.Lock()

        self.s3 = ThreadingHTTPServer(("127.0.0.2", 0), _make_s3_handler(self.record, self.lock))
        s3_port = self.s3.server_address[1]
        s3_url = f"http://127.0.0.2:{s3_port}/blob"

        self.art = ThreadingHTTPServer(
            ("127.0.0.1", 0), _make_artifactory_handler(s3_url, self.record, self.lock)
        )
        art_port = self.art.server_address[1]
        self.art_base = f"http://127.0.0.1:{art_port}"

        self.threads = []
        for server in (self.s3, self.art):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

        self.client = HttpClient(Auth(token="secret"), retries=0, sleep=lambda _s: None)

    def tearDown(self):
        for server in (self.s3, self.art):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=5)

    def test_cross_host_redirect_strips_authorization(self):
        # Would 400 if the bearer token leaked to "S3"; succeeds because it doesn't.
        data = self.client.get_bytes(self.art_base + "/metadata")
        self.assertEqual(data, b"PRIMARY-DATA")
        self.assertIsNone(self.record["s3_auth"], "Authorization must not cross to another host")

    def test_same_host_redirect_keeps_authorization(self):
        data = self.client.get_bytes(self.art_base + "/same-redirect")
        self.assertEqual(data, b"SAME-HOST-DATA")
        self.assertEqual(self.record["landing_auth"], "Bearer secret")


class HttpErrorBodyTests(unittest.TestCase):
    """get_bytes should fold the server's error body into the exception message."""

    def setUp(self):
        class ErrorHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b'{"errors":[{"status":400,"message":"explain the problem"}]}'
                self.send_response(400)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ErrorHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_error_message_includes_response_body(self):
        client = HttpClient(retries=0, sleep=lambda _s: None)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            client.get_bytes(self.base + "/anything")
        self.assertIn("explain the problem", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
