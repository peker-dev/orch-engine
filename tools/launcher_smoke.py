"""launcher.py 스모크.

scripted profile 로 init -> run -> 종료 흐름을 stdin 모킹으로 1회 검증.
live LLM 호출 없음. tempdir 사용.
"""
from __future__ import annotations

import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

# repo 루트(=orch-engine/)를 import path 에 추가해서 직접 실행해도 동작하게 한다.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import launcher, store  # noqa: E402


def case_menu_init_run_quit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "launcher_smoke_target"
        target.mkdir()

        script = "\n".join([
            "1",                # menu: init
            str(target),        # init: target
            "smoke goal",       # init: goal
            "scripted",         # init: profile
            "2",                # menu: run
            str(target),        # run: target
            "1",                # run: max-cycles
            "3",                # menu: 종료
            "",
        ])

        original_stdin = sys.stdin
        sys.stdin = io.StringIO(script)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = launcher.main([])
        finally:
            sys.stdin = original_stdin
        output = buf.getvalue()

        assert rc == 0, f"launcher.main rc={rc} (output: {output!r})"

        orch = store.orch_dir(target)
        assert orch.exists(), f".orch not created at {orch}"

        project = store.load_json(store.paths(target)["project"])
        assert project["goal"] == "smoke goal", project

        roles = store.load_json(store.paths(target)["roles"])
        assert roles.get("adapters", {}).get("planner") == "scripted", roles

        assert "initialised" in output, "init confirmation missing in stdout"


def case_run_without_init_returns_to_menu() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "no_orch_here"
        target.mkdir()

        script = "\n".join([
            "2",                # menu: run (without init)
            str(target),        # run: target (no .orch)
            "3",                # menu: 종료
            "",
        ])

        original_stdin = sys.stdin
        sys.stdin = io.StringIO(script)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = launcher.main([])
        finally:
            sys.stdin = original_stdin
        output = buf.getvalue()

        assert rc == 0, f"launcher.main rc={rc}"
        assert ".orch 가 없습니다" in output, f"missing guidance in stdout: {output!r}"


def main() -> int:
    cases = [
        ("menu_init_run_quit", case_menu_init_run_quit),
        ("run_without_init_returns_to_menu", case_run_without_init_returns_to_menu),
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
