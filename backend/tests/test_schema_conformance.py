"""The bug this file exists for: half the agents generated against a truncated system
prompt, so their output drifted from the declared shape — and both safety gates read
that drift as "nothing to report" and let the run through.

Three layers are covered, because the fix is three layers deep:

  * the request now carries a probed context window and an output budget, so the
    prompt is never silently cut from the head;
  * the declared shape is enforced at decode time and checked afterwards, with the
    key names models actually drifted to normalised back;
  * the gates read through remaining drift, and stop the run when they cannot read
    at all rather than returning "carry on".
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.agents import get_agent
from app.agents.base import AgentContext, BaseAgent
from app.core.constants import ApprovalMode, GateKind, Phase, RoutingMode, SchemaStatus
from app.orchestration.approval import (
    decide_gate,
    projected_monthly_cost,
    severe_findings,
)
from app.router.model_profile import (
    ModelProfile,
    build_profile,
    fallback_profile,
    kv_bytes_per_token,
    resolve_window,
)
from app.schemas.agent_outputs import (
    CostEstimationOutput,
    DevOpsEngineerOutput,
    QAEngineerOutput,
    SecurityEngineerOutput,
)
from app.schemas.llm import GenerationOptions

# A real `POST /api/show` payload, trimmed to the keys that decide anything.
SHOW = {
    "capabilities": ["completion", "tools"],
    "details": {"family": "qwen2", "parameter_size": "7.6B", "quantization_level": "Q4_K_M"},
    "model_info": {
        "general.architecture": "qwen2",
        "general.parameter_count": 7_615_616_512,
        "qwen2.context_length": 32768,
        "qwen2.block_count": 28,
        "qwen2.attention.head_count": 28,
        "qwen2.attention.head_count_kv": 4,
        "qwen2.embedding_length": 3584,
    },
}


class _Project:
    """The two fields `decide_gate` reads off the live row."""

    def __init__(self, mode=ApprovalMode.CHECKPOINTS, cap=None):
        self.effective_approval_mode = mode.value
        self.cost_cap_usd = cap


# ── 1. the window is probed, never assumed ───────────────────────────────────
def test_context_window_comes_from_the_model_not_a_literal():
    profile = build_profile(
        provider="ollama",
        model="qwen2.5:7b",
        show=SHOW,
        supports_schema_format=True,
        ram_bytes=64 * 2**30,  # plenty, so the model's own limit is the binding one
    )
    assert profile.context_limit == 32768
    assert profile.context_window == 32768
    assert profile.source == "probe"
    assert profile.parameter_size == "7.6B"
    # And the phase that used to break — 6,019 tokens — now fits with room to spare.
    assert profile.prompt_token_budget > 6019


def test_the_kv_cache_cost_is_computed_from_the_model_not_guessed():
    kv = kv_bytes_per_token(SHOW["model_info"], "qwen2")
    assert kv == 2 * 28 * 4 * (3584 // 28) * 2  # K+V × layers × kv-heads × head-dim × f16


def test_a_small_machine_clamps_the_window_instead_of_swapping(monkeypatch):
    from app.router import model_profile

    monkeypatch.setattr(model_profile.settings, "ollama_context_ceiling", None)
    window, reason = resolve_window(
        context_limit=32768,
        kv_bytes_per_token=kv_bytes_per_token(SHOW["model_info"], "qwen2"),
        ram_bytes=2 * 2**30,
    )
    assert window < 32768
    assert reason and "RAM" in reason


def test_an_ordinary_laptop_is_not_clamped_into_uselessness(monkeypatch):
    """The clamp must not be worse than having no clamp at all.

    Subtracting the model's on-disk size from available RAM turned an 8 GiB laptop
    running a 7B model into a 2,048-token window — worse than the behaviour this
    replaced. Ollama mmaps the weights, so they are page-cache backed rather than a
    fixed deduction, and on unified-memory machines may not sit in system RAM at all.
    """
    from app.router import model_profile

    monkeypatch.setattr(model_profile.settings, "ollama_context_ceiling", None)
    window, _ = resolve_window(
        context_limit=32768,
        kv_bytes_per_token=kv_bytes_per_token(SHOW["model_info"], "qwen2"),
        ram_bytes=8 * 2**30,
    )
    assert window == 32768, "an 8 GiB laptop running a 7B model was clamped"


def test_a_model_that_reports_nothing_falls_back_to_a_stated_window():
    profile = fallback_profile("ollama", "mystery:latest")
    assert profile.source == "fallback"
    assert profile.context_window > 0
    assert profile.prompt_char_budget > 0


def test_small_models_are_flagged_rather_than_left_to_puzzle_the_user():
    tiny = {
        **SHOW,
        "details": {**SHOW["details"], "parameter_size": "1.5B"},
        "model_info": {**SHOW["model_info"], "general.parameter_count": 1_500_000_000},
    }
    profile = build_profile(
        provider="ollama",
        model="tiny",
        show=tiny,
        supports_schema_format=True,
        ram_bytes=64 * 2**30,
    )
    assert profile.is_small
    assert any("small model" in w for w in profile.warnings)


def test_the_ollama_request_carries_the_window_and_the_output_budget():
    """The whole bug in one assertion: these two keys were absent from every call."""
    from app.router.providers.ollama import OllamaProvider

    provider = OllamaProvider()
    profile = ModelProfile(
        provider="ollama",
        model="m",
        context_limit=32768,
        context_window=32768,
        max_output_tokens=4096,
        supports_schema_format=True,
    )
    schema = get_agent(Phase.SECURITY_ENGINEER.value).response_schema()
    payload = provider._payload(
        [], "m", GenerationOptions(json_mode=True, json_schema=schema), profile, schema=True
    )
    assert payload["options"]["num_ctx"] == 32768
    assert payload["options"]["num_predict"] == 4096
    # …and `format` carries the shape, not merely the word "json".
    assert payload["format"] == schema

    downgraded = provider._payload(
        [],
        "m",
        GenerationOptions(json_mode=True, json_schema=schema),
        ModelProfile(
            provider="ollama",
            model="m",
            context_limit=8192,
            context_window=8192,
            max_output_tokens=2048,
            supports_schema_format=False,
        ),
        schema=True,
    )
    assert downgraded["format"] == "json"  # graceful degradation, not a crash
    assert downgraded["options"]["num_ctx"] == 8192


@pytest.mark.parametrize(
    "version,capabilities,expected,why",
    [
        ((0, 30, 10), ["completion", "tools"], True, "a current server and a chat model"),
        ((0, 4, 9), ["completion"], False, "schema `format` predates this server"),
        ((0, 30, 10), ["embedding"], False, "a model that cannot complete text"),
        ((0, 30, 10), [], True, "a server too old to report capabilities at all"),
        (None, ["completion"], False, "a server that would not say which version it is"),
    ],
)
def test_schema_constrained_decoding_degrades_rather_than_failing(
    version, capabilities, expected, why
):
    """Every path out of "this model can't be grammar-constrained" is plain JSON mode."""
    from unittest.mock import patch

    from app.router.providers.ollama import OllamaProvider

    provider = OllamaProvider()
    provider._version = version
    show = {
        "capabilities": capabilities,
        "details": {"family": "qwen2"},
        "model_info": {"general.architecture": "qwen2", "qwen2.context_length": 8192},
    }
    with patch.object(OllamaProvider, "_show", return_value=show), patch.object(
        OllamaProvider, "server_version", return_value=version
    ):
        profile = provider.profile(f"m-{why}")

    assert profile.supports_schema_format is expected, why
    # Whatever the answer, the window and the output budget still arrive.
    assert profile.context_window == 8192
    assert profile.max_output_tokens == 4096


def test_a_failure_that_was_never_about_the_schema_keeps_its_advice():
    """A 404 is a model that isn't pulled. Retrying without the schema hides that."""
    from unittest.mock import patch

    import httpx

    from app.router.base import ProviderError
    from app.router.providers.ollama import OllamaProvider

    class _NotFound:
        status_code = 404
        text = "model 'ghost' not found"

        def raise_for_status(self):
            raise httpx.HTTPStatusError("404", request=None, response=self)

    provider = OllamaProvider()
    with patch.object(OllamaProvider, "_show", return_value=None), patch(
        "httpx.post", return_value=_NotFound()
    ):
        with pytest.raises(ProviderError) as caught:
            provider.generate(
                [],
                "ghost",
                GenerationOptions(json_mode=True, json_schema={"type": "object"}),
            )
    assert "ollama pull ghost" in str(caught.value)


