"""A model-agnostic library of procedural knowledge the agents are given.

RAG retrieves *facts about this project*. A skill carries *procedure that is true
across projects* — how to write an acceptance criterion that can be tested, which
OWASP categories matter for a form that takes card details — and it reaches the
agent by being in its prompt, which is the one delivery mechanism that behaves
identically on a 7B local model and on Claude, GPT or Gemini.

    loader   — one `SKILL.md`: frontmatter, body, and the rules checked as it loads
    registry — the library on disk, what is switched off, and how to change it
    selection — which skills a phase gets, scored without a model call

The packing into the prompt is *not* here. It lives in `app/agents/base.py`, where
the budget does, because only the agent knows which model it is about to call.
"""
from __future__ import annotations

from app.skills.loader import Skill, render
from app.skills.registry import SkillError, library
from app.skills.selection import Overrides, Selected, select

__all__ = [
    "Overrides",
    "Selected",
    "Skill",
    "SkillError",
    "library",
    "render",
    "select",
]
