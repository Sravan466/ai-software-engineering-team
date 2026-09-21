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
    # The declared defaults, not an instance of them. `Settings(_env_file=None)` skips
    # the .env file but still reads the *environment*, so a developer who exported one
    # of these knobs saw this test report that `.env.example` disagreed with the code —
    # naming the two files that were in fact identical.
    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    for line in example.read_text().splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
        if not match:
            continue
        name, raw = match.group(1).lower(), match.group(2).strip()
        if not raw or name not in defaults:
            continue
        expected = defaults[name]
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


# ── the RAM clamp belongs to the machine the runtime is actually on ──────────
def test_a_runtime_on_another_machine_is_not_clamped_by_this_machines_ram(monkeypatch):
    """`profile()`, not `is_same_machine()` — the bug lived one call past the check.

    The provider already answered "unknown RAM" for a runtime it does not share a
    machine with. `build_profile` then read `None` as "not passed" and substituted
    this host's memory, so the window was clamped by a number about the wrong
    computer. Only going through `profile()` catches that; the host check on its own
    passed before the fix and after it.
    """
    from app.router import model_profile
    from app.router.providers import ollama as ollama_module
    from app.router.providers.ollama import OllamaProvider

    monkeypatch.setattr(model_profile.settings, "ollama_context_ceiling", None)
    # Far too little for a 32k window of this model's KV cache — so if this figure
    # is consulted at all, the assertion below fails.
    monkeypatch.setattr(model_profile, "total_ram_bytes", lambda: 2 * 2**30)
    monkeypatch.setattr(ollama_module, "total_ram_bytes", lambda: 2 * 2**30)

    def _probed(base_url: str):
        prov = OllamaProvider(base_url)
        monkeypatch.setattr(prov, "_show", lambda model, **_: SHOW)
        monkeypatch.setattr(prov, "server_version", lambda: (0, 5, 0))
        return prov.profile("qwen2.5:7b")

    remote = _probed("http://ollama.internal:11434")
    assert remote.context_window == 32768, "clamped by RAM belonging to another computer"
    assert remote.clamp_reason is None

    # The same model on this machine still is clamped. The fix is about *which*
    # machine gets asked, not about dropping the clamp.
    local = _probed("http://localhost:11434")
    assert local.context_window < 32768
    assert "RAM" in (local.clamp_reason or "")


def test_unknown_ram_is_not_silently_replaced_with_this_machines(monkeypatch):
    """The unit underneath: `None` means "do not clamp", never "go and look"."""
    from app.router import model_profile

    monkeypatch.setattr(model_profile.settings, "ollama_context_ceiling", None)
    monkeypatch.setattr(model_profile, "total_ram_bytes", lambda: 2 * 2**30)
    profile = build_profile(
        provider="ollama",
        model="qwen2.5:7b",
        show=SHOW,
        supports_schema_format=True,
        ram_bytes=None,
    )
    assert profile.context_window == 32768
    assert profile.clamp_reason is None


# ── a model that cannot complete text is not something to run a build on ─────
EMBEDDING_SHOW = {
    "capabilities": ["embedding"],
    "details": {"family": "nomic-bert", "parameter_size": "137M"},
    "model_info": {"general.architecture": "nomic-bert", "nomic-bert.context_length": 2048},
}


def _stubbed_local(monkeypatch, models: dict, *, tags_report: bool = False):
    """Point the router's local provider at a fixed runtime.

    Only the HTTP edge is stubbed. `available()`, `list_models()` and `_tags()` run
    for real, so a test exercises the caching the tag list does in production rather
    than a copy of it; and `/api/show` resolves a name the way the runtime does, so
    `nomic-embed-text` finds `nomic-embed-text:latest`. `tags_report` is whether the
    tag list carries capabilities (a recent runtime) or leaves them to `/api/show`.
    """
    from app.router.model_profile import ProfileCache
    from app.router.providers import ollama as ollama_module
    from app.router.providers.ollama import _spellings
    from app.router.router import router as model_router

    prov = model_router._providers["ollama"]

    class _Tags:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "models": [
                    {
                        "name": name,
                        **(
                            {"capabilities": show["capabilities"]}
                            if tags_report and "capabilities" in show
                            else {}
                        ),
                    }
                    for name, show in models.items()
                ]
            }

    def _show(model, **_):
        for name in _spellings(model):
            if name in models:
                return models[name]
        return None

    monkeypatch.setattr(ollama_module.httpx, "get", lambda url, **_: _Tags())
    monkeypatch.setattr(prov, "_show", _show)
    monkeypatch.setattr(prov, "server_version", lambda: (0, 5, 0))
    # Swapped rather than emptied: the provider is a process-wide singleton, and
    # monkeypatch puts the real caches back afterwards — so these stubbed answers
    # cannot outlive the test that asked for them.
    monkeypatch.setattr(prov, "_capabilities", {})
    monkeypatch.setattr(prov, "_profiles", ProfileCache())
    return model_router


