"""ClaudeCliAdapter timeout retry smoke.

검증 포인트:
1. subprocess.run 이 TimeoutExpired 한 번 -> 정상 반환 시퀀스일 때, invoke 가 결과를
   반환하고 호출 횟수가 정확히 2회 (1회차 실패 + 1회 재시도).
2. subprocess.run 이 TimeoutExpired 두 번 연속이면 invoke 가 TimeoutExpired 를 raise
   하고, 호출 횟수는 정확히 2회 (재시도가 1회로 제한된다 = 무한 재시도 아님).

assert 기반 평면 스크립트. mvp_smoke 와 같은 형식.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters import claude_cli  # noqa: E402


_FAKE_VERIFIER_RESULT = {
    "verdict": "needs_iteration",
    "summary": "fake",
    "issues": ["x"],
    "improvements": ["y"],
}

_FAKE_STDOUT = json.dumps({
    "structured_output": _FAKE_VERIFIER_RESULT,
    "is_error": False,
    "usage": {},
    "total_cost_usd": 0.0,
    "duration_ms": 0,
})


def _make_adapter() -> claude_cli.ClaudeCliAdapter:
    with mock.patch.object(claude_cli.shutil, "which", return_value="/fake/claude"):
        return claude_cli.ClaudeCliAdapter(timeout=1)


def _success_completed() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout=_FAKE_STDOUT, stderr=""
    )


def _run_with_sequence(sequence: list) -> tuple[list, BaseException | dict | None]:
    """subprocess.run 을 시퀀스로 흉내내고 호출 기록과 결과(또는 raise 된 예외)를 반환."""
    calls: list = []

    def fake_run(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        result = sequence.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    adapter = _make_adapter()
    raised: BaseException | None = None
    out: dict | None = None
    with mock.patch.object(claude_cli.subprocess, "run", side_effect=fake_run):
        try:
            out = adapter.invoke("verifier", {"goal": "x", "previous_review": None})
        except BaseException as exc:  # noqa: BLE001
            raised = exc
    return calls, raised if raised is not None else out


def case_retry_once_then_success() -> None:
    sequence = [
        subprocess.TimeoutExpired(cmd=["claude"], timeout=1),
        _success_completed(),
    ]
    calls, result = _run_with_sequence(sequence)

    assert isinstance(result, dict), f"expected dict result, got {type(result).__name__}: {result!r}"
    assert result == _FAKE_VERIFIER_RESULT, result
    assert len(calls) == 2, f"expected exactly 2 subprocess.run calls (1 + 1 retry), got {len(calls)}"


def case_retry_does_not_repeat_on_double_timeout() -> None:
    sequence = [
        subprocess.TimeoutExpired(cmd=["claude"], timeout=1),
        subprocess.TimeoutExpired(cmd=["claude"], timeout=1),
        # 세 번째는 절대 호출되면 안 됨. 호출되면 calls 길이로 잡힌다.
        _success_completed(),
    ]
    calls, result = _run_with_sequence(sequence)

    assert isinstance(result, subprocess.TimeoutExpired), (
        f"expected TimeoutExpired raised after second timeout, got {type(result).__name__}: {result!r}"
    )
    assert len(calls) == 2, (
        f"expected exactly 2 subprocess.run calls (no second retry), got {len(calls)}"
    )


def main() -> int:
    cases = [
        ("retry_once_then_success", case_retry_once_then_success),
        ("retry_does_not_repeat_on_double_timeout", case_retry_does_not_repeat_on_double_timeout),
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
