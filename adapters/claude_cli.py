"""ClaudeCliAdapter — claude CLI 비대화형 호출.

`claude --print --output-format json --json-schema <schema>` 로 schema-constrained
응답을 받고, wrapper 의 `result` / `structured_output` 필드에서 실제 JSON 을 꺼낸다.

역할별 도구 정책 (2026-05-07 박제관 결정):
- planner / orchestrator: 도구 비활성, 격리 임시 cwd. 텍스트 판단만.
- builder: Write/Edit/Bash 등 파일 작성 도구 활성, cwd = target. target 밖은
  Claude Code 의 기본 워크스페이스 격리로 자동 차단(별도 --add-dir 안 줌).
- verifier: 읽기 + 최소 실행(Bash) 활성, cwd = target.

도구 활성 호출은 `--permission-mode bypassPermissions` 가 필요(비대화형이라 권한
프롬프트가 뜨면 행이 막힘). target 폴더 안으로만 cwd 가 한정되므로 영향 범위는
target 으로 제한된다.

설계 메모:
- `--bare` 는 ANTHROPIC_API_KEY 를 강제하므로 OAuth 로그인 사용자에서는 못 쓴다.
- 응답 파싱 실패는 fallback 하지 않고 RuntimeError 로 명시 실패시킨다 — 어댑터에서
  조용히 'blocked' 를 만들면 자동 개선 루프가 가짜 진행을 하기 때문.
"""
from __future__ import annotations

import atexit
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .contracts import response_schema, system_prompt, user_prompt


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")

# 역할별 기본 도구 정책. tools="" 면 도구 비활성(과거 동작), use_target_cwd=False
# 면 격리 임시폴더에서 호출. tools 가 비어있지 않으면 use_target_cwd=True 권장.
DEFAULT_ROLE_TOOL_POLICY: dict[str, dict[str, Any]] = {
    "planner":      {"tools": "", "use_target_cwd": False},
    "builder":      {"tools": "Bash,Edit,Read,Write,Glob,Grep", "use_target_cwd": True},
    "verifier":     {"tools": "Bash,Read,Glob,Grep", "use_target_cwd": True},
    "orchestrator": {"tools": "", "use_target_cwd": False},
}


class ClaudeCliAdapter:
    name = "claude_cli"

    def __init__(
        self,
        model: str | None = None,
        timeout: int = 600,
        executable: str = "claude",
        target: str | None = None,
        tool_policy: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise FileNotFoundError(f"{executable!r} CLI not found on PATH")
        self.executable = resolved
        self.model = model
        self.timeout = timeout
        self.target = str(Path(target).resolve()) if target else None
        self.tool_policy = tool_policy or DEFAULT_ROLE_TOOL_POLICY
        # 글로벌/프로젝트 CLAUDE.md 자동 발견을 막기 위한 빈 임시 폴더.
        # 도구 비활성 역할(planner/orchestrator)에서 cwd 로 사용.
        self._isolated_cwd = tempfile.mkdtemp(prefix="orch-claude-")
        atexit.register(shutil.rmtree, self._isolated_cwd, ignore_errors=True)

    def _policy_for(self, role: str) -> dict[str, Any]:
        return self.tool_policy.get(role, {"tools": "", "use_target_cwd": False})

    def _cwd_for(self, role: str) -> str:
        policy = self._policy_for(role)
        if policy.get("use_target_cwd") and self.target:
            return self.target
        return self._isolated_cwd

    def invoke(self, role: str, context: dict[str, Any]) -> dict[str, Any]:
        schema = response_schema(role)
        policy = self._policy_for(role)
        tools = policy.get("tools", "")
        cwd = self._cwd_for(role)

        cmd = [
            self.executable,
            "--print",
            "--output-format", "json",
            "--json-schema", json.dumps(schema, ensure_ascii=False),
            "--append-system-prompt", system_prompt(role),
            "--tools", tools,
            "--no-session-persistence",
            "--exclude-dynamic-system-prompt-sections",
            "--setting-sources", "",
        ]
        # 도구 활성 호출은 비대화형에서 권한 프롬프트가 뜨면 멈추므로 bypass 가 필요.
        # cwd 가 target 으로 제한돼 있어 영향 범위는 target 폴더 안.
        if tools:
            cmd += ["--permission-mode", "bypassPermissions"]
        if self.model:
            cmd += ["--model", self.model]
        cmd.append(user_prompt(role, context))

        completed = subprocess.run(
            cmd,
            cwd=cwd,
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
        usage = _extract_usage(completed.stdout)
        self.last_usage = usage
        if usage:
            print(
                "[claude_cli usage] "
                f"role={role} tools={'yes' if tools else 'no'} "
                f"cost_usd={usage.get('total_cost_usd'):.4f} "
                f"input={usage.get('input_tokens')} output={usage.get('output_tokens')} "
                f"cache_read={usage.get('cache_read_input_tokens')} "
                f"cache_creation={usage.get('cache_creation_input_tokens')} "
                f"duration_ms={usage.get('duration_ms')}",
                file=sys.stderr,
                flush=True,
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


def _extract_usage(stdout: str) -> dict[str, Any] | None:
    """wrapper JSON 에서 토큰/비용 정보를 꺼낸다. wrapper 가 아니면 None."""
    text = stdout.strip()
    if not text:
        return None
    try:
        wrapper = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(wrapper, dict):
        return None
    usage = wrapper.get("usage") or {}
    return {
        "total_cost_usd": wrapper.get("total_cost_usd") or 0.0,
        "duration_ms": wrapper.get("duration_ms"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
    }


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
