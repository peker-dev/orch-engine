from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
from math import ceil
from pathlib import Path
from typing import Callable

import yaml

from adapters.base import AdapterExecutionError, AdapterFatalError, BaseAdapter, Invocation
from adapters.claude_cli import ClaudeCliAdapter
from adapters.codex_cli import CodexCliAdapter
from core.artifact_store import ArtifactStore
from core.dispatcher import Dispatcher
from core.handoff_manager import (
    HandoffError,
    HandoffManager,
    HandoffRequest,
    SUPPORTED_MODES,
)
from core.policy_loader import PolicyLoader
from core.runtime_store import RuntimeStore
from core.state_machine import EngineState, RESUMABLE_STATES


ENGINE_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_ROOT = ENGINE_ROOT / "templates" / "target_project" / ".orch"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="orch-engine scaffold entry")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="bootstrap a target project")
    init_parser.add_argument("--target", required=True, help="Target project path")
    init_parser.add_argument("--domain", default="web", help="Domain id")
    init_parser.add_argument("--mode", default="greenfield", help="Run mode")
    init_parser.add_argument("--project-name", default="", help="Optional project name")
    init_parser.add_argument("--goal-summary", default="", help="Optional initial goal")
    init_parser.set_defaults(func=run_init)

    status_parser = subparsers.add_parser("status", help="show target runtime status")
    status_parser.add_argument("--target", required=True, help="Target project path")
    status_parser.set_defaults(func=run_status)

    cycle_parser = subparsers.add_parser("run-cycle", help="execute one orchestration cycle")
    cycle_parser.add_argument("--target", required=True, help="Target project path")
    cycle_parser.add_argument("--goal-summary", default="", help="Optional goal override")
    cycle_parser.set_defaults(func=run_cycle)

    handoff_request_parser = subparsers.add_parser(
        "handoff-request",
        help="Open a Codex App handoff (pauses automatic writes and writes a request)",
    )
    handoff_request_parser.add_argument("--target", required=True, help="Target project path")
    handoff_request_parser.add_argument(
        "--mode",
        required=True,
        choices=sorted(SUPPORTED_MODES),
        help="Handoff mode per codex-app-handoff-protocol.md",
    )
    handoff_request_parser.add_argument("--reason", required=True, help="Why the handoff is needed")
    handoff_request_parser.add_argument(
        "--what-needs-decision",
        required=True,
        help="Exact judgment or action expected from the external tool",
    )
    handoff_request_parser.add_argument(
        "--allowed-edit-scope",
        action="append",
        default=[],
        help="Repeatable. Paths the external tool may edit (relative to target).",
    )
    handoff_request_parser.add_argument(
        "--goal-override",
        default="",
        help="Override the stored project goal for this handoff request.",
    )
    handoff_request_parser.set_defaults(func=run_handoff_request)

    handoff_status_parser = subparsers.add_parser(
        "handoff-status",
        help="Print the current Codex App handoff lifecycle state",
    )
    handoff_status_parser.add_argument("--target", required=True, help="Target project path")
    handoff_status_parser.set_defaults(func=run_handoff_status)

    handoff_ingest_parser = subparsers.add_parser(
        "handoff-ingest",
        help="Read and validate response.yaml, then archive the current handoff",
    )
    handoff_ingest_parser.add_argument("--target", required=True, help="Target project path")
    handoff_ingest_parser.set_defaults(func=run_handoff_ingest)

    handoff_cancel_parser = subparsers.add_parser(
        "handoff-cancel",
        help="Abort an active handoff without ingesting a response",
    )
    handoff_cancel_parser.add_argument("--target", required=True, help="Target project path")
    handoff_cancel_parser.add_argument("--reason", default="user cancelled", help="Optional note")
    handoff_cancel_parser.set_defaults(func=run_handoff_cancel)

    return parser.parse_args()


def run_init(args: argparse.Namespace) -> int:
    target_root = Path(args.target).resolve()
    orch_root = target_root / ".orch"
    if orch_root.exists():
        print(f".orch 폴더가 이미 존재합니다: {orch_root}")
        return 1

    target_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_ROOT, orch_root)

    project_name = args.project_name or target_root.name
    _write_yaml(
        orch_root / "config" / "project.yaml",
        {
            "project": {
                "id": target_root.name,
                "name": project_name,
                "target_path": str(target_root),
                "mode": args.mode,
                "domain": args.domain,
                "goal_summary": args.goal_summary,
            }
        },
    )
    _write_yaml(
        orch_root / "config" / "domain.yaml",
        {"domain": {"selected": args.domain, "calibrated": False}},
    )

    runtime = RuntimeStore(target_root)
    runtime.write_json(
        "runtime/session.json",
        {
            "state": "idle",
            "cycle": 0,
            "mode": args.mode,
            "domain": args.domain,
            "target_path": str(target_root),
            "active_role": None,
        },
    )
    runtime.append_event(
        "project_initialized",
        {"domain": args.domain, "mode": args.mode, "target_path": str(target_root)},
    )

    print(f"오케스트레이션 스캐폴드 생성 완료: {orch_root}")
    return 0


def run_status(args: argparse.Namespace) -> int:
    target_root = Path(args.target).resolve()
    runtime = RuntimeStore(target_root)
    session = runtime.read_json("runtime/session.json", {})
    queue = runtime.read_json("runtime/queue.json", {})
    preflight = runtime.read_json("runtime/preflight.json", {})
    print("세션:")
    print(session)
    print("큐:")
    print(queue)
    if preflight:
        print("토큰 preflight:")
        print(preflight)
    return 0


