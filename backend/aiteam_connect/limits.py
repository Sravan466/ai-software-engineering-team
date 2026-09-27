"""What this computer lets the website ask of it — set here, never by the server.

A build can send hundreds of model calls. These limits keep that from taking the
computer over (OWASP LLM10, unbounded consumption):

  concurrency          calls running at once; more wait their turn (default 1)
  requests_per_minute  calls started in any minute; more are refused
  max_prompt_chars     the longest prompt accepted; longer is refused
  max_output_tokens    the most one answer may generate; asked for more, it is clamped
  timeout_seconds      how long one call may run before it is stopped

Machine settings — GPU layers, threads, keep-alive — are here too, and only here.
The server has no way to send one: its `chat` request has no field for them.

All of it lives in `state.json` and is changed with `aiteam-connect limits`.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

from app.connector import protocol as P

DEFAULTS = {
    "concurrency": 1,
    "requests_per_minute": 60,
    "max_prompt_chars": 600_000,
    "max_output_tokens": 16_384,
    "timeout_seconds": 900,
}
#: How many calls may wait for a free slot, per slot, before more are refused.
QUEUE_PER_SLOT = 8
MACHINE_KEYS = ("gpu_layers", "threads", "keep_alive")


class LimitRefused(Exception):
    code = P.ERR_LIMIT


def read(state: dict) -> dict:
    """The limits in `state`, each checked, with the defaults for anything unset."""
    saved = state.get("limits") if isinstance(state.get("limits"), dict) else {}
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = saved.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            out[key] = value
    # Checked against the same schema the website reads them with.
    return P.LimitsReport(**out).model_dump()


def machine(state: dict) -> dict:
    """GPU layers, threads and keep-alive, as set on this computer."""
    saved = state.get("machine") if isinstance(state.get("machine"), dict) else {}
    out: dict = {}
    for key in ("gpu_layers", "threads"):
        value = saved.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    keep = saved.get("keep_alive")
    if isinstance(keep, str) and keep.strip():
        out["keep_alive"] = keep.strip()[:16]
    return out


class Gate:
    """Admits one model call at a time (or `concurrency`), and counts them per minute."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._running = 0
        self._waiting = 0
        self._started: deque[float] = deque()

    def admit(self, limits: dict, prompt_chars: int, cancelled: threading.Event) -> None:
        """Wait for a slot, or raise `LimitRefused` saying which limit and how to change it."""
        if prompt_chars > limits["max_prompt_chars"]:
            raise LimitRefused(
                f"The prompt is {prompt_chars:,} characters; this computer accepts at most "
                f"{limits['max_prompt_chars']:,} (aiteam-connect limits --max-prompt-chars)."
            )
        with self._cond:
            now = time.monotonic()
            while self._started and now - self._started[0] > 60:
                self._started.popleft()
            if len(self._started) >= limits["requests_per_minute"]:
                raise LimitRefused(
                    f"This computer allows {limits['requests_per_minute']} model calls a minute, and "
                    "that many have already started (aiteam-connect limits --requests-per-minute)."
                )
            if self._waiting >= limits["concurrency"] * QUEUE_PER_SLOT:
                raise LimitRefused(
                    f"{self._waiting} calls are already waiting for this computer "
                    f"(it runs {limits['concurrency']} at a time: aiteam-connect limits --concurrency)."
                )
            self._started.append(now)
            self._waiting += 1
            deadline = now + limits["timeout_seconds"]
            try:
                while self._running >= limits["concurrency"]:
                    if cancelled.is_set():
                        raise LimitRefused("Stopped while waiting for a free slot.")
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise LimitRefused(
                            f"Waited {limits['timeout_seconds']} seconds for a free slot "
                            f"(this computer runs {limits['concurrency']} at a time)."
                        )
                    self._cond.wait(min(left, 0.5))
                self._running += 1
            finally:
                self._waiting -= 1

    def release(self) -> None:
        with self._cond:
            self._running = max(self._running - 1, 0)
            self._cond.notify_all()

    @property
    def running(self) -> int:
        with self._cond:
            return self._running


def clamp_tokens(requested: int, limits: dict) -> int:
    return max(1, min(int(requested), limits["max_output_tokens"]))


def describe(limits: dict, machine_settings: Optional[dict] = None) -> list[str]:
    lines = [
        f"  concurrency          {limits['concurrency']}",
        f"  requests per minute  {limits['requests_per_minute']}",
        f"  max prompt chars     {limits['max_prompt_chars']:,}",
        f"  max output tokens    {limits['max_output_tokens']:,}",
        f"  timeout              {limits['timeout_seconds']} s",
    ]
    for key, value in (machine_settings or {}).items():
        lines.append(f"  {key.replace('_', ' '):<20} {value}")
    return lines
