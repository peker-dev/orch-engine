"""CLI 진입점.

명령 계약 (MVP):

    python -m core.app init --target <path> --goal "<goal>" [--profile P]
    python -m core.app run  --target <path> [--max-cycles N]

`init` 은 `.orch/` 템플릿을 대상 폴더에 만들고 목표를 저장한다. `--profile` 로
역할별 어댑터 매핑(roles.json)의 기본값을 함께 지정한다.

`run` 은 STOP 파일을 확인한 뒤 roles.json 의 adapters 매핑을 읽어 역할별 어댑터
인스턴스를 만들고 cycle 을 돌린다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from adapters.base import Adapter

from . import loop, store


# Windows 기본 콘솔(cp949)에서 LLM 응답에 섞인 en-dash / 한글 등이 깨지는 걸 방지.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


PROFILES: dict[str, dict[str, str]] = {
    "scripted": {
        "planner": "scripted",
        "builder": "scripted",
        "verifier": "scripted",
        "orchestrator": "scripted",
    },
    "mixed": {
        "planner": "codex_cli",
        "builder": "claude_cli",
        "verifier": "claude_cli",
        "orchestrator": "codex_cli",
    },
    "claude": {
        "planner": "claude_cli",
        "builder": "claude_cli",
        "verifier": "claude_cli",
        "orchestrator": "claude_cli",
    },
    "codex": {
        "planner": "codex_cli",
        "builder": "codex_cli",
        "verifier": "codex_cli",
        "orchestrator": "codex_cli",
    },
}


def _make_adapter(name: str, model: str | None = None, target: str | None = None) -> Adapter:
    if name == "scripted":
        from adapters.scripted import ScriptedAdapter

        return ScriptedAdapter()
    if name == "claude_cli":
        from adapters.claude_cli import ClaudeCliAdapter

        return ClaudeCliAdapter(model=model, target=target)
    if name == "codex_cli":
        from adapters.codex_cli import CodexCliAdapter

        return CodexCliAdapter(model=model)
    raise SystemExit(f"unknown adapter: {name!r}")


def _build_adapters(target: Path) -> dict[str, Adapter]:
    roles = store.load_json(store.paths(target)["roles"])
    mapping: dict[str, str] = roles.get("adapters", {})
    models: dict[str, str] = roles.get("models", {})
    if not mapping:
        raise SystemExit(".orch/config/roles.json has no 'adapters' mapping")
    cache: dict[tuple[str, str | None], Adapter] = {}
    out: dict[str, Adapter] = {}
    for role, name in mapping.items():
        model = models.get(role)
        key = (name, model)
        if key not in cache:
            cache[key] = _make_adapter(name, model, target=str(target))
        out[role] = cache[key]
    return out


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    base = store.init_orch(target, goal=args.goal)

    if args.profile != "scripted":
        roles_path = store.paths(target)["roles"]
        roles = store.load_json(roles_path)
        roles["adapters"] = dict(PROFILES[args.profile])
        store.save_json(roles_path, roles)

    print(f"initialised {base} (profile={args.profile})")
    print(json.dumps({"reason": "initialised", "orch": str(base), "profile": args.profile}, ensure_ascii=False))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    if not store.orch_dir(target).exists():
        raise SystemExit(f"no .orch found under {target}. run init first.")

    adapters = _build_adapters(target)
    result = loop.run(str(target), adapters, max_cycles=args.max_cycles)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orch-engine", description="Orchestration MVP CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create .orch under target")
    p_init.add_argument("--target", required=True, help="target project folder")
    p_init.add_argument("--goal", required=True, help="user goal text")
    p_init.add_argument(
        "--profile",
        default="scripted",
        choices=sorted(PROFILES.keys()),
        help="initial role->adapter mapping (default: scripted)",
    )
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="run cycles on target")
    p_run.add_argument("--target", required=True)
    p_run.add_argument("--max-cycles", type=int, default=2)
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
