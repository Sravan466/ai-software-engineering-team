"""Project lifecycle + pipeline control (run / approve / reject / stop / resume).

Every control here is non-blocking. The phase itself runs in a background task and the
request returns as soon as the project has been moved into `running`, because a POST
that holds the connection open for the length of a model call is indistinguishable
from a hang: the client cannot poll, the status badge lies, and a refresh mid-flight
loses the gate entirely.

Each control claims the project with a conditional UPDATE. That single statement is the
idempotency guard — two tabs racing to approve the same phase produce one winner and
one `409`, instead of quietly advancing the pipeline twice on one click's worth of intent.
"""
from __future__ import annotations

import threading
from typing import Optional

import io
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.connector.hub import hub
from app.api.deps import current_user, get_project, shipping_or_404
from app.build import dbconnect, scan
from app.core import artifacts, model_roles, project_secrets, secretbox
from app.core.config import settings
from app.core.constants import (
    PHASE_ORDER,
    ApprovalMode,
    FindingStatus,
    GateKind,
    Phase,
    PipelineStatus,
    RoutingMode,
)
from app.core.logging import get_logger
from app.db.base import SessionLocal, get_db
from app.db.models import Project, SecurityDisposition, User
from app.orchestration import autofix, claim, remediation
from app.orchestration.runner import runner
from app.router.router import router as model_router
from app.schemas.project import (
    AcceptRequest,
    ApprovalRequest,
    KeepTryingRequest,
    PreflightRequest,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    RedoRequest,
    RunResponse,
    StopRequest,
    WaiveRequest,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_approval_mode(payload: ProjectCreate) -> str:
    """The review policy for a new run.

    `require_approval` is still accepted, and still means what it always did — stop
    at every handoff — so an existing caller keeps its behaviour. Everything else
    gets the two-checkpoint default.
    """
    if payload.approval_mode is not None:
        return payload.approval_mode.value
    if payload.require_approval is not None:
        return (
            ApprovalMode.EVERY_PHASE.value
            if payload.require_approval
            else ApprovalMode.UNATTENDED.value
        )
    return (
        ApprovalMode.CHECKPOINTS.value
        if settings.require_approval
        else ApprovalMode.UNATTENDED.value
    )


def _checked_model(spec: Optional[str]) -> Optional[str]:
    """A pinned model, refused unless it names its source — never a guessed runtime."""
    if not spec or not spec.strip():
        return None
    try:
        return ":".join(model_router.parse(spec))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _connector_choice(payload: ProjectCreate) -> Optional[dict]:
    """`{use, skip}` as the person left the chips, or None when they said nothing.

    Only connectors the platform can wire are kept; `skip` wins over `use`.
    """
    from app.build import integrations

    choice = payload.connectors
    if choice is None:
        return None
    skip = sorted({i for i in choice.skip if integrations.get(i)})
    use = sorted({i for i in choice.use if integrations.connectable(i) and i not in skip})
    unknown = sorted({*choice.use, *choice.skip} - {i.id for i in integrations.catalog()})
    if unknown:
        raise HTTPException(422, f"There's no connector called {', '.join(unknown)}.")
    return {"use": use, "skip": skip} if (use or skip) else None


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Project:
    mode = (payload.routing_mode or RoutingMode(settings.default_routing_mode)).value
    preferred = _checked_model(payload.preferred_model)
    approval = _resolve_approval_mode(payload)
    project = Project(
        owner_id=user.id,
        idea=payload.idea,
        name=payload.name,
        routing_mode=mode,
        preferred_model=preferred,
        approval_mode=approval,
        cost_cap_usd=payload.cost_cap_usd,
        # Stored only when something was actually said. An empty override set and
        # "nothing was said" both mean the automatic choice stands, and writing the
        # first into every row would make the column unable to tell them apart.
        skill_overrides=(
            payload.skill_overrides.model_dump()
            if payload.skill_overrides
            and (payload.skill_overrides.pinned or payload.skill_overrides.excluded)
            else None
        ),
        integrations_choice=_connector_choice(payload),
        # Kept in step with the mode so anything still reading the old flag — a saved
        # query, an older client — never disagrees with the policy actually in force.
        require_approval=approval != ApprovalMode.UNATTENDED.value,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _rederive_gate(db: Session, project: Project) -> None:
    """Re-answer "why are we stopped here?" after the policy changed under a gate.

    A run parked on a single handoff and then switched to two-checkpoint review is
    standing at a Plan review, and should say so. Without this the panel keeps the
    title the old policy gave it until the run moves again.

    A policy that would no longer stop here at all keeps the existing gate: the run
    is parked and something has to release it, so the decision on screen stays real.
    """
    if project.status != PipelineStatus.AWAITING_APPROVAL.value or not project.current_phase:
        return
    # "Needs help" is not a policy's gate: the crew stopped on something it could not
    # fix, and no review mode makes that go away. Nor is the database question — only
    # the person's answer releases it.
    if project.gate_kind in (
        GateKind.NEEDS_HELP.value,
        GateKind.DATABASE.value,
        GateKind.INTEGRATIONS.value,
    ):
        return
    # The runner's own definition of "latest", so this cannot re-derive the gate from
    # a different row than the one the loop parked on.
    row = runner.latest_row(db, project, project.current_phase)
    if row is None:
        return
    # `schema_status` matters as much as the output here: without it a re-derive
    # would drop the "this check could not run" warning off a parked gate, and the
    # only sign the reviewer had that nothing was actually checked would vanish the
    # moment they adjusted the cost cap.
    gate = runner.gate_for(project, row)
    if gate is not None:
        project.gate_kind = gate.kind
        project.gate_note = gate.note


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    payload: ProjectUpdate,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> Project:
    """Change how a run is reviewed — including one that is already in flight.

    The policy used to be frozen at create time: a build heading somewhere expensive
    could not be given a gate, and one you had come to trust could not be let off its
    leash without starting over. The runner re-reads these before every handoff, so a
    change lands on the next one rather than at the next restart.
    """
    if payload.approval_mode is not None:
        project.approval_mode = payload.approval_mode.value
        project.require_approval = payload.approval_mode != ApprovalMode.UNATTENDED
    if payload.clear_cost_cap:
        project.cost_cap_usd = None
    elif payload.cost_cap_usd is not None:
        project.cost_cap_usd = payload.cost_cap_usd
    _rederive_gate(db, project)
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[Project]:
    found = list(
        db.execute(
            select(Project).where(Project.owner_id == user.id).order_by(Project.created_at.desc())
        ).scalars()
    )
    # Each build's version and open change (#79), read for the whole list in two queries
    # rather than two per build.
    from app.orchestration import changes, versions

    versions.prime(db, found)
    changes.prime(db, found)
    return found


@router.get("/{project_id}", response_model=ProjectOut)
def get_one(project: Project = Depends(get_project)) -> Project:
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    """Delete a project and everything it produced.

    A run may still be mid-phase; the flag tells it to stop touching a row that is
    about to disappear (the runner also treats a vanished project as cancelled).
    """
    project.cancel_requested = True
    db.commit()
    owner_id, project_id = project.owner_id, project.id
    db.delete(project)
    db.commit()
    # The database credentials go with it: nothing is left for a project that isn't.
    project_secrets.remove_project(owner_id, project_id)
    # And its live progress, which would otherwise sit in memory until a restart.
    from app.orchestration import activity

    activity.clear(project_id)


# ── Generated-project artifacts (preview + download) ─────────────────────────
@router.get("/{project_id}/artifacts")
def get_artifacts(
    version: Optional[int] = None,
    live: bool = False,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """Assembled files + docs + setup steps the agents produced (for Preview/Summary).

    What ships, by default (#79): while a change is being made, the version it is made
    on. `live` is the build as it stands — what a review is about; `version` names one."""
    assembled, shipped = shipping_or_404(db, project, version, live)
    return {
        "idea": project.idea,
        "name": project.name,
        "status": project.status,
        "readme": artifacts.readme_md(project, assembled),
        "version": shipped.number if shipped is not None else None,
        **assembled,
    }




@router.get("/{project_id}/download")
def download_project(
    request: Request,
    include_credentials: bool = False,
    version: Optional[int] = None,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    """Stream the generated project as a .zip (code + docs + README).

    `include_credentials` adds a real `backend/.env` with the saved database values —
    only here, only when asked. The preview, `/artifacts` and the GitHub push always
    carry `.env.example` with placeholders. Connector keys (#59) ride along: server
    ones in `backend/.env`, publishable ones in `frontend/.env.local` too.
    """
    env = None
    client_env = None
    if include_credentials and project.owner_id:
        # The one response that carries a credential in the clear: held to the same
        # rule as saving one.
        from app.api.routes.settings import _trusted_host

        if not _trusted_host(request):
            raise HTTPException(
                403,
                "Credentials can only be downloaded from an address this backend is "
                "served at (localhost, or BACKEND_PUBLIC_URL).",
            )
        from app.orchestration import connectors

        try:
            env = {**project_secrets.reveal(project.owner_id, project.id), **connectors.values_for(project)}
        except secretbox.SecretsLocked as e:
            raise HTTPException(503, str(e))
    # A named version (#79): the one asked for, else the one that ships.
    assembled, shipped = shipping_or_404(db, project, version)
    if env is not None:
        try:
            client_env = artifacts.frontend_env(project, assembled)
        except secretbox.SecretsLocked as e:
            raise HTTPException(503, str(e))
    data = artifacts.build_zip(project, assembled, env=env, client_env=client_env)
    suffix = f"-v{shipped.number}" if shipped is not None else ""
    filename = artifacts.slug(project.name or project.idea) + suffix + ".zip"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Pipeline control ─────────────────────────────────────────────────────────
def _claim(
    db: Session,
    project: Project,
    allowed: set[str],
    *,
    answers_question: bool = False,
    answers_database: bool = False,
) -> Optional[str]:
    """Atomically move the project into `running` — but only from `allowed`.

    Returns None when someone else got there first (a second tab, a double click),
    which is the whole point: the phase is handed to a background task exactly once.
    Otherwise returns the claim's token, which the background task must be handed:
    it is how a run stopped mid-call learns, once its call returns, that a resume has
    taken the build over (#41). Read it from here, never from the row later — by
    then it may be someone else's.

    A build parked on a question only the person can answer — its database, or its
    app connectors — is released only by an answer to it (`answers_question`; the
    older `answers_database` means the same) — not by approve, a redo, a finding's
    fix, or anything else that claims a waiting build. Checked here, once, so no
    route can forget it.
    """
    if not (answers_question or answers_database):
        _refuse_at_database_gate(project)
    token = claim.new_token()
    result = db.execute(
        update(Project)
        .where(Project.id == project.id, Project.status.in_(allowed))
        .values(
            status=PipelineStatus.RUNNING.value,
            run_token=token,
            cancel_requested=False,
            last_error=None,
            last_error_kind=None,
            last_error_provider=None,
            heartbeat_at=_now(),
        )
    )
    db.commit()
    if result.rowcount != 1:
        db.refresh(project)
        return None
    db.refresh(project)
    return token


def _conflict(project: Project, action: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            f"This build is already '{project.status}' — nothing to {action}. "
            "Another tab or click probably got there first; reload to see where it is."
        ),
    )


def _drive(project_id: str, token: str) -> None:
    """Background task: run the pipeline forward until it needs a human again."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is not None:
            runner.continue_run(db, project, claim_token=token)
    except Exception as e:  # noqa: BLE001 - a background crash must not strand the run
        log.exception("Pipeline task crashed for %s", project_id)
        _strand(db, project_id, str(e), token)
    finally:
        db.close()


def _resume_paused(owner_id: str, device_id: str) -> None:
    """Pick up every build that paused for this computer, now that it is back."""
    db = SessionLocal()
    try:
        paused = (
            db.query(Project)
            .filter(
                Project.owner_id == owner_id,
                Project.status == PipelineStatus.PAUSED.value,
                Project.paused_device_id == device_id,
            )
            .all()
        )
        for project in paused:
            # Claimed first: a Stop that landed a moment ago keeps its own message.
            token = _claim(db, project, {PipelineStatus.PAUSED.value})
            if not token:
                continue  # resumed by hand, or stopped, a moment ago
            runner.prepare_resume(db, project)
            log.info("Device %s reconnected; resuming build %s.", device_id, project.id)
            threading.Thread(
                target=_drive, args=(project.id, token), name=f"resume-{project.id[:8]}", daemon=True
            ).start()
    except Exception:  # noqa: BLE001 - the builds stay paused and resumable by hand
        log.exception("Couldn't resume the builds paused for device %s", device_id)
    finally:
        db.close()


def resume_paused_for(owner_id: str, device_id: str) -> None:
    """`hub.on_ready` listener. Called on the event loop, so the work goes to a thread."""
    threading.Thread(
        target=_resume_paused, args=(owner_id, device_id), name="resume-paused", daemon=True
    ).start()


hub.on_ready.append(resume_paused_for)


def _drive_reject(project_id: str, feedback: str, token: str) -> None:
    """Background task: regenerate the current phase with the reviewer's note."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is not None:
            runner.reject(db, project, feedback, claim_token=token)
    except Exception as e:  # noqa: BLE001
        log.exception("Reject task crashed for %s", project_id)
        _strand(db, project_id, str(e), token)
    finally:
        db.close()


def _drive_redo(project_id: str, phase: str, feedback: str, token: str) -> None:
    """Background task: regenerate one named phase and return to the same review."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is not None:
            runner.redo(db, project, phase, feedback, claim_token=token)
    except Exception as e:  # noqa: BLE001
        log.exception("Redo task crashed for %s/%s", project_id, phase)
        _strand(db, project_id, str(e), token)
    finally:
        db.close()


def _drive_deploy_fix(project_id: str, problems: list[dict], token: str) -> None:
    """Background task: a build Vercel failed, back to the crew with its errors (#75)."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is not None:
            runner.fix_deploy(db, project, problems, claim_token=token)
    except Exception as e:  # noqa: BLE001
        log.exception("Deploy fix task crashed for %s", project_id)
        _strand(db, project_id, str(e), token)
    finally:
        db.close()


#: What a reviewer is told when the platform itself fell over, rather than a model.
#: The exception text goes to the log, where someone can act on it; a field the UI
#: renders should not read "UPDATE statement on table 'phase_results'".
_CRASHED = (
    "The build stopped because this platform hit an internal error, not because a "
    "model failed. Everything already approved is kept — resume to pick it up from "
    "the last checkpoint. The details are in the backend log."
)


def _strand(db: Session, project_id: str, message: str, token: str) -> None:
    """Last resort: never leave a project `running` with nothing running.

    This runs on the session the crash happened on, whose transaction may already be
    poisoned — so roll back first. Without that the recovery write fails too and the
    project stays `running` forever, which is the dead end this whole change removes.

    Only while the crashed run still held the build: one resumed meanwhile is running
    under someone else's claim, and failing it would strand *that* run.
    """
    try:
        db.rollback()
        project = db.get(Project, project_id)
        if (
            project is not None
            and project.status == PipelineStatus.RUNNING.value
            and project.run_token == token
        ):
            project.status = PipelineStatus.FAILED.value
            project.last_error = _CRASHED
            project.last_error_kind = None
            project.last_error_provider = None
            db.commit()
            log.error("Stranded %s: %s", project_id, message)
    except Exception:  # noqa: BLE001
        db.rollback()


def _require_models(project: Project) -> None:
    """Refuse to start a run whose models are not actually there.

    Checked before the project is claimed, so a refusal leaves the build exactly as
    it was — ready to start again once the download finishes. Without this the run
    went `running`, and then died eight seconds later inside the first agent with a
    404 from the runtime for a message, having already moved the project into a state the
    user had to work out how to get back out of.
    """
    ready = _readiness(RoutingMode(project.routing_mode), project.preferred_model, recheck_keys=True)
    if not ready.ok:
        raise HTTPException(status_code=409, detail=ready.reason)


def _readiness(mode: RoutingMode, preferred_model: Optional[str], *, recheck_keys: bool = False):
    """The one readiness question, shared by every route that starts a run and by
    the preflight the composer asks before it creates one."""
    return model_router.readiness(
        mode,
        preferred_model,
        # Embeddings are left out: they never go through the chat router, so
        # resolving that role here would check the wrong model entirely. RAG and
        # memory degrade to no-ops when their model is missing; a build does not.
        roles=[r["role"] for r in model_roles.catalogue() if r["role"] != "embeddings"],
        # A run is starting: cloud keys whose standing is old or unsettled are checked
        # again now. The preflight only reads what is known.
        recheck_keys=recheck_keys,
    )


@router.post("/preflight")
def preflight(payload: PreflightRequest) -> dict:
    """Would a build with these settings start? Asked before one is created.

    The composer used to answer this itself, from a copy of the rules below — and
    three reviews in a row found the copy disagreeing with the original, each time
    leaving a project created and then refused. It now asks. Same readiness check,
    same roles, same answer as `/run` will give.
    """
    try:
        mode = RoutingMode(payload.routing_mode)
    except ValueError:
        raise HTTPException(400, f"'{payload.routing_mode}' is not a routing mode.")
    if payload.preferred_model and payload.preferred_model.strip():
        try:
            model_router.parse(payload.preferred_model)
        except ValueError as e:
            # The same refusal creating the project would give, said before it exists.
            return {"ok": False, "reason": str(e), "unreachable": False, "checks": []}
    ready = _readiness(mode, payload.preferred_model)
    return {
        "ok": ready.ok,
        "reason": ready.reason,
        "unreachable": ready.unreachable,
        # What the pre-Start check found for each model this build would use.
        "checks": list(ready.checks),
    }


@router.post("/{project_id}/run", response_model=RunResponse)
def run_pipeline(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    _require_models(project)
    token = _claim(db, project, {PipelineStatus.CREATED.value, PipelineStatus.FAILED.value})
    if not token:
        raise _conflict(project, "start")

    background.add_task(_drive, project.id, token)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message=(
            "Running — the first agent is generating now."
            if project.require_approval
            else "Running end-to-end (approvals disabled)."
        ),
    )


def _require_findings_settled(db: Session, project: Project) -> None:
    """Refuse to approve over a severe finding nobody has decided about.

    Warden used to report a high-severity issue, write a remediation for it, and
    watch the code go into the archive unchanged — because the report was the end of
    the road. It is not any more: a critical or high finding leaves this build fixed
    and re-audited, or waived on the record with a reason. This is the sentence that
    makes the second option a decision rather than an omission.
    """
    # Every review mode, unattended included: a build that parked with a severe
    # finding open is being approved by a person, and the same rule applies to them.
    outstanding = remediation.unresolved(db, project)
    if not outstanding:
        return
    count = len(outstanding)
    subject = (
        "1 serious or high-severity security finding is"
        if count == 1
        else f"{count} serious or high-severity security findings are"
    )
    raise HTTPException(
        status_code=409,
        detail=(
            f"{subject} still open. Send each one back to be fixed, or waive it with "
            "a reason — this build can't ship with them simply unread."
        ),
    )


def _require_tests_settled(project: Project) -> None:
    """Refuse to approve over tests that ran and failed, unless they were waived (#76).

    The same rule as a severe finding: a red suite ships fixed, or waived on the record
    with a reason — never because the button was there.
    """
    failing = runner.failing_tests(project)
    if not failing:
        return
    count = len(failing)
    raise HTTPException(
        status_code=409,
        detail=(
            f"{count} of QA's test{'s' if count != 1 else ''} still fail{'' if count != 1 else 's'}. "
            "Send the code or the tests back, or waive the failures with a reason."
        ),
    )


def _refuse_at_database_gate(project: Project) -> None:
    """Refuse anything but an answer while the build waits on a question gate."""
    if runner.at_database_gate(project):
        raise HTTPException(
            409,
            "This build is waiting on its database. Connect it, or choose \"Continue, "
            "I'll add it later\".",
        )
    if runner.at_integrations_gate(project):
        raise HTTPException(
            409,
            "This build is waiting on its services. Connect them, or choose \"Add all "
            "later\".",
        )


#: Said when a reviewer's note carries something that looks like a credential.
_CREDENTIAL_IN_FEEDBACK = (
    "Don't paste credentials here — this note goes to a model. Use Connect database "
    "instead; the crew only ever sees the variable names."
)


def _refuse_credentials(text: str, project: Project) -> None:
    if (
        dbconnect.looks_like_credential(text)
        or project_secrets.holds_saved_secret(project.owner_id, project.id, text)
        or project_secrets.holds_integration_secret(project.owner_id, project.id, text)
    ):
        # Not logged with the text, for the obvious reason.
        raise HTTPException(422, _CREDENTIAL_IN_FEEDBACK)


@router.post("/{project_id}/approve", response_model=RunResponse)
def approve_phase(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    if project.status != PipelineStatus.AWAITING_APPROVAL.value:
        raise (
            _conflict(project, "approve")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, f"Nothing to approve (status '{project.status}').")
        )
    if project.gate_kind == GateKind.NEEDS_HELP.value:
        raise HTTPException(
            409,
            "The crew is stuck on a serious problem, and approving would ship it. Keep "
            "trying, stop the build, or waive each finding with a reason.",
        )
    # Before anything else is checked, so the answer names what is actually waiting.
    _refuse_at_database_gate(project)
    _require_findings_settled(db, project)
    _require_tests_settled(project)
    # Every route that starts model calls asks first, as `run` and `resume` do —
    # a default changed since the last phase would otherwise fail inside it.
    _require_models(project)
    token = _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value})
    if not token:
        raise _conflict(project, "approve")

    if project.gate_kind == GateKind.SECURITY.value:
        # Approving past the Security stop is reading its review notes (#77).
        remediation.mark_notes_read(db, project)
        db.commit()
    runner.approve_current(db, project)
    background.add_task(_drive, project.id, token)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Approved — the next agent is starting.",
    )


@router.post("/{project_id}/reject", response_model=RunResponse)
def reject_phase(
    payload: ApprovalRequest,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Send back whichever phase is under review.

    `POST /redo` does this and more — it can name any phase — and is what the UI
    calls. This stays for API callers that already use it; both run the same code.
    """
    if not payload.feedback or not payload.feedback.strip():
        raise HTTPException(400, "Feedback is required when rejecting a phase.")
    _refuse_credentials(payload.feedback, project)
    _refuse_at_database_gate(project)
    if project.status != PipelineStatus.AWAITING_APPROVAL.value:
        raise (
            _conflict(project, "reject")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, f"Nothing to reject (status '{project.status}').")
        )
    # Every route that starts model calls asks first, as `run` and `resume` do —
    # a default changed since the last phase would otherwise fail inside it.
    _require_models(project)
    token = _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value})
    if not token:
        raise _conflict(project, "reject")

    background.add_task(_drive_reject, project.id, payload.feedback.strip(), token)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Sent back — the agent is regenerating this phase with your note.",
    )


# ── security findings: fix them, or waive them on the record ─────────────────
@router.get("/{project_id}/security")
def list_findings(
    project: Project = Depends(get_project), db: Session = Depends(get_db)
) -> dict:
    """Every finding this build is tracking, and what has been done about each.

    Derived from the audit rather than re-read from it: the status is the history of
    what was sent back, fixed and waived, which the report itself cannot know.
    """
    rows = (
        db.query(SecurityDisposition)
        .filter(SecurityDisposition.project_id == project.id)
        .order_by(SecurityDisposition.created_at)
        .all()
    )
    track = autofix.track(autofix.load(project), autofix.SECURITY)
    warden = runner.latest_row(db, project, Phase.SECURITY_ENGINEER.value)
    read = remediation.notes_read(project)
    return {
        "findings": [
            {
                "key": row.finding_key,
                "title": row.title,
                "severity": row.severity,
                "category": row.category,
                "location": row.location,
                "recommendation": row.recommendation,
                "owner_phase": row.owner_phase,
                "status": row.status,
                "note": row.note,
                # The crew's to fix (true), or the reviewer's to judge.
                "serious": remediation.row_is_serious(row),
                # Whether it holds the build until fixed or waived (#77): a scanner's
                # severe finding. A review note never does.
                "blocks": remediation.row_blocks(row),
                "fixed_round": row.fixed_round,
                "waive_kind": row.waive_kind,
                # Where it came from (#77): a scanner, with its rule and line, or Warden.
                "source": remediation.row_source(row),
                "tool": row.tool,
                "rule_id": row.rule_id,
                "rule_url": row.rule_url,
                "cwe": row.cwe,
                "path": row.path,
                "line": row.line,
                # A review note a person approved past at a Security stop (#77).
                "read": remediation.is_read(read, row),
                # A review note the scanner now reports itself: `tool:rule` (#77).
                "superseded_by": row.rule_id
                if remediation.row_source(row) == remediation.SOURCE_MODEL and row.rule_id
                else None,
            }
            for row in rows
        ],
        # Which scanners ran on the latest audit, or why none could (#77).
        "scan": _scan_summary(warden.scan if warden is not None else None),
        "unresolved": len(remediation.unresolved(db, project)),
        # Only the small ones: what the Security stop asks about.
        "unresolved_small": len(remediation.unresolved(db, project, serious=False)),
        "rounds_used": len(track["rounds"]),
        "rounds_allowed": int(track["allowed"]),
        "auto_fix_min_severity": settings.auto_fix_min_severity,
    }


def _scan_summary(record: object) -> Optional[dict]:
    """The latest scan, without its findings: those are the dispositions above."""
    if not isinstance(record, dict):
        return None
    return {k: record.get(k) for k in ("status", "summary", "reason", "tools", "seconds", "runner", "truncated", "rules", "at")}


def _findings(db: Session, project: Project, key: str) -> list[SecurityDisposition]:
    """Every tracked row with this key — normally one, occasionally more.

    All of them, not the first: a database written before findings were deduplicated
    on read can hold two rows sharing a key, and acting on one of them settles that
    one while `unresolved` goes on counting the other. The reviewer sees the finding
    marked waived and the Ship button stays shut with nothing left to click. Raising
    instead (the original `one_or_none`) was at least loud, but it was a 500 on a
    build that then could not be approved, fixed *or* waived.

    Deciding a finding decides every row that says the same thing about the same
    phase, which is what the reviewer believes they are doing.
    """
    rows = (
        db.query(SecurityDisposition)
        .filter(
            SecurityDisposition.project_id == project.id,
            SecurityDisposition.finding_key == key,
        )
        .order_by(SecurityDisposition.created_at)
        .all()
    )
    if not rows:
        raise HTTPException(404, "That finding isn't one this build is tracking.")
    return rows


@router.post("/{project_id}/security/{key}/fix", response_model=RunResponse)
def fix_finding(
    key: str,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Send one finding back to the agent that wrote the file it is about.

    This is `redo` with the finding's own remediation as the note, which means the
    phases built on top of the one being fixed are rebuilt — including the security
    review itself. The re-audit is what decides whether the fix took; nothing here
    marks a finding fixed on the strength of having asked.
    """
    rows = _findings(db, project, key)
    row = rows[0]
    if not row.owner_phase:
        raise HTTPException(
            400,
            "No phase owns this finding — it doesn't point at a file anyone wrote. "
            "Fix it by sending a phase back with your own note, or waive it.",
        )
    if remediation.row_source(row) == remediation.SOURCE_TOOL and row.tool in scan.DEPENDENCY_TOOLS:
        # The platform owns the manifests (#77): no agent can change a version, so a
        # rebuild would come back with the same dependency and the same finding.
        raise HTTPException(
            400,
            "This is a dependency's known vulnerability. The platform sets package "
            "versions, so no agent can fix it by rebuilding. Waive it with a reason, or "
            "update the version after you download the build.",
        )
    if project.status != PipelineStatus.AWAITING_APPROVAL.value:
        raise (
            _conflict(project, "fix")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, f"This build isn't waiting for a decision (status '{project.status}').")
        )
    # Every route that starts model calls asks first, as `run` and `resume` do —
    # a default changed since the last phase would otherwise fail inside it.
    _require_models(project)
    token = _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value})
    if not token:
        raise _conflict(project, "fix")

    for tracked in rows:
        tracked.status = FindingStatus.FIX_REQUESTED.value
    db.commit()
    finding = remediation.as_finding(row)
    background.add_task(
        _drive_redo, project.id, row.owner_phase, remediation.fix_instruction([finding]), token
    )
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Sent back — that agent is fixing it, and the review runs again after.",
    )


