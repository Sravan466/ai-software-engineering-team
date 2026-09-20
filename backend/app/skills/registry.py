"""The library: every skill on this machine, whether it is on, and how to change it.

Two directories feed it. `backend/skills/` ships in the repository — the procedures
this platform believes in, versioned with the code that uses them. `data/skills/` is
the operator's own, gitignored and sitting beside `providers.local.json`, so adding a
skill never means editing a checkout.

A user skill **shadows** a bundled one of the same name. That is what makes the
bundled library editable without being mutable: `PUT /api/skills/api-contract-design`
writes an override into `data/skills/`, and `DELETE` on that same name removes the
override and hands the bundled one back. Nothing in the repository is ever written to.

Which skills are switched off is state, not content, so it lives in its own small
file rather than being written back into the markdown. Turning a bundled skill off
and later pulling a new version of it should not be a merge conflict.

Everything here degrades to an empty library. A missing directory, an unreadable
file, a corrupt state file: the pipeline gets no skills and runs exactly as it did
before skills existed, which is the same contract RAG and memory already hold to.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.constants import PHASE_ORDER
from app.core.logging import get_logger
from app.skills.loader import SKILL_FILE, Skill, load_file, to_markdown

log = get_logger(__name__)

#: Relative to the backend process cwd, mirroring `sqlite:///./data/aiteam.db`.
_STATE_PATH = Path("data") / "skills.local.json"

#: Where the bundled library lives when the configured path does not resolve — the
#: directory beside `app/`, found from this file rather than from the cwd. A backend
#: started from somewhere other than `backend/` still ships with its own skills.
_PACKAGED_DIR = Path(__file__).resolve().parents[2] / "skills"


def known_phases() -> frozenset[str]:
    return frozenset(p.value for p in PHASE_ORDER)


def bundled_dir() -> Path:
    configured = Path(settings.skills_bundled_dir)
    return configured if configured.is_dir() else _PACKAGED_DIR


def user_dir() -> Path:
    return Path(settings.skills_user_dir)


# ── which skills are switched off ────────────────────────────────────────────
def _read_state() -> dict:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 - a corrupt file must not empty the library
        log.warning("Skill state file could not be read (%s); every skill stays on.", e)
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def disabled_names() -> set[str]:
    return {str(n) for n in (_read_state().get("disabled") or [])}


def set_enabled(name: str, enabled: bool) -> None:
    state = _read_state()
    off = {str(n) for n in (state.get("disabled") or [])}
    if enabled:
        off.discard(name)
    else:
        off.add(name)
    state["disabled"] = sorted(off)
    _write_state(state)


# ── reading the library ──────────────────────────────────────────────────────
def _scan(directory: Path, source: str) -> dict[str, Skill]:
    """Every `<dir>/<name>/SKILL.md` under `directory`, keyed by name."""
    found: dict[str, Skill] = {}
    try:
        entries = sorted(p for p in directory.iterdir() if p.is_dir())
    except FileNotFoundError:
        return found
    except Exception as e:  # noqa: BLE001
        log.warning("Skill directory %s could not be listed: %s", directory, e)
        return found

    for entry in entries:
        path = entry / SKILL_FILE
        if not path.is_file():
            continue
        skill = load_file(path, source, settings.skill_body_max_chars, known_phases())
        if skill is None:
            continue
        # The directory name wins over a frontmatter `name` that disagrees with it:
        # the directory is what a shadowing override has to match, and a file whose
        # two names differ would shadow something the author never named.
        if skill.name != entry.name:
            log.warning(
                "Skill in %s declares the name '%s'; using the folder name '%s'.",
                entry,
                skill.name,
                entry.name,
            )
            skill = replace(skill, name=entry.name)
        found[skill.name] = skill
    return found


def library() -> list[Skill]:
    """Every skill this machine has, user overrides shadowing bundled ones by name.

    Sorted by name so the library, the preview and the prompt all agree on order
    whenever scores tie — selection has to be reproducible from one run to the next
    or "which skills did this phase get?" has no stable answer.
    """
    if not settings.skills_enabled:
        return []
    merged = {**_scan(bundled_dir(), "bundled"), **_scan(user_dir(), "user")}
    return sorted(merged.values(), key=lambda s: s.name)


def get(name: str) -> Optional[Skill]:
    return next((s for s in library() if s.name == name), None)


# ── changing it ──────────────────────────────────────────────────────────────
class SkillError(ValueError):
    """A change that cannot be made, in words the API hands straight to the user."""


def _user_path(name: str) -> Path:
    return user_dir() / name / SKILL_FILE


def write(skill: Skill) -> Skill:
    """Save a skill to `data/skills/`, and hand back what loading it again yields.

    Written and then re-read rather than returned as passed: what the library will
    serve from now on is the file, and if the round trip changes anything — a rule
    the body trips, a list the parser reads differently — the caller finds out now
    rather than the next time a build quietly does not get it.
    """
    path = _user_path(skill.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(skill), encoding="utf-8")
    saved = load_file(path, "user", settings.skill_body_max_chars, known_phases())
    if saved is None:
        raise SkillError("The skill was written but could not be read back.")
    return saved


def delete(name: str) -> Optional[Skill]:
    """Remove a skill added here. On an override, this restores the bundled one.

    Returns what the library holds afterwards — the bundled skill when this removed
    an override, and None when the skill is simply gone — because "your edit was
    undone" and "that skill no longer exists" are different outcomes of one button.
    """
    path = _user_path(name)
    if not path.is_file():
        raise SkillError(
            f"'{name}' isn't a skill added on this machine. Skills shipped with the "
            "platform can be switched off, or edited — which saves your version "
            "alongside and leaves the original where it is."
        )
    shutil.rmtree(path.parent, ignore_errors=True)
    return get(name)


def is_override(name: str) -> bool:
    """Whether a local skill of this name is shadowing a bundled one of the same name.

    The distinction the UI needs: a local skill that shadows something can be
    *restored*, and one that shadows nothing can only be deleted.
    """
    return _user_path(name).is_file() and (bundled_dir() / name / SKILL_FILE).is_file()
