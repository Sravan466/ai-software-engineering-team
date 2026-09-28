"""The crew's own fix loop: rounds, progress, and when to stop and ask for help.

A serious problem — a critical or high security finding, code that does not compile,
a phase that contradicts the stack it was built on — has one right answer, and asking
a person "send back or waive?" about it is asking them to do the crew's job. So the
crew fixes it, re-checks, and goes again. That loop needs two things to be safe:

  **A stopping rule based on progress.** Self-repair gains most in its first two or
  three attempts, and a round that fixed nothing will not be rescued by an identical
  one. The loop stops at `auto_fix_max_rounds`, or earlier when a round's re-check
  finds that nothing it was sent to fix went away.

  **No silent shipping.** When the loop stops with something still wrong, the build
  parks as "needs help" — never "complete" — and says what was tried in each round.

This module is bookkeeping over `Project.auto_fix` and nothing else: pure functions
over a JSON document, so the runner stays a loop and every rule here is testable
without a model. One *track* per kind of problem:

  * `security` — severe findings, fixed by rewinding to the phases that own them;
  * `build:<phase>` — a phase whose code does not compile or contradicts the stack,
    fixed by re-running that phase in place.

Each track holds its rounds (what was sent, to whom, how, and what the re-check found
fixed), how many rounds it is allowed, and — once it stops — why.
"""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.core.config import settings
from app.core.constants import BuildStatus

SECURITY = "security"
BUILD_PREFIX = "build:"

#: Why a loop stopped.
STOP_LIMIT = "limit"  # every round it was allowed has run
STOP_NO_PROGRESS = "no_progress"  # the last round fixed nothing

#: Why a serious finding may be waived. GitHub's own dismissal reasons, minus the
#: ones that do not apply to generated code: a waiver of something serious is a risk
#: decision, and the record says which kind.
WAIVE_KINDS = ("false_positive", "mitigated", "accepted_risk")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_track(phase: str) -> str:
    return f"{BUILD_PREFIX}{phase}"


def load(project) -> dict:
    """A private copy of the project's loop state, safe to mutate and save back.

    Copied because SQLAlchemy only notices a JSON column changed when it is assigned
    a new object — mutating the loaded dict in place is silently never written.
    """
    data = copy.deepcopy(project.auto_fix) if isinstance(project.auto_fix, dict) else {}
    data.setdefault("tracks", {})
    return data


def save(project, data: dict) -> None:
    project.auto_fix = copy.deepcopy(data)


def track(data: dict, name: str) -> dict:
    found = data["tracks"].setdefault(name, {})
    found.setdefault("allowed", max(int(settings.auto_fix_max_rounds), 0))
    found.setdefault("rounds", [])
    found.setdefault("stopped", None)
    found.setdefault("accepted", None)
    # Rounds before this index were judged when the loop last stopped; "Keep trying"
    # moves it forward so the round that stopped it cannot stop it again.
    found.setdefault("resumed_after", 0)
    return found


def open_round(t: dict) -> Optional[dict]:
    """The round sent off and not yet re-checked, if there is one."""
    rounds = t.get("rounds") or []
    last = rounds[-1] if rounds else None
    return last if last is not None and last.get("fixed") is None else None


def close_round(t: dict, remaining: Iterable[str]) -> Optional[list[str]]:
    """Record what the re-check found fixed. Returns those keys, or None if no round
    was waiting on a re-check."""
    last = open_round(t)
    if last is None:
        return None
    left = set(remaining)
    sent = [p["key"] for p in last.get("problems", [])]
    fixed = [k for k in sent if k not in left]
    last["fixed"] = fixed
    last["remaining"] = [k for k in sent if k in left]
    last["checked_at"] = _now()
    return fixed


def next_step(t: dict) -> str:
    """"fix" to run another round, or the reason to stop and ask for help."""
    rounds = t.get("rounds") or []
    judged = rounds[int(t.get("resumed_after") or 0) :]
    if judged and judged[-1].get("fixed") == []:
        return STOP_NO_PROGRESS
    if len(rounds) >= int(t.get("allowed") or 0):
        return STOP_LIMIT
    return "fix"