@router.post("/{project_id}/security/{key}/waive")
def waive_finding(
    key: str,
    payload: WaiveRequest,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """Accept a finding deliberately, with the reason recorded beside it.

    A reason is required. "Waived" with nothing attached is indistinguishable from
    the silence this whole path exists to replace — and it is the one record anyone
    reading this build later has of why a known issue was shipped.
    """
    rows = _findings(db, project, key)
    row = rows[0]
    kind = (payload.kind or "").strip() or None
    if remediation.row_is_serious(row) and kind not in autofix.WAIVE_KINDS:
        # A serious finding is the crew's to fix. Waiving one is a risk decision, and
        # the record says which kind: it was never real, it is handled elsewhere, or
        # the risk is knowingly accepted.
        raise HTTPException(
            422,
            "Waiving a serious finding needs a reason kind — false positive, mitigated "
            "elsewhere, or accepted risk — as well as the reason itself.",
        )
    if kind is not None and kind not in autofix.WAIVE_KINDS:
        raise HTTPException(422, f"'{kind}' is not a waiver reason this build records.")
    for tracked in rows:
        tracked.status = FindingStatus.WAIVED.value
        tracked.note = payload.reason.strip()
        tracked.waive_kind = kind
    db.commit()
    log.info("Security finding waived on %s: %s — %s", project.id, row.title, row.note)
    return {
        "key": row.finding_key,
        "status": row.status,
        "note": row.note,
        "waive_kind": row.waive_kind,
        "unresolved": len(remediation.unresolved(db, project)),
    }


# ── when the crew asked for help ─────────────────────────────────────────────
def _require_needs_help(project: Project, action: str) -> None:
    if (
        project.status != PipelineStatus.AWAITING_APPROVAL.value
        or project.gate_kind != GateKind.NEEDS_HELP.value
    ):
        raise (
            _conflict(project, action)
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, "This build isn't waiting for help.")
        )