def _profile(window: int) -> ModelProfile:
    return ModelProfile(
        provider="ollama",
        model="m",
        context_limit=window,
        context_window=window,
        max_output_tokens=min(4096, window // 2),
    )


def test_prompt_truncation_follows_the_window():
    """The same agent inlines more upstream context on a model with more room."""
    agent = get_agent(Phase.DEVOPS_ENGINEER.value)
    ctx = AgentContext(
        idea="A team standup bot",
        prior_outputs={d: {"summary": "x"} for d in agent.depends_on},
    )
    budgets = lambda w: agent._section_budgets(ctx, _profile(w))["depends_on"]  # noqa: E731
    assert budgets(32768) > budgets(8192)


def test_an_absent_section_does_not_reserve_room_it_will_never_use():
    """A share held back for a knowledge base nobody uploaded is window spent on nothing."""
    agent = get_agent(Phase.DEVOPS_ENGINEER.value)
    deps = {d: {"summary": "x"} for d in agent.depends_on}

    alone = agent._section_budgets(
        AgentContext(idea="A team standup bot", prior_outputs=deps), _profile(32768)
    )
    crowded = agent._section_budgets(
        AgentContext(
            idea="A team standup bot",
            prior_outputs=deps,
            rag_context="reference material",
            memory_context="a lesson from a past build",
        ),
        _profile(32768),
    )
    assert alone["rag"] == 0 and alone["memory"] == 0
    assert alone["depends_on"] > crowded["depends_on"]
    assert crowded["rag"] > 0 and crowded["memory"] > 0


@pytest.mark.parametrize("window", [32768, 16384, 8192, 4096])
def test_no_agent_can_build_a_prompt_that_overruns_its_window(window):
    """The invariant the whole fix rests on, at every size, for every agent.

    Ollama truncates an over-long prompt from the head, so one character past the
    budget is the system prompt starting to disappear — and the system prompt is
    where the required output shape is written. Every section is fed far more than
    it could ever be given, with and without reviewer feedback, and the assembled
    prompt still has to come in under the budget derived from the window.
    """
    from app.agents import AGENTS

    profile = _profile(window)
    for key, agent in AGENTS.items():
        for feedback in (None, "Tighten the scope." * 20):
            ctx = AgentContext(
                idea="A team standup bot " * 50,
                prior_outputs={
                    d: {"files": [{"code": "x" * 400_000}]} for d in agent.depends_on
                },
                rag_context="r" * 300_000,
                memory_context="m" * 300_000,
                extra_context="Debate decision: use Postgres.",
                feedback=feedback,
            )
            built = sum(len(m.content) for m in agent._build_messages(ctx, profile).messages)
            assert built <= profile.prompt_char_budget, (
                f"{key} built {built:,} chars against a {profile.prompt_char_budget:,} budget"
            )


def test_the_repair_round_still_fits_the_window():
    """The one call whose job is to restate the shape must not be cut from the head."""
    agent = get_agent(Phase.SECURITY_ENGINEER.value)
    profile = _profile(32768)
    ctx = AgentContext(
        idea="A team standup bot",
        prior_outputs={d: {"code": "x" * 200_000} for d in agent.depends_on},
    )

    # A rejected attempt as long as anything a model could return.
    messages = agent._repair_messages(
        ctx, profile, "y" * 400_000, ["`findings` — Field required"]
    ).messages
    total = sum(len(m.content) for m in messages)
    assert total <= profile.prompt_char_budget, (
        f"repair prompt is {total:,} chars against a {profile.prompt_char_budget:,} budget"
    )
    # And the system prompt — the thing carrying the shape — is still first.
    assert messages[0].role == "system"
    assert "findings" in messages[0].content


# ── 2. the declared shape is one declaration, read three ways ────────────────
@pytest.mark.parametrize("phase", [p.value for p in Phase])
def test_every_agent_declares_a_shape_that_prompt_and_schema_agree_on(phase):
    agent = get_agent(phase)
    schema = agent.response_schema()
    assert schema["type"] == "object"
    assert schema["required"], f"{phase} requires nothing — nothing can drift from that"
    # The sketch in the prompt is rendered from the same model, so every required key
    # is named in the instructions the model actually reads.
    spec = agent.output_spec
    for key in schema["required"]:
        assert f'"{key}"' in spec
    assert "$ref" not in json.dumps(schema)  # inlined, for grammar converters


@pytest.mark.parametrize(
    "model,drifted,canonical",
    [
        (
            SecurityEngineerOutput,
            "overallRiskAssessment",
            "risk_assessment",
        ),
        (QAEngineerOutput, "estimated_coverage", "coverage_estimate"),
        (QAEngineerOutput, "estimatedCoverage", "coverage_estimate"),
        (DevOpsEngineerOutput, "docker_compose", "compose_or_manifests"),
        (DevOpsEngineerOutput, "k8s_manifests", "compose_or_manifests"),
        (DevOpsEngineerOutput, "github_actions", "ci_cd"),
        (SecurityEngineerOutput, "security_findings", "findings"),
        (SecurityEngineerOutput, "vulnerabilities", "findings"),
        (CostEstimationOutput, "totalMonthlyHighUsd", "total_monthly_high_usd"),
        (CostEstimationOutput, "totalMonthlyLowUsd", "total_monthly_low_usd"),
    ],
)
def test_the_names_agents_actually_drifted_to_are_read_as_the_canonical_ones(
    model, drifted, canonical
):
    """Every rename here was observed in a real run against the live database."""
    payload = _minimal(model)
    payload[drifted] = payload.pop(canonical)
    assert model.model_validate(payload).model_dump(mode="json")[canonical]


def test_a_lone_finding_is_still_a_list_of_findings():
    payload = _minimal(SecurityEngineerOutput)
    payload["findings"] = payload["findings"][0]  # a dict where a list belongs
    out = SecurityEngineerOutput.model_validate(payload).model_dump(mode="json")
    assert isinstance(out["findings"], list) and len(out["findings"]) == 1


def test_money_written_as_prose_still_reads_as_money():
    payload = _minimal(CostEstimationOutput)
    payload["total_monthly_high_usd"] = "$1,240/month"
    out = CostEstimationOutput.model_validate(payload).model_dump(mode="json")
    assert out["total_monthly_high_usd"] == 1240.0


def test_a_losing_alias_is_kept_rather_than_dropped():
    """`AliasChoices` picks one name. The others are not thrown away.

    A model that writes both `docker_compose` and `k8s_manifests` has said two
    different things; the gate needs one canonical key, but `extra="allow"` means
    the other still reaches the file browser instead of vanishing on the way in.
    """
    payload = {
        **_minimal(DevOpsEngineerOutput),
        "docker_compose": [{"path": "compose.yml", "content": "c"}],
        "k8s_manifests": [{"path": "deploy.yaml", "content": "k"}],
    }
    payload.pop("compose_or_manifests")
    out = DevOpsEngineerOutput.model_validate(payload).model_dump(mode="json")
    assert [f["path"] for f in out["compose_or_manifests"]] == ["compose.yml"]
    assert out["k8s_manifests"] == [{"path": "deploy.yaml", "content": "k"}]


def test_extra_keys_survive_validation():
    """The shape is a floor. An agent that says more is not corrected into silence."""
    payload = {**_minimal(SecurityEngineerOutput), "threat_model": "STRIDE"}
    out = SecurityEngineerOutput.model_validate(payload).model_dump(mode="json")
    assert out["threat_model"] == "STRIDE"


# ── 3. validation, and one repair round ──────────────────────────────────────
class _ScriptedAgent(BaseAgent):
    """An agent whose model returns whatever the test queued, one reply per call.

    `_complete` is overridden, but `run()` still asks the router for a profile — so
    every test using this needs `stub_router`, or it reaches Ollama over HTTP and the
    budgets it computes depend on whatever the machine happens to have pulled.
    """

    key = "scripted"
    title = "Scripted"
    output_model = SecurityEngineerOutput

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list] = []

    def _complete(self, messages, ctx, options):
        from app.schemas.llm import LLMResponse, Usage

        self.calls.append(messages)
        return LLMResponse(
            text=self.replies.pop(0),
            provider="mock",
            model="mock",
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=5,
        )


