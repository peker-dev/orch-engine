"""End-to-end smoke tests for run_cycle + handoff mode.

This module runs the actual `run_cycle` flow (planner -> builder ->
verifier_functional -> verifier_human -> orchestrator decision) with a
deterministic ScriptedAdapter and asserts that session state,
score_history, and decision transitions match what the orchestrator LLM
decides. Since Phase 2 removed the cycle-based escalation policy
(`max_cycles`, `stop_on_stagnation`), the engine now trusts the
orchestrator LLM's decision verbatim — there is no rule-based override.

Scenarios (post Phase 2 P1-5):
    - complete_on_first_cycle               : high scores -> completed
    - needs_iteration_then_success          : recovery path
    - handoff_mode_pauses_cycle             : workflow.human_review_mode=handoff
    - handoff_feedback_reaches_planner      : prior handoff findings reach planner
    - terminal_handoff_clears_feedback      : approved handoff clears stale feedback
    - utterance_next_speaker_skips_legacy_chain : D5 rule #2
    - declare_done_forces_orchestrator      : D5 rule #1
    - max_utterances_blocks_session         : safety cap → blocked
    - orchestrator_disagree_resumes_cycle   : D5 P0-R 1 — disagree → next_speaker → agree
    - orchestrator_disagree_to_end_blocks_cycle : disagree + __end__ 모순 입력 가드
    - orchestrator_disagree_routes_to_handoff : disagree → verifier_human (handoff) → pause
    - consecutive_disagrees_warns_but_continues : D10 P0-R 3 — N회 연속 disagree 경고
    - orchestrator_disagree_empty_next_blocks_cycle : disagree + next_speaker 빈값 가드
    - custom_verifier_routing                : P0-R 2 (D9/D11) — 도메인 roles.yaml 의 custom verifier 라우팅
    - runner_routes_external_result          : 옵션 C 1차 — echo_runner 가 BaseRunnerAdapter 경로로 utterance 합성
    - runner_nonzero_exit_routes_normally    : 옵션 C 1차 — runner 실패 시 엔진 가로채지 않고 cycle 진행
    - unity_batchmode_dry_run                : 옵션 C 다음 — Unity 미설치 환경 dry-run 인터페이스 회귀
    - user_stop_blocks_cycle                 : 사용자 stop 훅 — .orch/STOP 감지 → cycle BLOCKED + archive

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
        # Phase 2 D4/D5 hook: plan entries can attach per-role utterance
        # routing metadata via `utterances: {role: {next_speaker, declare_done}}`.
        # When present, the ScriptedAdapter returns it as
        # InvocationResult.utterance so run_cycle can exercise the free-
        # utterance dispatch instead of the legacy role chain.
        utterance_map = entry.get("utterances") or {}
        role_utt = utterance_map.get(self.role) if isinstance(utterance_map, dict) else None
        utt_meta: dict[str, Any] | None = None
        if isinstance(role_utt, dict):
            utt_meta = {
                "speaker": self.role,
                "next_speaker": role_utt.get("next_speaker"),
                "declare_done": bool(role_utt.get("declare_done") or False),
                "arbitration": role_utt.get("arbitration"),
            }
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
                utterance=utt_meta,
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
                utterance=utt_meta,
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
                utterance=utt_meta,
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
                utterance=utt_meta,
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
                utterance=utt_meta,
            )
        # P0-R 2 (D9/D11): family="verifier" custom 역할 (예: verifier_safety)
        # 도 functional 형태로 응답한다. native verifier 가 아닌데 verifier_*
        # prefix 인 경우만 적용 — 다른 family 가 추가될 때까지 misuse 방지.
        if self.role.startswith("verifier_"):
            return InvocationResult(
                status="ok",
                summary=f"scripted custom verifier ({self.role})",
                payload={
                    "summary": f"scripted custom verifier ({self.role})",
                    "result": entry.get("result", "pass"),
                    "score": float(entry.get("functional", 0.9)),
                    "findings": [],
                    "evidence": [],
                    "blocking_issues": [],
                    "suggested_actions": [],
                },
                utterance=utt_meta,
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
            # After switching to cli mode, cycles 2 and 3 return needs_iteration
            # so the orchestrator decides continuation (no rule-based override).
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

    # Switch to cli mode so the next cycles actually run verifier_human
    # (and thus the orchestrator) instead of pausing for handoff.
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
    _init_project(target)
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


def _scenario_utterance_next_speaker_skips_legacy_chain(sandbox: Path) -> ScenarioResult:
    """Phase 2 D5 rule #2: when a speaker's utterance.v1 names `next_speaker`,
    the engine follows it instead of the legacy role chain.

    Setup: planner emits utterance.v1 with `next_speaker=verifier_functional`,
    so the engine must skip the builder step entirely. verifier_functional and
    verifier_human then proceed as normal, and orchestrator closes the cycle.
    """
    target = sandbox / "utt-next-speaker"
    _init_project(target)
    adapters = _install_scripted_adapters(
        [
            {
                "functional": 0.95,
                "human": 0.95,
                "result": "pass",
                "utterances": {
                    "planner": {"next_speaker": "verifier_functional"},
                },
            }
        ]
    )
    rc = _run_cycle(target)
    if rc != 0:
        return ScenarioResult(
            "utterance_next_speaker_skips_legacy_chain", False, f"run_cycle rc={rc}"
        )
    if adapters["builder"].invocations:
        return ScenarioResult(
            "utterance_next_speaker_skips_legacy_chain",
            False,
            "builder was invoked despite planner routing past it",
        )
    for required in ("planner", "verifier_functional", "verifier_human", "orchestrator"):
        if not adapters[required].invocations:
            return ScenarioResult(
                "utterance_next_speaker_skips_legacy_chain",
                False,
                f"{required} was not invoked (utterance routing broken)",
            )
    session = _read_session(target)
    if session.get("state") != "completed":
        return ScenarioResult(
            "utterance_next_speaker_skips_legacy_chain",
            False,
            f"state={session.get('state')} != completed",
        )
    return ScenarioResult(
        "utterance_next_speaker_skips_legacy_chain",
        True,
        "planner.next_speaker=verifier_functional skipped the builder step",
    )


def _scenario_declare_done_forces_orchestrator(sandbox: Path) -> ScenarioResult:
    """Phase 2 D5 rule #1: declare_done=true forces an immediate orchestrator
    call, regardless of the named next_speaker.

    Setup: builder returns utterance.v1 with `declare_done=true` and a
    deliberately wrong `next_speaker=verifier_functional`. The engine must
    ignore that and jump straight to orchestrator, skipping both verifiers.
    """
    target = sandbox / "declare-done"
    _init_project(target)
    adapters = _install_scripted_adapters(
        [
            {
                "functional": 0.95,
                "human": 0.95,
                "result": "pass",
                "utterances": {
                    "builder": {
                        "declare_done": True,
                        "next_speaker": "verifier_functional",
                    },
                },
            }
        ]
    )
    rc = _run_cycle(target)
    if rc != 0:
        return ScenarioResult(
            "declare_done_forces_orchestrator", False, f"run_cycle rc={rc}"
        )
    if adapters["verifier_functional"].invocations:
        return ScenarioResult(
            "declare_done_forces_orchestrator",
            False,
            "verifier_functional invoked despite declare_done forcing orchestrator",
        )
    if adapters["verifier_human"].invocations:
        return ScenarioResult(
            "declare_done_forces_orchestrator",
            False,
            "verifier_human invoked despite declare_done forcing orchestrator",
        )
    if not adapters["orchestrator"].invocations:
        return ScenarioResult(
            "declare_done_forces_orchestrator",
            False,
            "orchestrator was not invoked after declare_done",
        )
    session = _read_session(target)
    if session.get("state") != "completed":
        return ScenarioResult(
            "declare_done_forces_orchestrator",
            False,
            f"state={session.get('state')} != completed",
        )
    return ScenarioResult(
        "declare_done_forces_orchestrator",
        True,
        "builder.declare_done=true routed straight to orchestrator (verifiers skipped)",
    )


def _scenario_max_utterances_blocks_session(sandbox: Path) -> ScenarioResult:
    """Safety net: if the free-utterance loop exceeds the configured
    `cycle_safety.max_utterances_per_cycle` without reaching an orchestrator
    decision, the engine must leave the session in a RESUMABLE state
    (blocked), not mid-flight (planning/building). Otherwise the next
    run-cycle would be rejected by RESUMABLE_STATES and the project would
    deadlock.

    Setup: planner and builder ping-pong forever via utterance.next_speaker.
    Limits override sets max_utterances_per_cycle=12 so the cap fires fast
    (production default is 100). Session.state should end up 'blocked' with
    last_decision='cycle_max_utterances_exceeded'.
    """
    target = sandbox / "max-utterances"
    # cycle_safety 는 _init_project 의 shallow update 로 통째 교체되므로
    # 두 키 모두 명시 (그렇지 않으면 max_consecutive_disagrees 가 누락돼 fallback 7).
    # 이 시나리오는 ping-pong 만이라 disagree 가 발생하지 않아 동작에는 무관하지만,
    # 향후 시나리오 확장 시 의도치 않은 fallback 을 막기 위한 안전 패턴.
    _init_project(
        target,
        limits_override={
            "cycle_safety": {
                "max_utterances_per_cycle": 12,
                "max_consecutive_disagrees": 7,
            }
        },
    )
    _install_scripted_adapters(
        [
            {
                "functional": 0.95,
                "human": 0.95,
                "result": "pass",
                "utterances": {
                    "planner": {"next_speaker": "builder"},
                    "builder": {"next_speaker": "planner"},
                },
            }
        ]
    )
    rc = _run_cycle(target)
    if rc != 2:
        return ScenarioResult(
            "max_utterances_blocks_session",
            False,
            f"expected rc=2 after loop cap, got rc={rc}",
        )
    session = _read_session(target)
    if session.get("state") != "blocked":
        return ScenarioResult(
            "max_utterances_blocks_session",
            False,
            f"state={session.get('state')} != blocked (would deadlock next run-cycle)",
        )
    if session.get("last_decision") != "cycle_max_utterances_exceeded":
        return ScenarioResult(
            "max_utterances_blocks_session",
            False,
            f"last_decision={session.get('last_decision')} != cycle_max_utterances_exceeded",
        )
    return ScenarioResult(
        "max_utterances_blocks_session",
        True,
        "loop cap tripped, session transitioned to blocked (resumable guard)",
    )


class StatefulOrchestratorAdapter(ScriptedAdapter):
    """orchestrator 가 같은 cycle 안에서 호출될 때마다 다른 결정·arbitration 을 반환.

    P0-R 1 (D5: arbitration=disagree → next_speaker 지명으로 cycle 재개) 검증용.
    1차 호출에서 disagree, 2차 호출에서 agree 같은 흐름을 시나리오에 주입할 수 있다.
    """

    def __init__(self, decisions: list[dict[str, Any]]):
        super().__init__("orchestrator", [])
        self._decisions = decisions
        self._call_idx = 0

    def invoke(self, invocation: Invocation) -> InvocationResult:
        self.invocations.append(invocation)
        idx = min(self._call_idx, len(self._decisions) - 1)
        entry = self._decisions[idx]
        self._call_idx += 1
        utt_meta = {
            "speaker": "orchestrator",
            "next_speaker": entry.get("next_speaker", "__end__"),
            "declare_done": False,
            "arbitration": entry.get("arbitration"),
        }
        return InvocationResult(
            status="ok",
            summary="stateful orchestrator",
            payload={
                "summary": "stateful orchestrator",
                "decision": entry.get("decision", "complete_cycle"),
                "next_state": entry.get("next_state", "completed"),
                "reason": entry.get("reason", "scripted stateful"),
                "unresolved_items": [],
                "recommended_next_action": "",
            },
            utterance=utt_meta,
        )


def _scenario_orchestrator_disagree_resumes_cycle(sandbox: Path) -> ScenarioResult:
    """Phase 2 D5 (P0-R 1): orchestrator arbitration=disagree 시 같은 cycle 안에서
    next_speaker 따라 재개. 두 번째 orchestrator 호출에서 agree 받으면 cycle 종료.

    흐름: planner → builder → verifier_functional → verifier_human(declare_done)
        → orchestrator [1차, disagree, next_speaker=builder]
        → builder → verifier_functional → verifier_human(declare_done)
        → orchestrator [2차, agree, next_speaker=__end__] → END.

    검증: 한 cycle 안에서 orchestrator 2회·builder 2회 호출되고, 최종 state=completed,
    score_history 길이 1 (재개 흐름이 새 cycle 을 열지 않았다).
    """
    target = sandbox / "orch-disagree-resume"
    _init_project(target)

    plan = [
        {
            "functional": 0.7,
            "human": 0.7,
            "result": "pass",
            "utterances": {
                # verifier_human 발화에서 declare_done → orchestrator 강제 호출.
                # 두 번 호출되더라도 동일 utterance 가 적용되므로 매번 declare_done.
                "verifier_human": {"declare_done": True, "next_speaker": "orchestrator"},
            },
        }
    ]
    role_adapters: dict[str, ScriptedAdapter] = {
        "planner": ScriptedAdapter("planner", plan),
        "builder": ScriptedAdapter("builder", plan),
        "verifier_functional": ScriptedAdapter("verifier_functional", plan),
        "verifier_human": ScriptedAdapter("verifier_human", plan),
        "orchestrator": StatefulOrchestratorAdapter(
            [
                {
                    "arbitration": "disagree",
                    "next_speaker": "builder",
                    "decision": "needs_iteration",
                    "next_state": "iterating",
                    "reason": "보강 필요 (disagree)",
                },
                {
                    "arbitration": "agree",
                    "next_speaker": "__end__",
                    "decision": "complete_cycle",
                    "next_state": "completed",
                    "reason": "수렴 (agree)",
                },
            ]
        ),
    }

    def fake_build_adapter(_name: str, *, role_adapters=role_adapters):
        import inspect

        frame = inspect.currentframe()
        try:
            caller = frame.f_back  # type: ignore[union-attr]
            if caller is not None:
                fn_name = caller.f_code.co_name
                role_map = {
                    "_run_planner": "planner",
                    "_run_builder": "builder",
                    "_run_orchestrator": "orchestrator",
                }
                if fn_name in role_map:
                    return role_adapters[role_map[fn_name]]
                if fn_name == "_run_verifier":
                    role_arg = caller.f_locals.get("role")
                    if role_arg in role_adapters:
                        return role_adapters[role_arg]
        finally:
            del frame
        raise RuntimeError("StatefulOrchestrator scenario could not resolve target role")

    app._build_adapter = fake_build_adapter  # type: ignore[assignment]

    rc = _run_cycle(target)
    if rc != 0:
        return ScenarioResult(
            "orchestrator_disagree_resumes_cycle", False, f"run_cycle rc={rc}"
        )

    orch_calls = len(role_adapters["orchestrator"].invocations)
    if orch_calls != 2:
        return ScenarioResult(
            "orchestrator_disagree_resumes_cycle",
            False,
            f"orchestrator invoked {orch_calls} times, expected 2 (disagree → resume → agree)",
        )
    builder_calls = len(role_adapters["builder"].invocations)
    if builder_calls != 2:
        return ScenarioResult(
            "orchestrator_disagree_resumes_cycle",
            False,
            f"builder invoked {builder_calls} times, expected 2 (initial + resume after disagree)",
        )
    session = _read_session(target)
    if session.get("state") != "completed":
        return ScenarioResult(
            "orchestrator_disagree_resumes_cycle",
            False,
            f"state={session.get('state')} != completed",
        )
    if session.get("last_decision") != "complete_cycle":
        return ScenarioResult(
            "orchestrator_disagree_resumes_cycle",
            False,
            f"last_decision={session.get('last_decision')} != complete_cycle",
        )
    history = session.get("score_history", [])
    if len(history) != 1:
        return ScenarioResult(
            "orchestrator_disagree_resumes_cycle",
            False,
            f"score_history len={len(history)}, expected 1 (resume must stay inside one cycle)",
        )
    if history[0].get("decision") != "complete_cycle":
        return ScenarioResult(
            "orchestrator_disagree_resumes_cycle",
            False,
            f"history[0].decision={history[0].get('decision')} != complete_cycle",
        )
    return ScenarioResult(
        "orchestrator_disagree_resumes_cycle",
        True,
        "disagree → next_speaker=builder → declare_done → agree, 한 cycle 안에서 closure",
    )


def _install_role_adapters(
    role_adapters: dict[str, ScriptedAdapter],
    *,
    fallback_build_adapter: Callable[[str], Any] | None = None,
) -> None:
    """`_install_scripted_adapters` 와 같은 inspect-based 라우팅을 임의 role_adapters
    딕셔너리에 적용. StatefulOrchestratorAdapter 등 시나리오 전용 adapter 를 섞을 때 사용.

    fallback_build_adapter 가 주어지면, _run_verifier 가 role_adapters 에 없는 역할을
    호출할 때 그 함수로 위임 — 옵션 C 의 echo_runner 같이 진짜 entry point 를 통해
    돌려야 하는 runner 용. None 이면 기존처럼 RuntimeError.
    """

    def fake_build_adapter(_name: str, *, role_adapters=role_adapters, fallback=fallback_build_adapter):
        import inspect

        frame = inspect.currentframe()
        try:
            caller = frame.f_back  # type: ignore[union-attr]
            if caller is not None:
                fn_name = caller.f_code.co_name
                role_map = {
                    "_run_planner": "planner",
                    "_run_builder": "builder",
                    "_run_orchestrator": "orchestrator",
                }
                if fn_name in role_map:
                    return role_adapters[role_map[fn_name]]
                if fn_name == "_run_verifier":
                    role_arg = caller.f_locals.get("role")
                    if role_arg in role_adapters:
                        return role_adapters[role_arg]
                    if fallback is not None:
                        return fallback(_name)
        finally:
            del frame
        raise RuntimeError("install_role_adapters could not resolve target role")

    app._build_adapter = fake_build_adapter  # type: ignore[assignment]


def _scenario_orchestrator_disagree_to_end_blocks_cycle(sandbox: Path) -> ScenarioResult:
    """모순 입력 가드: orchestrator arbitration=disagree 인데 next_speaker=__end__
    이면 AdapterExecutionError 로 cycle 을 BLOCKED 처리.
    """
    target = sandbox / "orch-disagree-to-end"
    _init_project(target)

    plan = [
        {
            "functional": 0.7,
            "human": 0.7,
            "result": "pass",
            "utterances": {
                "verifier_human": {"declare_done": True, "next_speaker": "orchestrator"},
            },
        }
    ]
    role_adapters: dict[str, ScriptedAdapter] = {
        "planner": ScriptedAdapter("planner", plan),
        "builder": ScriptedAdapter("builder", plan),
        "verifier_functional": ScriptedAdapter("verifier_functional", plan),
        "verifier_human": ScriptedAdapter("verifier_human", plan),
        "orchestrator": StatefulOrchestratorAdapter(
            [
                {
                    "arbitration": "disagree",
                    "next_speaker": "__end__",
                    "decision": "needs_iteration",
                    "next_state": "iterating",
                    "reason": "모순 입력 (disagree + __end__)",
                },
            ]
        ),
    }
    _install_role_adapters(role_adapters)

    rc = _run_cycle(target)
    if rc != 2:
        return ScenarioResult(
            "orchestrator_disagree_to_end_blocks_cycle",
            False,
            f"expected rc=2 (BLOCKED), got rc={rc}",
        )
    session = _read_session(target)
    if session.get("state") != "blocked":
        return ScenarioResult(
            "orchestrator_disagree_to_end_blocks_cycle",
            False,
            f"state={session.get('state')} != blocked",
        )
    return ScenarioResult(
        "orchestrator_disagree_to_end_blocks_cycle",
        True,
        "disagree + next_speaker=__end__ 모순 입력이 명시 에러로 BLOCKED 처리됨",
    )


def _scenario_orchestrator_disagree_routes_to_handoff(sandbox: Path) -> ScenarioResult:
    """disagree → next_speaker=verifier_human (+ workflow.human_review_mode=handoff) 시
    cycle 이 일시정지 (handoff_active) 로 진입하고, reviews/orchestrator_latest.json
    이 비어있음 (P0-R 1 stale 방지 가드 검증). verifier_functional 이 declare_done
    을 내서 verifier_human 우회 후 orchestrator 가 disagree → verifier_human 으로
    되돌리는 흐름.
    """
    target = sandbox / "orch-disagree-to-handoff"
    _init_project(target, workflow_override={"human_review_mode": "handoff"})

    plan = [
        {
            "functional": 0.7,
            "human": 0.7,
            "result": "pass",
            "utterances": {
                # verifier_functional 에서 declare_done → handoff 모드에서 verifier_human 우회.
                "verifier_functional": {
                    "declare_done": True,
                    "next_speaker": "orchestrator",
                },
            },
        }
    ]
    role_adapters: dict[str, ScriptedAdapter] = {
        "planner": ScriptedAdapter("planner", plan),
        "builder": ScriptedAdapter("builder", plan),
        "verifier_functional": ScriptedAdapter("verifier_functional", plan),
        "verifier_human": ScriptedAdapter("verifier_human", plan),
        "orchestrator": StatefulOrchestratorAdapter(
            [
                {
                    "arbitration": "disagree",
                    "next_speaker": "verifier_human",
                    "decision": "needs_iteration",
                    "next_state": "iterating",
                    "reason": "사람 검수 필요",
                },
            ]
        ),
    }
    _install_role_adapters(role_adapters)

    rc = _run_cycle(target)
    if rc != 0:
        return ScenarioResult(
            "orchestrator_disagree_routes_to_handoff",
            False,
            f"expected rc=0 (handoff pause), got rc={rc}",
        )
    session = _read_session(target)
    if session.get("state") != "handoff_active":
        return ScenarioResult(
            "orchestrator_disagree_routes_to_handoff",
            False,
            f"state={session.get('state')} != handoff_active",
        )
    if session.get("last_decision") != "handoff_requested":
        return ScenarioResult(
            "orchestrator_disagree_routes_to_handoff",
            False,
            f"last_decision={session.get('last_decision')} != handoff_requested",
        )
    # P0-R 1 stale 방지 가드 검증: disagree 발화의 reviews 가 비워져야 함.
    reviews_path = target / ".orch" / "runtime" / "reviews" / "orchestrator_latest.json"
    if reviews_path.exists():
        body = json.loads(reviews_path.read_text(encoding="utf-8"))
        if body:
            return ScenarioResult(
                "orchestrator_disagree_routes_to_handoff",
                False,
                f"reviews/orchestrator_latest.json should be empty after disagree, got keys={list(body.keys())}",
            )
    if not (target / ".orch" / "handoff" / "request.yaml").exists():
        return ScenarioResult(
            "orchestrator_disagree_routes_to_handoff",
            False,
            "handoff request.yaml not created",
        )
    return ScenarioResult(
        "orchestrator_disagree_routes_to_handoff",
        True,
        "disagree → verifier_human (handoff 모드) → 일시정지 + reviews stale 가드 검증",
    )


def _scenario_consecutive_disagrees_warns_but_continues(sandbox: Path) -> ScenarioResult:
    """P0-R 3 (D10): orchestrator 가 같은 cycle 안에서 max_consecutive_disagrees
    회 연속 disagree 면 stdout + events.jsonl 에 경고 1회. 엔진은 중단하지 않고
    계속 진행하다가 결국 agree 로 closure. 사용자 stop 이 최종 방어선.

    Setup: max_consecutive_disagrees=3, max_utterances_per_cycle=50 으로 override.
    StatefulOrchestratorAdapter 가 disagree 4회 후 agree 1회 시퀀스 반환. 흐름:
    planner → builder → vf → vh(done) → orch[1차 disagree] → builder → vf →
    vh(done) → orch[2차 disagree] → ... → orch[5차 agree] → END.
    """
    target = sandbox / "consecutive-disagrees"
    _init_project(
        target,
        limits_override={
            "cycle_safety": {
                "max_utterances_per_cycle": 50,
                "max_consecutive_disagrees": 3,
            }
        },
    )

    plan = [
        {
            "functional": 0.7,
            "human": 0.7,
            "result": "pass",
            "utterances": {
                "verifier_human": {"declare_done": True, "next_speaker": "orchestrator"},
            },
        }
    ]
    role_adapters: dict[str, ScriptedAdapter] = {
        "planner": ScriptedAdapter("planner", plan),
        "builder": ScriptedAdapter("builder", plan),
        "verifier_functional": ScriptedAdapter("verifier_functional", plan),
        "verifier_human": ScriptedAdapter("verifier_human", plan),
        "orchestrator": StatefulOrchestratorAdapter(
            [
                {
                    "arbitration": "disagree",
                    "next_speaker": "builder",
                    "decision": "needs_iteration",
                    "next_state": "iterating",
                    "reason": f"disagree #{i + 1}",
                }
                for i in range(4)
            ]
            + [
                {
                    "arbitration": "agree",
                    "next_speaker": "__end__",
                    "decision": "complete_cycle",
                    "next_state": "completed",
                    "reason": "마침내 수렴",
                }
            ]
        ),
    }
    _install_role_adapters(role_adapters)

    rc = _run_cycle(target)
    if rc != 0:
        return ScenarioResult(
            "consecutive_disagrees_warns_but_continues",
            False,
            f"expected rc=0 (eventual agree), got rc={rc}",
        )
    session = _read_session(target)
    if session.get("state") != "completed":
        return ScenarioResult(
            "consecutive_disagrees_warns_but_continues",
            False,
            f"state={session.get('state')} != completed (engine should NOT block on disagree warnings)",
        )

    orch_calls = len(role_adapters["orchestrator"].invocations)
    if orch_calls != 5:
        return ScenarioResult(
            "consecutive_disagrees_warns_but_continues",
            False,
            f"orchestrator invoked {orch_calls} times, expected 5 (4 disagree + 1 agree)",
        )

    # events.jsonl 에 consecutive_disagrees_warning 이 정확히 1회 (한 cycle 안에서 한 번만 emit).
    events_path = target / ".orch" / "runtime" / "events.jsonl"
    if not events_path.exists():
        return ScenarioResult(
            "consecutive_disagrees_warns_but_continues",
            False,
            "events.jsonl not created",
        )
    warning_lines = []
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        evt = json.loads(raw)
        if evt.get("event") == "consecutive_disagrees_warning":
            warning_lines.append(evt)
    if len(warning_lines) != 1:
        return ScenarioResult(
            "consecutive_disagrees_warns_but_continues",
            False,
            f"consecutive_disagrees_warning emitted {len(warning_lines)} times, expected 1",
        )
    warn = warning_lines[0]
    if warn.get("threshold") != 3:
        return ScenarioResult(
            "consecutive_disagrees_warns_but_continues",
            False,
            f"warning threshold={warn.get('threshold')} != 3",
        )
    if warn.get("consecutive_disagrees") != 3:
        return ScenarioResult(
            "consecutive_disagrees_warns_but_continues",
            False,
            f"warning fired at consecutive_disagrees={warn.get('consecutive_disagrees')} != 3",
        )

    return ScenarioResult(
        "consecutive_disagrees_warns_but_continues",
        True,
        "disagree 4회 연속에 임계(3) 도달 경고 1회, agree 까지 계속 진행해서 cycle closure",
    )


def _scenario_orchestrator_disagree_empty_next_blocks_cycle(sandbox: Path) -> ScenarioResult:
    """모순 입력 가드 (P0-R 1 후속): orchestrator arbitration=disagree 인데
    next_speaker 가 빈값이면 명시적 BLOCKED. 그대로 두면 Rule #2 의 legacy chain
    종점 에러가 던져져서 디버깅이 느림.
    """
    target = sandbox / "orch-disagree-empty-next"
    _init_project(target)

    plan = [
        {
            "functional": 0.7,
            "human": 0.7,
            "result": "pass",
            "utterances": {
                "verifier_human": {"declare_done": True, "next_speaker": "orchestrator"},
            },
        }
    ]
    role_adapters: dict[str, ScriptedAdapter] = {
        "planner": ScriptedAdapter("planner", plan),
        "builder": ScriptedAdapter("builder", plan),
        "verifier_functional": ScriptedAdapter("verifier_functional", plan),
        "verifier_human": ScriptedAdapter("verifier_human", plan),
        "orchestrator": StatefulOrchestratorAdapter(
            [
                {
                    "arbitration": "disagree",
                    "next_speaker": "",  # 빈값 — 모순 입력
                    "decision": "needs_iteration",
                    "next_state": "iterating",
                    "reason": "disagree but next_speaker missing",
                },
            ]
        ),
    }
    _install_role_adapters(role_adapters)

    rc = _run_cycle(target)
    if rc != 2:
        return ScenarioResult(
            "orchestrator_disagree_empty_next_blocks_cycle",
            False,
            f"expected rc=2 (BLOCKED), got rc={rc}",
        )
    session = _read_session(target)
    if session.get("state") != "blocked":
        return ScenarioResult(
            "orchestrator_disagree_empty_next_blocks_cycle",
            False,
            f"state={session.get('state')} != blocked",
        )
    if session.get("last_decision") != "orchestrator_disagree_invalid_next":
        return ScenarioResult(
            "orchestrator_disagree_empty_next_blocks_cycle",
            False,
            f"last_decision={session.get('last_decision')} != orchestrator_disagree_invalid_next",
        )
    return ScenarioResult(
        "orchestrator_disagree_empty_next_blocks_cycle",
        True,
        "disagree + next_speaker 빈값 모순 입력이 명시 BLOCKED 처리됨",
    )


def _scenario_custom_verifier_routing(sandbox: Path) -> ScenarioResult:
    """P0-R 2 (D9/D11) 1차 MVP: 도메인이 `domains/<id>/roles.yaml` 에 family="verifier"
    custom 역할을 선언하면 dispatch loop 가 정상 라우팅하고 reviews/<role>_latest.json
    이 작성된다. 엔진 코드 변경 없이 도메인 설정 파일만으로 새 검수자가 합류하는지 검증.
    """
    # ENGINE_ROOT/domains/<test_id>/roles.yaml 임시 fixture. 다른 시나리오가
    # 같은 sandbox 를 공유할 수 있으므로 UUID 로 unique id 를 만들고 finally 에서 정리.
    from adapters import base as _ab
    from uuid import uuid4 as _uuid4

    test_domain_id = f"__test_custom_role_{_uuid4().hex[:8]}__"
    test_domain_dir = Path(_ab.ENGINE_ROOT) / "domains" / test_domain_id
    test_domain_dir.mkdir(parents=True, exist_ok=True)
    (test_domain_dir / "roles.yaml").write_text(
        "roles:\n"
        "  - id: verifier_safety\n"
        "    family: verifier\n"
        "    display: 안전 검수자\n"
        "    default_provider: codex_cli\n",
        encoding="utf-8",
    )
    target = sandbox / "custom-verifier"
    try:
        _init_project(target)
        # session.json 의 domain 키를 임시 도메인 id 로 교체. _resolve_domain_id 가
        # session.json -> project.yaml 순으로 보므로 session.json 만 갱신해도 충분.
        session_path = target / ".orch" / "runtime" / "session.json"
        session_data = json.loads(session_path.read_text(encoding="utf-8"))
        session_data["domain"] = test_domain_id
        session_path.write_text(
            json.dumps(session_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        plan = [
            {
                "functional": 0.95,
                "human": 0.95,
                "result": "pass",
                "utterances": {
                    "planner": {"next_speaker": "builder"},
                    "builder": {"next_speaker": "verifier_safety"},
                    "verifier_safety": {"next_speaker": "verifier_functional"},
                    "verifier_functional": {"next_speaker": "verifier_human"},
                    "verifier_human": {"declare_done": True, "next_speaker": "orchestrator"},
                    "orchestrator": {"arbitration": "agree", "next_speaker": "__end__"},
                },
            }
        ]
        role_adapters: dict[str, ScriptedAdapter] = {
            "planner": ScriptedAdapter("planner", plan),
            "builder": ScriptedAdapter("builder", plan),
            "verifier_safety": ScriptedAdapter("verifier_safety", plan),
            "verifier_functional": ScriptedAdapter("verifier_functional", plan),
            "verifier_human": ScriptedAdapter("verifier_human", plan),
            "orchestrator": ScriptedAdapter("orchestrator", plan),
        }
        _install_role_adapters(role_adapters)

        rc = _run_cycle(target)
        if rc != 0:
            return ScenarioResult("custom_verifier_routing", False, f"run_cycle rc={rc}")
        if not role_adapters["verifier_safety"].invocations:
            return ScenarioResult(
                "custom_verifier_routing", False, "verifier_safety adapter was never invoked"
            )
        review_path = target / ".orch" / "reviews" / "verifier_safety_latest.json"
        if not review_path.exists():
            return ScenarioResult(
                "custom_verifier_routing", False,
                "reviews/verifier_safety_latest.json was not written",
            )
        review_data = json.loads(review_path.read_text(encoding="utf-8"))
        if review_data.get("role") != "verifier_safety":
            return ScenarioResult(
                "custom_verifier_routing", False,
                f"review.role={review_data.get('role')!r} != 'verifier_safety'",
            )
        session = _read_session(target)
        if session.get("state") != "completed":
            return ScenarioResult(
                "custom_verifier_routing", False,
                f"final state={session.get('state')!r} != 'completed'",
            )
        return ScenarioResult(
            "custom_verifier_routing", True,
            "domain roles.yaml 의 verifier_safety 가 dispatch loop 라우팅 + reviews 분리 저장 + cycle closure",
        )
    finally:
        shutil.rmtree(test_domain_dir, ignore_errors=True)


def _scenario_runner_routes_external_result(sandbox: Path) -> ScenarioResult:
    """옵션 C 1차 stride: domain roles.yaml 의 verifier_echo (default_provider=echo_runner)
    가 BaseRunnerAdapter 경로로 합류해서 utterance 를 자동 합성, reviews/<role>_latest.json
    이 작성되고 cycle 이 정상 완료된다. echo_runner 는 실제 코드를 그대로 돌린다.
    """
    from adapters import base as _ab
    from uuid import uuid4 as _uuid4

    test_domain_id = f"__test_runner_routing_{_uuid4().hex[:8]}__"
    test_domain_dir = Path(_ab.ENGINE_ROOT) / "domains" / test_domain_id
    test_domain_dir.mkdir(parents=True, exist_ok=True)
    (test_domain_dir / "roles.yaml").write_text(
        "roles:\n"
        "  - id: verifier_echo\n"
        "    family: verifier\n"
        "    display: 에코 검수자\n"
        "    default_provider: echo_runner\n"
        "    next_speaker_default: verifier_human\n",
        encoding="utf-8",
    )
    target = sandbox / "runner-echo"
    original_build_adapter = app._build_adapter
    try:
        _init_project(target)
        session_path = target / ".orch" / "runtime" / "session.json"
        session_data = json.loads(session_path.read_text(encoding="utf-8"))
        session_data["domain"] = test_domain_id
        session_path.write_text(
            json.dumps(session_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        roles_path = target / ".orch" / "config" / "roles.yaml"
        roles_data = json.loads(roles_path.read_text(encoding="utf-8"))
        roles_data.setdefault("roles", {})["verifier_echo"] = "echo_runner"
        roles_path.write_text(
            json.dumps(roles_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        plan = [
            {
                "functional": 0.95,
                "human": 0.95,
                "result": "pass",
                "utterances": {
                    "planner": {"next_speaker": "builder"},
                    "builder": {"next_speaker": "verifier_echo"},
                    "verifier_human": {"declare_done": True, "next_speaker": "orchestrator"},
                    "orchestrator": {"arbitration": "agree", "next_speaker": "__end__"},
                },
            }
        ]
        role_adapters: dict[str, ScriptedAdapter] = {
            "planner": ScriptedAdapter("planner", plan),
            "builder": ScriptedAdapter("builder", plan),
            "verifier_human": ScriptedAdapter("verifier_human", plan),
            "orchestrator": ScriptedAdapter("orchestrator", plan),
        }
        _install_role_adapters(role_adapters, fallback_build_adapter=original_build_adapter)

        rc = _run_cycle(target)
        if rc != 0:
            return ScenarioResult("runner_routes_external_result", False, f"run_cycle rc={rc}")
        review_path = target / ".orch" / "reviews" / "verifier_echo_latest.json"
        if not review_path.exists():
            return ScenarioResult(
                "runner_routes_external_result", False,
                "reviews/verifier_echo_latest.json was not written",
            )
        review_data = json.loads(review_path.read_text(encoding="utf-8"))
        if review_data.get("role") != "verifier_echo":
            return ScenarioResult(
                "runner_routes_external_result", False,
                f"review.role={review_data.get('role')!r} != 'verifier_echo'",
            )
        if review_data.get("result") != "pass":
            return ScenarioResult(
                "runner_routes_external_result", False,
                f"review.result={review_data.get('result')!r} != 'pass'",
            )
        session = _read_session(target)
        if session.get("state") != "completed":
            return ScenarioResult(
                "runner_routes_external_result", False,
                f"final state={session.get('state')!r} != 'completed'",
            )
        return ScenarioResult(
            "runner_routes_external_result", True,
            "echo_runner 가 BaseRunnerAdapter 경로로 utterance 합성 + reviews 작성 + cycle closure",
        )
    finally:
        shutil.rmtree(test_domain_dir, ignore_errors=True)


def _scenario_two_cycle_feedback_terminates(sandbox: Path) -> ScenarioResult:
    """자율 피드백 루프 종단 검증: cycle 1 needs_iteration → cycle 2 planner 가
    이전 cycle 의 verifier suggested_actions / orchestrator unresolved_items 를
    previous_reviews 로 받아서 cycle 2 가 complete_cycle 로 닫힌다.

    `needs_iteration_then_success` 는 state 전이만 검증. 이 시나리오는 한 단계
    더 들어가서 "두 번째 cycle 의 planner 가 실제로 이전 결과를 context 로
    봤는지" 를 ScriptedAdapter.invocations 의 마지막 planner context 로 직접
    검증한다. D13 v3 unity cycle 2 → 3 자율 추적 작동 관측을 회귀로 못박음.
    """
    target = sandbox / "two-cycle-feedback"
    _init_project(target)
    plan = [
        {
            "functional": 0.4,
            "human": 0.4,
            "result": "needs_iteration",
            # ScriptedAdapter 의 verifier 가 출력하는 suggested_actions 를 변경 못 하므로
            # 기본 빈 리스트지만, orchestrator 가 derived needs_iteration → unresolved_items
            # 가 default 로 만들어진다. previous_reviews.orchestrator 만 검증해도 충분.
        },
        {"functional": 0.9, "human": 0.9, "result": "pass"},
    ]
    adapters = _install_scripted_adapters(plan)
    if _run_cycle(target) != 0:
        return ScenarioResult("two_cycle_feedback_terminates", False, "cycle1 rc!=0")
    if _run_cycle(target) != 0:
        return ScenarioResult("two_cycle_feedback_terminates", False, "cycle2 rc!=0")
    s2 = _read_session(target)
    if s2.get("state") != "completed":
        return ScenarioResult(
            "two_cycle_feedback_terminates", False,
            f"cycle2 state={s2.get('state')} != 'completed'",
        )
    # Cycle 2 의 planner invocation 에서 previous_reviews 키가 context 에 있는지 검증.
    planner_invocations = adapters["planner"].invocations
    if len(planner_invocations) != 2:
        return ScenarioResult(
            "two_cycle_feedback_terminates", False,
            f"planner invoked {len(planner_invocations)} times, expected 2 (one per cycle)",
        )
    cycle2_planner_ctx = planner_invocations[-1].context or {}
    previous_reviews = cycle2_planner_ctx.get("previous_reviews")
    if not isinstance(previous_reviews, dict) or not previous_reviews:
        return ScenarioResult(
            "two_cycle_feedback_terminates", False,
            f"cycle2 planner.context.previous_reviews missing or empty: {previous_reviews!r}",
        )
    # 자율 피드백 루프의 세 채널 (functional / human / orchestrator) 모두 흘렀는지.
    # ScriptedAdapter verifier 가 cycle 1 에서 result="needs_iteration" 을 돌려주면
    # _collect_previous_reviews 가 functional/human 채널에 그 결과를 채운다.
    # orchestrator 는 derived needs_iteration → unresolved/recommendation.
    expected_channels = ("functional", "human", "orchestrator")
    missing = [ch for ch in expected_channels if ch not in previous_reviews]
    if missing:
        return ScenarioResult(
            "two_cycle_feedback_terminates", False,
            f"cycle2 planner.previous_reviews missing channels {missing}: keys={list(previous_reviews)}",
        )
    return ScenarioResult(
        "two_cycle_feedback_terminates", True,
        "cycle1 verdict→cycle2 planner.previous_reviews 의 functional/human/orchestrator 세 채널 + cycle2 complete_cycle 종단",
    )


def _scenario_user_stop_blocks_cycle(sandbox: Path) -> ScenarioResult:
    """사용자 stop 훅: `.orch/STOP` 파일이 발화 시작 전에 발견되면 cycle 정상 BLOCKED.
    STOP 파일은 `.orch/runtime/stops/` 로 archive 되어 다음 cycle 에 잔류하지 않음.
    user_stop_detected runtime event 도 emit.
    """
    target = sandbox / "user-stop"
    _init_project(target)
    # planner 발화 전에 STOP 신호 박아 두기. 첫 발화도 들어가기 전에 잡힘.
    stop_path = target / ".orch" / "STOP"
    stop_path.write_text("smoke test stop reason", encoding="utf-8")

    plan = [{"functional": 0.95, "human": 0.95, "result": "pass"}]
    _install_scripted_adapters(plan)
    rc = _run_cycle(target)
    if rc != 2:
        return ScenarioResult("user_stop_blocks_cycle", False, f"expected rc=2, got rc={rc}")
    session = _read_session(target)
    if session.get("state") != "blocked":
        return ScenarioResult(
            "user_stop_blocks_cycle", False,
            f"state={session.get('state')!r} != 'blocked'",
        )
    if session.get("last_decision") != "user_stop":
        return ScenarioResult(
            "user_stop_blocks_cycle", False,
            f"last_decision={session.get('last_decision')!r} != 'user_stop'",
        )
    if "smoke test stop reason" not in str(session.get("last_decision_reason") or ""):
        return ScenarioResult(
            "user_stop_blocks_cycle", False,
            f"last_decision_reason missing user reason: {session.get('last_decision_reason')!r}",
        )
    if stop_path.exists():
        return ScenarioResult(
            "user_stop_blocks_cycle", False,
            "STOP file should be archived (moved out of .orch/STOP)",
        )
    archive_dir = target / ".orch" / "runtime" / "stops"
    if not archive_dir.exists() or not list(archive_dir.glob("stop_*.txt")):
        return ScenarioResult(
            "user_stop_blocks_cycle", False,
            "archive .orch/runtime/stops/stop_*.txt missing",
        )
    events_path = target / ".orch" / "runtime" / "events.jsonl"
    if not events_path.exists():
        return ScenarioResult("user_stop_blocks_cycle", False, "events.jsonl missing")
    saw_event = False
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        evt = json.loads(raw)
        if evt.get("event") == "user_stop_detected":
            saw_event = True
            break
    if not saw_event:
        return ScenarioResult(
            "user_stop_blocks_cycle", False, "user_stop_detected event not emitted"
        )
    return ScenarioResult(
        "user_stop_blocks_cycle", True,
        ".orch/STOP detected → cycle BLOCKED + reason 보존 + archive + event emit",
    )


def _scenario_user_stop_directory_does_not_loop(sandbox: Path) -> ScenarioResult:
    """code-review 후속 회귀: `.orch/STOP` 이 파일이 아니라 디렉터리인 비정상
    상황에서도 dispatch loop 가 무한 stop 루프에 빠지지 않고 1회 BLOCKED 후
    디렉터리가 정리되어 다음 cycle 진입이 가능해야 한다.
    """
    target = sandbox / "user-stop-dir"
    _init_project(target)
    stop_dir = target / ".orch" / "STOP"
    stop_dir.mkdir(parents=True, exist_ok=True)
    plan = [{"functional": 0.95, "human": 0.95, "result": "pass"}]
    _install_scripted_adapters(plan)
    rc = _run_cycle(target)
    if rc != 2:
        return ScenarioResult(
            "user_stop_directory_does_not_loop", False, f"expected rc=2, got rc={rc}"
        )
    if stop_dir.exists():
        return ScenarioResult(
            "user_stop_directory_does_not_loop", False,
            "STOP directory was not removed — would cause infinite stop loop on next cycle",
        )
    session = _read_session(target)
    reason = str(session.get("last_decision_reason") or "")
    if "directory" not in reason:
        return ScenarioResult(
            "user_stop_directory_does_not_loop", False,
            f"reason should mention directory: {reason!r}",
        )
    return ScenarioResult(
        "user_stop_directory_does_not_loop", True,
        "STOP 이 디렉터리여도 1회 BLOCKED + rmtree 후 다음 cycle 진입 가능",
    )


def _scenario_unity_batchmode_dry_run(sandbox: Path) -> ScenarioResult:
    """옵션 C 다음 stride: unity_batchmode runner 가 Unity 미설치 환경 (UNITY_EDITOR_PATH
    미설정) 에서 dry-run mode 로 진입해 인자만 빌드하고 fake pass 결과를 돌려준다.
    sandbox / CI 환경에서도 인터페이스 회귀를 보장. 박제관 PC 라이브 검증은 별도.
    """
    from adapters import base as _ab
    from uuid import uuid4 as _uuid4

    test_domain_id = f"__test_unity_dryrun_{_uuid4().hex[:8]}__"
    test_domain_dir = Path(_ab.ENGINE_ROOT) / "domains" / test_domain_id
    test_domain_dir.mkdir(parents=True, exist_ok=True)
    # roles.yaml 을 JSON 형식으로 적어 project yaml.py shim (JSON-only) 도 nested
    # runner_config 를 파싱할 수 있게 한다.
    (test_domain_dir / "roles.yaml").write_text(
        json.dumps(
            {
                "roles": [
                    {
                        "id": "verifier_unity_play",
                        "family": "verifier",
                        "default_provider": "unity_batchmode",
                        "next_speaker_default": "verifier_human",
                        "runner_config": {
                            "unity_method": "OrchSmoke.RunPlay",
                            # unity_executable 미설정 → dry-run 진입.
                        },
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    target = sandbox / "unity-dryrun"
    original_build_adapter = app._build_adapter
    # UNITY_EDITOR_PATH 환경변수가 우연히 설정돼 있으면 dry-run 진입이 안 되므로 임시 제거.
    import os as _os
    saved_env = _os.environ.pop("UNITY_EDITOR_PATH", None)
    try:
        _init_project(target)
        session_path = target / ".orch" / "runtime" / "session.json"
        session_data = json.loads(session_path.read_text(encoding="utf-8"))
        session_data["domain"] = test_domain_id
        session_path.write_text(
            json.dumps(session_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        roles_path = target / ".orch" / "config" / "roles.yaml"
        roles_data = json.loads(roles_path.read_text(encoding="utf-8"))
        roles_data.setdefault("roles", {})["verifier_unity_play"] = "unity_batchmode"
        roles_path.write_text(
            json.dumps(roles_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        plan = [
            {
                "functional": 0.95,
                "human": 0.95,
                "result": "pass",
                "utterances": {
                    "planner": {"next_speaker": "builder"},
                    "builder": {"next_speaker": "verifier_unity_play"},
                    "verifier_human": {"declare_done": True, "next_speaker": "orchestrator"},
                    "orchestrator": {"arbitration": "agree", "next_speaker": "__end__"},
                },
            }
        ]
        role_adapters: dict[str, ScriptedAdapter] = {
            "planner": ScriptedAdapter("planner", plan),
            "builder": ScriptedAdapter("builder", plan),
            "verifier_human": ScriptedAdapter("verifier_human", plan),
            "orchestrator": ScriptedAdapter("orchestrator", plan),
        }
        _install_role_adapters(role_adapters, fallback_build_adapter=original_build_adapter)

        rc = _run_cycle(target)
        if rc != 0:
            return ScenarioResult("unity_batchmode_dry_run", False, f"run_cycle rc={rc}")
        review_path = target / ".orch" / "reviews" / "verifier_unity_play_latest.json"
        if not review_path.exists():
            return ScenarioResult(
                "unity_batchmode_dry_run", False,
                "reviews/verifier_unity_play_latest.json was not written",
            )
        review_data = json.loads(review_path.read_text(encoding="utf-8"))
        if review_data.get("result") != "pass":
            return ScenarioResult(
                "unity_batchmode_dry_run", False,
                f"dry-run review.result={review_data.get('result')!r} != 'pass'",
            )
        # dry-run 로그 파일이 생성됐는지 확인.
        log_dir = target / ".orch" / "runtime" / "unity_logs"
        if not log_dir.exists() or not list(log_dir.glob("unity_*.log")):
            return ScenarioResult(
                "unity_batchmode_dry_run", False,
                "dry-run log file not written under .orch/runtime/unity_logs/",
            )
        return ScenarioResult(
            "unity_batchmode_dry_run", True,
            "unity_batchmode dry-run 이 인자 빌드 + 로그 파일 + fake pass 정상 합성",
        )
    finally:
        if saved_env is not None:
            _os.environ["UNITY_EDITOR_PATH"] = saved_env
        shutil.rmtree(test_domain_dir, ignore_errors=True)


def _scenario_runner_nonzero_exit_routes_normally(sandbox: Path) -> ScenarioResult:
    """옵션 C 1차 stride: runner 가 exit_code=1 을 돌려주면 utterance.result='fail' 로
    합성되지만 엔진은 가로채지 않고 정상 흐름 진행 (orchestrator 가 needs_iteration 결정).
    자율 피드백 루프 원칙 — runner 결과 자체로 cycle 을 강제 BLOCKED 하지 않는다.
    """
    from adapters import base as _ab
    from uuid import uuid4 as _uuid4

    test_domain_id = f"__test_runner_fail_{_uuid4().hex[:8]}__"
    test_domain_dir = Path(_ab.ENGINE_ROOT) / "domains" / test_domain_id
    test_domain_dir.mkdir(parents=True, exist_ok=True)
    (test_domain_dir / "roles.yaml").write_text(
        "roles:\n"
        "  - id: verifier_echo\n"
        "    family: verifier\n"
        "    default_provider: echo_runner\n"
        "    next_speaker_default: verifier_human\n",
        encoding="utf-8",
    )
    target = sandbox / "runner-echo-fail"
    original_build_adapter = app._build_adapter
    try:
        _init_project(target)
        session_path = target / ".orch" / "runtime" / "session.json"
        session_data = json.loads(session_path.read_text(encoding="utf-8"))
        session_data["domain"] = test_domain_id
        session_path.write_text(
            json.dumps(session_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        roles_path = target / ".orch" / "config" / "roles.yaml"
        roles_data = json.loads(roles_path.read_text(encoding="utf-8"))
        roles_data.setdefault("roles", {})["verifier_echo"] = "echo_runner"
        roles_path.write_text(
            json.dumps(roles_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 의도적으로 exit_code=1. echo_runner 를 monkey-patch 해서 exit_code=1 강제.
        # try/finally 가 patch 부터 _run_cycle 호출까지 모두 감싸서 _install_role_adapters
        # 가 예외를 내거나 plan setup 에서 실패해도 RUNNER.run 은 반드시 원복된다.
        from runners import echo_runner as _er

        original_run = _er.RUNNER.run

        def failing_run(invocation):
            from runners.base import RunnerResult
            return RunnerResult(
                exit_code=1,
                summary="echo_runner intentional fail",
                stderr_excerpt="simulated failure for smoke test",
                findings=["echo_runner returned non-zero exit code"],
            )

        _er.RUNNER.run = failing_run  # type: ignore[method-assign]
        try:
            plan = [
                {
                    "functional": 0.5,
                    "human": 0.5,
                    "result": "needs_iteration",
                    "utterances": {
                        "planner": {"next_speaker": "builder"},
                        "builder": {"next_speaker": "verifier_echo"},
                        # verifier_echo 는 runner 가 처리, next_speaker_default=verifier_human 따라감
                        "verifier_human": {"declare_done": True, "next_speaker": "orchestrator"},
                        # orchestrator 는 ScriptedAdapter 가 derived 매핑으로 needs_iteration→iterating.
                        # 사이클은 종료되되 BLOCKED 가 아니라 ITERATING.
                        "orchestrator": {"arbitration": "agree", "next_speaker": "__end__"},
                    },
                }
            ]
            role_adapters: dict[str, ScriptedAdapter] = {
                "planner": ScriptedAdapter("planner", plan),
                "builder": ScriptedAdapter("builder", plan),
                "verifier_human": ScriptedAdapter("verifier_human", plan),
                "orchestrator": ScriptedAdapter("orchestrator", plan),
            }
            _install_role_adapters(role_adapters, fallback_build_adapter=original_build_adapter)
            rc = _run_cycle(target)
        finally:
            _er.RUNNER.run = original_run  # type: ignore[method-assign]

        if rc != 0:
            return ScenarioResult(
                "runner_nonzero_exit_routes_normally", False,
                f"expected rc=0 (cycle continues despite runner fail), got rc={rc}",
            )
        review_path = target / ".orch" / "reviews" / "verifier_echo_latest.json"
        if not review_path.exists():
            return ScenarioResult(
                "runner_nonzero_exit_routes_normally", False,
                "reviews/verifier_echo_latest.json missing",
            )
        review_data = json.loads(review_path.read_text(encoding="utf-8"))
        if review_data.get("result") != "fail":
            return ScenarioResult(
                "runner_nonzero_exit_routes_normally", False,
                f"review.result={review_data.get('result')!r} != 'fail'",
            )
        if not review_data.get("blocking_issues"):
            return ScenarioResult(
                "runner_nonzero_exit_routes_normally", False,
                "blocking_issues missing despite non-zero exit",
            )
        session = _read_session(target)
        # ScriptedAdapter orchestrator 가 needs_iteration→iterating 으로 매핑.
        # cycle 정상 closure 이므로 state 는 iterating.
        if session.get("state") != "iterating":
            return ScenarioResult(
                "runner_nonzero_exit_routes_normally", False,
                f"final state={session.get('state')!r} != 'iterating' (engine should not block)",
            )
        return ScenarioResult(
            "runner_nonzero_exit_routes_normally", True,
            "runner exit_code=1 → result=fail 합성, 엔진은 가로채지 않고 cycle 정상 진행",
        )
    finally:
        shutil.rmtree(test_domain_dir, ignore_errors=True)


SCENARIOS: dict[str, Callable[[Path], ScenarioResult]] = {
    "complete_on_first_cycle": _scenario_complete_on_first_cycle,
    "needs_iteration_then_success": _scenario_needs_iteration_then_success,
    "handoff_mode_pauses_cycle": _scenario_handoff_mode_pauses_cycle,
    "handoff_feedback_reaches_planner": _scenario_handoff_feedback_reaches_planner,
    "terminal_handoff_clears_feedback": _scenario_terminal_handoff_clears_feedback,
    "utterance_next_speaker_skips_legacy_chain": _scenario_utterance_next_speaker_skips_legacy_chain,
    "declare_done_forces_orchestrator": _scenario_declare_done_forces_orchestrator,
    "max_utterances_blocks_session": _scenario_max_utterances_blocks_session,
    "orchestrator_disagree_resumes_cycle": _scenario_orchestrator_disagree_resumes_cycle,
    "orchestrator_disagree_to_end_blocks_cycle": _scenario_orchestrator_disagree_to_end_blocks_cycle,
    "orchestrator_disagree_routes_to_handoff": _scenario_orchestrator_disagree_routes_to_handoff,
    "consecutive_disagrees_warns_but_continues": _scenario_consecutive_disagrees_warns_but_continues,
    "orchestrator_disagree_empty_next_blocks_cycle": _scenario_orchestrator_disagree_empty_next_blocks_cycle,
    "custom_verifier_routing": _scenario_custom_verifier_routing,
    "runner_routes_external_result": _scenario_runner_routes_external_result,
    "runner_nonzero_exit_routes_normally": _scenario_runner_nonzero_exit_routes_normally,
    "unity_batchmode_dry_run": _scenario_unity_batchmode_dry_run,
    "user_stop_blocks_cycle": _scenario_user_stop_blocks_cycle,
    "two_cycle_feedback_terminates": _scenario_two_cycle_feedback_terminates,
    "user_stop_directory_does_not_loop": _scenario_user_stop_directory_does_not_loop,
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
