import concurrent.futures
import io
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_provider import NOW, URL, VALID, Fake

from moveit_py.auth.provider import TokenProvider
from moveit_py.auth.store import TokenStore
from moveit_py.engine import Outcome, TransferSession
from moveit_py.errors import AuthenticationError, SignInExpiredError

TIMEOUT = 10.0


def make_session(
    threads: int = 4, quiet: bool = False, total: int = 0
) -> tuple[TransferSession, io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    session = TransferSession(threads, quiet, out, err)
    session.total = total
    return session, out, err


def transferred(_: object) -> Outcome:
    return Outcome.TRANSFERRED


def progress(done: int, total: int, transferred: int, skipped: int, errors: int) -> str:
    return f"Progress: {done} / {total} ({transferred} transferred, {skipped} skipped, {errors} errors)\r"


def test_at41_an_unexpected_exception_fails_only_its_item() -> None:
    session, out, _ = make_session()

    def function(item: str) -> Outcome:
        if item == "b":
            raise ValueError("boom")
        return Outcome.TRANSFERRED

    session.total = 3
    session.transfer_multithread(["a", "b", "c"], function)
    assert (session.transferred, session.skipped, session.errors) == (2, 0, 1)
    assert "Error: could not transfer b: ValueError('boom')\n" in out.getvalue()


def test_the_error_line_uses_describe() -> None:
    session, out, _ = make_session(threads=1)

    def function(item: int) -> Outcome:
        raise OSError(item)

    session.transfer_multithread([7], function, describe=lambda item: f"file number {item}")
    assert "Error: could not transfer file number 7: OSError(7)\n" in out.getvalue()


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def test_at45_sign_in_expiry_under_load_makes_one_refresh_attempt(tmp_path: Path) -> None:
    clock = FakeClock()
    refresh, login = Fake(AuthenticationError("invalid_grant")), Fake()
    store = TokenStore(tmp_path / "tokens.json")
    store.save(URL, VALID)
    provider = TokenProvider(refresh=refresh, login=login, store=store, url=URL, clock=clock, keep_alive_interval=3600)
    provider.disable_login()
    start_lock = threading.Lock()
    started = 0

    def function(_: int) -> Outcome:
        nonlocal started
        with start_lock:  # items 1..9 get their token before the 10th start expires it: deterministic
            started += 1
            if started == 10:
                clock.now = VALID.expires_at + 1000
            provider.get_access_token()
        return Outcome.TRANSFERRED

    session, _, _ = make_session(threads=4, total=1000)
    with pytest.raises(SignInExpiredError):
        session.transfer_multithread(list(range(1000)), function)
    assert len(refresh.calls) == 1
    assert login.calls == []
    assert 9 <= session.transferred < 20
    assert started < 50
    assert session.errors == 0


def test_at46_every_item_expiring_the_sign_in_is_raised_and_summarized() -> None:
    session, out, _ = make_session(threads=1)

    def function(_: int) -> Outcome:
        raise SignInExpiredError("The sign-in expired.")

    session.total = 4
    with pytest.raises(SignInExpiredError):
        try:
            session.transfer_multithread([1, 2, 3, 4], function)
        finally:
            session.summary("Downloaded")
    assert "Downloaded 0 files, skipped 0 files, 0 errors, 4 files not transferred.\n" in out.getvalue()


def test_the_progress_line_ends_with_a_carriage_return_and_the_run_with_a_newline() -> None:
    session, out, _ = make_session(threads=1, total=3)
    outcomes = {"t": Outcome.TRANSFERRED, "s": Outcome.SKIPPED, "e": Outcome.ERROR}
    session.transfer_multithread(["t", "s", "e"], lambda item: outcomes[item])
    assert out.getvalue() == (progress(1, 3, 1, 0, 0) + progress(2, 3, 1, 1, 0) + progress(3, 3, 1, 1, 1) + "\n")


def test_ki1_the_total_set_by_the_caller_includes_the_files_skipped_before_the_engine() -> None:
    session, out, _ = make_session(threads=1, total=5)
    session.transfer_multithread([1, 2, 3], transferred)
    assert out.getvalue().startswith(progress(1, 5, 1, 0, 0))
    assert progress(3, 5, 3, 0, 0) in out.getvalue()


def test_quiet_hides_the_progress_but_not_the_errors_nor_the_summary() -> None:
    session, out, _ = make_session(threads=1, quiet=True, total=2)

    def function(item: int) -> Outcome:
        if item == 1:
            raise ValueError("bad")
        return Outcome.TRANSFERRED

    session.transfer_multithread([0, 1], function)
    assert out.getvalue() == "Error: could not transfer 1: ValueError('bad')\n"  # no progress, no final newline
    session.summary("Uploaded")
    assert out.getvalue().endswith("Uploaded 1 files, skipped 0 files, 1 errors.\n")


def test_no_items_print_nothing_and_do_not_start_anything() -> None:
    session, out, err = make_session()
    session.transfer_multithread([], transferred)
    assert out.getvalue() == ""
    assert err.getvalue() == ""
    assert (session.transferred, session.skipped, session.errors) == (0, 0, 0)


def test_the_summary_has_two_forms() -> None:
    session, out, _ = make_session(total=5)
    session.count(Outcome.TRANSFERRED)
    session.count(Outcome.SKIPPED)
    session.count(Outcome.ERROR)
    session.summary("Uploaded")
    session.total = 3
    session.summary("Downloaded")
    lines = out.getvalue().split("\n")
    assert lines[-3] == "Uploaded 1 files, skipped 1 files, 1 errors, 2 files not transferred."
    assert lines[-2] == "Downloaded 1 files, skipped 1 files, 1 errors."


def test_a_message_ends_the_pending_progress_line_first() -> None:
    session, out, _ = make_session(total=2)
    session.count(Outcome.TRANSFERRED)
    session.message("hello")
    session.message("again")
    session.count(Outcome.SKIPPED)
    assert out.getvalue() == progress(1, 2, 1, 0, 0) + "\nhello\nagain\n" + progress(2, 2, 1, 1, 0)


@pytest.mark.parametrize("threads", [0, -3, 1])
def test_less_than_one_thread_means_one_worker(threads: int) -> None:
    session, _, _ = make_session(threads=threads, quiet=True)
    first_running, second_started, release = threading.Event(), threading.Event(), threading.Event()

    def function(item: int) -> Outcome:
        if item == 0:
            first_running.set()
            assert release.wait(TIMEOUT)
        else:
            second_started.set()
        return Outcome.TRANSFERRED

    runner = threading.Thread(target=session.transfer_multithread, args=([0, 1], function))
    runner.start()
    try:
        assert first_running.wait(TIMEOUT)
        assert not second_started.wait(0.1)  # with more than one worker, the second item would start at once
    finally:
        release.set()
        runner.join(TIMEOUT)
    assert not runner.is_alive()
    assert session.transferred == 2


def test_workers_run_in_parallel_and_may_print_messages() -> None:
    session, out, _ = make_session(threads=3, quiet=True)
    barrier = threading.Barrier(3)

    def function(item: int) -> Outcome:
        barrier.wait(TIMEOUT)  # only passes if the three items run at the same time
        session.message(f"item {item}")
        return Outcome.TRANSFERRED

    session.transfer_multithread([0, 1, 2], function)
    assert session.transferred == 3
    assert sorted(out.getvalue().splitlines()) == ["item 0", "item 1", "item 2"]


class SignalingStream(io.StringIO):
    """A stream that sets an event when something is written to it."""

    def __init__(self) -> None:
        super().__init__()
        self.written = threading.Event()

    def write(self, text: str, /) -> int:
        count = super().write(text)
        self.written.set()
        return count


def test_ctrl_c_waits_for_the_running_items_and_never_starts_the_others() -> None:
    out, err = io.StringIO(), SignalingStream()
    running = threading.Event()
    ran: list[int] = []

    def interrupting_wait(
        futures: set[concurrent.futures.Future[Outcome | None]], timeout: float, return_when: str
    ) -> tuple[set[concurrent.futures.Future[Outcome | None]], set[concurrent.futures.Future[Outcome | None]]]:
        assert running.wait(TIMEOUT)  # the first item is running, the others are queued
        raise KeyboardInterrupt

    def function(item: int) -> Outcome:
        ran.append(item)
        running.set()
        assert err.written.wait(TIMEOUT)  # finishes only after the interrupt was reported
        return Outcome.TRANSFERRED

    session = TransferSession(1, False, out, err, wait_for=interrupting_wait)
    session.total = 4
    with pytest.raises(KeyboardInterrupt):
        session.transfer_multithread([0, 1, 2, 3], function)
    assert err.getvalue() == "\nInterrupted, waiting for the transfers in progress to finish...\n"
    assert ran == [0]
    assert (session.transferred, session.skipped, session.errors) == (1, 0, 0)
    session.summary("Downloaded")
    assert out.getvalue().endswith("Downloaded 1 files, skipped 0 files, 0 errors, 3 files not transferred.\n")


class InterruptedItems(list[int]):
    """Four items whose iteration raises KeyboardInterrupt, once `proceed` is set, after the first three."""

    def __init__(self, proceed: threading.Event) -> None:
        super().__init__([0, 1, 2, 3])
        self.proceed = proceed

    def __iter__(self) -> Iterator[int]:
        yield from (0, 1, 2)
        assert self.proceed.wait(TIMEOUT)
        raise KeyboardInterrupt


def test_ctrl_c_while_submitting_never_starts_the_items_not_started() -> None:
    out, err = io.StringIO(), SignalingStream()
    running = threading.Event()
    ran: list[int] = []

    def function(item: int) -> Outcome:
        ran.append(item)
        running.set()
        assert err.written.wait(TIMEOUT)
        return Outcome.TRANSFERRED

    session = TransferSession(1, True, out, err)
    session.total = 4
    with pytest.raises(KeyboardInterrupt):
        session.transfer_multithread(InterruptedItems(running), function)
    assert err.getvalue() == "\nInterrupted, waiting for the transfers in progress to finish...\n"
    assert ran == [0]
    assert session.transferred == 1


def test_an_unexpected_failure_of_the_main_thread_stops_the_others_without_the_interrupt_message() -> None:
    out, err = io.StringIO(), io.StringIO()
    futures: set[concurrent.futures.Future[Outcome | None]] = set()
    captured = threading.Event()
    ran: list[int] = []

    def failing_wait(
        pending: set[concurrent.futures.Future[Outcome | None]], timeout: float, return_when: str
    ) -> tuple[set[concurrent.futures.Future[Outcome | None]], set[concurrent.futures.Future[Outcome | None]]]:
        futures.update(pending)
        captured.set()
        raise BrokenPipeError

    def function(item: int) -> Outcome:
        ran.append(item)
        assert captured.wait(TIMEOUT)
        deadline = time.monotonic() + TIMEOUT
        while not all(f.cancelled() or f.running() for f in futures):  # the others get cancelled
            assert time.monotonic() < deadline
            time.sleep(0.001)
        return Outcome.TRANSFERRED

    session = TransferSession(1, True, out, err, wait_for=failing_wait)
    session.total = 4
    with pytest.raises(BrokenPipeError):
        session.transfer_multithread([0, 1, 2, 3], function)
    assert err.getvalue() == ""
    assert ran == [0]
    assert session.transferred == 1