def _ctx() -> AgentContext:
    return AgentContext(idea="A team standup bot", routing_mode=RoutingMode.LOCAL_ONLY)


def test_a_conforming_response_is_kept_as_is(stub_router):
    agent = _ScriptedAgent([json.dumps(_minimal(SecurityEngineerOutput))])
    result = agent.run(_ctx())
    assert result.schema_status == SchemaStatus.VALID.value
    assert result.repair_rounds == 0


def test_a_missing_required_key_triggers_a_repair_call_rather_than_being_persisted(stub_router):
    broken = _minimal(SecurityEngineerOutput)
    broken.pop("findings")
    agent = _ScriptedAgent([json.dumps(broken), json.dumps(_minimal(SecurityEngineerOutput))])

    result = agent.run(_ctx())

    assert len(agent.calls) == 2, "the drifted response was accepted without a repair round"
    assert result.schema_status == SchemaStatus.REPAIRED.value
    assert result.output["findings"], "the repaired output is what gets persisted"
    # The repair call names what was wrong, so the model has something to act on.
    assert "findings" in agent.calls[1][-1].content
    # And the phase reports what the whole exchange cost, not just the last call —
    # while still remembering that it *was* two calls, so analytics does not report
    # one call that took as long as two.
    assert result.response.usage.total_tokens == 60
    assert len(result.calls) == 2


