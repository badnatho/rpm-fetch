"""Parse yum/dnf repository metadata into a list of package URLs.

The discovery chain mirrors how dnf reads a repo:

    <base>/repodata/repomd.xml        index of metadata files
        -> data[type=primary]/location@href     points at primary.xml(.gz/.zst)
            -> package/location@href             one entry per RPM

Both href layers are relative to the *repository root*, not to the file that
contains them (repomd's primary href already includes the ``repodata/`` prefix),
so everything resolves against the base URL with a single ``urljoin``.

primary.xml is the big one — hundreds of MB uncompressed for a full distro. It is
decompressed and parsed as a stream: ``iterparse`` clears the tree after each
package, so memory never holds more than one package's subtree. Given a streaming
client (the production path) the compressed bytes are pulled off the socket on
demand and never fully buffered either; given only a bytes-fetcher the compressed
file is buffered first, then its decompression is streamed.
"""

from __future__ import annotations

import http.client
import io
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import BinaryIO
from urllib.parse import urljoin

from .decompress import (
    DECOMPRESSION_ERRORS,
    UnsupportedCompressionError,
    ensure_supported,
    open_decompressed,
)
from .http import encode_url_path, same_origin

# Failures that mean "the primary stream was unreadable" — corrupt/truncated
# codec data, or a connection dropped mid-parse — as opposed to a logic error.
# OSError covers bad-gzip-header / bz2 / socket read failures; IncompleteRead is
# an HTTPException (not an OSError) so it is listed explicitly.
_PRIMARY_READ_ERRORS = (*DECOMPRESSION_ERRORS, http.client.IncompleteRead, OSError)

__all__ = [
    "RepoMetadataError",
    "repomd_url",
    "parse_repomd",
    "iter_package_hrefs",
    "discover_package_urls",
]

REPOMD_PATH = "repodata/repomd.xml"


class RepoMetadataError(Exception):
    """Raised when repository metadata is missing, malformed, or incomplete."""


def _localname(tag: str) -> str:
    """Strip the ``{namespace}`` prefix ElementTree prepends to qualified tags."""
    return tag.rsplit("}", 1)[-1]


def _with_trailing_slash(url: str) -> str:
    """Ensure *url* ends in ``/`` so it is treated as a directory by urljoin."""
    return url if url.endswith("/") else url + "/"


def repomd_url(base_url: str) -> str:
    """URL of ``repodata/repomd.xml`` under repository root *base_url*."""
    return urljoin(_with_trailing_slash(base_url), REPOMD_PATH)


