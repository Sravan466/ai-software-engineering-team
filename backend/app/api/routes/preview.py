"""Visual preview: render a project as a clickable site, and edit it element by element.

A post-pipeline, non-destructive editing loop that sits alongside the phase-approval gates.
Each generate/edit/patch appends a `PreviewRevision` made from the current head, and
undo/redo move that head over the history (`app.preview.history`) — no row is deleted,
so the original build is always one undo away and never gone.

Three ways to change the mockup, cheapest first:
  • `/patch` — text, Tailwind classes and `src`/`alt`/`href` on elements found by
    their `data-oid`. No model call.
  • `/theme` — the site's design tokens (fonts, palette, radius, shadow, density),
    rewritten in `<head>`. Every page restyles; no model call.
  • `/edit` — a plain-language change, scoped to one section or one element. The
    model sees only that part, and a part too large for it is refused, never cut.

Generating is a build of many model calls, so it does not happen inside the request:
`POST /generate` starts it and returns, and `GET` reports how far it has got. An edit
is one call (two with a repair) and stays synchronous.

**The running app (#78).** Once the crew has written a frontend that builds, the
Preview tab shows *it* — the generated app, built and served from the sandbox
(`app.preview.app_runtime`) — and the mockup above is the sketch: the early preview
before there is code, and the fallback when the app can't run. `PreviewOut.source`
says which is on screen and why. Edits sent with `target: "app"` change the code, not
a picture: each becomes a new Frontend attempt (`app.preview.app_edits`), checked and
built like any other, and in the archive and the deploy from then on. They run as a
short pipeline step rather than inside the request, so they return at once.
"""
from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.analytics import tracker
from app.api.deps import get_project
from app.core import identity
from app.core.constants import RoutingMode
from app.core.logging import get_logger
from app.db.base import SessionLocal, get_db
from app.db.models import PreviewRevision, Project
from app.preview import app_edits, app_runtime, app_state
from app.preview import design as D
from app.preview import history, oids, service
from app.preview.document import ThemeRefused, apply_theme, is_site, site_data
from app.preview.generator import EditRejected, EditTooLarge, edit_element, edit_section
from app.preview.html import (
    _VOID as VOID_TAGS,
    attr,
    extract_section,
    outer_element,
    replace_section,
    route_spans,
    scan_sections,
    with_attrs,
    wrap,
)
from app.preview.jobs import Reporter, jobs
from app.router.base import ProviderError
from app.router.router import router as model_router
from app.schemas.llm import LLMResponse
from app.schemas.preview import (
    AppOut,
    AppStartRequest,
    LocateOut,
    PreviewEditRequest,
    PreviewOut,
    PreviewPatchRequest,
    PreviewThemeRequest,
)

router = APIRouter(prefix="/api/projects", tags=["preview"])
log = get_logger(__name__)

def _provider_hint() -> str:
    """Advice that names the model this install actually uses.

    A literal here told everyone to pull one particular model, so anyone who had
    chosen a different one was handed a fix for a model they were not running. The
    router is asked rather than the settings object, because a choice made in
    Settings lands there first — `.env` is only where the default started.
    """
    default = model_router.local_default()
    if default is None:
        return " If you're running Local-Only, start a local runtime and choose a model in Settings."
    source = model_router.source(default[0])
    where = source.source.label if source is not None else default[0]
    return (
        f" If you're running Local-Only, make sure {where} is running and has "
        f"'{default[1]}'."
    )


# ── helpers ──────────────────────────────────────────────────────────────────
def _revisions(db: Session, project_id: str) -> List[PreviewRevision]:
    return history.revisions(db, project_id)


def _current(db: Session, project: Project) -> tuple[List[PreviewRevision], PreviewRevision]:
    """Every revision and the live one — or 400 when there is nothing to change yet."""
    revs = _revisions(db, project.id)
    live = history.head(revs)
    if live is None:
        raise HTTPException(400, "No preview to edit yet — generate one first.")
    return revs, live


def _save_revision(
    db: Session,
    project: Project,
    html: str,
    *,
    source: str,
    responses: Optional[List[LLMResponse]] = None,
    section_id: Optional[str] = None,
    instruction: Optional[str] = None,
) -> PreviewRevision:
    last = responses[-1] if responses else None
    made_from = history.head(_revisions(db, project.id))
    row = history.new_row(
        db,
        project.id,
        html=oids.tag(html),
        source=source,
        section_id=section_id,
        instruction=instruction,
        model_used=last.model if last else None,
        provider_used=last.provider if last else None,
        # An edit is of the same sketch: drawn from the same Frontend attempt.
        built_from=made_from.built_from if made_from is not None else None,
    )
    for resp in responses or []:  # fold preview tokens into the project's cost/analytics
        tracker.record(db, response=resp, project_id=project.id, phase="preview")
    return row


