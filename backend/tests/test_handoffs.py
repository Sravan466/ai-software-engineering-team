"""Hand-offs between agents (#80): digests, cuts at field boundaries, the name registry,
cut-off replies, and the narrowed DevOps and Cost briefs."""
from __future__ import annotations

import json
import re

import pytest

from app.agents import AGENTS, get_agent
from app.agents import base as agent_base
from app.agents import handoff
from app.agents.base import AgentContext, BaseAgent
from app.core.constants import Phase, SchemaStatus
from app.router.model_profile import ModelProfile
from app.schemas.agent_outputs import ProductManagerOutput
from app.schemas.llm import LLMResponse, normalise_finish

PM = {
    "product_name": "Tasky",
    "problem_statement": "Small teams lose track of chores.",
    "target_users": ["teams"],
    "mvp_scope": ["tasks"],
    "out_of_scope": ["billing"],
    "assumptions": ["one team per account"],
    "features": [
        {"name": "Task list", "priority": "P0", "description": "See and add tasks"},
        {"name": "Reports", "priority": "P2", "description": "Weekly report"},
    ],
    "user_stories": [
        {
            "as_a": "member",
            "i_want": "to add a task",
            "so_that": "nothing is forgotten",
            "feature": "Task list",
            "acceptance_criteria": ["Adding a task shows it at the top of the list"],
        },
        {
            "as_a": "lead",
            "i_want": "a weekly report",
            "so_that": "I see progress",
            "feature": "Reports",
            "acceptance_criteria": ["The report lists finished tasks"],
        },
    ],
    "success_metrics": ["tasks added"],
    "roadmap": [{"milestone": "MVP", "deliverables": ["list"]}],
}
SD = {
    "architecture_overview": "A FastAPI API and a Next.js app.",
    "tech_stack": {"frontend": ["Next.js"], "backend": ["FastAPI"], "database": ["PostgreSQL"]},
    "components": [{"name": "API", "responsibility": "serves tasks"}],
    "data_model": [{"entity": "Todo", "fields": ["id:int", "title:str"]}],
    "api_endpoints": [
        {"method": "GET", "path": "/api/todos", "purpose": "list", "entity": "Todo", "auth": "user"},
        {"method": "POST", "path": "/api/todos", "purpose": "add", "entity": "Todo", "auth": "user"},
    ],
    "pages_expected": ["/"],
    "scaling_considerations": ["none"],
    "architecture_diagram_mermaid": "flowchart TD\n a-->b",
}
ROUTES_PY = (
    "from fastapi import FastAPI\napp = FastAPI()\n\n"
    "@app.get('/api/todos')\ndef list_todos():\n    return []\n\n"
    "@app.post('/api/todos')\ndef add_todo():\n    return {}\n"
)
BE = {
    "framework": "FastAPI",
    "summary": "Todos API",
    "auth_flow": "session cookie",
    "setup_instructions": ["pip install"],
    "files": [
        {"path": "backend/main.py", "language": "python", "purpose": "routes", "code": ROUTES_PY},
        {
            "path": "backend/settings.py",
            "language": "python",
            "purpose": "config",
            "code": "import os\nDB = os.environ.get('DATABASE_URL')\n" + "# padding\n" * 4000,
        },
    ],
}
FE = {
    "framework": "Next.js",
    "summary": "One page",
    "pages": [{"route": "/", "purpose": "list"}],
    "components": [{"name": "TodoList", "purpose": "list"}],
    "state_management": "useState",
    "files": [
        {
            "path": "frontend/app/page.jsx",
            "language": "javascript",
            "purpose": "home",
            "code": "export default function Page(){ fetch('/api/todos'); return null }\n",
        }
    ],
}
QA = {
    "summary": "tests",
    "test_strategy": "unit",
    "edge_cases": [],
    "coverage_estimate": "some",
    "risks": [],
    "command_backend": "pytest backend/tests",
    "command_frontend": "npm test --prefix frontend",
    "test_files": [
        {
            "path": "backend/tests/test_todos.py",
            "framework": "pytest",
            "targets": "main",
            "code": "def test_adding_a_task_shows_it_at_the_top_of_the_list():\n    pass\n",
        }
    ],
}
PRIOR = {
    Phase.PRODUCT_MANAGER.value: ProductManagerOutput.model_validate(PM).model_dump(mode="json"),
    Phase.SYSTEM_DESIGN.value: SD,
    Phase.BACKEND_ENGINEER.value: BE,
    Phase.FRONTEND_ENGINEER.value: FE,
    Phase.QA_ENGINEER.value: QA,
}


