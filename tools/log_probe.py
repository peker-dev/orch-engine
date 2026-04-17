from __future__ import annotations

from pathlib import Path


def tail_text(path: str | Path, max_lines: int = 20) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return lines[-max_lines:]
