from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adapters.base import BaseCliAdapter, Invocation, find_payload_candidate


class CodexCliAdapter(BaseCliAdapter):
    provider_id = "codex_cli"
    provider_label = "Codex CLI"

    def build_command(
        self,
        *,
        invocation: Invocation,
        schema_path: Path,
        schema_text: str,
        provider_result_path: Path,
    ) -> tuple[list[str], bool]:
        sandbox_mode = "workspace-write" if invocation.role in {"builder", "verifier_functional"} else "read-only"
        # Do NOT pass `--output-schema`. Codex CLI wires that flag into OpenAI's
        # strict `response_format`, which rejects JSON Schema features we rely on
        # for utterance.v1 (optional keys without being in `required`, allOf, etc.).
        # Schema conformance is enforced by the prompt + engine-side _validate_schema.
        return (
            [
                "codex",
                "exec",
                "-",
                "--cd",
                str(Path(invocation.working_directory).resolve()),
                "--skip-git-repo-check",
                "--sandbox",
                sandbox_mode,
                "-o",
                str(provider_result_path),
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
        if not provider_result_path.exists():
            raise FileNotFoundError(f"Provider result file not found: {provider_result_path}")
        result_text = provider_result_path.read_text(encoding="utf-8").strip()
        if not result_text:
            raise ValueError(f"Provider result file is empty: {provider_result_path}")
        parsed = json.loads(result_text)
        found = find_payload_candidate(parsed, set(schema.get("required", [])))
        if found is None:
            raise ValueError("Could not extract role payload from Codex result file.")
        return found
