"""Honest escalation and a stuck card anyone can act on (#103).

No round claims a stronger model that doesn't exist, nothing tells a model to rewrite,
each round says what it ran on, and a person can tell the crew what to try — or pick a
cloud model for one fix — from the card.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.orchestration import autofix, remediation
from app.router.router import ModelRouter
from tests.conftest import _fake_complete, asked_files, fenced, stub, through_database_gate


def test_a_third_round_is_on_a_stronger_model_only_when_there_is_one():
    assert remediation.strategy_for(3) == remediation.STRATEGY_STRONGER
    assert remediation.strategy_for(3, stronger=False) == remediation.STRATEGY_WITH_CODE
    assert remediation.strategy_for(1, stronger=False) == remediation.STRATEGY_GUIDED


def test_no_note_tells_a_model_to_rewrite():
    note = autofix.code_note([{"key": "k", "title": "x", "kind": "build"}], 3)
    assert "rewrite" not in note.lower()
    finding = remediation.Finding(key="k", title="t", severity="critical", category="c", location="",
                                  recommendation="", owner_phase="backend_engineer")
    assert "rewrite" not in remediation.fix_instruction([finding], remediation.STRATEGY_STRONGER).lower()


def test_the_person_s_words_reach_the_round():
    note = autofix.code_note([{"key": "k", "title": "x", "kind": "build"}], 1, person="  keep the routes   in one file ")
    assert note.rstrip().endswith("the technology choices.") and "The person says: keep the routes in one file" in note


class Broken:
    """A backend whose main.py never parses, so the loop runs out and asks for help;
    records what each backend write call was sent."""

    def __init__(self, reach: bool = True) -> None:
        self.prompts: list[str] = []
        self.pins: list[str] = []
        #: Whether a pinned model answers, or the usual one does instead.
        self.reach = reach

    def __call__(self, messages, **kwargs):
        resp = _fake_complete(messages, **kwargs)
        if not messages[0].content.startswith("You are the Backend Engineer"):
            return resp
        if getattr(kwargs.get("options"), "json_schema", None):
            payload = json.loads(resp.text)
            payload["files"] = [{"path": "main.py", "purpose": "app"}]
            return resp.model_copy(update={"text": json.dumps(payload)})
        self.prompts.append(messages[-1].content)
        update = {"text": fenced({p: "x = (\n" for p in asked_files(messages)})}
        if kwargs.get("pin"):
            self.pins.append(kwargs["pin"])
            if self.reach:
                update["model"] = kwargs["pin"].split(":", 1)[-1]
        return resp.model_copy(update=update)


def _stuck(client, monkeypatch, cloud=None, reach=True):
    monkeypatch.setattr(ModelRouter, "strongest_for", lambda self, *a, **k: None)
    monkeypatch.setattr(ModelRouter, "strongest_cloud", lambda self: cloud)
    monkeypatch.setattr(settings, "auto_fix_max_rounds", 3)
    crew = Broken(reach)
    stub(monkeypatch, "complete", crew)
    pid = client.post("/api/projects", json={"idea": "An API", "routing_mode": "local_only",
                                             "approval_mode": "unattended"}).json()["id"]
    client.post(f"/api/projects/{pid}/run")
    through_database_gate(client, pid)
    return pid, crew


def test_a_one_model_account_never_hears_of_a_stronger_model(client, monkeypatch):
    pid, _crew = _stuck(client, monkeypatch)
    project = client.get(f"/api/projects/{pid}").json()
    assert project["gate_kind"] == "needs_help"
    t = project["auto_fix"]["tracks"]["build:backend_engineer"]
    assert "stronger_model" not in [r["strategy"] for r in t["rounds"]]
    assert t["stopped"]["stronger"] is None and t["stopped"]["cloud"] is None
    # Each round says what it ran on, and how it was chosen.
    assert all(r.get("model") for r in t["rounds"])
    assert "strongest" not in json.dumps(t).lower()


def test_a_note_from_the_person_goes_to_the_next_round_and_is_recorded(client, monkeypatch):
    pid, crew = _stuck(client, monkeypatch)
    before = len(crew.prompts)
    r = client.post(f"/api/projects/{pid}/auto-fix/retry", json={"note": "Use plain functions, no classes"})
    assert r.status_code == 200, r.text
    project = client.get(f"/api/projects/{pid}").json()
    t = project["auto_fix"]["tracks"]["build:backend_engineer"]
    noted = [x for x in t["rounds"] if x.get("note")]
    assert [x["note"] for x in noted] == ["Use plain functions, no classes"]
    # It reached the model, in the fix call itself.
    assert any("The person says: Use plain functions, no classes" in p for p in crew.prompts[before:])


def test_a_key_in_the_note_is_refused(client, monkeypatch):
    pid, _crew = _stuck(client, monkeypatch)
    r = client.post(f"/api/projects/{pid}/auto-fix/retry", json={"note": "use sk-ant-api03-" + "a" * 90})
    assert r.status_code == 422


@pytest.mark.parametrize("reach", [True, False])
def test_a_cloud_model_for_one_fix_only_when_offered_and_only_with_a_yes(client, monkeypatch, reach):
    pid, crew = _stuck(client, monkeypatch, cloud="anthropic:claude-test", reach=reach)
    project = client.get(f"/api/projects/{pid}").json()
    t = project["auto_fix"]["tracks"]["build:backend_engineer"]
    assert t["stopped"]["cloud"] == "anthropic:claude-test"
    # Nothing is sent to the cloud unless asked, and only the model offered.
    assert client.post(f"/api/projects/{pid}/auto-fix/retry", json={"model": "openai:gpt-x"}).status_code == 400
    assert crew.pins == []
    r = client.post(f"/api/projects/{pid}/auto-fix/retry", json={"model": "anthropic:claude-test"})
    assert r.status_code == 200, r.text
    project = client.get(f"/api/projects/{pid}").json()
    t = project["auto_fix"]["tracks"]["build:backend_engineer"]
    asked = [x for x in t["rounds"] if x.get("asked_model")]
    assert [x["asked_model"] for x in asked] == ["anthropic:claude-test"]
    # A model that didn't answer isn't claimed: the round says the usual one ran.
    assert (asked[0]["basis"], asked[0]["model"]) == (
        ("asked", "claude-test") if reach else ("asked_unreached", "mock-model")
    )
    # The one round ran on it, and only that round.
    assert set(crew.pins) == {"anthropic:claude-test"}
    later = t["rounds"][t["rounds"].index(asked[0]) + 1:]
    assert all(x.get("basis") != "asked" for x in later)


def test_the_cloud_offered_is_the_account_s_strongest_working_key(monkeypatch):
    from app.router.router import router as model_router

    monkeypatch.setitem(model_router._default_model, "anthropic", "claude-x")
    monkeypatch.setattr(model_router._cloud["anthropic"], "available", lambda: False)
    assert model_router.strongest_cloud() is None
    # Even where Auto would lead with it already: a local build is offered it.
    monkeypatch.setattr(model_router._cloud["anthropic"], "available", lambda: True)
    assert model_router.strongest_cloud() == "anthropic:claude-x"