def test_output_that_never_conforms_is_flagged_not_silently_used(stub_router):
    broken = json.dumps({"summary": "I could not do it"})
    agent = _ScriptedAgent([broken, broken])
    result = agent.run(_ctx())
    assert result.schema_status == SchemaStatus.INVALID.value
    assert result.schema_note


# ── 4. the gates, on the payloads that used to walk past them ────────────────
def test_the_cost_gate_fires_on_a_drifted_ledger_payload():
    """Ledger renamed its totals and the cap stopped being enforced. Not any more."""
    project = _Project(cap=100)

    drifted = {
        "summary": "…",
        "totalMonthlyHighUsd": 490,  # camelCase — the exact drift class observed
        "monthlyInfraCost": [{"item": "db", "high_usd": 400}],
    }
    assert projected_monthly_cost(drifted) == 490
    gate = decide_gate(project, Phase.COST_ESTIMATION.value, drifted)
    assert gate is not None and gate.kind == GateKind.COST.value
    assert "$490" in (gate.note or "")

    # No total at all: the line items are the answer, not zero.
    itemised = {
        "summary": "…",
        "monthly_infra_cost": [{"item": "db", "high_usd": 300}, {"item": "cdn", "low_usd": 90}],
        "api_or_third_party_cost": [{"item": "email", "monthly_usd": 100}],
    }
    assert projected_monthly_cost(itemised) == 490
    assert decide_gate(project, Phase.COST_ESTIMATION.value, itemised).kind == GateKind.COST.value


def test_the_security_gate_fires_on_a_drifted_warden_payload():
    project = _Project()
    drifted = {
        "summary": "…",
        # Renamed container, single finding not wrapped in a list, severity under
        # another name entirely. Any one of these used to be enough to lose the gate.
        "securityFindings": {"title": "SQL injection", "category": "SQLi", "risk": "Critical"},
    }
    assert len(severe_findings(drifted)) == 1
    gate = decide_gate(project, Phase.SECURITY_ENGINEER.value, drifted)
    assert gate is not None and gate.kind == GateKind.SECURITY.value
    assert "SQLi" in (gate.note or "")


