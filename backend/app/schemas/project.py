"""Request/response schemas for the projects & pipeline API."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import AliasChoices, BaseModel, BeforeValidator, Field, model_validator
from typing_extensions import Annotated

from app.core.constants import ApprovalMode, RoutingMode


def _as_utc(value: object) -> object:
    """Stamp naive timestamps as UTC before they leave the API.

    Every datetime written here is UTC, but SQLite hands back naive values even for
    `DateTime(timezone=True)` columns — so they serialised without an offset, and
    `Date.parse` in the browser reads an offset-less ISO string as *local* time.
    On a UTC+5:30 machine that turned a phase running for 47 seconds into one
    running for 5h31m, and skewed every relative timestamp in the sidebar.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


#: A datetime that always reaches the client with an explicit UTC offset.
UtcDatetime = Annotated[datetime, BeforeValidator(_as_utc)]


class SkillOverrides(BaseModel):
    """What one build was told about skills, over what scoring would have chosen.

    Selection is a keyword score with no model call behind it, which makes a miss
    silent: a skill that does not match simply never arrives, and nothing in the
    output says so. A pin is how that is corrected for one build without editing a
    library every other build reads; an exclusion is the same thing the other way.
    """

    #: Skills this build gets whether or not they scored — and whether or not they
    #: are switched off in the library, since naming one here is a more specific
    #: instruction than the standing default.
    pinned: list[str] = Field(default_factory=list)
    #: Skills this build never gets, whatever they scored. Excluding beats pinning:
    #: the two together are a contradiction, and refusing to inject is the safe half.
    excluded: list[str] = Field(default_factory=list)


class ProjectCreate(BaseModel):
    idea: str = Field(..., min_length=3, description="The product idea to build.")
    name: Optional[str] = None
    routing_mode: Optional[RoutingMode] = None
    preferred_model: Optional[str] = Field(
        None,
        description=(
            "source:model for Manual mode, as the pickers write it — a local source's "
            "id or a cloud provider, e.g. 'anthropic:claude-opus-4-8'."
        ),
    )
    approval_mode: Optional[ApprovalMode] = Field(
        None, description="How often the run stops for review. Defaults to two checkpoints."
    )
    cost_cap_usd: Optional[float] = Field(
        None,
        gt=0,
        description=(
            "Projected monthly run cost above which the build interrupts itself. "
            "Omit for no cap — zero would be indistinguishable from one. Only "
            "`checkpoints` acts on it; the other modes are explicit choices about "
            "being interrupted, and keep the value for when you switch back."
        ),
    )
    skill_overrides: Optional[SkillOverrides] = Field(
        None,
        description=(
            "Skills to force on or off for this build, over what keyword scoring "
            "would choose. Omit to let the automatic choice stand."
        ),
    )
    #: Legacy switch, still honoured when `approval_mode` is absent.
    require_approval: Optional[bool] = None


class ProjectUpdate(BaseModel):
    """Settings a reviewer can change on a run that is already going.

    Both of these were fixed at create time, which meant a run heading somewhere
    expensive could not be given a gate, and a run you had come to trust could not
    be let off its leash without starting over.
    """

    approval_mode: Optional[ApprovalMode] = None
    cost_cap_usd: Optional[float] = Field(None, gt=0)
    #: Send `null` to lift the cap. Pydantic cannot tell absent from null, so this
    #: says which of the two a null in the body meant.
    clear_cost_cap: bool = False


class WaiveRequest(BaseModel):
    """Accept a security finding deliberately, with the reason on the record.

    The reason is mandatory. A waiver with nothing attached is indistinguishable from
    the silence this replaces, and it is the only record anyone reading this build
    later has of why a known issue shipped.
    """

    reason: str = Field(
        ...,
        min_length=3,
        description="Why this finding is acceptable for this build.",
    )


class RedoRequest(BaseModel):
    """Send one phase back to its agent, from whichever review is on screen."""

    phase: str = Field(..., description="Phase key to re-run, e.g. 'backend_engineer'.")
    feedback: str = Field(..., min_length=1, description="What to change.")


