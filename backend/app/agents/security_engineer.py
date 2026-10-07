from __future__ import annotations

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.build import scan
from app.core.constants import Phase
from app.schemas.agent_outputs import SecurityEngineerOutput


class SecurityEngineerAgent(BaseAgent):
    key = Phase.SECURITY_ENGINEER.value
    title = "Security Engineer"
    complexity = "high"
    role = (
        "Report only what you can point at: a file and line you were shown, or an endpoint "
        "in the design. A finding in code that is not in your hand-off is a guess."
    )
    depends_on = (
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
        Phase.FRONTEND_ENGINEER.value,
    )
    output_model = SecurityEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Scanners already checked this code; `# Scanner findings` lists what they "
            "reported. Don't repeat those. Review what scanners can't see: each endpoint's "
            "`auth` against what its handler enforces (ownership checks), business-logic "
            "abuse, and data an API response exposes. Give each finding its `path` and "
            "`line`, a severity and a concrete fix. Where the code you were shown was cut, "
            "say so rather than assume. Give an overall risk assessment."
        )

    def run(self, ctx: AgentContext) -> AgentResult:
        """Scan the build, then review what the scanners can't see (#77).

        The scanners run first, in the sandbox, so Warden is handed what they found —
        capped, as known findings not to repeat — and asked only for what a rule can't
        express: authorisation, business logic, data exposure. Their run is kept with
        the phase: what they report is what the fix loop acts on, and what a rescan has
        to stop reporting before anything is called fixed.
        """
        from app.orchestration import activity

        board = activity.begin(self.key)
        try:
            result = scan.scan_build(ctx.prior_outputs, ctx.charter)
            ctx.scan_context = scan.prompt_block(result)
            activity.stage("reviewing")
            out = super().run(ctx)
        except BaseException:
            activity.end(board, finished=False)
            raise
        activity.end(board)
        out.scan = result.as_dict()
        return out
