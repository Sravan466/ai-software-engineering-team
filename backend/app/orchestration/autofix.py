"""The crew's own fix loop: rounds, progress, and when to stop and ask for help.

A serious problem — a critical or high security finding, code that does not compile,
a phase that contradicts the stack it was built on — has one right answer, and asking
a person "send back or waive?" about it is asking them to do the crew's job. So the
crew fixes it, re-checks, and goes again. That loop needs two things to be safe:

  **A stopping rule based on progress.** Self-repair gains most in its first two or
  three attempts, and a round that fixed nothing will not be rescued by an identical
  one. The loop stops at `auto_fix_max_rounds`, or earlier when a round's re-check
  finds that nothing it was sent to fix went away. On code (#103) progress is counted
  net: a round that fixed 3 problems and made 6 new ones went backwards, and the loop
  stops there and puts back the attempt with the fewest problems.

  **No silent shipping.** When the loop stops with something still wrong, the build
  parks as "needs help" — never "complete" — and says what was tried in each round.

This module is bookkeeping over `Project.auto_fix` and nothing else: pure functions
over a JSON document, so the runner stays a loop and every rule here is testable
without a model. One *track* per kind of problem:

  * `security` — severe findings, fixed by rewinding to the phases that own them;
  * `build:<phase>` — a phase whose code does not compile or contradicts the stack,
    fixed by re-running that phase in place;
  * `tests` — QA's tests that ran and failed (#76). Each failure goes first to the
    engineer who owns the code it tests, with QA's suite kept as it is; one the fix
    didn't turn green goes to QA next, to decide whether the test or the code is wrong.

Each track holds its rounds (what was sent, to whom, how, and what the re-check found
fixed), how many rounds it is allowed, and — once it stops — why.
"""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.core.artifacts import NOTE_KIND
from app.core.config import settings
from app.core.constants import BuildStatus, Phase, TestStatus

SECURITY = "security"
BUILD_PREFIX = "build:"
TESTS = "tests"

#: Why a loop stopped.
STOP_LIMIT = "limit"  # every round it was allowed has run
STOP_NO_PROGRESS = "no_progress"  # the last round fixed nothing
#: The re-check couldn't run at all (#77): the scanners had no sandbox, so nothing the
#: round was sent can be called fixed or not — and another round wouldn't be either.
STOP_UNCHECKED = "unchecked"

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
    # Where the current episode began: a fresh problem gets a fresh budget and starts
    # again from the first approach. Keep trying does *not* move it, so the rounds it
    # buys carry on escalating instead of repeating the approach that already failed.
    found.setdefault("episode_start", found["resumed_after"])
    return found


def open_round(t: dict) -> Optional[dict]:
    """The round sent off and not yet re-checked, if there is one."""
    rounds = t.get("rounds") or []
    last = rounds[-1] if rounds else None
    return last if last is not None and last.get("fixed") is None else None


def close_round(
    t: dict, remaining: Iterable[str], attempt: Optional[str] = None, net: bool = False
) -> Optional[list[str]]:
    """Record what the re-check found fixed. Returns those keys, or None if no round
    was waiting on a re-check.

    `net` (#103, code rounds): also every problem it found now, new ones included, and
    `attempt`, the attempt the re-check read — so the trend is real and the best
    attempt can be put back. Security and test rounds keep their own judgement."""
    last = open_round(t)
    if last is None:
        return None
    left = list(dict.fromkeys(remaining))
    now = set(left)
    sent = [p["key"] for p in last.get("problems", [])]
    was = set(sent)
    fixed = [k for k in sent if k not in now]
    last["fixed"] = fixed
    last["remaining"] = [k for k in sent if k in now]
    if net:
        # Net, not just what went away: "fixed 3, broke 6" is a round that made it worse.
        last["after"] = len(left)
        last["broke"] = len([k for k in left if k not in was])
        if attempt:
            last["result"] = attempt
    last["checked_at"] = _now()
    return fixed


