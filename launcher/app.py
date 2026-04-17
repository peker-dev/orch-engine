from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


ENGINE_ROOT = Path(__file__).resolve().parent.parent
DOMAINS_ROOT = ENGINE_ROOT / "domains"
TEMPLATE_ORCH_ROOT = ENGINE_ROOT / "templates" / "target_project" / ".orch"
# Recent-project list location. `ORCH_LAUNCHER_STATE` lets callers (e.g.
# smoke tests) redirect to a sandbox path so they don't mutate the user's
# real recent list.
DEFAULT_STATE_PATH = Path.home() / ".orch_engine" / "recent_projects.json"
RECENT_PROJECT_LIMIT = 10


def main() -> int:
    print("orch-engine 런처")
    print("================")
    print()

    action = _prompt_choice(
        "실행할 작업을 선택하세요",
        [
            ("1", "새 프로젝트 시작"),
            ("2", "기존 프로젝트 연결"),
            ("3", "오케스트레이션된 프로젝트 재개"),
            ("4", "기존 오케스트레이션 프로젝트 관리"),
        ],
        default="1",
    )
    target_root = _prompt_path_with_recent("프로젝트 폴더 경로")
    _remember_project(target_root)
    detected_mode = _detect_mode(target_root)
    if action == "4":
        # 4번은 이미 초기화된 프로젝트를 대상으로 하므로 init/goal 흐름을
        # 건너뛰고 바로 관리 메뉴로 진입한다.
        if detected_mode != "resume":
            print(
                f"{target_root / '.orch'} 경로에 .orch 폴더가 없습니다. "
                "먼저 메뉴 1 또는 2로 프로젝트를 초기화하세요."
            )
            return 1
        return _manage_menu(target_root)
    mode = _mode_for_action(action, detected_mode)
    _print_detected_state(target_root, detected_mode, mode)

    if action == "1" and detected_mode == "resume":
        print(
            f"'새 프로젝트 시작'을 골랐지만 {target_root / '.orch'}에 이미 "
            f".orch 폴더가 있습니다. 메뉴 3(재개)을 선택해주세요."
        )
        return 1
    if action == "2" and detected_mode == "greenfield":
        print(
            f"'기존 프로젝트 연결'을 골랐지만 폴더가 비어 있습니다: "
            f"{target_root}. 메뉴 1(새 프로젝트)을 선택해주세요."
        )
        return 1
    if action == "3" and detected_mode != "resume":
        print(f"재개를 선택했지만 {target_root / '.orch'}에 .orch 폴더가 없습니다.")
        return 1

    existing_project = _read_project_settings(target_root)
    if mode == "resume":
        domain = str(existing_project.get("domain") or "web")
        project_name = str(existing_project.get("name") or target_root.name or "orch-project")
        goal_summary = str(existing_project.get("goal_summary") or "")
        if _confirm("재개 전에 목표 요약을 수정하시겠어요?", default=False):
            updated_goal = _prompt_multiline("새 목표 요약")
            if updated_goal:
                goal_summary = updated_goal
    else:
        domain = _prompt_choice("도메인", [(name, name) for name in _available_domains()], default="web")
        project_name = _prompt_text("프로젝트 이름", default=target_root.name or "orch-project")
        goal_summary = _prompt_multiline("목표 요약")
    if mode in {"greenfield", "retrofit"} and not goal_summary:
        print("새 프로젝트 또는 연결 모드에는 목표 요약이 필요합니다.")
        return 1

    roles = _read_yaml(TEMPLATE_ORCH_ROOT / "config" / "roles.yaml").get("roles", {})
    limits = _read_yaml(TEMPLATE_ORCH_ROOT / "config" / "limits.yaml").get("limits", {})
    _print_summary(
        target_root=target_root,
        mode=mode,
        domain=domain,
        project_name=project_name,
        goal_summary=goal_summary,
        roles=roles,
        limits=limits,
    )

    if not _confirm("이 설정으로 시작하시겠어요?", default=True):
        print("취소되었습니다.")
        return 1

    if mode in {"greenfield", "retrofit"}:
        init_rc = _run_core(
            [
                "init",
                "--target",
                str(target_root),
                "--domain",
                domain,
                "--mode",
                mode,
                "--project-name",
                project_name,
                "--goal-summary",
                goal_summary,
            ]
        )
        if init_rc != 0:
            return init_rc
    elif goal_summary:
        _update_existing_goal(target_root, goal_summary)

    if _confirm("지금 한 사이클을 실행하시겠어요?", default=False):
        args = ["run-cycle", "--target", str(target_root)]
        if goal_summary:
            args.extend(["--goal-summary", goal_summary])
        return _run_core(args)

    print(f"준비 완료. 런타임 스캐폴드: {target_root / '.orch'}")
    return 0


