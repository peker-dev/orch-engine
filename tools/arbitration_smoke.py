"""Arbitration / next-speaker routing smoke.

These scenarios lock in the orchestrator's timeline-side next_speaker
mapping from the legacy decision enum (`complete_cycle` / `needs_iteration`
/ `blocked`). Even after the Phase 2 P1-5-B free-utterance loop landed,
the orchestrator's legacy decision is still what `_append_orchestrator_timeline`
uses to populate `next_speaker` in its timeline entry, so this smoke
remains the canonical guard against accidental regressions in that mapping.

Scenarios:

  1. orchestrator_decision_maps_to_next_speaker — three decisions map to
     the expected timeline next_speaker:
       complete_cycle → __end__
       needs_iteration → planner
       blocked → __end__
  2. unknown_decision_defaults_to_planner — if an orchestrator entry is
     emitted with an empty / unknown decision (e.g. legacy bug path),
     the timeline routing falls back to 'planner' rather than crashing.

The actual run_cycle-level free-utterance routing (D5 rules: follow
utterance.next_speaker, force orchestrator on declare_done) is covered
by cycle_e2e_smoke scenarios `utterance_next_speaker_skips_legacy_chain`
and `declare_done_forces_orchestrator`.

TODO (future):
  - arbitration=agree + next_speaker=__end__ ends session (currently
    "orchestrator decision = cycle end" covers this implicitly).
  - arbitration=disagree resumes with named next_speaker in the SAME
    cycle (engine currently terminates the cycle after orchestrator
    runs; disagree-reopen would require run_cycle to resume the loop).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from core.runtime_store import RuntimeStore  # noqa: E402
from core.app import _append_orchestrator_timeline, _ORCHESTRATOR_NEXT_SPEAKER  # noqa: E402


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    message: str


def _read_last_entry(runtime_root: Path) -> dict | None:
    path = runtime_root / "timeline.jsonl"
    if not path.exists():
        return None
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def _scenario_decision_maps_to_next_speaker(sandbox: Path) -> ScenarioResult:
    expectations = {
        "complete_cycle": "__end__",
        "needs_iteration": "planner",
        "blocked": "__end__",
    }

    for decision, expected in expectations.items():
        target = sandbox / f"decision-{decision}"
        rt = RuntimeStore(target)
        rt.write_json("runtime/session.json", {})
        _append_orchestrator_timeline(
            rt, cycle_index=1, summary="judge",
            decision=decision, next_state="completed" if decision == "complete_cycle" else decision,
            reason="r", unresolved_items=[], recommended_next_action="",
            provider_id="codex_cli",
        )
        entry = _read_last_entry(target / ".orch" / "runtime")
        if entry is None:
            return ScenarioResult("decision_maps_to_next_speaker", False, f"{decision}: no timeline entry emitted")
        actual = entry["utterance"]["next_speaker"]
        if actual != expected:
            return ScenarioResult(
                "decision_maps_to_next_speaker",
                False,
                f"{decision} → {actual}, expected {expected}",
            )
        if _ORCHESTRATOR_NEXT_SPEAKER.get(decision) != expected:
            return ScenarioResult(
                "decision_maps_to_next_speaker",
                False,
                f"module table desync: _ORCHESTRATOR_NEXT_SPEAKER[{decision}]={_ORCHESTRATOR_NEXT_SPEAKER.get(decision)}",
            )

    return ScenarioResult(
        "decision_maps_to_next_speaker",
        True,
        "3 decisions (complete_cycle/needs_iteration/blocked) route as expected",
    )


def _scenario_unknown_decision_defaults_to_planner(sandbox: Path) -> ScenarioResult:
    target = sandbox / "unknown-decision"
    rt = RuntimeStore(target)
    rt.write_json("runtime/session.json", {})

    # Legacy engine should never pass an unknown decision here (run_orchestrator
    # raises before reaching timeline). This smoke pins the timeline-side fallback
    # so future refactors don't accidentally drop the guard.
    _append_orchestrator_timeline(
        rt, cycle_index=1, summary="edge",
        decision="", next_state="", reason="", unresolved_items=[],
        recommended_next_action="", provider_id="codex_cli",
    )
    entry = _read_last_entry(target / ".orch" / "runtime")
    if entry is None:
        return ScenarioResult("unknown_decision_defaults_to_planner", False, "no entry emitted")
    actual = entry["utterance"]["next_speaker"]
    if actual != "planner":
        return ScenarioResult(
            "unknown_decision_defaults_to_planner",
            False,
            f"empty decision routed to {actual}, expected 'planner'",
        )
    return ScenarioResult(
        "unknown_decision_defaults_to_planner",
        True,
        "empty / unknown decision falls back to planner (safe default)",
    )


SCENARIOS: dict[str, Callable[[Path], ScenarioResult]] = {
    "decision_maps_to_next_speaker": _scenario_decision_maps_to_next_speaker,
    "unknown_decision_defaults_to_planner": _scenario_unknown_decision_defaults_to_planner,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Arbitration / next-speaker routing smoke")
    parser.add_argument("--only", default="", help="Comma-separated scenario names")
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    wanted = [n.strip() for n in args.only.split(",") if n.strip()] or list(SCENARIOS)
    unknown = [n for n in wanted if n not in SCENARIOS]
    if unknown:
        print(f"Unknown scenarios: {unknown}")
        return 2

    sandbox = Path(tempfile.mkdtemp(prefix="orch-arbitration-smoke-"))
    print(f"Sandbox: {sandbox}")
    results: list[ScenarioResult] = []
    try:
        for name in wanted:
            print(f"\n=== {name} ===")
            try:
                result = SCENARIOS[name](sandbox)
            except Exception as exc:  # noqa: BLE001
                result = ScenarioResult(name, False, f"scenario raised: {exc!r}")
            results.append(result)
            status = "OK" if result.ok else "FAIL"
            print(f"[{status}] {name}: {result.message}")
    finally:
        if not args.keep_temp:
            shutil.rmtree(sandbox, ignore_errors=True)

    print("\nSummary\n-------")
    for r in results:
        status = "OK  " if r.ok else "FAIL"
        print(f"  {status}  {r.name}: {r.message}")
    passed = sum(1 for r in results if r.ok)
    total = len(results)
    print(f"{passed}/{total} scenarios passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
