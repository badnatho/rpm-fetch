import base64
import unittest

from . import _pathsetup  # noqa: F401  (sys.path side effect)
from rpm_fetch.http import Auth, HttpClient, RETRYABLE_STATUS, WarmResult


class AuthHeaderTests(unittest.TestCase):
    def test_anonymous_has_no_headers(self):
        self.assertEqual(Auth().headers(), {})

    def test_bearer_token(self):
        self.assertEqual(Auth(token="abc").headers(), {"Authorization": "Bearer abc"})

    def test_basic_auth_encodes_user_and_password(self):
        header = Auth(username="u", password="p").headers()["Authorization"]
        self.assertEqual(header, "Basic " + base64.b64encode(b"u:p").decode())

    def test_basic_auth_allows_empty_password(self):
        header = Auth(username="u").headers()["Authorization"]
        self.assertEqual(header, "Basic " + base64.b64encode(b"u:").decode())

    def test_api_key_header(self):
        self.assertEqual(Auth(api_key="k").headers(), {"X-JFrog-Art-Api": "k"})

    def test_token_takes_precedence_over_basic_and_apikey(self):
        headers = Auth(token="t", username="u", api_key="k").headers()
        self.assertEqual(headers, {"Authorization": "Bearer t"})


class RetryBehaviourTests(unittest.TestCase):
    """warm_one retries transient failures using an injected sleep + opener."""

    class _FakeResponse:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *_):
            return b""

    class _ScriptedOpener:
        """Returns/raises according to a scripted list of outcomes."""

        def __init__(self, outcomes):
            self._outcomes = list(outcomes)
            self.calls = 0

        def open(self, request, timeout=None):
            self.calls += 1
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return RetryBehaviourTests._FakeResponse(outcome)

    def _client(self, outcomes, retries=2):
        client = HttpClient(retries=retries, sleep=lambda _s: None)
        client._opener = self._ScriptedOpener(outcomes)
        return client

    def test_retries_then_succeeds(self):
        import urllib.error

        err = urllib.error.URLError("temporary")
        client = self._client([err, 200])
        result = client.warm_one("https://art/pkg.rpm")
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)

    def test_gives_up_after_retries_exhausted(self):
        import urllib.error

        err = urllib.error.URLError("down")
        client = self._client([err, err, err], retries=2)
        result = client.warm_one("https://art/pkg.rpm")
        self.assertFalse(result.ok)
        self.assertEqual(client._opener.calls, 3)  # 1 try + 2 retries

    def test_http_4xx_does_not_retry(self):
        import urllib.error

        err = urllib.error.HTTPError("https://art/pkg.rpm", 404, "Not Found", {}, None)
        client = self._client([err], retries=2)
        result = client.warm_one("https://art/pkg.rpm")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 404)
        self.assertEqual(client._opener.calls, 1)  # no retry on 404

    def test_http_503_is_retryable(self):
        self.assertIn(503, RETRYABLE_STATUS)
        import urllib.error

        err = urllib.error.HTTPError("https://art/pkg.rpm", 503, "Busy", {}, None)
        client = self._client([err, 200], retries=2)
        result = client.warm_one("https://art/pkg.rpm")
        self.assertTrue(result.ok)
        self.assertEqual(client._opener.calls, 2)

    def test_malformed_response_becomes_failure_not_crash(self):
        # http.client.HTTPException is not an OSError; it must not escape warm_one.
        import http.client

        client = self._client([http.client.BadStatusLine("garbage")], retries=0)
        result = client.warm_one("https://art/pkg.rpm")
        self.assertFalse(result.ok)
        self.assertEqual(client._opener.calls, 1)


class GetBytesRetryCloseTests(unittest.TestCase):
    """get_bytes must close a retryable HTTPError's response (no socket leak)."""

    class _SuccessResponse:
        def __init__(self, body=b"DATA"):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *_):
            return self._body

    class _ScriptedOpener:
        def __init__(self, outcomes):
            self._outcomes = list(outcomes)

        def open(self, request, timeout=None):
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    def test_retryable_httperror_is_closed_before_retry(self):
        import urllib.error

        closed = []

        class TrackingHTTPError(urllib.error.HTTPError):
            def close(self):
                closed.append(True)
                super().close()

        err = TrackingHTTPError("https://art/primary.xml.gz", 503, "Busy", {}, None)
        client = HttpClient(retries=1, sleep=lambda _s: None)
        client._opener = self._ScriptedOpener([err, self._SuccessResponse(b"DATA")])

        self.assertEqual(client.get_bytes("https://art/primary.xml.gz"), b"DATA")
        self.assertEqual(closed, [True], "the retried 503 response should have been closed")

    def test_malformed_response_is_retried(self):
        import http.client

        client = HttpClient(retries=1, sleep=lambda _s: None)
        client._opener = self._ScriptedOpener(
            [http.client.BadStatusLine("garbage"), self._SuccessResponse(b"OK")]
        )
        self.assertEqual(client.get_bytes("https://art/primary.xml.gz"), b"OK")

    def test_oversized_body_is_rejected(self):
        import rpm_fetch.http as http_mod

        original = http_mod._MAX_BUFFERED_BYTES
        http_mod._MAX_BUFFERED_BYTES = 8
        try:
            client = HttpClient(retries=0, sleep=lambda _s: None)
            client._opener = self._ScriptedOpener([self._SuccessResponse(b"X" * 9)])
            with self.assertRaises(OSError) as ctx:
                client.get_bytes("https://art/repomd.xml")
            self.assertIn("refusing to buffer", str(ctx.exception))
        finally:
            http_mod._MAX_BUFFERED_BYTES = original