def _prompt_choice(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    valid = {key: label for key, label in options}
    while True:
        print(prompt + ":")
        for key, label in options:
            suffix = " (기본값)" if key == default else ""
            if key == label:
                print(f"  {label}{suffix}")
            else:
                print(f"  {key}. {label}{suffix}")
        answer = input("> ").strip() or default
        if answer in valid:
            return answer
        matching = [key for key, label in options if answer.lower() == label.lower()]
        if matching:
            return matching[0]
        print(f"다음 중 하나를 선택하세요: {', '.join(valid)}")


def _prompt_path(prompt: str) -> Path:
    while True:
        raw = input(f"{prompt}: ").strip()
        path = _clean_path(raw)
        if path:
            return path.resolve()
        print("폴더 경로를 입력하세요.")


def _prompt_path_with_recent(prompt: str) -> Path:
    """Like `_prompt_path` but offers the last used projects as shortcuts.

    A numeric answer picks that recent entry; if that entry no longer
    exists on disk we warn and re-prompt instead of silently letting
    `_detect_mode` classify the missing path as a fresh greenfield.
    Each displayed item is clipped to 200 chars so a pathological state
    file can't flood the screen.
    """
    recent = _load_recent_projects()
    list_printed = False

    def _print_list() -> None:
        nonlocal list_printed
        if not recent:
            return
        print("최근 프로젝트 (번호로 선택하거나 새 경로 입력):")
        for idx, item in enumerate(recent, start=1):
            display = item if len(item) <= 200 else item[:197] + "..."
            print(f"  {idx}. {display}")
        list_printed = True

    _print_list()
    while True:
        raw = input(f"{prompt}: ").strip()
        if raw.isdigit() and recent:
            pick = int(raw)
            if 1 <= pick <= len(recent):
                candidate = Path(recent[pick - 1]).expanduser().resolve()
                if not candidate.exists():
                    print(
                        f"선택한 최근 프로젝트가 더 이상 존재하지 않습니다: {candidate}"
                    )
                    print("다른 번호를 고르거나 새 경로를 입력하세요.")
                    if not list_printed:
                        _print_list()
                    continue
                return candidate
            print(f"1 ~ {len(recent)} 사이 번호를 고르거나 경로를 입력하세요.")
            continue
        path = _clean_path(raw)
        if path:
            return path.resolve()
        print("폴더 경로를 입력하거나 최근 목록에서 번호를 고르세요.")


def _state_path() -> Path:
    override = os.environ.get("ORCH_LAUNCHER_STATE")
    if override:
        return Path(override).expanduser()
    return DEFAULT_STATE_PATH


def _load_recent_projects() -> list[str]:
    path = _state_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    items = raw.get("recent_projects", [])
    return [str(item) for item in items if isinstance(item, str)]


def _remember_project(path: Path) -> None:
    """Move this path to the front of the recent list (dedup, cap length).

    Failures are swallowed — this is a convenience feature, not a
    correctness requirement, so we never want it to break a real session.
    """
    state_file = _state_path()
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_recent_projects()
        as_str = str(path)
        if as_str in existing:
            existing.remove(as_str)
        existing.insert(0, as_str)
        trimmed = existing[:RECENT_PROJECT_LIMIT]
        state_file.write_text(
            json.dumps({"recent_projects": trimmed}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _prompt_text(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _prompt_multiline(prompt: str) -> str:
    print(f"{prompt}:")
    print("한 줄 이상 입력하세요. 빈 줄(엔터만)을 입력하면 종료됩니다.")
    lines: list[str] = []
    while True:
        line = input("> ").rstrip()
        if not line:
            break
        lines.append(line)
    return " ".join(lines).strip()


def _confirm(prompt: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "예", "네"}


def _clean_path(raw: str) -> Path | None:
    text = raw.strip().strip('"').strip("'")
    if text.startswith("file:///"):
        text = text[8:]
    if not text:
        return None
    return Path(text).expanduser()


def _detect_mode(target_root: Path) -> str:
    if (target_root / ".orch").exists():
        return "resume"
    if not target_root.exists():
        return "greenfield"
    try:
        has_entries = any(target_root.iterdir())
    except OSError:
        has_entries = True
    return "retrofit" if has_entries else "greenfield"


def _print_detected_state(target_root: Path, detected_mode: str, selected_mode: str) -> None:
    print()
    print(f"감지된 모드: {detected_mode}")
    if selected_mode != detected_mode:
        print(f"선택된 모드: {selected_mode}")
    if detected_mode != "resume":
        print()
        return

    session = _read_yaml_or_json(target_root / ".orch" / "runtime" / "session.json")
    project = _read_project_settings(target_root)
    print(f"프로젝트: {project.get('name') or target_root.name}")
    print(f"도메인: {project.get('domain') or '(알 수 없음)'}")
    print(f"상태: {session.get('state') or '(알 수 없음)'}")
    print(f"사이클: {session.get('cycle', 0)}")
    if session.get("last_decision"):
        print(f"최근 결정: {session.get('last_decision')}")
    print()


def _mode_for_action(action: str, detected_mode: str) -> str:
    """Resolve the effective mode from the user's action choice.

    Conflict combinations (action=1 with existing .orch, action=2 with an
    empty folder, action=3 without an existing .orch) are caught in `main()`
    before this helper runs and result in an early error, so here we only
    need to map the valid pairs.
    """
    if action == "1":
        # "Start a new project" — greenfield if the folder is empty, retrofit
        # if it has files but no .orch.
        return "greenfield" if detected_mode == "greenfield" else "retrofit"
    if action == "2":
        # "Attach an existing project" — retrofit an un-initialized folder,
        # or resume one that already has .orch.
        return "resume" if detected_mode == "resume" else "retrofit"
    return "resume"


def _available_domains() -> list[str]:
    domains = [
        path.name
        for path in DOMAINS_ROOT.iterdir()
        if path.is_dir() and path.name not in {"common", "schema"}
    ]
    return sorted(domains) or ["web"]


def _read_project_settings(target_root: Path) -> dict[str, object]:
    config = _read_yaml(target_root / ".orch" / "config" / "project.yaml")
    project = config.get("project", {}) if isinstance(config, dict) else {}
    return project if isinstance(project, dict) else {}


def _print_summary(
    *,
    target_root: Path,
    mode: str,
    domain: str,
    project_name: str,
    goal_summary: str,
    roles: dict[str, object],
    limits: dict[str, object],
) -> None:
    print()
    print("요약")
    print("----")
    print(f"대상 폴더: {target_root}")
    print(f"모드: {mode}")
    print(f"도메인: {domain}")
    print(f"프로젝트 이름: {project_name}")
    print(f"목표: {goal_summary or '(기존 프로젝트 목표 사용)'}")
    print("런타임 역할 (role → adapter):")
    for role, adapter in roles.items():
        print(f"  - {role}: {adapter}")
    token_preflight = limits.get("token_preflight", {}) if isinstance(limits, dict) else {}
    if token_preflight:
        print("토큰 preflight:")
        print(f"  - 라운드 예산: {token_preflight.get('round_budget_tokens')}")
        print(f"  - role별 출력 예약분: {token_preflight.get('output_reserve_per_role')}")
    print()


def _run_core(args: list[str]) -> int:
    command = [sys.executable, "-m", "core.app", *args]
    print()
    print("실행: " + " ".join(_quote_arg(arg) for arg in command), flush=True)
    return subprocess.run(command, cwd=str(ENGINE_ROOT), check=False).returncode


def _run_core_in_new_console(args: list[str]) -> subprocess.Popen | None:
    """Spawn `core.app` in a separate console window on Windows and keep the
    launcher menu interactive. Returns the `Popen` handle so the caller can
    tell whether the child is still alive (see `_manage_menu`).

    `stdin=DEVNULL` prevents the new child from contending with the parent
    launcher for keystrokes on shared-terminal setups. On non-Windows we
    fall back to inline because there is no portable way to open a detached
    terminal without assuming a specific emulator, and return None so the
    caller doesn't track a bogus handle.
    """
    command = [sys.executable, "-m", "core.app", *args]
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        child = subprocess.Popen(
            command,
            cwd=str(ENGINE_ROOT),
            creationflags=flags,
            stdin=subprocess.DEVNULL,
        )
        print()
        print(f"별도 콘솔 창에서 사이클 실행 시작 (pid={child.pid}).")
        print("이 메뉴는 여기에 계속 열려 있습니다. 진행 상황은 새 창에서 확인하시고,")
        print("창이 닫히면 이 메뉴의 `1. 상태 보기`로 결과를 확인하세요.")
        return child
    # POSIX 폴백 — 분리 터미널을 안전하게 띄울 방법이 없으므로 현재 창에서 실행.
    print("(Windows가 아닌 환경: 분리 콘솔을 띄울 수 없어 현재 창에서 실행합니다)")
    subprocess.run(command, cwd=str(ENGINE_ROOT), check=False)
    return None


def _quote_arg(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _read_yaml(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _read_yaml_or_json(path: Path) -> dict[str, object]:
    return _read_yaml(path)


def _write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _update_existing_goal(target_root: Path, goal_summary: str) -> None:
    project_path = target_root / ".orch" / "config" / "project.yaml"
    project_config = _read_yaml(project_path)
    project_config.setdefault("project", {})["goal_summary"] = goal_summary
    _write_yaml(project_path, project_config)


def _manage_menu(target_root: Path) -> int:
    """Operations submenu for an already-initialized target project.

    Each entry wraps a `core.app` subcommand so the user never has to leave
    the menu to exercise status/run-cycle/handoff/goal flows. The loop exits
    when the user picks `7` (or EOF arrives).
    """
    _print_detected_state(target_root, "resume", "resume")
    detached_child: subprocess.Popen | None = None
    while True:
        choice = _prompt_choice(
            "관리 메뉴",
            [
                ("1", "상태 보기"),
                ("2", "사이클 1회 실행"),
                ("3", "handoff 열기 (요청)"),
                ("4", "handoff 응답 수용 (ingest)"),
                ("5", "활성 handoff 취소"),
                ("6", "목표 요약 갱신"),
                ("7", "종료"),
            ],
            default="1",
        )
        if choice == "1":
            _show_status_pretty(target_root)
        elif choice == "2":
            # 이전에 분리된 사이클이 아직 살아 있으면 두 번째를 띄우지 않는다.
            # 두 writer가 동일 session.json에 경쟁 쓰기하면 last-write-wins로
            # 상태가 조용히 덮어쓰이기 때문.
            if detached_child is not None and detached_child.poll() is None:
                print(
                    f"이전에 실행된 사이클이 아직 진행 중입니다 (pid={detached_child.pid}). "
                    "해당 창이 닫히거나 종료될 때까지 기다린 뒤 다시 시도하세요."
                )
            elif _confirm(
                "사이클을 별도 콘솔 창에서 실행할까요? (기본값 아니오)",
                default=False,
            ):
                detached_child = _run_core_in_new_console(
                    ["run-cycle", "--target", str(target_root)]
                )
            else:
                rc = _run_core(["run-cycle", "--target", str(target_root)])
                if rc != 0:
                    print(f"run-cycle 반환 코드={rc}. `상태 보기` 메뉴로 원인을 확인하세요.")
        elif choice == "3":
            rc = _handoff_request_wizard(target_root)
            if rc != 0:
                print(f"handoff-request 반환 코드={rc}.")
            else:
                _after_handoff_request_guidance(target_root)
        elif choice == "4":
            response_path = target_root / ".orch" / "handoff" / "response.yaml"
            if not response_path.exists():
                print(
                    f"{response_path} 에 response.yaml이 없습니다. "
                    "먼저 메뉴 3으로 handoff를 여세요."
                )
                continue
            print(f"다음 응답 파일을 사용합니다: {response_path}")
            if not _confirm("response.yaml을 채우고 저장하셨나요?", default=True):
                print("건너뜁니다. 파일을 편집한 뒤 다시 시도하세요.")
                continue
            _run_core(["handoff-ingest", "--target", str(target_root)])
        elif choice == "5":
            reason = _prompt_text("취소 사유", default="사용자 취소")
            _run_core([
                "handoff-cancel",
                "--target",
                str(target_root),
                "--reason",
                reason,
            ])
        elif choice == "6":
            updated_goal = _prompt_multiline("새 목표 요약")
            if not updated_goal:
                print("목표 요약이 비어 있어 변경하지 않았습니다.")
                continue
            _update_existing_goal(target_root, updated_goal)
            print("목표 요약이 갱신되었습니다.")
        elif choice == "7":
            if detached_child is not None and detached_child.poll() is None:
                print(
                    f"안내: 분리된 사이클이 아직 실행 중입니다 (pid={detached_child.pid}). "
                    "이 메뉴를 종료해도 별도 창에서 계속 실행됩니다."
                )
            print("관리 메뉴를 종료합니다.")
            return 0
        print()


def _show_status_pretty(target_root: Path) -> None:
    """Render `.orch/runtime/session.json` as a readable panel.

    We read the file directly instead of shelling out to `core.app status`
    because the CLI prints raw dicts; the launcher audience wants the
    highlights (state / cycle / last decision / score history) first.
    """
    session_path = target_root / ".orch" / "runtime" / "session.json"
    if not session_path.exists():
        print(f"{session_path} 에 세션 파일이 아직 없습니다.")
        print("먼저 사이클을 한 번 돌리거나, 프로젝트가 초기화됐는지 확인하세요.")
        return
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"session.json 을 읽을 수 없습니다: {exc}")
        return
    project = _read_project_settings(target_root)
    print()
    print("-" * 60)
    print(f"프로젝트:         {project.get('name') or target_root.name}")
    print(f"도메인:           {project.get('domain') or '(알 수 없음)'}")
    print(f"상태:             {session.get('state', '(알 수 없음)')}")
    print(f"사이클:           {session.get('cycle', 0)}")
    last = session.get("last_decision")
    if last:
        print(f"최근 결정:        {last}")
    reason = str(session.get("last_decision_reason") or "")
    if reason:
        # reason이 여러 줄인 경우(일부 adapter 에러는 스택트레이스 포함) 첫 줄만
        # 취하고 길이도 클립해 패널이 한 줄로 유지되게 한다.
        first_line = reason.splitlines()[0] if reason else ""
        clipped = first_line if len(first_line) <= 120 else first_line[:117] + "..."
        print(f"최근 사유:        {clipped}")
    pause = int(session.get("handoff_pause_count", 0) or 0)
    if pause:
        print(f"handoff 일시정지: {pause}회")
    active_role = session.get("active_role")
    if active_role:
        print(f"현재 활성 role:   {active_role}")
    history = session.get("score_history", [])
    if isinstance(history, list) and history:
        print("최근 사이클:")
        for entry in history[-5:]:
            if not isinstance(entry, dict):
                continue
            fn_score = entry.get("functional_score") or 0
            hu_score = entry.get("human_score") or 0
            try:
                fn_txt = f"{float(fn_score):.2f}"
                hu_txt = f"{float(hu_score):.2f}"
            except (TypeError, ValueError):
                fn_txt, hu_txt = str(fn_score), str(hu_score)
            print(
                f"  사이클 {entry.get('cycle')}: {entry.get('decision')} "
                f"(기능점수={fn_txt}, 사람점수={hu_txt})"
            )
    # handoff.json is written by RuntimeStore as JSON; reading via the yaml
    # loader works because JSON is a subset of YAML, but switch to json here
    # to make intent obvious and skip the yaml dependency in the status path.
    handoff_path = target_root / ".orch" / "runtime" / "handoff.json"
    handoff_status: dict[str, object] = {}
    if handoff_path.exists():
        try:
            parsed = json.loads(handoff_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                handoff_status = parsed
        except (OSError, json.JSONDecodeError):
            handoff_status = {}
    if handoff_status.get("active"):
        print(f"활성 handoff:     id={handoff_status.get('handoff_id')} "
              f"mode={handoff_status.get('mode')}")
    print("-" * 60)


def _after_handoff_request_guidance(target_root: Path) -> None:
    """Tell the user exactly what to do next after a handoff request opens.

    We gate the guidance on the response template actually existing so a
    core.app path that returns rc=0 without creating the file (e.g. a
    future retry-on-conflict shortcut) cannot misfire this banner on a
    no-op.
    """
    response_path = target_root / ".orch" / "handoff" / "response.yaml"
    if not response_path.exists():
        print()
        print("안내: response.yaml이 생성되지 않았습니다. 요청이 no-op이었을 수 있어요.")
        print("ingest 전에 먼저 상태를 확인하세요.")
        return
    print()
    print("=" * 60)
    print("이 handoff의 다음 단계:")
    print(f"  1) 다음 파일을 열어 편집: {response_path}")
    print("  2) 필수 필드를 채워 넣으세요 (특히 `result`, `summary`,")
    print("     `decision`, `findings`, `recommended_next_action`).")
    print("  3) 파일을 저장.")
    print("  4) 관리 메뉴로 돌아와 `4. handoff 응답 수용 (ingest)` 선택.")
    print("=" * 60)
    if _confirm("지금 response.yaml을 기본 편집기로 여시겠어요?", default=True):
        _open_in_default_editor(response_path)


def _open_in_default_editor(path: Path) -> None:
    """Best-effort file open. Windows uses the shell association; POSIX
    falls back to `$EDITOR` then `xdg-open`/`open`. Failures are non-fatal —
    the user can always edit the file by hand.
    """
    import os
    import shutil as _shutil
    import platform
    try:
        if platform.system() == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        editor = os.environ.get("EDITOR")
        if editor:
            subprocess.Popen([editor, str(path)])
            return
        for tool in ("xdg-open", "open"):
            if _shutil.which(tool):
                subprocess.Popen([tool, str(path)])
                return
        print(f"자동 열기에 실패했습니다. 수동으로 열어서 편집하세요: {path}")
    except OSError as exc:
        print(f"편집기 자동 열기에 실패했습니다 ({exc}). 수동으로 편집하세요: {path}")


def _handoff_request_wizard(target_root: Path) -> int:
    """Collect handoff-request arguments interactively and delegate to core.app.

    Mirrors the CLI flags the user would otherwise have to remember
    (`--mode`, `--reason`, `--what-needs-decision`, `--allowed-edit-scope`).
    Scope entries are split on newlines so the user can paste a multi-path
    list directly.
    """
    from core.handoff_manager import SUPPORTED_MODES  # 런처 시작 속도를 위해 지역 import
    mode = _prompt_choice(
        "handoff 모드",
        [(name, name) for name in sorted(SUPPORTED_MODES)],
        default="review_only",
    )
    reason = _prompt_text("handoff가 필요한 이유", default="")
    while not reason:
        print("이유는 비워둘 수 없습니다.")
        reason = _prompt_text("handoff가 필요한 이유", default="")
    decision = _prompt_multiline("외부 도구에게 기대하는 정확한 판단/작업")
    while not decision:
        print("what-needs-decision은 비워둘 수 없습니다.")
        decision = _prompt_multiline("외부 도구에게 기대하는 정확한 판단/작업")
    print("편집 허용 범위 (상대 경로, 한 줄에 하나, 빈 줄로 입력 종료):")
    scope_lines: list[str] = []
    while True:
        entry = input("> ").strip()
        if not entry:
            break
        scope_lines.append(entry)
    args = [
        "handoff-request",
        "--target",
        str(target_root),
        "--mode",
        mode,
        "--reason",
        reason,
        "--what-needs-decision",
        decision,
    ]
    for path in scope_lines:
        args.extend(["--allowed-edit-scope", path])
    return _run_core(args)


if __name__ == "__main__":
    raise SystemExit(main())