def _theme(html: str) -> Optional[dict]:
    data = site_data(html)
    if data is None or not is_site(html):
        return None
    ds = D.from_dict(data.get("design"), str(data.get("product") or ""))
    return {
        "current": {k: ds.as_dict()[k] for k in ("primary", "accent", "tint", "font_pair", "radius", "shadow", "density")},
        "fonts": [
            {"id": key, "label": spec["label"], "display": spec["display"], "body": spec["body"]}
            for key, spec in D.FONT_PAIRS.items()
        ],
        "tints": list(D._TINTS),
        "radii": list(D._RADIUS),
        "shadows": list(D._SHADOW),
        "densities": list(D._DENSITY),
        # The ramp the design system derives from the brand colour — the swatches a
        # colour control offers, so a picked colour is one the site already speaks.
        "palette": {
            "primary": {str(k): v for k, v in ds.primary_scale.items()},
            "accent": {str(k): v for k, v in ds.accent_scale.items()},
            "neutral": {str(k): v for k, v in ds.neutral_scale.items()},
        },
    }


def _sketch_from(live: Optional[PreviewRevision], revs: List[PreviewRevision]) -> Optional[str]:
    return next((r.built_from for r in history.lineage(live, revs) if r.built_from), None) if live else None


def _source(html: Optional[str], app: dict) -> tuple[Optional[str], Optional[str]]:
    """What the Preview tab shows, and the line that says so (#78).

    The app whenever it can run — starting counts, its progress is on screen — and
    the sketch only when it can't, with the reason it can't."""
    status = app.get("status")
    if status in ("running", "starting", "idle", "waiting"):
        return "app", None
    if status == "none":
        return ("sketch" if html else None), app.get("reason") or "The crew hasn't written the frontend yet."
    if status == "failed":
        return ("sketch" if html else None), app.get("reason") or "The app failed to build."
    return ("sketch" if html else None), app.get("reason")


def _preview_out(db: Session, project: Project, touch: bool = False) -> PreviewOut:
    revs = _revisions(db, project.id)
    live = history.head(revs)
    # Ids are added on read as well as on save: a mockup drawn before they existed
    # gets the same ids every time (tagging is deterministic), so it can be edited
    # element by element without first being rewritten.
    html = oids.tag(live.html) if live else None
    # An edit does not rebuild the site, so it carries no report of its own; the
    # build it was made from is still the one being described.
    report = (
        next((r.report for r in history.lineage(live, revs) if r.report), None)
        if html and is_site(html)
        else None
    )
    return PreviewOut(
        project_id=project.id,
        html=html,
        sections=scan_sections(html) if html else [],
        routes=[{"path": r["path"], "title": r["title"]} for r in route_spans(html)] if html else [],
        revisions=revs,
        has_frontend=any(ph.phase == "frontend_engineer" for ph in project.phases),
        report=report,
        job=jobs.visible(project.id),
        head_id=live.id if live else None,
        can_undo=bool(live and history.parent(live, revs)),
        can_redo=bool(live and history.redo_target(live, revs)),
        theme=_theme(html) if html else None,
        **_app_fields(project, html, live, revs, touch),
    )


def _app_fields(project: Project, html: Optional[str], live, revs, touch: bool) -> dict:
    current = app_runtime.current_frontend(project)
    app = app_edits.out(project, touch=touch, current=current)
    source, note = _source(html, app)
    drawn = _sketch_from(live, revs)
    row = current[0]
    kept_from = (row.handoff or {}).get("kept_from") if row is not None and isinstance(row.handoff, dict) else None
    stale = bool(drawn and row is not None and drawn not in (row.id, kept_from))
    return {
        "source": source,
        "source_note": note,
        "app": AppOut(**app),
        "sketch_built_from": drawn,
        "sketch_stale": stale,
    }


def _too_large(e: EditTooLarge) -> HTTPException:
    return HTTPException(
        422,
        f"That part is too large for the model to rewrite in one go ({e.size:,} characters; it can read "
        f"about {e.room:,}). Select a smaller element inside it — a heading, a card, a button — and try again.",
    )


def _rejected(db: Session, project: Project, e: EditRejected) -> HTTPException:
    # The edit came back without what makes it work, twice. Saving it would leave a
    # list that no longer lists or a form that no longer stores, looking exactly as
    # if it did — so the tokens are billed and the change is not.
    for resp in e.responses:
        tracker.record(db, response=resp, project_id=project.id, phase="preview")
    return HTTPException(
        422,
        "That change would break how this part works, so it wasn't applied: "
        + "; ".join(e.problems[:3])
        + ". Try describing the change differently.",
    )


