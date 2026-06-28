"""Drive concurrent warming requests over a list of package URLs.

The work is pure network I/O, so a thread pool (not processes) is the right
tool: the GIL is released around socket reads and one client handles dozens of
in-flight requests cheaply. Results are consumed on the calling thread as they
complete, which keeps the progress callback single-threaded and lock-free.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .http import HttpClient, WarmResult

__all__ = ["WarmSummary", "warm_all"]

ProgressCallback = Callable[[WarmResult, int, int], None]


@dataclass
class WarmSummary:
    """Aggregate outcome of a warming run."""

    total: int
    results: list[WarmResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[WarmResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[WarmResult]:
        return [r for r in self.results if not r.ok]

    @property
    def all_ok(self) -> bool:
        return len(self.succeeded) == self.total


def warm_all(
    client: HttpClient,
    urls: Sequence[str],
    *,
    method: str = "HEAD",
    concurrency: int = 16,
    progress: ProgressCallback | None = None,
    fail_fast: bool = False,
) -> WarmSummary:
    """Warm every URL in *urls* with up to *concurrency* requests in flight.

    *progress* is invoked once per completed request as
    ``progress(result, completed_count, total)`` on the calling thread.

    With *fail_fast*, the first failure stops new requests from being scheduled;
    in-flight ones still finish and are reported.
    """
    summary = WarmSummary(total=len(urls))
    if not urls:
        return summary

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(client.warm_one, url, method): url for url in urls}
        try:
            for future in as_completed(futures):
                result = future.result()
                summary.results.append(result)
                if progress is not None:
                    progress(result, len(summary.results), summary.total)
                if fail_fast and not result.ok:
                    for pending in futures:
                        pending.cancel()
                    break
        except KeyboardInterrupt:
            # Drop not-yet-started requests so shutdown only waits on in-flight
            # ones; let the interrupt propagate to the CLI for a clean exit 130.
            for pending in futures:
                pending.cancel()
            raise
    return summary