def parse_repomd(data: bytes) -> dict[str, str]:
    """Map each ``<data type=...>`` to its location href from repomd.xml bytes.

    e.g. ``{"primary": "repodata/<hash>-primary.xml.zst", "filelists": ...}``.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RepoMetadataError(f"repomd.xml is not valid XML: {exc}") from exc

    locations: dict[str, str] = {}
    for data_el in root.findall("{*}data"):
        kind = data_el.get("type")
        location = data_el.find("{*}location")
        if kind and location is not None and location.get("href"):
            locations[kind] = location.get("href")
    if not locations:
        raise RepoMetadataError("repomd.xml contained no <data> entries")
    return locations


def iter_package_hrefs(stream: BinaryIO) -> Iterator[str]:
    """Yield every package ``location@href`` from a primary.xml *stream*.

    *stream* must already be decompressed. Parsing is incremental: each
    ``<package>`` subtree is dropped as soon as its href is read, so a repo with
    a million packages costs the same memory as one with ten.
    """
    context = ET.iterparse(stream, events=("start", "end"))
    try:
        _, root = next(context)  # grab <metadata> so we can clear it as we go
        for event, element in context:
            if event != "end" or _localname(element.tag) != "package":
                continue
            location = element.find("{*}location")
            if location is not None and location.get("href"):
                yield location.get("href")
            # Detach the finished package subtree from the root to bound memory.
            root.clear()
    except StopIteration:
        return  # empty stream: no <metadata> root at all
    except ET.ParseError as exc:
        raise RepoMetadataError(f"primary metadata is not valid XML: {exc}") from exc
    except _PRIMARY_READ_ERRORS as exc:
        # Truncated/corrupt compression or a dropped connection mid-parse: report
        # cleanly (exit 2) instead of leaking EOFError/zlib.error/ZstdError.
        raise RepoMetadataError(f"could not read primary metadata: {exc}") from exc


def _resolve_source(source):
    """Adapt *source* to ``(fetch_bytes, open_stream_or_None)``.

    Accepts either form so the function is convenient in production and trivial
    to fake in tests:

    * an ``HttpClient``-like object exposing ``get_bytes`` (and usually
      ``open_stream``) — the primary metadata is **streamed** off the socket;
    * a plain ``Callable[[str], bytes]`` — the primary metadata is fetched into
      memory and decompressed from a buffer.
    """
    if callable(source) and not hasattr(source, "get_bytes"):
        return source, None
    return source.get_bytes, getattr(source, "open_stream", None)


def _decompress_primary(fileobj: BinaryIO, href: str) -> BinaryIO:
    """open_decompressed, but surface an unsupported codec as RepoMetadataError."""
    try:
        return open_decompressed(fileobj, href)
    except UnsupportedCompressionError as exc:
        raise RepoMetadataError(str(exc)) from exc


def discover_package_urls(
    source,
    repo_base_url: str,
    metadata_base_url: str | None = None,
) -> list[str]:
    """Resolve every package into an absolute URL under *repo_base_url*.

    *source* is either an ``HttpClient``-like object (``get_bytes`` +
    ``open_stream``) or a ``Callable[[str], bytes]``; see :func:`_resolve_source`.
    With a client, ``primary`` is streamed and decompressed incrementally so peak
    memory stays flat; with a bare callable it is buffered then decompressed.

    *metadata_base_url* lets the repodata be read from somewhere other than the
    warm target (defaults to *repo_base_url*); package hrefs are always joined
    against *repo_base_url*, which is what gets cached.
    """
    fetch_bytes, open_stream = _resolve_source(source)
    metadata_base = _with_trailing_slash(metadata_base_url or repo_base_url)
    repo_base = _with_trailing_slash(repo_base_url)

    repomd_bytes = fetch_bytes(urljoin(metadata_base, REPOMD_PATH))
    locations = parse_repomd(repomd_bytes)
    if "primary" not in locations:
        raise RepoMetadataError(
            "repomd.xml has no 'primary' metadata; cannot enumerate packages"
        )

    primary_href = locations["primary"]
    # Reject an unreadable codec from the href alone, before spending a request
    # fetching the (large) primary file.
    try:
        ensure_supported(primary_href)
    except UnsupportedCompressionError as exc:
        raise RepoMetadataError(str(exc)) from exc
    primary_url = encode_url_path(urljoin(metadata_base, primary_href))
    # An absolute/protocol-relative href could point the (credentialed) primary
    # fetch at an attacker host; refuse anything off the metadata origin.
    if not same_origin(primary_url, metadata_base):
        raise RepoMetadataError(
            f"primary metadata location is off-origin ({primary_href!r}); "
            "refusing to fetch it with credentials"
        )

    if open_stream is not None:
        # Streaming path: decompress + parse straight off the response, so the
        # whole compressed primary is never held in memory at once.
        with open_stream(primary_url) as response:
            stream = _decompress_primary(response, primary_href)
            return [encode_url_path(urljoin(repo_base, href)) for href in iter_package_hrefs(stream)]

    # Buffered path: callers that only provide a bytes-fetcher.
    stream = _decompress_primary(io.BytesIO(fetch_bytes(primary_url)), primary_href)
    return [encode_url_path(urljoin(repo_base, href)) for href in iter_package_hrefs(stream)]