def run_cycle(args: argparse.Namespace) -> int:
    target_root = Path(args.target).resolve()
    orch_root = target_root / ".orch"
    if not orch_root.exists():
        print(f".orch 폴더가 없습니다: {orch_root}")
        return 1

    runtime = RuntimeStore(target_root)
    artifacts = ArtifactStore(target_root)
    dispatcher = Dispatcher()
    policy_loader = PolicyLoader(ENGINE_ROOT)

    project_config = _read_yaml(orch_root / "config" / "project.yaml")
    roles_config = _read_yaml(orch_root / "config" / "roles.yaml").get("roles", {})
    limits_config = _read_yaml(orch_root / "config" / "limits.yaml").get("limits", {})
    workflow_config = _read_yaml(orch_root / "config" / "workflow.yaml").get("workflow", {})
    common_policy = policy_loader.load_common().get("defaults", {})
    session = runtime.read_json("runtime/session.json", {})

    current_state = session.get("state", EngineState.IDLE.value)
    if current_state not in RESUMABLE_STATES:
        print(f"현재 상태({current_state})에서는 새 사이클을 시작할 수 없습니다.")
        return 1

    # `codex_app`은 handoff 전용 라벨이므로 `human_review_mode: handoff`와 세트로만
    # 쓸 수 있다. 둘이 어긋난 구성에서 cycle을 돌리면 verifier_human 단계에서
    # adapter를 빌드할 수 없어 실패할 것이 확실하므로, 런처나 사용자가 원인을
    # 바로 파악할 수 있도록 시작 단계에서 명확한 메시지로 거부한다.
    vh_role = str(roles_config.get("verifier_human", "codex_cli"))
    if vh_role == "codex_app" and _human_review_mode(workflow_config) != "handoff":
        print(
            "설정 충돌: roles.yaml 의 `verifier_human: codex_app` 은 "
            "`workflow.yaml` 의 `human_review_mode: handoff` 와 세트로만 사용할 수 있습니다.\n"
            "해결: 다음 둘 중 하나를 선택하세요.\n"
            "  (A) workflow.yaml 에서 `human_review_mode: handoff` 로 변경 "
            "— 사람이 Codex App 등으로 파일 기반 리뷰를 하는 흐름\n"
            "  (B) roles.yaml 에서 `verifier_human: codex_cli` 로 변경 "
            "— Codex CLI 가 자동으로 human review 단계까지 수행하는 흐름"
        )
        return 1

    project_goal = args.goal_summary or project_config.get("project", {}).get("goal_summary", "")
    if not project_goal.strip():
        print("사이클 실행에는 목표(goal summary)가 필요합니다.")
        return 1

    if args.goal_summary:
        project_config.setdefault("project", {})["goal_summary"] = args.goal_summary
        _write_yaml(orch_root / "config" / "project.yaml", project_config)

    preflight = _run_token_preflight(project_goal, roles_config, limits_config)
    runtime.write_json("runtime/preflight.json", preflight)
    runtime.append_event("token_preflight", preflight)
    if preflight["status"] == "block":
        runtime.write_json(
            "runtime/session.json",
            {
                **session,
                "state": EngineState.BLOCKED.value,
                "active_role": None,
                "last_preflight_status": "block",
            },
        )
        print("토큰 preflight가 이 사이클을 차단했습니다 (예산 초과).")
        return 2

    cycle_index = int(session.get("cycle", 0)) + 1
    previous_state = session.get("state", EngineState.IDLE.value)
    # iterating 사이클에서 planner/builder 가 "기존 산출물이 있다"는 걸 인지하도록
    # artifact index 를 한 번 수집해 양쪽 context 에 동일 목록을 전달한다.
    existing_artifacts = _collect_existing_artifacts(target_root, runtime)
    print(f"\n=== 사이클 {cycle_index} 시작 (이전 상태={previous_state}) ===", flush=True)
    print(f"목표: {project_goal}", flush=True)
    if existing_artifacts and previous_state == EngineState.ITERATING.value:
        paths_preview = ", ".join(a["path"] for a in existing_artifacts[:3])
        extra = f" 외 {len(existing_artifacts) - 3}건" if len(existing_artifacts) > 3 else ""
        print(f"기존 산출물 {len(existing_artifacts)}건 증분 수정 대상: {paths_preview}{extra}", flush=True)
    planner_adapter = str(roles_config.get("planner", "claude_cli"))
    step_ctx = _log_step_start(cycle_index, 1, 4, "planner", planner_adapter)
    try:
        task = _run_planner(
            target_root=target_root,
            runtime=runtime,
            artifacts=artifacts,
            roles_config=roles_config,
            project_goal=project_goal,
            cycle_index=cycle_index,
            previous_state=previous_state,
            existing_artifacts=existing_artifacts,
        )
    except (AdapterFatalError, AdapterExecutionError) as exc:
        _log_step_end(
            cycle_index, 1, 4, "planner", planner_adapter, step_ctx,
            detail=f"실패: {type(exc).__name__}",
        )
        _finalize_adapter_failure(target_root, runtime, cycle_index, exc)
        print(f"사이클 {cycle_index} 중단 (adapter 실패): {exc}")
        return 2
    _log_step_end(
        cycle_index, 1, 4, "planner", planner_adapter, step_ctx,
        detail=f"task={(task or {}).get('title', 'none')}",
    )
    if task is None:
        runtime.write_json(
            "runtime/session.json",
            {
                **runtime.read_json("runtime/session.json", {}),
                "state": EngineState.BLOCKED.value,
                "active_role": None,
                "cycle": cycle_index,
                "last_decision": "no_work",
                "last_decision_reason": "planner returned no tasks and backlog is empty",
            },
        )
        runtime.append_event("cycle_blocked_no_work", {"cycle": cycle_index})
        print(f"사이클 {cycle_index} 중단: planner가 실행할 task를 반환하지 않았습니다.")
        return 2
    builder_adapter = str(roles_config.get("builder", "claude_cli"))
    vf_adapter = str(roles_config.get("verifier_functional", "codex_cli"))
    vh_adapter = str(roles_config.get("verifier_human", "codex_app"))
    try:
        step_ctx = _log_step_start(cycle_index, 2, 4, "builder", builder_adapter)
        _run_builder(
            target_root=target_root,
            runtime=runtime,
            artifacts=artifacts,
            roles_config=roles_config,
            task=task,
            cycle_index=cycle_index,
            existing_artifacts=existing_artifacts,
        )
        _log_step_end(cycle_index, 2, 4, "builder", builder_adapter, step_ctx)
        step_ctx = _log_step_start(cycle_index, 3, 4, "verifier_functional", vf_adapter)
        functional_review = _run_verifier(
            target_root=target_root,
            runtime=runtime,
            artifacts=artifacts,
            roles_config=roles_config,
            task=task,
            role="verifier_functional",
            state=EngineState.VERIFYING_FUNCTIONAL,
            cycle_index=cycle_index,
        )
        _log_step_end(
            cycle_index, 3, 4, "verifier_functional", vf_adapter, step_ctx,
            detail=(
                f"result={functional_review.get('result')} "
                f"score={_fmt_score(functional_review.get('score'))}"
            ),
        )
        if _human_review_mode(workflow_config) == "handoff":
            step_ctx = _log_step_start(cycle_index, 4, 4, "verifier_human", "handoff")
            handoff_status = _pause_for_human_handoff(
                target_root=target_root,
                runtime=runtime,
                orch_root=orch_root,
                project_config=project_config,
                task=task,
                cycle_index=cycle_index,
                functional_review=functional_review,
            )
            _log_step_end(
                cycle_index, 4, 4, "verifier_human", "handoff", step_ctx,
                detail=f"일시정지, handoff_id={handoff_status.handoff_id}",
            )
            print(
                f"사이클 {cycle_index} 일시정지 (handoff id={handoff_status.handoff_id}). "
                f".orch/handoff/response.yaml을 채운 뒤 "
                f"`python -m core.app handoff-ingest --target {target_root}` 실행."
            )
            # 스크립트가 `사이클 .* (완료|일시정지)` 로 grep할 수 있도록
            # 완료 마커와 대칭되는 한 줄을 일시정지 분기에도 출력한다.
            print(
                f"=== 사이클 {cycle_index} 일시정지: "
                f"결정=handoff_requested 다음 상태={EngineState.HANDOFF_ACTIVE.value} ===",
                flush=True,
            )
            return 0
        step_ctx = _log_step_start(cycle_index, 4, 4, "verifier_human", vh_adapter)
        human_review = _run_verifier(
            target_root=target_root,
            runtime=runtime,
            artifacts=artifacts,
            roles_config=roles_config,
            task=task,
            role="verifier_human",
            state=EngineState.VERIFYING_HUMAN,
            cycle_index=cycle_index,
        )
        _log_step_end(
            cycle_index, 4, 4, "verifier_human", vh_adapter, step_ctx,
            detail=(
                f"result={human_review.get('result')} "
                f"score={_fmt_score(human_review.get('score'))}"
            ),
        )
    except (AdapterFatalError, AdapterExecutionError) as exc:
        _finalize_adapter_failure(target_root, runtime, cycle_index, exc)
        print(f"사이클 {cycle_index} 중단 (adapter 실패): {exc}")
        return 2

    resolved_limits = _resolve_iteration_limits(limits_config, common_policy)
    refreshed_session = runtime.read_json("runtime/session.json", {})
    score_history = _score_history(refreshed_session)
    handoff_pause_count = int(refreshed_session.get("handoff_pause_count", 0) or 0)
    base_decision = _orchestrator_decision(common_policy, functional_review, human_review)
    decision = _apply_iteration_policy(
        base_decision,
        cycle_index=cycle_index,
        score_history=score_history,
        limits=resolved_limits,
        functional_review=functional_review,
        human_review=human_review,
        handoff_pause_count=handoff_pause_count,
    )
    _finalize_cycle(
        target_root=target_root,
        runtime=runtime,
        task=task,
        decision=decision,
        cycle_index=cycle_index,
        dispatcher=dispatcher,
        functional_review=functional_review,
        human_review=human_review,
    )

    print(
        f"=== 사이클 {cycle_index} 완료: 결정={decision['decision']} "
        f"다음 상태={decision['next_state']} ===",
        flush=True,
    )
    return 0


