from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def run_command(
    command: list[str],
    cwd: str | Path | None = None,
    stdin_text: str | None = None,
    timeout_sec: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _resolve_command(command),
        cwd=str(cwd) if cwd is not None else None,
        input=stdin_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_sec,
        encoding="utf-8",
    )


def _resolve_command(command: list[str]) -> list[str]:
    """Replace the executable token with its full path so Windows can find .cmd shims.

    On Windows, Python's `subprocess.run([name, ...])` calls CreateProcess
    directly and won't auto-append PATHEXT extensions for shims like
    `codex.cmd`. `shutil.which` honors PATHEXT, so resolving up-front avoids
    `FileNotFoundError` for npm-style wrappers while still being a no-op when
    the caller already passed an absolute path.
    """
    if not command:
        return command
    head = command[0]
    if Path(head).is_absolute():
        return command
    resolved = shutil.which(head)
    if resolved is None:
        return command
    return [resolved, *command[1:]]
