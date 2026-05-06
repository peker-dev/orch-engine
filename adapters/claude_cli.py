"""ClaudeCliAdapter — claude CLI 비대화형 호출.

`claude --print --output-format json --json-schema <schema>` 로 schema-constrained
응답을 받고, wrapper 의 `result` 필드에서 실제 JSON 을 꺼낸다.

설계 메모:
- `--bare` 는 ANTHROPIC_API_KEY 를 강제하므로 OAuth 로그인 사용자에서는 못 쓴다.
  대신 `--no-session-persistence` + `--tools ""` + `--append-system-prompt` 만으로
  프로젝트 CLAUDE.md 의 영향을 줄인다.
- 응답 파싱 실패는 fallback 하지 않고 RuntimeError 로 명시 실패시킨다 — 어댑터에서
  조용히 'blocked' 를 만들면 자동 개선 루프가 가짜 진행을 하기 때문.
"""
from __future__ import annotations

import atexit
import json
import re
import shutil
import subprocess
import tempfile
from typing import Any

from .contracts import response_schema, system_prompt, user_prompt


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


class ClaudeCliAdapter:
    name = "claude_cli"

    def __init__(
        self,
        model: str | None = None,
        timeout: int = 240,
        executable: str = "claude",
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise FileNotFoundError(f"{executable!r} CLI not found on PATH")
        self.executable = resolved
        self.model = model
        self.timeout = timeout
        # 글로벌/프로젝트 CLAUDE.md 자동 발견을 막기 위해 빈 임시 폴더에서 호출.
        # 실측: 호출당 21,938 → 1,921 토큰 (1/10), $0.15 → $0.05.
        self._cwd = tempfile.mkdtemp(prefix="orch-claude-")
        atexit.register(shutil.rmtree, self._cwd, ignore_errors=True)

    def invoke(self, role: str, context: dict[str, Any]) -> dict[str, Any]:
        schema = response_schema(role)
        cmd = [
            self.executable,
            "--print",
            "--output-format", "json",
            "--json-schema", json.dumps(schema, ensure_ascii=False),
            "--append-system-prompt", system_prompt(role),
            "--tools", "",
            "--no-session-persistence",
            "--exclude-dynamic-system-prompt-sections",
            "--setting-sources", "",
        ]
        if self.model:
            cmd += ["--model", self.model]
        cmd.append(user_prompt(role, context))

        completed = subprocess.run(
            cmd,
            cwd=self._cwd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"claude CLI failed rc={completed.returncode}: "
                f"{completed.stderr.strip()[:500] or completed.stdout.strip()[:500]}"
            )
        return _parse_stdout(completed.stdout)


def _parse_stdout(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise RuntimeError("empty stdout from claude CLI")
    try:
        wrapper = json.loads(text)
    except json.JSONDecodeError:
        return _extract_object(text)

    if isinstance(wrapper, dict):
        if wrapper.get("is_error"):
            raise RuntimeError(f"claude returned error: {wrapper.get('result') or wrapper}")

        # schema-constrained 응답은 `structured_output` 필드에 들어옴 (실측 기준)
        structured = wrapper.get("structured_output")
        if isinstance(structured, dict):
            return structured
        if isinstance(structured, str) and structured.strip():
            return _extract_object(structured)

        # 일부 버전에서는 `result` 에 JSON string 이 박혀 올 수도 있음 — fallback
        result = wrapper.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(result, str) and result.strip():
            return _extract_object(result)

        # wrapper 자체가 응답 dict 인 경우 (drift)
        if "role" in wrapper or {"verdict", "decision", "title", "summary"} & wrapper.keys():
            return wrapper

        raise RuntimeError(f"no structured_output in claude wrapper: keys={list(wrapper.keys())}")

    return _extract_object(text)


def _extract_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJ_RE.search(text)
    if not match:
        raise RuntimeError(f"no JSON object in claude stdout: {text[:300]}")
    return json.loads(match.group(0))
