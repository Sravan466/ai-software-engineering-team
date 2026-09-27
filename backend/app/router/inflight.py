"""The model calls a build has in flight right now, so Stop can interrupt them.

A build runs its agents in a worker thread, and Stop arrives on another request.
Stop used to set a flag the run read when the agent *returned* — minutes later, on
a local model. Now the run names itself while it works (`building`), every model
call it makes registers how to cancel it (`track`), and Stop cancels them all
(`cancel`). The call then raises `RequestCancelled`, which no retry and no fallback
link swallows.

Each source cancels in its own way — closing the HTTP connection for a runtime on
this machine, a `cancel` over the connector for one on the user's computer — so what
is registered is a callable, not a mechanism.
"""
from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

_build: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("build", default=None)
#: Which agent is asking — only ever shown as a label ("Backend Engineer"), never a prompt.
_agent: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("agent", default=None)

_lock = threading.Lock()
#: project id -> {call id: cancel callable}
_calls: dict[str, dict[str, Callable[[], None]]] = {}
#: Call ids that were cancelled, so the call can tell a cancel from a failure. Cleared
#: when the call leaves `track`.
_cancelled: set[str] = set()


@contextmanager
def building(project_id: str, name: Optional[str] = None) -> Iterator[None]:
    """Everything in the block is work for this build."""
    token = _build.set({"id": project_id, "name": name})
    try:
        yield
    finally:
        _build.reset(token)


@contextmanager
def agent(title: Optional[str]) -> Iterator[None]:
    token = _agent.set(title)
    try:
        yield
    finally:
        _agent.reset(token)


def current_agent() -> Optional[str]:
    return _agent.get()


def current() -> Optional[dict]:
    """`{id, name}` of the build this work is for, or None outside of one."""
    return _build.get()


@contextmanager
def track(call_id: str, cancel: Callable[[], None]) -> Iterator[None]:
    """Register one model call as cancellable for the current build, if there is one."""
    build = _build.get()
    if build is None:
        yield
        return
    with _lock:
        _calls.setdefault(build["id"], {})[call_id] = cancel
    try:
        yield
    finally:
        with _lock:
            calls = _calls.get(build["id"])
            if calls is not None:
                calls.pop(call_id, None)
                if not calls:
                    _calls.pop(build["id"], None)
            _cancelled.discard(call_id)


def was_cancelled(call_id: str) -> bool:
    with _lock:
        return call_id in _cancelled


def cancel(project_id: str) -> int:
    """Cancel every model call this build has in flight. Returns how many."""
    with _lock:
        calls = list((_calls.get(project_id) or {}).items())
        _cancelled.update(call_id for call_id, _ in calls)
    for _, fn in calls:
        try:
            fn()
        except Exception:  # noqa: BLE001 - a call that already ended has nothing to stop
            pass
    return len(calls)


def active(project_id: str) -> int:
    with _lock:
        return len(_calls.get(project_id) or {})