def _fail(exc: Exception, what: str) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=f"A model provider failed while {what}: {exc}.{_provider_hint()}",
    )


def _busy(project: Project) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail="The mockup is being built right now. Wait for it to finish, then try again.",
    )


def _build_in_background(project_id: str, reporter: Reporter) -> None:
    """The work behind `POST /generate`, on its own session and thread."""
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is None:
            return
        # Its own thread starts with no account; the mockup is drawn on the owner's.
        with identity.acting_as(project.owner_id):
            row, _ = app_runtime.current_frontend(project)
            try:
                service.build_and_save(db, project, reporter, built_from=row.id if row is not None else None)
            except ProviderError as e:
                raise RuntimeError(f"A model provider failed while drawing the mockup: {e}.{_provider_hint()}") from e
    finally:
        db.close()


# ── routes ───────────────────────────────────────────────────────────────────
@router.get("/{project_id}/preview", response_model=PreviewOut)
def get_preview(
    touch: bool = False, project: Project = Depends(get_project), db: Session = Depends(get_db)
) -> PreviewOut:
    """`touch`: the Preview tab is open and showing the app — keep it running."""
    return _preview_out(db, project, touch=touch)


# ── the running app (#78) ────────────────────────────────────────────────────
@router.post("/{project_id}/preview/app", response_model=PreviewOut)
def start_app(
    payload: Optional[AppStartRequest] = None,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> PreviewOut:
    """Build and start the app for the current frontend, unless it runs or is starting.

    Returns at once; `GET /preview` reports the steps. `retry` builds again an attempt
    whose last preview build failed."""
    ok, why = app_runtime.available()
    if not ok:
        raise HTTPException(409, why)
    retry = bool(payload and payload.retry)
    if retry and app_state.failed(project) is not None:
        app_state.clear_failure(project)
        db.commit()
    app_runtime.ensure(project, retry=retry)
    return _preview_out(db, project, touch=True)


@router.get("/{project_id}/app", response_model=AppOut)
def get_app(project: Project = Depends(get_project)) -> AppOut:
    """The running app's state and address — the address is the only way to it."""
    return AppOut(**app_edits.out(project))


@router.get("/{project_id}/preview/locate", response_model=LocateOut)
def locate(oid: str, project: Project = Depends(get_project)) -> LocateOut:
    """Where an element on the app comes from, and what can be changed there directly."""
    return LocateOut(**app_edits.locate(project, oid))


def _drive_revise(project_id: str, spec: dict, token: str) -> None:
    """Background task: make a change from the app preview to the code."""
    from app.api.routes.projects import _strand
    from app.orchestration.runner import runner

    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is not None:
            runner.revise_frontend(db, project, spec, claim_token=token)
    except Exception as e:  # noqa: BLE001 - a background crash must not strand the run
        log.exception("Preview change crashed for %s", project_id)
        try:
            db.rollback()
            project = db.get(Project, project_id)
            if project is not None:
                app_state.refuse_edit(project, "The platform hit an internal error making that change.")
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        _strand(db, project_id, str(e), token)
    finally:
        db.close()


def _app_change(
    db: Session,
    project: Project,
    background: BackgroundTasks,
    spec: Optional[dict],
    *,
    kind: str,
    label: str,
    files: list[str],
) -> PreviewOut:
    """Hand a change to the crew as a short run of its own, and answer at once."""
    from app.api.routes.projects import _claim, _require_models

    if spec is None:
        return _preview_out(db, project)
    if kind == "ask":
        _require_models(project)
    was = project.status
    token = _claim(db, project, set(app_edits.EDITABLE))
    if not token:
        raise HTTPException(409, "Something else started on this build a moment ago. Reload to see where it is.")
    app_state.begin_edit(project, kind, label, files)
    db.commit()
    background.add_task(_drive_revise, project.id, {**spec, "kind": kind, "label": label, "was": was}, token)
    return _preview_out(db, project)


def _clip_label(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@router.post("/{project_id}/preview/generate", response_model=PreviewOut, status_code=202)
def generate(project: Project = Depends(get_project), db: Session = Depends(get_db)) -> PreviewOut:
    """Start building the mockup. Returns at once; `GET` reports progress."""
    project_id = project.id
    started = jobs.start(
        project_id, "request", lambda reporter: _build_in_background(project_id, reporter)
    )
    if not started:
        raise _busy(project)
    return _preview_out(db, project)


@router.post("/{project_id}/preview/edit", response_model=PreviewOut)
def edit(
    payload: PreviewEditRequest,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> PreviewOut:
    if payload.target == "app":
        # "Ask the crew" on the running app: the file that draws the element, changed
        # by the Frontend Engineer's own per-file write call.
        from app.api.routes.projects import _refuse_credentials

        if not payload.oid:
            raise HTTPException(400, "Select an element in the app first.")
        _refuse_credentials(payload.instruction, project)
        app_edits.guard(project, payload.built_from)
        spec, path = app_edits.ask_spec(project, payload.oid, payload.instruction)
        return _app_change(
            db, project, background, spec,
            kind="ask", label=_clip_label(f"Preview: {payload.instruction}"), files=[path],
        )
    if jobs.running(project.id):
        raise _busy(project)
    if not payload.section_id and not payload.oid:
        raise HTTPException(400, "Say what to change: a section_id or an element's oid.")
    _revs, live = _current(db, project)
    current = oids.tag(live.html)

    section_id = payload.section_id
    if payload.oid:
        found = oids.locate(current, payload.oid)
        if found is None:
            raise HTTPException(400, "That element isn't in the current preview. Select it again.")
        # A section's own element is edited as a section, under its contract.
        own = attr(found[0].group(2), "data-section")
        if own:
            section_id = own
        else:
            return _edit_element(db, project, current, payload.oid, payload.instruction)

    fragment = extract_section(current, section_id)
    if fragment is None:
        raise HTTPException(400, f"Section '{section_id}' isn't in the current preview.")
    try:
        new_fragment, responses = edit_section(
            fragment,
            payload.instruction,
            mode=RoutingMode(project.routing_mode),
            preferred_model=project.preferred_model,
            site=site_data(current),
            section_id=section_id,
        )
    except ProviderError as e:
        raise _fail(e, "editing the section")
    except EditTooLarge as e:
        raise _too_large(e)
    except EditRejected as e:
        raise _rejected(db, project, e)
    if "<" not in new_fragment:
        raise HTTPException(502, "The model didn't return a usable section. Try rephrasing the change.")
    new_html = replace_section(current, section_id, new_fragment)
    _save_revision(
        db,
        project,
        new_html,
        source="edited",
        responses=responses,
        section_id=section_id,
        instruction=payload.instruction,
    )
    return _preview_out(db, project)


def _edit_element(db: Session, project: Project, current: str, oid: str, instruction: str) -> PreviewOut:
    fragment = oids.outer(current, oid) or ""
    try:
        new_fragment, responses = edit_element(
            fragment,
            instruction,
            context=oids.ancestors(current, oid),
            mode=RoutingMode(project.routing_mode),
            preferred_model=project.preferred_model,
            site=site_data(current),
        )
    except ProviderError as e:
        raise _fail(e, "editing that element")
    except EditTooLarge as e:
        raise _too_large(e)
    except EditRejected as e:
        raise _rejected(db, project, e)
    # The element keeps its id, so the selection survives the edit.
    parts = outer_element(new_fragment)
    if parts is None:
        raise HTTPException(502, "The model didn't return a usable element. Try rephrasing the change.")
    tag, attrs, inner = parts
    attrs = with_attrs(attrs.rstrip().rstrip("/"), {oids.ATTR: oid})
    # A void element (<img>, <input>) is its start tag; wrapping it would emit </img>.
    new_fragment = f"<{tag}{attrs}>" if tag in VOID_TAGS else wrap(tag, attrs, inner)
    section = _section_of(current, oid)
    _save_revision(
        db,
        project,
        oids.replace(current, oid, new_fragment),
        source="edited",
        responses=responses,
        section_id=section,
        instruction=instruction,
    )
    return _preview_out(db, project)


def _section_of(html: str, oid: str) -> Optional[str]:
    """The `data-section` an element sits in, for the revision's record."""
    context = oids.ancestors(html, oid)
    for opening in reversed(context):
        sid = attr(opening, "data-section")
        if sid:
            return sid[:64]
    return None


@router.post("/{project_id}/preview/patch", response_model=PreviewOut)
def patch(
    payload: PreviewPatchRequest,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> PreviewOut:
    """Direct edits — text, classes, links, images — by element id. No model call.

    On the app (#78) each is made to the JSX that renders the element, where the code
    states it plainly; anything computed is refused with the reason."""
    if payload.target == "app":
        app_edits.guard(project, payload.built_from)
        ops = [op.model_dump() for op in payload.ops]
        spec = app_edits.patch_spec(project, ops)
        files = sorted({a.path for a in (app_edits.source.parse(op["oid"]) for op in ops) if a is not None})
        summary = payload.summary or f"{len(ops)} direct change{'s' if len(ops) != 1 else ''}"
        return _app_change(
            db, project, background, spec, kind="patch", label=_clip_label(f"Preview: {summary}"), files=files,
        )
    if jobs.running(project.id):
        raise _busy(project)
    _revs, live = _current(db, project)
    current = oids.tag(live.html)
    try:
        new_html = oids.apply_ops(current, [op.model_dump() for op in payload.ops])
    except oids.PatchRefused as e:
        raise HTTPException(422, e.reason)
    if new_html == current:
        return _preview_out(db, project)
    _save_revision(
        db,
        project,
        new_html,
        source="patched",
        instruction=(payload.summary or f"{len(payload.ops)} direct change{'s' if len(payload.ops) != 1 else ''}"),
    )
    return _preview_out(db, project)


@router.patch("/{project_id}/preview/theme", response_model=PreviewOut)
def theme(
    payload: PreviewThemeRequest,
    background: BackgroundTasks,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> PreviewOut:
    """Site style: fonts, palette and shape for every page at once. No model call.

    On the app (#78) the style is written into the Tailwind config the platform owns
    (and the global stylesheet's font import), so it ships."""
    changes = {
        k: v for k, v in payload.model_dump().items() if v is not None and k not in ("target", "built_from")
    }
    if payload.target == "app":
        if not changes:
            raise HTTPException(400, "Nothing to change — send at least one style.")
        row = app_edits.guard(project, payload.built_from)
        spec = app_edits.theme_spec(row, changes)
        label = "Preview: site style — " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in changes.items())
        return _app_change(
            db, project, background, spec, kind="theme", label=_clip_label(label), files=["frontend/tailwind.config.js"],
        )
    if jobs.running(project.id):
        raise _busy(project)
    _revs, live = _current(db, project)
    if not changes:
        raise HTTPException(400, "Nothing to change — send at least one style.")
    try:
        new_html = apply_theme(oids.tag(live.html), changes)
    except ThemeRefused as e:
        raise HTTPException(409 if "Rebuild" in str(e) else 422, str(e))
    _save_revision(
        db,
        project,
        new_html,
        source="themed",
        instruction="Site style: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in changes.items()),
    )
    return _preview_out(db, project)


@router.post("/{project_id}/preview/undo", response_model=PreviewOut)
def undo(
    background: BackgroundTasks,
    target: str = "sketch",
    built_from: Optional[str] = None,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> PreviewOut:
    if target == "app":
        # The Frontend attempt before the last change made here, made current again
        # as a new attempt with its code — attempts are never deleted.
        row = app_edits.guard(project, built_from)
        spec = app_edits.restore_spec(project, row, forward=False)
        return _app_change(db, project, background, spec, kind="undo", label="Preview: undo", files=[])
    if jobs.running(project.id):
        raise _busy(project)
    revs = _revisions(db, project.id)
    live = history.head(revs)
    if live is None:
        raise HTTPException(400, "Nothing to undo — no preview revisions yet.")
    before = history.parent(live, revs)
    if before is None:
        raise HTTPException(409, "This is the original build — there's nothing before it to go back to.")
    history.make_head(db, before)
    return _preview_out(db, project)


@router.post("/{project_id}/preview/redo", response_model=PreviewOut)
def redo(
    background: BackgroundTasks,
    target: str = "sketch",
    built_from: Optional[str] = None,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
) -> PreviewOut:
    if target == "app":
        row = app_edits.guard(project, built_from)
        spec = app_edits.restore_spec(project, row, forward=True)
        return _app_change(db, project, background, spec, kind="redo", label="Preview: redo", files=[])
    if jobs.running(project.id):
        raise _busy(project)
    revs = _revisions(db, project.id)
    live = history.head(revs)
    after = history.redo_target(live, revs) if live else None
    if after is None:
        raise HTTPException(409, "Nothing to redo.")
    history.make_head(db, after)
    return _preview_out(db, project)


@router.get("/{project_id}/preview/revisions/{revision_id}")
def revision_html(
    revision_id: str, project: Project = Depends(get_project), db: Session = Depends(get_db)
) -> dict:
    """One revision's document — the "before" of a model edit's before/after."""
    row = next((r for r in _revisions(db, project.id) if r.id == revision_id), None)
    if row is None:
        raise HTTPException(404, "That revision isn't part of this mockup.")
    return {"id": row.id, "html": oids.tag(row.html)}
