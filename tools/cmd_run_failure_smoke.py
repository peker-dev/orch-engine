"""cmd_run 실패 경로 stderr 메시지 + exit code smoke.

검증 포인트 (M1 E2 실패 안내 표준):
1. FileNotFoundError (어댑터 CLI 미설치 모사) → stderr 2줄 (reason + next-step) + return 2.
2. subprocess.TimeoutExpired (timeout 2회 후) → stderr 2줄 + return 2.
3. RuntimeError (어댑터 raise) → stderr 2줄 + return 2.
4. 성공 경로 회귀 → stderr 빈 상태, stdout 에 JSON + `artifacts:` 라인 유지, return 0.

어댑터 파일은 수정하지 않고, `core.app.loop.run` 만 monkeypatch 로 예외 주입.
live LLM 호출 0. tempdir 사용.
"""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import app, store  # noqa: E402


def _init_target(tmp: str) -> Path:
    target = Path(tmp) / "proj"
    store.init_orch(target, goal="x")
    return target


def _run_cmd(target: Path, max_cycles: int = 1) -> tuple[int, str, str]:
    args = argparse.Namespace(target=str(target), max_cycles=max_cycles)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = app.cmd_run(args)
    return rc, out.getvalue(), err.getvalue()


def case_filenotfound_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = _init_target(tmp)
        with mock.patch.object(
            app.loop, "run",
            side_effect=FileNotFoundError("'claude' CLI not found on PATH"),
        ):
            rc, out, err = _run_cmd(target)
        assert rc == 2, f"expected rc=2, got {rc}; stderr={err!r}"
        assert "실패" in err, f"missing reason marker in stderr: {err!r}"
        assert "다음 조치" in err, f"missing next-step line in stderr: {err!r}"
        assert "claude" in err and "PATH" in err, f"missing CLI hint in stderr: {err!r}"
        # 성공 경로의 JSON / artifacts 라인은 실패 시 출력되면 안 됨
        assert "artifacts:" not in out, f"artifacts line leaked on failure: {out!r}"


def case_timeout_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = _init_target(tmp)
        exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=600)
        with mock.patch.object(app.loop, "run", side_effect=exc):
            rc, out, err = _run_cmd(target)
        assert rc == 2, f"expected rc=2, got {rc}; stderr={err!r}"
        assert "실패" in err and "다음 조치" in err, err
        assert "timeout" in err.lower(), f"missing timeout in stderr: {err!r}"
        assert "600" in err, f"missing limit value in stderr: {err!r}"


def case_runtime_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = _init_target(tmp)
        with mock.patch.object(
            app.loop, "run",
            side_effect=RuntimeError("claude CLI failed rc=1: something bad"),
        ):
            rc, out, err = _run_cmd(target)
        assert rc == 2, f"expected rc=2, got {rc}; stderr={err!r}"
        assert "실패" in err and "다음 조치" in err, err
        assert "어댑터 호출 오류" in err, err
        assert "claude CLI failed" in err, f"original RuntimeError text should be preserved: {err!r}"


def case_success_unchanged() -> None:
    """성공 경로: stderr 비고, stdout JSON + artifacts 라인 보존, return 0."""
    with tempfile.TemporaryDirectory() as tmp:
        target = _init_target(tmp)
        # scripted profile default 가 2 사이클 데모 → max_cycles=2 로 complete 까지 간다.
        rc, out, err = _run_cmd(target, max_cycles=2)
        assert rc == 0, f"expected rc=0 on success path, got {rc}; stderr={err!r}"
        assert err == "", f"stderr should be empty on success, got {err!r}"
        assert "artifacts:" in out, f"stdout missing artifacts line: {out!r}"
        assert '"reason"' in out, f"stdout missing JSON result: {out!r}"


def main() -> int:
    cases = [
        ("filenotfound_failure", case_filenotfound_failure),
        ("timeout_failure", case_timeout_failure),
        ("runtime_failure", case_runtime_failure),
        ("success_unchanged", case_success_unchanged),
    ]
    failed = 0
    for name, fn in cases:
        try:
            fn()
            print(f"  ok  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
