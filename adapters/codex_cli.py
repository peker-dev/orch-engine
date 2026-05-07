"""CodexCliAdapter — codex CLI 비대화형 호출.

`codex exec --output-schema <file>` 로 final response 를 schema 에 맞춰 받는다.
codex 는 agentic 출력이라 stdout 에 trace 가 섞일 수 있어, 마지막 / 첫 JSON 객체를
순차적으로 시도하는 robust 파서를 둔다.

역할별 샌드박스 정책 (2026-05-07, ClaudeCliAdapter 와 일관):
- planner / orchestrator: `read-only` 샌드박스, 격리 임시 cwd. 텍스트 판단만.
- builder: `workspace-write` 샌드박스, cwd = target. 실제 파일 생성/수정.
- verifier: `workspace-write` 샌드박스, cwd = target. 읽기 + 실행 (테스트 등).

`workspace-write` 샌드박스는 cwd 안에서만 쓰기를 허용한다. cwd 를 target 으로
한정하므로 영향 범위는 target 폴더 안. mixed profile 에서 codex 는 planner /
orchestrator 에 할당되므로 당장 동작 변화는 없으나, codex profile 또는 매핑
변경 시 같은 MVP 차단(빌더가 디스크에 못 씀) 을 사전에 막는다.

설계 메모:
- `--skip-git-repo-check` — orch-engine/ 자체가 아직 git repo 가 아님.
- `--ignore-user-config` 는 박제관 글로벌 설정 충돌을 피하지만, 인증 흐름과 기본
  모델 선택까지 영향을 줄 수 있어 일단 끄지 않는다 (live smoke 결과로 조정).
- prompt 는 stdin 으로 전달해 인용 이슈를 회피한다.
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


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*?\}")

# 역할별 기본 샌드박스 정책. ClaudeCliAdapter 의 도구 정책과 의미상 일관:
# 도구 비활성 역할 → 격리 임시 cwd + read-only.
# 도구 활성 역할 → target cwd + workspace-write.
DEFAULT_ROLE_SANDBOX_POLICY: dict[str, dict[str, Any]] = {
    "planner":      {"sandbox": "read-only",       "use_target_cwd": False},
    "builder":      {"sandbox": "workspace-write", "use_target_cwd": True},
    "verifier":     {"sandbox": "workspace-write", "use_target_cwd": True},
    "orchestrator": {"sandbox": "read-only",       "use_target_cwd": False},
}


class CodexCliAdapter:
    name = "codex_cli"

    def __init__(
        self,
        model: str | None = None,
        timeout: int = 600,
        executable: str = "codex",
        sandbox: str = "read-only",
        target: str | None = None,
        sandbox_policy: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise FileNotFoundError(f"{executable!r} CLI not found on PATH")
        self.executable = resolved
        self.model = model
        self.timeout = timeout
        self.sandbox = sandbox  # fallback default 만 — 정책이 우선
        self.target = str(Path(target).resolve()) if target else None
        self.sandbox_policy = sandbox_policy or DEFAULT_ROLE_SANDBOX_POLICY
        # planner / orchestrator (도구 비활성 역할) 호출용 격리 임시 폴더
        self._isolated_cwd = tempfile.mkdtemp(prefix="orch-codex-")
        atexit.register(shutil.rmtree, self._isolated_cwd, ignore_errors=True)
        self.last_usage: dict[str, Any] | None = None

    def _policy_for(self, role: str) -> dict[str, Any]:
        return self.sandbox_policy.get(
            role, {"sandbox": self.sandbox, "use_target_cwd": False}
        )

    def _cwd_for(self, role: str) -> str:
        policy = self._policy_for(role)
        if policy.get("use_target_cwd") and self.target:
            return self.target
        return self._isolated_cwd

    def invoke(self, role: str, context: dict[str, Any]) -> dict[str, Any]:
        schema = response_schema(role)
        policy = self._policy_for(role)
        sandbox = policy.get("sandbox", self.sandbox)
        cwd = self._cwd_for(role)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(schema, f, ensure_ascii=False)
            schema_path = Path(f.name)

        try:
            cmd = [
                self.executable, "exec",
                "--skip-git-repo-check",
                "--sandbox", sandbox,
                "--output-schema", str(schema_path),
                "--color", "never",
                "--ephemeral",
            ]
            if self.model:
                cmd += ["--model", self.model]
            cmd.append("-")  # read prompt from stdin

            prompt = system_prompt(role) + "\n\n" + user_prompt(role, context)
            completed = subprocess.run(
                cmd,
                input=prompt,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                encoding="utf-8",
            )
        finally:
            try:
                schema_path.unlink()
            except OSError:
                pass

        if completed.returncode != 0:
            raise RuntimeError(
                f"codex CLI failed rc={completed.returncode}: "
                f"{completed.stderr.strip()[:500] or completed.stdout.strip()[:500]}"
            )

        usage = _extract_usage(completed.stdout, completed.stderr)
        self.last_usage = usage
        if usage:
            print(
                "[codex_cli usage] "
                f"role={role} sandbox={sandbox} "
                + " ".join(f"{k}={v}" for k, v in usage.items() if v is not None),
                file=sys.stderr,
                flush=True,
            )
        return _parse_stdout(completed.stdout)


def _parse_stdout(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise RuntimeError("empty stdout from codex CLI")

    # 1) stdout 전체가 단일 JSON 인 경우
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass

    # 2) 마지막부터 거꾸로 brace 매칭으로 valid JSON 찾기
    obj = _last_balanced_object(text)
    if obj is not None:
        return obj

    # 3) 첫 번째 JSON 객체 fallback
    match = _JSON_OBJ_RE.search(text)
    if not match:
        raise RuntimeError(f"no JSON object in codex stdout: {text[-400:]}")
    return json.loads(match.group(0))


_USAGE_LINE_RE = re.compile(
    r"tokens used.*?input[:=\s]+([\d,]+).*?output[:=\s]+([\d,]+)",
    re.IGNORECASE,
)


def _extract_usage(stdout: str, stderr: str) -> dict[str, Any] | None:
    """codex CLI 출력에서 토큰 정보를 best-effort 로 꺼낸다.

    codex 는 stdout/stderr 어느 쪽에든 'tokens used: input=... output=...' 같은
    trace 줄을 흘리는 버전이 있다. 매치 실패는 None.
    """
    for blob in (stderr or "", stdout or ""):
        match = _USAGE_LINE_RE.search(blob)
        if match:
            try:
                return {
                    "input_tokens": int(match.group(1).replace(",", "")),
                    "output_tokens": int(match.group(2).replace(",", "")),
                }
            except ValueError:
                continue
    return None


def _last_balanced_object(text: str) -> dict[str, Any] | None:
    """text 끝쪽에 있는 가장 마지막 balanced `{...}` 를 찾아 dict 로 반환."""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    candidate = text[i : end + 1]
                    try:
                        loaded = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(loaded, dict):
                        return loaded
                    break
        end = text.rfind("}", 0, end)
    return None