class PhaseResultOut(BaseModel):
    id: str
    phase: str
    agent: str
    status: str
    output: dict
    content_md: str
    model_used: Optional[str] = None
    provider_used: Optional[str] = None
    #: Whether the model ran on the user's own hardware — the backend's answer, so
    #: the page never guesses it from a provider's name. Rows written before calls
    #: recorded it are answered from the provider: only the cloud ones were not.
    is_local: Optional[bool] = None
    feedback: Optional[str] = None
    created_at: UtcDatetime

    #: valid | repaired | invalid — did this agent produce the shape it was asked
    #: for, and did it take a second attempt? `None` on rows written before the
    #: check existed. The reviewer is told rather than left to spot it.
    schema_status: Optional[str] = None
    schema_note: Optional[str] = None

    #: ok | violated — does this phase agree with the stack the architecture froze?
    #: A separate answer from `schema_status`: a deliverable can match its declared
    #: shape perfectly and still be written against a database nothing else uses.
    #: `None` on rows written before the check existed.
    stack_status: Optional[str] = None
    #: Each contradiction, in the words the agent was sent back with.
    stack_note: Optional[list[str]] = None

    #: ok | failed | unchecked — does this phase's code compile? A third answer, apart
    #: from shape and stack. `None` for rows from before the check, and for phases
    #: that write no code.
    build_status: Optional[str] = None
    #: `[{path, line, kind, message}]` for what still does not compile.
    build_note: Optional[list[dict]] = None

    #: The procedural skills this phase was actually given, by name and in the order
    #: they were injected. `None` on rows written before the library existed — which
    #: is not the same as `[]`, and the UI says nothing at all for those rather than
    #: reporting that an agent was offered skills and took none.
    skills_used: Optional[list[str]] = None

    # Timing, so a phase in flight can show elapsed time and a finished one can show
    # what it actually cost in wall-clock and tokens.
    started_at: Optional[UtcDatetime] = None
    completed_at: Optional[UtcDatetime] = None
    total_tokens: int = 0
    latency_ms: int = 0

    # from_attributes for ORM; disable the 'model_' protected namespace (we use model_used).
    model_config = {"from_attributes": True, "protected_namespaces": ()}

    @model_validator(mode="after")
    def _is_local_for_older_rows(self) -> "PhaseResultOut":
        if self.is_local is None and self.provider_used:
            from app.router.base import CLOUD_PROVIDERS

            self.is_local = self.provider_used not in CLOUD_PROVIDERS
        return self


class ProjectOut(BaseModel):
    id: str
    idea: str
    name: Optional[str]
    status: str
    current_phase: Optional[str]
    routing_mode: str
    preferred_model: Optional[str]
    require_approval: bool

    # ── review policy ────────────────────────────────────────────────────────
    #: Always resolved: a row written before the policy existed reports the mode its
    #: `require_approval` flag actually meant. See `Project.effective_approval_mode`.
    approval_mode: str = Field(
        ApprovalMode.CHECKPOINTS.value,
        validation_alias=AliasChoices("effective_approval_mode", "approval_mode"),
    )
    cost_cap_usd: Optional[float] = None
    #: Which review to render, and the one line saying why the run stopped here.
    gate_kind: Optional[str] = None
    gate_note: Optional[str] = None

    #: The technology decisions frozen after the architecture was approved, which
    #: every phase after it is written against and checked against. `None` on a run
    #: whose architecture hasn't been drawn yet, and on one whose architecture named
    #: nothing this pipeline recognises — in both cases nothing is being enforced,
    #: and the UI says so rather than showing an empty table as if it were a stack.
    charter: Optional[dict] = None
    #: How many times this build has already been sent back to fix its own severe
    #: security findings.
    remediation_rounds: Optional[int] = None
    #: The skills this build forces on or off, over the automatic choice. `None` on
    #: a build that never said anything about them.
    skill_overrides: Optional[SkillOverrides] = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    phases: list[PhaseResultOut] = []

    # ── liveness ─────────────────────────────────────────────────────────────
    phase_started_at: Optional[UtcDatetime] = None
    heartbeat_at: Optional[UtcDatetime] = None
    cancel_requested: bool = False
    last_error: Optional[str] = None
    #: `running` but nothing is driving it — the run died with its process. Computed
    #: server-side so the client never has to guess a threshold.
    stalled: bool = False
    #: Seconds the current phase has been generating (None when idle).
    elapsed_seconds: Optional[float] = None

    model_config = {"from_attributes": True, "populate_by_name": True}


class PreflightRequest(BaseModel):
    """The routing half of a build, asked about before the build exists."""

    routing_mode: str = Field(..., description="auto | manual | local_only")
    preferred_model: Optional[str] = Field(
        None, description="`source:model` pinned to the run, for Manual routing."
    )


class ApprovalRequest(BaseModel):
    feedback: Optional[str] = Field(
        None, description="Optional guidance, required-ish when rejecting."
    )


class StopRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Why the run was stopped.")


class RunResponse(BaseModel):
    project_id: str
    status: str
    current_phase: Optional[str]
    message: str
