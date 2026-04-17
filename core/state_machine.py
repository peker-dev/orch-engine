from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EngineState(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    BUILDING = "building"
    VERIFYING_FUNCTIONAL = "verifying_functional"
    VERIFYING_HUMAN = "verifying_human"
    ITERATING = "iterating"
    HANDOFF_ACTIVE = "handoff_active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"


RESUMABLE_STATES = frozenset(
    {
        EngineState.IDLE.value,
        EngineState.ITERATING.value,
        EngineState.COMPLETED.value,
    }
)

HALTED_STATES = frozenset(
    {
        EngineState.HANDOFF_ACTIVE.value,
        EngineState.PAUSED.value,
        EngineState.BLOCKED.value,
    }
)


@dataclass(slots=True)
class CycleDecision:
    next_state: EngineState
    reason: str


def default_start_state() -> EngineState:
    return EngineState.IDLE
