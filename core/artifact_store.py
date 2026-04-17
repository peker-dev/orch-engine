from __future__ import annotations

from pathlib import Path
from typing import Any

from core.runtime_store import RuntimeStore


class ArtifactStore:
    def __init__(self, project_root: Path) -> None:
        self.runtime_store = RuntimeStore(project_root)

    def register(self, kind: str, path: str, summary: str) -> None:
        index = self.runtime_store.read_json("artifacts/index.json", {"items": []})
        index["items"].append({"kind": kind, "path": path, "summary": summary})
        self.runtime_store.write_json("artifacts/index.json", index)
