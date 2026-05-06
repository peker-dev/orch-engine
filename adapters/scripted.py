"""ScriptedAdapter.

미리 짜둔 응답을 cycle 순서대로 돌려주는 가짜 어댑터. 실제 LLM 연결 전에
2 사이클 자동 개선 루프 자체가 동작하는지 검증하기 위한 도구다.

사용 패턴:

    adapter = ScriptedAdapter()  # 기본 시나리오
    adapter = ScriptedAdapter(scripts={"planner": [r1, r2], ...})

invoke 는 호출 순서대로 다음 응답을 반환한다. 응답이 모자라면 마지막 응답을 재사용한다.
호출 이력은 `adapter.calls` 에 기록되어 smoke test 가 검증할 수 있다.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def _default_scripts() -> dict[str, list[dict[str, Any]]]:
    return {
        "planner": [
            {
                "title": "Initial draft",
                "description": "Produce a first attempt that addresses the goal.",
            },
            {
                "title": "Revision",
                "description": "Apply verifier improvements to the previous draft.",
            },
        ],
        "builder": [
            {
                "summary": "Wrote initial draft v1.",
                "artifacts": [{"name": "draft.txt", "summary": "v1 placeholder"}],
            },
            {
                "summary": "Revised draft based on verifier feedback.",
                "artifacts": [{"name": "draft.txt", "summary": "v2 with improvements"}],
            },
        ],
        "verifier": [
            {
                "verdict": "needs_iteration",
                "summary": "First draft is too thin.",
                "issues": ["lacks detail"],
                "improvements": ["expand each section with one concrete example"],
            },
            {
                "verdict": "pass",
                "summary": "Revision addresses the prior feedback.",
                "issues": [],
                "improvements": [],
            },
        ],
        "orchestrator": [
            {"decision": "needs_iteration", "reason": "verifier requested improvements"},
            {"decision": "complete", "reason": "verifier passed"},
        ],
    }


class ScriptedAdapter:
    name = "scripted"

    def __init__(self, scripts: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.scripts: dict[str, list[dict[str, Any]]] = scripts or _default_scripts()
        self._cursor: dict[str, int] = {role: 0 for role in self.scripts}
        self.calls: list[dict[str, Any]] = []

    def invoke(self, role: str, context: dict[str, Any]) -> dict[str, Any]:
        if role not in self.scripts:
            raise KeyError(f"ScriptedAdapter has no script for role {role!r}")
        idx = self._cursor[role]
        responses = self.scripts[role]
        if idx >= len(responses):
            idx = len(responses) - 1
        else:
            self._cursor[role] += 1
        response = deepcopy(responses[idx])
        self.calls.append(
            {
                "role": role,
                "cycle": context.get("cycle"),
                "context": deepcopy(context),
                "response": deepcopy(response),
            }
        )
        return response
