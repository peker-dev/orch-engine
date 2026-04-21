"""Smoke tests for the ITERATING / max_cycles / stagnation policy glue.

This covers the pure-function escalation logic that wraps
`_orchestrator_decision` inside `core/app.py`, without starting a real
cycle. Each scenario asserts that `_apply_iteration_policy` converts a
`needs_iteration` base decision into the correct terminal state when the
configured policy limits are reached.

Run:
    python -m tools.iteration_policy_smoke
"""

from __future__ import annotations

import sys
from typing import Any

# Force UTF-8 stdout so non-ASCII characters in decision.reason (e.g., em dash
# inserted by _orchestrator_decision) do not break the Windows cp949 console.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass

from core.app import (
    _apply_iteration_policy,
    _is_stagnating,
    _resolve_iteration_limits,
)
from core.state_machine import EngineState


def _fake_reviews(functional_score: float = 0.4, human_score: float = 0.4) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {"result": "needs_iteration", "score": functional_score},
        {"result": "needs_iteration", "score": human_score},
    )


def _needs_iteration_decision() -> dict[str, Any]:
    return {
        "decision": "needs_iteration",
        "next_state": EngineState.ITERATING.value,
        "reason": "reviews below pass threshold — re-plan next cycle",
    }


def _complete_decision() -> dict[str, Any]:
    return {
        "decision": "complete_cycle",
        "next_state": EngineState.COMPLETED.value,
        "reason": "both reviews passed scoring thresholds",
    }


def run_case(name: str, predicate: bool, detail: str = "") -> bool:
    status = "OK" if predicate else "FAIL"
    message = f"[{status}] {name}"
    if detail:
        message += f" -- {detail}"
    print(message)
    return predicate


