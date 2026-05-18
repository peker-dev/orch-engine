"""어댑터 RuntimeError 메시지 본문 포맷 표준화 smoke.

검증 포인트 (cmd_run E2 호출자가 그대로 노출 가능한 1줄 원인 메시지):
1. claude_cli rc!=0 → `claude_cli role={role} subprocess returned rc={rc}: <stderr>`
2. claude_cli empty stdout → `claude_cli role={role} empty stdout`
3. codex_cli rc!=0 → `codex_cli role={role} subprocess returned rc={rc}: <stderr>`
4. codex_cli empty stdout → `codex_cli role={role} empty stdout`

`subprocess.run` 만 monkeypatch 로 `CompletedProcess` 주입. live 호출 0.
어댑터 retry / 도구 정책 / 새 예외 클래스 / 구조화 로그 무관 — 메시지 본문만 검증.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters import claude_cli, codex_cli  # noqa: E402


def _claude_adapter() -> claude_cli.ClaudeCliAdapter:
    with mock.patch.object(claude_cli.shutil, "which", return_value="/fake/claude"):
        return claude_cli.ClaudeCliAdapter(timeout=1)


def _codex_adapter() -> codex_cli.CodexCliAdapter:
    with mock.patch.object(codex_cli.shutil, "which", return_value="/fake/codex"):
        return codex_cli.CodexCliAdapter(timeout=1)


def _invoke_and_capture(adapter, role: str) -> str:
    try:
        adapter.invoke(role, {"goal": "x", "previous_review": None})
    except RuntimeError as e:
        return str(e)
    raise AssertionError("expected RuntimeError, got success")


def case_claude_rc_nonzero() -> None:
    fake = subprocess.CompletedProcess(
        args=["claude"], returncode=7, stdout="", stderr="boom: quota exhausted"
    )
    adapter = _claude_adapter()
    with mock.patch.object(claude_cli.subprocess, "run", return_value=fake):
        msg = _invoke_and_capture(adapter, "builder")
    assert msg.startswith("claude_cli role=builder "), msg
    assert "subprocess returned rc=7" in msg, msg
    assert "boom: quota exhausted" in msg, msg


def case_claude_empty_stdout() -> None:
    fake = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout="   ", stderr=""
    )
    adapter = _claude_adapter()
    with mock.patch.object(claude_cli.subprocess, "run", return_value=fake):
        msg = _invoke_and_capture(adapter, "verifier")
    assert msg == "claude_cli role=verifier empty stdout", msg


def case_codex_rc_nonzero() -> None:
    fake = subprocess.CompletedProcess(
        args=["codex"], returncode=3, stdout="", stderr="auth error"
    )
    adapter = _codex_adapter()
    with mock.patch.object(codex_cli.subprocess, "run", return_value=fake):
        msg = _invoke_and_capture(adapter, "planner")
    assert msg.startswith("codex_cli role=planner "), msg
    assert "subprocess returned rc=3" in msg, msg
    assert "auth error" in msg, msg


def case_codex_empty_stdout() -> None:
    fake = subprocess.CompletedProcess(
        args=["codex"], returncode=0, stdout="", stderr=""
    )
    adapter = _codex_adapter()
    with mock.patch.object(codex_cli.subprocess, "run", return_value=fake):
        msg = _invoke_and_capture(adapter, "orchestrator")
    assert msg == "codex_cli role=orchestrator empty stdout", msg


def main() -> int:
    cases = [
        ("claude_rc_nonzero", case_claude_rc_nonzero),
        ("claude_empty_stdout", case_claude_empty_stdout),
        ("codex_rc_nonzero", case_codex_rc_nonzero),
        ("codex_empty_stdout", case_codex_empty_stdout),
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
