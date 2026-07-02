import io
import unittest

from . import _pathsetup  # noqa: F401  (sys.path side effect)
from rpm_fetch.repodata import (
    RepoMetadataError,
    discover_package_urls,
    iter_package_hrefs,
    parse_repomd,
    repomd_url,
)

REPOMD = b"""<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo" xmlns:rpm="http://linux.duke.edu/metadata/rpm">
  <revision>1700000000</revision>
  <data type="primary">
    <checksum type="sha256">abc</checksum>
    <location href="repodata/abc-primary.xml.gz"/>
    <size>123</size>
  </data>
  <data type="filelists">
    <location href="repodata/def-filelists.xml.gz"/>
  </data>
</repomd>
"""

PRIMARY = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common"
          xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="2">
  <package type="rpm">
    <name>foo</name><arch>x86_64</arch>
    <location href="Packages/f/foo-1.0-1.el9.x86_64.rpm"/>
  </package>
  <package type="rpm">
    <name>bar</name><arch>noarch</arch>
    <location href="Packages/b/bar-2.0-1.el9.noarch.rpm"/>
  </package>
</metadata>
"""


class RepomdUrlTests(unittest.TestCase):
    def test_appends_repodata_path_with_trailing_slash(self):
        self.assertEqual(
            repomd_url("https://art/repo"),
            "https://art/repo/repodata/repomd.xml",
        )

    def test_handles_existing_trailing_slash(self):
        self.assertEqual(
            repomd_url("https://art/repo/"),
            "https://art/repo/repodata/repomd.xml",
        )


class ParseRepomdTests(unittest.TestCase):
    def test_extracts_typed_locations(self):
        locations = parse_repomd(REPOMD)
        self.assertEqual(locations["primary"], "repodata/abc-primary.xml.gz")
        self.assertEqual(locations["filelists"], "repodata/def-filelists.xml.gz")

    def test_rejects_non_xml(self):
        with self.assertRaises(RepoMetadataError):
            parse_repomd(b"not xml <<<")

    def test_html_response_gets_a_helpful_message(self):
        # e.g. a suspended JFrog trial 302s to an HTML landing page.
        html = b'<!DOCTYPE html><html lang="en"><head><title>JFrog Landing</title></head></html>'
        with self.assertRaises(RepoMetadataError) as ctx:
            parse_repomd(html)
        self.assertIn("HTML page", str(ctx.exception))

    def test_rejects_empty_repomd(self):
        with self.assertRaises(RepoMetadataError):
            parse_repomd(b'<repomd xmlns="http://linux.duke.edu/metadata/repo"/>')


class IterPackageHrefsTests(unittest.TestCase):
    def test_yields_every_package_location(self):
        hrefs = list(iter_package_hrefs(io.BytesIO(PRIMARY)))
        self.assertEqual(
            hrefs,
            ["Packages/f/foo-1.0-1.el9.x86_64.rpm", "Packages/b/bar-2.0-1.el9.noarch.rpm"],
        )

    def test_empty_metadata_yields_nothing(self):
        empty = b'<metadata xmlns="http://linux.duke.edu/metadata/common"/>'
        self.assertEqual(list(iter_package_hrefs(io.BytesIO(empty))), [])


class DiscoverPackageUrlsTests(unittest.TestCase):
    def _fetcher(self, primary_bytes):
        import gzip

        def fetch(url: str) -> bytes:
            if url.endswith("repomd.xml"):
                return REPOMD
            if "primary" in url:
                return gzip.compress(primary_bytes)
            raise AssertionError(f"unexpected fetch: {url}")

        return fetch

    def test_resolves_metadata_then_package_urls(self):
        urls = discover_package_urls(self._fetcher(PRIMARY), "https://art/repo/")
        self.assertEqual(
            urls,
            [
                # Metadata first: repomd.xml itself, then every repomd location.
                "https://art/repo/repodata/repomd.xml",
                "https://art/repo/repodata/abc-primary.xml.gz",
                "https://art/repo/repodata/def-filelists.xml.gz",
                "https://art/repo/Packages/f/foo-1.0-1.el9.x86_64.rpm",
                "https://art/repo/Packages/b/bar-2.0-1.el9.noarch.rpm",
            ],
        )

    def test_package_url_path_is_percent_encoded(self):
        # RPM snapshot versions use '^', illegal in a URL -> must become %5E.
        import gzip

        primary = (
            b'<metadata xmlns="http://linux.duke.edu/metadata/common">'
            b'<package type="rpm"><location href="Packages/c/crontabs-1.11^2019-6.el10.noarch.rpm"/></package>'
            b"</metadata>"
        )

        def fetch(url: str) -> bytes:
            return REPOMD if url.endswith("repomd.xml") else gzip.compress(primary)

        urls = discover_package_urls(fetch, "https://art/repo/")
        self.assertIn("https://art/repo/Packages/c/crontabs-1.11%5E2019-6.el10.noarch.rpm", urls)
        self.assertNotIn("https://art/repo/Packages/c/crontabs-1.11^2019-6.el10.noarch.rpm", urls)

    def test_separate_metadata_base_warms_repo_base(self):
        # Metadata read from mirror/, but package URLs must target repo/.
        calls = []

        def fetch(url: str) -> bytes:
            import gzip

            calls.append(url)
            if url.endswith("repomd.xml"):
                return REPOMD
            return gzip.compress(PRIMARY)

        urls = discover_package_urls(fetch, "https://art/repo/", "https://mirror/upstream/")
        self.assertTrue(all(u.startswith("https://art/repo/") for u in urls))
        self.assertTrue(any(c.startswith("https://mirror/upstream/") for c in calls))
        # The warm target's own metadata is included even though repodata was
        # read from the mirror — that's the point of warming.
        self.assertIn("https://art/repo/repodata/repomd.xml", urls)

    def test_missing_primary_raises(self):
        def fetch(url: str) -> bytes:
            return b'<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="filelists"><location href="x"/></data></repomd>'

        with self.assertRaises(RepoMetadataError):
            discover_package_urls(fetch, "https://art/repo/")

    def test_unsupported_primary_codec_raises_before_fetching_primary(self):
        repomd = (
            b'<repomd xmlns="http://linux.duke.edu/metadata/repo">'
            b'<data type="primary"><location href="repodata/abc-primary.xml.zck"/></data>'
            b"</repomd>"
        )
        fetched: list[str] = []

        def fetch(url: str) -> bytes:
            fetched.append(url)
            return repomd if url.endswith("repomd.xml") else b"\x00\x01\x02"

        with self.assertRaises(RepoMetadataError) as ctx:
            discover_package_urls(fetch, "https://art/repo/")
        self.assertIn("zck", str(ctx.exception))
        # Fix #4: the .zck primary is rejected from the href; only repomd is fetched.
        self.assertEqual(fetched, ["https://art/repo/repodata/repomd.xml"])

    def test_truncated_gzip_primary_is_a_clean_error(self):
        import gzip

        good = gzip.compress(PRIMARY)

        def fetch(url: str) -> bytes:
            return REPOMD if url.endswith("repomd.xml") else good[: len(good) // 2]

        with self.assertRaises(RepoMetadataError) as ctx:
            discover_package_urls(fetch, "https://art/repo/")
        self.assertIn("could not read primary metadata", str(ctx.exception))

    def test_off_origin_primary_href_is_refused_without_fetching(self):
        # An absolute href could redirect the credentialed primary fetch to an
        # attacker host; it must be rejected before any fetch happens.
        repomd = (
            b'<repomd xmlns="http://linux.duke.edu/metadata/repo">'
            b'<data type="primary"><location href="https://evil.example/p.xml.gz"/></data>'
            b"</repomd>"
        )

        def fetch(url: str) -> bytes:
            if url.endswith("repomd.xml"):
                return repomd
            raise AssertionError(f"primary must not be fetched off-origin: {url}")

        with self.assertRaises(RepoMetadataError) as ctx:
            discover_package_urls(fetch, "https://art/repo/")
        self.assertIn("off-origin", str(ctx.exception))

    def test_corrupt_zstd_primary_is_a_clean_error(self):
        # repomd points at a .zst primary whose body is corrupt.
        repomd = (
            b'<repomd xmlns="http://linux.duke.edu/metadata/repo">'
            b'<data type="primary"><location href="repodata/abc-primary.xml.zst"/></data>'
            b"</repomd>"
        )

        def fetch(url: str) -> bytes:
            return repomd if url.endswith("repomd.xml") else b"\x28\xb5\x2f\xfd garbage"

        with self.assertRaises(RepoMetadataError):
            discover_package_urls(fetch, "https://art/repo/")

    def test_streaming_client_path_is_used_when_available(self):
        import contextlib
        import gzip

        streamed: list[str] = []

        class FakeClient:
            def get_bytes(self, url: str) -> bytes:
                # primary must be streamed, not buffered through get_bytes.
                assert url.endswith("repomd.xml"), url
                return REPOMD

            @contextlib.contextmanager
            def open_stream(self, url: str):
                streamed.append(url)
                yield io.BytesIO(gzip.compress(PRIMARY))

        urls = discover_package_urls(FakeClient(), "https://art/repo/")
        self.assertEqual(
            urls[-2:],
            [
                "https://art/repo/Packages/f/foo-1.0-1.el9.x86_64.rpm",
                "https://art/repo/Packages/b/bar-2.0-1.el9.noarch.rpm",
            ],
        )
        self.assertIn("https://art/repo/repodata/repomd.xml", urls)
        self.assertEqual(len(streamed), 1)
        self.assertTrue(streamed[0].endswith("-primary.xml.gz"))


class _NonSeekableStream:
    """A readable, explicitly non-seekable wrapper, like an HTTP response."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def seek(self, *args):
        raise OSError("not seekable")

    def tell(self):
        raise OSError("not seekable")

    def close(self):
        self._buf.close()


class StreamingDecompressionTests(unittest.TestCase):
    """Each codec must parse from a non-seekable stream (the #5B guarantee)."""

    BIG = (
        b'<metadata xmlns="http://linux.duke.edu/metadata/common">'
        + b"".join(
            b'<package type="rpm"><location href="Packages/p/pkg-%d.rpm"/></package>' % i
            for i in range(300)
        )
        + b"</metadata>"
    )

    def _check(self, href: str, blob: bytes):
        from rpm_fetch.decompress import open_decompressed

        stream = open_decompressed(_NonSeekableStream(blob), href)
        self.assertEqual(len(list(iter_package_hrefs(stream))), 300, msg=href)

    def test_gzip_streams(self):
        import gzip

        self._check("x-primary.xml.gz", gzip.compress(self.BIG))

    def test_zstd_streams(self):
        from compression import zstd

        self._check("x-primary.xml.zst", zstd.compress(self.BIG))

    def test_xz_streams(self):
        import lzma

        self._check("x-primary.xml.xz", lzma.compress(self.BIG))

    def test_bz2_streams(self):
        import bz2

        self._check("x-primary.xml.bz2", bz2.compress(self.BIG))


if __name__ == "__main__":
    unittest.main()
