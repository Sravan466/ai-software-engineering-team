"""Versions of a finished build (#79): what Deploy, Download and a push send.

A finished build used to be one state, overwritten in place by anything that changed
it. Every way of finishing now records a version — the first build is v1, every change
kept adds one, and so does a change made on the app preview or a version restored — so
"what is live" and "what did I download last week" have names, and any earlier state
can be brought back.

A version keeps its own copy of each phase's deliverable (`snapshot`), not only the ids
of the rows that held it: a change rewinds phases, the rows after the one it re-runs are
replaced, and a version that only pointed at them would have nothing left to restore.

Restoring never rewrites history: the old deliverables become current again as the
newest attempt of their phase (a copy, when a newer attempt exists), and restoring v1
after v3 records v4 — the way v0 and Lovable do it.
"""
from __future__ import annotations

import difflib
import threading
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Iterator, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer, object_session

from app.core import artifacts
from app.core.constants import PHASE_ORDER, PhaseStatus, PipelineStatus
from app.core.logging import get_logger
from app.db.models import PhaseResult, Project, Version

log = get_logger(__name__)

#: What a version keeps of each phase: everything the archive, the review and a
#: restored attempt read. Never the person's feedback, which belongs to the attempt.
SNAP_FIELDS = (
    "phase",
    "agent",
    "output",
    "content_md",
    "model_used",
    "provider_used",
    "is_local",
    "schema_status",
    "schema_note",
    "stack_status",
    "stack_note",
    "build_status",
    "build_note",
    "build_run",
    "test_run",
    "scan",
    "skills_used",
    "handoff",
    "total_tokens",
    "latency_ms",
)

#: How a version came to be.
FIRST_BUILD = "first_build"
CHANGE = "change"
RESTORE = "restore"
EDIT = "edit"

#: A diff shows this many lines of one file, and this many in all, before it says
#: the rest is in the download.
_FILE_LINES = 400
_TOTAL_LINES = 4000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


# ── one writer of a build's version numbers at a time ─────────────────────────
#
# A version's number is the next one after the newest, read and then inserted — so two
# writers at once (a page loading `/artifacts` and `/versions` together on an old
# build, or a finishing run and a refetch) would both take the same number. Each build
# gets a lock, held from the read to the commit; the unique constraint on the table is
# the backstop for anything that gets past it (another process). Weak values, so the
# table holds only builds someone is writing right now.
_locks: "weakref.WeakValueDictionary[str, threading.RLock]" = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()


@contextmanager
def writing(project_id: str) -> Iterator[None]:
    """Hold this build's version numbers; commit inside, before leaving."""
    with _locks_guard:
        lock = _locks.get(project_id)
        if lock is None:
            lock = threading.RLock()
            _locks[project_id] = lock
    with lock:
        yield


# ── reading ───────────────────────────────────────────────────────────────────
def current_rows(db: Session, project: Project) -> list[PhaseResult]:
    """The attempt that counts for each phase — what the build is right now."""
    return artifacts._current(artifacts._rows(project))  # noqa: SLF001 - the one definition


def current_ids(db: Session, project: Project) -> list[str]:
    """The ids of the attempts that count, finished ones only."""
    return [r.id for r in current_rows(db, project) if r.status != PhaseStatus.RUNNING.value]


def all_for(db: Session, project: Project, light: bool = False) -> list[Version]:
    """Every version, oldest first. `light` leaves the snapshots unread — a list needs
    labels and counts, not every phase's code."""
    query = db.query(Version).filter(Version.project_id == project.id)
    if light:
        query = query.options(defer(Version.snapshot))
    return query.order_by(Version.number).all()


def by_number(db: Session, project: Project, number: int) -> Optional[Version]:
    # The first one recorded, should a duplicate from before the constraint exist.
    return (
        db.query(Version)
        .filter(Version.project_id == project.id, Version.number == number)
        .order_by(Version.created_at, Version.id)
        .first()
    )


def current(db: Session, project: Project) -> Optional[Version]:
    """The version the build is at, or None before its first."""
    if not project.current_version_id:
        return None
    found = db.get(Version, project.current_version_id)
    return found if found is not None and found.project_id == project.id else None


def latest_number(db: Session, project: Project) -> int:
    newest = (
        db.query(Version.number)
        .filter(Version.project_id == project.id)
        .order_by(Version.number.desc())
        .first()
    )
    return int(newest[0]) if newest else 0


def summary_of(project: Project) -> Optional[dict]:
    """`{number, label, kind, created_at}` for the page — one lookup, never a write."""
    db = object_session(project)
    if db is None or not project.current_version_id:
        return None
    found = db.get(Version, project.current_version_id, options=[defer(Version.snapshot)])
    if found is None:
        return None
    return {
        "number": found.number,
        "label": found.label,
        "kind": found.kind,
        "created_at": _iso(found.created_at),
    }