def test_a_gate_whose_phase_failed_validation_stops_the_run():
    """Reading nothing is not the same as reading "nothing to worry about"."""
    project = _Project()
    unreadable = {"summary": "Here is my security review in prose."}

    passes = decide_gate(project, Phase.SECURITY_ENGINEER.value, unreadable)
    assert passes is None, "a valid-but-quiet report is not a reason to stop"

    stops = decide_gate(
        project,
        Phase.SECURITY_ENGINEER.value,
        unreadable,
        SchemaStatus.INVALID.value,
    )
    assert stops is not None
    # Not a SECURITY gate: that one announces a finding Warden made. This stop
    # happened because nobody could read the report, and the reviewer is told which.
    assert stops.kind == GateKind.UNCHECKED.value
    assert "could not run" in (stops.note or "")


def test_an_unreadable_cost_estimate_says_so_at_the_ship_review():
    project = _Project(cap=100)
    gate = decide_gate(
        project, Phase.COST_ESTIMATION.value, {"summary": "…"}, SchemaStatus.INVALID.value
    )
    assert gate is not None
    assert "could not be checked" in (gate.note or "")


_CRITICAL = {
    "title": "SQLi",
    "severity": "critical",
    "category": "SQL injection",
    "location": "users.py",
    "description": "d",
    "recommendation": "r",
}
_SEVERE = {"summary": "…", "findings": [_CRITICAL]}
_CLEAN = {"summary": "…", "findings": []}
_PRICEY = {"summary": "…", "total_monthly_high_usd": 490}
_CHEAP = {"summary": "…", "total_monthly_high_usd": 50}


@pytest.mark.parametrize(
    "mode,phase,output,status,expected",
    [
        # Unattended is an explicit choice about not being interrupted. Failing
        # closed is a checkpoints promise, not a licence to override the mode.
        (ApprovalMode.UNATTENDED, Phase.SECURITY_ENGINEER, _SEVERE, "valid", None),
        (ApprovalMode.UNATTENDED, Phase.SECURITY_ENGINEER, _CLEAN, "invalid", None),
        (ApprovalMode.UNATTENDED, Phase.COST_ESTIMATION, _PRICEY, "valid", None),
        # Every-phase keeps its rhythm whatever the output says.
        (ApprovalMode.EVERY_PHASE, Phase.BACKEND_ENGINEER, {}, "valid", GateKind.PHASE.value),
        (ApprovalMode.EVERY_PHASE, Phase.SECURITY_ENGINEER, _CLEAN, "invalid", GateKind.PHASE.value),
        # Checkpoints: two scheduled decisions…
        (ApprovalMode.CHECKPOINTS, Phase.SYSTEM_DESIGN, {}, "valid", GateKind.PLAN.value),
        (ApprovalMode.CHECKPOINTS, Phase.BACKEND_ENGINEER, {}, "valid", None),
        (ApprovalMode.CHECKPOINTS, Phase.COST_ESTIMATION, _CHEAP, "valid", GateKind.SHIP.value),
        # …plus what the run raises for itself.
        (ApprovalMode.CHECKPOINTS, Phase.SECURITY_ENGINEER, _SEVERE, "valid", GateKind.SECURITY.value),
        (ApprovalMode.CHECKPOINTS, Phase.SECURITY_ENGINEER, _CLEAN, "valid", None),
        (ApprovalMode.CHECKPOINTS, Phase.COST_ESTIMATION, _PRICEY, "valid", GateKind.COST.value),
        # …and, when a gate's own phase failed its shape, a stop that says so.
        (ApprovalMode.CHECKPOINTS, Phase.SECURITY_ENGINEER, _CLEAN, "invalid", GateKind.UNCHECKED.value),
        (ApprovalMode.CHECKPOINTS, Phase.COST_ESTIMATION, {}, "invalid", GateKind.UNCHECKED.value),
        # A finding it *could* read outranks "could not read": a known critical is
        # more actionable than an unreadable report.
        (ApprovalMode.CHECKPOINTS, Phase.SECURITY_ENGINEER, _SEVERE, "invalid", GateKind.SECURITY.value),
    ],
)
def test_the_whole_gate_policy_in_one_table(mode, phase, output, status, expected):
    """Every mode against every gated phase and both schema outcomes.

    The policy is small enough to state exhaustively, and a table is the only form
    in which "unattended never stops" and "a check that did not run stops the run"
    can be read as the single consistent rule they are.
    """
    project = _Project(mode=mode, cap=100 if phase == Phase.COST_ESTIMATION else None)
    gate = decide_gate(project, phase.value, output, status)
    assert (gate.kind if gate else None) == expected


def test_unattended_still_means_unattended():
    """Failing closed is a checkpoints-mode promise, not a licence to ignore the mode."""
    project = _Project(mode=ApprovalMode.UNATTENDED)
    assert (
        decide_gate(
            project, Phase.SECURITY_ENGINEER.value, {}, SchemaStatus.INVALID.value
        )
        is None
    )


