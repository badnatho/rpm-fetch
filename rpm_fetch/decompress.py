"""Transparent decompression for RPM repodata streams.

repomd.xml points at metadata files compressed with whatever the repo creator
chose. ``createrepo_c`` defaults have shifted over the years: gzip historically,
zstd on modern Fedora/RHEL, xz/bz2 in between. We dispatch on the filename
extension because that is what repomd records and it never lies about the codec.

Every codec used here lives in the standard library. ``compression.zstd`` is
new in Python 3.14 (PEP 784); on older interpreters zstd repodata is the one
format we cannot read without a third-party wheel, which is why this tool
requires 3.14+.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import zlib
from collections.abc import Callable
from typing import BinaryIO

# Imported eagerly so an interpreter without it fails loudly at import time
# rather than only when a zstd repo is encountered.
from compression import zstd

__all__ = [
    "open_decompressed",
    "ensure_supported",
    "UnsupportedCompressionError",
    "DECOMPRESSION_ERRORS",
    "SUPPORTED_EXTENSIONS",
]


class UnsupportedCompressionError(Exception):
    """Raised when a metadata file uses a codec rpm-fetch cannot decode.

    Distinguishes "this is a compression format we don't support" (e.g. zchunk
    ``.zck``) from "this is plain XML", so the failure is reported as an
    unsupported codec rather than a confusing downstream XML parse error.
    """


# Extension (lower-case, no dot) -> factory that wraps a binary file object in a
# streaming decompressor.
_DECOMPRESSORS: dict[str, Callable[[BinaryIO], BinaryIO]] = {
    "gz": lambda f: gzip.GzipFile(fileobj=f),
    "xz": lambda f: lzma.LZMAFile(f),
    "bz2": lambda f: bz2.BZ2File(f),
    "zst": lambda f: zstd.ZstdFile(f),
    "zstd": lambda f: zstd.ZstdFile(f),
}

# Suffixes that mean "already plain XML, no decompression": a literal ``.xml``
# and the no-extension case (empty string).
_PLAIN_SUFFIXES = frozenset({"xml", ""})

SUPPORTED_EXTENSIONS = frozenset(_DECOMPRESSORS)

# Exceptions a streaming decoder raises on corrupt or truncated input. (bz2 and
# a bad gzip header raise OSError, handled separately by callers.) repodata
# parsing catches these to turn a damaged download into a clean error instead of
# a traceback.
DECOMPRESSION_ERRORS = (EOFError, zlib.error, lzma.LZMAError, zstd.ZstdError)


def _extension(name: str) -> str:
    """Lower-case final extension of *name*'s basename ("" if none).

    Uses the basename so a dot in a parent directory can't be mistaken for an
    extension (e.g. ``a.b/primary`` -> "" not "b/primary").
    """
    basename = name.rsplit("/", 1)[-1]
    return basename.rsplit(".", 1)[-1].lower() if "." in basename else ""


def _unsupported(ext: str, name: str) -> UnsupportedCompressionError:
    readable = ", ".join("." + e for e in sorted(SUPPORTED_EXTENSIONS))
    return UnsupportedCompressionError(
        f"unsupported metadata compression '.{ext}' in {name!r}; "
        f"rpm-fetch reads {readable} or plain .xml"
    )


def ensure_supported(name: str) -> None:
    """Raise :class:`UnsupportedCompressionError` if *name*'s codec is unreadable.

    Lets a caller reject an unsupported codec from the href alone — before
    fetching the (potentially large) file it points at.
    """
    ext = _extension(name)
    if ext not in _DECOMPRESSORS and ext not in _PLAIN_SUFFIXES:
        raise _unsupported(ext, name)


def open_decompressed(fileobj: BinaryIO, name: str) -> BinaryIO:
    """Wrap *fileobj* in a streaming decoder chosen from *name*'s extension.

    *name* is the metadata href (e.g. ``repodata/abc-primary.xml.zst``). A known
    codec is wrapped; a plain ``.xml`` (or extensionless) name is returned as-is;
    anything else raises :class:`UnsupportedCompressionError` so an unreadable
    codec (e.g. zchunk ``.zck``) is reported clearly instead of failing later as
    "not valid XML".
    """
    ext = _extension(name)
    decompressor = _DECOMPRESSORS.get(ext)
    if decompressor is not None:
        return decompressor(fileobj)
    if ext in _PLAIN_SUFFIXES:
        return fileobj
    raise _unsupported(ext, name)