def _profile(window: int) -> ModelProfile:
    return ModelProfile(
        provider="ollama", model="m", context_limit=window, context_window=window,
        max_output_tokens=min(4096, window // 2),
    )


def _prompt(phase: str, window: int = 32768, prior=None) -> str:
    agent = get_agent(phase)
    ctx = AgentContext(idea="A shared todo list", prior_outputs=prior if prior is not None else PRIOR)
    return "\n".join(m.content for m in agent._build_messages(ctx, _profile(window)).messages)


def _frames(user_turn: str) -> dict[str, dict]:
    found = re.findall(r"# From (\w+)\n```json\n(.*?)\n```", user_turn, re.DOTALL)
    return {phase: json.loads(body) for phase, body in found}


# ── 1. every agent is shown what it is asked to use ──────────────────────────
def test_stories_take_their_feature_priority():
    out = ProductManagerOutput.model_validate(PM)
    assert [s.priority for s in out.user_stories] == ["P0", "P2"]


def test_qa_sees_the_p0_acceptance_criteria_and_not_the_p2_ones():
    text = _prompt(Phase.QA_ENGINEER.value)
    assert "Adding a task shows it at the top of the list" in text
    digest = _frames(text)[Phase.PRODUCT_MANAGER.value]["digest"]
    assert [s["i_want"] for s in digest["p0_stories"]] == ["to add a task"]


def test_cost_sees_the_p0_features_the_file_counts_and_the_price_list():
    text = _prompt(Phase.COST_ESTIMATION.value)
    frames = _frames(text)
    assert frames[Phase.PRODUCT_MANAGER.value]["digest"]["p0_features"][0]["name"] == "Task list"
    assert len(frames[Phase.BACKEND_ENGINEER.value]["digest"]["files"]) == 2
    assert len(frames[Phase.FRONTEND_ENGINEER.value]["digest"]["files"]) == 1
    assert "PRICE LIST" in text and "Render web service" in text


def test_devops_sees_qa_commands_and_its_contract():
    text = _prompt(Phase.DEVOPS_ENGINEER.value)
    qa = _frames(text)[Phase.QA_ENGINEER.value]["digest"]
    assert qa["command_backend"] == "pytest backend/tests"
    assert "PLATFORM DEPLOY" in text and "Do NOT write render.yaml" in text


#: A word in a task text that names an upstream artefact -> the phase that makes it.
_ARTEFACTS = {
    "acceptance criteri": Phase.PRODUCT_MANAGER.value,
    "P0 feature": Phase.PRODUCT_MANAGER.value,
    "P0 stor": Phase.PRODUCT_MANAGER.value,
    "Product Manager": Phase.PRODUCT_MANAGER.value,
    "registry": Phase.SYSTEM_DESIGN.value,
    "endpoints_implemented": Phase.BACKEND_ENGINEER.value,
    "Backend digest": Phase.BACKEND_ENGINEER.value,
    "Backend and Frontend": Phase.FRONTEND_ENGINEER.value,
    "command_backend": Phase.QA_ENGINEER.value,
}


@pytest.mark.parametrize("key", list(AGENTS))
def test_every_artefact_a_task_names_is_in_that_agents_prompt(key):
    agent = AGENTS[key]
    task = agent.task_text()
    for word, phase in _ARTEFACTS.items():
        if word not in task or phase == key:
            continue
        if phase == Phase.SYSTEM_DESIGN.value and word == "registry":
            assert agent._registry(AgentContext(idea="x", prior_outputs=PRIOR)), key
            continue
        if word == "command_backend" and key == Phase.QA_ENGINEER.value:
            continue  # QA writes these; it is not asked to read them
        assert phase in agent.depends_on, f"{key} is asked about '{word}' but never shown {phase}"


# ── 2. digests, and cuts at field boundaries ─────────────────────────────────
def test_on_an_8k_window_every_frame_parses_has_its_digest_and_says_what_was_cut():
    text = _prompt(Phase.QA_ENGINEER.value, window=8192)
    frames = _frames(text)
    assert set(frames) == set(get_agent(Phase.QA_ENGINEER.value).depends_on)
    for phase, frame in frames.items():
        assert "digest" in frame and "output" in frame, phase
    backend = frames[Phase.BACKEND_ENGINEER.value]["output"]
    assert "_cut" in backend, "the 40 KB backend can't fit whole in an 8K window"
    # Cut between fields, code last: the index survives, the padding does not.
    assert backend.get("framework") == "FastAPI" or backend["_cut"] == "all"


def test_a_cut_keeps_whole_items_and_names_the_rest():
    output = {"summary": "s", "files": [{"path": f"f{i}.py", "code": "x" * 500} for i in range(10)]}
    text, omitted = handoff._cut_output(output, 1800)
    parsed = json.loads(text)
    assert parsed["summary"] == "s"
    assert 1 <= len(parsed["files"]) < 10
    assert omitted and omitted[0].startswith("files[")
    assert "files[" in parsed["_cut"]


def test_code_comes_last_whatever_order_the_model_wrote():
    output = {"files": [{"path": "a", "code": "x" * 4000}], "summary": "kept", "auth_flow": "kept too"}
    parsed = json.loads(handoff._cut_output(output, 200)[0])
    assert parsed["summary"] == "kept" and parsed["auth_flow"] == "kept too"
    assert "files" not in parsed


def test_a_small_dependency_hands_its_unused_share_to_a_large_one():
    shares = handoff.share({"pm": 300, "backend": 50_000}, 10_000)
    assert shares["pm"] == 300
    assert shares["backend"] == 9_700


def test_the_frame_never_costs_more_than_its_room():
    output = BE
    skeleton = handoff.render("backend_engineer", output, 0)
    for room in (0, 50, 400, 3_000, 20_000):
        shown = handoff.render("backend_engineer", output, room)
        assert len(shown.text) <= len(skeleton.text) + room, room
        json.loads(shown.text.split("```json\n", 1)[1].rsplit("\n```", 1)[0])


def test_a_digest_fits_its_limit_by_shortening_lists():
    big = {"rationale": "r", "files": [{"path": f"p{i}", "purpose": "x" * 50} for i in range(60)]}
    text = handoff.fit(big, 1500)
    assert len(text) <= 1500
    assert "_trimmed" in json.loads(text)


@pytest.mark.parametrize("window", [32768, 8192])
def test_prompts_with_real_hand_offs_fit_the_window(window):
    profile = _profile(window)
    for key, agent in AGENTS.items():
        ctx = AgentContext(idea="A shared todo list", prior_outputs=PRIOR)
        built = sum(len(m.content) for m in agent._build_messages(ctx, profile).messages)
        assert built <= profile.prompt_char_budget, key


# ── 3. the name registry ─────────────────────────────────────────────────────
def test_later_phases_get_the_registry_and_system_design_does_not():
    assert "NAME REGISTRY" in _prompt(Phase.BACKEND_ENGINEER.value)
    assert "GET /api/todos" in _prompt(Phase.FRONTEND_ENGINEER.value)
    assert "NAME REGISTRY" not in _prompt(Phase.SYSTEM_DESIGN.value)


def test_a_frontend_call_nobody_serves_is_a_reference_problem_naming_the_nearest_path():
    from app.build.check import name_problems

    files = {
        "backend/main.py": ROUTES_PY,
        "frontend/app/page.jsx": "export default function P(){ fetch('/api/tasks'); return null }\n",
    }
    problems = name_problems(files, ["frontend/app/page.jsx"], Phase.FRONTEND_ENGINEER.value, PRIOR)
    assert len(problems) == 1
    assert problems[0].kind == "reference" and "/api/tasks" in problems[0].message
    assert "/api/todos" in problems[0].message


def test_a_call_through_a_base_url_or_a_router_prefix_is_still_served():
    from app.build.check import name_problems

    files = {
        "backend/routes.py": "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/todos/{todo_id}')\ndef one(todo_id: int):\n    return {}\n",
        "frontend/lib/api.js": "export const one = (id) => fetch(`${API_URL}/api/todos/${id}`)\n",
    }
    assert name_problems(files, ["frontend/lib/api.js"], Phase.FRONTEND_ENGINEER.value, PRIOR) == []


def test_a_backend_route_that_misspells_the_registry_is_sent_back_and_an_extra_one_is_not():
    from app.build.check import name_problems

    code = (
        "from fastapi import FastAPI\napp = FastAPI()\n"
        "@app.get('/api/todo')\ndef a():\n    return []\n"
        "@app.get('/health')\ndef b():\n    return {}\n"
    )
    files = {"backend/main.py": code}
    problems = name_problems(files, ["backend/main.py"], Phase.BACKEND_ENGINEER.value, PRIOR)
    assert [p.message.split(",")[0] for p in problems] == ["serves GET /api/todo"]
    assert "/api/todos" in problems[0].message


# ── 4. a reply cut off at the output limit ───────────────────────────────────
@pytest.mark.parametrize("raw", ["length", "max_tokens", "MAX_TOKENS"])
def test_every_providers_word_for_the_output_limit_is_length(raw):
    assert normalise_finish(raw) == "length"


class _Scripted(BaseAgent):
    key = "scripted"
    title = "Scripted"
    output_model = get_agent(Phase.SECURITY_ENGINEER.value).output_model

    def __init__(self, replies):
        self.replies = list(replies)
        self.asked: list[list] = []
        self.schemas: list = []

    def _complete(self, messages, ctx, options):
        self.asked.append(messages)
        self.schemas.append(options.json_schema)
        text, reason = self.replies.pop(0)
        return LLMResponse(text=text, provider="mock", model="m", finish_reason=reason)


def test_a_cut_off_reply_asks_for_the_remaining_keys_only_and_is_merged(stub_router):
    first = '{"summary": "ok", "findings": [], "secrets_check": "none", "risk_ass'
    rest = '{"risk_assessment": "low", "overall_posture": "fine"}'
    agent = _Scripted([(first, "length"), (rest, "stop")])
    result = agent.run(AgentContext(idea="x"))

    assert result.truncated_replies == 1
    assert result.handoff["truncated_replies"] == 1
    assert result.schema_status == SchemaStatus.REPAIRED.value
    assert result.output["summary"] == "ok" and result.output["risk_assessment"] == "low"
    ask = agent.asked[1][-1].content
    assert "cut off" in ask and "risk_assessment, overall_posture" in ask
    assert "summary" not in ask.split("ONLY the remaining keys:")[1]
    assert set(agent.schemas[1]["properties"]) == {"risk_assessment", "overall_posture"}


def test_a_reply_still_cut_off_is_recorded_as_truncated(stub_router):
    first = '{"summary": "ok", "findings": [], "secrets_check": "none", "risk_ass'
    again = '{"risk_assessment": "lo'
    agent = _Scripted([(first, "length"), (again, "length")])
    result = agent.run(AgentContext(idea="x"))
    assert result.truncated_replies == 2
    assert result.schema_status == SchemaStatus.INVALID.value
    assert "truncated" in (result.schema_note or "")


# ── 5. DevOps and Cost ───────────────────────────────────────────────────────
def test_devops_files_cannot_replace_platform_files():
    from app.core.artifacts import devops_may_write

    out = {"ci_workflow": None}
    assert not devops_may_write("render.yaml", out)
    assert not devops_may_write("frontend/package.json", out)
    assert not devops_may_write(".github/workflows/ci.yml", out)
    assert devops_may_write("docs/DEPLOY.md", out)
    good = {"ci_workflow": {"path": ".github/workflows/ci.yml", "tool": "gha", "content": "on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n"}}
    assert devops_may_write(".github/workflows/ci.yml", good)
    bad = {"ci_workflow": {"path": ".github/workflows/ci.yml", "tool": "gha", "content": "jobs: [unclosed"}}
    assert not devops_may_write(".github/workflows/ci.yml", bad)


def test_ci_workflow_only_when_github_is_connected(monkeypatch):
    from app.agents import devops_engineer

    agent = get_agent(Phase.DEVOPS_ENGINEER.value)
    wf = {"path": ".github/workflows/ci.yml", "tool": "gha", "content": "jobs: {t: {runs-on: x}}"}
    monkeypatch.setattr(devops_engineer, "github_connected", lambda: False)
    out, problems = agent.own_checks({"summary": "s", "ci_workflow": wf}, AgentContext(idea="x"))
    assert out["ci_workflow"] is None and problems == []
    assert "NOT CONNECTED" in agent.system_prompt()

    monkeypatch.setattr(devops_engineer, "github_connected", lambda: True)
    out, problems = agent.own_checks({"summary": "s", "ci_workflow": wf}, AgentContext(idea="x"))
    assert out["ci_workflow"] == wf and problems == []
    broken = {**wf, "content": "jobs: [unclosed"}
    _, problems = agent.own_checks({"summary": "s", "ci_workflow": broken}, AgentContext(idea="x"))
    assert problems and "YAML" in problems[0]


def test_cost_requires_assumptions_and_shows_them_under_each_figure():
    agent = get_agent(Phase.COST_ESTIMATION.value)
    assert "assumptions" in agent.response_schema()["required"]
    md = agent.to_markdown(
        {
            "summary": "cheap",
            "assumptions": ["100 users"],
            "monthly_infra_cost": [{"item": "Render", "low_usd": 0, "high_usd": 7, "assumption": "Starter tier"}],
            "total_monthly_low_usd": 0,
            "total_monthly_high_usd": 7,
        }
    )
    assert "Assumes:_ Starter tier" in md and "100 users" in md and "Estimates" in md


# ── 6. measured ──────────────────────────────────────────────────────────────
def test_scorecard_metrics_read_the_build():
    from app.build.layout import Placer
    from app.evals import scorecard

    files = {"backend/main.py": ROUTES_PY, "frontend/app/page.jsx": FE["files"][0]["code"],
             "backend/tests/test_todos.py": QA["test_files"][0]["code"]}
    assert scorecard.names_used(PRIOR, files) == 1.0
    assert scorecard.endpoints_wired(files) == 1.0
    assert scorecard.criteria_covered(PRIOR, files) == 1.0
    assert Placer  # placed paths are what the scorecard reads


def test_a_prompt_variant_replaces_the_task_text(monkeypatch, tmp_path):
    from scripts.eval_harness import load_variant

    path = tmp_path / "v.json"
    path.write_text(json.dumps({"name": "terse", "tasks": {"qa_engineer": "Write tests. Be terse."}}))
    name, tasks = load_variant(str(path))
    monkeypatch.setattr(agent_base, "TASK_OVERRIDES", tasks)
    assert name == "terse"
    assert "Be terse." in _prompt(Phase.QA_ENGINEER.value)

    path.write_text(json.dumps({"tasks": {"nobody": "x"}}))
    with pytest.raises(SystemExit):
        load_variant(str(path))
