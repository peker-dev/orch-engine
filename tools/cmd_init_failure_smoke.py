"""cmd_init 실패 경로 stderr 메시지 + exit code smoke.

검증 포인트 (E2 동형 이식 — init 측 실패 안내 표준):
1. 기존 .orch/ 존재 (ValueError) → stderr 2줄 (reason + next-step) + return 2.
2. 권한 거부 (PermissionError, store.init_orch monkeypatch) → stderr 2줄 + return 2.
3. 성공 경로 회귀 → stderr 빈 상태, stdout 정상 메시지 + JSON, return 0.

어댑터 파일은 수정하지 않고, store.init_orch 만 monkeypatch 로 PermissionError 주입.
live LLM 호출 0. tempdir 사용.
"""
from __future__ import annotations

import argparse
import io
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import app, store  # noqa: E402


def _init_args(target: Path, profile: str = "scripted") -> argparse.Namespace:
    return argparse.Namespace(
        target=str(target),
        goal="x",
        profile=profile,
        builder_model=None,
    )


def _run_init(target: Path, profile: str = "scripted") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = app.cmd_init(_init_args(target, profile=profile))
    return rc, out.getvalue(), err.getvalue()


def case_existing_orch_failure() -> None:
    """이미 .orch/ 가 있는 폴더에 다시 init → ValueError 잡혀 2줄 + rc=2."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "proj"
        # 첫 init 은 성공해야 한다 (사전 조건).
        rc0, _, err0 = _run_init(target)
        assert rc0 == 0, f"first init should succeed, got rc={rc0}, stderr={err0!r}"

        rc, out, err = _run_init(target)
        assert rc == 2, f"expected rc=2 on re-init, got {rc}; stderr={err!r}"
        assert "실패" in err, f"missing reason marker in stderr: {err!r}"
        assert "다음 조치" in err, f"missing next-step line in stderr: {err!r}"
        assert "이미 초기화" in err, f"missing existing-orch hint in stderr: {err!r}"
        assert ".orch" in err, f"missing .orch path in stderr: {err!r}"
        # 성공 시 stdout 라인은 새어나오면 안 됨.
        assert "initialised" not in out, f"success line leaked on failure: {out!r}"


def case_permission_error_failure() -> None:
    """store.init_orch 가 PermissionError raise → 2줄 + rc=2."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "proj"
        with mock.patch.object(
            app.store, "init_orch",
            side_effect=PermissionError("[Errno 13] Permission denied: 'C:/locked/.orch'"),
        ):
            rc, out, err = _run_init(target)
        assert rc == 2, f"expected rc=2 on permission error, got {rc}; stderr={err!r}"
        assert "실패" in err and "다음 조치" in err, err
        assert "권한" in err, f"missing permission hint in stderr: {err!r}"
        assert "Permission denied" in err, f"original PermissionError text should be preserved: {err!r}"
        assert "initialised" not in out, f"success line leaked on failure: {out!r}"


def case_success_unchanged() -> None:
    """성공 경로: stderr 비고, stdout 에 'initialised' + JSON 보존, return 0."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "proj"
        rc, out, err = _run_init(target)
        assert rc == 0, f"expected rc=0 on success path, got {rc}; stderr={err!r}"
        assert err == "", f"stderr should be empty on success, got {err!r}"
        assert "initialised" in out, f"stdout missing initialised line: {out!r}"
        assert '"reason"' in out and '"initialised"' in out, f"stdout missing JSON payload: {out!r}"
        # 실제로 .orch 폴더가 생겼는지 1줄 확인 (회귀 가드).
        assert store.orch_dir(target).exists(), "expected .orch/ to be created on success"


def main() -> int:
    cases = [
        ("existing_orch_failure", case_existing_orch_failure),
        ("permission_error_failure", case_permission_error_failure),
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
