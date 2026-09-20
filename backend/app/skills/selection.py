"""Which skills a phase gets, decided without asking a model.

Deterministic on purpose. A selector that called a model would add a round trip to
every one of eight phases, and on the local path — this project's default — that is
minutes per build spent deciding what to read rather than reading it. Keyword score
against the work in front of the agent is cheap, reproducible, and explainable: the
preview endpoint can show *why* a skill was picked, which a model's opinion cannot.

The cost of that choice is honest and worth writing down: **a keyword miss is
silent**. A skill that does not match simply never arrives and nothing in the output
says so. That is what the per-build pin is for, and why the preview exists — it is
the only way to see a miss before spending a build on it.

The character budget is *not* decided here. It belongs to the agent, because only
the agent knows which model it is about to call and therefore how much room there
is; `AgentAdapter._section_budgets` gives `skills` a share alongside `rag` and
`memory`, and the packing happens there. A standalone constant added on top of that
allocator is how a phase ends up over the window on a small local model.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from app.core.config import settings
from app.core.constants import PHASE_LABELS
from app.skills import registry
from app.skills.loader import Skill

#: How much of the work in front of the agent is read when scoring. Prior phase
#: outputs are generated source by the time the back end has run — hundreds of
#: kilobytes — and a keyword that appears on line 9,000 of a file tree is not
#: evidence that a skill is relevant to the phase about to start.
_HAYSTACK_CHARS = 8000

#: A keyword that hits the idea is worth more than one that hits a file somewhere in
#: a prior phase's output: the idea is what the build *is*, the outputs are what it
#: has become. Both count, because "this build has a payments form in it" is exactly
#: the kind of thing only the outputs know.
_WEIGHT_IDEA = 3.0
_WEIGHT_PHASE = 2.0
_WEIGHT_PRIOR = 1.0

#: A skill bound to this phase by name beats an unbound one on a tie. It was written
#: for this agent; a general skill was written for whoever asks.
_BOUND_BONUS = 0.75

#: What a skill with no keywords at all scores. It declares no trigger, so it is
#: relevant to every build its agents run in — a baseline, below anything that
#: actually matched, and above nothing.
_UNKEYED_SCORE = 0.5


@dataclass(frozen=True)
class Selected:
    """One skill chosen for one phase, and why."""

    skill: Skill
    score: float
    pinned: bool
    #: The keywords that actually hit, in the order they were declared. Empty on a
    #: pin that matched nothing, which is the case the pin exists for.
    matched: tuple[str, ...]

    @property
    def reason(self) -> str:
        """Why this skill is here, in a sentence a person reads in the preview."""
        if self.pinned:
            return (
                "Pinned to this build"
                + (f" · matched {', '.join(self.matched)}" if self.matched else "")
            )
        if self.matched:
            return f"Matched {', '.join(self.matched)}"
        return "Bound to this phase"


@dataclass(frozen=True)
class Overrides:
    """What this build was told to do, over what scoring would have chosen.

    The automatic choice is a default, not a verdict: a keyword miss has to be
    correctable without editing the library, and a skill that is wrong for *this*
    build has to be removable without switching it off for every other one.
    """

    pinned: frozenset[str] = frozenset()
    excluded: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Overrides":
        if not isinstance(data, dict):
            return cls()
        return cls(
            pinned=frozenset(str(n) for n in (data.get("pinned") or []) if str(n).strip()),
            excluded=frozenset(str(n) for n in (data.get("excluded") or []) if str(n).strip()),
        )

    def as_dict(self) -> dict:
        return {"pinned": sorted(self.pinned), "excluded": sorted(self.excluded)}

    def __bool__(self) -> bool:
        return bool(self.pinned or self.excluded)


def _parts(idea: str, phase: str, prior_outputs: Optional[dict]) -> list[tuple[str, float]]:
    """The three texts a skill's keywords are scored against, and what each is worth.

    Three rather than one blob so a hit on the idea can outweigh one buried in a file
    the back end generated four phases ago.
    """
    label = PHASE_LABELS.get(phase, phase).lower()
    prior = ""
    if prior_outputs:
        try:
            prior = json.dumps(prior_outputs, default=str)[:_HAYSTACK_CHARS].lower()
        except Exception:  # noqa: BLE001 - scoring must never fail a phase
            prior = ""
    return [
        ((idea or "").lower(), _WEIGHT_IDEA),
        (f"{phase} {label}".lower(), _WEIGHT_PHASE),
        (prior, _WEIGHT_PRIOR),
    ]


def _hits(keyword: str, parts: list[tuple[str, float]]) -> float:
    """What one keyword is worth across the three texts, or 0 when it appears in none.

    Matched on a word boundary: `api` must not be found inside `capillary`, and a
    library whose keywords silently match substrings is one where every skill about
    `ci` reaches every build that mentions `specify`.
    """
    pattern = re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)")
    return sum(weight for text, weight in parts if text and pattern.search(text))


def score(skill: Skill, parts: list[tuple[str, float]], phase: str) -> tuple[float, tuple[str, ...]]:
    """(score, the keywords that hit) for one skill against one build."""
    if not skill.keywords:
        base = _UNKEYED_SCORE
        matched: tuple[str, ...] = ()
    else:
        scored = [(k, _hits(k, parts)) for k in skill.keywords]
        matched = tuple(k for k, hit in scored if hit)
        base = sum(hit for _, hit in scored)
    if base and phase in skill.agents:
        base += _BOUND_BONUS
    return base, matched


def select(
    phase: str,
    idea: str,
    prior_outputs: Optional[dict] = None,
    overrides: Optional[Overrides] = None,
    limit: Optional[int] = None,
    candidates: Optional[Iterable[Skill]] = None,
) -> list[Selected]:
    """The skills this phase should receive, best first.

    1. Anything switched off in the library, or excluded on this build, is dropped.
    2. Anything not bound to this phase is dropped.
    3. What is left is scored on keyword hits against the idea, the phase and the
       prior phases' outputs.
    4. Pinned skills sort first, whether they scored or not — that is what a pin is.

    How many of these actually reach the model is decided by the agent, against the
    window of the model it is about to call. This returns the order to take them in.
    """
    if not settings.skills_enabled:
        return []
    ov = overrides or Overrides()
    pool = list(candidates) if candidates is not None else registry.library()
    off = registry.disabled_names()
    parts = _parts(idea, phase, prior_outputs)

    chosen: list[Selected] = []
    for skill in pool:
        # A skill that breaks a load rule is never injected, pinned or not: the
        # rules exist because the damage they prevent — a prompt over the window, a
        # format instruction fighting the schema — lands on the build, not on the
        # library.
        if not skill.usable or skill.name in ov.excluded or not skill.serves(phase):
            continue
        pinned = skill.name in ov.pinned
        # Switched off in the library means "not by default". A pin is this build
        # asking for it by name, which is a more specific instruction than the
        # standing one — otherwise pinning a skill you had turned off would do
        # nothing at all and say nothing about why.
        if skill.name in off and not pinned:
            continue
        value, matched = score(skill, parts, phase)
        if not pinned and value <= 0:
            continue  # nothing about this build says this procedure applies
        chosen.append(Selected(skill=skill, score=value, pinned=pinned, matched=matched))

    # Pinned first, then the strongest match, then by name — so two builds of the
    # same idea select the same skills in the same order, which is what makes
    # "which skills did this phase get?" a question with one answer.
    chosen.sort(key=lambda s: (not s.pinned, -s.score, s.skill.name))
    # Zero means none. Reading it as "uncapped" would turn the most obvious way
    # to ask for no skills into the one that delivers every one of them.
    cap = settings.skills_max_per_phase if limit is None else limit
    return chosen[: max(cap, 0)]
