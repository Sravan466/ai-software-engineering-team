"""How many local generations may share one computer's memory at once.

A local model's window is clamped so its KV cache fits in `LOCAL_RAM_FRACTION` of
the RAM of the computer it runs on (`model_profile.resolve_window`). That budget is
per generation. Since builds on different projects run at the same time (#40), two
of them on two different local models would each claim the whole fraction — about
1.2 of RAM between them at the default — and the runtime then either reloads models
on every call or the host swaps (#42).

So generations are admitted per *memory pool* — this machine for every runtime that
shares its RAM, and each paired computer for its own — at most
`LOCAL_CONCURRENT_GENERATIONS` at a time, first come first served. The RAM fraction
is split between those slots, so the combined budget never exceeds it, and each
model's window stays the same from call to call rather than shrinking with whatever
else happens to be running. Cloud calls and runtimes on hosts whose memory is
unknown are never queued: nothing clamped them, so there is nothing to protect.

Zero leaves it to the runtime — no queue, and every generation is budgeted the
whole fraction, which is the pre-#42 behaviour.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

#: The pool every runtime sharing this machine's RAM draws from.
THIS_MACHINE = "this-machine"

#: How often a queued call looks again for a Stop. The slot itself is handed over
#: as soon as it frees; this only bounds how long a cancelled waiter lingers.
_POLL_SECONDS = 0.25


def limit() -> int:
    """Generations one computer runs at once; 0 means unlimited."""
    return max(int(settings.local_concurrent_generations or 0), 0)


def ram_share() -> float:
    """The share of RAM one generation's KV cache may claim."""
    slots = limit()
    return settings.local_ram_fraction / slots if slots > 1 else settings.local_ram_fraction


class _Pool:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.active = 0
        self.queue: deque[object] = deque()


_pools: dict[str, _Pool] = {}
_pools_lock = threading.Lock()


def _pool(key: str) -> _Pool:
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = _pools[key] = _Pool()
        return pool


class GateCancelled(Exception):
    """Stop arrived while the call was still queued for a slot."""


@contextmanager
def slot(
    key: Optional[str],
    *,
    cancelled: Callable[[], bool] = lambda: False,
    label: str = "",
) -> Iterator[None]:
    """Hold one of `key`'s generation slots for the block.

    `key` None (a cloud model, or a computer whose memory is unknown) and a limit of
    zero pass straight through. Otherwise the call queues in arrival order, and
    `cancelled` is polled while it waits so a Stop doesn't sit behind another
    build's generation.
    """
    cap = limit()
    if key is None or cap <= 0:
        yield
        return
    pool = _pool(key)
    ticket = object()
    started = time.monotonic()
    with pool.cond:
        pool.queue.append(ticket)
        waited = False
        while not (pool.queue[0] is ticket and pool.active < cap):
            if not waited:
                waited = True
                log.info(
                    "%s is waiting for a generation slot on %s (%d running, limit %d).",
                    label or "A model call",
                    "this machine" if key == THIS_MACHINE else key,
                    pool.active,
                    cap,
                )
            if cancelled():
                pool.queue.remove(ticket)
                pool.cond.notify_all()
                raise GateCancelled()
            pool.cond.wait(_POLL_SECONDS)
        pool.queue.popleft()
        pool.active += 1
        pool.cond.notify_all()
    if waited:
        log.info("%s got its slot after %.1fs.", label or "A model call", time.monotonic() - started)
    try:
        yield
    finally:
        with pool.cond:
            pool.active -= 1
            pool.cond.notify_all()


def running(key: str) -> int:
    """Generations holding a slot in `key` right now."""
    with _pools_lock:
        pool = _pools.get(key)
    if pool is None:
        return 0
    with pool.cond:
        return pool.active


def waiting(key: str) -> int:
    """Generations queued for a slot in `key` right now."""
    with _pools_lock:
        pool = _pools.get(key)
    if pool is None:
        return 0
    with pool.cond:
        return len(pool.queue)