# ── 5. what a review pass found, kept found ──────────────────────────────────
def test_a_setting_left_blank_does_not_take_the_app_down():
    """`.env.example` ships `OLLAMA_CONTEXT_CEILING=` and the README says to copy it."""
    from app.core.config import Settings

    assert Settings(_env_file=None, ollama_context_ceiling="").ollama_context_ceiling is None
    assert Settings(_env_file=None, ollama_context_ceiling="4096").ollama_context_ceiling == 4096


def test_a_null_does_not_mask_the_alias_that_holds_the_findings():
    """The nastiest shape of all: it used to validate as `valid` and lose a critical.

    `{"findings": null, "security_findings": [critical]}` — `AliasChoices` took the
    null because the key was present, the null became an empty list, and the gate read
    zero findings off output nothing had flagged as suspect.
    """
    critical = {
        "title": "SQLi",
        "severity": "critical",
        "category": "SQL injection",
        "location": "users.py",
        "description": "d",
        "recommendation": "r",
    }
    payload = {
        **_minimal(SecurityEngineerOutput),
        "findings": None,
        "security_findings": [critical],
    }
    out = SecurityEngineerOutput.model_validate(payload).model_dump(mode="json")
    assert len(out["findings"]) == 1
    assert len(severe_findings(out)) == 1
    assert decide_gate(_Project(), Phase.SECURITY_ENGINEER.value, out).kind == GateKind.SECURITY.value


def test_a_null_on_a_required_list_is_a_repair_not_an_empty_list():
    payload = {**_minimal(SecurityEngineerOutput), "findings": None}
    with pytest.raises(ValidationError):
        SecurityEngineerOutput.model_validate(payload)


def test_a_placeholder_zero_does_not_hide_the_real_total():
    """Grouping alias names and taking the first present reintroduced the bug.

    `{"total_monthly_high_usd": 0, "monthly_total_usd": 490}` — the zero is present,
    so a grouped lookup stops there, and a $490/month build passes a $100 cap. Every
    spelling has to be tried individually, in order, exactly as the original
    single-name loop did.
    """
    drifted = {"summary": "…", "total_monthly_high_usd": 0, "monthly_total_usd": 490}
    assert projected_monthly_cost(drifted) == 490
    gate = decide_gate(_Project(cap=100), Phase.COST_ESTIMATION.value, drifted)
    assert gate is not None and gate.kind == GateKind.COST.value


@pytest.mark.parametrize(
    "reported,expected",
    [
        ("$1,240/mo", 1240.0),
        ("about 490 USD per month", 490.0),
        ("free tier", None),
        (0, 0.0),
    ],
)
def test_the_gate_reads_money_a_model_wrapped_in_prose(reported, expected):
    """Validation coerces these on the way in — but the gate also reads phases that
    *failed* validation, and refusing to read "$1,240/mo" there is refusing to gate."""
    assert projected_monthly_cost({"summary": "…", "total_monthly_high_usd": reported}) == expected


def test_a_build_that_really_is_free_still_reads_as_free():
    """The zero fall-through must not turn "costs nothing" into "unknown"."""
    assert projected_monthly_cost(
        {"summary": "…", "total_monthly_high_usd": 0, "total_monthly_low_usd": 0}
    ) == 0.0


def test_an_empty_list_does_not_beat_the_alias_that_holds_the_findings():
    """The null fix, one value-type over — and just as fatal.

    A schema-constrained model *must* emit `findings`, so emitting it empty beside a
    populated `security_findings` is at least as likely as emitting it null.
    """
    critical = {
        "title": "SQLi",
        "severity": "critical",
        "category": "SQL injection",
        "location": "users.py",
        "description": "d",
        "recommendation": "r",
    }
    drifted = {"summary": "…", "findings": [], "security_findings": [critical]}
    assert len(severe_findings(drifted)) == 1

    payload = {**_minimal(SecurityEngineerOutput), **drifted}
    out = SecurityEngineerOutput.model_validate(payload).model_dump(mode="json")
    assert len(out["findings"]) == 1, "validation kept the empty list over the real one"
    assert decide_gate(_Project(), Phase.SECURITY_ENGINEER.value, out) is not None


def test_a_clean_security_review_is_still_allowed_to_report_nothing():
    """"No findings" is an answer, not a drift to be repaired."""
    payload = {**_minimal(SecurityEngineerOutput), "findings": []}
    out = SecurityEngineerOutput.model_validate(payload).model_dump(mode="json")
    assert out["findings"] == []
    assert severe_findings(out) == []
    assert decide_gate(_Project(), Phase.SECURITY_ENGINEER.value, out) is None


def test_a_blank_severity_does_not_hide_the_one_beside_it():
    finding = {"title": "SQLi", "category": "SQLi", "severity": "", "risk": "critical"}
    assert len(severe_findings({"findings": [finding]})) == 1


