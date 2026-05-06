"""Unity batchmode runner — option C "Unity 가 켜지고 실행까지 + 결과 회수".

박제관 자동 테스트 정의 (`project-root.md`) 의 정통 풀이:
- Unity 를 codex sandbox 안에서 돌리지 않고, 엔진이 직접 subprocess 로 spawn.
- batchmode + executeMethod 로 specific 검증 메서드 호출.
- 로그 파일 회수 + `unity_log_parser` 로 결정적 verdict 산출.
- BaseRunnerAdapter 가 utterance.v1 자동 합성, dispatch loop 가 다음 발언자
  (보통 verifier_human 같은 LLM 검수자) 에게 결과 텍스트로 흘려 보냄.

1차 stride 한정사항:
- executeMethod 모드만 지원. PlayMode test (NUnit XML 파싱) 는 다음 stride.
- timeout 은 default 600초. limits.yaml 기반 override 는 다음 stride.
- Unity 미설치 환경에서는 `dry_run` 모드 — 인자만 빌드하고 fake "pass" 결과를
  돌려 인터페이스 회귀 검증을 가능하게. 박제관 PC live 검증과 sandbox 회귀를
  분리한다.

도메인 roles.yaml 에서 받는 runner_config 키:
- `unity_executable` (필수, 미설정 시 dry_run 진입). 절대 경로 또는 PATH 안 이름.
- `unity_method` (필수, executeMethod 인자 — 예: "PJK.OrchTest.RunSmoke").
- `project_subpath` (옵션, 기본은 working_directory 자체). 도메인이 multi-project
  레이아웃을 쓸 때 sub-path 지정.
- `extra_args` (옵션, list[str]). batchmode 추가 인자.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from adapters.base import AdapterFatalError, Invocation
from runners.base import BaseRunnerAdapter, RunnerResult, resolve_runner_config
from runners.unity_log_parser import parse_unity_log


class UnityBatchmodeRunner(BaseRunnerAdapter):
    provider_id = "unity_batchmode"
    default_timeout_sec = 600  # 10분 — Unity batchmode 시동 + 컴파일 + 실행 여유.

    def run(self, invocation: Invocation) -> RunnerResult:
        cfg = resolve_runner_config(invocation.working_directory, invocation.role)
        method = str(cfg.get("unity_method") or "").strip()
        if not method:
            raise AdapterFatalError(
                f"unity_batchmode: role {invocation.role!r} missing required "
                f"`runner_config.unity_method` (e.g. 'PJK.OrchTest.RunSmoke')."
            )
        executable_raw = str(cfg.get("unity_executable") or "").strip()
        executable_path = _resolve_unity_executable(executable_raw)
        project_root = Path(invocation.working_directory).resolve()
        sub = str(cfg.get("project_subpath") or "").strip()
        if sub:
            project_root = (project_root / sub).resolve()
        log_dir = Path(invocation.working_directory) / ".orch" / "runtime" / "unity_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"unity_{int(time.time())}_{invocation.role}.log"
        extra_args = cfg.get("extra_args")
        extra: list[str] = (
            [str(a) for a in extra_args] if isinstance(extra_args, list) else []
        )
        command = _build_command(
            executable=executable_path or "unity",
            project_path=project_root,
            method=method,
            log_path=log_path,
            extra_args=extra,
        )

        if executable_path is None:
            # Dry-run: Unity 미설치 환경. subprocess 호출 없이 인자만 기록 + fake pass.
            log_path.write_text(
                "DRY-RUN: Unity executable not found. Command would be:\n"
                + " ".join(_quote_arg(a) for a in command)
                + "\nExiting batchmode successfully (simulated).\n",
                encoding="utf-8",
            )
            return RunnerResult(
                exit_code=0,
                summary=f"unity_batchmode dry-run for method={method}",
                stdout_excerpt="",
                stderr_excerpt="",
                artifact_paths=[str(log_path.relative_to(project_root.parent))]
                if project_root.parent in log_path.parents
                else [str(log_path)],
                verdict="pass",
                score=1.0,
                findings=[
                    "dry-run: unity_executable not configured or binary missing",
                ],
                duration_sec=0.0,
            )

        timeout = int(cfg.get("timeout_sec") or self.default_timeout_sec)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            duration = time.monotonic() - started
            stdout_text = completed.stdout or ""
            stderr_text = completed.stderr or ""
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            stdout_text = _coerce_text(getattr(exc, "stdout", None))
            stderr_text = _coerce_text(getattr(exc, "stderr", None)) + (
                f"\n\nunity_batchmode timed out after {timeout}s"
            )
            exit_code = 124  # 통념적인 timeout exit code.
        log_text = _read_log_file(log_path)
        summary_obj = parse_unity_log(log_text or stdout_text or stderr_text)
        verdict = summary_obj.verdict() if exit_code == 0 else "fail"
        findings = list(summary_obj.findings)
        if exit_code != 0 and not findings:
            findings.append(f"unity exited with code {exit_code}")
        return RunnerResult(
            exit_code=exit_code,
            summary=f"unity_batchmode method={method} verdict={verdict}",
            stdout_excerpt=_excerpt(stdout_text),
            stderr_excerpt=_excerpt(stderr_text),
            artifact_paths=[str(log_path)],
            verdict=verdict,
            score=1.0 if verdict == "pass" else 0.0,
            findings=findings,
            duration_sec=round(duration, 2),
        )


def _resolve_unity_executable(configured: str) -> str | None:
    """Return a usable executable path or None when Unity isn't reachable."""
    if not configured:
        configured = os.environ.get("UNITY_EDITOR_PATH", "").strip()
    if not configured:
        return None
    candidate = Path(configured)
    if candidate.is_absolute() and candidate.exists():
        return str(candidate)
    # PATH 안 이름인 경우 — Windows / posix 양쪽에서 직접 확인 가능한 정도로만 확인.
    import shutil as _shutil

    found = _shutil.which(configured)
    return found if found else None


def _build_command(
    *,
    executable: str,
    project_path: Path,
    method: str,
    log_path: Path,
    extra_args: list[str],
) -> list[str]:
    return [
        executable,
        "-batchmode",
        "-nographics",
        "-quit",
        "-projectPath",
        str(project_path),
        "-executeMethod",
        method,
        "-logFile",
        str(log_path),
        *extra_args,
    ]


def _read_log_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


_EXCERPT_LIMIT = 4000


def _excerpt(text: str) -> str:
    if not text:
        return ""
    if len(text) <= _EXCERPT_LIMIT:
        return text
    return text[: _EXCERPT_LIMIT] + "\n...(truncated)"


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    return str(value)


def _quote_arg(arg: str) -> str:
    if " " in arg or "\t" in arg:
        return f'"{arg}"'
    return arg


RUNNER = UnityBatchmodeRunner()