def next_step(t: dict, net: bool = False) -> str:
    """"fix" to run another round, or the reason to stop and ask for help.

    `net` (#103, code): a round that didn't leave fewer problems than it was sent made
    no progress, whatever it fixed on the way."""
    rounds = t.get("rounds") or []
    judged = rounds[int(t.get("resumed_after") or 0) :]
    # A round that only asked the code's owners to fix failing tests hands what it
    # didn't fix to QA, rather than stopping: "the code is right, the test is wrong"
    # is the other half of the question, and nobody has been asked it yet (#76).
    # Nor does a round whose re-check couldn't run at all (#76): it tried nothing that
    # anyone could judge, so it says nothing about progress — the limit still holds.
    last = judged[-1] if judged else None
    # Nor does a code round that sent the model nothing (#103): every file it had to fix
    # was too long to show whole, so it tried nothing — a later round on a larger model
    # still might. The limit holds.
    if (
        last and last.get("fixed") == [] and not last.get("handover") and not last.get("unjudged_all")
        and not last.get("nothing_sent")
    ):
        return STOP_NO_PROGRESS
    if (
        net and last and last.get("after") is not None and not last.get("nothing_sent")
        and int(last["after"]) >= len(last.get("problems") or [])
    ):
        return STOP_NO_PROGRESS
    if len(rounds) >= int(t.get("allowed") or 0):
        return STOP_LIMIT
    return "fix"


def start_round(
    t: dict, strategy: str, phases: list[str], problems: list[dict], attempt: Optional[str] = None,
    from_round: Optional[int] = None,
) -> dict:
    record = {
        "n": len(t["rounds"]) + 1,
        "strategy": strategy,
        "phases": phases,
        "problems": problems,
        "fixed": None,
        "at": _now(),
        # Which episode it belongs to (#103): `settle` moves the start past it once the
        # problems are gone, and the record still says which rounds went together.
        "episode": int(t.get("episode_start") or 0),
    }
    if attempt:
        # The attempt the round was sent, with every one of `problems` (#103), and which
        # round of the episode made it — not always the one before: the loop may have put
        # an earlier one back.
        record["attempt"] = attempt
        record["from_round"] = from_round if from_round is not None else max(record["n"] - 1 - int(t.get("episode_start") or 0), 0)
    t["rounds"].append(record)
    t["stopped"] = None
    return record


def stop(t: dict, reason: str, keys: list[str], problems: Optional[list[dict]] = None) -> None:
    t["stopped"] = {"reason": reason, "left": len(keys), "keys": list(keys), "at": _now()}
    if problems is not None:
        # What is left, new problems included (#103): the last round's own list only
        # holds the ones it was sent.
        t["stopped"]["problems"] = problems


def episode(t: dict) -> list[dict]:
    """The latest episode's rounds — still together after `settle` moved the start on."""
    rounds = t.get("rounds") or []
    if not rounds:
        return []
    latest = rounds[-1].get("episode")
    start = int(t.get("episode_start") or 0)
    # Rounds from before episodes were recorded belong to it when they are past its
    # start: a build from before #103 that was sent round after round, and kept trying.
    return [
        r for i, r in enumerate(rounds)
        if (latest is not None and r.get("episode") == latest) or (r.get("episode") is None and i >= start)
    ]


def _carried_on(t: dict, i: int) -> bool:
    """Whether round i+1 carried on round i's problems, rather than opening an episode."""
    rounds = t["rounds"]
    r, nxt = rounds[i], rounds[i + 1]
    if r.get("episode") is not None and nxt.get("episode") is not None:
        return r["episode"] == nxt["episode"]
    # Recorded before rounds said their episode: the current one is known, and a round
    # that left some of its own problems was carried on whatever came next.
    return i >= int(t.get("episode_start") or 0) or bool(r.get("remaining"))


def after_of(t: dict, i: int) -> Optional[int]:
    """What code round i left, new problems included (#103). Recorded since #103; for a
    round before that, read off the round after it — each was sent every problem the
    attempt before it had — or off how the loop ended."""
    rounds = t.get("rounds") or []
    r = rounds[i]
    if r.get("fixed") is None:
        return None
    if r.get("after") is not None:
        return int(r["after"])
    if i + 1 < len(rounds):
        return len(rounds[i + 1].get("problems") or []) if _carried_on(t, i) else None
    if t.get("stopped"):
        return int(t["stopped"].get("left") or 0)
    if t.get("accepted"):
        return len(t["accepted"].get("keys") or [])
    return 0 if not r.get("remaining") else None


