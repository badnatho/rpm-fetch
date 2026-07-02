"""End-to-end test: a real local HTTP server standing in for Artifactory.

Serves a tiny but real repo (repomd.xml + gzip primary.xml + package paths),
runs the CLI against it, and asserts that every package received a HEAD request.
Exercises urllib, the thread pool, XML streaming, and decompression together.
"""

import contextlib
import gzip
import io
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import _pathsetup  # noqa: F401  (sys.path side effect)
from rpm_fetch.cli import main

REPOMD = b"""<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="primary">
    <location href="repodata/primary.xml.gz"/>
  </data>
</repomd>
"""

PRIMARY = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common" packages="3">
  <package type="rpm"><location href="Packages/a/alpha-1.0-1.x86_64.rpm"/></package>
  <package type="rpm"><location href="Packages/b/beta-2.0-1.noarch.rpm"/></package>
  <package type="rpm"><location href="Packages/g/gamma-3.0-1.x86_64.rpm"/></package>
</metadata>
"""

# Warming covers the repo's metadata files as well as every package.
EXPECTED_WARMED = {
    "/repo/repodata/repomd.xml",
    "/repo/repodata/primary.xml.gz",
    "/repo/Packages/a/alpha-1.0-1.x86_64.rpm",
    "/repo/Packages/b/beta-2.0-1.noarch.rpm",
    "/repo/Packages/g/gamma-3.0-1.x86_64.rpm",
}


def _make_handler(recorder, lock):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence the default stderr logging
            pass

        def _body_for(self):
            if self.path == "/repo/repodata/repomd.xml":
                return REPOMD
            if self.path == "/repo/repodata/primary.xml.gz":
                return gzip.compress(PRIMARY)
            if self.path.startswith("/repo/Packages/"):
                return b""  # package exists; HEAD just needs the status
            return None

        def do_GET(self):
            body = self._body_for()
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            body = self._body_for()
            if body is None:
                self.send_error(404)
                return
            with lock:
                recorder.add((self.command, self.path))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()

    return Handler


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.recorder: set[tuple[str, str]] = set()
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.recorder, self.lock))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}/repo/"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([self.base_url, *args])
        return code, out.getvalue(), err.getvalue()

    def test_head_warms_metadata_and_every_package(self):
        code, _out, err = self._run("--quiet")
        self.assertEqual(code, 0, msg=err)
        head_paths = {path for method, path in self.recorder if method == "HEAD"}
        self.assertEqual(head_paths, EXPECTED_WARMED)

    def test_dry_run_lists_urls_without_requesting(self):
        code, out, _err = self._run("--dry-run")
        self.assertEqual(code, 0)
        listed = {line.strip() for line in out.splitlines() if line.strip()}
        self.assertEqual(listed, {self.base_url + p for p in (
            "repodata/repomd.xml",
            "repodata/primary.xml.gz",
            "Packages/a/alpha-1.0-1.x86_64.rpm",
            "Packages/b/beta-2.0-1.noarch.rpm",
            "Packages/g/gamma-3.0-1.x86_64.rpm",
        )})
        # Nothing should have been warmed.
        self.assertEqual({m for m, _p in self.recorder if m == "HEAD"}, set())

    def test_limit_caps_the_number_warmed(self):
        code, _out, err = self._run("--quiet", "--limit", "1")
        self.assertEqual(code, 0, msg=err)
        head_paths = {path for method, path in self.recorder if method == "HEAD"}
        self.assertEqual(len(head_paths), 1)

    def test_missing_repo_returns_metadata_error(self):
        out, err = io.StringIO(), io.StringIO()
        bad = self.base_url + "does-not-exist/"
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([bad, "--quiet", "--retries", "0"])
        self.assertEqual(code, 2)
        self.assertIn("error", err.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
