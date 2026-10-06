from __future__ import annotations

import re

from app.agents.base import AgentContext
from app.agents.code_phase import CodePhaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import FrontendEngineerOutput, FrontendPlan


class FrontendEngineerAgent(CodePhaseAgent):
    key = Phase.FRONTEND_ENGINEER.value
    title = "Frontend Engineer"
    complexity = "high"
    role = (
        "A page that calls the real backend and handles its empty and error states beats a "
        "polished page wired to nothing."
    )
    depends_on = (
        Phase.PRODUCT_MANAGER.value,
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
    )
    output_model = FrontendEngineerOutput
    plan_model = FrontendPlan

    def task_instruction(self) -> str:
        return (
            "Build the frontend for the P0 stories in the Product Manager's hand-off.\n"
            "- Create the pages in the registry's page list, at those routes.\n"
            "- Call only the endpoints in the Backend's `endpoints_implemented`, at exactly "
            "those paths — a call to a path the backend does not serve is sent back.\n"
            "- Show a loading, an empty and an error state for every list.\n"
            "- Put each source file in `files` with complete code.\n"
            "Use the stack the charter fixed; default to Next.js + React + Tailwind if it "
            "fixed none."
        )

    def plan_instruction(self) -> str:
        return (
            "Plan the frontend for the P0 stories: its files, not their code — each file is "
            "written in its own call next.\n"
            "- `pages`: the registry's page list, at those routes. `files`: every source file, "
            "a one-line `purpose`, the names other files import from it (`exports`), and the "
            "plan paths it imports (`imports`). One component per file.\n"
            "Use the stack the charter fixed; default to Next.js + React + Tailwind if it "
            "fixed none."
        )

    def write_instruction(self) -> str:
        return (
            "Call only the Backend digest's endpoints_implemented, at exactly those paths. "
            "Show loading, empty and error states for every list. Import every component you use."
        )

    def plan_problems(self, output: dict, ctx: AgentContext) -> list[str]:
        problems = super().plan_problems(output, ctx)
        # Every page the registry fixed has a place in the plan, whatever the router's
        # spelling of a parameter (`[id]`, `:id`, `{id}`).
        wanted = self._registry(ctx).pages
        planned = {_route(p.get("route")) for p in output.get("pages") or [] if isinstance(p, dict)}
        missing = [page for page in wanted if _route(page) not in planned]
        if missing:
            problems.append(f"`pages` — missing the registry's {', '.join(missing[:6])}")
        return problems


def _route(route: object) -> str:
    text = str(route or "").strip().rstrip("/") or "/"
    return re.sub(r"\[\.{0,3}[^\]]+\]|:[A-Za-z_]\w*|\{[^}]+\}", ":p", text).lower()