def trend(t: dict) -> list[dict]:
    """The episode's problem counts (#103), one point per attempt: what its first round
    was sent, then what each round left — 3 → 6 → 4 is a loop going nowhere. A round
    sent an attempt the loop put back, not the one the round before it made, starts
    from it again (`back`). Each point says which attempt it is and which round made
    it (0: none)."""
    rounds = t.get("rounds") or []
    current = episode(t)
    if not current:
        return []
    first = rounds.index(current[0])
    base = int(current[0].get("n") or 1) - 1
    out: list[dict] = []
    made: Optional[str] = None
    for i in range(first, first + len(current)):
        r = rounds[i]
        sent = len(r.get("problems") or [])
        if not out:
            out.append({"n": sent, "attempt": r.get("attempt"), "round": int(r.get("from_round") or 0), "back": False})
        elif r.get("attempt") and made and r["attempt"] != made:
            out.append({"n": sent, "attempt": r["attempt"], "round": int(r.get("from_round") or 0), "back": True})
        after = after_of(t, i)
        if after is None:
            break
        out.append({"n": after, "attempt": r.get("result"), "round": int(r.get("n") or 0) - base, "back": False})
        made = r.get("result")
    return out if len(out) > 1 else []


def best_attempt(t: dict, points: Optional[list[dict]] = None) -> Optional[dict]:
    """The episode's attempt with the fewest problems, when it isn't the latest (#103):
    `{"attempt", "problems", "round", "instead_of"}` — `round` 0 is the attempt before
    any round. A tie goes to the attempt itself over a copy the loop put back, then to
    the later one."""
    seen = [p for p in (points if points is not None else trend(t)) if p.get("attempt")]
    if not seen:
        return None
    latest = seen[-1]
    best = min(enumerate(seen), key=lambda ip: (ip[1]["n"], ip[1]["back"], -ip[0]))[1]
    if best["n"] >= latest["n"] or best["attempt"] == latest["attempt"]:
        return None
    return {"attempt": best["attempt"], "problems": best["n"], "round": best["round"], "instead_of": latest["n"]}


def view(data: object) -> object:
    """The loop's record as the page reads it (#103): each code track with its trend,
    and each of its rounds with what it left and broke — worked out here, once, for
    records from before those were kept too. Nothing is written back."""
    if not isinstance(data, dict) or not isinstance(data.get("tracks"), dict):
        return data
    out = copy.deepcopy(data)
    for name, t in out["tracks"].items():
        if not name.startswith(BUILD_PREFIX) or not isinstance(t, dict):
            continue
        for i, r in enumerate(t.get("rounds") or []):
            if r.get("after") is None and r.get("fixed") is not None:
                after = after_of(t, i)
                if after is not None:
                    r["after"] = after
                    r["broke"] = max(after - len(r.get("remaining") or []), 0)
        t["trend"] = trend(t)
    return out


def platform_fixed(row) -> dict:
    """What the platform mended itself on one attempt, before any model round (#103):
    `{"paths": n, "imports": n}`, from the attempt's `handoff.wiring`. Empty for none."""
    out: dict[str, int] = {}
    for fix in ((getattr(row, "handoff", None) or {}).get("wiring") or []):
        kind = "paths" if fix.get("kind") == "path" else "imports"
        out[kind] = out.get(kind, 0) + 1
    return out


def said(points: list[dict]) -> str:
    """A trend in words: `3 → 6 → back to 3 → 5`."""
    return " → ".join(("back to " if p.get("back") else "") + str(p["n"]) for p in points)


def abandon_open_round(t: dict) -> bool:
    """Forget a round whose fix never generated — a provider error, a disconnected
    computer, a Stop. Judging it later would call it "fixed nothing" when nothing
    was ever tried."""
    if open_round(t) is None:
        return False
    record = t["rounds"].pop()
    # What the person asked of it (#103) waits for the round that does run.
    if record.get("note") and not t.get("note"):
        t["note"] = record["note"]
    if record.get("asked_model") and not t.get("one_off"):
        t["one_off"] = record["asked_model"]
    return True


