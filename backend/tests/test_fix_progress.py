"""Fix rounds count real progress, and the build keeps its best attempt (#103).

A round used to be judged by which of the problems it was sent went away, so a round
that fixed 3 and broke 6 read "Fixed 3 of 3" and the loop went on. Now each round
records every problem its re-check found, the loop stops on the first round that
doesn't leave fewer than it started with, and the attempt with the fewest problems is
the one the build is parked on.
"""
from __future__ import annotations

import json

from app.orchestration import autofix
from tests.conftest import _fake_complete, asked_files, fenced, stub, through_question_gates

MAIN = "from app.services import total\n\n\ndef main():\n    return total([1, 2])\n"
REAL = "def total(items):\n    return sum(items)\n"
WEAK = "from app.services import total\n\n\ndef test_total{n}():\n    total([1, 2])\n"


def _round(t: dict, sent: list[str], left: list[str], attempt: str, result: str, from_round=None) -> None:
    autofix.start_round(t, "guided", ["backend_engineer"], [{"key": k} for k in sent], attempt=attempt, from_round=from_round)
    autofix.close_round(t, left, attempt=result, net=True)


def _counts(t: dict) -> list:
    return [("back " if p["back"] else "") + str(p["n"]) for p in autofix.trend(t)]


# ── the record ───────────────────────────────────────────────────────────────
def test_a_round_that_fixed_some_and_broke_more_is_worse_and_stops_the_loop():
    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    _round(t, ["a", "b", "c"], ["d", "e", "f", "g", "h", "i"], "r0", "r1")
    last = t["rounds"][-1]
    assert (last["fixed"], last["after"], last["broke"]) == (["a", "b", "c"], 6, 6)
    # Judged by what went away, it fixed all three; judged net, it went backwards.
    assert autofix.next_step(t) == "fix"
    assert autofix.next_step(t, net=True) == autofix.STOP_NO_PROGRESS
    assert _counts(t) == ["3", "6"]


def test_a_round_that_leaves_fewer_is_progress():
    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    _round(t, ["a", "b", "c"], ["b", "x"], "r0", "r1")
    assert autofix.next_step(t, net=True) == "fix"
    assert autofix.best_attempt(t) is None  # the latest is the best


def test_the_best_attempt_is_the_one_with_the_fewest_problems_and_a_tie_keeps_the_latest():
    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    # The issue's build: 3 → 6 → 4 → 4 → 3 → 8.
    counts = [3, 6, 4, 4, 3, 8]
    t["allowed"] = 10
    for n in range(5):
        _round(t, [f"k{n}-{i}" for i in range(counts[n])], [f"k{n + 1}-{i}" for i in range(counts[n + 1])],
               f"r{n}", f"r{n + 1}")
    assert _counts(t) == [str(n) for n in counts]
    best = autofix.best_attempt(t)
    # Attempt 5 (3 problems) beats the first (also 3): a tie keeps the later one.
    assert best == {"attempt": "r4", "problems": 3, "round": 4, "instead_of": 8}


