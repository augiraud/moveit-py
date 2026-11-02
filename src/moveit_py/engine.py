"""Run file transfers in parallel and report their progress."""

import sys
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from typing import IO

from moveit_py.errors import SignInExpiredError

POLL_INTERVAL = 0.5  # seconds; a bounded wait keeps Ctrl+C deliverable to the main thread
INTERRUPTED_MESSAGE = "\nInterrupted, waiting for the transfers in progress to finish..."


class Outcome(Enum):
    """What happened to one item."""

    TRANSFERRED = "transferred"
    SKIPPED = "skipped"
    ERROR = "error"


type Result = Outcome | None  # None means "not run": the stop flag was set before the item started
type Wait = Callable[[set[Future[Result]], float, str], tuple[set[Future[Result]], set[Future[Result]]]]


@dataclass
class _Batch[T]:
    """The state of one `transfer_multithread` call."""

    stop: threading.Event
    futures: dict[Future[Result], tuple[int, T]] = field(default_factory=dict)  # future -> (submission rank, item)
    pending: set[Future[Result]] = field(default_factory=set)
    expired: SignInExpiredError | None = None

    def cancel_pending(self) -> None:
        """Cancel transfers that have not started yet."""
        self.stop.set()
        for future in self.pending:
            future.cancel()


class TransferSession:
    """Count transfers, show progress and print the summary for one command.

    The caller sets total to the number of files considered, including files skipped before the transfers start.
    """

    def __init__(
        self,
        threads: int,
        quiet: bool,
        out: IO[str] | None = None,
        err: IO[str] | None = None,
        wait_for: Wait = wait,
    ) -> None:
        self.threads = threads
        self.quiet = quiet
        self.total = 0
        self._out = out if out is not None else sys.stdout
        self._err = err if err is not None else sys.stderr
        self._wait_for = wait_for
        self._lock = threading.Lock()
        self._counts = dict.fromkeys(Outcome, 0)
        self._progress_pending = False  # a progress line (ending with \r) is the last thing written on `out`

    @property
    def transferred(self) -> int:
        """Number of items transferred so far."""
        return self._count_of(Outcome.TRANSFERRED)

    @property
    def skipped(self) -> int:
        """Number of items skipped so far."""
        return self._count_of(Outcome.SKIPPED)

    @property
    def errors(self) -> int:
        """Number of items that failed so far."""
        return self._count_of(Outcome.ERROR)

    def count(self, outcome: Outcome, *, show_progress: bool = True) -> None:
        """Count an outcome and show progress unless quiet or show_progress is false."""
        with self._lock:
            self._counts[outcome] += 1
            if show_progress and not self.quiet:
                self._show_progress()

    def message(self, text: str) -> None:
        """Print a line without overwriting a pending progress line."""
        with self._lock:
            self._end_progress_line()
            print(text, file=self._out, flush=True)

    def summary(self, action: str) -> None:
        """Print the summary, even in quiet mode."""
        with self._lock:
            self._end_progress_line()
            counts = self._counts
            not_transferred = self.total - sum(counts.values())
            line = (
                f"{action} {counts[Outcome.TRANSFERRED]} files, skipped {counts[Outcome.SKIPPED]} files, "
                f"{counts[Outcome.ERROR]} errors"
            )
            suffix = f", {not_transferred} files not transferred." if not_transferred > 0 else "."
            print(line + suffix, file=self._out, flush=True)

    def transfer_multithread[T](
        self, items: Sequence[T], function: Callable[[T], Outcome], describe: Callable[[T], str] = str
    ) -> None:
        """Run the transfers in parallel and count the outcomes.

        One failure does not stop the other transfers. If the sign-in expires or the user interrupts the command, cancel transfers that have not started, wait for the others and raise the error.
        """
        if not items:
            return
        batch = _Batch[T](threading.Event())
        with ThreadPoolExecutor(max(1, self.threads)) as pool:
            try:
                self._submit_all(pool, batch, items, function)
                self._collect(batch, describe)
            except BaseException as error:
                self._abort(batch, describe, announce=isinstance(error, KeyboardInterrupt))
                raise
        self._finish_progress()
        if batch.expired is not None:
            raise batch.expired

    def _submit_all[T](
        self, pool: ThreadPoolExecutor, batch: _Batch[T], items: Sequence[T], function: Callable[[T], Outcome]
    ) -> None:
        for rank, item in enumerate(items):
            future = pool.submit(self._run_item, function, item, batch.stop)
            batch.futures[future] = (rank, item)
            batch.pending.add(future)

    @staticmethod
    def _run_item[T](function: Callable[[T], Outcome], item: T, stop: threading.Event) -> Result:
        if stop.is_set():
            return None
        try:
            return function(item)
        except SignInExpiredError:
            stop.set()
            raise

    def _collect[T](self, batch: _Batch[T], describe: Callable[[T], str]) -> None:
        while batch.pending:
            done, _ = self._wait_for(batch.pending, POLL_INTERVAL, FIRST_COMPLETED)
            for future in sorted(done, key=lambda f: batch.futures[f][0]):  # submission order: stable output
                batch.pending.discard(future)  # first: an interrupt in between must not count the item twice
                self._handle(batch, future, describe)

    def _abort[T](self, batch: _Batch[T], describe: Callable[[T], str], *, announce: bool) -> None:
        """Cancel pending transfers and wait for running ones, even if interrupted again."""
        batch.cancel_pending()
        if announce:
            print(INTERRUPTED_MESSAGE, file=self._err, flush=True)
        while True:
            try:
                wait(batch.pending)  # the real wait: the injected one may be the thing that failed
                break
            except KeyboardInterrupt:
                continue
        for future in sorted(batch.pending, key=lambda f: batch.futures[f][0]):
            self._handle(batch, future, describe)
        batch.pending.clear()

    def _handle[T](self, batch: _Batch[T], future: Future[Result], describe: Callable[[T], str]) -> None:
        if future.cancelled():
            return
        error = future.exception()
        if error is None:
            outcome = future.result()
            if outcome is not None:
                self.count(outcome)
        elif isinstance(error, SignInExpiredError):
            batch.expired = batch.expired or error
            batch.cancel_pending()
        else:
            item = batch.futures[future][1]
            self.message(f"Error: could not transfer {describe(item)}: {error!r}")
            self.count(Outcome.ERROR)

    def _count_of(self, outcome: Outcome) -> int:
        with self._lock:
            return self._counts[outcome]

    def _show_progress(self) -> None:
        """Called with the lock held."""
        counts = self._counts
        done = sum(counts.values())
        self._out.write(
            f"Progress: {done} / {self.total} ({counts[Outcome.TRANSFERRED]} transferred, "
            f"{counts[Outcome.SKIPPED]} skipped, {counts[Outcome.ERROR]} errors)\r"
        )
        self._out.flush()
        self._progress_pending = True

    def _end_progress_line(self) -> None:
        """Called with the lock held."""
        if self._progress_pending:
            self._out.write("\n")
            self._progress_pending = False

    def _finish_progress(self) -> None:
        """End the progress line if one was shown."""
        if not self.quiet:
            with self._lock:
                self._out.write("\n")
                self._out.flush()
                self._progress_pending = False
