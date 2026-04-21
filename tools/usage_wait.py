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
- On any other exception the tool exits rc=2 so the caller does not treat a
  real failure as "still waiting".
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


def _attempt_probe(target: Path, role: str, provider_id: str) -> tuple[bool, str]:
    """Try one cheap invocation. Return (ready, reason)."""
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
        return True, "probe accepted"
    except AdapterQuotaExceededError as exc:
        return False, f"quota: {exc}"
    except AdapterFatalError as exc:
        raise SystemExit(RC_USAGE) from exc
    except AdapterExecutionError as exc:
        return False, f"non-quota adapter error: {exc}"


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
) -> int:
    start = datetime.now(timezone.utc)
    deadline_seconds = max_hours * 3600
    attempts = 0
    print(
        f"[usage_wait] probing {probe_provider}/{probe_role} every {poll_interval_sec}s "
        f"for up to {max_hours}h"
    )

    def handle_sigint(signum, frame):
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
        raise SystemExit(RC_ABORTED)

    try:
        previous_handler = signal.signal(signal.SIGINT, handle_sigint)
    except (ValueError, OSError):
        previous_handler = None

    try:
        while True:
            attempts += 1
            ready, reason = _attempt_probe(target, probe_role, probe_provider)
            now = datetime.now(timezone.utc)
            elapsed = (now - start).total_seconds()
            if ready:
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
            print(f"[usage_wait] still waiting ({int(elapsed)}s, attempt {attempts}): {reason}")
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
            time.sleep(poll_interval_sec)
    finally:
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
    )


if __name__ == "__main__":
    raise SystemExit(main())