@router.post("/{project_id}/auto-fix/retry", response_model=RunResponse)
def keep_trying(
    background: BackgroundTasks,
    payload: Optional[KeepTryingRequest] = None,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Give the crew more rounds on whatever it stopped on, and carry on.

    Also how a build moves on once every serious finding it stopped on has been
    waived: with nothing left to fix, the loop finds nothing and the run continues.
    """
    _require_needs_help(project, "retry")
    _require_models(project)
    more = (payload.rounds if payload and payload.rounds else None) or settings.auto_fix_retry_rounds
    data = autofix.load(project)
    names = autofix.keep_trying(data, more)
    token = _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value})
    if not token:
        raise _conflict(project, "retry")
    autofix.save(project, data)
    db.commit()
    log.info("Keep trying on %s: %s, %d more round(s)", project.id, names, more)
    background.add_task(_drive, project.id, token)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message=f"Trying again — up to {more} more round{'' if more == 1 else 's'}.",
    )


@router.post("/{project_id}/auto-fix/accept", response_model=RunResponse)
def accept_code_problems(
    payload: AcceptRequest,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Carry on past code the crew could not fix, with the reason on the record.

    Only for compile errors and stack contradictions. A serious security finding is
    waived one at a time, with its own reason.
    """
    _require_needs_help(project, "accept")
    if payload.kind not in autofix.WAIVE_KINDS:
        raise HTTPException(422, f"'{payload.kind}' is not a reason this build records.")
    data = autofix.load(project)
    if not any(autofix.accepts_wholesale(n) for n in autofix.stuck(data)):
        raise HTTPException(
            400,
            "There are no code problems to move past — waive the security findings "
            "one at a time instead.",
        )
    _require_models(project)
    token = _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value})
    if not token:
        raise _conflict(project, "accept")
    names = autofix.accept(data, payload.kind, payload.reason.strip())
    autofix.save(project, data)
    db.commit()
    log.info("Code problems accepted on %s: %s — %s", project.id, names, payload.reason)
    background.add_task(_drive, project.id, token)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Continuing — the problems you accepted are on the record.",
    )