def start_round(
    t: dict, strategy: str, phases: list[str], problems: list[dict]
) -> dict:
    record = {
        "n": len(t["rounds"]) + 1,
        "strategy": strategy,
        "phases": phases,
        "problems": problems,
        "fixed": None,
        "at": _now(),
    }
    t["rounds"].append(record)
    t["stopped"] = None
    return record


def stop(t: dict, reason: str, left: int) -> None:
    t["stopped"] = {"reason": reason, "left": left, "at": _now()}


def stuck(data: dict) -> list[str]:
    """The tracks that stopped and are waiting for a person."""
    return [name for name, t in data["tracks"].items() if t.get("stopped") and not t.get("accepted")]


def keep_trying(data: dict, more: int) -> list[str]:
    """Give every stuck track `more` rounds, and forget why it stopped."""
    names = stuck(data)
    for name in names:
        t = data["tracks"][name]
        t["allowed"] = len(t["rounds"]) + max(int(more), 1)
        t["resumed_after"] = len(t["rounds"])
        t["stopped"] = None
    return names


def accept(data: dict, kind: str, reason: str) -> list[str]:
    """Continue past every stuck *build* track, with the reason on the record.

    Security tracks are not accepted wholesale: each severe finding is waived on its
    own, with its own reason, because "I accept this leaked credential" is a
    statement about one finding.
    """
    names = [n for n in stuck(data) if n.startswith(BUILD_PREFIX)]
    for name in names:
        data["tracks"][name]["accepted"] = {"kind": kind, "reason": reason, "at": _now()}
    return names


def accepted(data: dict, name: str) -> bool:
    t = data["tracks"].get(name)
    return bool(t and t.get("accepted"))


# ── compile errors and stack contradictions, as problems with keys ───────────
def _key(*parts: str) -> str:
    basis = "|".join(re.sub(r"\s+", " ", p.strip().lower()) for p in parts)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def code_problems(row) -> list[dict]:
    """Everything serious about one phase's code, each with a key that survives a
    rebuild: the file and the error, without the line number that moves."""
    problems: list[dict] = []
    for note in row.stack_note or []:
        text = str(note).strip()
        if text:
            problems.append(
                {"key": _key("stack", text), "title": text.replace("`", ""), "kind": "stack",
                 "where": None, "phase": row.phase}
            )
    if row.build_status == BuildStatus.FAILED.value:
        for p in row.build_note or []:
            if not isinstance(p, dict):
                continue
            message = str(p.get("message") or "does not compile")
            path = str(p.get("path") or "")
            line = p.get("line")
            problems.append(
                {
                    "key": _key("build", path, re.sub(r"\d+", "#", message)),
                    "title": message.replace("`", ""),
                    "kind": "build",
                    "where": f"{path}:{line}" if path and line else (path or None),
                    "phase": row.phase,
                }
            )
    # One entry per key: the same error in the same file twice is one problem.
    return list({p["key"]: p for p in problems}.values())


CODE_NOTE_PREFIX = "Your last deliverable still has problems"


def code_note(problems: list[dict], round_number: int) -> str:
    """The note a phase is re-run with to fix its own compile or stack problems."""
    lines = []
    for p in problems:
        where = f" in `{p['where']}`" if p.get("where") else ""
        label = "contradicts the stack" if p["kind"] == "stack" else "does not compile"
        lines.append(f"- ({label}){where}: {p['title']}")
    retry = (
        f"\n\nThis is fix round {round_number}. The previous attempt did not fix these. "
        "Do not repeat it: change the files named above, and check that every import "
        "resolves and every file parses before you return them."
        if round_number > 1
        else ""
    )
    return (
        f"{CODE_NOTE_PREFIX}. Fix every one and return the complete deliverable:\n"
        + "\n".join(lines)
        + retry
        + "\n\nKeep the stack the architecture froze — the fix is to the code, not to "
        "the technology choices."
    )


__all__ = [
    "SECURITY",
    "STOP_LIMIT",
    "STOP_NO_PROGRESS",
    "WAIVE_KINDS",
    "accept",
    "accepted",
    "build_track",
    "close_round",
    "code_note",
    "code_problems",
    "keep_trying",
    "load",
    "next_step",
    "open_round",
    "save",
    "start_round",
    "stop",
    "stuck",
    "track",
]