# ── through the loop ─────────────────────────────────────────────────────────
class Suite:
    """QA writes one test that asserts nothing, then — on its fix round — two."""

    def __init__(self) -> None:
        self.qa = 0

    def __call__(self, messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        system = messages[0].content
        schema = getattr(kwargs.get("options"), "json_schema", None)
        if system.startswith("You are the QA Engineer"):
            self.qa += 1
            files = [{"path": "backend/tests/test_one.py", "framework": "pytest", "targets": "backend/app/services.py",
                      "code": WEAK.format(n=1)}]
            if self.qa > 1:
                files.append({"path": "backend/tests/test_two.py", "framework": "pytest",
                              "targets": "backend/app/services.py", "code": WEAK.format(n=2)})
            payload = json.loads(resp.text)
            payload["test_files"] = files
            return resp.model_copy(update={"text": json.dumps(payload)})
        if system.startswith("You are the Backend Engineer"):
            if schema:
                payload = json.loads(resp.text)
                if "files" in payload:
                    payload["files"] = [{"path": "main.py", "purpose": "entry"}, {"path": "app/services.py", "purpose": "totals"}]
                return resp.model_copy(update={"text": json.dumps(payload)})
            code = {"backend/main.py": MAIN, "backend/app/services.py": REAL}
            return resp.model_copy(update={"text": fenced({p: code[p] for p in asked_files(messages)})})
        return resp


def test_a_round_that_made_it_worse_parks_the_build_on_the_attempt_before_it(client, monkeypatch):
    suite = Suite()
    stub(monkeypatch, "complete", suite)
    pid = client.post("/api/projects", json={"idea": "A totals service", "routing_mode": "local_only",
                                             "approval_mode": "unattended"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_question_gates(client, pid)
    project = client.get(f"/api/projects/{pid}").json()

    assert project["status"] == "awaiting_approval" and project["gate_kind"] == "needs_help"
    t = project["auto_fix"]["tracks"]["build:qa_engineer"]
    # One round: it was sent 1 and left 2, so the loop stopped there — no second round.
    assert [(len(r["problems"]), r["after"], r["broke"]) for r in t["rounds"]] == [(1, 2, 1)]
    assert t["stopped"]["reason"] == "no_progress" and t["stopped"]["left"] == 1
    assert t["stopped"]["kept_best"]["round"] == 0
    # The build is on QA's first suite again: one weak test, not two.
    qa = [p for p in project["phases"] if p["phase"] == "qa_engineer" and p["status"] != "rejected"]
    assert len(qa) == 1 and [f["path"] for f in qa[0]["output"]["test_files"]] == ["backend/tests/test_one.py"]
    assert qa[0]["handoff"]["kept_best"]["instead_of"] == 2
    assert "back on the attempt before any round" in project["gate_note"]


def test_a_security_round_keeps_its_own_judgement():
    data = {"tracks": {}}
    t = autofix.track(data, autofix.SECURITY)
    autofix.start_round(t, "guided", ["backend_engineer"], [{"key": "a"}, {"key": "b"}])
    autofix.close_round(t, ["c", "d", "e"])
    seen = autofix.view(data)["tracks"][autofix.SECURITY]
    assert "after" not in seen["rounds"][-1] and "trend" not in seen


def test_after_a_put_back_the_next_round_starts_from_it_and_says_so():
    """R1: 3 → 6, put back to the first attempt (a copy, c0). Keep trying: R2 sent c0
    and left 5 — worse than 3 again. The trend says where R2 started, and the best is
    still the first attempt, made by no round."""
    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    t["allowed"] = 5
    _round(t, ["a", "b", "c"], [f"x{i}" for i in range(6)], "r0", "r1")
    _round(t, ["a", "b", "c"], [f"y{i}" for i in range(5)], "c0", "r2", from_round=0)
    assert _counts(t) == ["3", "6", "back 3", "5"]
    best = autofix.best_attempt(t)
    assert best == {"attempt": "r0", "problems": 3, "round": 0, "instead_of": 5}
    assert autofix.said(autofix.trend(t)) == "3 → 6 → back to 3 → 5"


# ── review of #110 ───────────────────────────────────────────────────────────
def test_an_old_record_reads_with_its_real_trend_and_net_counts():
    """The issue's own build, as recorded before #103: rounds with no `after`. Each was
    sent every problem the attempt before it had, so the trend is read off them."""
    keys = [[f"r{n}-{i}" for i in range(c)] for n, c in enumerate([3, 6, 4, 4, 3])]
    rounds = []
    for n, sent in enumerate(keys):
        nxt = keys[n + 1] if n + 1 < len(keys) else [f"end-{i}" for i in range(7)] + sent[:1]
        rounds.append({"n": n + 1, "strategy": "guided", "phases": ["backend_engineer"],
                       "problems": [{"key": k} for k in sent],
                       "fixed": [k for k in sent if k not in nxt], "remaining": [k for k in sent if k in nxt]})
    data = {"tracks": {"build:backend_engineer": {
        "allowed": 5, "rounds": rounds, "resumed_after": 3, "episode_start": 0, "accepted": None,
        "stopped": {"reason": "limit", "left": 8, "keys": [], "at": "x"},
    }}}
    seen = autofix.view(data)["tracks"]["build:backend_engineer"]
    assert [p["n"] for p in seen["trend"]] == [3, 6, 4, 4, 3, 8]
    assert seen["rounds"][-1]["after"] == 8 and seen["rounds"][-1]["broke"] == 7
    assert "after" not in data["tracks"]["build:backend_engineer"]["rounds"][0]  # nothing written back


def test_a_tie_goes_to_the_attempt_itself_not_a_copy_put_back():
    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    t["allowed"] = 5
    _round(t, ["a", "b", "c"], [f"x{i}" for i in range(6)], "r0", "r1")
    _round(t, ["a", "b", "c"], [f"y{i}" for i in range(5)], "c0", "r2", from_round=0)
    assert autofix.best_attempt(t)["attempt"] == "r0"


def test_a_put_back_s_round_is_forgotten_when_the_episode_settles():
    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    t["kept"] = {"attempt": "c0", "round": 3}
    autofix.start_round(t, "guided", ["backend_engineer"], [{"key": "a"}])
    autofix.close_round(t, [], net=True)
    autofix.settle(t)
    assert "kept" not in t


def test_a_build_from_before_episodes_were_recorded_keeps_its_old_rounds_in_the_trend():
    data = {"tracks": {}}
    t = autofix.track(data, autofix.build_track("backend_engineer"))
    t["allowed"] = 5
    t["rounds"] = [{"n": n, "problems": [{"key": "a"}, {"key": "b"}, {"key": "c"}], "fixed": [], "remaining": ["a", "b", "c"],
                    "strategy": "guided", "phases": ["backend_engineer"]} for n in (1, 2)]
    _round(t, ["a", "b", "c"], ["a", "b", "c"], "r2", "r3")
    assert [p["n"] for p in autofix.trend(t)] == [3, 3, 3, 3]