def settle(t: dict) -> None:
    """The track's problems are all gone: the next problem is a new episode, with
    its own budget, and its own "did the last round help?"."""
    # What the person asked for this episode (#103) — a note, a model they agreed to
    # send its code to — is not a standing order for the next one.
    t.pop("note", None)
    t.pop("decision", None)
    t.pop("one_off", None)
    if t.get("episode_start") == len(t["rounds"]) and t.get("allowed") == len(t["rounds"]) + max(
        int(settings.auto_fix_max_rounds), 0
    ):
        return
    t["resumed_after"] = len(t["rounds"])
    t["episode_start"] = len(t["rounds"])
    t["allowed"] = len(t["rounds"]) + max(int(settings.auto_fix_max_rounds), 0)
    t["stopped"] = None
    t.pop("kept", None)  # an attempt put back belongs to the episode that put it back


def stuck(data: dict) -> list[str]:
    """The tracks that stopped and are waiting for a person."""
    return [name for name, t in data["tracks"].items() if t.get("stopped") and not t.get("accepted")]


def keep_trying(
    data: dict, more: int, note: Optional[str] = None, model: Optional[str] = None, decided: bool = False
) -> list[str]:
    """Give every stuck track `more` rounds, and forget why it stopped. `note` (#103) is
    what the person told the crew to try, for each track's next round; `model` a model
    they chose for the next round of each code track it was offered for, once. `decided`
    (#102): the note is a decision about the app ("skip voice input"), and the round may
    remove what it drops instead of fixing it."""
    names = stuck(data)
    for name in names:
        t = data["tracks"][name]
        offered = (t.get("stopped") or {}).get("cloud")
        t["allowed"] = len(t["rounds"]) + max(int(more), 1)
        t["resumed_after"] = len(t["rounds"])
        t["stopped"] = None
        if note and note.strip():
            t["note"] = note.strip()
            if decided:
                # Stands for the rest of the episode: every round in it is told (#102).
                t["decision"] = note.strip()
        if model and name.startswith(BUILD_PREFIX) and model == offered:
            t["one_off"] = model
    return names


def accept(data: dict, kind: str, reason: str) -> list[str]:
    # Only the problems on screen are accepted: a later rebuild that breaks the same
    # phase differently is a new problem, and goes through the loop like any other.
    """Continue past every stuck *build* track, with the reason on the record.

    Security tracks are not accepted wholesale: each severe finding is waived on its
    own, with its own reason, because "I accept this leaked credential" is a
    statement about one finding.
    """
    names = [n for n in stuck(data) if accepts_wholesale(n)]
    for name in names:
        t = data["tracks"][name]
        keys = list((t.get("stopped") or {}).get("keys") or [])
        t["accepted"] = {"kind": kind, "reason": reason, "keys": keys, "at": _now()}
    return names


def accepts_wholesale(name: str) -> bool:
    """Tracks a person can move past in one decision: code problems and failing tests."""
    return name.startswith(BUILD_PREFIX) or name == TESTS


def accept_tests(data: dict, kind: str, reason: str, keys: list[str]) -> None:
    """Ship past the failing tests now on screen, with the reason on the record (#76).

    Recorded on the `tests` track whether or not the loop ever ran on it — a red suite
    met at the Ship review is waived the same way as one the crew gave up on — and for
    exactly these failures: a different test failing later is a new problem."""
    t = track(data, TESTS)
    before = list((t.get("accepted") or {}).get("keys") or [])
    t["accepted"] = {"kind": kind, "reason": reason, "keys": list(dict.fromkeys(before + list(keys))), "at": _now()}


def accepted(data: dict, name: str) -> bool:
    t = data["tracks"].get(name)
    return bool(t and t.get("accepted"))


def covers(t: dict, keys: Iterable[str]) -> bool:
    """Whether a person accepted exactly these problems (or a subset of them)."""
    acc = t.get("accepted") or {}
    return bool(acc) and set(keys) <= set(acc.get("keys") or [])


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
            if not isinstance(p, dict) or p.get("kind") == NOTE_KIND:
                continue

            problems.append(_build_problem(p, row.phase))
    # One entry per key: the same error in the same file twice is one problem.
    return list({p["key"]: p for p in problems}.values())