def test_the_repair_that_came_back_worse_is_not_the_one_that_is_kept(stub_router):
    """`run()` used to persist the last attempt while claiming it kept the best."""
    nearly = _minimal(SecurityEngineerOutput)
    nearly.pop("risk_assessment")  # one thing missing
    worse = {"summary": "I could not do it"}  # four things missing

    agent = _ScriptedAgent([json.dumps(nearly), json.dumps(worse)])
    result = agent.run(_ctx())

    assert result.schema_status == SchemaStatus.INVALID.value
    assert result.output.get("findings"), "the worse of the two attempts was persisted"
    assert "risk_assessment" in (result.schema_note or "")


def test_a_long_idea_cannot_push_the_prompt_out_of_the_window():
    """`idea` and `feedback` have no maximum length at the API."""
    agent = get_agent(Phase.PRODUCT_MANAGER.value)
    profile = _profile(8192)
    ctx = AgentContext(idea="i" * 60_000, feedback="f" * 40_000, extra_context="e" * 20_000)
    built = sum(len(m.content) for m in agent._build_messages(ctx, profile).messages)
    assert built <= profile.prompt_char_budget


def test_the_gate_reads_past_a_null_to_the_key_that_holds_the_number():
    drifted = {"summary": "…", "total_monthly_high_usd": None, "monthly_total_usd": 640}
    assert projected_monthly_cost(drifted) == 640


def test_a_configured_ceiling_is_not_overridden_by_a_floor(monkeypatch):
    """The knob `.env.example` documents has to actually lower the window."""
    from app.core import config
    from app.router import model_profile

    monkeypatch.setattr(model_profile.settings, "ollama_context_ceiling", 2048)
    window, reason = model_profile.resolve_window(
        context_limit=32768, kv_bytes_per_token=None, ram_bytes=None
    )
    assert window == 2048
    assert "OLLAMA_CONTEXT_CEILING" in (reason or "")


def test_a_ram_estimate_below_what_runs_is_floored_and_says_so(monkeypatch):
    """The RAM figure is an estimate blind to GPU offload, so it alone gets a floor.

    Sending a window nothing can run in fails more confusingly than a tight one, and
    the reason has to admit the estimate was overruled rather than imply comfort.
    """
    from app.router import model_profile

    monkeypatch.setattr(model_profile.settings, "ollama_context_ceiling", None)
    window, reason = model_profile.resolve_window(
        context_limit=32768,
        kv_bytes_per_token=57344,
        ram_bytes=128 * 2**20,  # 128 MiB — nothing fits
    )
    assert window == model_profile._MIN_WORKABLE_TOKENS
    assert "held up to" in (reason or "") and "smaller model" in (reason or "")


def test_the_floor_is_where_an_agent_prompt_actually_fits():
    """Not a number picked for looking round: measured against the real prompts."""
    from app.agents import AGENTS
    from app.router.model_profile import _MIN_WORKABLE_TOKENS

    profile = _profile(_MIN_WORKABLE_TOKENS)
    for key, agent in AGENTS.items():
        overhead = len(agent.system_prompt()) + len(agent.task_instruction())
        assert overhead < profile.prompt_char_budget, (
            f"{key} cannot fit its own instructions at the {_MIN_WORKABLE_TOKENS}-token floor"
        )


def test_a_user_set_ceiling_is_never_floored(monkeypatch):
    """A cap someone typed is a fact about what they want, not an estimate to correct."""
    from app.router import model_profile

    monkeypatch.setattr(model_profile.settings, "ollama_context_ceiling", 1024)
    window, reason = model_profile.resolve_window(
        context_limit=32768, kv_bytes_per_token=None, ram_bytes=None
    )
    assert window == 1024, "the floor overrode a ceiling the user set"
    assert "OLLAMA_CONTEXT_CEILING" in (reason or "")


def test_the_ram_clamp_is_skipped_when_ollama_is_on_another_machine():
    """This process's RAM says nothing about a KV cache allocated somewhere else."""
    from app.router.providers.ollama import OllamaProvider

    assert OllamaProvider("http://localhost:11434").is_same_machine()
    assert not OllamaProvider("http://ollama.internal:11434").is_same_machine()


@pytest.mark.parametrize(
    "text,expected",
    [("0.5", (0, 5, 0)), ("0.30.10", (0, 30, 10)), ("v0.30.1", (0, 30, 1)), ("unknown", ())],
)
def test_version_parsing_pads_and_refuses(text, expected):
    """`"0.5"` as `(0, 5)` compares below `(0, 5, 0)` — rejecting the first release
    that supports the feature being checked for."""
    from app.router.providers.ollama import _parse_version

    assert _parse_version(text) == expected


def test_an_unreadable_version_is_retried_rather_than_cached():
    from unittest.mock import patch

    from app.router.providers.ollama import OllamaProvider

    provider = OllamaProvider()
    with patch("httpx.get") as get:
        get.return_value.json.return_value = {"version": "unknown"}
        get.return_value.raise_for_status.return_value = None
        assert provider.server_version() is None
        assert provider.server_version() is None
        assert get.call_count == 2, "an unreadable version was cached and never retried"


