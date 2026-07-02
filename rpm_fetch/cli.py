"""Command-line interface: argument parsing, orchestration, and reporting.

Flow: parse args -> resolve auth -> discover package URLs from repodata -> warm
them concurrently -> print a summary. Secrets default to environment variables
so tokens never have to appear in shell history or process listings.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import os
import sys
import time
from collections.abc import Sequence

from . import __version__
from .http import Auth, HttpClient, WarmResult, same_origin
from .repodata import RepoMetadataError, discover_package_urls
from .warmer import WarmSummary, warm_all

# Environment fallbacks for secrets, checked when the matching flag is absent.
ENV_TOKEN = ("ARTIFACTORY_TOKEN", "RPM_FETCH_TOKEN")
ENV_PASSWORD = ("ARTIFACTORY_PASSWORD", "RPM_FETCH_PASSWORD")
ENV_API_KEY = ("ARTIFACTORY_API_KEY", "RPM_FETCH_API_KEY")

# Cap the per-failure lines printed in the summary so a wholly-broken repo can't
# bury the terminal in thousands of URLs. --verbose still prints each one live.
_MAX_FAILURE_LINES = 20


def _env_first(names: Sequence[str]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _bounded_int(minimum: int):
    """argparse `type` for an int that must be >= *minimum* (clear error, no traceback)."""

    def parse(value: str) -> int:
        number = int(value)
        if number < minimum:
            raise argparse.ArgumentTypeError(f"must be >= {minimum}, got {number}")
        return number

    return parse


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rpm-fetch",
        description=(
            "Warm an Artifactory cache from an RPM repository's metadata: read "
            "repodata, enumerate every package, and HEAD (or GET) each one."
        ),
    )
    parser.add_argument(
        "repo_url",
        help="Base URL of the Artifactory RPM repository (the directory containing repodata/).",
    )
    parser.add_argument(
        "--metadata-base-url",
        metavar="URL",
        help="Read repodata from this base instead of REPO_URL (package URLs still target REPO_URL).",
    )

    auth = parser.add_argument_group("authentication (prefer the env vars for secrets)")
    auth.add_argument("--token", help=f"Bearer token. Env: {', '.join(ENV_TOKEN)}.")
    auth.add_argument("--user", help="Username for HTTP Basic auth.")
    auth.add_argument("--password", help=f"Password/API key for Basic auth. Env: {', '.join(ENV_PASSWORD)}.")
    auth.add_argument("--api-key", help=f"X-JFrog-Art-Api key. Env: {', '.join(ENV_API_KEY)}.")

    req = parser.add_argument_group("request behaviour")
    req.add_argument("--method", choices=("HEAD", "GET"), default="HEAD",
                     help="HTTP method per package (default: HEAD). GET also downloads the body.")
    req.add_argument("--concurrency", type=_bounded_int(1), default=16, metavar="N",
                     help="Maximum concurrent requests (default: 16).")
    req.add_argument("--timeout", type=_positive_float, default=30.0, metavar="SECS",
                     help="Per-request timeout in seconds (default: 30).")
    req.add_argument("--retries", type=_bounded_int(0), default=2, metavar="N",
                     help="Retries on transient failures (429/5xx/network) (default: 2).")
    req.add_argument("--insecure", action="store_true", help="Skip TLS certificate verification.")
    req.add_argument("--fail-fast", action="store_true", help="Stop scheduling new requests after the first failure.")

    out = parser.add_argument_group("scope and output")
    out.add_argument("--limit", type=_bounded_int(0), metavar="N",
                     help="Only process the first N URLs (metadata files come first; for testing).")
    out.add_argument("--dry-run", action="store_true", help="Print the package URLs that would be warmed, then exit.")
    out.add_argument("--verbose", action="store_true", help="Print a line for every request.")
    out.add_argument("--quiet", action="store_true", help="Only print the final summary and errors.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def resolve_auth(args: argparse.Namespace) -> Auth:
    """Merge CLI flags with environment fallbacks into an :class:`Auth`."""
    return Auth(
        token=args.token or _env_first(ENV_TOKEN),
        username=args.user,
        password=args.password or _env_first(ENV_PASSWORD),
        api_key=args.api_key or _env_first(ENV_API_KEY),
    )


def _make_progress(quiet: bool, verbose: bool):
    """Build a progress callback; returns None when nothing should be printed."""
    if quiet:
        return None
    tty = sys.stderr.isatty()

    def progress(result: WarmResult, done: int, total: int) -> None:
        if verbose or not result.ok:
            mark = "ok " if result.ok else "ERR"
            line = f"[{done}/{total}] {mark} {result.detail} {result.url}"
            end = "\n"
        elif tty:
            line = f"[{done}/{total}] warming..."
            end = "\r"
        else:
            return  # non-verbose, non-tty, successful: stay silent until summary
        print(line, end=end, file=sys.stderr, flush=True)

    return progress


def _print_summary(summary: WarmSummary, elapsed: float, quiet: bool) -> None:
    if not quiet and sys.stderr.isatty():
        print(" " * 40, end="\r", file=sys.stderr)  # wipe the transient progress line
    failed = summary.failed
    if failed:
        print(f"\n{len(failed)} request(s) failed:", file=sys.stderr)
        for result in failed[:_MAX_FAILURE_LINES]:
            print(f"  {result.detail:>10}  {result.url}", file=sys.stderr)
        hidden = len(failed) - _MAX_FAILURE_LINES
        if hidden > 0:
            print(f"  ... and {hidden} more (use --verbose to see each).", file=sys.stderr)
    print(
        f"Warmed {len(summary.succeeded)}/{summary.total} packages "
        f"({len(failed)} failed) in {elapsed:.1f}s.",
        file=sys.stderr,
    )


def _restrict_to_origin(urls: Sequence[str], repo_url: str) -> tuple[list[str], int]:
    """Keep only URLs sharing *repo_url*'s origin; return (kept, dropped_count)."""
    kept = [url for url in urls if same_origin(url, repo_url)]
    return kept, len(urls) - len(kept)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code = _run(args)
        # Flush now, inside the guard: with a piped (block-buffered) stdout the
        # failing write often lands at interpreter shutdown otherwise — too late
        # to catch, producing the "Exception ignored … BrokenPipeError" notice.
        sys.stdout.flush()
        return code
    except KeyboardInterrupt:
        # 130 = terminated by Ctrl-C, by shell convention. Friendly line, no traceback.
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # A downstream consumer (e.g. `| head`) closed the pipe. Point the std
        # streams at devnull so the interpreter's shutdown flush doesn't raise
        # again (the "Exception ignored ... BrokenPipeError" noise), then exit.
        with contextlib.suppress(OSError, ValueError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.dup2(devnull, sys.stderr.fileno())
        return 120


def _run(args: argparse.Namespace) -> int:
    auth = resolve_auth(args)
    client = HttpClient(
        auth,
        timeout=args.timeout,
        retries=args.retries,
        insecure=args.insecure,
    )

    if not args.quiet:
        print(f"Discovering packages from {args.repo_url} ...", file=sys.stderr)
    try:
        urls = discover_package_urls(client, args.repo_url, args.metadata_base_url)
    except RepoMetadataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, http.client.HTTPException) as exc:
        # OSError = network/TLS; HTTPException = malformed response (BadStatusLine, ...).
        print(f"error: could not fetch repository metadata: {exc}", file=sys.stderr)
        return 2

    # Never send the bearer token off the repo's own origin: absolute/protocol-
    # relative hrefs in (untrusted) metadata could otherwise exfiltrate it.
    urls, off_origin = _restrict_to_origin(urls, args.repo_url)
    if off_origin:
        print(
            f"warning: skipped {off_origin} package URL(s) pointing outside {args.repo_url} "
            "(not warmed, to avoid leaking credentials).",
            file=sys.stderr,
        )

    if args.limit is not None:
        urls = urls[: args.limit]

    if args.dry_run:
        for url in urls:
            print(url)
        if not args.quiet:
            print(f"{len(urls)} package(s) would be warmed.", file=sys.stderr)
        return 0

    if not urls:
        print("No packages found in repository metadata; nothing to warm.", file=sys.stderr)
        return 0

    if not args.quiet:
        print(f"Warming {len(urls)} package(s) with {args.method} (concurrency {args.concurrency}) ...",
              file=sys.stderr)

    start = time.monotonic()
    summary = warm_all(
        client,
        urls,
        method=args.method,
        concurrency=args.concurrency,
        progress=_make_progress(args.quiet, args.verbose),
        fail_fast=args.fail_fast,
    )
    _print_summary(summary, time.monotonic() - start, args.quiet)
    return 0 if summary.all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