def test_local_status_says_what_each_pulled_model_can_do(monkeypatch):
    """The tag list cannot tell a chat model from an embedding one. This can."""
    model_router = _stubbed_local(
        monkeypatch, {"qwen2.5:7b": SHOW, "nomic-embed-text": EMBEDDING_SHOW}
    )
    status = model_router.local_status()
    caps = status["model_capabilities"]
    assert caps["nomic-embed-text"] == ["embedding"]
    assert "completion" in caps["qwen2.5:7b"]
    # The verdict is decided here, once — the pages read it rather than re-derive it.
    assert status["cannot_build"] == ["nomic-embed-text"]
    # The same view reaches the per-role picker, so the two cannot disagree.
    roles = model_router.role_settings()
    assert roles["model_capabilities"] == caps
    assert roles["cannot_build"] == status["cannot_build"]


def test_a_runtime_that_will_not_say_leaves_the_model_unlisted(monkeypatch):
    """"Did not report" is not "reported nothing", and only one hides a model.

    An Ollama old enough not to return `capabilities` would otherwise have every
    model it serves read as incapable — and a picker applying the rule would go
    empty on the runtimes least able to explain why.
    """
    silent = {"details": {"family": "llama"}, "model_info": {}}
    model_router = _stubbed_local(monkeypatch, {"llama3.1:8b": silent})
    assert model_router.local_status()["model_capabilities"] == {}


def test_a_failed_probe_is_left_alone_briefly_then_asked_again(monkeypatch):
    """A broken blob must not cost every poll a timeout; a new pull must not wait long.

    The failure is remembered as *unknown* — never as a verdict — for a short while,
    then forgotten, so the model is asked again on the next call after that.
    """
    from app.router.providers import ollama as ollama_module
    from app.router.providers.ollama import OllamaProvider

    prov = OllamaProvider("http://localhost:11434")
    asked: list[str] = []

    def _show(model, **_):
        asked.append(model)
        return None if len(asked) == 1 else EMBEDDING_SHOW

    clock = [1000.0]
    monkeypatch.setattr(ollama_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(prov, "_show", _show)

    assert prov.capabilities("nomic-embed-text") is None
    assert prov.capabilities("nomic-embed-text") is None
    assert asked == ["nomic-embed-text"], "a failed probe was repeated on the next poll"

    clock[0] += ollama_module._FAILED_PROBE_TTL_SECONDS + 1
    assert prov.capabilities("nomic-embed-text") == ("embedding",)
    assert len(asked) == 2


def test_the_tag_list_answers_for_every_model_in_one_round_trip(monkeypatch):
    """A server that reports capabilities in `/api/tags` is not probed again.

    `local_status` is polled by the sidebar. One `/api/show` per pulled model
    behind every poll is a page that waits on a list it already had — so the real
    HTTP call is stubbed here rather than the method, to prove the tag list itself
    is what fills the answer.
    """
    from app.router.providers import ollama as ollama_module
    from app.router.providers.ollama import OllamaProvider

    class _Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "models": [
                    {"name": "qwen2.5:7b", "capabilities": ["completion", "tools"]},
                    {"name": "nomic-embed-text:latest", "capabilities": ["embedding"]},
                ]
            }

    calls: list[str] = []

    def _get(url, **_):
        calls.append(url)
        return _Response()

    monkeypatch.setattr(ollama_module.httpx, "get", _get)

    def _refuse(*a, **k):
        raise AssertionError("a model was probed although the tag list said what it does")

    monkeypatch.setattr(ollama_module.httpx, "post", _refuse)

    prov = OllamaProvider("http://localhost:11434")
    assert prov.list_models() == ["qwen2.5:7b", "nomic-embed-text:latest"]
    assert prov.capabilities("nomic-embed-text:latest") == ("embedding",)
    assert prov.capabilities("qwen2.5:7b") == ("completion", "tools")
    assert calls == ["http://localhost:11434/api/tags"], "the answer cost more than one call"


