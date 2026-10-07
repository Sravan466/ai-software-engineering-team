from __future__ import annotations

from typing import Optional

from app.agents.base import AgentContext, BaseAgent
from app.build import buildlog, layout
from app.build import runner as build_runner
from app.build import testrun
from app.build.check import BuildCheck, phase_tree
from app.core.config import settings
from app.core.constants import BuildStatus, Phase
from app.core.logging import get_logger
from app.orchestration.claim import Superseded
from app.router.base import RequestCancelled
from app.schemas.agent_outputs import QAEngineerOutput

log = get_logger(__name__)


class QAEngineerAgent(BaseAgent):
    key = Phase.QA_ENGINEER.value
    title = "QA Engineer"
    complexity = "medium"
    role = (
        "Name each test after the criterion it checks; import real code by its real path."
    )
    depends_on = (
        Phase.PRODUCT_MANAGER.value,
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
        Phase.FRONTEND_ENGINEER.value,
    )
    output_model = QAEngineerOutput

    def task_instruction(self) -> str:
        return (
            "Test the code the Backend and Frontend phases wrote.\n"
            "- One test per P0 acceptance criterion from the Product Manager, named after it.\n"
            "- Use the registry's paths and the files in each digest; add the edge cases that matter.\n"
            "- pytest for a Python backend, the frontend's runner for the frontend; "
            "commands in `command_backend` / `command_frontend`.\n"
            "- Tests run offline; one that can't run counts against you."
        )

    def _run_tests(self, ctx: AgentContext, output: dict, build: Optional[BuildCheck]) -> Optional[dict]:
        """Install each side and run the tests written for it, once they parse (#76).

        A suite that can't be collected — a syntax error the parser missed, an import
        that doesn't resolve at runtime, a runner the side doesn't have — joins `build`
        as QA's own problems, exactly as a compile error does: QA is sent back with them.
        Tests that run and fail are recorded for the fix loop, which sends them to
        whoever owns the code first. Nothing to run with is `not_run`, with the reason.
        """
        if build is None or not settings.enforce_build_check:
            return None
        if build.status == BuildStatus.FAILED.value:
            return testrun.combine([], "The tests don't compile yet, so they weren't run.")
        from app.orchestration import activity

        try:
            files, mine = phase_tree(ctx.prior_outputs, self.key, output, ctx.charter)
            sides = sorted(
                {
                    side
                    for p in mine
                    if (side := layout.side_of(p)) and testrun.is_test(layout.relative_to_side(p))
                }
            )
            if not sides:
                return testrun.combine([], "QA wrote no test files.", no_tests=True)
            runs = []
            activity.begin(self.key)
            try:
                for side in sides:
                    runs.append(build_runner.run_tests(files, side))
            finally:
                activity.end()
        except (RequestCancelled, Superseded):
            raise
        except Exception as e:  # noqa: BLE001 - the runner must not become the failure
            log.warning("%s: the tests couldn't run: %s", self.title, e)
            return testrun.combine([testrun.TestRun.not_run("tests", f"The tests couldn't run: {e}")])
        problems = [p for r in runs for p in r.problems]
        if problems:
            build.problems = buildlog.capped(build.problems + problems)
            build.status = BuildStatus.FAILED.value
        return testrun.combine(runs)
