import bz2
import gzip
import io
import lzma
import unittest
from compression import zstd

from . import _pathsetup  # noqa: F401  (sys.path side effect)
from rpm_fetch.decompress import UnsupportedCompressionError, ensure_supported, open_decompressed

PAYLOAD = b"<metadata><package><location href='Packages/f/foo.rpm'/></package></metadata>"


class OpenDecompressedTests(unittest.TestCase):
    def _check(self, name: str, blob: bytes) -> None:
        stream = open_decompressed(io.BytesIO(blob), name)
        self.assertEqual(stream.read(), PAYLOAD, msg=name)

    def test_gzip(self):
        self._check("primary.xml.gz", gzip.compress(PAYLOAD))

    def test_xz(self):
        self._check("primary.xml.xz", lzma.compress(PAYLOAD))

    def test_bz2(self):
        self._check("primary.xml.bz2", bz2.compress(PAYLOAD))

    def test_zstd(self):
        self._check("primary.xml.zst", zstd.compress(PAYLOAD))

    def test_plain_xml_passthrough(self):
        self._check("primary.xml", PAYLOAD)

    def test_no_extension_passthrough(self):
        # A name without an extension is treated as already-plain XML.
        self._check("primary", PAYLOAD)

    def test_unsupported_codec_raises(self):
        # An unreadable codec (e.g. zchunk) is reported clearly, not parsed as XML.
        with self.assertRaises(UnsupportedCompressionError) as ctx:
            open_decompressed(io.BytesIO(b"\x00"), "repodata/abc-primary.xml.zck")
        self.assertIn("zck", str(ctx.exception))

    def test_dot_in_directory_not_mistaken_for_extension(self):
        # The extension comes from the basename, so a dotted parent dir is fine.
        self._check("a.b/primary", PAYLOAD)


class EnsureSupportedTests(unittest.TestCase):
    def test_accepts_known_codecs_and_plain(self):
        for name in ("p-primary.xml.gz", "p-primary.xml.zst", "p-primary.xml", "primary"):
            ensure_supported(name)  # must not raise

    def test_rejects_unknown_codec_without_data(self):
        with self.assertRaises(UnsupportedCompressionError):
            ensure_supported("repodata/abc-primary.xml.zck")


if __name__ == "__main__":
    unittest.main()