def test_a_tag_list_that_omits_capabilities_still_gets_probed(monkeypatch):
    """Silence in `/api/tags` must not be cached as an answer.

    An older server does not report capabilities there but does on `/api/show`.
    Remembering the missing key as "reported nothing" would hide every model it
    serves behind a probe that never runs.
    """
    from app.router.providers.ollama import OllamaProvider

    prov = OllamaProvider("http://localhost:11434")
    monkeypatch.setattr(prov, "_tags", lambda: [{"name": "llama3.1:8b"}])
    monkeypatch.setattr(prov, "_show", lambda model, **_: {"capabilities": ["completion"]})
    assert prov.list_models() == ["llama3.1:8b"]
    assert prov.capabilities("llama3.1:8b") == ("completion",)


def test_the_default_model_is_a_key_even_when_the_tag_list_spells_it_differently(
    monkeypatch,
):
    """`OLLAMA_MODEL=nomic-embed-text`, tag list says `nomic-embed-text:latest`.

    The UI looks the default up by the string this same payload handed it. Keyed
    only by tag, that lookup misses and reads as "unknown" — which is the answer
    that lets the run start, quietly switching off the check that exists to stop
    a build on a model that cannot write.
    """
    model_router = _stubbed_local(
        monkeypatch,
        {"qwen2.5:7b": SHOW, "nomic-embed-text:latest": EMBEDDING_SHOW},
        tags_report=True,
    )
    monkeypatch.setitem(model_router._default_model, "ollama", "nomic-embed-text")

    status = model_router.local_status()
    assert status["default_model"] == "nomic-embed-text"
    assert status["model_capabilities"]["nomic-embed-text"] == ["embedding"]
    assert "nomic-embed-text" in status["cannot_build"]


# ── what the pre-merge review of #40 found ───────────────────────────────────
def test_a_runtime_that_is_down_costs_the_settings_page_no_probe(monkeypatch):
    """Nothing is pulled and nothing is answering, so nothing may be asked.

    Asking about the configured default here spent a probe timeout on every
    Settings poll whenever the runtime was down — the case `local_status` was
    already written to keep to a single wait.
    """
    from app.router.model_profile import ProfileCache
    from app.router.router import router as model_router

    prov = model_router._providers["ollama"]

    def _refuse(model, **_):
        raise AssertionError(f"probed '{model}' although the runtime is down")

    monkeypatch.setattr(prov, "available", lambda: False)
    monkeypatch.setattr(prov, "list_models", lambda: [])
    monkeypatch.setattr(prov, "_show", _refuse)
    monkeypatch.setattr(prov, "_capabilities", {})
    monkeypatch.setattr(prov, "_profiles", ProfileCache())

    status = model_router.local_status()
    assert status["reachable"] is False
    assert status["model_capabilities"] == {} and status["cannot_build"] == []
    assert model_router.role_settings()["cannot_build"] == []


def test_models_the_tag_list_did_not_describe_are_probed_side_by_side(monkeypatch):
    """Ten models on an older runtime cost one probe's wait, not ten in a row."""
    import threading

    shows = {f"model-{i}:latest": SHOW for i in range(3)}
    model_router = _stubbed_local(monkeypatch, shows)  # tags do not report
    prov = model_router._providers["ollama"]

    together = threading.Barrier(len(shows), timeout=5)
    serial: list[str] = []

    def _show(model, **_):
        try:
            together.wait()
        except threading.BrokenBarrierError:
            serial.append(model)
        return shows.get(model)

    monkeypatch.setattr(prov, "_show", _show)
    found = prov.capabilities_for(list(shows))
    assert not serial, "capability probes ran one after another"
    assert set(found) == set(shows)


def test_readiness_refuses_a_model_that_cannot_write(monkeypatch):
    """The guard that holds for every path to `/run`, not only the one page.

    A default set in `.env`, a role pinned through the API, a resume — none of them
    pass through the picker, and each started a build that died on its first call.
    """
    from app.core.constants import RoutingMode

    model_router = _stubbed_local(
        monkeypatch,
        {"qwen2.5:7b": SHOW, "nomic-embed-text:latest": EMBEDDING_SHOW},
        tags_report=True,
    )
    monkeypatch.setitem(model_router._default_model, "ollama", "nomic-embed-text")

    ready = model_router.readiness(RoutingMode.LOCAL_ONLY, None, roles=["product_manager"])
    assert not ready.ok
    assert ready.incapable and ready.incapable[0]["model"] == "nomic-embed-text"
    assert "cannot write" in (ready.reason or "") and "embedding" in (ready.reason or "")
    assert "ollama" not in (ready.reason or "").lower(), "#26: no runtime name in copy"

    # And a model that writes is let through by the same check.
    monkeypatch.setitem(model_router._default_model, "ollama", "qwen2.5:7b")
    assert model_router.readiness(RoutingMode.LOCAL_ONLY, None, roles=["product_manager"]).ok