def main() -> int:
    results: list[bool] = []

    # ------------------------------------------------------------------
    # _resolve_iteration_limits
    # ------------------------------------------------------------------
    limits = _resolve_iteration_limits(
        {"max_cycles": 4, "stop_on_stagnation": False},
        {"limits": {"cycle_limits": {"max_cycles": 9}, "auto_stop_rules": {"stop_on_stagnation": True}}},
    )
    results.append(
        run_case(
            "project limits.yaml wins over common defaults",
            limits == {"max_cycles": 4, "stop_on_stagnation": False},
            f"got {limits}",
        )
    )

    limits = _resolve_iteration_limits(
        {},
        {"limits": {"cycle_limits": {"max_cycles": 9}, "auto_stop_rules": {"stop_on_stagnation": True}}},
    )
    results.append(
        run_case(
            "falls back to common defaults when project limits empty",
            limits == {"max_cycles": 9, "stop_on_stagnation": True},
            f"got {limits}",
        )
    )

    limits = _resolve_iteration_limits({}, {})
    results.append(
        run_case(
            "falls back to hard-coded safe defaults",
            limits == {"max_cycles": 6, "stop_on_stagnation": True},
            f"got {limits}",
        )
    )

    # ------------------------------------------------------------------
    # complete_cycle must not be escalated
    # ------------------------------------------------------------------
    functional, human = _fake_reviews(0.9, 0.9)
    passthrough = _apply_iteration_policy(
        _complete_decision(),
        cycle_index=10,
        score_history=[],
        limits={"max_cycles": 2, "stop_on_stagnation": True},
        functional_review=functional,
        human_review=human,
    )
    results.append(
        run_case(
            "complete_cycle decisions bypass policy even at cycle>max",
            passthrough["decision"] == "complete_cycle",
            f"got decision={passthrough['decision']}",
        )
    )

    # ------------------------------------------------------------------
    # max_cycles trip
    # ------------------------------------------------------------------
    functional, human = _fake_reviews(0.4, 0.4)
    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=6,
        score_history=[],
        limits={"max_cycles": 6, "stop_on_stagnation": False},
        functional_review=functional,
        human_review=human,
    )
    results.append(
        run_case(
            "max_cycles escalates needs_iteration to blocked",
            decision["decision"] == "max_cycles_reached"
            and decision["next_state"] == EngineState.BLOCKED.value,
            f"got {decision}",
        )
    )

    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=3,
        score_history=[],
        limits={"max_cycles": 6, "stop_on_stagnation": False},
        functional_review=functional,
        human_review=human,
    )
    results.append(
        run_case(
            "below max_cycles with no history stays as needs_iteration",
            decision["decision"] == "needs_iteration",
            f"got {decision}",
        )
    )

    # ------------------------------------------------------------------
    # stagnation detection (2026-04-21 relaxed: 3 consecutive regressions)
    # ------------------------------------------------------------------
    history_one_iteration = [
        {
            "cycle": 1,
            "decision": "needs_iteration",
            "functional_score": 0.4,
            "human_score": 0.4,
        }
    ]
    functional, human = _fake_reviews(0.4, 0.4)
    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=2,
        score_history=history_one_iteration,
        limits={"max_cycles": 6, "stop_on_stagnation": True},
        functional_review=functional,
        human_review=human,
    )
    results.append(
        run_case(
            "single flat cycle does NOT trip stagnation (2026-04-21 relaxed)",
            decision["decision"] == "needs_iteration",
            f"got {decision}",
        )
    )

    functional, human = _fake_reviews(0.55, 0.4)
    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=2,
        score_history=history_one_iteration,
        limits={"max_cycles": 6, "stop_on_stagnation": True},
        functional_review=functional,
        human_review=human,
    )
    results.append(
        run_case(
            "functional score improving disables stagnation stop",
            decision["decision"] == "needs_iteration",
            f"got {decision}",
        )
    )

    functional, human = _fake_reviews(0.4, 0.4)
    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=2,
        score_history=history_one_iteration,
        limits={"max_cycles": 6, "stop_on_stagnation": False},
        functional_review=functional,
        human_review=human,
    )
    results.append(
        run_case(
            "stop_on_stagnation=false does not trip the stagnation stop",
            decision["decision"] == "needs_iteration",
            f"got {decision}",
        )
    )

    # Three consecutive regressions (f and h both non-improving 3 transitions
    # in a row) — the new threshold that should still trip.
    history_three_regressions = [
        {"cycle": 1, "decision": "needs_iteration", "functional_score": 1.0, "human_score": 1.0},
        {"cycle": 2, "decision": "needs_iteration", "functional_score": 0.8, "human_score": 0.8},
        {"cycle": 3, "decision": "needs_iteration", "functional_score": 0.5, "human_score": 0.5},
    ]
    functional, human = _fake_reviews(0.3, 0.3)
    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=4,
        score_history=history_three_regressions,
        limits={"max_cycles": 6, "stop_on_stagnation": True},
        functional_review=functional,
        human_review=human,
    )
    results.append(
        run_case(
            "three consecutive regressions trip stagnation",
            decision["decision"] == "stagnation_detected"
            and decision["next_state"] == EngineState.BLOCKED.value,
            f"got {decision}",
        )
    )

    # Recovery in the middle breaks the streak — even if the very last cycle
    # regresses, earlier upward move resets the counter.
    history_with_recovery = [
        {"cycle": 1, "decision": "needs_iteration", "functional_score": 1.0, "human_score": 1.0},
        {"cycle": 2, "decision": "needs_iteration", "functional_score": 0.4, "human_score": 0.4},
        {"cycle": 3, "decision": "needs_iteration", "functional_score": 0.7, "human_score": 0.7},
        {"cycle": 4, "decision": "needs_iteration", "functional_score": 0.5, "human_score": 0.5},
    ]
    functional, human = _fake_reviews(0.4, 0.4)
    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=5,
        score_history=history_with_recovery,
        limits={"max_cycles": 6, "stop_on_stagnation": True},
        functional_review=functional,
        human_review=human,
    )
    results.append(
        run_case(
            "recovery cycle in the middle breaks regression streak",
            decision["decision"] == "needs_iteration",
            f"got {decision}",
        )
    )

    # Direct _is_stagnating unit — cycle 1 ~ 4 mirroring the 15차 live run
    # trajectory (1.0/0.84 → 0.96/0.95 → 0.28/0.32 → 0.86/0.78). Under the
    # new rule this must NOT be flagged stagnating at cycle 3 (only one
    # transition is a double-regression), so the backstop would have left
    # the door open for cycle 4's recovery.
    live15_history_through_cycle_2 = [
        {"cycle": 1, "decision": "needs_iteration", "functional_score": 1.0, "human_score": 0.84},
        {"cycle": 2, "decision": "needs_iteration", "functional_score": 0.96, "human_score": 0.95},
    ]
    results.append(
        run_case(
            "15차 live run cycle 3 (0.28/0.32) no longer trips stagnation",
            not _is_stagnating(
                score_history=live15_history_through_cycle_2,
                current_functional=0.28,
                current_human=0.32,
            ),
            "expected False",
        )
    )

    # ------------------------------------------------------------------
    # handoff_pause_count offsets cycle_index when comparing against max_cycles
    # ------------------------------------------------------------------
    functional, human = _fake_reviews(0.4, 0.4)
    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=6,
        score_history=[],
        limits={"max_cycles": 6, "stop_on_stagnation": False},
        functional_review=functional,
        human_review=human,
        handoff_pause_count=3,
    )
    results.append(
        run_case(
            "handoff pauses subtract from cycle_index: 6-3 < 6 stays needs_iteration",
            decision["decision"] == "needs_iteration",
            f"got {decision}",
        )
    )

    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=6,
        score_history=[],
        limits={"max_cycles": 2, "stop_on_stagnation": False},
        functional_review=functional,
        human_review=human,
        handoff_pause_count=6,
    )
    results.append(
        run_case(
            "effective cycle 0 (all handoff pauses) never trips max_cycles",
            decision["decision"] == "needs_iteration",
            f"got {decision}",
        )
    )

    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=3,
        score_history=[],
        limits={"max_cycles": 2, "stop_on_stagnation": False},
        functional_review=functional,
        human_review=human,
        handoff_pause_count=0,
    )
    results.append(
        run_case(
            "no handoff pauses keeps legacy behaviour: 3 >= 2 -> max_cycles_reached",
            decision["decision"] == "max_cycles_reached",
            f"got {decision}",
        )
    )

    decision = _apply_iteration_policy(
        _needs_iteration_decision(),
        cycle_index=5,
        score_history=[],
        limits={"max_cycles": 3, "stop_on_stagnation": False},
        functional_review=functional,
        human_review=human,
        handoff_pause_count=1,
    )
    results.append(
        run_case(
            "effective cycle 4 > max 3 -> max_cycles_reached with exclusion note",
            decision["decision"] == "max_cycles_reached"
            and "handoff 일시정지 1회 제외" in str(decision.get("reason", "")),
            f"got {decision}",
        )
    )

    # ------------------------------------------------------------------
    # _is_stagnating sanity
    # ------------------------------------------------------------------
    results.append(
        run_case(
            "empty history is never stagnating",
            _is_stagnating(score_history=[], current_functional=0.1, current_human=0.1) is False,
        )
    )

    results.append(
        run_case(
            "non-needs_iteration history entries are ignored",
            _is_stagnating(
                score_history=[
                    {"decision": "complete_cycle", "functional_score": 0.9, "human_score": 0.9},
                ],
                current_functional=0.1,
                current_human=0.1,
            )
            is False,
        )
    )

    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{passed}/{total} iteration policy cases passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
