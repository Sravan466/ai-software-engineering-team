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
_MODEL_FAILURES = ("The model provider failed", "The agent produced no output")

_lock = threading.Lock()
_generation = 0
#: account -> (read at, generation, {role: {model: Record}})
_cache: dict[str, tuple[float, int, dict[str, dict[str, Record]]]] = {}


def changed() -> None:
    """A phase finished: every account's next read is fresh. Cheap — a counter."""
    global _generation
    with _lock:
        _generation += 1


def records(user_id: Optional[str]) -> dict[str, dict[str, Record]]:
    """`{role: {model: Record}}` for this account. Empty with no account, or when
    the database can't be read — Auto then decides on what the router knows."""
    if not user_id:
        return {}
    now = time.monotonic()
    with _lock:
        generation = _generation
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
        if _generation == generation:
            _cache[user_id] = (now, generation, found)
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

    A row normally speaks for the model that served it. Two cases need the model the
    phase *started* on, which only `model_pick` (written when it starts) records:
    a phase that failed before anything was served, and one whose chain fell back —
    there the picked model didn't answer, and the one that did is not to blame.
    """
    pick = row.get("model_pick") if isinstance(row.get("model_pick"), dict) else {}
    picked = pick.get("model") if isinstance(pick.get("model"), str) else None
    provider, model = row.get("provider_used"), row.get("model_used")
    served = f"{provider}:{model}" if provider and model else None

    if row.get("status") == PhaseStatus.FAILED.value:
        note = row.get("feedback") or ""
        who = picked or served
        if who and note.startswith(_MODEL_FAILURES):
            return [Outcome(model=who, failed=True)]
        return []
    if not served:
        return []

    fell_back = pick.get("fell_back") if isinstance(pick.get("fell_back"), bool) else None
    out = [
        Outcome(
            model=served,
            schema=row.get("schema_status"),
            built=_built(row),
            tested=_status(row.get("test_status"), TestStatus.OK.value, TestStatus.FAILED.value),
            cut_off=_cut_off(row.get("truncated")),
            # The served model is only answerable for a fallback when it was the one
            # picked: after one, it is the model that answered.
            fell_back=fell_back if served == picked else None,
            latency_ms=int(row.get("latency_ms") or 0),
        )
    ]
    if fell_back and picked and picked != served:
        out.append(Outcome(model=picked, failed=True))
    return out


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
