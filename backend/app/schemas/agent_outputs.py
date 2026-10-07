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
actually drifted to in real runs (`docker_compose` for `compose_or_manifests`,
`security_findings` for `findings`), normalising them back to the canonical
key on the way in. Renaming a field is the cheapest kind of drift to absorb and the
most expensive kind to discover downstream.
"""
from __future__ import annotations

from typing import Annotated, Any, Optional, Union, get_args, get_origin

from app.core.reading import as_number, has_content
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)


# ── coercions ────────────────────────────────────────────────────────────────
def _as_list(value: Any) -> Any:
    """A lone item where a list belongs is the list.

    Worth absorbing rather than rejecting because of where it bites: a Warden that
    reports its one critical finding as `"findings": {...}` instead of `[{...}]` is
    a run that sails past the security gate on a formatting detail.

    `None` is emphatically **not** absorbed. Turning a null into an empty list is how
    "the model reported nothing here" becomes "the model reported that there is
    nothing here" — and those are the two readings the security gate must never
    confuse. A null fails validation and earns a repair round instead.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _as_str_list(value: Any) -> Any:
    """The same, plus the two shapes models reach for instead of a list of strings."""
    if value is None:
        return None
    if isinstance(value, dict):
        return [f"{k}: {v}" for k, v in value.items()]
    items = _as_list(value)
    return [item if isinstance(item, str) else str(item) for item in items]


def _as_number(value: Any) -> Any:
    """Read a number out of the prose models wrap money in: "$1,240/mo" -> 1200.0.

    A string that holds no number at all is passed through untouched, so validation
    fails and a repair round runs — better than inventing a zero for the cost cap to
    compare against. See `core.reading`.
    """
    if isinstance(value, str):
        number = as_number(value)
        return value if number is None else number
    return value


StrList = Annotated[list[str], BeforeValidator(_as_str_list)]
Number = Annotated[float, BeforeValidator(_as_number)]


class _Shape(BaseModel):
    """Base for every declared shape: strict about what is required, open to more."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _prefer_the_alias_that_answers(cls, data: Any) -> Any:
        """Pick, among the names for one field, whichever actually carries the answer.

        `AliasChoices` takes the first alias that is *present*. That is the wrong test
        twice over, and both wrongs are security properties here:

          `{"findings": null,  "security_findings": [critical]}`
          `{"findings": [],    "security_findings": [critical]}`

        In both, the canonical key wins on presence alone, the populated alias survives
        only as an ignored extra, and the list the gate reads comes back empty — on
        output marked `valid`, so the fail-closed path never fires either. A critical
        vulnerability disappears because a model wrote its answer under a second name.

        So an empty value never beats a populated sibling. It is still *kept* when no
        sibling answers, because "no findings" and "no cost" are real answers and must
        not be turned into a repair round. Nulls are dropped outright: a null on a
        field with a default means take the default, and on a required one it means
        the model declined, which is a miss.
        """
        if not isinstance(data, dict):
            return data
        out = {key: value for key, value in data.items() if value is not None}
        for name, field in cls.model_fields.items():
            choices = getattr(field.validation_alias, "choices", None)
            if not choices:
                continue
            answered = next((c for c in choices if has_content(out.get(c))), None)
            if answered is not None and answered != name:
                # Moved, not copied. Leaving the drifted key behind means `extra`
                # keeps a second copy of the same payload: the row stores every
                # generated file twice, the UI renders the section twice, and the
                # next agent is handed the duplicate as prior-phase context —
                # spending the context budget this exists to protect on a verbatim
                # copy of what is already there.
                out[name] = out.pop(answered)
        return out


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
    #: Which feature this story delivers, by name, and so its priority. "The P0
    #: stories" used to be unanswerable: only features carried a priority.
    feature: str = ""
    priority: str = Field("", description="P0 | P1 | P2 — the feature's, when left out")


class Milestone(_Shape):
    milestone: str
    deliverables: StrList


class ProductManagerOutput(_Shape):
    product_name: str
    problem_statement: str
    target_users: StrList
    mvp_scope: StrList
    out_of_scope: StrList
    #: What the spec takes for granted. Named so the next phase can see a guess is
    #: a guess, rather than reading it as a requirement.
    assumptions: StrList = Field(default_factory=list)
    features: _list_of(Feature)
    user_stories: _list_of(UserStory)
    success_metrics: StrList
    roadmap: _list_of(Milestone)

    @model_validator(mode="after")
    def _stories_take_their_feature_priority(self) -> "ProductManagerOutput":
        """A story with no priority of its own has its feature's — or P0 when its
        feature can't be found, so a story is never silently left out of "the P0
        stories" that QA writes tests for."""
        by_name = {f.name.strip().lower(): f.priority for f in self.features}
        for story in self.user_stories:
            if not story.priority.strip():
                story.priority = by_name.get(story.feature.strip().lower(), "") or "P0"
        return self


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
    #: Which entity it serves and who may call it. Both were invented downstream
    #: when the architecture left them out — the auth flow especially.
    entity: str = ""
    auth: str = Field("", description="public | user | owner | admin")