def _write_yaml(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_adapter(adapter_name: str) -> BaseAdapter:
    if adapter_name == "claude_cli":
        return ClaudeCliAdapter()
    if adapter_name == "codex_cli":
        return CodexCliAdapter()
    if adapter_name == "codex_app":
        # `codex_app`은 handoff 전용 라벨 — 실제 adapter 호출 경로가 없다.
        # cli 모드에서 `verifier_human = codex_app`인 조합은 `run_cycle` 시작
        # 단계의 정합성 검사에서 차단되며, handoff 모드에서는 adapter를
        # 아예 빌드하지 않고 `_pause_for_human_handoff`로 빠진다.
        # 여기에 도달했다는 건 정합성 검사 누락 버그이므로 명확히 실패시킨다.
        raise ValueError(
            "`codex_app` adapter는 handoff 전용 라벨이라 직접 호출할 수 없습니다. "
            "`workflow.yaml`의 `human_review_mode: handoff`와 함께 사용하거나, "
            "`roles.yaml`의 `verifier_human`을 `codex_cli`로 바꾸세요."
        )
    raise ValueError(f"Unsupported adapter: {adapter_name}")


def _run_token_preflight(
    project_goal: str,
    roles_config: dict[str, str],
    limits_config: dict[str, object],
) -> dict[str, object]:
    token_settings = limits_config.get("token_preflight", {})
    round_budget = int(token_settings.get("round_budget_tokens", 8000))
    warn_ratio = float(token_settings.get("warn_at_ratio", 0.75))
    output_reserve = int(token_settings.get("output_reserve_per_role", 400))
    active_roles = [role for role in roles_config if role in {"planner", "builder", "verifier_functional", "verifier_human"}]
    input_estimate = ceil(len(project_goal) / 4) + (len(active_roles) * 180)
    output_estimate = len(active_roles) * output_reserve
    total_estimate = input_estimate + output_estimate
    usage_ratio = total_estimate / round_budget if round_budget else 0.0
    if usage_ratio >= 1.0:
        status = "block"
    elif usage_ratio >= warn_ratio:
        status = "warn"
    else:
        status = "allow"
    return {
        "status": status,
        "estimated_input_tokens": input_estimate,
        "estimated_output_tokens": output_estimate,
        "estimated_total_tokens": total_estimate,
        "round_budget_tokens": round_budget,
        "active_roles": active_roles,
    }


def _log_step_start(
    cycle_index: int, index: int, total: int, role: str, adapter: str
) -> tuple[float, Callable[[], None]]:
    """'단계 시작' 마커 한 줄을 출력하고, 시작 시각 + heartbeat 종료 콜백을 돌려준다.

    반환된 두 값을 `_log_step_end`로 넘기면 heartbeat 스레드가 정리되고
    경과 시간이 함께 출력된다. heartbeat 덕분에 사용자는 adapter subprocess가
    오래 걸려도 런처/콘솔이 멈춘 게 아니라는 걸 알 수 있다.
    """
    print(
        f"[사이클 {cycle_index}] [{index}/{total}] {role} ({adapter}) ... 시작",
        flush=True,
    )
    stop_hb = _start_heartbeat(
        f"  [사이클 {cycle_index}] [{index}/{total}] {role} 진행 중"
    )
    return time.perf_counter(), stop_hb


def _log_step_end(
    cycle_index: int,
    index: int,
    total: int,
    role: str,
    adapter: str,
    ctx: tuple[float, Callable[[], None]],
    detail: str = "",
) -> None:
    start_ts, stop_hb = ctx
    stop_hb()
    elapsed = time.perf_counter() - start_ts
    suffix = f" | {detail}" if detail else ""
    print(
        f"[사이클 {cycle_index}] [{index}/{total}] {role} ({adapter}) "
        f"완료 {elapsed:.1f}s{suffix}",
        flush=True,
    )


def _start_heartbeat(label: str, interval: float = 1.0) -> Callable[[], None]:
    """별도 스레드에서 같은 줄에 점과 경과 시간을 덮어써 출력한다.

    동일 라인을 `\\r`로 덮어쓰므로 스크롤이 늘지 않는다. 스레드는 daemon으로
    띄우고, 반환된 stop 콜백이 호출되면 이벤트로 깨워 즉시 종료하고
    heartbeat 라인을 공백으로 지운다. 테스트처럼 스텝이 1초 안에 끝나면
    heartbeat 라인은 한 번도 출력되지 않는다.

    파이프/리다이렉션 환경(비 TTY)에서는 `\\r` 덮어쓰기가 의미 없어 줄마다
    찌꺼기만 쌓이므로 heartbeat 자체를 건너뛴다. 대신 완료 시 찍히는
    `완료 X.Xs` 라인으로도 진행 상황은 그대로 추적 가능하다.
    """
    try:
        if not sys.stdout.isatty():
            return lambda: None
    except (AttributeError, ValueError):
        return lambda: None
    stop_event = threading.Event()
    # cp949 콘솔에서 한글 bar가 깨지면 점(.)만으로도 의도 전달되므로
    # ASCII 점 마커만 사용한다. 한 줄 너비는 60자 내외.
    def _run() -> None:
        start = time.perf_counter()
        dots = 0
        while not stop_event.wait(interval):
            dots += 1
            elapsed = time.perf_counter() - start
            bar = "." * ((dots % 20) + 1)
            try:
                sys.stdout.write(f"\r{label} {bar:<21s} ({elapsed:.0f}s)")
                sys.stdout.flush()
            except (OSError, ValueError):
                # stdout이 닫혔거나 리디렉션된 환경에서는 조용히 종료.
                return

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    def _stop() -> None:
        stop_event.set()
        thread.join(timeout=1.5)
        try:
            # heartbeat 라인을 공백으로 덮어쓴 뒤 커서를 줄 맨 앞으로.
            sys.stdout.write("\r" + " " * (len(label) + 32) + "\r")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    return _stop


def _fmt_score(value: object) -> str:
    """로그용 점수 포맷터. None/비숫자도 안전하게 처리."""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value) if value is not None else "n/a"


def _finalize_adapter_failure(
    target_root: Path,
    runtime: RuntimeStore,
    cycle_index: int,
    exc: AdapterExecutionError | AdapterFatalError,
) -> None:
    session = runtime.read_json("runtime/session.json", {})
    failed_role = str(session.get("active_role") or "")
    error_class = type(exc).__name__
    artifact_path = _latest_adapter_artifact_path(target_root, failed_role)
    payload = {
        "cycle": cycle_index,
        "role": failed_role,
        "error_class": error_class,
        "message": str(exc),
        "fatal": isinstance(exc, AdapterFatalError),
        "artifact_path": artifact_path,
    }
    runtime.write_json(
        "runtime/session.json",
        {
            **session,
            "state": EngineState.BLOCKED.value,
            "active_role": None,
            "cycle": cycle_index,
            "last_decision": "adapter_error",
            "last_decision_reason": str(exc),
            "last_error_class": error_class,
            "last_error_role": failed_role,
            "last_error_artifact_path": artifact_path,
        },
    )
    runtime.append_event("adapter_failed", payload)


