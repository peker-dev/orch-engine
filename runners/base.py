"""Base runner adapter for option C (external standard runner).

Runners replace an LLM call for verifier-family roles. They run a deterministic
external process (Unity batchmode, a lighthouse CLI, a python test harness, ...),
collect its result, and synthesize a `utterance.v1` envelope so the rest of the
engine flow (dispatch loop, reviews/<role>_latest.json, timeline append) keeps
working unchanged.

Design summary (see `memory/option-c-notes.md` for full notes):

- Runner subclasses implement `run(invocation) -> RunnerResult`. The result is a
  small dataclass with `exit_code`, `stdout_excerpt`, `stderr_excerpt`,
  `artifact_paths`, and an optional pre-classified `verdict` ("pass" / "fail" /
  "needs_iteration" / "block").
- `BaseRunnerAdapter.invoke(invocation)` does the engine-side bookkeeping:
  validates the role, pulls `next_speaker_default` from the domain's
  roles.yaml entry, asks the subclass for a `RunnerResult`, then synthesizes a
  utterance.v1 dict that mimics the verifier_functional shape (a fenced
  ```json``` block with result/score/findings/evidence/blocking_issues/
  suggested_actions). The dispatch loop, _coerce_verifier_functional, and
  reviews path are reused verbatim.
- next_speaker_default is mandatory because runners cannot reason about who
  should speak next. Missing field → AdapterFatalError before the runner runs.
- exit_code != 0 → result="fail" but the cycle keeps going. Engine does not
  override the orchestrator decision; the LLM judges what to do next.

Why not let the LLM synthesize the utterance? Because the result of a runner is
already deterministic data (exit codes, log paths). Asking an LLM to re-narrate
that wastes tokens and adds non-determinism that breaks regression smokes.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from adapters.base import (
    AdapterExecutionError,
    AdapterFatalError,
    BaseAdapter,
    Invocation,
    InvocationResult,
    UTTERANCE_SCHEMA_PATH,
    _check_utterance_invariants,
    _validate_schema,
    resolve_role_family,
)


# Runner provider ids that collide with LLM CLI labels are blocked at
# resolve-time, not here. This list is the source of truth referenced by
# `core.app._build_adapter`. Includes `codex_app` (handoff-only label) so a
# runner module cannot shadow it.
RESERVED_LLM_PROVIDERS = frozenset({"claude_cli", "codex_cli", "codex_app"})

_UTTERANCE_SCHEMA_CACHE: dict[str, object] | None = None


def _utterance_schema() -> dict:
    """Load and cache utterance.v1 schema for runner-side validation."""
    global _UTTERANCE_SCHEMA_CACHE
    if _UTTERANCE_SCHEMA_CACHE is None:
        _UTTERANCE_SCHEMA_CACHE = json.loads(
            UTTERANCE_SCHEMA_PATH.read_text(encoding="utf-8")
        )
    return _UTTERANCE_SCHEMA_CACHE  # type: ignore[return-value]


@dataclass(slots=True)
class RunnerResult:
    """Deterministic output of a runner.run() call.

    Subclasses populate this and return it; BaseRunnerAdapter handles utterance
    synthesis. `verdict` is optional — when omitted, exit_code drives the
    default mapping (0 → pass, otherwise → fail). `duration_sec` is optional
    and informational; runners that wrap a subprocess are encouraged to set it
    so timeline / handoff readers can see how long the external work took.
    """

    exit_code: int
    summary: str
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    artifact_paths: list[str] = field(default_factory=list)
    verdict: str | None = None
    score: float | None = None
    findings: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)
    duration_sec: float | None = None


class BaseRunnerAdapter(BaseAdapter, ABC):
    """Abstract base for non-LLM verifier-family adapters.

    Subclasses MUST set `provider_id` (matches the value in roles.yaml
    `default_provider` / `.orch/config/roles.yaml`) and implement `run`. The
    `default_timeout_sec` class attribute lets each runner declare its own
    sensible default (1s for echo, 600s for unity batchmode). limits.yaml-based
    override is a follow-up stride.
    """

    provider_id: str = ""
    default_timeout_sec: int = 60

    def invoke(self, invocation: Invocation) -> InvocationResult:
        family = resolve_role_family(invocation.working_directory, invocation.role)
        if family != "verifier":
            raise AdapterFatalError(
                f"BaseRunnerAdapter only supports family='verifier' "
                f"(got role={invocation.role!r}, family={family!r})"
            )
        next_speaker = _resolve_next_speaker(
            invocation.working_directory, invocation.role
        )
        if not next_speaker:
            raise AdapterFatalError(
                f"Runner-backed role {invocation.role!r} requires "
                f"`next_speaker_default` in domains/<id>/roles.yaml — "
                f"runners cannot infer who speaks next."
            )
        try:
            runner_result = self.run(invocation)
        except (AdapterExecutionError, AdapterFatalError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise AdapterExecutionError(
                f"Runner {self.provider_id!r} raised {type(exc).__name__}: {exc}"
            ) from exc
        utterance = _synthesize_utterance(invocation, runner_result, next_speaker)
        # 합성 utterance 도 LLM adapter 와 동일한 스키마/invariant 검증을 통과해야 한다.
        # subclass 가 잘못된 RunnerResult 를 돌려주면 dispatch loop 에 닿기 전에 명시적 실패.
        try:
            _validate_schema(utterance, _utterance_schema())
            _check_utterance_invariants(utterance)
        except Exception as exc:  # noqa: BLE001
            raise AdapterExecutionError(
                f"Runner {self.provider_id!r} synthesized invalid utterance.v1: {exc}"
            ) from exc
        payload = _synthesize_payload(runner_result)
        return InvocationResult(
            status="ok",
            summary=runner_result.summary or f"{self.provider_id} runner completed",
            payload=payload,
            utterance=utterance,
        )

    @abstractmethod
    def run(self, invocation: Invocation) -> RunnerResult:
        """Execute the external process and return its decoded result."""
        raise NotImplementedError


def _resolve_next_speaker(working_directory: str | Path, role: str) -> str | None:
    """Look up `next_speaker_default` for a custom role in domains/<id>/roles.yaml."""
    # 도메인 roles.yaml 캐시는 adapters.base 가 관리. 여기서는 그 결과를 다시 훑어 next_speaker_default 만 추출.
    from adapters.base import _load_domain_custom_roles

    for entry in _load_domain_custom_roles(working_directory):
        if entry.get("id") == role:
            value = entry.get("next_speaker_default")
            if isinstance(value, str) and value.strip():
                return value.strip()
            return None
    return None


def resolve_runner_config(working_directory: str | Path, role: str) -> dict:
    """Look up `runner_config` (free-form dict) for a custom role.

    Returns an empty dict when the role has no `runner_config` field — runners
    that need configuration should validate their own required keys and raise
    AdapterFatalError with a domain-author-friendly message.
    """
    from adapters.base import _load_domain_custom_roles

    for entry in _load_domain_custom_roles(working_directory):
        if entry.get("id") == role:
            cfg = entry.get("runner_config")
            return dict(cfg) if isinstance(cfg, dict) else {}
    return {}


def _default_verdict(exit_code: int) -> str:
    return "pass" if exit_code == 0 else "fail"


def _build_blocking_issues(result: RunnerResult) -> list[str]:
    """공통 blocking_issues 생성 — payload 와 utterance fenced JSON 양쪽이 동일 값 사용."""
    if result.exit_code == 0:
        return []
    first_line = result.stderr_excerpt.splitlines()[0] if result.stderr_excerpt else ""
    text = f"runner exited with code {result.exit_code}"
    if first_line:
        text += f": {first_line}"
    return [text]


def _synthesize_payload(result: RunnerResult) -> dict[str, object]:
    """Build the legacy verifier_functional payload the engine still consumes."""
    verdict = (result.verdict or _default_verdict(result.exit_code)).strip().lower()
    score = result.score
    if score is None:
        score = 1.0 if verdict == "pass" else 0.0
    return {
        "summary": result.summary or "runner completed",
        "result": verdict,
        "score": float(score),
        "findings": list(result.findings),
        "evidence": list(result.artifact_paths),
        "blocking_issues": _build_blocking_issues(result),
        "suggested_actions": list(result.suggested_actions),
    }


def _synthesize_utterance(
    invocation: Invocation, result: RunnerResult, next_speaker: str
) -> dict[str, object]:
    """Compose a utterance.v1-shaped dict from a RunnerResult.

    Body carries human-readable narrative (summary / verdict / artifacts /
    stdout-stderr excerpts) for timeline / handoff visibility, plus a fenced
    ```json``` block mirroring the verifier_functional structured payload so
    body re-parsers (timeline tools, future analyzers) see the same numbers
    as the engine's payload path. The fenced block is a copy of
    `_synthesize_payload` minus the `summary` key, kept in sync via
    `_build_blocking_issues` etc.
    """
    verdict = (result.verdict or _default_verdict(result.exit_code)).strip().lower()
    body_lines = [
        f"# {invocation.role} (runner)",
        f"summary: {result.summary or 'runner completed'}",
        f"verdict: {verdict}",
        f"exit_code: {result.exit_code}",
    ]
    if result.artifact_paths:
        body_lines.append("")
        body_lines.append("artifacts:")
        for path in result.artifact_paths:
            body_lines.append(f"- {path}")
    if result.stdout_excerpt:
        body_lines.append("")
        body_lines.append("stdout (excerpt):")
        body_lines.append("```")
        body_lines.append(result.stdout_excerpt)
        body_lines.append("```")
    if result.stderr_excerpt:
        body_lines.append("")
        body_lines.append("stderr (excerpt):")
        body_lines.append("```")
        body_lines.append(result.stderr_excerpt)
        body_lines.append("```")
    # Embed the verifier_functional fenced json block so any future component
    # that re-reads body still finds the structured verdict.
    fenced_payload = {
        "result": verdict,
        "score": float(result.score) if result.score is not None else (
            1.0 if verdict == "pass" else 0.0
        ),
        "findings": list(result.findings),
        "evidence": list(result.artifact_paths),
        "blocking_issues": _build_blocking_issues(result),
        "suggested_actions": list(result.suggested_actions),
    }
    body_lines.append("")
    body_lines.append("```json")
    body_lines.append(json.dumps(fenced_payload, ensure_ascii=False))
    body_lines.append("```")
    return {
        "speaker": invocation.role,
        "body": "\n".join(body_lines),
        "next_speaker": next_speaker,
        "declare_done": False,
        "arbitration": None,
    }
