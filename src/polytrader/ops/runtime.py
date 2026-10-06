"""Process heartbeat, atomic research checkpoints and cooperative SIGTERM."""

import asyncio
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import signal
import time

from .files import atomic_json, encode, utc


@contextmanager
def graceful_signals():
    """Let asyncio.run cancel the main coroutine and execute its finally blocks."""
    previous = signal.getsignal(signal.SIGTERM)
    try:
        main_task = asyncio.current_task()
    except RuntimeError:
        main_task = None
    def stop(signum, frame):
        if main_task is not None:
            main_task.get_loop().call_soon_threadsafe(main_task.cancel)
        else:
            raise KeyboardInterrupt
    # signal handlers only work in the main thread (some notebook/test callers aren't).
    try:
        signal.signal(signal.SIGTERM, stop)
    except ValueError:
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


async def until_stopped(coroutine):
    # Capture the root task, not whichever feed task happens to receive SIGTERM.
    with graceful_signals():
        return await coroutine


class Runtime:
    def __init__(self, directory, strategy, config):
        self.directory = Path(directory)
        self.next_status = self.next_summary = 0
        self.started = time.monotonic()
        self.last = {}
        self.identity = dict(instance_id=os.environ.get("POLYTRADER_INSTANCE", strategy),
                             run_id=self.directory.name, strategy=strategy,
                             git_sha=os.environ.get("POLYTRADER_GIT_SHA", "unknown"),
                             image=os.environ.get("POLYTRADER_IMAGE", "local"),
                             release=os.environ.get("POLYTRADER_RELEASE", "local"),
                             config_hash=hashlib.sha256(encode(config).encode()).hexdigest(),
                             started_at=utc())
        atomic_json(self.directory / "runtime.json", self.identity)

    def publish(self, summary, service=None, *, warming_up=False, force=False):
        now = time.monotonic()
        if not force and now < self.next_status:
            return
        usage = shutil.disk_usage(self.directory)
        minimum = int(os.environ.get("POLYTRADER_MIN_FREE_BYTES", "0"))
        self.last = {**self.identity, "heartbeat_at": utc(), "process_state": "running",
                     "transport_healthy": bool(service and service.healthy),
                     "continuity": getattr(service, "continuity", 0), "warming_up": warming_up,
                     "unavailable_books": sum(b.status != "live" for b in service.collection.books.values()) if service else None,
                     "disk_free_bytes": usage.free, "disk_used_fraction": usage.used / usage.total,
                     "logging_ok": True, "elapsed_seconds": now - self.started}
        atomic_json(self.directory / "status.json", self.last)
        if force or now >= self.next_summary:
            atomic_json(self.directory / "current_summary.json", dict(updated_at=utc(), summary=summary))
            self.next_summary = now + 60
        # Positions are checkpointed separately so crashes do not erase open risk.
        atomic_json(self.directory / "checkpoint.json", dict(updated_at=utc(), summary=summary,
                    restart_policy="censor_unresolved_and_start_fresh"))
        self.next_status = now + 10
        if usage.free < minimum:
            raise OSError("Insufficient free disk space for durable research logging")

    def finish(self, termination):
        atomic_json(self.directory / "status.json", {**self.identity, **self.last,
                    "heartbeat_at": utc(), "process_state": "stopped", "termination": termination})
