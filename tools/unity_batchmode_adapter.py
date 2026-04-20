"""Wrapper around Unity -batchmode for the unity domain verifier.

B 2단계 (g) 세 번째 래퍼. unity 도메인 `scoring.blocking_failures` 중 정적
체커로 잡히지 않는 항목(컴파일 에러·런타임 예외·Build 실패)을 Unity 실 프로세스
출력으로 증빙하기 위한 어댑터. W3C / Lighthouse 와 동일한 offline / self-check
/ online 패턴. 실 Unity 호출은 프로젝트 단위 무거운 작업이므로 기본 비활성.

## 모드
- **offline (default)**: no-op.
- **self-check**: 내장 fixture editor.log 스니펫 파싱으로 code path 검증.
- **from-log**: 기존 `editor.log` 파일 경로를 파싱 (`--log path`).
- **online**: `Unity -batchmode -nographics -executeMethod ... -quit -logFile ...`
  실행. `--project-path PATH --method BuildScript.BuildAll` 필수.

## 정규화 출력
    {
      "mode": ...,
      "compile_errors": [ { "file": str, "line": int|None, "code": str, "message": str }, ... ],
      "missing_scripts": [str, ...],            # MissingReferenceException / Missing Script 메시지 목록
      "exceptions":      [str, ...],            # 기타 Exception 요약
      "build_result":    "success" | "failed" | None,
      "raw_excerpt_lines": int                 # 파싱한 줄 수
    }

## 실행
    python -m tools.unity_batchmode_adapter --self-check
    python -m tools.unity_batchmode_adapter --log C:/path/to/editor.log
    python -m tools.unity_batchmode_adapter --online \
        --project-path C:/unity/proj --method BuildScript.BuildAll

## 향후
- `domain_validity_smoke.py` unity 체커에 `--with-unity-log <path>` 옵션으로
  연결. 샘플 폴더에 `evidence/editor.log` 가 있으면 그걸 읽어 `compile_error` /
  `runtime_exception` / `build_failed` violation 으로 반환.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass


DEFAULT_TIMEOUT = 900.0  # Unity batchmode can be slow; 15min default.

# Typical Unity compile error: `Assets\Scripts\X.cs(17,20): error CS1002: ; expected`
_COMPILE_ERROR_RE = re.compile(
    r"^(?P<file>[^\s()]+\.cs)\s*\((?P<line>\d+),(?P<col>\d+)\)\s*:\s*"
    r"error\s+(?P<code>CS\d+)\s*:\s*(?P<message>.+)$",
    re.MULTILINE,
)

_MISSING_SCRIPT_RE = re.compile(
    r"^(?P<msg>MissingReferenceException.*|The referenced script .*? is missing.*)$",
    re.MULTILINE,
)

_EXCEPTION_RE = re.compile(
    r"^(?P<msg>(?!MissingReferenceException)\w+Exception(?::.*)?)$",
    re.MULTILINE,
)

_BUILD_SUCCESS_MARKERS = (
    "Build succeeded",
    "Build completed successfully",
    "BuildReport: build succeeded",
)

_BUILD_FAIL_MARKERS = (
    "Build failed",
    "BuildReport: build failed",
    "Error building Player",
)


def parse_log(text: str) -> dict:
    """Extract compile errors / missing script refs / exceptions / build result."""
    compile_errors: list[dict] = []
    for m in _COMPILE_ERROR_RE.finditer(text):
        compile_errors.append(
            {
                "file": m.group("file"),
                "line": int(m.group("line")),
                "code": m.group("code"),
                "message": m.group("message").strip(),
            }
        )

    missing_scripts: list[str] = []
    for m in _MISSING_SCRIPT_RE.finditer(text):
        msg = m.group("msg").strip()
        if msg not in missing_scripts:
            missing_scripts.append(msg)

    exceptions: list[str] = []
    for m in _EXCEPTION_RE.finditer(text):
        msg = m.group("msg").strip()
        if msg and msg not in exceptions:
            exceptions.append(msg)

    build_result: str | None = None
    if any(marker in text for marker in _BUILD_SUCCESS_MARKERS):
        build_result = "success"
    if any(marker in text for marker in _BUILD_FAIL_MARKERS):
        # fail takes precedence
        build_result = "failed"

    return {
        "compile_errors": compile_errors,
        "missing_scripts": missing_scripts,
        "exceptions": exceptions,
        "build_result": build_result,
        "raw_excerpt_lines": text.count("\n"),
    }


_SELF_CHECK_FIXTURE = """\
[Project] Opening project C:\\proj
Assets\\Scripts\\BrokenBehaviour.cs(5,35): error CS1002: ; expected
Assets\\Scripts\\BrokenBehaviour.cs(10,9): error CS1525: Invalid expression term 'if'
UnityEngine.Debug:LogError
MissingReferenceException: The referenced script (GameManager) is missing.
NullReferenceException: Object reference not set to an instance of an object.
   at Runtime.Main.Start () [0x00000]
