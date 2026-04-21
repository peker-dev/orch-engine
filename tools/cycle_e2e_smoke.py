"""End-to-end smoke tests for run_cycle iteration policy + handoff mode.

Previous coverage:
- `tools/iteration_policy_smoke.py` exercises the pure functions
  (`_apply_iteration_policy`, `_is_stagnating`, `_resolve_iteration_limits`).
- `tools/launcher_smoke.py` exercises the launcher wizard and CLI
  passthrough.

This module fills the gap between them: it runs the actual `run_cycle`
flow (planner -> builder -> verifier_functional -> verifier_human ->
orchestrator decision) multiple times with a deterministic FakeAdapter,
and asserts that session state, score_history, and decision transitions
match the configured policy.

Scenarios:
    - complete_on_first_cycle      : high scores -> completed
    - max_cycles_reached           : low scores + max_cycles=2 -> blocked
    - stagnation_detected          : four needs_iteration cycles with three
                                     consecutive regressions -> blocked by
                                     stagnation policy (2026-04-21 relaxed
                                     threshold)
    - handoff_mode_pauses_cycle    : workflow.human_review_mode=handoff ->
                                     cycle pauses at verifier_functional ->
                                     handoff_active, then handoff-ingest
                                     resumes to completed

Run:
    python -m tools.cycle_e2e_smoke
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

# Force UTF-8 stdout so Korean/em-dash characters in engine messages do not
# crash the Windows cp949 console.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass

import core.app as app
from adapters.base import (
    AdapterQuotaExceededError,
    BaseAdapter,
    Invocation,
    InvocationResult,
)


# ----------------------------------------------------------------------
# Fake adapter
# ----------------------------------------------------------------------


class ScriptedAdapter(BaseAdapter):
    """Returns payloads from a pre-seeded score plan.

    The plan is a list of per-cycle dicts:
        [{"functional": 0.9, "human": 0.9, "result": "pass"}, ...]

    Each ScriptedAdapter reads the cycle index from invocation.context and
    looks up the matching entry. If the list is shorter than the cycle
    index, the last entry is reused.
    """

    def __init__(self, role: str, plan: list[dict[str, Any]]):
        self.role = role
        self.plan = plan
        self.invocations: list[Invocation] = []

    def invoke(self, invocation: Invocation) -> InvocationResult:
        self.invocations.append(invocation)
        cycle_index = int(invocation.context.get("cycle", 1)) if invocation.context else 1
        entry = self.plan[min(cycle_index - 1, len(self.plan) - 1)]
        # Optional quota-failure hook: when the plan entry marks a role for
        # quota failure, raise AdapterQuotaExceededError exactly as a real
        # adapter would when the provider rejects with a rate-limit marker.
        quota_fail_roles = entry.get("quota_fail_roles")
        if isinstance(quota_fail_roles, (list, set, tuple)) and self.role in quota_fail_roles:
            raise AdapterQuotaExceededError(
                f"scripted quota: {self.role} hit rate limit on cycle {cycle_index}"
            )
        if self.role == "planner":
            return InvocationResult(
                status="ok",
                summary="scripted plan",
                payload={
                    "summary": "scripted plan",
                    "plan_summary": "scripted plan",
                    "tasks": [
                        {
                            "id": f"task-{cycle_index}",
                            "title": f"scripted task for cycle {cycle_index}",
                            "acceptance": "objective satisfied",
                            "priority": "medium",
                            "notes": [],
                        }
                    ],
                    "risks": [],
                },
            )
        if self.role == "builder":
            return InvocationResult(
                status="ok",
                summary="scripted build",
                payload={
                    "summary": "scripted build",
                    "change_summary": "scripted build",
                    "files_changed": [],
                    "artifact_paths": [],
                    "self_check": {"summary": "ok", "unresolved": []},
                },
            )
        if self.role == "verifier_functional":
            return InvocationResult(
                status="ok",
                summary="scripted functional review",
                payload={
                    "summary": "scripted functional review",
                    "result": entry.get("result", "pass"),
                    "score": float(entry.get("functional", 0.9)),
                    "findings": [],
                    "evidence": [],
                    "blocking_issues": [],
                    "suggested_actions": [],
                },
            )
        if self.role == "verifier_human":
            return InvocationResult(
                status="ok",
                summary="scripted human review",
                payload={
                    "summary": "scripted human review",
                    "result": entry.get("result", "pass"),
                    "score": float(entry.get("human", 0.9)),
                    "findings": [],
                    "strengths": [],
                    "comparison_notes": [],
                    "suggested_actions": [],
                },
            )
        if self.role == "orchestrator":
            # Test scenarios may override orchestrator judgment explicitly via
            # `orchestrator_decision` / `orchestrator_next_state` / `orchestrator_unresolved`.
            # Otherwise we derive a sensible default from the verifier `result` field so
            # existing regression plans keep behaving identically once the engine starts
            # routing through an LLM orchestrator.
            result = str(entry.get("result", "pass")).lower()
            derived = {
                "pass": ("complete_cycle", "completed"),
                "needs_iteration": ("needs_iteration", "iterating"),
                "fail": ("needs_iteration", "iterating"),
                "block": ("blocked", "blocked"),
            }.get(result, ("needs_iteration", "iterating"))
            decision = str(entry.get("orchestrator_decision", derived[0]))
            next_state = str(entry.get("orchestrator_next_state", derived[1]))
            unresolved = entry.get("orchestrator_unresolved", [])
            if not isinstance(unresolved, list):
                unresolved = []
            return InvocationResult(
                status="ok",
                summary="scripted orchestrator judgment",
                payload={
                    "summary": "scripted orchestrator judgment",
                    "decision": decision,
                    "next_state": next_state,
                    "reason": str(entry.get("orchestrator_reason", f"scripted: derived from result={result}")),
                    "unresolved_items": [str(item) for item in unresolved],
                    "recommended_next_action": str(entry.get("orchestrator_recommendation", "")),
                },
            )
        raise ValueError(f"Unsupported role: {self.role}")


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------


@dataclass(slots=True)
class ScenarioResult:
    name: str
    ok: bool
    message: str


def _init_project(target: Path, *, limits_override: dict[str, Any] | None = None, workflow_override: dict[str, Any] | None = None) -> None:
    """Run `core.app init` in-process and apply optional overrides."""
    init_args = argparse.Namespace(
        target=str(target),
        domain="web",
        mode="greenfield",
        project_name=target.name,
        goal_summary="e2e smoke goal",
    )
    rc = app.run_init(init_args)
    if rc != 0:
        raise RuntimeError(f"run_init failed rc={rc}")
    if limits_override:
        path = target / ".orch" / "config" / "limits.yaml"
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        current.setdefault("limits", {}).update(limits_override)
        path.write_text(yaml.safe_dump(current), encoding="utf-8")
    if workflow_override:
        path = target / ".orch" / "config" / "workflow.yaml"
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        current.setdefault("workflow", {}).update(workflow_override)
        path.write_text(yaml.safe_dump(current), encoding="utf-8")


def _install_scripted_adapters(plan: list[dict[str, Any]]) -> dict[str, ScriptedAdapter]:
    """Patch `core.app._build_adapter` so every role returns scripted payloads."""
    adapters = {
        "planner": ScriptedAdapter("planner", plan),
        "builder": ScriptedAdapter("builder", plan),
        "verifier_functional": ScriptedAdapter("verifier_functional", plan),
        "verifier_human": ScriptedAdapter("verifier_human", plan),
        "orchestrator": ScriptedAdapter("orchestrator", plan),
    }

    def fake_build_adapter(_name: str, *, adapters=adapters, _state: dict[str, str] = {"current": "planner"}):
        # core.app calls _build_adapter(adapter_name) with the provider name
        # ("claude_cli", "codex_cli", "codex_app"). We ignore that and use the
        # current role tracked by the app itself via session.active_role.
        # Simpler: inspect the traceback to see which _run_* function called us.
        # Since all four _run_* helpers call with the provider name, we resolve
        # via the *role* that appears right after _build_adapter in the stack.
        import inspect
        frame = inspect.currentframe()
        try:
            caller = frame.f_back  # type: ignore[union-attr]
            role = (caller.f_locals.get("invocation") or caller.f_locals.get("role")) if caller else None
            if isinstance(role, str) and role in adapters:
                return adapters[role]
            # Fallback: infer from caller function name
            if caller is not None:
                fn_name = caller.f_code.co_name
                role_map = {
                    "_run_planner": "planner",
                    "_run_builder": "builder",
                    "_run_verifier": None,  # resolved below
                    "_run_orchestrator": "orchestrator",
                }
                if fn_name in role_map and role_map[fn_name]:
                    return adapters[role_map[fn_name]]
                if fn_name == "_run_verifier":
                    role_arg = caller.f_locals.get("role")
                    if role_arg in adapters:
                        return adapters[role_arg]
        finally:
            del frame
        # Last resort — should not happen in the smoke flow
        raise RuntimeError("ScriptedAdapter could not resolve the target role")

    app._build_adapter = fake_build_adapter  # type: ignore[assignment]
    return adapters


def _run_cycle(target: Path) -> int:
    cycle_args = argparse.Namespace(target=str(target), goal_summary="")
    return app.run_cycle(cycle_args)


def _read_session(target: Path) -> dict[str, Any]:
    return json.loads(
        (target / ".orch" / "runtime" / "session.json").read_text(encoding="utf-8")
    )


# ----------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------


def _scenario_complete_on_first_cycle(sandbox: Path) -> ScenarioResult:
    target = sandbox / "complete-first"
    _init_project(target)
    _install_scripted_adapters(
        [{"functional": 0.95, "human": 0.95, "result": "pass"}]
    )
    rc = _run_cycle(target)
    if rc != 0:
        return ScenarioResult("complete_on_first_cycle", False, f"run_cycle rc={rc}")
    session = _read_session(target)
    if session.get("state") != "completed":
        return ScenarioResult(
            "complete_on_first_cycle",
            False,
            f"state={session.get('state')} != completed",
        )
    history = session.get("score_history", [])
    if len(history) != 1 or history[0]["decision"] != "complete_cycle":
        return ScenarioResult(
            "complete_on_first_cycle",
            False,
            f"unexpected score_history: {history}",
        )
    return ScenarioResult(
        "complete_on_first_cycle", True, "completed in one cycle with correct history"
    )


def _scenario_max_cycles_reached(sandbox: Path) -> ScenarioResult:
    target = sandbox / "max-cycles"
    _init_project(target, limits_override={"max_cycles": 2, "stop_on_stagnation": False})
    # All cycles return low score + needs_iteration, but different enough to
    # avoid triggering the stagnation stop (which is disabled anyway).
    _install_scripted_adapters(
        [
            {"functional": 0.3, "human": 0.3, "result": "needs_iteration"},
            {"functional": 0.4, "human": 0.4, "result": "needs_iteration"},
        ]
    )
    _run_cycle(target)  # cycle 1 -> needs_iteration -> iterating
    session1 = _read_session(target)
    if session1.get("state") != "iterating":
        return ScenarioResult(
            "max_cycles_reached",
            False,
            f"cycle 1 state={session1.get('state')} != iterating",
        )
    _run_cycle(target)  # cycle 2 -> reaches max_cycles -> blocked
    session2 = _read_session(target)
    if session2.get("state") != "blocked":
        return ScenarioResult(
            "max_cycles_reached",
            False,
            f"cycle 2 state={session2.get('state')} != blocked (expected max_cycles_reached)",
        )
    if session2.get("last_decision") != "max_cycles_reached":
        return ScenarioResult(
            "max_cycles_reached",
            False,
            f"last_decision={session2.get('last_decision')} != max_cycles_reached",
        )
    history = session2.get("score_history", [])
    if len(history) != 2:
        return ScenarioResult(
            "max_cycles_reached",
            False,
            f"expected 2 history entries, got {len(history)}",
        )
    return ScenarioResult(
        "max_cycles_reached", True, "blocked at cycle 2 with max_cycles_reached"
    )


def _scenario_stagnation_detected(sandbox: Path) -> ScenarioResult:
    target = sandbox / "stagnation"
    _init_project(target, limits_override={"max_cycles": 10, "stop_on_stagnation": True})
    # 2026-04-21 relaxed threshold: stagnation requires three consecutive
    # regressions. Here cycles 2→3→4 each regress on both scores, so cycle 4
    # should trip `stagnation_detected` / state=blocked.
    _install_scripted_adapters(
        [
            {"functional": 0.9, "human": 0.9, "result": "needs_iteration"},
            {"functional": 0.7, "human": 0.7, "result": "needs_iteration"},
            {"functional": 0.5, "human": 0.5, "result": "needs_iteration"},
            {"functional": 0.3, "human": 0.3, "result": "needs_iteration"},
        ]
    )
    for _ in range(4):
        _run_cycle(target)
    session = _read_session(target)
    if session.get("state") != "blocked":
        return ScenarioResult(
            "stagnation_detected",
            False,
            f"state={session.get('state')} != blocked",
        )
    if session.get("last_decision") != "stagnation_detected":
        return ScenarioResult(
            "stagnation_detected",
            False,
            f"last_decision={session.get('last_decision')} != stagnation_detected",
        )
    return ScenarioResult(
        "stagnation_detected",
        True,
        "three-regression streak stopped the loop (cycles 2→3→4 each regressed)",
    )


def _scenario_handoff_mode_pauses_cycle(sandbox: Path) -> ScenarioResult:
    target = sandbox / "handoff-mode"
    _init_project(target, workflow_override={"human_review_mode": "handoff"})
    adapters = _install_scripted_adapters(
        [{"functional": 0.9, "human": 0.9, "result": "pass"}]
    )
    rc = _run_cycle(target)
    if rc != 0:
        return ScenarioResult("handoff_mode_pauses_cycle", False, f"run_cycle rc={rc}")
    session = _read_session(target)
    if session.get("state") != "handoff_active":
        return ScenarioResult(
            "handoff_mode_pauses_cycle",
            False,
            f"state={session.get('state')} != handoff_active",
        )
    if session.get("last_decision") != "handoff_requested":
        return ScenarioResult(
            "handoff_mode_pauses_cycle",
            False,
            f"last_decision={session.get('last_decision')} != handoff_requested",
        )
    # verifier_human adapter must NOT have been invoked.
    if adapters["verifier_human"].invocations:
        return ScenarioResult(
            "handoff_mode_pauses_cycle",
            False,
            "verifier_human adapter was called despite handoff mode",
        )
    # Request payload must be on disk.
    request_path = target / ".orch" / "handoff" / "request.yaml"
    if not request_path.exists():
        return ScenarioResult(
            "handoff_mode_pauses_cycle",
            False,
            "handoff request.yaml not created",
        )
    request_doc = yaml.safe_load(request_path.read_text(encoding="utf-8"))
    if request_doc.get("mode") != "approve_gate":
        return ScenarioResult(
            "handoff_mode_pauses_cycle",
            False,
            f"handoff mode={request_doc.get('mode')} != approve_gate",
        )

    # Simulate the external tool filling the response and then ingest.
    response_path = target / ".orch" / "handoff" / "response.yaml"
    response_payload = {
        "handoff_id": request_doc["handoff_id"],
        "completed_at": "2026-04-17T00:00:00Z",
        "result": "approved",
        "summary": "e2e approval",
        "decision": "proceed",
        "findings": [],
        "files_changed": [],
        "artifacts_added": [],
        "recommended_next_action": "resume automated loop",
        "resume_condition": "none",
        "remaining_risks": [],
    }
    response_path.write_text(yaml.safe_dump(response_payload), encoding="utf-8")

    ingest_args = argparse.Namespace(target=str(target))
    ingest_rc = app.run_handoff_ingest(ingest_args)
    if ingest_rc != 0:
        return ScenarioResult(
            "handoff_mode_pauses_cycle",
            False,
            f"handoff-ingest rc={ingest_rc}",
        )
    session_after = _read_session(target)
    if session_after.get("state") != "completed":
        return ScenarioResult(
            "handoff_mode_pauses_cycle",
            False,
            f"state after ingest={session_after.get('state')} != completed",
        )
    # C-4 guard: terminal ingest must clear handoff_pause_count so a later
    # resume cycle does not inherit stale pause accounting.
    if int(session_after.get("handoff_pause_count", -1) or 0) != 0:
        return ScenarioResult(
            "handoff_mode_pauses_cycle",
            False,
            f"handoff_pause_count={session_after.get('handoff_pause_count')} != 0 after terminal ingest",
        )
    return ScenarioResult(
        "handoff_mode_pauses_cycle",
        True,
        "cycle paused for handoff, external approval ingested to completed",
    )


# ----------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------


def _scenario_handoff_feedback_reaches_planner(sandbox: Path) -> ScenarioResult:
    """After handoff-ingest(changes_made) the next planner run must see
    `previous_reviews.handoff` populated from reviews/handoff_latest.json.
    """
    target = sandbox / "handoff-feedback"
    _init_project(target, workflow_override={"human_review_mode": "handoff"})
    adapters = _install_scripted_adapters(
        [{"functional": 0.4, "human": 0.4, "result": "needs_iteration"}]
    )
    rc = _run_cycle(target)
    if rc != 0:
        return ScenarioResult("handoff_feedback_reaches_planner", False, f"cycle1 rc={rc}")

    # Simulate the external reviewer returning changes_made with concrete findings.
    request_path = target / ".orch" / "handoff" / "request.yaml"
    request_doc = yaml.safe_load(request_path.read_text(encoding="utf-8"))
    response_payload = {
        "handoff_id": request_doc["handoff_id"],
        "completed_at": "2026-04-17T00:00:00Z",
        "result": "changes_made",
        "summary": "patched copy but needs planner follow-up",
        "decision": "continue iterating with the concrete notes below",
        "findings": ["copy tone inconsistent on hero", "CTA color contrast low"],
        "files_changed": ["ui/hero.vue"],
        "artifacts_added": [],
        "recommended_next_action": "plan a polish cycle that addresses contrast first",
        "resume_condition": "engine resumes via handoff-ingest",
        "remaining_risks": ["screenshot diff not yet re-run"],
    }
    response_path = target / ".orch" / "handoff" / "response.yaml"
    response_path.write_text(yaml.safe_dump(response_payload), encoding="utf-8")
    ingest_rc = app.run_handoff_ingest(argparse.Namespace(target=str(target)))
    if ingest_rc != 0:
        return ScenarioResult(
            "handoff_feedback_reaches_planner", False, f"ingest rc={ingest_rc}"
        )

    # Second cycle must pick up handoff findings via planner context.
    rc2 = _run_cycle(target)
    if rc2 != 0:
        return ScenarioResult("handoff_feedback_reaches_planner", False, f"cycle2 rc={rc2}")

    planner_adapter = adapters["planner"]
    if not planner_adapter.invocations:
        return ScenarioResult(
            "handoff_feedback_reaches_planner", False, "planner was never invoked on cycle2"
        )
    ctx = planner_adapter.invocations[-1].context or {}
    prior = ctx.get("previous_reviews") or {}
    handoff_block = prior.get("handoff") if isinstance(prior, dict) else None
    if not isinstance(handoff_block, dict):
        return ScenarioResult(
            "handoff_feedback_reaches_planner",
            False,
            f"planner context missing previous_reviews.handoff: {ctx!r}",
        )
    if handoff_block.get("result") != "changes_made":
        return ScenarioResult(
            "handoff_feedback_reaches_planner",
            False,
            f"handoff.result={handoff_block.get('result')} != changes_made",
        )
    if "CTA color contrast low" not in (handoff_block.get("findings") or []):
        return ScenarioResult(
            "handoff_feedback_reaches_planner",
            False,
            f"handoff findings missing: {handoff_block.get('findings')}",
        )
    if "contrast" not in (handoff_block.get("recommended_next_action") or ""):
        return ScenarioResult(
            "handoff_feedback_reaches_planner",
            False,
            f"handoff recommended_next_action missing: {handoff_block.get('recommended_next_action')}",
        )
    return ScenarioResult(
        "handoff_feedback_reaches_planner",
        True,
        "planner received previous_reviews.handoff",
    )


def _scenario_handoff_pause_not_counted_toward_max_cycles(sandbox: Path) -> ScenarioResult:
    """Verify `handoff_pause_count` actually offsets `cycle_index` inside
    `_apply_iteration_policy`. The previous version of this scenario ran
    only handoff-paused cycles, which never call the policy at all — so the
    offset math was never exercised. This version adds a real cli-mode cycle
    at the end whose `needs_iteration` decision DOES flow through
    `_apply_iteration_policy`, and asserts that the stored pause_count
    correctly prevents max_cycles from tripping.

    Plan:
      cycle 1 — handoff mode, pause, ingest(changes_made) -> iterating, pause=1
      cycle 2 — handoff mode, pause, ingest(changes_made) -> iterating, pause=2
      switch workflow to cli
      cycle 3 — cli mode, adapters return needs_iteration.
                cycle_index=3, pause_count=2, effective=1 < max_cycles=2
                -> state stays `iterating`, last_decision=needs_iteration.
                Without the offset it would have been max_cycles_reached.
    """
    target = sandbox / "handoff-max-exempt"
    _init_project(
        target,
        limits_override={"max_cycles": 2, "stop_on_stagnation": False},
        workflow_override={"human_review_mode": "handoff"},
    )
    _install_scripted_adapters(
        [{"functional": 0.4, "human": 0.4, "result": "needs_iteration"}]
    )

    def _ingest_changes_made(tag: str) -> ScenarioResult | None:
        request_doc = yaml.safe_load(
            (target / ".orch" / "handoff" / "request.yaml").read_text(encoding="utf-8")
        )
        response_payload = {
            "handoff_id": request_doc["handoff_id"],
            "completed_at": "2026-04-17T00:00:00Z",
            "result": "changes_made",
            "summary": f"keep iterating ({tag})",
            "decision": "continue",
            "findings": [],
            "files_changed": [],
            "artifacts_added": [],
            "recommended_next_action": "next round",
            "resume_condition": "resume via handoff-ingest",
            "remaining_risks": [],
        }
        (target / ".orch" / "handoff" / "response.yaml").write_text(
            yaml.safe_dump(response_payload), encoding="utf-8"
        )
        if app.run_handoff_ingest(argparse.Namespace(target=str(target))) != 0:
            return ScenarioResult(
                "handoff_pause_not_counted_toward_max_cycles",
                False,
                f"ingest {tag} rc!=0",
            )
        return None

    # Cycle 1 — handoff pause
    if _run_cycle(target) != 0:
        return ScenarioResult(
            "handoff_pause_not_counted_toward_max_cycles", False, "cycle1 rc!=0"
        )
    session1 = _read_session(target)
    if session1.get("state") != "handoff_active" or session1.get("handoff_pause_count") != 1:
        return ScenarioResult(
            "handoff_pause_not_counted_toward_max_cycles",
            False,
            f"cycle1 unexpected session: state={session1.get('state')} pause={session1.get('handoff_pause_count')}",
        )
    err = _ingest_changes_made("cycle1")
    if err:
        return err

    # Cycle 2 — still handoff mode, another pause
    if _run_cycle(target) != 0:
        return ScenarioResult(
            "handoff_pause_not_counted_toward_max_cycles", False, "cycle2 rc!=0"
        )
    session2 = _read_session(target)
    if session2.get("state") != "handoff_active" or session2.get("handoff_pause_count") != 2:
        return ScenarioResult(
            "handoff_pause_not_counted_toward_max_cycles",
            False,
            f"cycle2 unexpected session: state={session2.get('state')} pause={session2.get('handoff_pause_count')}",
        )
    err = _ingest_changes_made("cycle2")
    if err:
        return err

    # Switch to cli mode so the next cycle actually runs verifier_human and
    # reaches `_apply_iteration_policy`.
    workflow_path = target / ".orch" / "config" / "workflow.yaml"
    workflow_doc = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
    workflow_doc.setdefault("workflow", {})["human_review_mode"] = "cli"
    workflow_path.write_text(yaml.safe_dump(workflow_doc), encoding="utf-8")

    # Cycle 3 — real policy check. cycle_index=3, pause=2, effective=1 < 2.
    if _run_cycle(target) != 0:
        return ScenarioResult(
            "handoff_pause_not_counted_toward_max_cycles", False, "cycle3 rc!=0"
        )
    session3 = _read_session(target)
    if session3.get("state") != "iterating":
        return ScenarioResult(
            "handoff_pause_not_counted_toward_max_cycles",
            False,
            f"cycle3 state={session3.get('state')} != iterating "
            f"(max_cycles=2 should have been offset by pause_count=2 to effective=1)",
        )
    if session3.get("last_decision") != "needs_iteration":
        return ScenarioResult(
            "handoff_pause_not_counted_toward_max_cycles",
            False,
            f"cycle3 last_decision={session3.get('last_decision')} != needs_iteration "
            f"(expected offset to prevent max_cycles_reached)",
        )
    if session3.get("handoff_pause_count") != 2:
        return ScenarioResult(
            "handoff_pause_not_counted_toward_max_cycles",
            False,
            f"cycle3 pause_count={session3.get('handoff_pause_count')} != 2 (should not change in cli cycle)",
        )
    return ScenarioResult(
        "handoff_pause_not_counted_toward_max_cycles",
        True,
        "real cli cycle after 2 handoff pauses stayed iterating instead of max_cycles_reached",
    )


def _scenario_terminal_handoff_clears_feedback(sandbox: Path) -> ScenarioResult:
    """A terminal handoff outcome (approved/blocked/rejected) must not leave
    a stale `reviews/handoff_latest.json` behind. Otherwise a later iterating
    cycle — even on unrelated work — would inherit the old verdict and feed
    it to the planner as if it were fresh human feedback.

    Flow:
      cycle 1 — handoff mode, pause, ingest(approved) -> completed
      assert reviews/handoff_latest.json is empty after ingest
      switch workflow to cli, adapters now return needs_iteration
      cycle 2 — completed -> (planner runs with previous_state=completed, safe)
                ends needs_iteration -> state=iterating
      cycle 3 — iterating. Planner context must NOT contain a handoff block
                since the prior approved handoff was cleared.
    """
    target = sandbox / "handoff-terminal-clear"
    _init_project(target, workflow_override={"human_review_mode": "handoff"})
    adapters = _install_scripted_adapters(
        [
            # Cycle 1 adapters never reach verifiers past functional thanks to handoff pause.
            {"functional": 0.9, "human": 0.9, "result": "pass"},
            # After switching to cli mode, cycles 2 and 3 use needs_iteration
            # so _apply_iteration_policy is exercised but max_cycles stays large.
            {"functional": 0.4, "human": 0.4, "result": "needs_iteration"},
            {"functional": 0.4, "human": 0.4, "result": "needs_iteration"},
        ]
    )
    if _run_cycle(target) != 0:
        return ScenarioResult("terminal_handoff_clears_feedback", False, "cycle1 rc!=0")

    request_doc = yaml.safe_load(
        (target / ".orch" / "handoff" / "request.yaml").read_text(encoding="utf-8")
    )
    response_payload = {
        "handoff_id": request_doc["handoff_id"],
        "completed_at": "2026-04-17T00:00:00Z",
        "result": "approved",
        "summary": "LGTM, ship it",
        "decision": "approve",
        "findings": ["this finding should NEVER reach a later iteration"],
        "files_changed": [],
        "artifacts_added": [],
        "recommended_next_action": "stale bait — planner must not see this",
        "resume_condition": "none",
        "remaining_risks": [],
    }
    (target / ".orch" / "handoff" / "response.yaml").write_text(
        yaml.safe_dump(response_payload), encoding="utf-8"
    )
    if app.run_handoff_ingest(argparse.Namespace(target=str(target))) != 0:
        return ScenarioResult("terminal_handoff_clears_feedback", False, "ingest rc!=0")

    latest_path = target / ".orch" / "runtime" / "reviews" / "handoff_latest.json"
    if latest_path.exists():
        body = json.loads(latest_path.read_text(encoding="utf-8"))
        if body:
            return ScenarioResult(
                "terminal_handoff_clears_feedback",
                False,
                f"handoff_latest.json should be empty after approved, got keys={list(body.keys())}",
            )

    # Switch to cli mode so the next cycles actually run verifier_human and
    # reach `_apply_iteration_policy`.
    workflow_path = target / ".orch" / "config" / "workflow.yaml"
    workflow_doc = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
    workflow_doc.setdefault("workflow", {})["human_review_mode"] = "cli"
    workflow_path.write_text(yaml.safe_dump(workflow_doc), encoding="utf-8")

    # Cycle 2 (previous_state=completed) — planner must not get previous_reviews.
    # Then cycle ends needs_iteration so session state becomes iterating.
    if _run_cycle(target) != 0:
        return ScenarioResult("terminal_handoff_clears_feedback", False, "cycle2 rc!=0")
    session2 = _read_session(target)
    if session2.get("state") != "iterating":
        return ScenarioResult(
            "terminal_handoff_clears_feedback",
            False,
            f"cycle2 state={session2.get('state')} != iterating",
        )

    # Cycle 3 (previous_state=iterating) — this is where a leaked stale
    # handoff block would surface. Assert the planner context does NOT have
    # a `previous_reviews.handoff` entry.
    if _run_cycle(target) != 0:
        return ScenarioResult("terminal_handoff_clears_feedback", False, "cycle3 rc!=0")
    planner_invs = adapters["planner"].invocations
    if len(planner_invs) < 3:
        return ScenarioResult(
            "terminal_handoff_clears_feedback",
            False,
            f"planner invoked {len(planner_invs)} times, expected >=3",
        )
    cycle3_ctx = planner_invs[-1].context or {}
    prior = cycle3_ctx.get("previous_reviews") or {}
    if isinstance(prior, dict) and "handoff" in prior:
        return ScenarioResult(
            "terminal_handoff_clears_feedback",
            False,
            f"cycle3 planner inherited stale handoff block: {prior.get('handoff')!r}",
        )
    return ScenarioResult(
        "terminal_handoff_clears_feedback",
        True,
        "approved handoff cleared handoff_latest; later iterating cycle had no stale block",
    )


def _scenario_needs_iteration_then_success(sandbox: Path) -> ScenarioResult:
    """Cycle 1 fails naturally (needs_iteration) and cycle 2 recovers to pass.

    C-1 regression for the organic iteration path: verify the engine does
    NOT jump to a terminal decision after a single low-score cycle, and that
    a follow-up cycle with improved scores closes it out cleanly.
    """
    target = sandbox / "needs-iter-then-success"
    _init_project(target, limits_override={"max_cycles": 5, "stop_on_stagnation": False})
    _install_scripted_adapters(
        [
            {"functional": 0.4, "human": 0.4, "result": "needs_iteration"},
            {"functional": 0.9, "human": 0.9, "result": "pass"},
        ]
    )
    if _run_cycle(target) != 0:
        return ScenarioResult("needs_iteration_then_success", False, "cycle1 rc!=0")
    s1 = _read_session(target)
    if s1.get("state") != "iterating":
        return ScenarioResult(
            "needs_iteration_then_success",
            False,
            f"cycle1 state={s1.get('state')} != iterating",
        )
    if s1.get("last_decision") != "needs_iteration":
        return ScenarioResult(
            "needs_iteration_then_success",
            False,
            f"cycle1 decision={s1.get('last_decision')} != needs_iteration",
        )
    if _run_cycle(target) != 0:
        return ScenarioResult("needs_iteration_then_success", False, "cycle2 rc!=0")
    s2 = _read_session(target)
    if s2.get("state") != "completed":
        return ScenarioResult(
            "needs_iteration_then_success",
            False,
            f"cycle2 state={s2.get('state')} != completed",
        )
    if s2.get("last_decision") != "complete_cycle":
        return ScenarioResult(
            "needs_iteration_then_success",
            False,
            f"cycle2 decision={s2.get('last_decision')} != complete_cycle",
        )
    history = s2.get("score_history", [])
    if len(history) != 2:
        return ScenarioResult(
            "needs_iteration_then_success",
            False,
            f"expected 2 history entries, got {len(history)}",
        )
    return ScenarioResult(
        "needs_iteration_then_success",
        True,
        "cycle1 needs_iteration -> cycle2 complete_cycle with 2 history entries",
    )


SCENARIOS: dict[str, Callable[[Path], ScenarioResult]] = {
    "complete_on_first_cycle": _scenario_complete_on_first_cycle,
    "needs_iteration_then_success": _scenario_needs_iteration_then_success,
    "max_cycles_reached": _scenario_max_cycles_reached,
    "stagnation_detected": _scenario_stagnation_detected,
    "handoff_mode_pauses_cycle": _scenario_handoff_mode_pauses_cycle,
    "handoff_feedback_reaches_planner": _scenario_handoff_feedback_reaches_planner,
    "handoff_pause_not_counted_toward_max_cycles": _scenario_handoff_pause_not_counted_toward_max_cycles,
    "terminal_handoff_clears_feedback": _scenario_terminal_handoff_clears_feedback,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="cycle E2E smoke")
    parser.add_argument("--only", default="", help="Comma-separated scenario names")
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    original_build_adapter = app._build_adapter
    wanted = [name.strip() for name in args.only.split(",") if name.strip()] or list(SCENARIOS)
    unknown = [name for name in wanted if name not in SCENARIOS]
    if unknown:
        print(f"Unknown scenarios: {unknown}")
        return 2

    sandbox = Path(tempfile.mkdtemp(prefix="orch-cycle-e2e-"))
    print(f"Sandbox: {sandbox}")
    results: list[ScenarioResult] = []
    try:
        for name in wanted:
            print(f"\n=== {name} ===")
            try:
                result = SCENARIOS[name](sandbox)
            except Exception as exc:  # noqa: BLE001
                result = ScenarioResult(name, False, f"scenario raised: {exc!r}")
            results.append(result)
            status = "OK" if result.ok else "FAIL"
            print(f"[{status}] {name}: {result.message}")
            # Restore the real adapter builder between scenarios so each test
            # starts clean.
            app._build_adapter = original_build_adapter  # type: ignore[assignment]
    finally:
        if not args.keep_temp:
            shutil.rmtree(sandbox, ignore_errors=True)
        else:
            print(f"Sandbox kept at: {sandbox}")
        app._build_adapter = original_build_adapter  # type: ignore[assignment]

    print("\nSummary")
    print("-------")
    passed = sum(1 for r in results if r.ok)
    for r in results:
        print(f"  {'OK  ' if r.ok else 'FAIL'}  {r.name}: {r.message}")
    print(f"{passed}/{len(results)} scenarios passed.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
