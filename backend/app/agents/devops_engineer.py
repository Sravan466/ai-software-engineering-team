from __future__ import annotations

from typing import Optional

from app.agents.base import AgentContext, BaseAgent
from app.core.constants import Phase
from app.core.logging import get_logger
from app.schemas.agent_outputs import DevOpsEngineerOutput

log = get_logger(__name__)

_WORKFLOWS = ".github/workflows/"


def github_connected() -> bool:
    """Whether the build's account has GitHub connected — the only time a CI workflow
    has anywhere to run. Best effort: a store that can't be read reads as not connected."""
    from app.core import deploy_store, identity

    uid = identity.current_user_id()
    if not uid:
        return False
    try:
        return bool(deploy_store.for_user(uid).entry(deploy_store.GITHUB).get("token"))
    except Exception as e:  # noqa: BLE001 - never fails a phase
        log.warning("Couldn't read whether GitHub is connected (assuming not): %s", e)
        return False


def workflow_problems(ci: Optional[dict]) -> list[str]:
    """What is wrong with a CI workflow file, or [] for none/fine."""
    if not isinstance(ci, dict):
        return []
    path = str(ci.get("path") or "").strip().lstrip("/")
    problems = []
    if not path.startswith(_WORKFLOWS) or not path.endswith((".yml", ".yaml")):
        problems.append(f"`ci_workflow.path` — must be a file under {_WORKFLOWS} ending .yml")
    try:
        import yaml

        parsed = yaml.safe_load(ci.get("content") or "")
    except Exception as e:  # noqa: BLE001 - any parse error is the same answer
        problems.append(f"`ci_workflow.content` — is not valid YAML: {str(e).splitlines()[0]}")
        return problems
    if not isinstance(parsed, dict) or "jobs" not in parsed:
        problems.append("`ci_workflow.content` — a GitHub Actions workflow needs a `jobs` map")
    return problems


class DevOpsEngineerAgent(BaseAgent):
    key = Phase.DEVOPS_ENGINEER.value
    title = "DevOps Engineer"
    complexity = "medium"
    role = (
        "The platform deploys this build itself; what helps is the short list a person "
        "needs to run it and recover it, taken from the code."
    )
    depends_on = (
        Phase.SYSTEM_DESIGN.value,
        Phase.BACKEND_ENGINEER.value,
        Phase.FRONTEND_ENGINEER.value,
        Phase.QA_ENGINEER.value,
    )
    output_model = DevOpsEngineerOutput

    def standing_brief(self, charter=None) -> str:
        if github_connected():
            return (
                "GITHUB IS CONNECTED — write `ci_workflow`: one GitHub Actions workflow at "
                ".github/workflows/ci.yml that installs each side and runs QA's "
                "`command_backend` and `command_frontend` exactly as given. It must parse as YAML."
            )
        return "GITHUB IS NOT CONNECTED — set `ci_workflow` to null; there is nowhere for it to run."

    def own_checks(self, output: dict, ctx: AgentContext) -> tuple[dict, list[str]]:
        if output.get("ci_workflow") and not github_connected():
            # Dropped rather than sent back: it is not wrong, only unwanted, and a
            # workflow that runs in nobody's repository is never shipped.
            output = {**output, "ci_workflow": None}
        return output, workflow_problems(output.get("ci_workflow"))

    def task_instruction(self) -> str:
        return (
            "Write the deployment notes for this build, from the code in your hand-off.\n"
            "- `env_vars`: every environment variable the Backend digest lists (and any the "
            "frontend reads), each with what it is for.\n"
            "- `health_check_path`: an endpoint the backend serves that answers when it is "
            "up, or empty if none does.\n"
            "- `migration_command`: the command that applies the database schema, or empty.\n"
            "- `rollback`: one paragraph on undoing a bad deploy on Vercel and Render.\n"
            "Example env var: {\"name\": \"DATABASE_URL\", \"purpose\": \"Postgres connection "
            "string the API reads at startup\"}."
        )
