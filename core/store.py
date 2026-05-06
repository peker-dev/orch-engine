"""파일 기반 상태 read/write 유틸.

`.orch/` 폴더 안에서 JSON / JSONL 파일들을 읽고 쓰는 작은 함수만 모아 둔다.
역할 실행 로직(loop.py)이나 어댑터(adapters/*)는 이 모듈을 통해서만 디스크에 접근한다.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ORCH_DIR_NAME = ".orch"
STOP_FILE_NAME = "STOP"

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "orch"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def orch_dir(target: str | Path) -> Path:
    return Path(target) / ORCH_DIR_NAME


def paths(target: str | Path) -> dict[str, Path]:
    base = orch_dir(target)
    return {
        "base": base,
        "stop": base / STOP_FILE_NAME,
        "project": base / "config" / "project.json",
        "roles": base / "config" / "roles.json",
        "limits": base / "config" / "limits.json",
        "session": base / "runtime" / "session.json",
        "events": base / "runtime" / "events.jsonl",
        "current_task": base / "tasks" / "current.json",
        "latest_review": base / "reviews" / "latest.json",
        "artifacts_index": base / "artifacts" / "index.json",
    }


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def append_event(target: str | Path, event: dict[str, Any]) -> None:
    p = paths(target)["events"]
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": now_iso(), **event}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_events(target: str | Path) -> list[dict[str, Any]]:
    p = paths(target)["events"]
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def stop_requested(target: str | Path) -> bool:
    return paths(target)["stop"].exists()


def init_orch(target: str | Path, goal: str) -> Path:
    """대상 폴더에 .orch 템플릿을 복사하고 goal 을 project.json 에 저장.

    이미 .orch 가 있으면 덮어쓰지 않고 ValueError 를 던진다. 사용자가 직접 지우는 편이
    안전하다.
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    base = orch_dir(target)
    if base.exists():
        raise ValueError(f"{base} already exists. remove it first if you want to re-init.")
    shutil.copytree(_TEMPLATE_DIR, base)

    p = paths(target)
    project = load_json(p["project"])
    project["goal"] = goal
    project["created_at"] = now_iso()
    project["target_path"] = str(target.resolve())
    save_json(p["project"], project)

    session = load_json(p["session"])
    session["state"] = "ready"
    session["cycle"] = 0
    session["updated_at"] = now_iso()
    save_json(p["session"], session)

    return base


def update_session(target: str | Path, **changes: Any) -> dict[str, Any]:
    p = paths(target)["session"]
    data = load_json(p)
    data.update(changes)
    data["updated_at"] = now_iso()
    save_json(p, data)
    return data