def _build_problem(p: dict, phase: str) -> dict:
    """One compile or build problem with its key: the file and the error, without the
    numbers that move (a line, a count, a duration)."""
    message = str(p.get("message") or "does not compile")
    path = str(p.get("path") or "")
    line = p.get("line")
    # A problem no line of output pinned down carries the build's last lines under its
    # first (#75). Keyed on that first line and the first error among them, not the
    # whole tail: chunk hashes and timings change every round while the error doesn't,
    # and two different errors still get two keys.
    head, _, tail = message.partition("\n")
    cause = next((l.strip() for l in tail.splitlines() if re.search(r"error", l, re.IGNORECASE)), "")
    # A one-line problem keys exactly as it always did, so a fix round already open
    # when this shipped still recognises its problems.
    extra = [re.sub(r"[0-9a-f]{6,}|\d+", "#", cause)] if cause else []
    out = {
        "key": _key("build", path, re.sub(r"\d+", "#", head), *extra),
        "title": message.replace("`", ""),
        "kind": "build",
        "where": f"{path}:{line}" if path and line else (path or None),
        "phase": phase,
    }
    # Which real step found it (#75): install | build | boot | vercel. The parser's own
    # problems have none.
    if p.get("step"):
        out["step"] = str(p["step"])
    # Code that parses and isn't finished (#101): said as that, not "does not compile".
    if p.get("kind") in _UNFINISHED:
        out["problem"] = str(p["kind"])
    return out


def fix_scope(row) -> list[dict]:
    """The problems `code_problems` keys, as a fix round reads them (#103): the file,
    the line, the checker's own kind and its words — what `fixscope` finds the files
    to edit from."""
    from app.build.fixscope import stack_path

    out: list[dict] = []
    for note in row.stack_note or []:
        text = str(note).strip()
        if text:
            out.append({"path": stack_path(text), "line": None, "kind": "stack", "message": text})
    if row.build_status == BuildStatus.FAILED.value:
        for p in row.build_note or []:
            if not isinstance(p, dict) or p.get("kind") == NOTE_KIND:
                continue
            message = str(p.get("message") or "does not compile")
            # Said as what it is, as the round's note says it: unfinished, or what a
            # real build step it broke — "does not parse" already says itself.
            label = _UNFINISHED.get(str(p.get("kind") or "")) or _BROKE.get(str(p.get("step") or ""))
            out.append({
                "path": p.get("path") or None,
                "line": p.get("line"),
                "kind": p.get("kind") or "build",
                "message": f"({label}) {message}" if label else message,
                "step": p.get("step"),
            })
    # One entry per problem, as `code_problems` keys them: the same error twice is one.
    return list({(p["path"], p["message"]): p for p in out}.values())


def deploy_problems(problems: Iterable[dict], phase: str) -> list[dict]:
    """A failed Vercel build's problems, keyed like the sandbox's own (#75)."""
    found = [_build_problem({**p, "step": "vercel"}, phase) for p in problems if isinstance(p, dict)]
    return list({p["key"]: p for p in found}.values())


#: What the compile gate's #101 kinds are called in a fix note.
_UNFINISHED = {"placeholder": "is unfinished", "weak_test": "tests nothing"}

#: How the fix note names what a problem broke, by the step that found it.
_BROKE = {
    "install": "does not install",
    "build": "fails the build",
    "boot": "crashes when it starts",
    "vercel": "failed the build on Vercel",
    "test": "can't run as a test",
}


# ── failing tests (#76) ──────────────────────────────────────────────────────
_SIDE_OWNER = {
    "backend": Phase.BACKEND_ENGINEER.value,
    "frontend": Phase.FRONTEND_ENGINEER.value,
}
QA = Phase.QA_ENGINEER.value


