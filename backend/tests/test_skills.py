"""The skill library: what loads, what is chosen, what actually reaches the model.

Three properties are worth a test each, because each of them is a way the feature
could look like it works and not:

  • A skill that breaks a load rule is *listed* and never injected. Silently
    dropping it leaves a file on disk that does nothing and says nothing.
  • Selection is deterministic and bounded. The same build picks the same skills in
    the same order, and a pin beats a score that would have missed.
  • A skill takes its room from the same allocator everything else does. The
    prompt stays inside the window it was sized for, whatever the library holds.
"""
from __future__ import annotations

import pytest

from app.agents import get_agent
from app.agents.base import AgentContext
from app.core.config import Settings, settings
from app.core.constants import Phase, PhaseStatus
from app.db.base import SessionLocal
from app.db.models import PhaseResult
from app.router.model_profile import ModelProfile
from app.skills import registry
from app.skills.loader import Skill, parse
from app.skills.selection import Overrides, select

def _shipped_default(name: str):
    """What this setting is in the repository, whatever this machine's `.env` says.

    These tests assert properties of the library *as shipped* — that every bundled
    skill clears the ceiling, that a phase gets more than one skill. A developer who
    has tuned a knob in their own `.env` would otherwise see those fail, pointing at
    the library rather than at their configuration.
    """
    return Settings.model_fields[name].default


