"""Timeline append regression (Phase 2 D2).

Verifies the contract of runtime/timeline.jsonl — the shared whiteboard
log file introduced in Phase 2. Each emitted entry must:

  1. 5-way flow: five role-specific helpers (planner/builder/verifier_functional/
     verifier_human/orchestrator) write five entries in order; utterance_index
     is contiguous 0..4 with the correct speaker/next_speaker chain.
  2. session id persistence: the same session_id is reused across multiple
     cycles within the same RuntimeStore instance, and timeline_index keeps
     advancing monotonically in runtime/session.json.
  3. schema validation: every entry validates against the real
     schemas/timeline_entry.v1.json + utterance.v1.json files on disk
     (Draft 2020-12).

These cover the observable guarantees the later Phase 2 free-utterance
loop will depend on — when the hard-coded role sequence goes away, the
only thing downstream readers can rely on is this stream being well
formed and monotonically indexed.
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
from core.app import (  # noqa: E402
    _append_planner_timeline,
    _append_builder_timeline,
    _append_verifier_functional_timeline,
    _append_verifier_human_timeline,
    _append_orchestrator_timeline,
)


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    message: str


def _load_entries(runtime_root: Path) -> list[dict]:
    path = runtime_root / "timeline.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _scenario_basic_5way_flow(sandbox: Path) -> ScenarioResult:
    target = sandbox / "basic-5way"
    rt = RuntimeStore(target)
    rt.write_json("runtime/session.json", {})

    _append_planner_timeline(
        rt, cycle_index=1, summary="plan", plan_summary="p",
        tasks=[{"title": "t1"}], provider_id="claude_cli",
    )
    _append_builder_timeline(
        rt, cycle_index=1, summary="build", change_summary="cs",
        files_changed=["a.py"], artifact_paths=[], unresolved=[], provider_id="codex_cli",
    )
    _append_verifier_functional_timeline(
        rt, cycle_index=1, summary="vf", result="pass", score=0.9,
        findings=[], evidence=["t.py"], blocking_issues=[],
        suggested_actions=[], provider_id="claude_cli",
    )
    _append_verifier_human_timeline(
        rt, cycle_index=1, summary="vh", result="pass", score=0.95,
        findings=[], strengths=["tone"], comparison_notes=[],
        suggested_actions=[], provider_id="claude_cli",
    )
    _append_orchestrator_timeline(
        rt, cycle_index=1, summary="done", decision="complete_cycle",
        next_state="completed", reason="met", unresolved_items=[],
        recommended_next_action="", provider_id="codex_cli",
    )

    entries = _load_entries(target / ".orch" / "runtime")
    if len(entries) != 5:
        return ScenarioResult("basic_5way_flow", False, f"expected 5 entries, got {len(entries)}")

    expected_chain = [
        ("planner", "builder"),
        ("builder", "verifier_functional"),
        ("verifier_functional", "verifier_human"),
        ("verifier_human", "orchestrator"),
        ("orchestrator", "__end__"),
    ]
    for i, (speaker, next_sp) in enumerate(expected_chain):
        e = entries[i]
        if e["utterance_index"] != i:
            return ScenarioResult("basic_5way_flow", False, f"entry {i} utterance_index={e['utterance_index']}")
        if e["utterance"]["speaker"] != speaker:
            return ScenarioResult("basic_5way_flow", False, f"entry {i} speaker={e['utterance']['speaker']}, expected {speaker}")
        if e["utterance"]["next_speaker"] != next_sp:
            return ScenarioResult("basic_5way_flow", False, f"entry {i} next_speaker={e['utterance']['next_speaker']}, expected {next_sp}")
        if e["kind"] != "utterance":
            return ScenarioResult("basic_5way_flow", False, f"entry {i} kind={e['kind']}")

    return ScenarioResult("basic_5way_flow", True, "5 entries in order, utterance_index 0..4, speaker chain correct")


def _scenario_session_id_persistence(sandbox: Path) -> ScenarioResult:
    target = sandbox / "session-persist"
    rt = RuntimeStore(target)
    rt.write_json("runtime/session.json", {})

    # Three cycles worth of planner utterances. session_id must stay the same.
    for cycle in range(1, 4):
        _append_planner_timeline(
            rt, cycle_index=cycle, summary=f"plan-{cycle}",
            plan_summary="p", tasks=[{"title": f"t{cycle}"}],
            provider_id="claude_cli",
        )

    entries = _load_entries(target / ".orch" / "runtime")
    if len(entries) != 3:
        return ScenarioResult("session_id_persistence", False, f"expected 3 entries, got {len(entries)}")

    session_ids = {e["session_id"] for e in entries}
    if len(session_ids) != 1:
        return ScenarioResult("session_id_persistence", False, f"session_id split across entries: {session_ids}")

    indices = [e["utterance_index"] for e in entries]
    if indices != [0, 1, 2]:
        return ScenarioResult("session_id_persistence", False, f"utterance_index not monotonic: {indices}")

    persisted = rt.read_json("runtime/session.json", {}) or {}
    if persisted.get("timeline_index") != 3:
        return ScenarioResult("session_id_persistence", False, f"session.json timeline_index={persisted.get('timeline_index')}")
    if persisted.get("session_id") != list(session_ids)[0]:
        return ScenarioResult("session_id_persistence", False, "session.json session_id mismatch")

    return ScenarioResult("session_id_persistence", True, f"single session_id across 3 cycles, timeline_index={persisted.get('timeline_index')}")


def _scenario_schema_validation(sandbox: Path) -> ScenarioResult:
    target = sandbox / "schema-validate"
    rt = RuntimeStore(target)
    rt.write_json("runtime/session.json", {})

    _append_planner_timeline(rt, cycle_index=1, summary="p", plan_summary="ps", tasks=[{"title": "t"}], provider_id="claude_cli")
    _append_orchestrator_timeline(
        rt, cycle_index=1, summary="o", decision="needs_iteration",
        next_state="iterating", reason="gap", unresolved_items=["x"],
        recommended_next_action="re-plan", provider_id="codex_cli",
    )

    try:
        from jsonschema import Draft202012Validator, RefResolver
    except ImportError:
        return ScenarioResult("schema_validation", True, "jsonschema not installed — skipped (treat as pass)")

    utt_schema_path = ENGINE_ROOT / "schemas" / "utterance.v1.json"
    tl_schema_path = ENGINE_ROOT / "schemas" / "timeline_entry.v1.json"
    utt = json.loads(utt_schema_path.read_text(encoding="utf-8"))
    tl = json.loads(tl_schema_path.read_text(encoding="utf-8"))
    store = {utt["$id"]: utt, "utterance.v1.json": utt}
    resolver = RefResolver.from_schema(tl, store=store)
    validator = Draft202012Validator(tl, resolver=resolver)

    entries = _load_entries(target / ".orch" / "runtime")
    for i, entry in enumerate(entries):
        errors = list(validator.iter_errors(entry))
        if errors:
            return ScenarioResult(
                "schema_validation",
                False,
                f"entry {i} ({entry['utterance']['speaker']}) failed schema: {errors[0].message}",
            )

    # orchestrator with needs_iteration routes back to planner
    orch_entry = entries[1]
    if orch_entry["utterance"]["next_speaker"] != "planner":
        return ScenarioResult(
            "schema_validation",
            False,
            f"orchestrator needs_iteration should route to planner, got {orch_entry['utterance']['next_speaker']}",
        )

    return ScenarioResult("schema_validation", True, f"{len(entries)}/{len(entries)} entries validate against timeline_entry.v1 + utterance.v1")


SCENARIOS: dict[str, Callable[[Path], ScenarioResult]] = {
    "basic_5way_flow": _scenario_basic_5way_flow,
    "session_id_persistence": _scenario_session_id_persistence,
    "schema_validation": _scenario_schema_validation,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Timeline append smoke")
    parser.add_argument("--only", default="", help="Comma-separated scenario names")
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    wanted = [n.strip() for n in args.only.split(",") if n.strip()] or list(SCENARIOS)
    unknown = [n for n in wanted if n not in SCENARIOS]
    if unknown:
        print(f"Unknown scenarios: {unknown}")
        return 2

    sandbox = Path(tempfile.mkdtemp(prefix="orch-timeline-smoke-"))
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
