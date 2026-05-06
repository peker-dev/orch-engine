"""Adapter 인터페이스.

역할별 응답 구조 (MVP 약속):

- planner    : {"title": str, "description": str}
- builder    : {"summary": str, "artifacts": [{"name": str, "summary": str}, ...]}
- verifier   : {"verdict": "pass"|"needs_iteration"|"blocked",
                "summary": str, "issues": [str], "improvements": [str]}
- orchestrator: {"decision": "complete"|"needs_iteration"|"blocked",
                 "reason": str}

context 구조 (loop.py 가 채워서 넘김):

- goal: str
- cycle: int (1부터 시작)
- previous_review: dict | None  (cycle >= 2 일 때 직전 cycle 의 verifier 결과)
- current_task: dict | None     (verifier/orchestrator 입력)
- current_build: dict | None    (verifier/orchestrator 입력)
- current_review: dict | None   (orchestrator 입력)
"""
from __future__ import annotations

from typing import Any, Protocol


Role = str  # "planner" | "builder" | "verifier" | "orchestrator"


class Adapter(Protocol):
    name: str

    def invoke(self, role: Role, context: dict[str, Any]) -> dict[str, Any]:
        ...
