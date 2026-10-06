"""ORM models: accounts and sessions, paired computers, projects, phase results, debates, analytics,
knowledge docs."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.core.constants import ApprovalMode
from app.db.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes even for `DateTime(timezone=True)` columns.

    Everything written here is UTC, so re-attach the timezone rather than letting a
    naive/aware comparison raise in the middle of a status check.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class User(Base):
    """One account. Everything a person makes or configures belongs to one of these.

    `password_hash` is null only on the account the accounts migration creates to
    hold what existed before accounts did: nobody can sign in to it until the first
    person to set up this install claims it with an email and password.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    #: Lower-cased and trimmed when written, so one address is one account.
    email: Mapped[Optional[str]] = mapped_column(String(320), unique=True, nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    #: The account that owns this install: the first one. It alone starts from the
    #: keys in `.env`, reaches runtimes on the server's own network, and edits the
    #: shared skill library.
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def claimed(self) -> bool:
        return bool(self.password_hash)


class AuthSession(Base):
    """A signed-in browser. The cookie holds a random token; only its hash is here,
    so a copy of the database signs nobody in."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    #: The account this build belongs to. Nullable only because the column is added
    #: to a populated table; the accounts migration fills every existing row, and
    #: every route reads projects through their owner, so a null one is unreachable.
    owner_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    idea: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Pipeline state
    status: Mapped[str] = mapped_column(String(32), default="created")  # see PipelineStatus
    current_phase: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)

    # When the phase named by `current_phase` started generating. Set *before* the
    # agent runs, so the UI can show elapsed time while a phase is in flight.
    phase_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Written every few seconds by the live runner. A `running` project whose
    # heartbeat has gone quiet is stalled — see `stalled` below.
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set by Stop. The runner checks it between phases and after each agent returns.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Which driver the build belongs to (#41). Every claim writes a fresh one; a driver
    # whose token is no longer this one stops without writing. See `orchestration.claim`.
    run_token: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Why the last run stopped, in words a person can act on.
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: What kind of failure that was, when a cloud provider refused a key
    #: (`app.core.keyerrors`: `no_credit`, `expired`, …), and which provider — so the
    #: page offers that fix instead of blaming the local runtime. Null otherwise.
    last_error_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_error_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: The paired computer a `paused` build is waiting for. Set when the build
    #: pauses, cleared when it resumes; when that computer reconnects, the build
    #: picks itself back up. Not a foreign key: a computer forgotten meanwhile
    #: leaves the build paused and resumable by hand, not deleted with it.
    paused_device_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Routing config chosen for this project
    routing_mode: Mapped[str] = mapped_column(String(16), default="local_only")
    preferred_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    require_approval: Mapped[bool] = mapped_column(default=True)

    # ── review policy (mutable mid-run) ──────────────────────────────────────
    # How often this run stops for a person. Nullable so a database written before
    # the policy existed still reads correctly — see `effective_approval_mode`.
    approval_mode: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: Projected monthly run cost, in USD, above which Ledger interrupts the build.
    cost_cap_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Why the pipeline is currently waiting — set when it parks at a gate, cleared
    # when it moves. The reviewer is shown a different surface for each.
    gate_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    gate_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: How the person answered "connect your database": `connected`, `unchecked`,
    #: `later`, or `none` when the database needs nothing. Null until the
    #: question has been asked (and on every build from before it was). The values
    #: themselves are never in the database — see `app.core.project_secrets`.
    database_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: What the person said about app connectors before the build started:
    #: `{"use": [...], "skip": [...]}`. Null when they said nothing (#59).
    integrations_choice: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    #: How the connectors question was answered, per connector: `{"stripe": "later"}`.
    #: A connector connected in the account, or saved for this build, needs no entry —
    #: that is read live, so a key rotated in Connectors reaches every build. Null
    #: until asked, and on every build from before connectors existed.
    integrations_status: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    @property
    def connectors_unconnected(self) -> list[str]:
        """App connectors this build uses that are answered and still not connected."""
        if not (self.charter or {}).get("integrations"):
            return []
        from app.orchestration import connectors

        return connectors.unconnected_answered(self)

    # ── where the finished build went (#55) ──────────────────────────────────
    #: The repository in the user's own GitHub (`owner/name`) this build was pushed
    #: to, so a second push adds a commit there instead of failing on the name.
    github_repo: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    github_branch: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    github_pushed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: `vercel` (uploaded straight to the user's Vercel) or `render` (handed off to
    #: Render's Blueprint page, from the GitHub repo).
    deploy_target: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: The live URL: Vercel's, or the one the user pasted back from Render.
    deploy_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    #: `queued`, `uploading`, `building`, `ready`, `error`, or `handed_off` for Render.
    deploy_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: Vercel's deployment id, polled for its state.
    deploy_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    deployed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why the last deploy failed, scrubbed, in words a person can act on.
    deploy_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── the stack this build is held to ──────────────────────────────────────
    #: Frozen once the architecture is settled, and binding on every phase after it:
    #: language, frameworks, database, test runner, package manager. Mirrored out of
    #: the graph's own state so the UI can show it without reading a checkpoint, and
    #: so a run resumed in a new process is still held to the same decisions. Null on
    #: runs that started before charters existed and on runs whose architecture named
    #: nothing this pipeline recognises — in both cases nothing is enforced, rather
    #: than something being invented.
    charter: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    #: How many times this build has already been sent back to fix its own severe
    #: security findings. Bounded, because each round re-runs the owning phase and
    #: everything after it — and a model that could not fix a finding twice will not
    #: fix it on the fourth attempt. Past the bound the decision goes to a person.
    remediation_rounds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: The crew's own fix loop, one track per kind of serious problem — `security`
    #: for severe findings, `build:<phase>` for a phase whose code does not compile
    #: or contradicts the stack. Each track records every round (what was sent back,
    #: to whom, how, and what the re-check found fixed), whether the loop stopped and
    #: why, and how many rounds it is allowed. See `orchestration.autofix`. Null on a
    #: build that never had anything to fix, and on every build from before the loop.
    auto_fix: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    #: What this build was told about skills, over what scoring would have chosen:
    #: `{"pinned": [...], "excluded": [...]}`. Selection is a keyword score, and a
    #: keyword miss is silent — a skill that does not match simply never arrives and
    #: nothing in the output says so. This is the correction for that, per build,
    #: without editing the library every other build reads. Null means the automatic
    #: choice stands, which is the state every run starts in.
    skill_overrides: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    phases: Mapped[list["PhaseResult"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="PhaseResult.created_at"
    )
    preview_revisions: Mapped[list["PreviewRevision"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="PreviewRevision.created_at",
    )

    # ── derived state ────────────────────────────────────────────────────────
    @property
    def effective_approval_mode(self) -> str:
        """The review policy this run actually follows.

        `approval_mode` is nullable on purpose: rows created before it existed have
        only `require_approval`, and those runs were gated at *every* phase. Reading
        them as `checkpoints` would silently drop gates a person was relying on, so
        an unset column keeps the old meaning and only new runs get the new default.
        """
        if self.approval_mode:
            return self.approval_mode
        return (
            ApprovalMode.EVERY_PHASE.value
            if self.require_approval
            else ApprovalMode.UNATTENDED.value
        )

    @property
    def stalled(self) -> bool:
        """True when this run says `running` but nothing is actually driving it.

        Background tasks die with the process, so a `running` row that outlives its
        server is unrecoverable on its own — and used to poll forever. A missing
        heartbeat is proof there is no live runner: only the runner writes one.
        """
        if self.status != "running":
            return False
        beat = _aware(self.heartbeat_at) or _aware(self.phase_started_at)
        if beat is None:
            return True
        return (_now() - beat).total_seconds() > settings.stall_after_seconds

    @property
    def last_error_help(self) -> Optional[dict]:
        """The words and the one action for `last_error_kind` — from the one place
        they live, so the page never writes its own."""
        if not self.last_error_kind or not self.last_error_provider:
            return None
        from app.core import keyerrors

        return keyerrors.advice(self.last_error_kind, self.last_error_provider).as_dict()

    @property
    def elapsed_seconds(self) -> Optional[float]:
        """Seconds the current phase has been generating, or None when idle."""
        started = _aware(self.phase_started_at)
        if self.status != "running" or started is None:
            return None
        return max(0.0, (_now() - started).total_seconds())


class PhaseResult(Base):
    """Output of a single agent/phase, plus its approval state."""

    __tablename__ = "phase_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))

    phase: Mapped[str] = mapped_column(String(48), nullable=False)
    agent: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending_approval")

    # A row is created the moment the agent starts (status `running`) and filled in
    # when it finishes, so "which agent has the work, since when" is always answerable.
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Structured output the agent produced (dict) + the human-readable markdown.
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    content_md: Mapped[str] = mapped_column(Text, default="")

    # Which model actually produced it (after routing/fallback).
    model_used: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    #: Why an earlier model in the chain was passed over, when a provider refused its
    #: key: "OpenAI: out of credit; continued on Google Gemini (…)" (#63).
    fallback_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider_used: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: Whether that model ran on hardware the user controls, as the provider that
    #: served it said at the time. A source's id says nothing about that — a local
    #: runtime can serve a model it sends elsewhere to run. Null on rows written
    #: before calls recorded it.
    is_local: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Human feedback when rejected.
    feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # What validation made of this output against the agent's declared shape, and
    # what was wrong when something was. Nullable because rows written before the
    # check existed cannot answer for themselves — see `SchemaStatus`.
    schema_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    schema_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Where this phase's output contradicts the stack charter, after the repair round
    # had its chance. Kept apart from `schema_status` deliberately: "nobody could read
    # this" and "this is written against a different database than the rest of the
    # build" are different failures, fixed by different people in different ways.
    # Nullable because rows written before the check existed cannot answer for
    # themselves — the same reasoning as `schema_status` above.
    stack_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    stack_note: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Whether this phase's generated code parses and resolves — see `BuildStatus` —
    # and, when it does not, the problems as `[{path, line, message}]`. A third fact
    # beside the two above: a deliverable can match its shape and agree with the
    # stack and still not compile. Nullable for rows written before the check, and
    # for phases that write no code at all.
    build_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    build_note: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    #: The skills this phase was actually given, by name, in the order they were
    #: injected. What was *selected* is a different fact: a skill that did not fit
    #: the model's window never reached the agent. A skill you cannot confirm was
    #: used is indistinguishable from one that did nothing, which is the whole
    #: reason this column exists rather than the reviewer being asked to trust it.
    #: Null on rows written before skills existed, and on a phase that got none.
    skills_used: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    #: What this agent was shown (#80): per dependency, its digest and whether the
    #: full output went whole, cut, or not at all; whether the name registry and the
    #: platform contract were in its instructions; how many replies the output limit
    #: cut off. Null on rows written before hand-offs were recorded.
    handoff: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped["Project"] = relationship(back_populates="phases")


class PreviewRevision(Base):
    """One version of the project's visual HTML preview (a self-contained mockup).

    Revisions form an append-only history per project: undo and redo move a head
    pointer over them (see `parent_id`/`head_at`) and never delete a row. `source` records how it came to be
    (a full regenerate vs a single-section edit); `section_id`/`instruction` capture which
    section a user edited and what they asked for.
    """

    __tablename__ = "preview_revisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))

    html: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="generated")  # generated | edited

    # Set only for single-section edits.
    section_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    instruction: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Which model produced this revision (after routing/fallback).
    model_used: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    provider_used: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # What building this revision found: pages, sections and how each was produced
    # (generated, repaired, or the platform's template), the checks run on the
    # assembled site and their results. Null for single-document mockups drawn
    # before the site builder, and for edits — an edit carries its parent's report.
    report: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Undo and redo move a pointer instead of deleting rows. `parent_id` is the
    # revision this one was made from; the live one is the row with the latest
    # `head_at`. Both are null on rows written before the pointer existed: their
    # parent is the revision before them, and with no head set the newest is live.
    parent_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    head_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped["Project"] = relationship(back_populates="preview_revisions")


class DebateRecord(Base):
    """A recorded debate between agents and the platform's verdict."""

    __tablename__ = "debates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    topic: Mapped[str] = mapped_column(String(256))
    arguments: Mapped[list] = mapped_column(JSON, default=list)  # [{agent, position, rationale}]
    decision: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UsageEvent(Base):
    """One LLM call's token usage and estimated cost — powers the analytics dashboard."""

    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    #: Whose call this was — the dashboard shows each account only its own. Some
    #: calls belong to no project, so the project cannot answer this.
    owner_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    phase: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    #: Whether `cost_usd` is a price or a placeholder. A model with no published rate
    #: used to be billed at zero, so pointing a role at an unrecognised cloud model
    #: produced a dashboard reading $0.00 and a cost cap that could never trip.
    #: Nullable: rows written before this existed cannot say which of the two they are.
    cost_known: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    #: Whether the call ran on the user's own hardware — what makes it free. Null on
    #: rows written before calls recorded it.
    is_local: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    fallback_used: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SecurityDisposition(Base):
    """What was actually *done* about one security finding.

    Warden's findings used to be terminal. A documented high-severity "No CSRF
    Protection in Frontend", with a written remediation, went into the archive with
    the code unchanged, because the report landed in a panel and the pipeline moved
    on to DevOps. A finding with nowhere to go is a finding nobody acts on.

    This is the somewhere. One row per finding per project, carrying who owns the
    offending file and what happened next — sent back and fixed, or waived on the
    record by the reviewer. Neither of those is "shipped silently", which is the
    only outcome this table exists to remove.

    The key is derived from the finding's own words rather than its position in a
    list, because the list is regenerated every time the phase re-runs: a waiver has
    to survive the re-audit that follows it, or waiving a finding would mean being
    asked about it again on the very next pass.
    """

    __tablename__ = "security_dispositions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))

    #: Stable across re-audits: a hash of category + title. Deliberately not the
    #: location — that is the most volatile field a model writes, and including it
    #: meant a reworded path minted a new key and resurrected a waived finding.
    finding_key: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(16), default="")
    category: Mapped[str] = mapped_column(String(128), default="")
    location: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str] = mapped_column(Text, default="")

    #: The phase that wrote the offending file, resolved from the file trees the
    #: phases produced. Null when the location matches nothing anyone wrote — an
    #: architectural finding, say — which is exactly when a person has to decide.
    owner_phase: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    #: open | fix_requested | fixed | waived — see `FindingStatus`.
    status: Mapped[str] = mapped_column(String(16), default="open")
    #: The reviewer's reason for a waiver, or the note sent back with a fix.
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Which automatic fix round made this finding go away, when one did. Null for a
    #: finding fixed by hand, or not fixed.
    fixed_round: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: Why a serious finding was waived: false_positive | mitigated | accepted_risk.
    #: Required for those — a leaked credential is not waived on a free-text shrug —
    #: and null on every waiver of a small finding, which keeps its free-text reason.
    waive_kind: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class KnowledgeDoc(Base):
    """Metadata for an uploaded RAG document (chunks live in ChromaDB)."""

    __tablename__ = "knowledge_docs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    #: The account that uploaded it. Its chunks are only ever searched for that
    #: account's builds.
    owner_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Device(Base):
    """A computer paired to an account through the connector.

    The server holds only the device's **public** key: the connector made the
    keypair itself, and the private half never leaves that computer. Every
    connection proves it still holds it by signing a fresh challenge.

    A device is `pending` from the moment its connector claims a pairing code until
    the account approves it on the website, and nothing is sent to it before that.
    Forgetting a device deletes this row, which is the revocation: there is no
    other credential to revoke.
    """

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    #: What the account calls it. Starts as the computer's own name.
    name: Mapped[str] = mapped_column(String(120))
    #: Raw Ed25519 public key, base64url without padding.
    public_key: Mapped[str] = mapped_column(String(64), unique=True)
    #: pending | approved — see `app.connector.protocol`.
    status: Mapped[str] = mapped_column(String(16), default="pending")
    os: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    connector_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: The network address the pairing came from, shown when approving it. An
    #: approximate location, and the only one this server can honestly give.
    paired_from: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: The last `hello` the connector sent: runtimes, models, RAM. Validated
    #: against its schema before it is stored.
    hello: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    #: The account's choices on this computer, as `source:model`.
    chat_model: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    embed_model: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PairingCode(Base):
    """One "Connect my computer" code (RFC 8628's user code, typed the other way).

    Made only for a signed-in account, single-use, short-lived. Only a hash is
    stored, and each code allows a handful of attempts before it is burned.
    """

    __tablename__ = "pairing_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: Set when a connector claims it; a claimed code never works again.
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The device the claim created. Nulled if that device is forgotten.
    device_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
