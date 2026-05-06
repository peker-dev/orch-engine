r"""Minimal Unity batchmode log parser.

Unity 의 batchmode 로그는 free-form text 인데, 자동화 관점에서 자주 보는
패턴이 정해져 있다. 이 파서는 다음 신호만 결정적으로 잡는다:

- compile error: `error CS\d+:` 패턴 (C# 컴파일 에러).
- exception: `Unhandled Exception:` / `[Error]` / `Exception: ` 라인.
- batchmode abort: `BatchMode: Cannot execute method` / `project folder is read only`.
- success marker: `Exiting batchmode successfully`.

판정 규약:
- success marker 발견 + compile error 0 + exception 0 → verdict="pass".
- 그 외 → verdict="fail" (caller 가 exit_code 와 함께 종합 판단).

Findings 는 발견된 첫 N(=10) 개 라인만 보존 — 로그가 수만 줄이어도 RunnerResult
에 담는 양은 일정. evidence 는 호출자가 로그 파일 path 를 별도로 채운다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_COMPILE_ERROR_RE = re.compile(r"\berror CS\d+:")
# code-review 후속 (2026-05-06): `.*Exception:` 은 progress 라인 ("Loading SomeException: type")
# 같은 무해한 텍스트도 잡았음. `^\s*(...)` 로 line 시작 강제 + match 메서드 사용.
# `[A-Z]\w+Exception:` 은 첫 글자 대문자 + 단어 경계 보장. `re.MULTILINE` 은 line-by-line
# scan 에서 무의미라 제거.
_EXCEPTION_HEADER_RE = re.compile(r"^\s*(Unhandled Exception:|[A-Z]\w+Exception:|\[Error\])")
_ABORT_PATTERNS = (
    "BatchMode: Cannot execute method",
    "project folder is read only",
    "Aborting batchmode",
    "Failed to load",
)
# code-review 후속: "Exiting Unity" 만 있으면 강제 종료 시에도 매칭되어 false pass
# 위험. "Exiting batchmode successfully" 와 "Exiting Unity successfully" 만 인정.
_SUCCESS_PATTERNS = (
    "Exiting batchmode successfully",
    "Exiting Unity successfully",
)
_MAX_FINDINGS = 10


@dataclass(slots=True)
class UnityLogSummary:
    success_marker_seen: bool
    compile_errors: list[str]
    exceptions: list[str]
    abort_markers: list[str]
    findings: list[str]

    def verdict(self) -> str:
        if self.success_marker_seen and not self.compile_errors and not self.exceptions and not self.abort_markers:
            return "pass"
        return "fail"


def parse_unity_log(text: str) -> UnityLogSummary:
    """Return a deterministic summary of a Unity batchmode log."""
    if not text:
        return UnityLogSummary(False, [], [], [], [])
    success_marker_seen = any(marker in text for marker in _SUCCESS_PATTERNS)
    compile_errors: list[str] = []
    exceptions: list[str] = []
    abort_markers: list[str] = []
    for line in text.splitlines():
        if _COMPILE_ERROR_RE.search(line):
            if len(compile_errors) < _MAX_FINDINGS:
                compile_errors.append(line.strip())
        if _EXCEPTION_HEADER_RE.match(line):
            if len(exceptions) < _MAX_FINDINGS:
                exceptions.append(line.strip())
        for marker in _ABORT_PATTERNS:
            if marker in line:
                if len(abort_markers) < _MAX_FINDINGS:
                    abort_markers.append(line.strip())
                break
    findings: list[str] = []
    findings.extend(compile_errors[:_MAX_FINDINGS])
    findings.extend(exceptions[:_MAX_FINDINGS - len(findings)])
    findings.extend(abort_markers[:_MAX_FINDINGS - len(findings)])
    return UnityLogSummary(
        success_marker_seen=success_marker_seen,
        compile_errors=compile_errors,
        exceptions=exceptions,
        abort_markers=abort_markers,
        findings=findings[:_MAX_FINDINGS],
    )
