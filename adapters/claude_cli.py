from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adapters.base import BaseCliAdapter, Invocation, find_first_dict_candidate, find_payload_candidate


class ClaudeCliAdapter(BaseCliAdapter):
    provider_id = "claude_cli"
    provider_label = "Claude CLI"

    def build_command(
        self,
        *,
        invocation: Invocation,
        schema_path: Path,
        schema_text: str,
        provider_result_path: Path,
    ) -> tuple[list[str], bool]:
        permission_mode = "bypassPermissions" if invocation.role in {"builder", "verifier_functional"} else "dontAsk"
        return (
            [
                "claude",
                "-p",
                "--output-format",
                "json",
                "--json-schema",
                schema_text,
                "--permission-mode",
                permission_mode,
                "--add-dir",
                str(Path(invocation.working_directory).resolve()),
            ],
            True,
        )

    def extract_payload(
        self,
        *,
        stdout_text: str,
        provider_result_path: Path,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        if not stdout_text.strip():
            raise ValueError("Claude CLI returned empty stdout.")
        parsed = json.loads(stdout_text)
        if isinstance(parsed, dict):
            wrapper_type = str(parsed.get("type", "")).lower()
            if parsed.get("is_error") is True or wrapper_type in {"error", "result_error"}:
                message = str(parsed.get("error") or parsed.get("message") or parsed.get("result") or "unknown Claude wrapper error")
                raise ValueError(f"Claude CLI wrapper reported error: {message}")
        required = set(schema.get("required", []))
        found = find_payload_candidate(parsed, required)
        if found is None:
            found = find_first_dict_candidate(parsed.get("result")) if isinstance(parsed, dict) else None
        if found is None:
            found = find_first_dict_candidate(parsed)
        if found is None:
            raise ValueError("Could not extract role payload from Claude stdout.")
        return found