def _latest_adapter_artifact_path(target_root: Path, role: str) -> str:
    runs_root = target_root / ".orch" / "runtime" / "adapter_runs"
    if not runs_root.exists():
        return ""
    candidates = [path for path in runs_root.iterdir() if path.is_dir()]
    if role:
        role_candidates = [path for path in candidates if f"-{role}-" in path.name]
        if role_candidates:
            candidates = role_candidates
    if not candidates:
        return ""
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    return str(latest.relative_to(target_root))


def _run_planner(
    *,
    target_root: Path,
    runtime: RuntimeStore,
    artifacts: ArtifactStore,
    roles_config: dict[str, str],
    project_goal: str,
    cycle_index: int,
    previous_state: str = EngineState.IDLE.value,
    existing_artifacts: list[dict[str, str]] | None = None,
) -> dict[str, object] | None:
    runtime.write_json(
        "runtime/session.json",
        {
            **runtime.read_json("runtime/session.json", {}),
            "state": EngineState.PLANNING.value,
            "active_role": "planner",
            "cycle": cycle_index,
        },
    )
    planner_context: dict[str, object] = {"cycle": cycle_index, "previous_state": previous_state}
    existing_artifacts = existing_artifacts or []
    if previous_state == EngineState.ITERATING.value:
        prior = _collect_previous_reviews(runtime)
        if prior:
            planner_context["previous_reviews"] = prior
        if existing_artifacts:
            planner_context["existing_artifacts"] = existing_artifacts
        # iteration_hint는 existing_artifacts 유무 × handoff 유무 조합으로 구성한다.
        # 핵심 메시지: "파일이 이미 존재하므로 신규 작성이 아닌 증분 수정 task를 뽑아라".
        artifact_names = (
            ", ".join(a.get("path", "") for a in existing_artifacts[:5])
            if existing_artifacts
            else ""
        )
        increment_directive = (
            f"기존 산출물이 디스크에 이미 존재합니다: {artifact_names}. "
            "이 파일들을 처음부터 다시 쓰지 말고, 이전 리뷰에서 지적된 부분만 증분으로 "
            "수정하는 task를 뽑아주세요. task title은 `<파일명> 증분 수정` 또는 "
            "`<파일명> 부분 개선` 형태로, task action에는 어느 섹션을 어떻게 바꿀지 "
            "구체적으로 명시해야 합니다."
            if existing_artifacts
            else ""
        )
        if prior and "handoff" in prior and existing_artifacts:
            planner_context["iteration_hint"] = (
                "이 사이클은 Codex App handoff 이후 재개되는 이터레이션입니다. "
                "previous_reviews.handoff 블록이 최상위 판단이고, findings / "
                "recommended_next_action 을 우선 반영하세요. "
                + increment_directive
            )
        elif prior and "handoff" in prior:
            planner_context["iteration_hint"] = (
                "이 사이클은 Codex App handoff 이후 재개되는 이터레이션입니다. "
                "previous_reviews.handoff 블록이 최상위 판단이고, findings / "
                "recommended_next_action 을 우선 반영하세요."
            )
        elif existing_artifacts:
            planner_context["iteration_hint"] = (
                "이 사이클은 직전 리뷰가 통과하지 못해 재시도하는 이터레이션입니다. "
                + increment_directive
            )
        elif prior:
            planner_context["iteration_hint"] = (
                "이 사이클은 직전 리뷰가 통과하지 못해 재시도하는 이터레이션입니다. "
                "previous_reviews의 findings를 반영해 다음 task를 다듬어주세요 — "
                "직전과 동일한 task를 그대로 반복하지 마세요."
            )
    adapter = _build_adapter(roles_config.get("planner", "claude_cli"))
    result = adapter.invoke(
        Invocation(
            role="planner",
            objective=project_goal,
            working_directory=str(target_root),
            context=planner_context,
        )
    )
    payload = result.payload or {}
    tasks = payload.get("tasks", [])
    completed = runtime.read_json("tasks/completed.json", [])
    completed_keys = _task_keys(completed)
    existing_backlog = runtime.read_json("tasks/backlog.json", [])
    # Carry forward the still-open backlog, drop anything already completed,
    # then append only new tasks that are neither already queued nor done.
    backlog = _dedupe_tasks(
        [task for task in existing_backlog if _task_key(task) not in completed_keys]
    )
    backlog_keys = _task_keys(backlog)
    for task in tasks:
        key = _task_key(task)
        if not key or key in backlog_keys or key in completed_keys:
            continue
        backlog.append(task)
        backlog_keys.add(key)
    runtime.write_json("tasks/backlog.json", backlog)
    active_task = backlog[0] if backlog else (tasks[0] if tasks else None)
    active_list = [active_task] if active_task else []
    runtime.write_json("tasks/active.json", active_list)
    runtime.write_json(
        "runtime/queue.json",
        {"backlog": backlog, "active": active_list, "completed": completed},
    )
    plan_summary = payload.get("plan_summary", result.summary)
    first_title = active_task.get("title") if isinstance(active_task, dict) else "None"
    _write_text(
        target_root / ".orch" / "reports" / "current-plan.md",
        f"# Current Plan\n\n- Goal: {project_goal}\n- Summary: {plan_summary}\n- First task: {first_title}\n",
    )
    artifacts.register("plan", ".orch/reports/current-plan.md", str(plan_summary))
    runtime.append_event(
        "planner_completed",
        {
            "cycle": cycle_index,
            "summary": result.summary,
            "backlog_size": len(backlog),
            "new_task_ids": [_task_key(t) for t in tasks if _task_key(t)],
        },
    )
    if not active_task:
        runtime.append_event(
            "planner_returned_no_tasks",
            {"cycle": cycle_index, "summary": result.summary},
        )
        return None
    return active_task


def _collect_previous_reviews(runtime: RuntimeStore) -> dict[str, object]:
    """Build a compact summary of the most recent functional/human/handoff reviews.

    Returns an empty dict when no real review has been written yet (template
    placeholders use `result == "not_run"`).

    `reviews/handoff_latest.json` is written by `handoff-ingest` and cleared
    whenever a cycle completes, so its presence implies the handoff feedback is
    still pending consumption by the next planner run.
    """
    summary: dict[str, object] = {}
    for role, path in (
        ("functional", "reviews/functional_latest.json"),
        ("human", "reviews/human_latest.json"),
    ):
        review = runtime.read_json(path, {})
        if not isinstance(review, dict):
            continue
        if review.get("result") in (None, "", "not_run"):
            continue
        summary[role] = {
            "result": review.get("result"),
            "score": review.get("score"),
            "summary": review.get("summary", ""),
            "findings": review.get("findings", []),
            "suggested_actions": review.get("suggested_actions", []),
        }
    handoff_review = runtime.read_json("reviews/handoff_latest.json", {})
    if isinstance(handoff_review, dict) and handoff_review.get("result"):
        summary["handoff"] = {
            "result": handoff_review.get("result"),
            "summary": handoff_review.get("summary", ""),
            "findings": handoff_review.get("findings", []),
            "recommended_next_action": handoff_review.get("recommended_next_action", ""),
            "remaining_risks": handoff_review.get("remaining_risks", []),
            "decision_note": handoff_review.get("decision_note", ""),
        }
    return summary


