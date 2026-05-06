"""CodexCliAdapter — codex CLI 비대화형 호출.

`codex exec --output-schema <file>` 로 final response 를 schema 에 맞춰 받는다.
codex 는 agentic 출력이라 stdout 에 trace 가 섞일 수 있어, 마지막 / 첫 JSON 객체를
순차적으로 시도하는 robust 파서를 둔다.

설계 메모:
- `--sandbox read-only` 로 LLM 이 임의 파일을 수정하지 못하게 잠근다.
- `--skip-git-repo-check` — orch-engine/ 자체가 아직 git repo 가 아님.
- `--ignore-user-config` 는 박제관 글로벌 설정 충돌을 피하지만, 인증 흐름과 기본
  모델 선택까지 영향을 줄 수 있어 일단 끄지 않는다 (live smoke 결과로 조정).
- prompt 는 stdin 으로 전달해 인용 이슈를 회피한다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .contracts import response_schema, system_prompt, user_prompt


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*?\}")


class CodexCliAdapter:
    name = "codex_cli"

    def __init__(
        self,
        model: str | None = None,
        timeout: int = 300,
        executable: str = "codex",
        sandbox: str = "read-only",
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise FileNotFoundError(f"{executable!r} CLI not found on PATH")
        self.executable = resolved
        self.model = model
        self.timeout = timeout
        self.sandbox = sandbox

    def invoke(self, role: str, context: dict[str, Any]) -> dict[str, Any]:
        schema = response_schema(role)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(schema, f, ensure_ascii=False)
            schema_path = Path(f.name)

        try:
            cmd = [
                self.executable, "exec",
                "--skip-git-repo-check",
                "--sandbox", self.sandbox,
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
