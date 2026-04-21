"""Wait for Claude/Codex provider quota to refill, then exit with a signal.

Background
----------
Neither CLI exposes an official usage query (see `memory/usage-probe-research.md`),
so "quota available?" can only be answered by attempting a minimal invocation and
observing whether the provider still rejects it with a quota marker. This tool
performs that polling loop so a nightly autonomous run-cycle can pause and resume
without user intervention.

Behavior
--------
- Issues a cheap probe every `--poll-interval-sec` (default 300s, roughly the
  5-minute granularity the providers advertise).
- The probe reuses `adapters.base.BaseCliAdapter` + the configured adapter for
  the requested role (default planner role via roles.yaml) with a minimal
  objective. Success means the provider accepted the call — quota is live.
- On `AdapterQuotaExceededError` the loop sleeps the interval and retries.
- On `AdapterFatalError` (auth, missing binary) the tool exits rc=2 immediately
  because no amount of waiting will fix those.
- On a non-quota `AdapterExecutionError` (timeout, schema error, generic
  non-zero exit), the probe counts as inconclusive and the loop retries, but a
  streak of `--max-consecutive-errors` (default 3) in a row exits rc=2 to avoid
  silent infinite polling when the failure is unrelated to quota.
- Stops after `--max-hours` (default 6) and exits rc=3.
- Ctrl+C exits rc=4 (user abort).
- On success exits rc=0 and writes a small report to
  `.orch/runtime/usage_wait_last.json` so run-cycle can detect it.

CLI
---
    python -m tools.usage_wait --target <path>
    python -m tools.usage_wait --target <path> --poll-interval-sec 600 --max-hours 4
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from adapters.base import (
    AdapterExecutionError,
    AdapterFatalError,
    AdapterQuotaExceededError,
    Invocation,
)
from adapters.claude_cli import ClaudeCliAdapter
from adapters.codex_cli import CodexCliAdapter


RC_READY = 0
RC_USAGE = 2
RC_TIMEOUT = 3
RC_ABORTED = 4


def _load_roles_yaml(target: Path) -> dict:
    path = target / ".orch" / "config" / "roles.yaml"
    if not path.exists():
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    roles = doc.get("roles") if isinstance(doc, dict) else None
    return roles if isinstance(roles, dict) else {}


def _build_probe_adapter(provider_id: str):
    if provider_id == "claude_cli":
        return ClaudeCliAdapter()
    if provider_id == "codex_cli":
        return CodexCliAdapter()
    raise ValueError(
        f"usage_wait only supports claude_cli and codex_cli, got {provider_id}"
    )


def _attempt_probe(target: Path, role: str, provider_id: str) -> tuple[str, str]:
    """Try one cheap invocation. Return (outcome, reason).

    outcome is one of: "ready", "quota", "inconclusive". "ready" means the
    provider accepted the call; "quota" means it rejected with a quota marker
    and we should keep waiting; "inconclusive" means some other recoverable
    error happened. `AdapterFatalError` bypasses this function and terminates
    the process.
    """
    adapter = _build_probe_adapter(provider_id)
    try:
        adapter.invoke(
            Invocation(
                role=role,
                objective="usage_wait probe: report 'ready' in the summary field and no tasks.",
                working_directory=str(target),
                context={"probe": True},
            )
        )
        return "ready", "probe accepted"
    except AdapterQuotaExceededError as exc:
        return "quota", f"quota: {exc}"
    except AdapterFatalError as exc:
        raise SystemExit(RC_USAGE) from exc
    except AdapterExecutionError as exc:
        return "inconclusive", f"non-quota adapter error: {exc}"


def _write_result(target: Path, payload: dict) -> None:
    runtime_dir = target / ".orch" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "usage_wait_last.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def wait_for_quota(
    *,
    target: Path,
    probe_role: str,
    probe_provider: str,
    poll_interval_sec: int,
    max_hours: float,
    max_consecutive_errors: int = 3,
) -> int:
    start = datetime.now(timezone.utc)
    deadline_seconds = max_hours * 3600
    attempts = 0
    consecutive_errors = 0
    aborted = {"flag": False}
    print(
        f"[usage_wait] probing {probe_provider}/{probe_role} every {poll_interval_sec}s "
        f"for up to {max_hours}h"
    )

    def handle_sigint(signum, frame):
        # Ctrl+C 타이밍에 따라 Windows PowerShell에서는 subprocess만 죽고
        # Python 본체는 KeyboardInterrupt로만 탈출할 수도 있어, 파일 기록과
        # rc=4 종료는 finally 블록에서 flag를 보고 한 번 더 보장한다.
        aborted["flag"] = True
        raise KeyboardInterrupt

    try:
        previous_handler = signal.signal(signal.SIGINT, handle_sigint)
    except (ValueError, OSError):
        previous_handler = None

    def _persist_abort() -> None:
        _write_result(
            target,
            {
                "result": "aborted",
                "attempts": attempts,
                "started_at": start.isoformat(),
                "ended_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        print("[usage_wait] aborted by user", file=sys.stderr)

    try:
        while True:
            attempts += 1
            try:
                outcome, reason = _attempt_probe(target, probe_role, probe_provider)
            except KeyboardInterrupt:
                _persist_abort()
                return RC_ABORTED
            now = datetime.now(timezone.utc)
            elapsed = (now - start).total_seconds()
            if outcome == "ready":
                print(f"[usage_wait] READY after {int(elapsed)}s and {attempts} attempts")
                _write_result(
                    target,
                    {
                        "result": "ready",
                        "attempts": attempts,
                        "started_at": start.isoformat(),
                        "ended_at": now.isoformat(),
                        "probe_provider": probe_provider,
                        "probe_role": probe_role,
                    },
                )
                return RC_READY
            if outcome == "quota":
                consecutive_errors = 0
            else:  # "inconclusive"
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    print(
                        f"[usage_wait] giving up after {consecutive_errors} consecutive "
                        f"non-quota errors (last reason: {reason})",
                        file=sys.stderr,
                    )
                    _write_result(
                        target,
                        {
                            "result": "inconclusive",
                            "attempts": attempts,
                            "consecutive_errors": consecutive_errors,
                            "last_reason": reason,
                            "started_at": start.isoformat(),
                            "ended_at": now.isoformat(),
                        },
                    )
                    return RC_USAGE
            print(
                f"[usage_wait] still waiting ({int(elapsed)}s, attempt {attempts}, "
                f"outcome={outcome}): {reason}"
            )
            if elapsed >= deadline_seconds:
                print(
                    f"[usage_wait] deadline reached ({max_hours}h); giving up",
                    file=sys.stderr,
                )
                _write_result(
                    target,
                    {
                        "result": "deadline",
                        "attempts": attempts,
                        "started_at": start.isoformat(),
                        "ended_at": now.isoformat(),
                    },
                )
                return RC_TIMEOUT
            try:
                time.sleep(poll_interval_sec)
            except KeyboardInterrupt:
                _persist_abort()
                return RC_ABORTED
    finally:
        if aborted["flag"]:
            # 핸들러에서 KeyboardInterrupt 만 올라오는 경로에서 finally가 먼저
            # 돌 수 있어 이중 기록을 방지. aborted 파일이 아직 없으면 기록.
            runtime = target / ".orch" / "runtime" / "usage_wait_last.json"
            if not runtime.exists():
                _persist_abort()
        if previous_handler is not None:
            try:
                signal.signal(signal.SIGINT, previous_handler)
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll until provider quota refills")
    parser.add_argument("--target", required=True, help="Target project path")
    parser.add_argument("--probe-role", default="planner", help="Which role to probe with")
    parser.add_argument(
        "--probe-provider",
        default=None,
        help="Override adapter id. Defaults to roles.yaml[role] or claude_cli.",
    )
    parser.add_argument(
        "--poll-interval-sec", type=int, default=300, help="Seconds between probes"
    )
    parser.add_argument(
        "--max-hours", type=float, default=6.0, help="Hard stop after this many hours"
    )
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=3,
        help="Exit rc=2 after this many non-quota probe failures in a row",
    )
    args = parser.parse_args(argv)

    target = Path(args.target).resolve()
    if not (target / ".orch").is_dir():
        print(f"error: {target} has no .orch directory", file=sys.stderr)
        return RC_USAGE

    roles_cfg = _load_roles_yaml(target)
    probe_provider = args.probe_provider or roles_cfg.get(args.probe_role) or "claude_cli"
    return wait_for_quota(
        target=target,
        probe_role=args.probe_role,
        probe_provider=str(probe_provider),
        poll_interval_sec=max(30, int(args.poll_interval_sec)),
        max_hours=max(0.1, float(args.max_hours)),
        max_consecutive_errors=max(1, int(args.max_consecutive_errors)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
