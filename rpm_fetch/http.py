"""A small urllib-based HTTP client with auth, retries, and HEAD/GET warming.

Deliberately stdlib-only. ``urllib`` is clumsier than ``requests`` but it is
always present, and the surface this tool needs (custom method, a couple of
headers, a timeout, bounded retries) is small enough that the clumsiness stays
contained here.

Thread-safety: a single :class:`HttpClient` is shared across the warming thread
pool. ``urllib`` openers are stateless per-request, so concurrent ``warm_one``
calls are safe; the only shared mutable thing would be the SSL context, which is
read-only after construction.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

from . import __version__

__all__ = ["Auth", "HttpClient", "WarmResult", "RETRYABLE_STATUS"]

# Transient server/throttling responses worth retrying; 4xx (other than 429) are
# the caller's fault and will never succeed on retry, so they fail fast.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Connection-level failures handled as transient (retried, then surfaced as a
# failure rather than a crash). http.client.HTTPException covers malformed
# responses (BadStatusLine, IncompleteRead, ...) and is NOT an OSError, so it
# must be named explicitly or it escapes and kills the whole run.
_TRANSIENT_ERRORS = (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException)

_DEFAULT_USER_AGENT = f"rpm-fetch/{__version__}"

# Headers carrying credentials, stripped when a redirect leaves the origin.
_CREDENTIAL_HEADERS = frozenset({"authorization", "x-jfrog-art-api", "cookie"})

_DEFAULT_PORTS = {"https": 443, "http": 80}


def origin(url: str) -> tuple[str, str, int | None]:
    """The (scheme, host, effective-port) tuple a credential is scoped to."""
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme)


def same_origin(a: str, b: str) -> bool:
    """True if *a* and *b* share scheme, host, and effective port."""
    return origin(a) == origin(b)


def encode_url_path(url: str) -> str:
    """Percent-encode characters that are illegal in *url*'s path component.

    Repodata hrefs are raw filenames, and RPM names legitimately contain
    characters that are not allowed unescaped in a URL — e.g. ``^`` (snapshot
    versions, which must be ``%5E``) — which otherwise make Artifactory/S3 answer
    ``400 Bad Request``. Path separators, sub-delims, and existing ``%XX`` escapes
    are preserved, so an already-encoded href is not double-encoded.
    """
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/:@!$&'()*+,;=~%")
    return urllib.parse.urlunsplit(parts._replace(path=path))


def _should_strip_credentials(old_url: str, new_url: str) -> bool:
    """Whether a redirect old_url -> new_url must not carry credentials.

    Mirrors requests' ``should_strip_auth``: strip on any host change, and on a
    scheme/port change too (so an ``https -> http`` downgrade can't leak the
    bearer token over cleartext) — but permit the safe ``http -> https`` upgrade
    on default ports.
    """
    old = urllib.parse.urlsplit(old_url)
    new = urllib.parse.urlsplit(new_url)
    if (old.hostname or "").lower() != (new.hostname or "").lower():
        return True
    if old.scheme == "http" and (old.port or 80) == 80 and new.scheme == "https" and (new.port or 443) == 443:
        return False  # safe upgrade, same host
    old_port = old.port or _DEFAULT_PORTS.get(old.scheme)
    new_port = new.port or _DEFAULT_PORTS.get(new.scheme)
    return old.scheme != new.scheme or old_port != new_port


class _CredentialStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but drop credentials when the target leaves the origin.

    Artifactory answers artifact/metadata GETs with a 302 to a *presigned* S3
    (or CDN) URL whose authentication already lives in the query string. urllib's
    stock handler re-sends our ``Authorization`` header to that target, and S3
    rejects requests bearing two auth mechanisms with ``400 InvalidArgument``.
    Stripping credentials when the origin changes fixes that and — like requests
    and browsers — avoids leaking the bearer token to a third party or over a
    downgraded (https -> http) hop. Same-origin redirects keep their headers so
    normal Artifactory auth still works.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _should_strip_credentials(req.full_url, newurl):
            for name in [h for h in new.headers if h.lower() in _CREDENTIAL_HEADERS]:
                del new.headers[name]
        return new


@dataclass(frozen=True)
class Auth:
    """Authentication material resolved from CLI flags / environment.

    Exactly one scheme's headers are emitted, in precedence order: bearer token,
    then basic, then API key. ``Auth()`` (all-None) means anonymous.
    """

    token: str | None = None
    username: str | None = None
    password: str | None = None
    api_key: str | None = None

    def headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        if self.username is not None:
            raw = f"{self.username}:{self.password or ''}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        if self.api_key:
            return {"X-JFrog-Art-Api": self.api_key}
        return {}


@dataclass(frozen=True)
class WarmResult:
    """Outcome of warming one package URL."""

    url: str
    status: int | None
    ok: bool
    error: str | None
    attempts: int
    elapsed: float

    @property
    def detail(self) -> str:
        if self.status is not None:
            return f"HTTP {self.status}"
        return self.error or "unknown error"


class HttpClient:
    def __init__(
        self,
        auth: Auth | None = None,
        *,
        timeout: float = 30.0,
        retries: int = 2,
        insecure: bool = False,
        user_agent: str = _DEFAULT_USER_AGENT,
        backoff_base: float = 0.5,
        backoff_cap: float = 10.0,
        sleep=time.sleep,
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)  # negative would make the attempt loop empty
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self._base_headers = {"User-Agent": user_agent, **(auth or Auth()).headers()}

        # Replaces urllib's default redirect handler (build_opener prefers a
        # passed-in subclass) so the bearer token is dropped on the cross-host
        # hop to presigned S3/CDN URLs instead of colliding with their auth.
        handlers: list[urllib.request.BaseHandler] = [_CredentialStrippingRedirectHandler()]
        if insecure:
            # Corporate Artifactory often sits behind an internal CA; --insecure
            # trades verification for "it works on the VPN".
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        self._opener = urllib.request.build_opener(*handlers)

    def _request(self, url: str, method: str) -> urllib.request.Request:
        return urllib.request.Request(url, method=method, headers=dict(self._base_headers))

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_cap)

    def _open_with_retries(self, url: str, method: str):
        """Open *url*, retrying transient failures, and return the live response.

        The caller owns the returned response and must close it. Retries cover
        the connection and initial response only — by the time a response is
        returned, redirects (e.g. to presigned S3) have been followed. On final
        failure raises; non-retryable HTTP errors carry the server's body.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 2):
            try:
                return self._opener.open(self._request(url, method), timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in RETRYABLE_STATUS and attempt <= self.retries:
                    exc.close()  # an HTTPError is the live response; free its socket before retrying
                    last_exc = exc
                    self._sleep(self._backoff(attempt))
                    continue
                raise _with_response_body(exc, url)
            except _TRANSIENT_ERRORS as exc:
                last_exc = exc
                if attempt <= self.retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise
        # Unreachable: retries >= 0 guarantees >= 1 attempt, and every attempt
        # returns or raises. Guarded so a future change can't `raise None`.
        raise last_exc if last_exc is not None else RuntimeError("request loop produced no result")

    def get_bytes(self, url: str) -> bytes:
        """GET *url* and return the full body, retrying transient failures.

        Used for small metadata (repomd.xml) where buffering is fine. Raises on
        final failure — a missing repomd is fatal to the whole run.
        """
        with self._open_with_retries(url, "GET") as resp:
            return resp.read()

    @contextlib.contextmanager
    def open_stream(self, url: str) -> Iterator[BinaryIO]:
        """Yield a live, readable GET response for *url* — for streaming large
        metadata (primary.xml) without buffering it all in memory.

        The connection/initial response is retried like :meth:`get_bytes`, but a
        mid-stream failure is not retried (it surfaces to the caller). The
        response is closed on exit.
        """
        resp = self._open_with_retries(url, "GET")
        try:
            yield resp
        finally:
            resp.close()

    def warm_one(self, url: str, method: str = "HEAD") -> WarmResult:
        """Issue one warming request, returning a result instead of raising.

        For ``GET`` the body is drained so Artifactory actually fetches and
        stores the artifact; for ``HEAD`` we only read the status line. Any
        2xx/3xx counts as a hit.
        """
        start = time.monotonic()
        last_error: str | None = None
        for attempt in range(1, self.retries + 2):
            try:
                with self._opener.open(self._request(url, method), timeout=self.timeout) as resp:
                    if method == "GET":
                        while resp.read(65536):
                            pass
                    status = resp.status
                    return WarmResult(url, status, 200 <= status < 400, None, attempt, time.monotonic() - start)
            except urllib.error.HTTPError as exc:
                exc.close()  # an HTTPError is itself the response; free its socket
                if exc.code in RETRYABLE_STATUS and attempt <= self.retries:
                    last_error = f"HTTP {exc.code}"
                    self._sleep(self._backoff(attempt))
                    continue
                return WarmResult(url, exc.code, False, f"HTTP {exc.code}", attempt, time.monotonic() - start)
            except _TRANSIENT_ERRORS as exc:
                last_error = _describe(exc)
                if attempt <= self.retries:
                    self._sleep(self._backoff(attempt))
                    continue
                return WarmResult(url, None, False, last_error, attempt, time.monotonic() - start)
        # Unreachable: every path above returns or continues.
        return WarmResult(url, None, False, last_error or "unknown error", self.retries + 1, time.monotonic() - start)


def _describe(exc: Exception) -> str:
    reason = getattr(exc, "reason", None)
    return str(reason) if reason is not None else str(exc)


def _with_response_body(exc: urllib.error.HTTPError, url: str) -> urllib.error.HTTPError:
    """Fold the server's error body into the exception's message.

    urllib's ``HTTPError`` stringifies to just ``HTTP Error 400: Bad Request``;
    the actual cause (e.g. JFrog/S3's JSON ``message``) is in the body, which is
    otherwise discarded. Splicing a snippet into ``msg`` makes a failed metadata
    fetch self-explanatory instead of a guessing game.
    """
    try:
        body = exc.read(65536).decode("utf-8", "replace").strip()  # cap: a huge error page shouldn't be slurped
    except Exception:
        body = ""
    finally:
        exc.close()  # release the socket; str(exc) still works afterwards
    if body:
        snippet = " ".join(body.split())[:300]
        exc.msg = f"{exc.msg} — {snippet}"  # HTTPError.__str__ uses code + msg
    return exc