def _run_builder(
    *,
    target_root: Path,
    runtime: RuntimeStore,
    artifacts: ArtifactStore,
    roles_config: dict[str, str],
    task: dict[str, object],
    cycle_index: int,
    existing_artifacts: list[dict[str, str]] | None = None,
) -> None:
    runtime.write_json(
        "runtime/session.json",
        {
            **runtime.read_json("runtime/session.json", {}),
            "state": EngineState.BUILDING.value,
            "active_role": "builder",
        },
    )
    adapter = _build_adapter(roles_config.get("builder", "claude_cli"))
    builder_context: dict[str, object] = {"task": task, "cycle": cycle_index}
    if existing_artifacts:
        # 이미 존재하는 산출물 파일 목록을 builder 에게 명시적으로 전달해
        # "파일을 처음부터 다시 쓰지 말고 부분 수정하라" 는 지침을 함께 준다.
        # 이는 매 사이클마다 builder 가 전체 파일을 재작성해 시간/토큰을 낭비하던
        # 경향을 억제하기 위한 장치다.
        builder_context["existing_artifacts"] = existing_artifacts
        builder_context["change_directive"] = (
            "existing_artifacts 에 나열된 파일들은 디스크에 이미 존재합니다. "
            "먼저 Read 도구로 각 파일의 현재 내용을 읽고, 이전 리뷰에서 지적된 부분만 "
            "Edit 도구로 최소 수정하세요. 파일 전체를 새로 Write 하거나, 영향 없는 섹션을 "
            "재작성하지 마세요. 변경하지 않은 부분은 기존 내용을 그대로 유지해야 합니다."
        )
    result = adapter.invoke(
        Invocation(
            role="builder",
            objective=str(task.get("title", "")),
            working_directory=str(target_root),
            context=builder_context,
        )
    )
    payload = result.payload or {}
    raw_files = payload.get("files_changed")
    files_changed = [str(item) for item in raw_files] if isinstance(raw_files, list) else []
    raw_artifacts = payload.get("artifact_paths")
    artifact_paths = [str(item) for item in raw_artifacts] if isinstance(raw_artifacts, list) else []
    self_check = payload.get("self_check") if isinstance(payload.get("self_check"), dict) else {}
    raw_unresolved = self_check.get("unresolved")
    unresolved = [str(item) for item in raw_unresolved] if isinstance(raw_unresolved, list) else []

    snapshot_path = target_root / ".orch" / "artifacts" / "snapshots" / f"cycle-{cycle_index:03d}-builder.md"
    _write_text(
        snapshot_path,
        (
            "# Builder Output\n\n"
            f"- Task: {task.get('title', '')}\n"
            f"- Summary: {payload.get('change_summary', result.summary)}\n"
            f"- Files changed: {', '.join(files_changed) if files_changed else '(none reported)'}\n"
            f"- Artifact paths: {', '.join(artifact_paths) if artifact_paths else '(none reported)'}\n"
            f"- Unresolved: {', '.join(unresolved) if unresolved else '(none)'}\n"
        ),
    )
    artifacts.register(
        "builder_output",
        str(snapshot_path.relative_to(target_root)),
        result.summary,
    )
    for changed in files_changed:
        artifacts.register("builder_file_changed", changed, f"cycle-{cycle_index:03d}")
    for extra in artifact_paths:
        artifacts.register("builder_artifact", extra, f"cycle-{cycle_index:03d}")
    runtime.append_event(
        "builder_completed",
        {
            "cycle": cycle_index,
            "summary": result.summary,
            "files_changed": files_changed,
            "artifact_paths": artifact_paths,
            "unresolved": unresolved,
        },
    )


def _run_verifier(
    *,
    target_root: Path,
    runtime: RuntimeStore,
    artifacts: ArtifactStore,
    roles_config: dict[str, str],
    task: dict[str, object],
    role: str,
    state: EngineState,
    cycle_index: int,
) -> dict[str, object]:
    runtime.write_json(
        "runtime/session.json",
        {
            **runtime.read_json("runtime/session.json", {}),
            "state": state.value,
            "active_role": role,
        },
    )
    adapter = _build_adapter(roles_config.get(role, "codex_cli"))
    result = adapter.invoke(
        Invocation(
            role=role,
            objective=str(task.get("title", "")),
            working_directory=str(target_root),
            context={"task": task, "cycle": cycle_index},
        )
    )
    payload = result.payload or {}
    review_path = (
        "reviews/functional_latest.json" if role == "verifier_functional" else "reviews/human_latest.json"
    )
    review_payload: dict[str, object] = {
        "role": role,
        "status": result.status,
        "summary": result.summary,
        "result": payload.get("result", "pass"),
        "score": payload.get("score", 0.0),
        "findings": payload.get("findings", []),
        "suggested_actions": payload.get("suggested_actions", []),
        "cycle": cycle_index,
    }
    if role == "verifier_functional":
        review_payload["evidence"] = payload.get("evidence", [])
        review_payload["blocking_issues"] = payload.get("blocking_issues", [])
    else:
        review_payload["strengths"] = payload.get("strengths", [])
        review_payload["comparison_notes"] = payload.get("comparison_notes", [])
    runtime.write_json(review_path, review_payload)
    artifacts.register(role, f".orch/{review_path}", result.summary)
    runtime.append_event(f"{role}_completed", {"cycle": cycle_index, "summary": result.summary})
    return review_payload


def _resolve_iteration_limits(
    limits_config: dict[str, object],
    common_defaults: dict[str, object],
) -> dict[str, object]:
    """Merge target-level limits.yaml with the common policy defaults.

    Parameters mirror what `run_cycle` already has in scope:
      - `limits_config` is the `limits` dict inside the target's `.orch/config/limits.yaml`.
      - `common_defaults` is the `defaults` dict under `domains/common/common.yaml`
        (i.e. `PolicyLoader.load_common()["defaults"]`). It contains `limits`,
        `scoring`, and `guardrails` sections — we only read `limits` here.

    Project-level values win. Falls back to the common defaults, then to
    hard-coded safe values (max_cycles=6, stop_on_stagnation=True).

    `max_cycles <= 0` (after merging) is interpreted as "no cycle cap" by
    `_apply_iteration_policy`; we therefore normalize any such value to 0.
    """
    common_limits = common_defaults.get("limits", {}) if isinstance(common_defaults, dict) else {}
    common_cycle = common_limits.get("cycle_limits", {}) if isinstance(common_limits, dict) else {}
    common_stop = common_limits.get("auto_stop_rules", {}) if isinstance(common_limits, dict) else {}

    project_max = limits_config.get("max_cycles") if isinstance(limits_config, dict) else None
    project_stop = limits_config.get("stop_on_stagnation") if isinstance(limits_config, dict) else None

    default_max = common_cycle.get("max_cycles", 6) if isinstance(common_cycle, dict) else 6
    default_stop = common_stop.get("stop_on_stagnation", True) if isinstance(common_stop, dict) else True

    try:
        max_cycles = int(project_max if project_max is not None else default_max)
    except (TypeError, ValueError):
        max_cycles = 6
    if max_cycles < 0:
        max_cycles = 0  # treated as "no cap" by _apply_iteration_policy
    stop_on_stagnation = bool(
        project_stop if project_stop is not None else default_stop
    )
    return {"max_cycles": max_cycles, "stop_on_stagnation": stop_on_stagnation}


