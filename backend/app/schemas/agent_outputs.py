"""What each agent must return, declared as a type.

These models are the one place the shape of a deliverable is written down. Three
things read them, and because they read the *same* declaration they cannot disagree:

  1. the prompt — the shape shown to the model is rendered from the fields;
  2. decoding — the JSON Schema handed to the provider is derived from the same fields,
     so a model that supports constrained decoding cannot produce anything else;
  3. validation — the response is parsed back through the model, and a miss is a
     repair round rather than a key some gate quietly fails to find three phases later.

Two deliberate leniencies. `extra="allow"` keeps whatever else an agent thought worth
saying — the shape is a floor, not a cage. And several fields accept the names models
actually drifted to in real runs (`estimated_coverage` for `coverage_estimate`,
`docker_compose` for `compose_or_manifests`), normalising them back to the canonical
key on the way in. Renaming a field is the cheapest kind of drift to absorb and the
most expensive kind to discover downstream.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Optional, Union, get_args, get_origin

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field


# ── coercions ────────────────────────────────────────────────────────────────
def _as_list(value: Any) -> Any:
    """A lone item where a list belongs is the list. `None` is the empty one.

    Worth absorbing rather than rejecting because of where it bites: a Warden that
    reports its one critical finding as `"findings": {...}` instead of `[{...}]` is
    a run that sails past the security gate on a formatting detail.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _as_str_list(value: Any) -> Any:
    """The same, plus the two shapes models reach for instead of a list of strings."""
    if isinstance(value, dict):
        return [f"{k}: {v}" for k, v in value.items()]
    items = _as_list(value)
    return [item if isinstance(item, str) else str(item) for item in items]


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _as_number(value: Any) -> Any:
    """Read a number out of the prose models wrap money in: "$1,200/mo" -> 1200.0.

    A string that holds no number at all is passed through untouched, so validation
    fails and a repair round runs — better than inventing a zero for the cost cap to
    compare against.
    """
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, str):
        match = _NUMBER.search(value.replace(",", ""))
        if match:
            return float(match.group(0))
    return value


StrList = Annotated[list[str], BeforeValidator(_as_str_list)]
Number = Annotated[float, BeforeValidator(_as_number)]


