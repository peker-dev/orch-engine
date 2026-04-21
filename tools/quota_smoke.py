"""Quota detection + usage_wait polling regression (scripted, no live LLM).

Covers the quota-aware infrastructure added in the 15th session
(see `memory/handoff-history-session-14.md` / `handoff.md`):

  1. quota_detected_in_cycle — when a scripted adapter raises
     `AdapterQuotaExceededError` during `run_cycle`, the engine must:
       - exit with rc=3 (distinct from rc=2 for generic adapter errors)
       - set `last_decision="quota_exceeded"` on the session
       - preserve `handoff_pause_count` (quota is recoverable, not fatal)
       - emit an `adapter_quota_exceeded` event rather than `adapter_failed`

  2. usage_wait_polls_until_ready — `wait_for_quota` must keep polling on
     "quota" outcomes and return rc=0 exactly when the probe finally reports
     "ready". It must also write `.orch/runtime/usage_wait_last.json` with
     `result="ready"`.

  3. usage_wait_inconclusive_streak — a run of `max_consecutive_errors`
     non-quota failures must exit rc=2 with `result="inconclusive"` so a
     real failure is never silently treated as "still waiting for quota".

These tests do not touch Claude/Codex CLIs. ScriptedAdapter injects the
quota failure for scenario 1; `_attempt_probe` is monkey-patched for
scenarios 2 and 3 so the polling loop is exercised without any subprocess.
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

from core import app  # noqa: E402
from tools import usage_wait  # noqa: E402
from tools.cycle_e2e_smoke import (  # noqa: E402
    ScenarioResult,
    _init_project,
    _install_scripted_adapters,
    _read_session,
    _run_cycle,
)


def _read_events(target: Path) -> list[dict]:
    events_path = target / ".orch" / "runtime" / "events.jsonl"
    if not events_path.exists():
        return []
    lines = events_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ----------------------------------------------------------------------
# Scenario 1 — quota during run_cycle
# ----------------------------------------------------------------------


def _scenario_quota_detected_in_cycle(sandbox: Path) -> ScenarioResult:
    name = "quota_detected_in_cycle"
    target = sandbox / "quota-in-cycle"
    _init_project(target)
    # Seed handoff_pause_count > 0 so we can assert quota preserves it
    # (the counter would be reset on a non-quota adapter failure).
    session_path = target / ".orch" / "runtime" / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["handoff_pause_count"] = 2
    session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    _install_scripted_adapters(
        [
            {
                "functional": 0.95,
                "human": 0.95,
                "result": "pass",
                # Fail the planner role with a quota error on cycle 1.
                "quota_fail_roles": ["planner"],
            }
        ]
    )
    rc = _run_cycle(target)
    if rc != 3:
        return ScenarioResult(name, False, f"run_cycle rc={rc} != 3 (quota rc)")
    session = _read_session(target)
    if session.get("state") != "blocked":
        return ScenarioResult(
            name, False, f"state={session.get('state')} != blocked"
        )
    if session.get("last_decision") != "quota_exceeded":
        return ScenarioResult(
            name,
            False,
            f"last_decision={session.get('last_decision')} != quota_exceeded",
        )
    if int(session.get("handoff_pause_count", -1) or 0) != 2:
        return ScenarioResult(
            name,
            False,
            "handoff_pause_count was reset — quota must preserve it "
            f"(got {session.get('handoff_pause_count')!r}, expected 2)",
        )
    events = _read_events(target)
    event_types = [e.get("event") for e in events]
    if "adapter_quota_exceeded" not in event_types:
        return ScenarioResult(
            name,
            False,
            f"adapter_quota_exceeded event missing from {event_types}",
        )
    if "adapter_failed" in event_types:
        return ScenarioResult(
            name,
            False,
            "adapter_failed event emitted alongside quota event — must be mutually exclusive",
        )
    return ScenarioResult(
        name,
        True,
        "rc=3, last_decision=quota_exceeded, handoff_pause_count preserved, quota event only",
    )


# ----------------------------------------------------------------------
# Scenario 2 — wait_for_quota polls until ready
# ----------------------------------------------------------------------


def _scenario_usage_wait_polls_until_ready(sandbox: Path) -> ScenarioResult:
    name = "usage_wait_polls_until_ready"
    target = sandbox / "wait-ready"
    _init_project(target)

    scripted_outcomes = [
        ("quota", "scripted quota (attempt 1)"),
        ("quota", "scripted quota (attempt 2)"),
        ("ready", "scripted ready (attempt 3)"),
    ]
    attempts: list[int] = []

    def fake_probe(_target: Path, _role: str, _provider: str) -> tuple[str, str]:
        attempts.append(len(attempts) + 1)
        return scripted_outcomes[min(len(attempts) - 1, len(scripted_outcomes) - 1)]

    original_probe = usage_wait._attempt_probe
    # Disable sleep so the test runs instantly even with poll_interval_sec > 0.
    original_sleep = usage_wait.time.sleep
    usage_wait._attempt_probe = fake_probe  # type: ignore[assignment]
    usage_wait.time.sleep = lambda _seconds: None  # type: ignore[assignment]
    try:
        rc = usage_wait.wait_for_quota(
            target=target,
            probe_role="planner",
            probe_provider="claude_cli",
            poll_interval_sec=30,
            max_hours=1.0,
            max_consecutive_errors=3,
        )
    finally:
        usage_wait._attempt_probe = original_probe  # type: ignore[assignment]
        usage_wait.time.sleep = original_sleep  # type: ignore[assignment]

    if rc != 0:
        return ScenarioResult(name, False, f"wait_for_quota rc={rc} != 0")
    if len(attempts) != 3:
        return ScenarioResult(
            name, False, f"expected 3 probe attempts, got {len(attempts)}"
        )
    result_path = target / ".orch" / "runtime" / "usage_wait_last.json"
    if not result_path.exists():
        return ScenarioResult(name, False, "usage_wait_last.json not written")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if payload.get("result") != "ready":
        return ScenarioResult(
            name, False, f"usage_wait_last.result={payload.get('result')} != ready"
        )
    if int(payload.get("attempts", 0) or 0) != 3:
        return ScenarioResult(
            name,
            False,
            f"usage_wait_last.attempts={payload.get('attempts')} != 3",
        )
    return ScenarioResult(
        name, True, "quota → quota → ready sequence exited rc=0 after 3 attempts"
    )


# ----------------------------------------------------------------------
# Scenario 3 — inconclusive streak exits rc=2
# ----------------------------------------------------------------------


def _scenario_usage_wait_inconclusive_streak(sandbox: Path) -> ScenarioResult:
    name = "usage_wait_inconclusive_streak"
    target = sandbox / "wait-inconclusive"
    _init_project(target)

    def fake_probe(_target: Path, _role: str, _provider: str) -> tuple[str, str]:
        return "inconclusive", "scripted non-quota failure"

    original_probe = usage_wait._attempt_probe
    original_sleep = usage_wait.time.sleep
    usage_wait._attempt_probe = fake_probe  # type: ignore[assignment]
    usage_wait.time.sleep = lambda _seconds: None  # type: ignore[assignment]
    try:
        rc = usage_wait.wait_for_quota(
            target=target,
            probe_role="planner",
            probe_provider="claude_cli",
            poll_interval_sec=30,
            max_hours=1.0,
            max_consecutive_errors=3,
        )
    finally:
        usage_wait._attempt_probe = original_probe  # type: ignore[assignment]
        usage_wait.time.sleep = original_sleep  # type: ignore[assignment]

    if rc != usage_wait.RC_USAGE:
        return ScenarioResult(
            name, False, f"wait_for_quota rc={rc} != RC_USAGE({usage_wait.RC_USAGE})"
        )
    result_path = target / ".orch" / "runtime" / "usage_wait_last.json"
    if not result_path.exists():
        return ScenarioResult(name, False, "usage_wait_last.json not written")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if payload.get("result") != "inconclusive":
        return ScenarioResult(
            name, False, f"usage_wait_last.result={payload.get('result')} != inconclusive"
        )
    if int(payload.get("consecutive_errors", 0) or 0) < 3:
        return ScenarioResult(
            name,
            False,
            f"consecutive_errors={payload.get('consecutive_errors')} should be >=3",
        )
    return ScenarioResult(
        name,
        True,
        "3 consecutive non-quota failures exited rc=2 with result=inconclusive",
    )


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------


SCENARIOS: dict[str, Callable[[Path], ScenarioResult]] = {
    "quota_detected_in_cycle": _scenario_quota_detected_in_cycle,
    "usage_wait_polls_until_ready": _scenario_usage_wait_polls_until_ready,
    "usage_wait_inconclusive_streak": _scenario_usage_wait_inconclusive_streak,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Quota + usage_wait smoke")
    parser.add_argument("--only", default="", help="Comma-separated scenario names")
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    original_build_adapter = app._build_adapter
    # Keep originals so main()'s finally can restore usage_wait monkey-patches
    # even if a scenario exits abnormally before its own finally runs.
    original_attempt_probe = usage_wait._attempt_probe
    original_sleep = usage_wait.time.sleep
    wanted = [name.strip() for name in args.only.split(",") if name.strip()] or list(SCENARIOS)
    unknown = [name for name in wanted if name not in SCENARIOS]
    if unknown:
        print(f"Unknown scenarios: {unknown}")
        return 2

    sandbox = Path(tempfile.mkdtemp(prefix="orch-quota-smoke-"))
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
            app._build_adapter = original_build_adapter  # type: ignore[assignment]
    finally:
        # Belt-and-suspenders: restore every monkey-patch even on abrupt exit
        # (scenario raising SystemExit, caller importing main() programmatically,
        # etc.). Each scenario already restores these in its own finally, but
        # nesting guarantees the invariant at the module boundary.
        app._build_adapter = original_build_adapter  # type: ignore[assignment]
        usage_wait._attempt_probe = original_attempt_probe  # type: ignore[assignment]
        usage_wait.time.sleep = original_sleep  # type: ignore[assignment]
        if not args.keep_temp:
            shutil.rmtree(sandbox, ignore_errors=True)

    print("\nSummary")
    print("-------")
    for result in results:
        status = "OK  " if result.ok else "FAIL"
        print(f"  {status}  {result.name}: {result.message}")
    passed = sum(1 for r in results if r.ok)
    total = len(results)
    print(f"{passed}/{total} scenarios passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