Error building Player: 2 compile errors
Build failed
"""


def run_unity(
    project_path: Path,
    method: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Invoke Unity in batchmode and return the log text.

    Raises FileNotFoundError if the Unity binary is not found on PATH.
    """
    unity_bin = shutil.which("Unity") or shutil.which("Unity.exe")
    if not unity_bin:
        raise FileNotFoundError(
            "Unity not on PATH (try adding <Unity>/Editor/Unity.exe or use --log)"
        )
    if not project_path.exists():
        raise FileNotFoundError(f"project path not found: {project_path}")

    with tempfile.TemporaryDirectory(prefix="unity-bm-") as tmp:
        log_path = Path(tmp) / "editor.log"
        cmd = [
            unity_bin,
            "-batchmode",
            "-nographics",
            "-projectPath",
            str(project_path),
            "-executeMethod",
            method,
            "-quit",
            "-logFile",
            str(log_path),
        ]
        subprocess.run(cmd, timeout=timeout, check=False)
        if not log_path.exists():
            return ""
        return log_path.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="Unity batchmode / editor.log adapter")
    parser.add_argument("--log", help="path to an existing editor.log")
    parser.add_argument("--self-check", action="store_true", help="use embedded fixture")
    parser.add_argument("--online", action="store_true", help="invoke Unity batchmode")
    parser.add_argument("--project-path", help="unity project directory (with --online)")
    parser.add_argument("--method", help="executeMethod target (with --online)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args()

    text = ""
    mode = "offline"

    if args.self_check:
        mode = "self-check"
        text = _SELF_CHECK_FIXTURE
    elif args.log:
        mode = "from-log"
        try:
            text = Path(args.log).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[ERR] read failed: {exc}", file=sys.stderr)
            return 2
    elif args.online:
        if not args.project_path or not args.method:
            print(
                "[ERR] --online requires --project-path and --method",
                file=sys.stderr,
            )
            return 2
        mode = "online"
        try:
            text = run_unity(Path(args.project_path), args.method, args.timeout)
        except FileNotFoundError as exc:
            print(f"[ERR] {exc}", file=sys.stderr)
            return 1
        except subprocess.TimeoutExpired:
            print("[ERR] Unity batchmode timed out", file=sys.stderr)
            return 1

    parsed = parse_log(text) if text else {
        "compile_errors": [],
        "missing_scripts": [],
        "exceptions": [],
        "build_result": None,
        "raw_excerpt_lines": 0,
    }
    result = {"mode": mode, **parsed}

    if args.json_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    ce = parsed["compile_errors"]
    ms = parsed["missing_scripts"]
    ex = parsed["exceptions"]
    print(
        f"[{mode}] compile_errors={len(ce)} missing_scripts={len(ms)} "
        f"exceptions={len(ex)} build_result={parsed['build_result']}"
    )
    for err in ce[:10]:
        print(f"  [CS] {err['file']}:{err['line']} {err['code']} — {err['message']}")
    for msg in ms[:5]:
        print(f"  [miss] {msg}")
    for msg in ex[:5]:
        print(f"  [exc] {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
