from __future__ import annotations

from core.state_machine import EngineState


class Dispatcher:
    """Very small role selector for the first scaffold."""

    def next_role(self, state: EngineState) -> str | None:
        mapping = {
            EngineState.IDLE: "planner",
            EngineState.PLANNING: "builder",
            EngineState.BUILDING: "verifier_functional",
            EngineState.VERIFYING_FUNCTIONAL: "verifier_human",
            EngineState.VERIFYING_HUMAN: "orchestrator",
            EngineState.ITERATING: "planner",
            EngineState.COMPLETED: "planner",
        }
        return mapping.get(state)
