"""The scoping pass over a change request (#79): who changes what.

One small call, before any engineer is asked anything. It reads the request against the
app as it is — the idea, the stack, every file with what it is for, what earlier changes
settled — and names the engineers whose files change, the files likely to, and whether
the architecture or the database schema has to change first. Routed under its own role
(`planner`), so a person can point it at a small model; by default it runs wherever the
router sends low-complexity work. Never by model name.

Not part of the pipeline: it decides which of the pipeline's phases run, so it has no
phase of its own, no hand-offs, no charter checks on what it says.
"""
from __future__ import annotations

from typing import Optional

from app.agents.base import AgentContext, BaseAgent, Prompt, _clip
from app.router.model_profile import ModelProfile
from app.router.router import router
from app.schemas.agent_outputs import ChangePlan
from app.schemas.llm import ChatMessage

#: The role the router and Settings know this call by.
ROLE = "planner"

_TASK = (
    "Plan this change to the app above. Name who edits their files — backend_engineer for "
    "the server and its API, frontend_engineer for pages and components, devops_engineer "
    "only for deployment files — and list the files likely to change or be added, by their "
    "paths. Set needs_design when the architecture itself must change (a new service, a "
    "different database), and needs_db_change when the database schema must (a new table "
    "or column). Keep it as small as the request allows; a one-line summary a person reads."
)


class ChangePlannerAgent(BaseAgent):
    key = ROLE
    title = "Change Planner"
    complexity = "low"
    role = "Read a change request against an app's files and say which engineers change which files."
    output_model = ChangePlan

    def task_instruction(self) -> str:
        return _TASK

    def _build_messages(self, ctx: AgentContext, profile: Optional[ModelProfile] = None, reserve: int = 0) -> Prompt:
        """The app, its files, and the request — the file list cut first when it must be.

        `ctx.change` is the request, `ctx.extra_context` the app's file list and what it
        has settled on (the runner writes both). Nothing else of the pipeline's context
        applies: there are no hand-offs to this call and no skills for it.
        """
        if profile is None:
            profile = router.profile_for(
                ctx.routing_mode, ctx.preferred_model, complexity=self._complexity(ctx), role=self.key, pin=ctx.pin_model
            )
        system = (
            f"You are the {self.title} on an AI software engineering team. {self.role}\n\n"
            + (f"{ctx.charter.prompt_block()}\n\n" if ctx.charter else "")
            + "Respond with ONLY a single valid JSON object — no prose, no markdown fences — "
            f"matching this shape:\n{self.output_spec}"
        )
        head = f"# The app\n{_clip(ctx.idea, min(len(ctx.idea), 600))}\n"
        ask = f"# The change\n{_clip(ctx.change, min(len(ctx.change), 2400))}\n\n{self.task_text()}"
        room = max(profile.prompt_char_budget - len(system) - len(head) - len(ask) - max(reserve, 0) - 40, 0)
        files = f"# What it is made of\n{_clip(ctx.extra_context, room)}\n" if ctx.extra_context else ""
        return Prompt(
            messages=[
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content="\n".join(p for p in (head, files, ask) if p)),
            ]
        )


planner = ChangePlannerAgent()
