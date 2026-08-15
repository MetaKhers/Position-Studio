"""Background work with progress the UI can watch.

Everything slow in this app - scanning for terminals, pulling history,
computing tick-level excursions, rendering 600 charts, building the workbook -
runs here on a worker thread so the window never freezes.

Two decisions shape the design:

  * **One job at a time.** The MT5 Python bridge is a single global connection
    per process; two jobs talking to it at once would interleave and corrupt
    each other's reads. A queue is honest about that, where a thread pool would
    just produce mysterious failures.
  * **Cancellation is cooperative.** A job is asked to stop, not killed. The
    progress callback raises `Cancelled` on the worker thread, which unwinds
    through the pipeline's own `try/except` so the terminal session closes and
    the run row is finished rather than left dangling.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Any, Callable

# Terminal states - a job in one of these will never change again.
FINAL = ("done", "error", "cancelled")


class Cancelled(RuntimeError):
    """Raised inside a worker when the user asks it to stop."""


class Job:
    """One unit of background work, and everything the UI shows about it."""

    _counter = 0
    _counter_lock = threading.Lock()

    def __init__(self, kind: str, title: str, payload: dict | None = None):
        with Job._counter_lock:
            Job._counter += 1
            self.id = Job._counter
        self.kind = kind
        self.title = title
        self.payload = payload or {}
        self.status = "queued"
        self.message = "Queued"
        # None means "running, but the total is not known yet" - a spinner
        # rather than a bar. Faking a percentage is worse than admitting it.
        self.fraction: float | None = None
        self.done = 0
        self.total = 0
        self.detail: dict[str, Any] = {}
        self.result: Any = None
        self.error: str | None = None
        self.queued_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # -- progress, called from the worker -------------------------------
    def update(self, message: str | None = None, fraction: float | None = None,
               done: int | None = None, total: int | None = None,
               **detail) -> None:
        """Record progress. Raises `Cancelled` if a stop has been requested."""
        with self._lock:
            if message is not None:
                self.message = message
            if total is not None:
                self.total = int(total)
            if done is not None:
                self.done = int(done)
            if fraction is not None:
                self.fraction = max(0.0, min(1.0, float(fraction)))
            elif self.total:
                self.fraction = max(0.0, min(1.0, self.done / self.total))
            if detail:
                self.detail.update(detail)
        # Checked after recording, so the last thing the user saw is where it
        # actually stopped.
        if self._cancel.is_set():
            raise Cancelled("cancelled by user")

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            if self.status == "queued":
                # Never started, so it can be finished here and now.
                self.status = "cancelled"
                self.message = "Cancelled before starting"
                self.finished_at = time.time()

    @property
    def cancelling(self) -> bool:
        return self._cancel.is_set() and self.status == "running"

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "title": self.title,
                "status": self.status,
                "message": self.message,
                "fraction": self.fraction,
                "done": self.done,
                "total": self.total,
                "detail": dict(self.detail),
                "result": self.result,
                "error": self.error,
                "cancelling": self._cancel.is_set() and self.status == "running",
                "elapsed_s": round(self.elapsed(), 1),
                "finished": self.status in FINAL,
            }


class Runner:
    """Serial job queue with one worker thread."""

    def __init__(self, history: int = 40):
        self._queue: queue.Queue[tuple[Job, Callable[[Job], Any]]] = queue.Queue()
        self._jobs: dict[int, Job] = {}
        self._order: list[int] = []
        self._history = history
        self._current: Job | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._work, name="pstudio-jobs", daemon=True
        )
        self._thread.start()

    def submit(self, kind: str, title: str, fn: Callable[[Job], Any],
               payload: dict | None = None) -> Job:
        job = Job(kind, title, payload)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim()
        self._queue.put((job, fn))
        return job

    def _trim(self) -> None:
        """Forget old finished jobs - this runs for weeks on one machine."""
        while len(self._order) > self._history:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            if job and job.status not in FINAL:
                break
            self._order.pop(0)
            self._jobs.pop(oldest, None)

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def current(self) -> Job | None:
        with self._lock:
            return self._current

    def active(self) -> Job | None:
        """The job the UI should be watching: running, or next in line."""
        with self._lock:
            if self._current is not None and self._current.status not in FINAL:
                return self._current
            for job_id in reversed(self._order):
                job = self._jobs[job_id]
                if job.status == "queued":
                    return job
        return None

    def recent(self, limit: int = 8) -> list[dict]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[i].as_dict() for i in ids]

    def last_finished(self) -> Job | None:
        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs[job_id]
                if job.status in FINAL:
                    return job
        return None

    def watch(self) -> Job | None:
        """The job the UI should be showing: whatever is running, else the last
        one to finish.

        `active()` alone is not enough. A short run that starts and ends between
        two polls - or while the machine is asleep - would never be seen, so the
        UI would never announce it or reload. The client dedupes by job id, so
        reporting a finished job repeatedly is harmless.
        """
        return self.active() or self.last_finished()

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def cancel(self, job_id: int | None = None) -> bool:
        job = self.get(job_id) if job_id else self.active()
        if job is None or job.status in FINAL:
            return False
        job.cancel()
        return True

    def _work(self) -> None:
        while True:
            job, fn = self._queue.get()
            if job.status in FINAL:  # cancelled while queued
                self._queue.task_done()
                continue
            with self._lock:
                self._current = job
            job.status = "running"
            job.started_at = time.time()
            job.message = "Starting"
            try:
                job.result = fn(job)
                job.status = "done"
                job.message = job.detail.get("summary") or "Finished"
                job.fraction = 1.0
            except Cancelled:
                job.status = "cancelled"
                job.message = "Cancelled"
            except Exception as exc:
                job.status = "error"
                job.error = str(exc) or exc.__class__.__name__
                job.message = f"Failed: {job.error}"
                # Full trace to the log, one line to the UI. A stack trace in a
                # toast teaches nothing; one in the log file is how a bug on
                # someone else's machine gets fixed.
                _log_failure(job, traceback.format_exc())
            finally:
                job.finished_at = time.time()
                with self._lock:
                    self._current = None
                self._queue.task_done()
                _close_thread_db()


def _close_thread_db() -> None:
    """Nothing to close between jobs - the worker keeps its own connection.

    Left as a seam: if the worker ever becomes a pool, each thread's SQLite
    handle has to be released here.
    """
    return None


def _log_failure(job: Job, trace: str) -> None:
    from . import paths

    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log = paths.logs_dir() / "jobs.log"
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{stamp}] job {job.id} {job.kind} failed\n{trace}")
    except Exception:
        # A logging failure must never mask the original error.
        pass


_RUNNER: Runner | None = None
_RUNNER_LOCK = threading.Lock()


def runner() -> Runner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = Runner()
        return _RUNNER
