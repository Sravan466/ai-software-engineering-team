"""The skill library: read it, change it, and see what a build would actually get.

Five endpoints, and the fifth is not a convenience. Selection is a keyword score
decided before the model call, which makes a miss *silent* — a skill that does not
match simply never arrives, and nothing in the finished build says so. `POST
/api/skills/preview` answers "which skills would this idea get, on this phase?"
before a build is started, which is the only way to see a miss without spending a
run on it.

Skills shipped with the platform are never written to. Editing one saves your
version into `data/skills/`, where it shadows the original; deleting that override
hands the original back. Switching one off is state, kept in its own small file, so
turning a bundled skill off and later pulling a new version of it is not a conflict.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.constants import PHASE_LABELS, PHASE_ORDER
from app.core.logging import get_logger
from app.skills import registry
from app.skills.loader import Skill, check, to_markdown
from app.skills.selection import Overrides, select

log = get_logger(__name__)

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillBody(BaseModel):
    """A skill as a person writes it — the frontmatter, flattened, plus the body."""

    name: Optional[str] = Field(
        None,
        description="Slug; also the folder it is saved in and the token a build pins by.",
    )
    title: str = ""
    description: str = ""
    agents: list[str] = Field(
        default_factory=list, description="Phases that may receive it. Empty means all."
    )
    keywords: list[str] = Field(
        default_factory=list, description="What makes it relevant to a particular build."
    )
    body: str = ""


def _as_dict(skill: Skill, disabled: set[str]) -> dict:
    return {
        "name": skill.name,
        "title": skill.title,
        "description": skill.description,
        "agents": list(skill.agents),
        "keywords": list(skill.keywords),
        "body": skill.body,
        "source": skill.source,
        "chars": skill.chars,
        "enabled": skill.name not in disabled,
        # A skill that breaks a load rule stays listed with its reasons showing. The
        # alternative is a file on disk that does nothing and never says why.
        "usable": skill.usable,
        "problems": list(skill.problems),
        #: True when a bundled skill is currently shadowed by a local edit — which
        #: is what makes "Restore the original" a thing the UI can offer.
        "overridden": registry.is_override(skill.name),
    }


@router.get("")
def list_skills() -> dict:
    """The whole library, plus what the selector needs to be understood."""
    disabled = registry.disabled_names()
    skills = registry.library()
    return {
        "skills": [_as_dict(s, disabled) for s in skills],
        "phases": [
            {"key": p.value, "label": PHASE_LABELS.get(p.value, p.value)} for p in PHASE_ORDER
        ],
        "enabled": settings.skills_enabled,
        "max_per_phase": settings.skills_max_per_phase,
        "max_chars": settings.skill_body_max_chars,
        "user_dir": str(registry.user_dir()),
        "bundled_dir": str(registry.bundled_dir()),
    }


@router.post("", status_code=201)
def create_skill(payload: SkillBody) -> dict:
    """Add a skill on this machine. It is live on the next phase that scores for it."""
    name = (payload.name or "").strip().lower()
    if registry.get(name) is not None:
        raise HTTPException(
            409,
            f"A skill called '{name}' already exists. Edit it instead — editing one "
            "that ships with the platform saves your version alongside the original.",
        )
    return _save(name, payload, created=True)


@router.put("/{name}")
def update_skill(name: str, payload: SkillBody) -> dict:
    """Change a skill. On a bundled one this writes a local copy that shadows it."""
    if registry.get(name) is None:
        raise HTTPException(404, f"There is no skill called '{name}'.")
    return _save(name, payload, created=False)


def _save(name: str, payload: SkillBody, created: bool) -> dict:
    known = registry.known_phases()
    problems = check(
        name,
        payload.title,
        payload.description,
        payload.body,
        settings.skill_body_max_chars,
    )
    unknown = [a for a in payload.agents if a not in known]
    if unknown:
        problems.append(
            f"'{unknown[0]}' is not a phase of this pipeline, so no agent would "
            "ever receive this skill."
        )
    if problems:
        # Refused at the door rather than saved and quietly never used. A skill that
        # is on disk, listed, and silently skipped is the failure mode this whole
        # feature is trying not to have.
        raise HTTPException(422, " ".join(problems))

    skill = Skill(
        name=name,
        title=payload.title.strip(),
        description=payload.description.strip(),
        agents=tuple(payload.agents),
        keywords=tuple(k.strip().lower() for k in payload.keywords if k.strip()),
        body=payload.body.strip(),
        source="user",
    )
    try:
        saved = registry.write(skill)
    except registry.SkillError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(
            500, f"The skill could not be written to {registry.user_dir()}: {e}"
        )
    log.info("Skill %s %s", saved.name, "added" if created else "updated")
    return _as_dict(saved, registry.disabled_names())


class EnabledUpdate(BaseModel):
    enabled: bool


@router.put("/{name}/enabled")
def set_enabled(name: str, payload: EnabledUpdate) -> dict:
    """Switch a skill on or off for every build that does not name it explicitly."""
    skill = registry.get(name)
    if skill is None:
        raise HTTPException(404, f"There is no skill called '{name}'.")
    registry.set_enabled(name, payload.enabled)
    return _as_dict(skill, registry.disabled_names())


@router.delete("/{name}")
def delete_skill(name: str) -> dict:
    """Remove a skill added here. On an edited bundled skill, restores the original."""
    if registry.get(name) is None:
        raise HTTPException(404, f"There is no skill called '{name}'.")
    try:
        restored = registry.delete(name)
    except registry.SkillError as e:
        raise HTTPException(400, str(e))
    disabled = registry.disabled_names()
    # A restored bundled skill is still a skill; a deleted user one is gone. The
    # caller is told which happened rather than being left to re-fetch and infer.
    return {
        "deleted": name,
        "restored": _as_dict(restored, disabled) if restored is not None else None,
    }


class PreviewRequest(BaseModel):
    """What a build would look like to the selector, before it is started."""

    idea: str = Field("", description="The product idea this build would be given.")
    pinned: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)


@router.post("/preview")
def preview(payload: PreviewRequest) -> dict:
    """Which skills each phase would get for this idea — the answer to a silent miss.

    Scored against the idea and the phase only. The prior phases have not run yet,
    so the real selection at, say, Warden will see more evidence than this does and
    may pick differently. That is the honest limit of a preview taken before the
    build: it shows what the idea alone earns, which is exactly the thing a person
    can still do something about.
    """
    overrides = Overrides(
        pinned=frozenset(payload.pinned), excluded=frozenset(payload.excluded)
    )
    # Read once and scored eight times. Letting each phase reach for the library
    # itself re-parses every file on disk eight times for one answer — and, worse,
    # would let a file changed mid-request give two phases different libraries.
    candidates = registry.library()
    phases = []
    for phase in PHASE_ORDER:
        chosen = select(phase.value, payload.idea, {}, overrides, candidates=candidates)
        phases.append(
            {
                "phase": phase.value,
                "label": PHASE_LABELS.get(phase.value, phase.value),
                "skills": [
                    {
                        "name": s.skill.name,
                        "title": s.skill.title,
                        "score": round(s.score, 2),
                        "pinned": s.pinned,
                        "matched": list(s.matched),
                        "reason": s.reason,
                        "chars": s.skill.chars,
                    }
                    for s in chosen
                ],
            }
        )
    return {"idea": payload.idea, "phases": phases}


@router.get("/{name}/markdown")
def skill_markdown(name: str) -> dict:
    """The skill as the file it is on disk — for copying one out, or reading it raw."""
    skill = registry.get(name)
    if skill is None:
        raise HTTPException(404, f"There is no skill called '{name}'.")
    return {"name": skill.name, "path": skill.path, "markdown": to_markdown(skill)}
