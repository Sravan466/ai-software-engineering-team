from __future__ import annotations

from app.agents.code_phase import CodePhaseAgent
from app.core.constants import Phase
from app.schemas.agent_outputs import BackendEngineerOutput, BackendPlan


class BackendEngineerAgent(CodePhaseAgent):
    key = Phase.BACKEND_ENGINEER.value
    title = "Backend Engineer"
    complexity = "high"
    role = (
        "Prefer the scaffold's conventions over your own: a smaller backend that runs beats "
        "a larger one that does not."
    )
    depends_on = (Phase.PRODUCT_MANAGER.value, Phase.SYSTEM_DESIGN.value)
    output_model = BackendEngineerOutput
    plan_model = BackendPlan

    def task_instruction(self) -> str:
        return (
            "Implement the backend for the P0 stories only.\n"
            "- Serve exactly the endpoints in the name registry, at those paths, using its "
            "entity names for models and tables. Add no endpoint the frontend has no use for.\n"
            "- Enforce each endpoint's `auth` from System Design, and describe the flow in "
            "`auth_flow`.\n"
            "- Include seed or demo data so the app shows something on first run.\n"
            "- Put each source file in `files` with complete, runnable code; give each a "
            "one-line `purpose`.\n"
            "Use the stack the charter fixed; default to FastAPI + SQLAlchemy if it fixed none."
        )

    def plan_instruction(self) -> str:
        return (
            "Plan the backend for the P0 stories only: its files, not their code — each file "
            "is written in its own call next.\n"
            "- `files`: every source file, a one-line `purpose`, the names other files import "
            "from it (`exports`), and the plan paths it imports (`imports`). Small files, one "
            "job each.\n"
            "- Serve exactly the registry's endpoints with its entity names; describe the flow "
            "in `auth_flow`. Plan seed data.\n"
            "Use the stack the charter fixed; default to FastAPI + SQLAlchemy if it fixed none."
        )

    def write_instruction(self) -> str:
        return (
            "Serve exactly the registry's endpoints at those paths, enforcing each one's auth. "
            "Import only files in the plan and packages the platform installs."
        )
