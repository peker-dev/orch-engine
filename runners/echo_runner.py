"""Reference runner that echoes invocation context back as the result.

This runner exists to validate the BaseRunnerAdapter interface end-to-end
without depending on Unity or any external binary. It treats the invocation as
a noop "verifier" and produces a deterministic pass result.

Test scripts can override the verdict by setting `invocation.context["echo"]`
to a dict like `{"exit_code": 1, "summary": "intentional fail"}`. This is the
hook that `cycle_e2e_smoke.runner_nonzero_exit_routes_normally` uses to verify
the failure path.
"""

from __future__ import annotations

from typing import Any

from adapters.base import Invocation
from runners.base import BaseRunnerAdapter, RunnerResult


class EchoRunner(BaseRunnerAdapter):
    provider_id = "echo_runner"
    default_timeout_sec = 5

    def run(self, invocation: Invocation) -> RunnerResult:
        override: dict[str, Any] = {}
        if isinstance(invocation.context, dict):
            raw = invocation.context.get("echo")
            if isinstance(raw, dict):
                override = raw
        exit_code = int(override.get("exit_code", 0))
        summary = str(
            override.get("summary")
            or f"echo_runner saw role={invocation.role}, objective={invocation.objective[:80]}"
        )
        verdict = override.get("verdict")
        score_raw = override.get("score")
        score = float(score_raw) if isinstance(score_raw, (int, float)) else None
        findings_raw = override.get("findings", [])
        findings = [str(item) for item in findings_raw] if isinstance(findings_raw, list) else []
        suggestions_raw = override.get("suggested_actions", [])
        suggestions = (
            [str(item) for item in suggestions_raw]
            if isinstance(suggestions_raw, list)
            else []
        )
        return RunnerResult(
            exit_code=exit_code,
            summary=summary,
            stdout_excerpt=str(override.get("stdout") or ""),
            stderr_excerpt=str(override.get("stderr") or ""),
            artifact_paths=[str(p) for p in override.get("artifact_paths", []) if isinstance(p, str)],
            verdict=str(verdict) if isinstance(verdict, str) else None,
            score=score,
            findings=findings,
            suggested_actions=suggestions,
        )


RUNNER = EchoRunner()
