"""대화형 launcher (얇은 wrapper).

`python -m core.app init/run` 의 인자를 외우지 않도록 표준 input() 만으로
target / goal / profile / max-cycles 를 묻고 같은 프로세스에서 cmd_init / cmd_run
을 호출한다. 새 의존성 없음. 기존 CLI 표면(`python -m core.app ...`)은 그대로.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import app as core_app


def _prompt(message: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        try:
            raw = input(f"{message}{suffix}: ").strip()
        except EOFError:
            return default if default is not None else ""
        if raw:
            return raw
        if default is not None:
            return default
        print("값이 필요합니다. 다시 입력해 주세요.")


def _do_init() -> int:
    target = _prompt("target 폴더 경로")
    goal = _prompt("goal 텍스트")
    profiles = sorted(core_app.PROFILES.keys())
    print(f"profile 선택지: {', '.join(profiles)}")
    profile = _prompt("profile", default="mixed")
    if profile not in core_app.PROFILES:
        print(f"알 수 없는 profile: {profile!r}. 메뉴로 돌아갑니다.")
        return 1
    args = argparse.Namespace(
        target=target, goal=goal, profile=profile, builder_model=None
    )
    try:
        return int(core_app.cmd_init(args) or 0)
    except SystemExit as exc:
        print(f"init 실패: {exc}")
        return 1


def _do_run() -> int:
    target = _prompt("target 폴더 경로")
    if not (Path(target).resolve() / ".orch").exists():
        print(f".orch 가 없습니다: {target}. init 부터 진행해 주세요.")
        return 1
    raw = _prompt("max-cycles", default="2")
    try:
        max_cycles = int(raw)
    except ValueError:
        print(f"숫자 아님: {raw!r}. 메뉴로 돌아갑니다.")
        return 1
    args = argparse.Namespace(target=target, max_cycles=max_cycles)
    try:
        return int(core_app.cmd_run(args) or 0)
    except SystemExit as exc:
        print(f"run 실패: {exc}")
        return 1


def _menu_loop() -> int:
    while True:
        print()
        print("orch-engine launcher")
        print("  1) init  — 새 target 에 .orch 만들기")
        print("  2) run   — 기존 target 에서 cycle 실행")
        print("  3) 종료")
        try:
            choice = input("선택: ").strip()
        except EOFError:
            return 0
        if choice == "1":
            _do_init()
        elif choice == "2":
            _do_run()
        elif choice in {"3", "q", "Q", ""}:
            return 0
        else:
            print(f"알 수 없는 선택: {choice!r}")


def main(argv: list[str] | None = None) -> int:
    return _menu_loop()


if __name__ == "__main__":
    sys.exit(main())