@router.post("/{project_id}/tests/waive")
def waive_failing_tests(
    payload: AcceptRequest,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> dict:
    """Ship past QA's failing tests at a review, with the reason on the record (#76).

    For a red suite met at a review rather than one the crew gave up on (that is
    `auto-fix/accept`). Covers exactly the failures on screen: a different test
    failing after a later rebuild is a new problem. The build stays parked; approving
    is still the reviewer's next click.
    """
    if project.status != PipelineStatus.AWAITING_APPROVAL.value:
        raise (
            _conflict(project, "waive")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, "There is nothing waiting for a decision.")
        )
    if payload.kind not in autofix.WAIVE_KINDS:
        raise HTTPException(422, f"'{payload.kind}' is not a reason this build records.")
    failing = runner.failing_tests(project)
    if not failing:
        raise HTTPException(400, "No tests are failing, so there is nothing to waive.")
    data = autofix.load(project)
    autofix.accept_tests(data, payload.kind, payload.reason.strip(), [f["key"] for f in failing])
    autofix.save(project, data)
    db.commit()
    log.info("Failing tests waived on %s (%d): %s — %s", project.id, len(failing), payload.kind, payload.reason)
    return {"waived": len(failing), "kind": payload.kind, "reason": payload.reason.strip()}


@router.post("/{project_id}/redo", response_model=RunResponse)
def redo_phase(
    payload: RedoRequest,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Send one phase back to its agent without leaving the review you are in.

    The Ship review shows a file tree assembled from four phases, so "this file is
    wrong" has to reach whoever wrote that file. Reject can only ever address the
    phase on screen; this addresses the one named.
    """
    if payload.phase not in {p.value for p in PHASE_ORDER}:
        raise HTTPException(400, f"'{payload.phase}' is not a phase of this pipeline.")
    if not payload.feedback.strip():
        raise HTTPException(400, "Say what to change — an agent cannot act on blank feedback.")
    _refuse_credentials(payload.feedback, project)
    _refuse_at_database_gate(project)
    if project.status != PipelineStatus.AWAITING_APPROVAL.value:
        raise (
            _conflict(project, "redo")
            if project.status == PipelineStatus.RUNNING.value
            else HTTPException(400, f"Nothing to redo (status '{project.status}').")
        )
    if not any(ph.phase == payload.phase and ph.output for ph in project.phases):
        raise HTTPException(
            400, f"The {payload.phase} phase hasn't produced anything to redo yet."
        )
    # Every route that starts model calls asks first, as `run` and `resume` do —
    # a default changed since the last phase would otherwise fail inside it.
    _require_models(project)
    token = _claim(db, project, {PipelineStatus.AWAITING_APPROVAL.value})
    if not token:
        raise _conflict(project, "redo")

    background.add_task(_drive_redo, project.id, payload.phase, payload.feedback.strip(), token)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Sent back — that agent is redoing its work with your note.",
    )


@router.post("/{project_id}/stop", response_model=RunResponse)
def stop_pipeline(
    payload: Optional[StopRequest] = None,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Stop a run, interrupting the model call in flight.

    Marked `cancelled` immediately so the UI is never stuck watching a run it has
    already abandoned — and everything produced so far is kept for the resume. The
    call in flight is cancelled, so the runtime stops generating: over the connector
    that is a `cancel` to the user's computer, and on this machine the connection
    to the runtime is closed.
    """
    if project.status not in (
        PipelineStatus.RUNNING.value,
        PipelineStatus.AWAITING_APPROVAL.value,
        PipelineStatus.PAUSED.value,
    ):
        raise HTTPException(400, f"This build isn't running (status '{project.status}').")

    reason = (payload.reason if payload and payload.reason else None) or (
        "Stopped by you. Resume picks up from the last approved phase."
    )
    runner.stop(db, project, reason)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Stopped. Anything already generated is kept — resume when you're ready.",
    )