def test_failures(run: object, unrunnable: bool = False) -> list[dict]:
    """Every test in QA's latest run (`PhaseResult.test_run`) that ran and failed,
    keyed by file and name.

    The key leaves out the message and the line, which change as the code does: a
    test still failing for a different reason after a fix is still that test failing.

    `unrunnable` adds one entry per side whose suite failed without running anything
    and without a problem anyone was sent (no runner set up for it). Nobody in the
    crew can fix that, so the loop never sees it — but it is never a pass either, so
    the Ship review does, and it ships only with a waiver.
    """
    if not isinstance(run, dict) or run.get("status") != TestStatus.FAILED.value:
        return []
    out: dict[str, dict] = {}
    for side_run in run.get("runs") or []:
        side = str(side_run.get("side") or "")
        if (
            unrunnable
            and side_run.get("status") == TestStatus.FAILED.value
            and not side_run.get("total")
            and not side_run.get("problems")
        ):
            reason = str(side_run.get("reason") or "The suite couldn't run.")
            key = _key("suite", side, reason)
            out[key] = {"key": key, "title": f"The {side} tests couldn't run\n{reason}", "kind": "test",
                        "where": None, "phase": QA, "test": f"{side} suite", "failure": "unrunnable",
                        "owner": QA, "side": side, "path": ""}
        for f in side_run.get("failures") or []:
            if not isinstance(f, dict):
                continue
            path, name = str(f.get("path") or ""), str(f.get("name") or "a test")
            message = str(f.get("message") or "failed").strip()
            key = _key("test", path, name)
            out[key] = {
                "key": key,
                "title": f"{name}\n{message}",
                "kind": "test",
                "where": f"{path}:{f['line']}" if path and f.get("line") else (path or None),
                "phase": QA,
                "test": name,
                "failure": str(f.get("kind") or "assertion"),
                "owner": _SIDE_OWNER.get(side, QA),
                "framework": side_run.get("framework"),
                "side": side,
                "path": path,
            }
    return list(out.values())


def judged(problem: dict, run: object) -> bool:
    """Whether `run` can say anything about this failing test: its side's suite ran,
    its file loaded, and the report wasn't cut short. A test the run never reached is
    not fixed — nobody knows. A test that no longer exists (QA deleted the file, or
    every test on its side) is judged: the suite says nothing failed because nothing
    is there, and that was QA's call to make."""
    if not isinstance(run, dict):
        return False
    path = str(problem.get("path") or "")
    if not path and problem.get("where"):
        path = str(problem["where"]).rsplit(":", 1)[0]
    # A round from before tests had sides recorded: the side is the path's first folder.
    side_name = problem.get("side") or (path.split("/", 1)[0] if "/" in path else None)
    runs = run.get("runs") or []
    if not runs:
        return bool(run.get("no_tests"))
    side = next((r for r in runs if r.get("side") == side_name), None)
    if side is None:
        return True  # no tests on this side any more
    files = side.get("files") or []
    if files and path and path not in files:
        return True  # its file is gone
    if side.get("status") not in (TestStatus.OK.value, TestStatus.FAILED.value) or not side.get("total"):
        return False
    if side.get("truncated"):
        return False
    broken = {str(p.get("path") or "") for p in side.get("problems") or [] if isinstance(p, dict)}
    return path not in broken


def test_problems(t: dict, run: object) -> tuple[list[dict], list[dict]]:
    """(the failing tests to deal with, those carried from the last round unjudged).

    What `run` reports failing, plus every test the current episode's last round was
    sent that `run` couldn't judge — a side that didn't run (the registry was down), a
    file that wouldn't load against the fix. Those stay failing until a run says
    otherwise: a re-check that couldn't run is never a fix.
    """
    current = test_failures(run)
    keys = {p["key"] for p in current}
    rounds = t.get("rounds") or []
    last = rounds[-1] if len(rounds) > int(t.get("episode_start") or 0) else None
    if last is None:
        return current, []
    pending = (last.get("problems") or []) if last.get("fixed") is None else (last.get("carried") or [])
    carried = [p for p in pending if p.get("key") not in keys and not judged(p, run)]
    return current + carried, carried


def unwaived_tests(data: dict, run: object) -> list[dict]:
    """The failing tests in `run` a person hasn't waived — the one rule that holds the
    Ship review, read by the gate, the approve route and the page alike. A side whose
    suite couldn't run at all counts too: never a pass, so never shipped silently."""
    failures = test_failures(run, unrunnable=True)
    t = data["tracks"].get(TESTS)
    # Per failure: what a person waived stays waived, and only what they never saw
    # (a new failure, a suite that stopped running) holds the review again.
    waived = set(((t or {}).get("accepted") or {}).get("keys") or [])
    return [f for f in failures if f["key"] not in waived]