def _write(directory, name: str, *, body: str = "Do the thing.", **meta) -> None:
    """A `SKILL.md` on disk, with sane defaults for everything not named."""
    front = {
        "name": name,
        "title": meta.pop("title", name.replace("-", " ")),
        "description": meta.pop("description", "Use when doing the thing."),
        "agents": meta.pop("agents", []),
        "keywords": meta.pop("keywords", []),
    }
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in front.items():
        rendered = f"[{', '.join(value)}]" if isinstance(value, list) else value
        lines.append(f"{key}: {rendered}")
    lines += ["---", "", body, ""]
    (folder / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def library(tmp_path, monkeypatch):
    """An empty library on disk, in place of whatever this machine happens to have.

    `skills_enabled` is pinned on with the rest. It is a documented setting, and a
    developer who has it off in their own `.env` would otherwise watch most of this
    file fail with an empty library and no hint that a setting, not the code, was
    the reason.
    """
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    monkeypatch.setattr(settings, "skills_enabled", True)
    monkeypatch.setattr(settings, "skills_max_per_phase", _shipped_default("skills_max_per_phase"))
    monkeypatch.setattr(settings, "skills_bundled_dir", str(bundled))
    monkeypatch.setattr(settings, "skills_user_dir", str(user))
    monkeypatch.setattr(registry, "_STATE_PATH", tmp_path / "skills.local.json")
    return bundled, user


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    """The library that ships, and nothing this machine happens to have added to it.

    Without the empty user directory these tests read the developer's own
    `data/skills/`, so one local skill bound to a phase that does not exist would
    fail a test about the *bundled* library — a failure pointing at the wrong file.
    """
    user = tmp_path / "no-user-skills"
    user.mkdir()
    monkeypatch.setattr(settings, "skills_enabled", True)
    monkeypatch.setattr(settings, "skills_max_per_phase", _shipped_default("skills_max_per_phase"))
    monkeypatch.setattr(
        settings, "skill_body_max_chars", _shipped_default("skill_body_max_chars")
    )
    monkeypatch.setattr(settings, "skills_user_dir", str(user))
    monkeypatch.setattr(registry, "_STATE_PATH", tmp_path / "skills.local.json")


def _profile(window: int) -> ModelProfile:
    return ModelProfile(
        provider="ollama",
        model="m",
        context_limit=window,
        context_window=window,
        max_output_tokens=min(4096, window // 2),
    )


# ── the library that ships ───────────────────────────────────────────────────
def test_every_bundled_skill_passes_its_own_rules(shipped):
    """The ceiling and the no-format rule are enforced on the library that ships.

    Not a style check. A bundled skill over the ceiling takes its excess out of the
    knowledge base and the prior phases on every build; one carrying a format
    instruction competes with the JSON shape the agent is decoding against, and on a
    small local model the prose wins.
    """
    shipped = [s for s in registry.library() if s.source == "bundled"]
    assert shipped, "the bundled library should not be empty"
    broken = {s.name: s.problems for s in shipped if not s.usable}
    assert not broken, f"bundled skills that would never be injected: {broken}"


def test_every_bundled_skill_reaches_at_least_one_agent(shipped):
    """A skill bound to a phase that does not exist is a file nobody ever gets."""
    phases = registry.known_phases()
    for skill in registry.library():
        assert not skill.agents or set(skill.agents) <= phases


# ── loading ──────────────────────────────────────────────────────────────────
def test_frontmatter_survives_both_list_spellings():
    meta, body = parse(
        "---\nname: x\nagents: [a, b]\nkeywords:\n  - one\n  - two words\n---\n\nThe body.\n"
    )
    assert meta["agents"] == ["a", "b"]
    assert meta["keywords"] == ["one", "two words"]
    assert body == "The body."


def test_a_file_with_no_frontmatter_is_a_skill_with_nothing_declared():
    meta, body = parse("Just a procedure.\n")
    assert meta == {}
    assert body == "Just a procedure."


def test_a_skill_over_the_ceiling_is_listed_and_never_injected(library):
    bundled, _ = library
    _write(bundled, "too-long", body="x" * (settings.skill_body_max_chars + 1))
    skill = registry.get("too-long")
    assert skill is not None, "it stays in the library so a person can see why"
    assert not skill.usable
    assert not select("product_manager", "anything", {}, Overrides(pinned={"too-long"}))


def test_a_skill_that_dictates_a_format_is_never_injected(library):
    """Since every agent answers in a declared shape, this one competes with it."""
    bundled, _ = library
    _write(bundled, "bossy", body="Design the endpoint. Respond with a table of fields.")
    skill = registry.get("bossy")
    assert not skill.usable
    assert "content, never format" in " ".join(skill.problems)


def test_a_skill_bound_to_a_phase_that_does_not_exist_says_so(library):
    bundled, _ = library
    _write(bundled, "orphan", agents=["marketing_lead"])
    assert not registry.get("orphan").usable


def test_a_missing_user_directory_is_simply_no_user_skills(library, monkeypatch):
    bundled, user = library
    _write(bundled, "shipped", keywords=["app"])
    monkeypatch.setattr(settings, "skills_user_dir", str(user / "gone"))
    assert [s.name for s in registry.library()] == ["shipped"]


def test_a_configured_directory_that_is_not_there_falls_back_to_what_ships(shipped, monkeypatch):
    """A backend started from another directory still has its own skills."""
    monkeypatch.setattr(settings, "skills_bundled_dir", "/nowhere/at/all")
    assert registry.bundled_dir().is_dir()
    assert any(s.source == "bundled" for s in registry.library())


def test_a_library_that_cannot_be_read_leaves_the_phase_with_no_skills(shipped, monkeypatch):
    """The same contract RAG and memory hold to: degrade, never fail the pipeline."""
    from app.orchestration import graph

    def explode(*_a, **_k):
        raise OSError("the disk is on fire")

    monkeypatch.setattr(graph.skills, "select", explode)
    assert graph.gather_skills({"idea": "an app"}, Phase.BACKEND_ENGINEER.value) == ()


# ── selection ────────────────────────────────────────────────────────────────
def test_a_skill_only_reaches_the_agents_it_names(library):
    bundled, _ = library
    _write(bundled, "backend-only", agents=["backend_engineer"], keywords=["app"])
    _write(bundled, "everyone", keywords=["app"])

    backend = [s.skill.name for s in select("backend_engineer", "an app", {})]
    frontend = [s.skill.name for s in select("frontend_engineer", "an app", {})]
    assert "backend-only" in backend
    assert "backend-only" not in frontend
    assert "everyone" in backend and "everyone" in frontend


def test_a_keyword_that_matches_nothing_means_the_skill_never_arrives(library):
    """The silent miss, stated as a test. This is what the pin and preview exist for."""
    bundled, _ = library
    _write(bundled, "payments", keywords=["stripe", "checkout"])
    assert select("backend_engineer", "a blog about birds", {}) == []


def test_a_pin_overrides_both_the_score_and_the_library_switch(library):
    bundled, _ = library
    _write(bundled, "payments", keywords=["stripe"])
    registry.set_enabled("payments", False)

    assert select("backend_engineer", "a blog", {}) == []
    pinned = select("backend_engineer", "a blog", {}, Overrides(pinned={"payments"}))
    assert [s.skill.name for s in pinned] == ["payments"]
    assert pinned[0].pinned and pinned[0].reason.startswith("Pinned")


def test_excluding_beats_pinning(library):
    """Two contradictory instructions; not injecting is the half that cannot harm."""
    bundled, _ = library
    _write(bundled, "payments", keywords=["stripe"])
    both = Overrides(pinned={"payments"}, excluded={"payments"})
    assert select("backend_engineer", "stripe checkout", {}, both) == []


def test_the_idea_counts_for_more_than_a_prior_phase_mentioning_it(library):
    bundled, _ = library
    _write(bundled, "in-the-idea", keywords=["kanban"])
    _write(bundled, "in-the-output", keywords=["thumbnail"])
    chosen = select(
        "backend_engineer",
        "a kanban board",
        {"product_manager": {"features": ["thumbnail previews"]}},
    )
    assert [s.skill.name for s in chosen] == ["in-the-idea", "in-the-output"]


def test_keywords_match_whole_words_only(library):
    """`ci` must not be found inside `specify`, or every build gets the CI skill."""
    bundled, _ = library
    _write(bundled, "pipelines", keywords=["ci"])
    assert select("devops_engineer", "a site that lets you specify a menu", {}) == []
    assert select("devops_engineer", "a site with ci", {})


def test_selection_is_reproducible(library):
    bundled, _ = library
    for name in ("alpha", "bravo", "charlie"):
        _write(bundled, name, keywords=["app"])
    once = [s.skill.name for s in select("qa_engineer", "an app", {})]
    twice = [s.skill.name for s in select("qa_engineer", "an app", {})]
    assert once == twice == sorted(once)


def test_no_more_skills_than_the_configured_cap(library, monkeypatch):
    bundled, _ = library
    for i in range(6):
        _write(bundled, f"skill-{i}", keywords=["app"])
    monkeypatch.setattr(settings, "skills_max_per_phase", 2)
    assert len(select("qa_engineer", "an app", {})) == 2


def test_turning_the_feature_off_empties_the_library(library, monkeypatch):
    bundled, _ = library
    _write(bundled, "anything", keywords=["app"])
    monkeypatch.setattr(settings, "skills_enabled", False)
    assert registry.library() == []
    assert select("qa_engineer", "an app", {}) == []


# ── how much of it reaches the model ─────────────────────────────────────────
def _selected(name: str, body: str) -> tuple:
    skill = Skill(name=name, title=name, description="d", body=body)
    return tuple(select("qa_engineer", "x", {}, candidates=[skill], limit=1))


def test_skills_take_their_room_from_the_same_budget_as_everything_else(library):
    """A separate budget on top of the allocator is how a phase overruns the window."""
    agent = get_agent(Phase.QA_ENGINEER.value)
    profile = _profile(8192)
    ctx = AgentContext(
        idea="i" * 40_000,
        prior_outputs={d: {"code": "x" * 200_000} for d in agent.depends_on},
        rag_context="r" * 100_000,
        memory_context="m" * 100_000,
        skills=_selected("huge", "s" * 50_000),
    )
    built = sum(len(m.content) for m in agent._build_messages(ctx, profile).messages)
    assert built <= profile.prompt_char_budget


def test_a_skill_that_does_not_fit_is_dropped_whole(library):
    """Half a procedure is a procedure missing its last step, and nothing says so."""
    agent = get_agent(Phase.QA_ENGINEER.value)
    profile = _profile(8192)
    body = "s" * 40_000
    ctx = AgentContext(idea="an app", skills=_selected("huge", body))
    ask = agent._build_messages(ctx, profile)
    assert ask.skills_used == []
    assert body[:200] not in ask.messages[1].content


def test_what_fits_is_what_is_recorded(library):
    agent = get_agent(Phase.QA_ENGINEER.value)
    ctx = AgentContext(idea="an app", skills=_selected("small", "Check the edges."))
    ask = agent._build_messages(ctx, _profile(32768))
    assert ask.skills_used == ["small"]
    assert "Check the edges." in ask.messages[1].content


def test_a_skill_that_cannot_fit_costs_the_rest_of_the_prompt_nothing(library):
    """Turning skills on must never make a build worse than leaving them off.

    Every other section spends whatever it is given; skills cannot, because a
    procedure is injected whole or not at all. Reserving a share that nothing can
    spend is how a phase ends up with no procedures *and* a fifth less room for the
    knowledge base than the identical run that never asked for any — strictly worse
    on both counts, and invisible except in a log line.
    """
    agent = get_agent(Phase.QA_ENGINEER.value)
    profile = _profile(8192)
    shared = dict(
        idea="an app",
        prior_outputs={d: {"code": "x" * 80_000} for d in agent.depends_on},
        rag_context="r" * 40_000,
        memory_context="m" * 40_000,
    )
    offered = agent._section_budgets(
        AgentContext(**shared, skills=_selected("huge", "s" * 40_000)), profile
    )
    none = agent._section_budgets(AgentContext(**shared), profile)

    assert offered["skills"] == 0, "nothing fitted, so nothing should have been charged"
    for section in ("depends_on", "rag", "memory"):
        # A character of integer-division rounding, not a share.
        assert none[section] - offered[section] <= 1


def test_what_skills_do_not_spend_goes_back_to_the_context_that_can(library):
    agent = get_agent(Phase.QA_ENGINEER.value)
    profile = _profile(32768)
    shared = dict(
        idea="an app",
        prior_outputs={d: {"code": "x" * 80_000} for d in agent.depends_on},
        rag_context="r" * 40_000,
        memory_context="m" * 40_000,
    )
    tiny = _selected("tiny", "Check the edges.")
    budget = agent._section_budgets(AgentContext(**shared, skills=tiny), profile)
    none = agent._section_budgets(AgentContext(**shared), profile)

    spent = budget["skills"]
    assert 0 < spent < 400, "a two-line skill should cost about two lines"
    given_back = sum(budget[s] for s in ("depends_on", "rag", "memory")) - sum(
        none[s] for s in ("depends_on", "rag", "memory")
    )
    # Everything the skill did not use is back in the other sections, give or take
    # the rounding of three integer divisions.
    assert abs(given_back + spent) <= 3


def test_an_agent_with_no_skills_gets_no_heading(library):
    agent = get_agent(Phase.QA_ENGINEER.value)
    ask = agent._build_messages(AgentContext(idea="an app"), _profile(32768))
    assert "How this team does this work" not in ask.messages[1].content


# ── end to end: the reviewer can see which skills a phase had ────────────────
def test_a_finished_phase_records_the_skills_it_was_given(shipped, client):
    """Provenance. A skill you cannot confirm was used did nothing, as far as anyone
    reading this build can tell."""
    project = client.post(
        "/api/projects",
        json={"idea": "A habit tracker with reminders", "approval_mode": "every_phase"},
    ).json()
    assert client.post(f"/api/projects/{project['id']}/run").status_code == 200

    fetched = client.get(f"/api/projects/{project['id']}").json()
    first = next(p for p in fetched["phases"] if p["phase"] == Phase.PRODUCT_MANAGER.value)
    assert "prd-user-stories" in first["skills_used"]

    db = SessionLocal()
    try:
        row = (
            db.query(PhaseResult)
            .filter(PhaseResult.project_id == project["id"])
            .filter(PhaseResult.status != PhaseStatus.RUNNING.value)
            .first()
        )
        assert "prd-user-stories" in row.skills_used
    finally:
        db.close()


def test_what_a_build_excludes_never_reaches_its_agents(shipped, client):
    """The override set is carried into the run, not just stored on the row."""
    project = client.post(
        "/api/projects",
        json={
            "idea": "A habit tracker",
            "approval_mode": "every_phase",
            "skill_overrides": {"pinned": [], "excluded": ["prd-user-stories"]},
        },
    ).json()
    assert project["skill_overrides"]["excluded"] == ["prd-user-stories"]
    client.post(f"/api/projects/{project['id']}/run")

    phases = client.get(f"/api/projects/{project['id']}").json()["phases"]
    first = next(p for p in phases if p["phase"] == Phase.PRODUCT_MANAGER.value)
    assert "prd-user-stories" not in first["skills_used"]
    assert first["skills_used"], "the other skills it would have had still arrive"


# ── the API ──────────────────────────────────────────────────────────────────
def test_the_preview_answers_which_skills_an_idea_would_get(shipped, client):
    body = client.post("/api/skills/preview", json={"idea": "an online shop"}).json()
    by_phase = {p["phase"]: [s["name"] for s in p["skills"]] for p in body["phases"]}
    assert by_phase[Phase.PRODUCT_MANAGER.value]
    assert set(by_phase) == registry.known_phases()


def test_a_skill_that_breaks_a_rule_is_refused_at_the_door(shipped, client):
    res = client.post(
        "/api/skills",
        json={
            "name": "bossy-skill",
            "title": "Bossy",
            "description": "d",
            "body": "Respond with a table.",
        },
    )
    assert res.status_code == 422
    assert "format" in res.json()["detail"]


def test_a_bundled_skill_is_edited_by_shadowing_it_not_by_writing_to_it(shipped, client):
    original = client.get("/api/skills").json()
    shipped = next(s for s in original["skills"] if s["source"] == "bundled")

    edited = client.put(
        f"/api/skills/{shipped['name']}",
        json={
            "title": shipped["title"],
            "description": shipped["description"],
            "agents": shipped["agents"],
            "keywords": shipped["keywords"],
            "body": "Do it this way instead.",
        },
    ).json()
    assert edited["source"] == "user" and edited["overridden"]

    restored = client.delete(f"/api/skills/{shipped['name']}").json()
    assert restored["restored"]["source"] == "bundled"
    assert restored["restored"]["body"] == shipped["body"]