# ── 6. what the third review pass found ──────────────────────────────────────
def test_the_example_env_agrees_with_the_code_it_configures():
    """The README says to copy this file, so a value in it is a value that ships.

    `APPROX_CHARS_PER_TOKEN` shipping at 3.5 while the code defaults to 3.0 is not a
    cosmetic mismatch: above ~3.2 the prompt budget exceeds the window it was sized
    for, and the head truncation this whole change exists to remove comes back for
    anyone who followed the setup instructions.
    """
    import re
    from pathlib import Path

    from app.core.config import Settings

    example = Path(__file__).resolve().parents[2] / ".env.example"
    defaults = Settings(_env_file=None)
    for line in example.read_text().splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
        if not match:
            continue
        name, raw = match.group(1).lower(), match.group(2).strip()
        if not raw or not hasattr(defaults, name):
            continue
        expected = getattr(defaults, name)
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            continue
        assert float(raw) == float(expected), (
            f".env.example ships {name.upper()}={raw}, but the code defaults to {expected}"
        )


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"totalMonthlyHighUsd": 490, "total_monthly_high_usd": 0}, 490.0),
        ({"total_monthly_high_usd": 0, "totalMonthlyHighUsd": 490}, 490.0),
        ({"total_monthly_high_usd": 0, "totalMonthlyHighUsd": 0}, 0.0),
        ({"total_monthly_high_usd": 0}, 0.0),
    ],
)
def test_two_spellings_of_one_key_do_not_depend_on_which_came_last(payload, expected):
    """Normalising keys collapses spellings, and a plain dict keeps the last one.

    Validation writes the canonical name beside the drifted one, so both routinely
    coexist — and a placeholder zero winning on insertion order alone is a $490
    build passing a $100 cap.
    """
    assert projected_monthly_cost({"summary": "…", **payload}) == expected


def test_a_consumed_alias_is_moved_not_copied():
    """`extra="allow"` would otherwise keep a second copy of the whole payload.

    For a file-carrying field that means the row stores every generated file twice,
    the UI renders the section twice, and the next agent is handed the duplicate as
    prior-phase context — spending the context budget on a verbatim copy.
    """
    payload = _minimal(DevOpsEngineerOutput)
    payload.pop("compose_or_manifests")
    payload["docker_compose"] = [{"path": "compose.yml", "content": "x"}]
    out = DevOpsEngineerOutput.model_validate(payload).model_dump(mode="json")
    assert out["compose_or_manifests"]
    assert "docker_compose" not in out


@pytest.mark.parametrize("reply", ["123", '"sorry"', "true", "[1,2,3]", "not json", ""])
def test_neither_json_extractor_dies_on_a_reply_that_is_not_an_object(reply):
    """One copy of this had been hardened and the other had not — which is the
    argument for there being one. The unfixed copy killed the Backend phase."""
    from app.agents.base import BaseAgent
    from app.orchestration.debate import _parse

    assert isinstance(BaseAgent._parse(reply), dict)
    assert isinstance(_parse(reply, "Which database?"), dict)


@pytest.mark.parametrize("window", [32768, 8192, 4096, 1024, 512])
def test_the_prompt_budget_never_exceeds_the_window_it_came_from(window):
    """The floor used to raise the budget *above* the room available: a 512-token
    ceiling produced 512 prompt + 256 output against a 512-token window."""
    profile = ModelProfile(
        provider="ollama",
        model="m",
        context_limit=window,
        context_window=window,
        max_output_tokens=max(1, min(4096, window // 2)),
    )
    assert profile.prompt_token_budget + profile.max_output_tokens <= window


def test_a_large_cloud_window_is_not_an_invitation_to_fill_it():
    """A 200k window would inline every prior phase in full into every later one —
    what the window allows, and about forty times the input cost per call."""
    from app.core.config import settings

    profile = ModelProfile(
        provider="anthropic",
        model="claude",
        context_limit=200_000,
        context_window=200_000,
        max_output_tokens=4096,
    )
    assert profile.prompt_token_budget <= settings.max_prompt_tokens


def test_a_stop_reports_every_reason_it_happened():
    """"Warden raised a critical" read alone invites fixing that one thing and
    moving on — when the report it came from failed its shape, and the findings
    that did not survive parsing are the ones nobody will go looking for."""
    critical = {
        "title": "SQLi",
        "severity": "critical",
        "category": "authz",
        "location": "x",
        "description": "d",
        "recommendation": "r",
    }
    gate = decide_gate(
        _Project(),
        Phase.SECURITY_ENGINEER.value,
        {"findings": [critical]},
        SchemaStatus.INVALID.value,
    )
    assert gate.kind == GateKind.SECURITY.value
    assert "raised 1 finding" in gate.note and "could not run" in gate.note


def test_an_empty_idea_is_not_reported_as_truncated():
    from app.agents.base import _clip

    assert _clip("", 0) == ""
    assert _clip("", 100) == ""


# ── helpers ──────────────────────────────────────────────────────────────────
def _minimal(model) -> dict:
    """A payload that satisfies `model`, built from the model's own schema."""
    from tests.conftest import _conforming
    from app.schemas.agent_outputs import response_schema

    return _conforming(response_schema(model))
