"""Agent registry — maps phase keys to their agent instances."""
from __future__ import annotations

from app.agents.backend_engineer import BackendEngineerAgent
from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.cost_estimation import CostEstimationAgent
from app.agents.devops_engineer import DevOpsEngineerAgent
from app.agents.frontend_engineer import FrontendEngineerAgent
from app.agents.product_manager import ProductManagerAgent
from app.agents.qa_engineer import QAEngineerAgent
from app.agents.security_engineer import SecurityEngineerAgent
from app.agents.system_design import SystemDesignAgent

AGENTS: dict[str, BaseAgent] = {
    a.key: a
    for a in (
        ProductManagerAgent(),
        SystemDesignAgent(),
        BackendEngineerAgent(),
        FrontendEngineerAgent(),
        QAEngineerAgent(),
        SecurityEngineerAgent(),
        DevOpsEngineerAgent(),
        CostEstimationAgent(),
    )
}


def get_agent(phase_key: str) -> BaseAgent:
    if phase_key not in AGENTS:
        raise KeyError(f"No agent registered for phase '{phase_key}'")
    return AGENTS[phase_key]


__all__ = ["AGENTS", "get_agent", "AgentContext", "AgentResult", "BaseAgent"]
