"""역할 실행 루프.

한 cycle 안에서 Planner → Builder → Verifier → Orchestrator 를 순차 실행하고,
Orchestrator 결정에 따라 다음 cycle 진행 여부를 결정한다.

핵심 보장: cycle N 의 Planner 가 받는 context 에는 cycle N-1 의 Verifier 리뷰가
`previous_review` 로 포함된다. 이게 MVP smoke 의 검증 포인트다.
"""
from __future__ import annotations

from typing import Any, Union

from adapters.base import Adapter

from . import store


ROLE_ORDER: tuple[str, ...] = ("planner", "builder", "verifier", "orchestrator")
TERMINAL_DECISIONS = {"complete", "blocked"}

AdapterArg = Union[Adapter, dict]


def _resolve(adapter: AdapterArg, role: str) -> Adapter:
    if isinstance(adapter, dict):
        if role not in adapter:
            raise KeyError(f"no adapter mapped for role {role!r}")
        return adapter[role]
    return adapter


def _previous_review(target: str) -> dict[str, Any] | None:
    """cycle >= 2 에서만 의미 있는 직전 verifier 리뷰. cycle 1 에서는 None."""
    review = store.load_json(store.paths(target)["latest_review"])
    if not review.get("verdict"):
        return None
    return review


def run_cycle(target: str, adapter: AdapterArg) -> dict[str, Any]:
    p = store.paths(target)
    session = store.load_json(p["session"])
    cycle = session.get("cycle", 0) + 1
    project = store.load_json(p["project"])
    goal = project.get("goal", "")

    base_context: dict[str, Any] = {
        "goal": goal,
        "cycle": cycle,
        "previous_review": _previous_review(target),
    }

    # Planner ------------------------------------------------------
    plan = _resolve(adapter, "planner").invoke("planner", dict(base_context))
    task = {
        "schema_version": 1,
        "cycle": cycle,
        "title": plan.get("title"),
        "description": plan.get("description"),
        "created_at": store.now_iso(),
    }
    store.save_json(p["current_task"], task)
    store.append_event(target, {"role": "planner", "cycle": cycle, "response": plan})

    # Builder ------------------------------------------------------
    build_ctx = dict(base_context, current_task=task)
    build = _resolve(adapter, "builder").invoke("builder", build_ctx)
    index = store.load_json(p["artifacts_index"])
    for art in build.get("artifacts", []):
        index["items"].append({"cycle": cycle, **art})
    store.save_json(p["artifacts_index"], index)
    store.append_event(target, {"role": "builder", "cycle": cycle, "response": build})

    # Verifier -----------------------------------------------------
    verify_ctx = dict(base_context, current_task=task, current_build=build)
    review = _resolve(adapter, "verifier").invoke("verifier", verify_ctx)
    review_record = {
        "schema_version": 1,
        "cycle": cycle,
        "verdict": review.get("verdict"),
        "summary": review.get("summary"),
        "issues": review.get("issues", []),
        "improvements": review.get("improvements", []),
        "created_at": store.now_iso(),
    }
    store.save_json(p["latest_review"], review_record)
    store.append_event(target, {"role": "verifier", "cycle": cycle, "response": review})

    # Orchestrator -------------------------------------------------
    orch_ctx = dict(
        base_context,
        current_task=task,
        current_build=build,
        current_review=review_record,
    )
    decision = _resolve(adapter, "orchestrator").invoke("orchestrator", orch_ctx)
    store.append_event(target, {"role": "orchestrator", "cycle": cycle, "response": decision})

    store.update_session(
        target,
        cycle=cycle,
        state="running",
        last_decision=decision.get("decision"),
        last_role="orchestrator",
    )

    return {"cycle": cycle, "decision": decision, "review": review_record}


def run(target: str, adapter: AdapterArg, max_cycles: int = 2) -> dict[str, Any]:
    """`max_cycles` 만큼 사이클을 반복하며 자동 개선 루프를 돌린다.

    종료 사유: stopped / complete / blocked / max_cycles
    """
    last: dict[str, Any] = {}
    for _ in range(max_cycles):
        if store.stop_requested(target):
            store.update_session(target, state="stopped", last_decision="stopped")
            store.append_event(target, {"role": "engine", "event": "stop_detected"})
            return {"reason": "stopped", "last": last}

        result = run_cycle(target, adapter)
        last = result
        decision_kind = (result["decision"] or {}).get("decision")
        if decision_kind in TERMINAL_DECISIONS:
            final_state = "completed" if decision_kind == "complete" else "blocked"
            store.update_session(target, state=final_state)
            return {"reason": decision_kind, "last": last}

    store.update_session(target, state="paused")
    return {"reason": "max_cycles", "last": last}