def _file_count(snap: dict) -> int:
    return sum(
        1
        for item in (snap or {}).get("phases") or []
        for _ in artifacts.iter_files(item.get("output") if isinstance(item.get("output"), dict) else {})
    )


def out(version: Version, project: Project) -> dict:
    """One version as the API shows it."""
    files = version.file_count if version.file_count is not None else _file_count(version.snapshot)
    live = project.deploy_status in ("ready", "handed_off")
    return {
        "id": version.id,
        "number": version.number,
        "label": version.label,
        "kind": version.kind,
        "change_request_id": version.change_request_id,
        "restored_from": version.restored_from,
        "created_at": _iso(version.created_at),
        "current": project.current_version_id == version.id,
        # What is live, not what was last sent: a deploy that failed changes nothing.
        "deployed": live and project.deployed_version == version.number,
        "pushed": project.github_pushed_version == version.number,
        "files": files,
    }


# ── writing ───────────────────────────────────────────────────────────────────
def snapshot(db: Session, project: Project) -> tuple[list[str], dict]:
    """(row ids, snapshot) of what the build holds now."""
    rows = [r for r in current_rows(db, project) if r.status != PhaseStatus.RUNNING.value]
    phases = []
    for row in rows:
        item = {"id": row.id, "created_at": _iso(row.created_at)}
        for name in SNAP_FIELDS:
            item[name] = getattr(row, name)
        phases.append(item)
    return [r.id for r in rows], {"charter": project.charter, "phases": phases}


def record(
    db: Session,
    project: Project,
    label: str,
    kind: str,
    change_id: Optional[str] = None,
    restored_from: Optional[int] = None,
) -> Version:
    """Name what the build holds now as its next version, and make it current.

    The caller commits — with whatever else made this version, in one transaction —
    inside `writing(project.id)`, which it holds from before this call."""
    ids, snap = snapshot(db, project)
    version = Version(
        project_id=project.id,
        number=latest_number(db, project) + 1,
        label=(label or "").strip()[:300] or "Untitled",
        kind=kind,
        change_request_id=change_id,
        restored_from=restored_from,
        phase_result_ids=ids,
        snapshot=snap,
        file_count=_file_count(snap),
    )
    db.add(version)
    db.flush()
    project.current_version_id = version.id
    log.info("Recorded v%d of %s (%s): %s", version.number, project.id, kind, version.label)
    return version


def ensure_first(db: Session, project: Project) -> Optional[Version]:
    """The version a finished build is at — recording v1 for one from before versions.

    Lazily, the first time anything asks, rather than in a migration: a backfill is a
    query over every build, and an old finished build that is never opened again
    never needs one."""
    found = current(db, project)
    if found is not None:
        return found
    if project.status != PipelineStatus.COMPLETED.value:
        return None
    with writing(project.id):
        # Asked again under the lock: another request may have recorded it meanwhile.
        db.refresh(project, ["current_version_id", "status"])
        found = current(db, project)
        if found is not None:
            return found
        existing = all_for(db, project, light=True)
        if existing:
            # A pointer lost to an interrupted write: the newest is where the build is.
            project.current_version_id = existing[-1].id
            db.commit()
            return existing[-1]
        try:
            version = record(db, project, "First build", FIRST_BUILD)
            db.commit()
        except IntegrityError:
            # Another process got there first; theirs is v1.
            db.rollback()
            return current(db, project) or by_number(db, project, 1)
        return version


def matches_current(db: Session, project: Project, version: Optional[Version] = None) -> bool:
    """Whether the build holds exactly what `version` (default: the current one) does."""
    version = version or current(db, project)
    if version is None:
        return False
    return set(current_ids(db, project)) == set(version.phase_result_ids or [])


# ── assembling one ────────────────────────────────────────────────────────────
def rows_of(version: Version) -> list[SimpleNamespace]:
    """Stand-ins for the phase rows a version saved, for `artifacts.assemble`."""
    out_rows = []
    base = version.created_at or _now()
    for i, item in enumerate((version.snapshot or {}).get("phases") or []):
        fields = {name: item.get(name) for name in SNAP_FIELDS}
        out_rows.append(
            SimpleNamespace(
                id=str(item.get("id") or f"v{version.number}-{i}"),
                status=PhaseStatus.APPROVED.value,
                # In the order they were saved: `_current` keeps one per phase anyway.
                created_at=base,
                **fields,
            )
        )
    return out_rows


def assemble(project: Project, version: Version) -> dict:
    """The archive of one version, exactly as it was when it was recorded."""
    return artifacts.assemble(project, rows=rows_of(version), charter=(version.snapshot or {}).get("charter"))


