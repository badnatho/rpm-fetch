"""CLI-level behaviour: argument validation and interrupt handling."""

import contextlib
import io
import unittest

from . import _pathsetup  # noqa: F401  (sys.path side effect)
import rpm_fetch.cli as cli
from rpm_fetch.http import WarmResult
from rpm_fetch.warmer import WarmSummary


class ArgumentValidationTests(unittest.TestCase):
    """Bad numeric arguments should be rejected cleanly (argparse exits 2)."""

    def _rejects(self, *args):
        parser = cli.build_parser()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["https://art/repo/", *args])
        self.assertEqual(ctx.exception.code, 2)

    def test_negative_retries_rejected(self):
        self._rejects("--retries", "-1")

    def test_zero_concurrency_rejected(self):
        self._rejects("--concurrency", "0")

    def test_negative_concurrency_rejected(self):
        self._rejects("--concurrency", "-4")

    def test_nonpositive_timeout_rejected(self):
        self._rejects("--timeout", "0")

    def test_negative_limit_rejected(self):
        self._rejects("--limit", "-5")

    def test_valid_values_accepted(self):
        args = cli.build_parser().parse_args(
            ["https://art/repo/", "--retries", "0", "--concurrency", "1", "--timeout", "0.5", "--limit", "0"]
        )
        self.assertEqual((args.retries, args.concurrency, args.timeout, args.limit), (0, 1, 0.5, 0))


class KeyboardInterruptTests(unittest.TestCase):
    def test_interrupt_during_discovery_returns_130(self):
        original = cli.discover_package_urls

        def boom(*args, **kwargs):
            raise KeyboardInterrupt

        cli.discover_package_urls = boom
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main(["https://art/repo/", "--quiet"])
        finally:
            cli.discover_package_urls = original

        self.assertEqual(code, 130)
        self.assertIn("Interrupted", err.getvalue())


class MainErrorHandlingTests(unittest.TestCase):
    def test_http_exception_during_discovery_returns_2(self):
        import http.client

        original = cli.discover_package_urls

        def boom(*args, **kwargs):
            raise http.client.BadStatusLine("garbage")

        cli.discover_package_urls = boom
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main(["https://art/repo/", "--quiet"])
        finally:
            cli.discover_package_urls = original

        self.assertEqual(code, 2)
        self.assertIn("could not fetch repository metadata", err.getvalue())

    def test_broken_pipe_returns_120(self):
        original = cli._run

        def boom(_args):
            raise BrokenPipeError

        cli._run = boom
        try:
            # Redirect to buffers without a real fileno() so the handler's dup2
            # is a no-op here (and the runner's stdout isn't sent to devnull).
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["https://art/repo/"])
        finally:
            cli._run = original

        self.assertEqual(code, 120)

    def test_broken_pipe_on_final_flush_returns_120(self):
        # The real `| head` case: a piped, block-buffered stdout whose flush
        # fails only after the run finishes.
        class _BrokenStdout(io.StringIO):
            def flush(self):
                raise BrokenPipeError

        original = cli._run
        cli._run = lambda _args: 0
        try:
            with contextlib.redirect_stdout(_BrokenStdout()), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["https://art/repo/"])
        finally:
            cli._run = original

        self.assertEqual(code, 120)


class OriginRestrictionTests(unittest.TestCase):
    def test_restrict_to_origin_drops_foreign_urls(self):
        urls = ["https://art/repo/a.rpm", "https://evil.example/x", "https://art/repo/b.rpm"]
        kept, dropped = cli._restrict_to_origin(urls, "https://art/repo/")
        self.assertEqual(kept, ["https://art/repo/a.rpm", "https://art/repo/b.rpm"])
        self.assertEqual(dropped, 1)

    def test_off_origin_urls_are_skipped_and_warned_end_to_end(self):
        original = cli.discover_package_urls

        def fake(*args, **kwargs):
            return ["https://art/repo/ok.rpm", "https://evil.example/steal"]

        cli.discover_package_urls = fake
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["https://art/repo/", "--dry-run"])
        finally:
            cli.discover_package_urls = original

        self.assertEqual(code, 0)
        self.assertIn("https://art/repo/ok.rpm", out.getvalue())
        self.assertNotIn("evil.example", out.getvalue())  # never even listed
        self.assertIn("skipped 1", err.getvalue())


class FailureSummaryTests(unittest.TestCase):
    def _summary_with_failures(self, n: int) -> WarmSummary:
        summary = WarmSummary(total=n)
        summary.results = [
            WarmResult(f"https://art/pkg-{i}.rpm", 500, False, "HTTP 500", 1, 0.0) for i in range(n)
        ]
        return summary

    def _render(self, summary: WarmSummary) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli._print_summary(summary, elapsed=1.0, quiet=False)
        return err.getvalue()

    def test_long_failure_list_is_capped(self):
        out = self._render(self._summary_with_failures(50))
        printed = out.count("https://art/pkg-")
        self.assertEqual(printed, cli._MAX_FAILURE_LINES)
        self.assertIn(f"and {50 - cli._MAX_FAILURE_LINES} more", out)
        out.encode("ascii")  # Fix #3: output must be ASCII-safe (no '…')

    def test_short_failure_list_is_not_capped(self):
        out = self._render(self._summary_with_failures(3))
        self.assertEqual(out.count("https://art/pkg-"), 3)
        self.assertNotIn("more", out)


if __name__ == "__main__":
    unittest.main()