@router.post("/{project_id}/resume", response_model=RunResponse)
def resume_pipeline(
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> RunResponse:
    """Pick a stopped, failed or stalled run back up from its last checkpoint."""
    resumable = {
        PipelineStatus.CANCELLED.value,
        PipelineStatus.FAILED.value,
        PipelineStatus.CREATED.value,
        PipelineStatus.PAUSED.value,
    }
    # A live `running` project is doing fine; only a stalled one may be taken over.
    if project.status == PipelineStatus.RUNNING.value and project.stalled:
        resumable.add(PipelineStatus.RUNNING.value)
    if project.status not in resumable:
        still_running = project.status == PipelineStatus.RUNNING.value
        raise HTTPException(
            400,
            f"Nothing to resume (status '{project.status}')."
            + (" This build is still running — stop it first." if still_running else ""),
        )

    # The same check the first start makes. A run that failed *because* a model was
    # missing is exactly the run someone reaches for Resume on, and starting it again
    # into the identical failure teaches nothing.
    _require_models(project)
    # Claimed first. Clearing the stop flag before the claim let a run stopped
    # mid-call read "not cancelled" under its old claim and carry on (#41); and a
    # resume that lost the race to a second tab must not have touched the row.
    token = _claim(db, project, resumable)
    if not token:
        raise _conflict(project, "resume")
    runner.prepare_resume(db, project)

    background.add_task(_drive, project.id, token)
    return RunResponse(
        project_id=project.id,
        status=project.status,
        current_phase=project.current_phase,
        message="Resumed from the last checkpoint.",
    )
