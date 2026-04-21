"""Estimate orch-engine's own provider usage from local adapter_runs logs.

Claude CLI / Codex CLI do not expose an official usage query (checked in the
15th session — see `memory/usage-probe-research.md`). This probe therefore
reports only what orch-engine itself has invoked, by counting adapter run
directories under `.orch/runtime/adapter_runs/` and bucketing them by their
directory mtime.

Limitations
-----------
- Does not see traffic from other sessions (e.g. the user running Claude Code
  interactively outside orch-engine). The numbers here are a lower bound on
  actual subscription consumption.
- Uses the directory's mtime as a proxy for call start time. Close enough for
  rolling-window bucketing.
- Has no visibility into Anthropic/OpenAI's real rolling-window offsets. A
  "5h rolling" here means "the last 5 hours from now", not the provider's
  actual window start.

CLI
---
    python -m tools.usage_probe --target <path>
    python -m tools.usage_probe --target <path> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(slots=True)
class UsageBucket:
    window_label: str
    window_seconds: int
    call_count: int
    by_provider: dict[str, int]
    by_role: dict[str, int]
    first_call_iso: str | None
    last_call_iso: str | None


@dataclass(slots=True)
class UsageReport:
    target: str
    generated_at_iso: str
    total_calls_seen: int
    buckets: list[UsageBucket]
    note: str


def build_report(target: Path, now: datetime | None = None) -> UsageReport:
    now = now or datetime.now(timezone.utc)
    runs_root = target / ".orch" / "runtime" / "adapter_runs"
    entries = _collect_entries(runs_root)
    buckets = [
        _bucket(entries, now=now, window_seconds=5 * 3600, label="5h"),
        _bucket(entries, now=now, window_seconds=24 * 3600, label="24h"),
        _bucket(entries, now=now, window_seconds=7 * 24 * 3600, label="7d"),
    ]
    return UsageReport(
        target=str(target),
        generated_at_iso=now.isoformat(),
        total_calls_seen=len(entries),
        buckets=buckets,
        note=(
            "Counts only orch-engine's own adapter invocations. External "
            "Claude/Codex sessions are not visible."
        ),
    )


def _collect_entries(runs_root: Path) -> list[tuple[datetime, str, str]]:
    """Return (timestamp, provider, role) tuples parsed from directory names.

    Directory format from adapters/base.py `_build_run_root`:
        {provider_id}-{role}-{YYYYMMDD-HHMMSS}-{uuid8}
    """
    if not runs_root.is_dir():
        return []
    entries: list[tuple[datetime, str, str]] = []
    for child in runs_root.iterdir():
        if not child.is_dir():
            continue
        parsed = _parse_run_dirname(child.name)
        if parsed is None:
            continue
        ts, provider, role = parsed
        entries.append((ts, provider, role))
    return entries


def _parse_run_dirname(name: str) -> tuple[datetime, str, str] | None:
    """Parse `{provider}-{role}-{YYYYMMDD}-{HHMMSS}-{uuid8}` produced by
    adapters.base.BaseCliAdapter.

    Reverses from the tail because the last four dash segments are fixed
    (date, time, uuid8-hex, nothing beyond). Everything before that belongs to
    provider+role. Current provider ids (claude_cli, codex_cli, fake_build) have
    no dashes so parts[0] is provider; but if a future provider uses a dashed
    id, this parser still recovers role correctly because it trims from the end.
    Role tokens are rejoined with `_` — current LLM roles use underscores
    (verifier_functional, verifier_human), and no role today contains a dash.
    """
    parts = name.split("-")
    if len(parts) < 5:
        return None
    uuid_token = parts[-1]
    time_token = parts[-2]
    date_token = parts[-3]
    head_tokens = parts[:-3]
    if not head_tokens or len(uuid_token) < 4:
        return None
    provider = head_tokens[0]
    role_tokens = head_tokens[1:]
    role = "_".join(role_tokens) if role_tokens else ""
    try:
        ts = datetime.strptime(f"{date_token}{time_token}", "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return ts.replace(tzinfo=timezone.utc), provider, role


def _bucket(
    entries: list[tuple[datetime, str, str]],
    *,
    now: datetime,
    window_seconds: int,
    label: str,
) -> UsageBucket:
    cutoff = now - timedelta(seconds=window_seconds)
    in_window = [(ts, p, r) for (ts, p, r) in entries if ts >= cutoff]
    by_provider: dict[str, int] = {}
    by_role: dict[str, int] = {}
    for _, provider, role in in_window:
        by_provider[provider] = by_provider.get(provider, 0) + 1
        by_role[role] = by_role.get(role, 0) + 1
    first_iso = min((ts for ts, _, _ in in_window), default=None)
    last_iso = max((ts for ts, _, _ in in_window), default=None)
    return UsageBucket(
        window_label=label,
        window_seconds=window_seconds,
        call_count=len(in_window),
        by_provider=by_provider,
        by_role=by_role,
        first_call_iso=first_iso.isoformat() if first_iso else None,
        last_call_iso=last_iso.isoformat() if last_iso else None,
    )


def _render_text(report: UsageReport) -> str:
    lines = [
        f"target: {report.target}",
        f"generated_at: {report.generated_at_iso}",
        f"total_calls_seen: {report.total_calls_seen}",
        "",
    ]
    for bucket in report.buckets:
        lines.append(
            f"[{bucket.window_label}] calls={bucket.call_count} "
            f"providers={bucket.by_provider} roles={bucket.by_role}"
        )
        if bucket.first_call_iso:
            lines.append(f"    first={bucket.first_call_iso}  last={bucket.last_call_iso}")
    lines.append("")
    lines.append(f"note: {report.note}")
    return "\n".join(lines)


def _report_to_dict(report: UsageReport) -> dict:
    data = asdict(report)
    data["buckets"] = [asdict(b) for b in report.buckets]
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report orch-engine's own provider usage")
    parser.add_argument("--target", required=True, help="Target project path (the one with .orch/)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    args = parser.parse_args(argv)

    target = Path(args.target).resolve()
    if not (target / ".orch").is_dir():
        print(f"error: {target} has no .orch directory", file=sys.stderr)
        return 2
    report = build_report(target)
    if args.json:
        print(json.dumps(_report_to_dict(report), ensure_ascii=False, indent=2))
    else:
        print(_render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
