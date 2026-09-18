"""Mockup builds in flight, and how far along each one is.

Drawing a site is a dozen or more model calls, which on a local model is minutes, not
seconds. Holding an HTTP request open for that is indistinguishable from a hang — the
same reason every pipeline control returns at once — so a build runs on its own
thread and reports its progress here, and the Preview tab polls it.

Process-local on purpose. A build is tied to the thread running it; if the process
restarts, the build is gone, and an empty registry says exactly that.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.core.logging import get_logger

log = get_logger(__name__)

#: The passes, in order, with what the Preview tab calls each one.
STAGES: dict[str, str] = {
    "queued": "Waiting for the model",
    "design": "Choosing the design system",
    "plan": "Planning pages and data",
    "seed": "Writing sample records",
    "sections": "Building sections",
    "verify": "Checking the site",
    "done": "Done",
    "failed": "Failed",
}


@dataclass
class Progress:
    project_id: str
    origin: str
    stage: str = "queued"
    done: int = 0
    total: int = 0
    detail: str = ""
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "label": STAGES.get(self.stage, self.stage),
            "done": self.done,
            "total": self.total,
            "detail": self.detail,
            "error": self.error,
            "running": self.running,
            "origin": self.origin,
            "elapsed_s": int((self.finished_at or time.time()) - self.started_at),
        }


class Reporter:
    """What a build writes its progress through. Thread-safe; cheap to call often."""

    def __init__(self, registry: "Jobs", project_id: str) -> None:
        self._registry = registry
        self._project_id = project_id

    def stage(self, name: str, total: int = 0, detail: str = "") -> None:
        self._registry._update(self._project_id, stage=name, done=0, total=total, detail=detail)

    def step(self, detail: str = "") -> None:
        self._registry._advance(self._project_id, detail)


class NullReporter(Reporter):
    """For a build nobody is watching — the evaluation harness, a test."""

    def __init__(self) -> None:  # noqa: D401 - deliberately no registry
        pass

    def stage(self, name: str, total: int = 0, detail: str = "") -> None:
        return None

    def step(self, detail: str = "") -> None:
        return None


class Jobs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Progress] = {}
        self._done = threading.Condition(self._lock)

    # ── reading ──────────────────────────────────────────────────────────────
    def get(self, project_id: str) -> Optional[Progress]:
        with self._lock:
            return self._jobs.get(project_id)

    def running(self, project_id: str) -> bool:
        job = self.get(project_id)
        return bool(job and job.running)

    def visible(self, project_id: str) -> Optional[dict]:
        """What the API reports: a build in flight, or one that failed and said why.

        A build that succeeded reports nothing — the revision it saved is the report.
        """
        job = self.get(project_id)
        if job is None or (not job.running and job.error is None):
            return None
        return job.as_dict()

    # ── running ──────────────────────────────────────────────────────────────
    def claim(self, project_id: str, origin: str) -> Optional[Reporter]:
        """Register a build, or None when one is already running for this project."""
        with self._lock:
            current = self._jobs.get(project_id)
            if current is not None and current.running:
                return None
            self._jobs[project_id] = Progress(project_id=project_id, origin=origin)
        return Reporter(self, project_id)

    def finish(self, project_id: str, error: Optional[str] = None) -> None:
        with self._lock:
            job = self._jobs.get(project_id)
            if job is None:
                return
            job.finished_at = time.time()
            job.error = error
            job.stage = "failed" if error else "done"
            self._done.notify_all()

    def start(
        self, project_id: str, origin: str, work: Callable[[Reporter], None]
    ) -> bool:
        """Run `work` on its own thread. False when a build is already running."""
        reporter = self.claim(project_id, origin)
        if reporter is None:
            return False

        def target() -> None:
            try:
                work(reporter)
            except Exception as e:  # noqa: BLE001 - reported to the Preview tab
                log.warning("Mockup build failed for %s: %s", project_id, e)
                self.finish(project_id, _message(e))
                return
            self.finish(project_id)

        threading.Thread(target=target, name=f"mockup-{project_id[:8]}", daemon=True).start()
        return True

    def wait(self, project_id: str, timeout: float = 60.0) -> bool:
        """Block until this project's build finishes. For tests and the harness."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                job = self._jobs.get(project_id)
                if job is None or not job.running:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._done.wait(remaining)

    def forget(self, project_id: str) -> None:
        with self._lock:
            job = self._jobs.get(project_id)
            if job is not None and not job.running:
                self._jobs.pop(project_id, None)

    # ── internal ─────────────────────────────────────────────────────────────
    def _update(self, project_id: str, **changes) -> None:
        with self._lock:
            job = self._jobs.get(project_id)
            if job is None:
                return
            for key, value in changes.items():
                setattr(job, key, value)

    def _advance(self, project_id: str, detail: str) -> None:
        with self._lock:
            job = self._jobs.get(project_id)
            if job is None:
                return
            job.done = min(job.done + 1, job.total) if job.total else job.done + 1
            if detail:
                job.detail = detail


def _message(error: Exception) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text[:400]


jobs = Jobs()
