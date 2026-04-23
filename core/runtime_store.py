from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RuntimeStore:
    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)
        self.orch_root = self.project_root / ".orch"
        self.runtime_root = self.orch_root / "runtime"
        self.artifacts_root = self.orch_root / "artifacts"

    def read_json(self, relative_path: str, default: Any) -> Any:
        path = self.orch_root / relative_path
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, relative_path: str, payload: Any) -> None:
        path = self.orch_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: staging file + replace. Guards against partial writes on
        # disk-full / SIGKILL / concurrent reads. POSIX rename is atomic; on
        # Windows Path.replace() maps to MoveFileEx with REPLACE_EXISTING, which
        # is atomic for closed targets and fine for sequential engine usage.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        path = self.runtime_root / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {"event": event_type, **payload}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=True) + "\n")

    def append_jsonl(self, relative_path: str, entry: dict[str, Any]) -> None:
        path = self.orch_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=True) + "\n")
