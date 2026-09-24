"""A single background writer for database work the user shouldn't wait on:
the execution trace, chat messages, and summary updates.

Jobs run strictly in the order they were queued, so a row is always written
before anything that references it. Code that must read its own writes (or
insert a row referencing a queued one) calls flush() first."""

import logging
import queue
import threading

log = logging.getLogger("gitshow.db")

_queue: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_start_lock = threading.Lock()


def _run():
    while True:
        fn, args, kwargs = _queue.get()
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("Background database job %s failed", getattr(fn, "__name__", fn))
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker
    with _start_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="db-background", daemon=True)
            _worker.start()


def enqueue(fn, *args, **kwargs) -> None:
    _ensure_worker()
    _queue.put((fn, args, kwargs))


def flush() -> None:
    """Block until every job queued so far has run."""
    _ensure_worker()
    _queue.join()