class _Shape(BaseModel):
    """Base for every declared shape: strict about what is required, open to more."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


def _list_of(item: type) -> Any:
    """`list[item]`, tolerating a single object where a list was asked for."""
    return Annotated[list[item], BeforeValidator(_as_list)]


# ── Product Manager ──────────────────────────────────────────────────────────
class Feature(_Shape):
    name: str
    priority: str = Field(description="P0 | P1 | P2")
    description: str


class UserStory(_Shape):
    as_a: str
    i_want: str
    so_that: str
    acceptance_criteria: StrList


class Milestone(_Shape):
    milestone: str
    deliverables: StrList


class ProductManagerOutput(_Shape):
    product_name: str
    problem_statement: str
    target_users: StrList
    mvp_scope: StrList
    out_of_scope: StrList
    features: _list_of(Feature)
    user_stories: _list_of(UserStory)
    success_metrics: StrList
    roadmap: _list_of(Milestone)


# ── System Design ────────────────────────────────────────────────────────────
class TechStack(_Shape):
    frontend: StrList
    backend: StrList
    database: StrList
    infra: StrList = Field(default_factory=list)


class Component(_Shape):
    name: str
    responsibility: str


class Entity(_Shape):
    entity: str
    fields: StrList = Field(description="name:type")
    relationships: StrList = Field(default_factory=list)


class Endpoint(_Shape):
    method: str
    path: str
    purpose: str


class SystemDesignOutput(_Shape):
    architecture_overview: str
    tech_stack: TechStack
    components: _list_of(Component)
    data_model: _list_of(Entity)
    api_endpoints: _list_of(Endpoint)
    scaling_considerations: StrList
    architecture_diagram_mermaid: str = Field(description="a Mermaid flowchart definition")


# ── Backend / Frontend engineers ─────────────────────────────────────────────
class SourceFile(_Shape):
    path: str
    language: str
    purpose: str
    code: str


class BackendEngineerOutput(_Shape):
    framework: str = Field(description="e.g. FastAPI")
    summary: str
    db_models: str = Field(description="code")
    auth_flow: str
    files: _list_of(SourceFile)
    setup_instructions: StrList


class Page(_Shape):
    route: str
    purpose: str


class UiComponent(_Shape):
    name: str
    purpose: str


class FrontendEngineerOutput(_Shape):
    framework: str = Field(description="e.g. Next.js + React")
    summary: str
    pages: _list_of(Page)
    components: _list_of(UiComponent)
    state_management: str
    files: _list_of(SourceFile)


# ── QA ───────────────────────────────────────────────────────────────────────
class TestFile(_Shape):
    path: str
    framework: str
    targets: str
    code: str


class QAEngineerOutput(_Shape):
    summary: str
    test_strategy: str
    test_files: _list_of(TestFile)
    edge_cases: StrList
    #: Observed drift: models return `estimated_coverage`. Same number, other name.
    coverage_estimate: str = Field(
        validation_alias=AliasChoices(
            "coverage_estimate", "estimated_coverage", "estimatedCoverage"
        )
    )
    risks: StrList


# ── Security ─────────────────────────────────────────────────────────────────
class SecurityFinding(_Shape):
    title: str
    severity: str = Field(description="critical | high | medium | low")
    category: str = Field(description="e.g. SQL injection, XSS, CSRF, authz, secrets")
    location: str
    description: str
    recommendation: str


class SecurityEngineerOutput(_Shape):
    summary: str
    #: The security gate reads this list. Everything about this model exists so it
    #: is present, is a list, and each entry carries a severity.
    findings: _list_of(SecurityFinding) = Field(
        validation_alias=AliasChoices(
            "findings", "security_findings", "vulnerabilities", "securityFindings"
        )
    )
    secrets_check: str
    risk_assessment: str = Field(
        validation_alias=AliasChoices(
            "risk_assessment", "overall_risk_assessment", "overallRiskAssessment"
        )
    )
    overall_posture: str


# ── DevOps ───────────────────────────────────────────────────────────────────
class ConfigFile(_Shape):
    path: str
    content: str


class CiCdFile(_Shape):
    path: str
    tool: str
    content: str


class DevOpsEngineerOutput(_Shape):
    summary: str
    target_platform: str
    dockerfiles: _list_of(ConfigFile)
    #: Observed drift: `docker_compose` / `k8s_manifests` invented in place of this.
    compose_or_manifests: _list_of(ConfigFile) = Field(
        validation_alias=AliasChoices(
            "compose_or_manifests", "docker_compose", "k8s_manifests", "kubernetes_manifests"
        )
    )
    #: Observed drift: `github_actions`.
    ci_cd: _list_of(CiCdFile) = Field(
        validation_alias=AliasChoices("ci_cd", "github_actions", "ci_cd_pipelines", "cicd")
    )
    deployment_steps: StrList
    rollback_plan: str


# ── Cost estimation ──────────────────────────────────────────────────────────
class InfraCost(_Shape):
    item: str
    low_usd: Number
    high_usd: Number
    notes: str = ""


class ThirdPartyCost(_Shape):
    item: str
    monthly_usd: Number


class DevEffort(_Shape):
    role: str
    weeks: Number


class CostEstimationOutput(_Shape):
    summary: str
    monthly_infra_cost: _list_of(InfraCost)
    api_or_third_party_cost: _list_of(ThirdPartyCost)
    dev_effort: _list_of(DevEffort)
    estimated_timeline_weeks: Number
    #: The cost gate reads these two. Same reasoning as `findings` above.
    total_monthly_low_usd: Number = Field(
        validation_alias=AliasChoices("total_monthly_low_usd", "totalMonthlyLowUsd")
    )
    total_monthly_high_usd: Number = Field(
        validation_alias=AliasChoices("total_monthly_high_usd", "totalMonthlyHighUsd")
    )
    cost_optimization_tips: StrList


class GenericOutput(_Shape):
    """The floor for an agent that has not declared a shape: say something."""

    summary: str


# ── rendering the same declaration two ways ──────────────────────────────────
def response_schema(model: type[BaseModel]) -> dict:
    """A JSON Schema a provider can constrain decoding to.

    Generated in serialisation mode so property names are the canonical ones rather
    than the first drift alias, and `$ref`s are inlined because a schema-to-grammar
    converter is not obliged to resolve them.
    """
    schema = model.model_json_schema(mode="serialization")
    return _inline_refs(schema)


def _inline_refs(schema: dict) -> dict:
    """Replace every `#/$defs/...` reference with the shape it points at.

    Only the reference is touched. Nothing else is rewritten — in particular a
    property genuinely *named* `title` is a field a security finding needs, not the
    JSON Schema annotation of the same name.
    """
    defs = schema.pop("$defs", {})

    def walk(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, list):
            return [walk(item, seen) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.split("/")[-1]
            target = defs.get(name)
            if target is None or name in seen:
                # Unresolvable, or a shape that contains itself — an open object is
                # the honest stand-in; validation still has the real model.
                return {"type": "object"}
            sibling = {k: v for k, v in node.items() if k != "$ref"}
            return {**walk(target, seen | {name}), **sibling}
        return {key: walk(value, seen) for key, value in node.items()}

    return walk(schema, frozenset())


def shape_text(model: type[BaseModel]) -> str:
    """The same declaration as the JSON sketch shown to the model in its prompt.

    Rendered from the fields rather than written beside them, so the shape an agent
    is asked for and the shape its output is checked against cannot drift apart.
    """
    return _render_object(model, indent=1)


def _render_object(model: type[BaseModel], indent: int) -> str:
    pad = "  " * indent
    lines = ["{"]
    fields = list(model.model_fields.items())
    for i, (name, info) in enumerate(fields):
        comma = "" if i == len(fields) - 1 else ","
        lines.append(f'{pad}"{name}": {_render_annotation(info.annotation, info, indent)}{comma}')
    lines.append("  " * (indent - 1) + "}")
    return "\n".join(lines)


def _render_annotation(annotation: Any, info: Any, indent: int) -> str:
    description = getattr(info, "description", None)

    # `Annotated[...]` hides the real type one level down. Pydantic normally strips
    # it before this is reached; a raw annotation from elsewhere might not have been.
    if hasattr(annotation, "__metadata__"):
        return _render_annotation(annotation.__origin__, info, indent)

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Union:  # Optional[X] — render the shape, not the "or nothing".
        real = [a for a in args if a is not type(None)]
        if real:
            return _render_annotation(real[0], info, indent)
    if origin is list or origin is tuple:
        inner = args[0] if args else str
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            return f"[{_render_object(inner, indent + 1)}]"
        return '["string"]'
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _render_object(annotation, indent + 1)
    if annotation in (int, float):
        return f"0 ({description})" if description else "0"
    return f'"string ({description})"' if description else '"string"'