@pytest.mark.parametrize("reported", [[], ["tools"]])
def test_a_list_without_completion_or_embedding_is_could_not_tell(monkeypatch, reported):
    """Only positive evidence is a "cannot write".

    The runtime decides `completion` against `embedding` by reading the model file.
    When the read fails it reports neither, leaving `[]`, or `["tools"]` from the
    template — a working chat model the runtime merely could not describe. Refusing
    it would block every build on a model that writes perfectly well.
    """
    odd = {**SHOW, "capabilities": reported}
    model_router = _stubbed_local(
        monkeypatch,
        {"qwen2.5:7b": SHOW, "odd:latest": odd, "nomic-embed-text:latest": EMBEDDING_SHOW},
        tags_report=True,
    )
    status = model_router.local_status()
    assert status["model_capabilities"]["odd:latest"] == reported
    assert status["cannot_build"] == ["nomic-embed-text:latest"]


def test_choosing_a_default_does_not_forget_what_every_model_can_do(monkeypatch):
    """A new default changes which model runs, not what any model can do."""
    model_router = _stubbed_local(
        monkeypatch, {"qwen2.5:7b": SHOW, "nomic-embed-text:latest": EMBEDDING_SHOW}
    )
    prov = model_router._providers["ollama"]
    prov.capabilities_for(prov.list_models())
    remembered = dict(prov._capabilities)
    assert remembered

    prov.forget_profile()  # what `_apply` does when the default changes
    assert prov._capabilities == remembered


def test_forgetting_a_re_pulled_model_forgets_every_spelling_of_it(monkeypatch):
    """The pull route passes `llama3.1`; the tag list cached `llama3.1:latest`."""
    from app.router.providers.ollama import OllamaProvider

    prov = OllamaProvider("http://localhost:11434")
    for name in ("llama3.1:latest", "qwen2.5:7b"):
        prov._remember_capabilities(name, {"capabilities": ["completion"]})
        prov._profiles.put((prov.base_url, name), _profile(8192))

    prov.forget_profile("llama3.1")
    assert prov.known_capabilities("llama3.1:latest") is None
    assert prov._profiles.get((prov.base_url, "llama3.1:latest")) is None, (
        "the profile a re-pull made stale was kept under the other spelling"
    )
    assert prov.known_capabilities("qwen2.5:7b") == ("completion",), "forgot an unrelated model"
    assert prov._profiles.get((prov.base_url, "qwen2.5:7b")) is not None


@pytest.mark.parametrize(
    "name,spellings",
    [
        ("llama3.1", ("llama3.1", "llama3.1:latest")),
        ("llama3.1:latest", ("llama3.1:latest", "llama3.1")),
        ("qwen2.5:7b", ("qwen2.5:7b",)),
        # A colon inside a registry host:port is not a tag.
        ("registry:5000/team/model", ("registry:5000/team/model", "registry:5000/team/model:latest")),
    ],
)
def test_a_model_name_is_known_by_each_way_the_runtime_spells_it(name, spellings):
    from app.router.providers.ollama import _spellings

    assert _spellings(name) == spellings


# ── what the second pre-merge review of #40 found ─────────────────────────────
def test_a_runtime_in_a_sibling_container_is_still_clamped_by_this_hosts_ram(monkeypatch):
    """docker compose: `http://ollama:11434` is another hostname, not another computer.

    Inferring "remote" from the name left the shipped Docker setup unclamped, sending
    a model's full trained window to a host that cannot hold its KV cache. The
    configured answer wins over the hostname.
    """
    from app.router import model_profile
    from app.router.providers import ollama as ollama_module
    from app.router.providers.ollama import OllamaProvider

    monkeypatch.setattr(model_profile.settings, "ollama_context_ceiling", None)
    monkeypatch.setattr(ollama_module, "total_ram_bytes", lambda: 2 * 2**30)

    def _probed():
        prov = OllamaProvider("http://ollama:11434")
        monkeypatch.setattr(prov, "_show", lambda model, **_: SHOW)
        monkeypatch.setattr(prov, "server_version", lambda: (0, 5, 0))
        return prov.profile("qwen2.5:7b")

    monkeypatch.setattr(ollama_module.settings, "ollama_same_machine", True)
    assert _probed().context_window < 32768, "a same-host container went unclamped"

    monkeypatch.setattr(ollama_module.settings, "ollama_same_machine", None)
    assert _probed().context_window == 32768, "unset must keep inferring from the address"


