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
        weight_bytes=4_683_087_332,
        supports_schema_format=True,
        ram_bytes=64 * 2**30,  # plenty, so the model's own limit is the binding one
    )
    assert profile.context_limit == 32768
    assert profile.context_window == 32768
    assert profile.source == "probe"
    assert profile.parameter_size == "7.6B"
    # And the phase that used to break — 6,019 tokens — now fits with room to spare.
    assert profile.prompt_token_budget > 6019


def test_a_small_machine_clamps_the_window_instead_of_swapping():
    kv = kv_bytes_per_token(SHOW["model_info"], "qwen2")
    assert kv == 2 * 28 * 4 * (3584 // 28) * 2  # K+V × layers × kv-heads × head-dim × f16

    window, reason = resolve_window(
        context_limit=32768,
        kv_bytes_per_token=kv,
        weight_bytes=4_683_087_332,
        ram_bytes=8 * 2**30,
    )
    assert window < 32768
    assert reason and "RAM" in reason


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
        weight_bytes=1_000_000_000,
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
        OllamaProvider, "_weight_bytes", return_value=None
    ), patch.object(OllamaProvider, "server_version", return_value=version):
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


def test_prompt_truncation_follows_the_window(monkeypatch):
    """The same agent inlines more upstream context on a model with more room."""
    agent = get_agent(Phase.DEVOPS_ENGINEER.value)
    ctx = AgentContext(idea="A team standup bot", prior_outputs={})

    def budgets(window: int) -> int:
        profile = ModelProfile(
            provider="ollama",
            model="m",
            context_limit=window,
            context_window=window,
            max_output_tokens=min(4096, window // 2),
        )
        return agent._section_budgets(ctx, profile)["depends_on"]

    assert budgets(32768) > budgets(8192)


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
        (DevOpsEngineerOutput, "docker_compose", "compose_or_manifests"),
        (DevOpsEngineerOutput, "github_actions", "ci_cd"),
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
    """An agent whose model returns whatever the test queued, one reply per call."""

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


def test_a_conforming_response_is_kept_as_is():
    agent = _ScriptedAgent([json.dumps(_minimal(SecurityEngineerOutput))])
    result = agent.run(_ctx())
    assert result.schema_status == SchemaStatus.VALID.value
    assert result.repair_rounds == 0


def test_a_missing_required_key_triggers_a_repair_call_rather_than_being_persisted():
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


def test_output_that_never_conforms_is_flagged_not_silently_used():
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
    assert stops is not None and stops.kind == GateKind.SECURITY.value
    assert "could not run" in (stops.note or "")


def test_an_unreadable_cost_estimate_says_so_at_the_ship_review():
    project = _Project(cap=100)
    gate = decide_gate(
        project, Phase.COST_ESTIMATION.value, {"summary": "…"}, SchemaStatus.INVALID.value
    )
    assert gate is not None
    assert "could not be checked" in (gate.note or "")


def test_unattended_still_means_unattended():
    """Failing closed is a checkpoints-mode promise, not a licence to ignore the mode."""
    project = _Project(mode=ApprovalMode.UNATTENDED)
    assert (
        decide_gate(
            project, Phase.SECURITY_ENGINEER.value, {}, SchemaStatus.INVALID.value
        )
        is None
    )


# ── helpers ──────────────────────────────────────────────────────────────────
def _minimal(model) -> dict:
    """A payload that satisfies `model`, built from the model's own schema."""
    from tests.conftest import _conforming
    from app.schemas.agent_outputs import response_schema

    return _conforming(response_schema(model))
