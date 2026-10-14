"""Each account's finished phases, read for Auto's choice of model (#96).

`track_record` judges; this reads what it judges, once per account per minute at
most — `_resolve_chain` runs on every model call, and a query per call would be a
query per call. A phase finishing (`changed`) makes the next read fresh, so the
build after a good or a bad run is chosen with that run counted.

Only the account's own builds are read: one person's results never decide another's
models.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from app.core.constants import CODE_PHASES, BuildStatus, PhaseStatus, TestStatus
from app.core.logging import get_logger
from app.router import track_record
from app.router.track_record import Outcome, Record

log = get_logger(__name__)

#: How long a read stands when nothing finishes in between.
TTL_SECONDS = 60.0
#: The most rows one read looks at — far more than `WINDOW` runs per agent per model
#: needs, and a bound on a read over years of builds.
SCAN_ROWS = 4000

#: What a failed row says when the model never delivered. A row failed for any other
#: reason (a Stop, a computer that went away) says nothing about the model.
MODEL_FAILURES = ("The model provider failed", "The agent produced no output")

#: The most accounts kept at once; the least recently read goes first.
MAX_ACCOUNTS = 256

_lock = threading.Lock()
#: Bumped for every account at once (`changed()` with no account).
_everyone = 0
#: account -> how many of its phases have finished since this process started.
_generation: dict[str, int] = {}
#: account -> (read at, (everyone, account) generations, {role: {model: Record}})
_cache: dict[str, tuple[float, tuple[int, int], dict[str, dict[str, Record]]]] = {}


def changed(user_id: Optional[str] = None) -> None:
    """A phase of `user_id`'s finished: that account's next read is fresh. Cheap —
    a counter. No account: every account's."""
    global _everyone
    with _lock:
        if user_id is None:
            _everyone += 1
        else:
            _generation[user_id] = _generation.get(user_id, 0) + 1


def records(user_id: Optional[str]) -> dict[str, dict[str, Record]]:
    """`{role: {model: Record}}` for this account. Empty with no account, or when
    the database can't be read — Auto then decides on what the router knows."""
    if not user_id:
        return {}
    now = time.monotonic()
    with _lock:
        generation = (_everyone, _generation.get(user_id, 0))
        hit = _cache.get(user_id)
        if hit is not None and hit[1] == generation and now - hit[0] < TTL_SECONDS:
            return hit[2]
    try:
        found = summarise(_read(user_id))
    except Exception as e:  # noqa: BLE001 - no record is a cold start, never a failed call
        log.warning("Couldn't read this account's build history for Auto: %s", e)
        return {}
    with _lock:
        # A phase that finished during the read leaves this one stale: not kept.
        if (_everyone, _generation.get(user_id, 0)) == generation:
            _cache.pop(user_id, None)
            _cache[user_id] = (now, generation, found)
            while len(_cache) > MAX_ACCOUNTS:
                _cache.pop(min(_cache, key=lambda uid: _cache[uid][0]))
    return found


def forget(user_id: Optional[str] = None) -> None:
    with _lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)


def summarise(rows: list[dict]) -> dict[str, dict[str, Record]]:
    """Rows, newest first, into each agent's record on each model."""
    runs: dict[str, dict[str, list[Outcome]]] = {}
    for row in rows:
        for outcome in outcomes(row):
            per_model = runs.setdefault(row["phase"], {})
            kept = per_model.setdefault(outcome.model, [])
            if len(kept) < track_record.WINDOW:
                kept.append(outcome)
    return {
        role: {model: track_record.judge(model, kept) for model, kept in per_model.items()}
        for role, per_model in runs.items()
    }