def judge_tests_round(t: dict, run: object, reason: Optional[str] = None) -> Optional[list[str]]:
    """Close the open tests round against `run` — the suite it kept, before anything
    rewrites it. Tests `run` couldn't reach are carried as still failing, with why.
    Returns the keys fixed, or None if no round was waiting."""
    last = open_round(t)
    if last is None:
        return None
    problems, carried = test_problems(t, run)
    fixed = close_round(t, [p["key"] for p in problems])
    if carried:
        last["carried"] = carried
        mark_unjudged(last, [p.get("key") for p in carried], reason or "Some tests couldn't run again to check the fix.")
    return fixed


def mark_unjudged(record: dict, keys: Iterable[str], reason: str) -> None:
    """Say on a round that its re-check couldn't reach `keys`, and why — and whether it
    reached nothing it was sent at all, which `next_step` and the help note read: a
    round nobody could judge says nothing about progress. Shared by the tests track
    (#76) and the security track (#77), so the two can't drift apart."""
    record["unjudged"] = reason
    sent = {p.get("key") for p in record.get("problems") or []}
    record["unjudged_all"] = sent <= set(keys)


def route_tests(t: dict, problems: list[dict]) -> list[dict]:
    """Who each failing test goes to this round.

    The owner of the code first — the test is QA's reading of the spec, and the code
    is what it is checking. A failure the owner already had a turn at goes to QA, to
    decide which of the two is wrong. A test that broke on its own scaffolding (a
    fixture, the network the sandbox doesn't have) is QA's from the start.
    """
    last_to: dict[str, str] = {}
    for r in (t.get("rounds") or [])[int(t.get("episode_start") or 0):]:
        unseen = {p.get("key") for p in r.get("carried") or []}
        for p in r.get("problems") or []:
            # A turn whose result nobody could see isn't a turn: the same phase goes again.
            if p.get("key") not in unseen:
                last_to[p.get("key")] = p.get("phase")
    routed = []
    for p in problems:
        owner = p.get("owner") or QA
        if p.get("failure") in ("environment", "error") or last_to.get(p["key"]) == owner:
            dest = QA
        else:
            dest = owner
        routed.append({**p, "phase": dest})
    return routed


TEST_NOTE_PREFIX = "Some of QA's tests fail"


def test_note(problems: list[dict], phase: str, round_number: int, bodies: Optional[dict] = None,
              standing: Optional[str] = None) -> str:
    """The note one phase is re-run with to deal with failing tests.

    An engineer is told to fix the code, with the assertion and the test's body — the
    suite is kept as it is and run again against the fix. QA is told the owner already
    had a turn, and asked to decide: fix a test that expects the wrong thing, or keep
    one that's right and say why.
    """
    lines = []
    for p in problems:
        where = f" (`{p['where']}`)" if p.get("where") else ""
        head, _, message = p["title"].partition("\n")
        lines.append(f"- {head}{where}: {' '.join(message.split())[:400]}")
        body = (bodies or {}).get(p["key"])
        if body:
            lines.append("\n".join(f"    {line}" for line in body.splitlines()))
    if phase == QA:
        env = [p for p in problems if p.get("failure") == "environment"]
        lead = (
            f"{TEST_NOTE_PREFIX}, and they are yours to look at. "
            + ("Where the engineer already tried to fix the code and the test still fails, decide which "
               "is wrong: if the test expects something the spec doesn't ask for, fix the test; if the "
               "code is wrong, keep the test and say so in `risks`. " if len(env) < len(problems) else "")
            + ("Tests run with no network and no database server: mock what they reach for. " if env else "")
            + "Return the complete deliverable, every test file included:"
        )
    else:
        lead = (
            f"{TEST_NOTE_PREFIX} against your code. Fix the code so they pass — the tests stay as they "
            "are and are run again against your fix. Return the complete deliverable:"
        )
    retry = (
        f"\n\nThis is fix round {round_number}. Don't repeat what the last round tried."
        if round_number > 1 else ""
    )
    carried = (
        "\n\nThis phase is also in the middle of a security fix. Keep every one of those fixes:\n\n" + standing
        if standing else ""
    )
    return lead + "\n" + "\n".join(lines) + retry + carried


CODE_NOTE_PREFIX = "Your last deliverable still has problems"


