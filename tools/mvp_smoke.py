"""MVP smoke test.

검증 포인트 (핵심):
1. `.orch/` 가 정상 생성되고 goal 이 저장된다.
2. Planner → Builder → Verifier → Orchestrator 순서로 2 사이클이 자동으로 돈다.
3. cycle 2 의 Planner 가 받은 context 의 `previous_review` 에 cycle 1 의 Verifier 리뷰가
   그대로 들어 있다. (자동 개선 루프의 핵심)
4. cycle 2 종료 후 session 이 'completed' 상태가 된다 (default scripts 기준).
5. `.orch/STOP` 을 만들면 다음 cycle 실행 전 멈춘다.

assert 기반 평면 스크립트. 실패 시 AssertionError + 상세 메시지.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# repo 루트(=orch-engine/)를 import path 에 추가해서 직접 실행해도 동작하게 한다.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters.scripted import ScriptedAdapter  # noqa: E402
from core import loop, store  # noqa: E402


def _planner_calls(adapter: ScriptedAdapter) -> list[dict]:
    return [c for c in adapter.calls if c["role"] == "planner"]


def case_two_cycle_feedback_loop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "proj"
        store.init_orch(target, goal="write a short report on apples")

        # init 검증
        project = store.load_json(store.paths(target)["project"])
        assert project["goal"] == "write a short report on apples", project

        adapter = ScriptedAdapter()
        result = loop.run(str(target), adapter, max_cycles=2)

        planner_calls = _planner_calls(adapter)
        assert len(planner_calls) == 2, f"expected 2 planner calls, got {len(planner_calls)}"

        # cycle 1 planner: previous_review 는 None
        c1 = planner_calls[0]
        assert c1["cycle"] == 1
        assert c1["context"]["previous_review"] is None, c1["context"]

        # cycle 2 planner: previous_review 가 cycle 1 verifier 결과여야 한다
        c2 = planner_calls[1]
        assert c2["cycle"] == 2
        prev = c2["context"]["previous_review"]
        assert prev is not None, "cycle 2 planner did not receive previous_review"
        assert prev["verdict"] == "needs_iteration", prev
        assert prev["cycle"] == 1, prev
        assert prev["improvements"], "previous_review.improvements should not be empty"

        # 종료 사유와 session 상태
        assert result["reason"] == "complete", result
        session = store.load_json(store.paths(target)["session"])
        assert session["state"] == "completed", session
        assert session["cycle"] == 2, session

        # events.jsonl 에 8개 역할 이벤트 + (옵션) 가 기록됐는지
        events = store.read_events(target)
        roles = [e.get("role") for e in events if "role" in e]
        assert roles.count("planner") == 2
        assert roles.count("builder") == 2
        assert roles.count("verifier") == 2
        assert roles.count("orchestrator") == 2


def case_stop_file_halts_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "proj"
        store.init_orch(target, goal="anything")
        # 시작 전 STOP
        (store.orch_dir(target) / "STOP").write_text("stop", encoding="utf-8")

        adapter = ScriptedAdapter()
        result = loop.run(str(target), adapter, max_cycles=2)

        assert result["reason"] == "stopped", result
        assert _planner_calls(adapter) == [], "planner should not run when STOP is present"
        session = store.load_json(store.paths(target)["session"])
        assert session["state"] == "stopped", session


def main() -> int:
    cases = [
        ("two_cycle_feedback_loop", case_two_cycle_feedback_loop),
        ("stop_file_halts_run", case_stop_file_halts_run),
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
