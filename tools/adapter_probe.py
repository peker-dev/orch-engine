from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ENGINE_ROOT = Path(__file__).resolve().parent.parent
ROLE_SCHEMA_MAP = {
    "planner": ENGINE_ROOT / "schemas" / "roles" / "planner.result.v1.json",
    "builder": ENGINE_ROOT / "schemas" / "roles" / "builder.result.v1.json",
    "verifier_functional": ENGINE_ROOT / "schemas" / "roles" / "verifier_functional.result.v1.json",
    "verifier_human": ENGINE_ROOT / "schemas" / "roles" / "verifier_human.result.v1.json",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Claude/Codex CLI adapter contracts")
    parser.add_argument("--provider", required=True, choices=["claude", "codex"])
    parser.add_argument(
        "--role",
        required=True,
        help=(
            "Role to probe, or 'all' to run probes for every role "
            "(planner, builder, verifier_functional, verifier_human) sequentially."
        ),
    )
    parser.add_argument("--objective", required=True, help="Short role objective")
    parser.add_argument("--working-dir", default=str(ENGINE_ROOT), help="Working directory for the provider")
    parser.add_argument("--context-summary", default="Probe run with minimal context.", help="Short context summary")
    parser.add_argument(
        "--out-dir",
        default="",
        help="Optional output directory. Defaults to orch-engine/.tmp/adapter_probes/<run-id>",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually execute the provider CLI. Default is dry-run only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    role_arg = args.role
    if role_arg == "all":
        roles = ["planner", "builder", "verifier_functional", "verifier_human"]
    elif role_arg in ROLE_SCHEMA_MAP:
        roles = [role_arg]
    else:
        raise SystemExit(
            f"Unknown role: {role_arg}. Choose one of "
            "[planner, builder, verifier_functional, verifier_human, all]."
        )

    rc = 0
    for role in roles:
        single_rc = _probe_single_role(args, role)
        if single_rc != 0 and rc == 0:
            rc = single_rc
    return rc


def _probe_single_role(args: argparse.Namespace, role: str) -> int:
    role_schema_path = ROLE_SCHEMA_MAP[role]
    role_schema = _read_json(role_schema_path)

    run_id = f"{args.provider}-{role}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    stage_dir = Path(args.out_dir).resolve() if args.out_dir else ENGINE_ROOT / ".tmp" / "adapter_probes" / run_id
    stage_dir.mkdir(parents=True, exist_ok=True)

    # `request` is captured for the dry-run artifact set. In live mode the
    # real adapter rebuilds its own envelope from the Invocation, so this dict
    # is only meant as a human-readable preview of what will be sent.
    request = {
        "version": "1.0",
        "request_id": run_id,
        "provider": f"{args.provider}_cli",
        "role": role,
        "objective": args.objective,
        "working_directory": str(Path(args.working_dir).resolve()),
        "mode": "probe",
        "context_summary": args.context_summary,
        "input_files": [],
        "write_scope": [],
        "output_schema_path": str(role_schema_path),
        "timeout_sec": _default_timeout(role),
    }
    prompt = _render_prompt(request, role_schema_path)

    request_path = stage_dir / "request.json"
    prompt_path = stage_dir / "prompt.txt"
    schema_copy_path = stage_dir / "schema.json"
    normalized_path = stage_dir / "normalized.json"
    provider_result_path = stage_dir / "provider-result.json"
    command_path = stage_dir / "command.json"

    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    prompt_path.write_text(prompt, encoding="utf-8")
    schema_copy_path.write_text(json.dumps(role_schema, ensure_ascii=False, indent=2), encoding="utf-8")

    command = _build_command(
        provider=args.provider,
        role=role,
        working_dir=Path(args.working_dir).resolve(),
        schema_path=schema_copy_path,
        schema_text=schema_copy_path.read_text(encoding="utf-8"),
        result_path=provider_result_path,
    )
    command_path.write_text(json.dumps({"command": command}, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.live:
        print(f"[dry-run] staged probe artifacts at: {stage_dir}")
        print(f"[dry-run] command preview saved to: {command_path}")
        return 0

    # Live mode runs through the real adapter so we exercise the same normalize
    # + schema validation path the engine uses at run-cycle time. The full
    # request/prompt/stdout/stderr trail is written under
    # `<working_dir>/.orch/runtime/adapter_runs/<run_id>/attempt-NN/...` by the
    # adapter itself; here we only summarize the outcome at the probe stage dir.
    from adapters.base import AdapterExecutionError, AdapterFatalError, Invocation
    from adapters.claude_cli import ClaudeCliAdapter
    from adapters.codex_cli import CodexCliAdapter

    adapter = ClaudeCliAdapter() if args.provider == "claude" else CodexCliAdapter()
    invocation = Invocation(
        role=role,
        objective=args.objective,
        working_directory=str(Path(args.working_dir).resolve()),
        context={"probe": True, "context_summary": args.context_summary},
    )
    try:
        result = adapter.invoke(invocation)
    except AdapterFatalError as exc:
        print(f"[fatal] {adapter.provider_label} fatal failure: {exc}")
        return 2
    except AdapterExecutionError as exc:
        print(f"[error] {adapter.provider_label} failed after retries: {exc}")
        return 3

    normalized = {
        "status": result.status,
        "provider": adapter.provider_id,
        "role": role,
        "summary": result.summary,
        "payload": result.payload,
        "stage_dir": str(stage_dir),
    }
    normalized_path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[ok] live probe succeeded ({adapter.provider_label} / {role})")
    print(f"[ok] normalized summary: {result.summary}")
    print(f"[ok] adapter run artifacts under: {Path(args.working_dir).resolve() / '.orch' / 'runtime' / 'adapter_runs'}")
    return 0


def _default_timeout(role: str) -> int:
    return {
        "planner": 180,
        "builder": 600,
        "verifier_functional": 300,
        "verifier_human": 240,
    }[role]


def _render_prompt(request: dict[str, Any], schema_path: Path) -> str:
    role = request["role"]
    objective = request["objective"]
    working_directory = request["working_directory"]
    context_summary = request["context_summary"]
    return (
        f"You are running as orch-engine role '{role}'.\n"
        "Return exactly one JSON object that matches the provided schema.\n"
        "Do not use markdown.\n"
        "Do not wrap the JSON in code fences.\n"
        f"Objective: {objective}\n"
        f"Working directory: {working_directory}\n"
        f"Context summary: {context_summary}\n"
        f"Schema path: {schema_path}\n"
        "This is a probe run. Do not assume external files beyond the working directory.\n"
        "If you are unsure, return the safest valid JSON for the role schema.\n"
    )


def _build_command(
    *,
    provider: str,
    role: str,
    working_dir: Path,
    schema_path: Path,
    schema_text: str,
    result_path: Path,
) -> list[str]:
    """Mirror the role-based permission/sandbox logic in the real adapters.

    Keeping probe and adapter in sync lets a live probe actually exercise the
    same provider contract the engine will use at run-cycle time.
    """
    writes_needed = role in {"builder", "verifier_functional"}

    if provider == "claude":
        permission_mode = "bypassPermissions" if writes_needed else "dontAsk"
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            schema_text,
            "--permission-mode",
            permission_mode,
            "--add-dir",
            str(working_dir),
        ]

    if provider == "codex":
        sandbox_mode = "workspace-write" if writes_needed else "read-only"
        return [
            "codex",
            "exec",
            "-",
            "--cd",
            str(working_dir),
            "--skip-git-repo-check",
            "--sandbox",
            sandbox_mode,
            "--output-schema",
            str(schema_path),
            "-o",
            str(result_path),
        ]

    raise ValueError(f"Unsupported provider: {provider}")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
