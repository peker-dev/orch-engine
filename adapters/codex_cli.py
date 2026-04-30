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
        # P0-R 7 옵션 A (22차 세션 2, 2026-04-30): builder / verifier_functional 은
        # 무거운 외부 시스템 (Unity batchmode 등) 을 spawn 해야 하는 역할이라
        # codex 의 workspace-write sandbox 가 파일 *삭제* 를 차단하는 정책에 부딪힘.
        # Unity 가 시동 중 파일 삭제 시도 → "project folder is read only" → batchmode abort.
        # 증거: test-phase5-unity cycle 3 stderr (2026-04-29) 의
        # `codex_core::tools::router: ...Remove-Item... rejected: blocked by policy`.
        # 응급 처치로 두 역할만 danger-full-access 로 격상. 장기 정답은 옵션 C
        # (엔진 표준 external runner 로 Unity 호출 분리 — `memory/next-work.md` P0-R 7).
        if invocation.role in {"builder", "verifier_functional"}:
            sandbox_mode = "danger-full-access"
        else:
            sandbox_mode = "read-only"
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