def outcomes(row: dict) -> list[Outcome]:
    """What one finished phase row says about the model, or models, behind it.

    A row normally speaks for the model that served it. A fallback is put down to
    the models the calls actually fell back from (`model_pick.fell_back_from`,
    recorded call by call when the phase finishes), never to whichever model
    answered. A phase that failed before anything was served can only be put down
    to the model it started on (`model_pick.model`, written when it starts).
    """
    found = row.get("model_pick")
    pick: dict = found if isinstance(found, dict) else {}
    picked = pick.get("model") if isinstance(pick.get("model"), str) else None
    provider, model = row.get("provider_used"), row.get("model_used")
    served = f"{provider}:{model}" if provider and model else None

    if row.get("status") == PhaseStatus.FAILED.value:
        note = row.get("feedback") or ""
        who = picked or served
        if who and note.startswith(MODEL_FAILURES):
            return [Outcome(model=who, failed=True)] + _failed_before(pick)
        return _failed_before(pick)
    if not served or pick.get("calls") == 0:
        # A kept attempt re-checked without a model call (#76) says nothing about one.
        return _failed_before(pick)

    raw = pick.get("fell_back_from")
    # None: a row from before calls recorded what they fell back from — unknown.
    fell_from = {m for m in raw if isinstance(m, str)} if isinstance(raw, list) else None
    out = [
        Outcome(
            model=served,
            schema=row.get("schema_status"),
            built=_built(row),
            tested=_status(row.get("test_status"), TestStatus.OK.value, TestStatus.FAILED.value),
            cut_off=_cut_off(row.get("truncated")),
            fell_back=None if fell_from is None else any(same_model(m, served) for m in fell_from),
            latency_ms=int(row.get("latency_ms") or 0),
        )
    ]
    for other in sorted(fell_from or ()):
        if not same_model(other, served):
            # It was sent this phase's work and didn't answer.
            out.append(Outcome(model=other, failed=True))
    return out + _failed_before(pick)


def _failed_before(pick: dict) -> list[Outcome]:
    """Attempts at this phase that failed on a model and were then deleted (a retry
    clears dead rows), carried on the row that replaced them so the failure counts."""
    raw = pick.get("failed_before")
    if not isinstance(raw, list):
        return []
    return [Outcome(model=m, failed=True) for m in raw if isinstance(m, str)]


def same_model(a: str, b: str) -> bool:
    """Whether two specs name one model: runtimes answer to `name` and `name:latest` alike."""
    def norm(spec: str) -> str:
        return spec[: -len(":latest")] if spec.endswith(":latest") else spec

    return norm(a) == norm(b)


def _built(row: dict) -> Optional[bool]:
    """The sandbox build's verdict, else the compile check's — for code phases only:
    every other phase reports `ok` because it wrote nothing to compile."""
    if row.get("phase") not in CODE_PHASES:
        return None
    ran = _status(row.get("build_run_status"), BuildStatus.OK.value, BuildStatus.FAILED.value)
    if ran is not None:
        return ran
    return _status(row.get("build_status"), BuildStatus.OK.value, BuildStatus.FAILED.value)


def _status(value: object, good: str, bad: str) -> Optional[bool]:
    if value == good:
        return True
    if value == bad:
        return False
    return None


def _cut_off(truncated: object) -> Optional[bool]:
    if isinstance(truncated, bool) or not isinstance(truncated, (int, float)):
        return None
    return truncated > 0


def _read(user_id: str) -> list[dict]:
    """This account's finished phase rows, newest first, with only the facts judged.

    The JSON columns can be large (a build's step logs, a hand-off's digests), so
    the one value wanted from each is read in the query rather than the column.
    """
    from app.db.base import SessionLocal
    from app.db.models import PhaseResult, Project

    columns = (
        PhaseResult.phase,
        PhaseResult.status,
        PhaseResult.provider_used,
        PhaseResult.model_used,
        PhaseResult.model_pick,
        PhaseResult.schema_status,
        PhaseResult.build_status,
        PhaseResult.build_run["status"].as_string().label("build_run_status"),
        PhaseResult.test_run["status"].as_string().label("test_status"),
        PhaseResult.handoff["truncated_replies"].as_integer().label("truncated"),
        PhaseResult.feedback,
        PhaseResult.latency_ms,
    )
    db = SessionLocal()
    try:
        rows = (
            db.query(*columns)
            .join(Project, Project.id == PhaseResult.project_id)
            .filter(
                Project.owner_id == user_id,
                PhaseResult.status != PhaseStatus.RUNNING.value,
                PhaseResult.completed_at.isnot(None),
            )
            .order_by(PhaseResult.completed_at.desc())
            .limit(SCAN_ROWS)
            .all()
        )
        return [dict(row._mapping) for row in rows]
    finally:
        db.close()