class SystemDesignOutput(_Shape):
    architecture_overview: str
    tech_stack: TechStack
    components: _list_of(Component)
    data_model: _list_of(Entity)
    api_endpoints: _list_of(Endpoint)
    #: The page routes the frontend should have ("/", "/todos/[id]"). With the
    #: entities and endpoints, the name registry every later phase is held to.
    pages_expected: StrList = Field(default_factory=list)
    #: What did not make the MVP's cut — entities or endpoints for later.
    later: StrList = Field(default_factory=list)
    open_questions: StrList = Field(default_factory=list)
    scaling_considerations: StrList
    architecture_diagram_mermaid: str = Field(description="a Mermaid flowchart definition")


# ── Backend / Frontend engineers ─────────────────────────────────────────────
class SourceFile(_Shape):
    path: str
    language: str
    purpose: str
    code: str


#: Code last, in every shape that carries code: a reader cut short for room loses
#: source before it loses the index of what was written. `db_models` is gone — it
#: was a second copy of the models file, as an escaped string, ahead of the files.
class BackendEngineerOutput(_Shape):
    framework: str = Field(description="e.g. FastAPI")
    summary: str
    auth_flow: str
    setup_instructions: StrList
    files: _list_of(SourceFile)


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


# ── the plan a code phase writes before its files (#81) ──────────────────────
class PlannedFile(_Shape):
    """One file the phase will write, before a line of it is written."""

    path: str
    purpose: str
    #: The names other files import from it — what the next file's call is told exists.
    exports: StrList = Field(default_factory=list)
    #: Paths in this plan it imports, so it is written after them where it can be.
    imports: StrList = Field(default_factory=list)


class BackendPlan(_Shape):
    """Everything in `BackendEngineerOutput` but the code: the code comes file by file."""

    framework: str = Field(description="e.g. FastAPI")
    summary: str
    auth_flow: str
    setup_instructions: StrList
    files: _list_of(PlannedFile)


class FrontendPlan(_Shape):
    framework: str = Field(description="e.g. Next.js + React")
    summary: str
    pages: _list_of(Page)
    components: _list_of(UiComponent)
    state_management: str
    files: _list_of(PlannedFile)


# ── QA ───────────────────────────────────────────────────────────────────────
class TestFile(_Shape):
    path: str
    framework: str
    targets: str
    code: str


#: What models call a coverage figure they made up. Still accepted — a model asked
#: for it for months will keep sending it — and never stored (#76): coverage is
#: measured by running the suite, or the page says it wasn't.
_GUESSED_COVERAGE = ("coverage_estimate", "estimated_coverage", "estimatedCoverage", "coverage")
#: Written by the platform after running the suite, never by the model: a reply that
#: carries one has it dropped, so a model can't report its own tests green.
_MEASURED = ("test_run",)


class QAEngineerOutput(_Shape):
    summary: str
    test_strategy: str
    edge_cases: StrList
    risks: StrList
    #: The commands that run these tests, which DevOps's CI workflow uses rather
    #: than guessing. Empty when the side has no tests.
    command_backend: str = Field("", description="e.g. pytest backend/tests")
    command_frontend: str = Field("", description="e.g. npm test --prefix frontend")
    test_files: _list_of(TestFile)

    @model_validator(mode="before")
    @classmethod
    def _drop_guessed_coverage(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in _GUESSED_COVERAGE and k not in _MEASURED}
        return data


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


class EnvVar(_Shape):
    name: str
    purpose: str


class DeploymentNotes(_Shape):
    """What the person deploying needs and the platform cannot work out by itself."""

    env_vars: _list_of(EnvVar) = Field(default_factory=list)
    health_check_path: str = ""
    migration_command: str = ""
    rollback: str = Field("", description="one paragraph")


def _first_file(value: Any) -> Any:
    """The old `ci_cd` list, or a lone file, read as the one workflow."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


class DevOpsEngineerOutput(_Shape):
    """Narrowed to what deploy uses or the person needs (#80).

    Deploy decides its own Dockerfiles, Blueprint and Vercel settings from the
    charter, never from a model, so the Dockerfiles, compose files and pipeline that
    used to be asked for here were written for nothing — or worse, shipped unchecked
    into the person's repository. What is left is notes, and a CI workflow only when
    GitHub is connected, built from QA's real test commands.
    """

    summary: str
    deployment_notes: DeploymentNotes
    #: Observed drift: `github_actions`, and the old list-shaped `ci_cd`.
    ci_workflow: Annotated[Optional[CiCdFile], BeforeValidator(_first_file)] = Field(
        None, validation_alias=AliasChoices("ci_workflow", "ci_cd", "github_actions")
    )


# ── Cost estimation ──────────────────────────────────────────────────────────
class InfraCost(_Shape):
    item: str
    low_usd: Number
    high_usd: Number
    notes: str = ""
    #: What this figure takes for granted ("Render starter, one instance").
    assumption: str = ""


class ThirdPartyCost(_Shape):
    item: str
    monthly_usd: Number
    assumption: str = ""


class DevEffort(_Shape):
    role: str
    weeks: Number
    assumption: str = ""


class CostEstimationOutput(_Shape):
    summary: str
    #: Every estimate rests on these. Shown beside the figures, because a number
    #: without its assumption reads as a quote.
    assumptions: StrList
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


def subset_schema(model: type[BaseModel], keys: list[str]) -> dict:
    """The response schema narrowed to `keys` — for the round that asks a reply cut
    off at the output limit for only the fields it did not finish."""
    schema = response_schema(model)
    if not keys:
        return schema
    props = schema.get("properties") or {}
    schema["properties"] = {k: v for k, v in props.items() if k in keys}
    schema["required"] = [k for k in schema.get("required") or [] if k in keys]
    return schema


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