class RetriesClampTests(unittest.TestCase):
    """A negative retries value must not make the attempt loop empty."""

    def test_negative_retries_clamped_to_zero(self):
        self.assertEqual(HttpClient(retries=-3).retries, 0)

    def test_negative_retries_propagates_error_not_assertionerror(self):
        import urllib.error

        class _AlwaysFails:
            def open(self, *args, **kwargs):
                raise urllib.error.URLError("boom")

        client = HttpClient(retries=-1, sleep=lambda _s: None)
        client._opener = _AlwaysFails()
        with self.assertRaises(urllib.error.URLError):
            client.get_bytes("https://art/x")


class RetryDelayTests(unittest.TestCase):
    """Retry pacing: server-driven (Retry-After) or jittered exponential backoff."""

    def _run_one_retry(self, err, jitter=lambda backoff: 0.0, backoff_base=0.5):
        sleeps: list[float] = []
        client = HttpClient(retries=1, sleep=sleeps.append, jitter=jitter, backoff_base=backoff_base)
        client._opener = RetryBehaviourTests._ScriptedOpener([err, 200])
        result = client.warm_one("https://art/pkg.rpm")
        self.assertTrue(result.ok)
        self.assertEqual(len(sleeps), 1)
        return sleeps[0]

    def test_retry_after_header_is_honored(self):
        import urllib.error

        err = urllib.error.HTTPError("https://art/x", 429, "Too Many", {"Retry-After": "7"}, None)
        self.assertEqual(self._run_one_retry(err), 7.0)

    def test_retry_after_is_capped(self):
        import urllib.error

        err = urllib.error.HTTPError("https://art/x", 503, "Busy", {"Retry-After": "3600"}, None)
        self.assertEqual(self._run_one_retry(err), 60.0)

    def test_unparsable_retry_after_falls_back_to_backoff(self):
        import urllib.error

        err = urllib.error.HTTPError(
            "https://art/x", 503, "Busy", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None
        )
        self.assertEqual(self._run_one_retry(err, backoff_base=0.5), 0.5)

    def test_jitter_is_added_to_backoff(self):
        import urllib.error

        err = urllib.error.URLError("down")  # no headers -> backoff + jitter path
        delay = self._run_one_retry(err, jitter=lambda backoff: backoff / 2, backoff_base=0.5)
        self.assertEqual(delay, 0.75)  # 0.5 backoff + 0.25 jitter

    def test_default_jitter_is_bounded(self):
        import urllib.error

        err = urllib.error.URLError("down")
        sleeps: list[float] = []
        client = HttpClient(retries=1, sleep=sleeps.append)  # default (random) jitter
        client._opener = RetryBehaviourTests._ScriptedOpener([err, 200])
        client.warm_one("https://art/pkg.rpm")
        self.assertTrue(0.5 <= sleeps[0] <= 0.75)  # backoff .. backoff * 1.5


class OriginTests(unittest.TestCase):
    def test_same_origin(self):
        from rpm_fetch.http import same_origin

        self.assertTrue(same_origin("https://h/a", "https://h:443/b"))  # default port
        self.assertFalse(same_origin("https://h/a", "https://h:8443/b"))  # port change
        self.assertFalse(same_origin("https://h/a", "http://h/b"))  # scheme downgrade
        self.assertFalse(same_origin("https://h/a", "https://evil/b"))  # host change


class CredentialStrippingLogicTests(unittest.TestCase):
    def setUp(self):
        from rpm_fetch.http import _should_strip_credentials

        self.strip = _should_strip_credentials

    def test_keeps_same_origin(self):
        self.assertFalse(self.strip("https://h/a", "https://h/b"))

    def test_strips_cross_host(self):
        self.assertTrue(self.strip("https://h/a", "https://s3.example/x"))

    def test_strips_https_to_http_downgrade(self):
        self.assertTrue(self.strip("https://h/a", "http://h/b"))

    def test_allows_http_to_https_upgrade(self):
        self.assertFalse(self.strip("http://h/a", "https://h/b"))

    def test_strips_port_change(self):
        self.assertTrue(self.strip("https://h/a", "https://h:8443/b"))


class EncodeUrlPathTests(unittest.TestCase):
    def setUp(self):
        from rpm_fetch.http import encode_url_path

        self.enc = encode_url_path

    def test_encodes_caret_in_rpm_name(self):
        self.assertEqual(
            self.enc("https://h/Packages/c/crontabs-1.11^2019-6.el10.noarch.rpm"),
            "https://h/Packages/c/crontabs-1.11%5E2019-6.el10.noarch.rpm",
        )

    def test_encodes_space(self):
        self.assertEqual(self.enc("https://h/a b.rpm"), "https://h/a%20b.rpm")

    def test_preserves_slashes_and_safe_chars(self):
        url = "https://h/a/b/c-1.2+3~4.rpm"
        self.assertEqual(self.enc(url), url)

    def test_does_not_double_encode(self):
        url = "https://h/a%5Eb.rpm"
        self.assertEqual(self.enc(url), url)

    def test_leaves_host_port_and_query_untouched(self):
        self.assertEqual(self.enc("https://h:8443/p/x^y?a=1&b=2"), "https://h:8443/p/x%5Ey?a=1&b=2")


class WarmResultTests(unittest.TestCase):
    def test_detail_prefers_status(self):
        self.assertEqual(WarmResult("u", 200, True, None, 1, 0.0).detail, "HTTP 200")

    def test_detail_falls_back_to_error(self):
        self.assertEqual(WarmResult("u", None, False, "timeout", 1, 0.0).detail, "timeout")


if __name__ == "__main__":
    unittest.main()