def test_the_docker_compose_backend_declares_its_runtime_on_the_same_host():
    """The shipped deployment has to actually set the knob, or the fix is not on."""
    import pathlib

    compose = (pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    assert "OLLAMA_BASE_URL: http://ollama:11434" in compose
    assert 'OLLAMA_SAME_MACHINE: "true"' in compose


@pytest.mark.parametrize("mode", ["auto", "local_only"])
def test_a_default_that_cannot_write_blocks_even_with_every_agent_pinned(monkeypatch, mode):
    """The contract the composer and the Settings notice have to mirror.

    Readiness always resolves "the rest of the run" to the default, and Auto's chain
    is headed by it whatever cloud keys exist. So neither a cloud key nor pinning
    every agent gets a run past a default that cannot write — which is why the UI no
    longer suggests either.
    """
    from app.core import model_roles
    from app.core.constants import RoutingMode

    model_router = _stubbed_local(
        monkeypatch,
        {"qwen2.5:7b": SHOW, "nomic-embed-text:latest": EMBEDDING_SHOW},
        tags_report=True,
    )
    monkeypatch.setitem(model_router._default_model, "ollama", "nomic-embed-text:latest")
    roles = ["product_manager", "system_design"]
    monkeypatch.setattr(model_roles, "get", lambda role: "ollama:qwen2.5:7b" if role in roles else None)

    ready = model_router.readiness(RoutingMode(mode), None, roles=roles)
    assert not ready.ok
    assert [m["role"] for m in ready.incapable] == ["the rest of the run"]


def test_an_answer_from_api_show_is_re_read_once_it_is_stale(monkeypatch):
    """`ollama create mymodel` from the CLI must not leave the old verdict forever."""
    from app.router.providers import ollama as ollama_module
    from app.router.providers.ollama import OllamaProvider

    prov = OllamaProvider("http://localhost:11434")
    answers = [EMBEDDING_SHOW, SHOW]
    clock = [1000.0]
    monkeypatch.setattr(ollama_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(prov, "_show", lambda model, **_: answers.pop(0))

    assert prov.writes(prov.capabilities("mymodel")) is False
    clock[0] += ollama_module._CAPABILITY_TTL_SECONDS - 1
    assert prov.writes(prov.capabilities("mymodel")) is False, "re-probed while still fresh"
    clock[0] += 2
    assert prov.writes(prov.capabilities("mymodel")) is True, "a stale verdict kept blocking"


@pytest.mark.parametrize(
    "configured,pulled,present",
    [
        ("nomic-embed-text", ["nomic-embed-text:latest"], True),
        ("nomic-embed-text:latest", ["nomic-embed-text:latest"], True),
        # The runtime resolves an untagged name to `:latest` only — so a different
        # tag of the same model is *not* this model, and a run on it would 404.
        ("nomic-embed-text", ["nomic-embed-text:v1.5"], False),
        ("qwen2.5", ["qwen2.5:7b"], False),
    ],
)
def test_presence_uses_the_runtimes_naming_rule(configured, pulled, present):
    from app.router.providers.ollama import OllamaProvider

    assert OllamaProvider.resolves(configured, pulled) is present


# ── what the third pre-merge review of #40 found ──────────────────────────────
def test_the_preflight_gives_the_answer_run_will_give(monkeypatch):
    """The composer asks this instead of re-deriving the rules, so it must agree."""
    from fastapi.testclient import TestClient

    from app.main import app

    model_router = _stubbed_local(
        monkeypatch,
        {"qwen2.5:7b": SHOW, "nomic-embed-text:latest": EMBEDDING_SHOW},
        tags_report=True,
    )
    with TestClient(app) as client:
        monkeypatch.setitem(model_router._default_model, "ollama", "nomic-embed-text")
        refused = client.post("/api/projects/preflight", json={"routing_mode": "local_only"}).json()
        assert refused["ok"] is False and "cannot write" in refused["reason"]

        monkeypatch.setitem(model_router._default_model, "ollama", "qwen2.5:7b")
        assert client.post("/api/projects/preflight", json={"routing_mode": "auto"}).json()["ok"]

        bad = client.post("/api/projects/preflight", json={"routing_mode": "sideways"})
        assert bad.status_code == 400


def test_a_model_that_cannot_write_is_refused_when_it_is_chosen(monkeypatch):
    """Accepted, it becomes a setting every later build is refused on, far from the choice."""
    from app.core import model_roles

    model_router = _stubbed_local(
        monkeypatch,
        {"qwen2.5:7b": SHOW, "nomic-embed-text:latest": EMBEDDING_SHOW},
        tags_report=True,
    )
    # Both raise before anything is persisted — neither reaches the settings files.
    with pytest.raises(ValueError, match="can't be the default"):
        model_router.set_default_model("ollama", "nomic-embed-text")
    with pytest.raises(ValueError, match="can't be an agent's model"):
        model_router.set_role_model("backend_engineer", "nomic-embed-text:latest")

    # Embeddings is the one role an embedding model is the right answer for.
    recorded: list = []
    monkeypatch.setattr(model_roles, "set_role", lambda role, spec: recorded.append((role, spec)))
    model_router.set_role_model("embeddings", "nomic-embed-text:latest")
    assert recorded == [("embeddings", "nomic-embed-text:latest")]


def test_a_cached_verdict_is_not_reported_while_the_runtime_is_down(monkeypatch):
    """Down, Auto sends the run to the cloud — a stale local verdict must not block it."""
    model_router = _stubbed_local(
        monkeypatch,
        {"qwen2.5:7b": SHOW, "nomic-embed-text:latest": EMBEDDING_SHOW},
        tags_report=True,
    )
    monkeypatch.setitem(model_router._default_model, "ollama", "nomic-embed-text")
    assert "nomic-embed-text" in model_router.local_status()["cannot_build"]  # primes the cache

    prov = model_router._providers["ollama"]
    monkeypatch.setattr(prov, "available", lambda: False)
    monkeypatch.setattr(prov, "list_models", lambda: [])
    status = model_router.local_status()
    assert status["reachable"] is False
    assert status["cannot_build"] == [], "a cached verdict outlived the runtime it was about"
    assert model_router.role_settings()["cannot_build"] == []


def test_one_model_is_remembered_once_however_it_is_spelled(monkeypatch):
    """A failed probe under one spelling must not hide the tag list's answer under another."""
    from app.router.providers import ollama as ollama_module
    from app.router.providers.ollama import OllamaProvider

    prov = OllamaProvider("http://localhost:11434")
    monkeypatch.setattr(prov, "_show", lambda model, **_: None)  # the runtime is busy
    assert prov.capabilities("nomic-embed-text") is None  # remembered as a failure

    # The tag list then reports it under its full name.
    prov._remember_capabilities("nomic-embed-text:latest", {"capabilities": ["embedding"]})
    assert prov.capabilities("nomic-embed-text") == ("embedding",), (
        "a stale failure under the short spelling hid the fresh answer"
    )
    assert len(prov._capabilities) == 1
    assert ollama_module._canonical("nomic-embed-text") == "nomic-embed-text:latest"


def test_readiness_reads_the_tag_list_once_however_many_models(monkeypatch):
    """One fetch per model asked about was a timeout each when the runtime was slow."""
    from app.core import model_roles
    from app.core.constants import RoutingMode
    from app.router.providers import ollama as ollama_module

    shows = {"qwen2.5:7b": SHOW, "llama3.1:8b": SHOW, "mistral:7b": SHOW}
    model_router = _stubbed_local(monkeypatch, shows, tags_report=True)
    pins = {"product_manager": "llama3.1:8b", "system_design": "mistral:7b"}
    monkeypatch.setattr(model_roles, "get", lambda role: pins.get(role))

    tag_reads: list[str] = []
    real_get = ollama_module.httpx.get

    def _counting_get(url, **kw):
        tag_reads.append(url)
        return real_get(url, **kw)

    monkeypatch.setattr(ollama_module.httpx, "get", _counting_get)
    assert model_router.readiness(RoutingMode.LOCAL_ONLY, None, roles=list(pins)).ok
    # One for reachability, one for the list — not one more per model.
    assert len(tag_reads) == 2, tag_reads
