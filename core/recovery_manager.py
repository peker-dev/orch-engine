from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RecoveryAction:
    action: str
    reason: str


class RecoveryManager:
    def decide(self, failure_class: str) -> RecoveryAction:
        if failure_class in {"timeout", "transient_process_failure"}:
            return RecoveryAction("retry", "Transient failure detected")
        if failure_class in {"missing_tool", "environment_error"}:
            return RecoveryAction("fallback", "Switch to fallback route")
        return RecoveryAction("escalate", "Manual review required")