def code_note(
    problems: list[dict],
    round_number: int,
    snippets: Optional[dict] = None,
    standing: Optional[str] = None,
    person: Optional[str] = None,
) -> str:
    """The note a phase is re-run with to fix its own compile or stack problems.

    `snippets` is the code each problem is about, from the second round on.
    `standing` is a security fix note this phase was already working to: a compile
    fix that forgot it would bring the finding straight back. `person` is what the
    person told the crew to try (#103), in their words.
    """
    lines = []
    for p in problems:
        where = f" in `{p['where']}`" if p.get("where") else ""
        label = (
            "contradicts the stack"
            if p["kind"] == "stack"
            else _UNFINISHED.get(p.get("problem") or "") or _BROKE.get(p.get("step") or "", "does not compile")
        )
        lines.append(f"- ({label}){where}: {p['title']}")
        code = (snippets or {}).get(p["key"])
        if code:
            lines.append("\n".join(f"    {line}" for line in code.splitlines()))
    retry = (
        f"\n\nThis is fix round {round_number}. The previous attempt did not fix these. "
        "Do not repeat it: change the files named above, and check that every import "
        "resolves and every file parses before you return them."
        if round_number > 1
        else ""
    )
    carried = (
        "\n\nThis phase is also in the middle of a security fix. Keep every one of those "
        "fixes while you fix the above:\n\n" + standing
        if standing
        else ""
    )
    return with_person(
        f"{CODE_NOTE_PREFIX}. Fix every one and return the complete deliverable:\n"
        + "\n".join(lines)
        + retry
        + carried
        + "\n\nKeep the stack the architecture froze — the fix is to the code, not to "
        "the technology choices.",
        person,
    )


#: Said to a fix round beside a note that is a decision about the app (#102).
DECIDED = (
    "This is the person's decision about the app, not just a hint: where a problem is in "
    "something it drops, remove that code (and what uses it) instead of fixing it."
)


def take_person(t: dict) -> tuple[Optional[str], Optional[str], bool]:
    """What the person told this round: their words as the round records them (taken off
    the track — this round's only), as its prompt carries them, and whether a decision
    stands. A decision (#102) stays on the track for the episode, so every round in it is
    told it and what it allows — the first in the person's words, the rest as the decision."""
    said = t.pop("note", None)
    decision = t.get("decision")
    if decision:
        return said, f"{said or f'The person decided: {decision}'}\n{DECIDED}", True
    return said, said, False


def mark_round(record: dict, said: Optional[str], decided: bool) -> None:
    """Keep the person's words on the round, and whether they were a decision."""
    if said:
        record["note"] = said
        if decided:
            record["decided"] = True


def said_by_person(note: Optional[str]) -> str:
    """What the person told the crew to try (#103), in one line."""
    text = " ".join(str(note or "").split())
    return f"The person says: {text}" if text else ""


def with_person(note: str, person: Optional[str]) -> str:
    """`note` with the person's words on the line after its first (#103): near the top,
    where clipping a long note to the window never reaches, and after the line the
    note is recognised by."""
    said = said_by_person(person)
    if not said:
        return note
    head, sep, rest = note.partition("\n")
    return f"{head}\n{said}\n\n{rest}" if sep else f"{note}\n\n{said}"


__all__ = [
    "SECURITY",
    "TESTS",
    "accept_tests",
    "accepts_wholesale",
    "route_tests",
    "test_failures",
    "test_note",
    "judge_tests_round",
    "mark_unjudged",
    "judged",
    "test_problems",
    "unwaived_tests",
    "STOP_LIMIT",
    "STOP_NO_PROGRESS",
    "STOP_UNCHECKED",
    "WAIVE_KINDS",
    "accept",
    "accepted",
    "best_attempt",
    "build_track",
    "close_round",
    "code_note",
    "code_problems",
    "deploy_problems",
    "fix_scope",
    "keep_trying",
    "load",
    "next_step",
    "open_round",
    "save",
    "start_round",
    "stop",
    "stuck",
    "after_of",
    "episode",
    "platform_fixed",
    "said",
    "said_by_person",
    "take_person",
    "mark_round",
    "DECIDED",
    "with_person",
    "view",
    "track",
    "trend",
]