def shipping(db: Session, project: Project, number: Optional[int] = None, live: bool = False) -> tuple[dict, Optional[Version]]:
    """(the archive, the version it is) for Deploy, Download and a push.

    A version named by number is that version. Otherwise: a finished build ships what
    it holds, which is its current version; a build that is being changed — running,
    waiting on a review, stuck, stopped — keeps shipping the version the change was
    made on, never the change half-made. `live` is what is on screen in a review.
    A build that never finished has no version, and ships what it has.
    """
    if number is not None:
        version = by_number(db, project, number)
        if version is None:
            raise LookupError(f"This build has no version {number}.")
        return assemble(project, version), version
    version = ensure_first(db, project)
    if live or version is None:
        return artifacts.assemble(project), (version if version is not None and matches_current(db, project, version) else None)
    if project.status == PipelineStatus.COMPLETED.value and matches_current(db, project, version):
        return artifacts.assemble(project), version
    return assemble(project, version), version


# ── restoring one ─────────────────────────────────────────────────────────────
def restore_rows(db: Session, project: Project, version: Version, note: str) -> dict[str, PhaseResult]:
    """Make the phases `version` saved the build's current attempts again.

    Each becomes the newest attempt of its phase — the row itself when nothing newer
    was written since, otherwise a copy — because the runner reads "the latest row of a
    phase" as "the attempt that counts". Every other live attempt is superseded, and a
    dead one (running with nothing driving it, or failed) is dropped. Returns the rows
    now current, by phase. The caller commits.
    """
    rows = artifacts._rows(project)  # noqa: SLF001 - the one query
    by_phase: dict[str, list[PhaseResult]] = {}
    for row in rows:
        by_phase.setdefault(row.phase, []).append(row)
    chosen: dict[str, PhaseResult] = {}
    for item in (version.snapshot or {}).get("phases") or []:
        phase = item.get("phase")
        if not phase:
            continue
        mine = by_phase.get(phase, [])
        newest = max(mine, key=lambda r: (r.created_at, r.id)) if mine else None
        row = db.get(PhaseResult, item.get("id")) if item.get("id") else None
        if row is not None and row.project_id == project.id and newest is not None and newest.id == row.id:
            row.status = PhaseStatus.APPROVED.value
        else:
            row = PhaseResult(
                project_id=project.id,
                phase=phase,
                agent=item.get("agent") or phase,
                status=PhaseStatus.APPROVED.value,
                feedback=note,
                completed_at=_now(),
                **{name: item.get(name) for name in SNAP_FIELDS if name not in ("phase", "agent")},
            )
            row.total_tokens = int(item.get("total_tokens") or 0)
            row.latency_ms = int(item.get("latency_ms") or 0)
            db.add(row)
        chosen[phase] = row
    dead = (PhaseStatus.RUNNING.value, PhaseStatus.FAILED.value)
    for row in rows:
        if chosen.get(row.phase) is row:
            continue
        if row.status in dead:
            db.delete(row)
        elif row.status != PhaseStatus.REJECTED.value:
            row.status = PhaseStatus.REJECTED.value
    db.flush()
    db.expire(project, ["phases"])
    return chosen


# ── comparing two ─────────────────────────────────────────────────────────────
def diff(before: dict, after: dict) -> dict:
    """What changed between two assembled builds, file by file.

    Code only: the phase documents are rewritten by every audit and estimate, and a
    diff full of them would bury the three files the change was about."""
    a = {f["path"]: f for f in before.get("files") or []}
    b = {f["path"]: f for f in after.get("files") or []}
    files: list[dict] = []
    budget = _TOTAL_LINES
    counts = {"added": 0, "changed": 0, "deleted": 0}
    for path in sorted(set(a) | set(b)):
        old = a.get(path, {}).get("content")
        new = b.get(path, {}).get("content")
        if old == new:
            continue
        if old is None:
            status = "added"
            lines = [f"+{line}" for line in (new or "").splitlines()]
        elif new is None:
            status = "deleted"
            lines = [f"-{line}" for line in (old or "").splitlines()]
        else:
            status = "changed"
            # The first two lines are the `---`/`+++` file headers. Only those: a removed
            # `-- comment` reads `--- comment`, and it is a line of the change.
            lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3))[2:]
        counts[status] += 1
        added = sum(1 for line in lines if line.startswith("+"))
        removed = sum(1 for line in lines if line.startswith("-"))
        room = max(min(_FILE_LINES, budget), 0)
        shown = lines[:room]
        budget -= len(shown)
        files.append(
            {
                "path": path,
                "status": status,
                "phase": (b.get(path) or a.get(path) or {}).get("phase"),
                "added": added,
                "removed": removed,
                "lines": shown,
                "truncated": len(shown) < len(lines),
            }
        )
    order = {"changed": 0, "added": 1, "deleted": 2}
    files.sort(key=lambda f: (order[f["status"]], f["path"]))
    return {"files": files, "counts": counts}


def phase_order_last() -> str:
    """The node a restored checkpoint is attributed to: the last, so the graph is done."""
    return PHASE_ORDER[-1].value