def _score_history(session: dict[str, object]) -> list[dict[str, object]]:
    raw = session.get("score_history") if isinstance(session, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _apply_iteration_policy(
    base_decision: dict[str, object],
    *,
    cycle_index: int,
    score_history: list[dict[str, object]],
    limits: dict[str, object],
    functional_review: dict[str, object],
    human_review: dict[str, object],
    handoff_pause_count: int = 0,
) -> dict[str, object]:
    """Escalate `needs_iteration` to a hard stop when policy limits are hit.

    `handoff_pause_count` is the number of cycles in this session that paused
    mid-flow for a Codex App handoff and therefore never ran a full
    planner->builder->verifier loop. Those cycles consume a `cycle_index`
    slot but should not count toward `max_cycles`, because the reviewer pause
    itself is not a failed iteration attempt. We subtract the pause count to
    get the effective iteration count that policy limits are measured against.
    """
    if base_decision.get("decision") != "needs_iteration":
        return base_decision

    # `max_cycles <= 0` is interpreted as "no cycle cap" — see
    # `_resolve_iteration_limits` for how that value is produced.
    max_cycles = int(limits.get("max_cycles", 0) or 0)
    pause_count = max(0, int(handoff_pause_count or 0))
    effective_cycle = max(0, cycle_index - pause_count)
    if max_cycles > 0 and effective_cycle >= max_cycles:
        pause_note = (
            f" (handoff 일시정지 {pause_count}회 제외)" if pause_count else ""
        )
        return {
            "decision": "max_cycles_reached",
            "next_state": EngineState.BLOCKED.value,
            "reason": (
                f"중단: 유효 사이클 {effective_cycle}{pause_note}이 "
                f"max_cycles={max_cycles}에 도달, 리뷰 결과는 needs_iteration."
            ),
        }

    if limits.get("stop_on_stagnation") and _is_stagnating(
        score_history=score_history,
        current_functional=float(functional_review.get("score", 0.0)),
        current_human=float(human_review.get("score", 0.0)),
    ):
        return {
            "decision": "stagnation_detected",
            "next_state": EngineState.BLOCKED.value,
            "reason": (
                "중단: needs_iteration 사이클 2회 연속 점수 개선 없음 "
                "(stop_on_stagnation=true)."
            ),
        }
    return base_decision


def _is_stagnating(
    *,
    score_history: list[dict[str, object]],
    current_functional: float,
    current_human: float,
) -> bool:
    """Return True when the last iteration cycle's scores matched or exceeded the current ones.

    A single "did not improve" signal after a previous needs_iteration cycle is
    enough to stop — this mirrors the expectation that the engine should not
    spin the same cycle indefinitely without making progress.
    """
    prior_iterations = [
        entry for entry in score_history if entry.get("decision") == "needs_iteration"
    ]
    if not prior_iterations:
        return False
    previous = prior_iterations[-1]
    try:
        prev_fun = float(previous.get("functional_score", 0.0) or 0.0)
        prev_hum = float(previous.get("human_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    functional_improved = current_functional > prev_fun
    human_improved = current_human > prev_hum
    return not (functional_improved or human_improved)


def _orchestrator_decision(
    common_policy: dict[str, object],
    functional_review: dict[str, object],
    human_review: dict[str, object],
) -> dict[str, object]:
    thresholds = common_policy.get("scoring", {}).get("thresholds", {})
    functional_pass = float(thresholds.get("functional_pass", 0.7))
    human_pass = float(thresholds.get("human_pass", 0.7))
    functional_result = functional_review.get("result")
    human_result = human_review.get("result")
    functional_score = float(functional_review.get("score", 0.0))
    human_score = float(human_review.get("score", 0.0))

    function_ok = functional_result == "pass" and functional_score >= functional_pass
    human_ok = human_result == "pass" and human_score >= human_pass
    if function_ok and human_ok:
        return {
            "decision": "complete_cycle",
            "next_state": EngineState.COMPLETED.value,
            "reason": "두 리뷰 모두 합격 기준을 통과했습니다.",
        }

    # 리뷰가 명시적으로 `block`을 반환하면 하드 스톱. 운영자 개입 없이
    # 다시 사이클을 돌리는 건 의미가 없다.
    if functional_result == "block" or human_result == "block":
        return {
            "decision": "blocked",
            "next_state": EngineState.BLOCKED.value,
            "reason": "리뷰어 중 한 명이 result=block을 반환했습니다.",
        }

    # 그 외 리뷰 결과 (fail / needs_iteration / 낮은 점수)는 재시도 가능.
    # 다음 사이클에 planner를 다시 돌린다.
    return {
        "decision": "needs_iteration",
        "next_state": EngineState.ITERATING.value,
        "reason": "리뷰 점수가 기준 이하 — 다음 사이클에 재계획.",
    }


def _finalize_cycle(
    *,
    target_root: Path,
    runtime: RuntimeStore,
    task: dict[str, object],
    decision: dict[str, object],
    cycle_index: int,
    dispatcher: Dispatcher,
    functional_review: dict[str, object] | None = None,
    human_review: dict[str, object] | None = None,
) -> None:
    task_key = _task_key(task)
    passed = decision["decision"] == "complete_cycle"
    functional_review = functional_review or {}
    human_review = human_review or {}

    completed = runtime.read_json("tasks/completed.json", [])
    backlog = runtime.read_json("tasks/backlog.json", [])

    if passed:
        # Only record the task as completed if the cycle actually succeeded.
        # For needs_iteration / blocked the work should stay in the backlog so
        # the next planner run can pick it up (or replace it).
        if task_key and task_key not in _task_keys(completed):
            completed.append(task)
        # Drop the just-done task from the backlog regardless — it is now
        # either complete or intentionally set aside.
        backlog = [item for item in backlog if _task_key(item) != task_key]
        # Clear any handoff review stored for the just-completed task so a
        # future iterating cycle on a different task does not inherit stale
        # handoff feedback. (`reviews/handoff_latest.json` is written by
        # `handoff-ingest` and only stays meaningful until the next cycle
        # actually closes.)
        if runtime.read_json("reviews/handoff_latest.json", {}):
            runtime.write_json("reviews/handoff_latest.json", {})

    runtime.write_json("tasks/completed.json", completed)
    runtime.write_json("tasks/backlog.json", backlog)
    runtime.write_json("tasks/active.json", [])
    runtime.write_json(
        "runtime/queue.json",
        {"backlog": backlog, "active": [], "completed": completed},
    )
    session = runtime.read_json("runtime/session.json", {})
    history = _score_history(session)
    history.append(
        {
            "cycle": cycle_index,
            "decision": decision["decision"],
            "functional_score": float(functional_review.get("score", 0.0) or 0.0),
            "human_score": float(human_review.get("score", 0.0) or 0.0),
            "functional_result": str(functional_review.get("result", "")),
            "human_result": str(human_review.get("result", "")),
        }
    )
    history = history[-12:]  # keep the tail; stagnation check only needs the last entry
    runtime.write_json(
        "runtime/session.json",
        {
            **session,
            "state": decision["next_state"],
            "active_role": None,
            "cycle": cycle_index,
            "last_decision": decision["decision"],
            "last_decision_reason": decision.get("reason", ""),
            "next_role_hint": dispatcher.next_role(EngineState(decision["next_state"])),
            "score_history": history,
        },
    )
    _write_text(
        target_root / ".orch" / "reports" / "cycle-summary.md",
        (
            "# Cycle Summary\n\n"
            f"- Cycle: {cycle_index}\n"
            f"- Task: {task.get('title', '')}\n"
            f"- Decision: {decision['decision']}\n"
            f"- Reason: {decision.get('reason', '')}\n"
            f"- Next state: {decision['next_state']}\n"
            f"- Backlog remaining: {len(backlog)}\n"
            f"- Completed total: {len(completed)}\n"
        ),
    )
    runtime.append_event(
        "cycle_completed",
        {
            "cycle": cycle_index,
            "decision": decision["decision"],
            "reason": decision.get("reason", ""),
            "backlog_size": len(backlog),
            "completed_size": len(completed),
        },
    )


def _task_key(task: object) -> str:
    if not isinstance(task, dict):
        return ""
    return str(task.get("id") or task.get("title") or "")


def _task_keys(tasks: list[dict[str, object]] | list[object]) -> set[str]:
    keys: set[str] = set()
    for item in tasks:
        key = _task_key(item)
        if key:
            keys.add(key)
    return keys


def _dedupe_tasks(tasks: list[dict[str, object]] | list[object]) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        key = _task_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _human_review_mode(workflow_config: dict[str, object]) -> str:
    """Return the effective human review mode: `cli` (adapter) or `handoff`.

    Any unrecognized value falls back to `cli` so a misconfigured workflow
    cannot silently strand a cycle in a handoff state.
    """
    mode = str(workflow_config.get("human_review_mode", "cli")).strip().lower()
    return mode if mode in {"cli", "handoff"} else "cli"


def _pause_for_human_handoff(
    *,
    target_root: Path,
    runtime: RuntimeStore,
    orch_root: Path,
    project_config: dict[str, object],
    task: dict[str, object],
    cycle_index: int,
    functional_review: dict[str, object],
):
    """Open an `approve_gate` handoff in place of the verifier_human adapter call.

    Returns the resulting `HandoffStatus` so the caller can surface the id.
    """
    project = project_config.get("project", {}) if isinstance(project_config, dict) else {}
    project_id = str(project.get("id") or target_root.name)
    goal = str(project.get("goal_summary", ""))
    task_title = str(task.get("title", "")) if isinstance(task, dict) else ""
    functional_summary = (
        str(functional_review.get("summary", "")) if isinstance(functional_review, dict) else ""
    )
    functional_findings = (
        functional_review.get("findings", []) if isinstance(functional_review, dict) else []
    )
    findings_list = (
        [str(item) for item in functional_findings]
        if isinstance(functional_findings, list)
        else []
    )

    request = HandoffRequest(
        project_id=project_id,
        mode="approve_gate",
        reason=(
            f"verifier_human handoff mode is enabled. Cycle {cycle_index} builder output "
            "needs a human-style review before the cycle can close."
        ),
        goal=goal,
        what_needs_decision=(
            "Review the latest builder output and the functional verifier report, "
            "then fill in response.yaml with result ∈ "
            "{approved, changes_made, replan_needed, blocked, rejected}."
        ),
        allowed_edit_scope=[],  # approve_gate is review-only by default
        blocked_by=findings_list,
        recommended_read_order=[
            f".orch/artifacts/snapshots/cycle-{cycle_index:03d}-builder.md",
            ".orch/reports/current-plan.md",
            ".orch/reviews/functional_latest.json",
        ],
        expected_return_format="yaml",
        active_tasks=[task] if isinstance(task, dict) else [],
        latest_plan_summary=_safe_read_text(orch_root / "reports" / "current-plan.md"),
        latest_functional_review_summary=functional_summary,
        latest_human_review_summary="",  # not produced yet; this handoff replaces it
        artifact_index=_build_artifact_index(runtime),
        constraints_and_guardrails=_collect_guardrails(orch_root),
        resume_expectation=(
            f"After Codex App returns a response, run `python -m core.app handoff-ingest "
            f"--target {target_root}` to resume the engine. The orchestrator will map "
            "`approved` -> completed, `changes_made`/`replan_needed` -> iterating, "
            "`blocked`/`rejected` -> blocked."
        ),
    )

    manager = HandoffManager(target_root)
    try:
        status = manager.create_request(request)
    except HandoffError as exc:
        # Should not normally happen because run-cycle refuses to start while a
        # handoff is active, but guard against it anyway.
        raise AdapterExecutionError(f"handoff mode failed to open: {exc}") from exc

    session = runtime.read_json("runtime/session.json", {})
    # Track handoff-pause cycles separately from cycle_index so the iteration
    # policy can exclude them from max_cycles. See `_apply_iteration_policy`.
    pause_count = int(session.get("handoff_pause_count", 0) or 0) + 1
    runtime.write_json(
        "runtime/session.json",
        {
            **session,
            "state": EngineState.HANDOFF_ACTIVE.value,
            "active_role": None,
            "cycle": cycle_index,
            "last_handoff_id": status.handoff_id,
            "last_decision": "handoff_requested",
            "last_decision_reason": "verifier_human routed to handoff mode",
            "handoff_pause_count": pause_count,
        },
    )
    runtime.append_event(
        "verifier_human_handoff_opened",
        {
            "cycle": cycle_index,
            "handoff_id": status.handoff_id,
            "mode": "approve_gate",
            "handoff_pause_count": pause_count,
        },
    )
    return status


def run_handoff_request(args: argparse.Namespace) -> int:
    target_root = Path(args.target).resolve()
    orch_root = target_root / ".orch"
    if not orch_root.exists():
        print(f".orch 폴더가 없습니다: {orch_root}")
        return 1

    runtime = RuntimeStore(target_root)
    session = runtime.read_json("runtime/session.json", {})
    project_config = _read_yaml(orch_root / "config" / "project.yaml")
    project = project_config.get("project", {}) if isinstance(project_config, dict) else {}
    project_id = str(project.get("id") or target_root.name)
    goal = args.goal_override or str(project.get("goal_summary", ""))

    backlog = runtime.read_json("tasks/backlog.json", [])
    active_tasks = runtime.read_json("tasks/active.json", [])
    functional_review = runtime.read_json("reviews/functional_latest.json", {})
    human_review = runtime.read_json("reviews/human_latest.json", {})

    request = HandoffRequest(
        project_id=project_id,
        mode=args.mode,
        reason=args.reason,
        goal=goal,
        what_needs_decision=args.what_needs_decision,
        allowed_edit_scope=list(args.allowed_edit_scope),
        blocked_by=[str(item) for item in (session.get("last_decision_reason") or "").split("\n") if item.strip()],
        recommended_read_order=[
            ".orch/reports/current-plan.md",
            ".orch/reviews/functional_latest.json",
            ".orch/reviews/human_latest.json",
        ],
        expected_return_format="yaml",
        active_tasks=active_tasks if isinstance(active_tasks, list) else [],
        latest_plan_summary=_safe_read_text(orch_root / "reports" / "current-plan.md"),
        latest_functional_review_summary=str(functional_review.get("summary", "")) if isinstance(functional_review, dict) else "",
        latest_human_review_summary=str(human_review.get("summary", "")) if isinstance(human_review, dict) else "",
        artifact_index=_build_artifact_index(runtime),
        constraints_and_guardrails=_collect_guardrails(orch_root),
        resume_expectation=(
            f"After Codex App returns a response, run `python -m core.app handoff-ingest "
            f"--target {target_root}` to resume the engine."
        ),
    )

    manager = HandoffManager(target_root)
    try:
        status = manager.create_request(request)
    except HandoffError as exc:
        print(f"handoff 요청이 거부되었습니다: {exc}")
        return 1

    runtime.write_json(
        "runtime/session.json",
        {
            **session,
            "state": EngineState.HANDOFF_ACTIVE.value,
            "active_role": None,
            "last_handoff_id": status.handoff_id,
            "last_decision": "handoff_requested",
            "last_decision_reason": args.reason,
        },
    )
    print(f"handoff 열림: {status.handoff_id} (mode={status.mode})")
    print(f"요청 파일: {manager.request_path.relative_to(target_root)}")
    print(f"응답 템플릿: {manager.response_path.relative_to(target_root)}")
    print("response.yaml을 채운 뒤 `handoff-ingest` 명령으로 재개하세요.")
    return 0


def run_handoff_status(args: argparse.Namespace) -> int:
    target_root = Path(args.target).resolve()
    manager = HandoffManager(target_root)
    status = manager.status()
    print(f"활성 여부: {status.active}")
    print(f"상태: {status.state}")
    print(f"handoff id: {status.handoff_id or '(없음)'}")
    print(f"모드: {status.mode or '(없음)'}")
    if status.created_at:
        print(f"생성 시각: {status.created_at}")
    if status.returned_at:
        print(f"응답 반환 시각: {status.returned_at}")
    return 0


def run_handoff_ingest(args: argparse.Namespace) -> int:
    target_root = Path(args.target).resolve()
    orch_root = target_root / ".orch"
    if not orch_root.exists():
        print(f".orch 폴더가 없습니다: {orch_root}")
        return 1

    runtime = RuntimeStore(target_root)
    manager = HandoffManager(target_root)
    try:
        response = manager.ingest()
    except HandoffError as exc:
        print(f"handoff ingest 실패: {exc}")
        return 1

    decision = _handoff_result_to_engine_decision(str(response.get("result")))
    session = runtime.read_json("runtime/session.json", {})
    # Only iterating outcomes feed back into the next planner run via
    # `_collect_previous_reviews`. Terminal outcomes (approved -> completed,
    # blocked/rejected -> blocked) must clear any prior handoff record so an
    # unrelated future iteration cycle does not inherit a stale verdict — see
    # reviewer finding: approved handoff followed by fail->iterating cycles
    # would otherwise leak the old "approved" feedback into the new planner.
    if decision["next_state"] == EngineState.ITERATING.value:
        handoff_findings = response.get("findings")
        handoff_risks = response.get("remaining_risks")
        runtime.write_json(
            "reviews/handoff_latest.json",
            {
                "role": "handoff",
                "status": "ok",
                "summary": str(response.get("summary") or ""),
                "result": str(response.get("result") or ""),
                "findings": list(handoff_findings) if isinstance(handoff_findings, list) else [],
                "recommended_next_action": str(response.get("recommended_next_action") or ""),
                "remaining_risks": list(handoff_risks) if isinstance(handoff_risks, list) else [],
                "decision_note": str(response.get("decision") or ""),
                "handoff_id": str(response.get("handoff_id") or ""),
                "ingested_after_cycle": int(session.get("cycle", 0) or 0),
            },
        )
    else:
        runtime.write_json("reviews/handoff_latest.json", {})
    runtime.write_json(
        "runtime/session.json",
        {
            **session,
            "state": decision["next_state"],
            "active_role": None,
            "last_decision": decision["decision"],
            "last_decision_reason": str(response.get("summary") or ""),
        },
    )
    manager.acknowledge_resume()
    print(f"handoff 수용 완료. result={response.get('result')} -> 다음 상태={decision['next_state']}")
    recommended = response.get("recommended_next_action")
    if recommended:
        print(f"권장 다음 작업: {recommended}")
    return 0


def run_handoff_cancel(args: argparse.Namespace) -> int:
    target_root = Path(args.target).resolve()
    orch_root = target_root / ".orch"
    if not orch_root.exists():
        print(f".orch 폴더가 없습니다: {orch_root}")
        return 1

    runtime = RuntimeStore(target_root)
    manager = HandoffManager(target_root)
    status_before = manager.status()
    if not status_before.active:
        print("취소할 활성 handoff가 없습니다.")
        return 0
    manager.cancel(reason=args.reason)
    # Defensive cleanup: if a prior ingest had persisted a handoff verdict and
    # a new handoff was then opened and cancelled, the earlier verdict would
    # still sit in `reviews/handoff_latest.json`. Clearing it here keeps the
    # invariant "handoff_latest is only present while its feedback is still
    # actionable for the next iteration".
    if runtime.read_json("reviews/handoff_latest.json", {}):
        runtime.write_json("reviews/handoff_latest.json", {})
    session = runtime.read_json("runtime/session.json", {})
    runtime.write_json(
        "runtime/session.json",
        {
            **session,
            "state": EngineState.IDLE.value,
            "active_role": None,
            "last_decision": "handoff_cancelled",
            "last_decision_reason": args.reason,
        },
    )
    print(f"handoff {status_before.handoff_id} 취소 완료.")
    return 0


def _safe_read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _build_artifact_index(runtime: RuntimeStore) -> list[dict[str, str]]:
    """Return the most recent artifact records in handoff-friendly shape.

    `ArtifactStore` writes `{"items": [...]}` into `artifacts/index.json`, so
    we must unwrap the dict form here. Older code paths that wrote a bare
    list are still tolerated for forward-compatibility with legacy projects.
    """
    raw = runtime.read_json("artifacts/index.json", {"items": []})
    if isinstance(raw, dict):
        items = raw.get("items", [])
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    compact: list[dict[str, str]] = []
    for item in items[-20:]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "kind": str(item.get("kind", "")),
                "path": str(item.get("path", "")),
                "summary": str(item.get("summary", ""))[:240],
            }
        )
    return compact


def _collect_existing_artifacts(
    target_root: Path, runtime: RuntimeStore
) -> list[dict[str, str]]:
    """현재 target 에 실제로 존재하는 builder 산출물 파일 목록을 반환한다.

    `artifact_store`에 `builder_file_changed` / `builder_artifact` /
    `builder_output` kind로 기록된 경로 중 디스크에 실제로 존재하는 것만
    모은다. 이 목록은 iterating 사이클에서 planner와 builder에게
    "신규 작성이 아니라 기존 파일의 증분 수정이 필요하다" 는 신호로
    전달된다. 매 cycle마다 builder가 파일 전체를 다시 써서 토큰과 시간을
    낭비하는 경향을 완화하는 것이 목적.
    """
    raw = runtime.read_json("artifacts/index.json", {"items": []})
    if isinstance(raw, dict):
        items = raw.get("items", [])
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    file_kinds = {"builder_file_changed", "builder_artifact", "builder_output"}
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    # 최신 기록부터 뒤로 훑어 dedupe. target_root 기준 상대 경로와 절대 경로
    # 모두 존재 여부를 확인해 stale 엔트리는 제외한다.
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", ""))
        if kind not in file_kinds:
            continue
        path = str(item.get("path", "")).strip()
        if not path or path in seen:
            continue
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = target_root / path
        if not candidate.exists():
            continue
        seen.add(path)
        result.append(
            {
                "kind": kind,
                "path": path,
                "summary": str(item.get("summary", ""))[:200],
            }
        )
        if len(result) >= 15:
            break
    # 오래된 것 → 최근 것 순으로 다시 뒤집어 반환 (맥락상 시간 순이 자연스러움).
    return list(reversed(result))


def _collect_guardrails(orch_root: Path) -> list[str]:
    guardrails = _read_yaml(orch_root / "config" / "guardrails.yaml")
    values: list[str] = []
    if isinstance(guardrails, dict):
        for key, value in guardrails.items():
            if isinstance(value, (str, int, float, bool)):
                values.append(f"{key}={value}")
            elif isinstance(value, list):
                values.append(f"{key}={','.join(str(item) for item in value)}")
    return values


def _handoff_result_to_engine_decision(result: str) -> dict[str, str]:
    mapping = {
        "approved": {"decision": "complete_cycle", "next_state": EngineState.COMPLETED.value},
        "changes_made": {"decision": "needs_iteration", "next_state": EngineState.ITERATING.value},
        "replan_needed": {"decision": "needs_iteration", "next_state": EngineState.ITERATING.value},
        "blocked": {"decision": "blocked", "next_state": EngineState.BLOCKED.value},
        "rejected": {"decision": "blocked", "next_state": EngineState.BLOCKED.value},
    }
    return mapping.get(result, {"decision": "needs_iteration", "next_state": EngineState.ITERATING.value})


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
