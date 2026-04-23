from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.process_runner import run_command
from tools.schema_utils import (
    find_first_dict_candidate as _shared_find_first_dict_candidate,
    find_payload_candidate as _shared_find_payload_candidate,
    validate_schema as _shared_validate_schema,
)


ENGINE_ROOT = Path(__file__).resolve().parent.parent
ROLE_SCHEMA_MAP = {
    "planner": ENGINE_ROOT / "schemas" / "roles" / "planner.result.v1.json",
    "builder": ENGINE_ROOT / "schemas" / "roles" / "builder.result.v1.json",
    "verifier_functional": ENGINE_ROOT / "schemas" / "roles" / "verifier_functional.result.v1.json",
    "verifier_human": ENGINE_ROOT / "schemas" / "roles" / "verifier_human.result.v1.json",
    "orchestrator": ENGINE_ROOT / "schemas" / "roles" / "orchestrator.result.v1.json",
}
ROLE_TIMEOUTS = {
    "planner": 180,
    "builder": 600,
    "verifier_functional": 300,
    "verifier_human": 240,
    "orchestrator": 120,
}


@dataclass(slots=True)
class Invocation:
    role: str
    objective: str
    working_directory: str
    context: dict[str, object] | None = None


@dataclass(slots=True)
class InvocationResult:
    status: str
    summary: str
    payload: dict[str, object] | None = None
    # Phase 2 free-utterance routing metadata (D4/D5). Present only when the
    # provider returned a utterance.v1 shape; contains the original speaker /
    # next_speaker / declare_done / arbitration fields BEFORE coerce dropped
    # them in favour of the legacy role payload. run_cycle reads this to
    # dispatch the next speaker; absent -> engine falls back to the legacy
    # role chain (planner -> builder -> verifier_f -> verifier_h -> orchestrator).
    utterance: dict[str, object] | None = None


class AdapterExecutionError(RuntimeError):
    """Recoverable adapter failure (retryable or out of retries)."""


class AdapterFatalError(AdapterExecutionError):
    """Adapter failure that must not be retried (auth, missing binary, denied sandbox)."""


class AdapterQuotaExceededError(AdapterExecutionError):
    """Adapter failed because the provider's usage quota / rate limit is exhausted.

    Distinct from AdapterFatalError because the condition is expected to recover
    on its own once the provider's rolling window resets. The engine may opt to
    pause and resume via `tools.usage_wait` instead of blocking the cycle.
    """


FATAL_EXIT_CODES = {
    127,  # command not found (POSIX)
    9009,  # command not found (Windows cmd)
}

FATAL_STDERR_MARKERS = (
    "not authenticated",
    "unauthorized",
    "authentication failed",
    "please run `claude login`",
    "please run `codex login`",
    "permission denied",
    "sandbox denied",
    "command not found",
    "is not recognized as an internal or external command",
)

# Markers emitted by Claude/Codex CLIs when the subscription quota or rate limit
# is exhausted. These are not fatal in the auth/binary sense — the quota will
# refill on the provider's rolling window, so the engine treats them as
# recoverable via `AdapterQuotaExceededError`.
# Note: bare "429" was tried earlier but matched paths and unrelated counters,
# so only the parenthesized form "(429)" is kept here. Claude CLI's real quota
# message already contains "too many requests".
QUOTA_STDERR_MARKERS = (
    "rate limit",
    "rate-limit",
    "quota exceeded",
    "usage limit",
    "usage-limit",
    "5-hour limit",
    "5 hour limit",
    "weekly limit",
    "limit reached",
    "you have reached your",
    "too many requests",
    "(429)",
    " 429 ",
)


class BaseAdapter(ABC):
    @abstractmethod
    def invoke(self, invocation: Invocation) -> InvocationResult:
        raise NotImplementedError


class BaseCliAdapter(BaseAdapter):
    provider_id: str = ""
    provider_label: str = ""

    def invoke(self, invocation: Invocation) -> InvocationResult:
        schema_path = ROLE_SCHEMA_MAP[invocation.role]
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        required_keys = list(schema.get("required", []))
        request_id = f"{self.provider_id}-{invocation.role}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        run_root = _build_run_root(Path(invocation.working_directory), request_id)
        prompt = _render_prompt(invocation, schema_path, required_keys)
        request = {
            "version": "1.0",
            "request_id": request_id,
            "provider": self.provider_id,
            "role": invocation.role,
            "objective": invocation.objective,
            "working_directory": str(Path(invocation.working_directory).resolve()),
            "mode": "run-cycle",
            "context_summary": _summarize_context(invocation.context),
            "input_files": [],
            "write_scope": _default_write_scope(invocation),
            "output_schema_path": str(schema_path),
            "timeout_sec": ROLE_TIMEOUTS[invocation.role],
        }

        last_error: str | None = None
        for attempt in range(1, 3):
            attempt_dir = run_root / f"attempt-{attempt:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            attempt_prompt = prompt if attempt == 1 else _append_retry_prompt(prompt, last_error)
            provider_result_path = attempt_dir / "provider-result.json"
            command, uses_stdin = self.build_command(
                invocation=invocation,
                schema_path=schema_path,
                schema_text=schema_path.read_text(encoding="utf-8"),
                provider_result_path=provider_result_path,
            )

            _write_json(attempt_dir / "request.json", request)
            _write_text(attempt_dir / "prompt.txt", attempt_prompt)
            _write_text(attempt_dir / "schema.json", schema_path.read_text(encoding="utf-8"))
            _write_json(attempt_dir / "command.json", {"command": command, "stdin": uses_stdin})

            try:
                completed = run_command(
                    command,
                    cwd=invocation.working_directory,
                    stdin_text=attempt_prompt if uses_stdin else None,
                    timeout_sec=ROLE_TIMEOUTS[invocation.role],
                )
            except (FileNotFoundError, PermissionError) as exc:
                last_error = f"{self.provider_label} could not start for role={invocation.role}: {exc}"
                _write_text(attempt_dir / "stdout.txt", "")
                _write_text(attempt_dir / "stderr.txt", last_error)
                _write_json(
                    attempt_dir / "error.json",
                    {
                        "class": "fatal_process_failure",
                        "fatal_reason": type(exc).__name__,
                        "message": last_error,
                    },
                )
                raise AdapterFatalError(last_error) from exc
            except subprocess.TimeoutExpired as exc:
                partial_stdout = _coerce_text(getattr(exc, "stdout", None))
                partial_stderr = _coerce_text(getattr(exc, "stderr", None))
                last_error = (
                    f"{self.provider_label} timed out after {ROLE_TIMEOUTS[invocation.role]}s "
                    f"for role={invocation.role}"
                )
                _write_text(attempt_dir / "stdout.txt", partial_stdout)
                _write_text(
                    attempt_dir / "stderr.txt",
                    (partial_stderr + "\n\n" if partial_stderr else "") + last_error,
                )
                _write_json(
                    attempt_dir / "error.json",
                    {
                        "class": "timeout",
                        "message": last_error,
                        "partial_stdout_chars": len(partial_stdout),
                        "partial_stderr_chars": len(partial_stderr),
                    },
                )
                if attempt == 2:
                    raise AdapterExecutionError(last_error) from exc
                continue

            _write_text(attempt_dir / "stdout.txt", completed.stdout or "")
            _write_text(attempt_dir / "stderr.txt", completed.stderr or "")

            if completed.returncode != 0:
                stderr_text = completed.stderr or ""
                quota_reason = _detect_quota_failure(stderr_text=stderr_text)
                fatal_reason = None if quota_reason else _detect_fatal_failure(
                    exit_code=completed.returncode,
                    stderr_text=stderr_text,
                )
                last_error = (
                    f"{self.provider_label} exited with code {completed.returncode} for role={invocation.role}"
                )
                _write_json(
                    attempt_dir / "error.json",
                    {
                        "class": (
                            "quota_exceeded" if quota_reason
                            else "fatal_process_failure" if fatal_reason
                            else "non_zero_exit"
                        ),
                        "exit_code": completed.returncode,
                        "fatal_reason": fatal_reason,
                        "quota_reason": quota_reason,
                        "message": last_error,
                    },
                )
                if quota_reason:
                    raise AdapterQuotaExceededError(f"{last_error} ({quota_reason})")
                if fatal_reason:
                    raise AdapterFatalError(f"{last_error} ({fatal_reason})")
                if attempt == 2:
                    raise AdapterExecutionError(last_error)
                continue

            try:
                payload = self.extract_payload(
                    stdout_text=completed.stdout or "",
                    provider_result_path=provider_result_path,
                    schema=schema,
                )
                # Phase 2 free-utterance routing (D4/D5): if payload is an
                # utterance.v1 shape, stash its routing metadata BEFORE coerce
                # strips it down to legacy role fields. run_cycle will read this
                # to dispatch the next speaker; absent -> legacy chain fallback.
                utterance_meta: dict[str, object] | None = None
                if isinstance(payload, dict) and _UTTERANCE_V1_KEYS.issubset(payload.keys()):
                    utterance_meta = {
                        "speaker": payload.get("speaker"),
                        "next_speaker": payload.get("next_speaker"),
                        "declare_done": bool(payload.get("declare_done") or False),
                        "arbitration": payload.get("arbitration"),
                    }
                # Phase 2 transition: coerce utterance.v1 → legacy BEFORE normalize.
                # Ordering matters — _normalize_role_payload fills in required role
                # fields, so the coerced output must pass through normalize so that
                # fenced-json payloads lacking some fields still validate.
                # See reviewer finding A-5.
                coercer = _UTTERANCE_COERCERS.get(invocation.role)
                if coercer is not None:
                    payload = coercer(payload)
                payload = _normalize_role_payload(invocation.role, payload)
                _validate_schema(payload, schema)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                _write_json(
                    attempt_dir / "error.json",
                    {
                        "class": _classify_parse_error(exc),
                        "message": last_error,
                    },
                )
                if attempt == 2:
                    raise AdapterExecutionError(last_error) from exc
                continue

            normalized = {
                "status": "ok",
                "provider": self.provider_id,
                "role": invocation.role,
                "summary": str(payload.get("summary", f"{self.provider_label} completed {invocation.role}")),
                "payload": payload,
            }
            _write_json(attempt_dir / "normalized.json", normalized)
            return InvocationResult(
                status="ok",
                summary=str(normalized["summary"]),
                payload=payload,
                utterance=utterance_meta,
            )

        raise AdapterExecutionError(last_error or f"{self.provider_label} failed for role={invocation.role}")

    @abstractmethod
    def build_command(
        self,
        *,
        invocation: Invocation,
        schema_path: Path,
        schema_text: str,
        provider_result_path: Path,
    ) -> tuple[list[str], bool]:
        """Return the subprocess argv and whether the rendered prompt is piped via stdin."""
        raise NotImplementedError

    @abstractmethod
    def extract_payload(
        self,
        *,
        stdout_text: str,
        provider_result_path: Path,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError


def _build_run_root(working_directory: Path, request_id: str) -> Path:
    return working_directory / ".orch" / "runtime" / "adapter_runs" / request_id


def _render_prompt(invocation: Invocation, schema_path: Path, required_keys: list[str]) -> str:
    role_guidance = {
        "planner": (
            "Break the objective into the smallest useful next task. "
            "If previous_reviews is provided, treat each entry as a uniform triad "
            "{kind, severity, suggestions}: `kind` tells you who produced it "
            "(functional/human/handoff/orchestrator), `severity` is the headline "
            "verdict (pass/fail/needs_iteration/block/complete_cycle/info/warning/critical), "
            "and `suggestions` lists the concrete items still to address. Prioritize tasks "
            "that resolve non-info severities before opening new work.\n\n"
            "--- Output format (Phase 2 transition — planner only, other roles still use legacy) ---\n"
            "The engine accepts EITHER of two JSON shapes. Pick (B) when possible; (A) remains valid for now.\n"
            "(A) Legacy planner.result.v1 shape: keys summary, plan_summary, tasks, risks (schema above).\n"
            "(B) PREFERRED utterance.v1 shape:\n"
            "    {\n"
            "      \"speaker\": \"planner\",\n"
            "      \"body\": \"<markdown free-form: observations, rationale, the plan in plain prose>\",\n"
            "      \"next_speaker\": \"builder\"\n"
            "    }\n"
            "    During the transition you MUST embed a fenced ```json``` block inside body containing "
            "the minimum legacy payload so the engine can still dispatch work while other roles migrate:\n"
            "    ```json\n"
            "    {\"plan_summary\": \"...\", \"tasks\": [{\"id\":\"task-1\",\"title\":\"...\","
            "\"acceptance\":\"...\",\"priority\":\"medium\",\"notes\":[]}], \"risks\": []}\n"
            "    ```\n"
            "    The fenced-block requirement will be removed once builder / verifiers are also migrated."
        ),
        "builder": (
            "Do the actual work in the working directory before you return JSON.\n\n"
            "--- Output format (Phase 2 transition — builder) ---\n"
            "The engine accepts EITHER of two JSON shapes. Pick (B) when possible; (A) remains valid for now.\n"
            "(A) Legacy builder.result.v1 shape: keys summary, change_summary, files_changed, artifact_paths, self_check.\n"
            "(B) PREFERRED utterance.v1 shape:\n"
            "    {\n"
            "      \"speaker\": \"builder\",\n"
            "      \"body\": \"<markdown free-form: what you did, why, surprises, caveats>\",\n"
            "      \"next_speaker\": \"verifier_functional\"\n"
            "    }\n"
            "    During the transition you MUST embed a fenced ```json``` block inside body containing "
            "the minimum legacy payload so the engine can still dispatch review while other roles migrate:\n"
            "    ```json\n"
            "    {\"change_summary\": \"...\", \"files_changed\": [\"...\"], \"artifact_paths\": [\"...\"], "
            "\"self_check\": {\"summary\":\"...\",\"unresolved\":[]}}\n"
            "    ```\n"
            "    The fenced-block requirement will be removed once verifier/orchestrator also migrate."
        ),
        "verifier_functional": (
            "Verify using concrete evidence such as commands, tests, logs, or produced files whenever possible. "
            "Then compare the final artifacts against the master objective itself, not just the active task's acceptance. "
            "If the objective calls for something that does not yet exist on disk (e.g. objective mentions "
            "'responsive + WCAG AA' but no styles.css or accessibility check has been produced), include that gap "
            "in suggested_actions even when the active task looks done. Never report suggested_actions=[] while "
            "an obvious objective-level item is still missing.\n\n"
            "--- Output format (Phase 2 transition — verifier_functional) ---\n"
            "The engine accepts EITHER of two JSON shapes. Pick (B) when possible.\n"
            "(A) Legacy verifier_functional.result.v1 shape: summary, result, score, findings, evidence, blocking_issues, suggested_actions.\n"
            "(B) PREFERRED utterance.v1 shape:\n"
            "    {\n"
            "      \"speaker\": \"verifier_functional\",\n"
            "      \"body\": \"<markdown: what you tested, what evidence you gathered, what is broken or missing>\",\n"
            "      \"next_speaker\": \"verifier_human\"\n"
            "    }\n"
            "    Embed a fenced ```json``` block inside body carrying the legacy payload:\n"
            "    ```json\n"
            "    {\"result\":\"pass|fail|needs_iteration|block\",\"score\":0.0,\"findings\":[],\"evidence\":[],"
            "\"blocking_issues\":[],\"suggested_actions\":[]}\n"
            "    ```"
        ),
        "verifier_human": (
            "Review from a human perspective and focus on quality, clarity, polish, and usability. "
            "Judge whether a real user would feel the master objective has been achieved end-to-end — not just "
            "whether the active task's acceptance is met. If the objective promises an experience that the "
            "current artifacts clearly do not deliver (placeholder content, missing responsive behavior, broken "
            "links, unverified accessibility), raise it in findings and suggested_actions even if the active "
            "task itself is technically done.\n\n"
            "--- Output format (Phase 2 transition — verifier_human) ---\n"
            "The engine accepts EITHER of two JSON shapes. Pick (B) when possible.\n"
            "(A) Legacy verifier_human.result.v1 shape: summary, result, score, findings, strengths, comparison_notes, suggested_actions.\n"
            "(B) PREFERRED utterance.v1 shape:\n"
            "    {\n"
            "      \"speaker\": \"verifier_human\",\n"
            "      \"body\": \"<markdown: user-perspective read, strengths, weaknesses, suggestions>\",\n"
            "      \"next_speaker\": \"orchestrator\"\n"
            "    }\n"
            "    Embed a fenced ```json``` block inside body carrying the legacy payload:\n"
            "    ```json\n"
            "    {\"result\":\"pass|fail|needs_iteration|block\",\"score\":0.0,\"findings\":[],\"strengths\":[],"
            "\"comparison_notes\":[],\"suggested_actions\":[]}\n"
            "    ```"
        ),
        "orchestrator": (
            "Decide whether this cycle is complete, should iterate, or is blocked. "
            "Weigh the original objective against the verifier reviews, suggested_actions, blocking_issues, "
            "and score_history supplied in context. The objective is the master mandate — a cycle is complete "
            "only when the objective is truly satisfied, not merely when scores exceed a threshold. If any "
            "suggested_actions remain actionable, prefer needs_iteration. "
            "Do not rely on verifiers alone: compare the master objective wording directly with existing_artifacts "
            "in context. If the objective demands something the artifacts clearly lack (e.g. responsive CSS, "
            "accessibility evidence, real content instead of placeholders) but verifiers missed it, raise it "
            "yourself in unresolved_items and choose needs_iteration. "
            "Use blocked only when the reviews explicitly declare a hard stop or the same failure has repeated "
            "without progress.\n\n"
            "--- Output format (Phase 2 transition — orchestrator) ---\n"
            "The engine accepts EITHER of two JSON shapes. Pick (B) when possible.\n"
            "(A) Legacy orchestrator.result.v1 shape: summary, decision, next_state, reason, unresolved_items, recommended_next_action.\n"
            "(B) PREFERRED utterance.v1 shape:\n"
            "    {\n"
            "      \"speaker\": \"orchestrator\",\n"
            "      \"body\": \"<markdown: your judgment, reasoning against objective, what blocks or unblocks>\",\n"
            "      \"next_speaker\": \"planner\"\n"
            "    }\n"
            "    Embed a fenced ```json``` block inside body carrying the legacy payload:\n"
            "    ```json\n"
            "    {\"decision\":\"complete_cycle|needs_iteration|blocked\","
            "\"next_state\":\"completed|iterating|blocked\","
            "\"reason\":\"...\",\"unresolved_items\":[],\"recommended_next_action\":\"...\"}\n"
            "    ```"
        ),
    }[invocation.role]
    scope_guidance = {
        "planner": "Do not modify files.",
        "builder": "Modify only files necessary for the objective and keep the change scope tight.",
        "verifier_functional": "Avoid editing project files unless a verification step requires generated test artifacts.",
        "verifier_human": "Do not modify files during review.",
        "orchestrator": "Do not modify files. You are a judgment-only role.",
    }[invocation.role]
    if invocation.role in {
        "planner", "builder", "verifier_functional", "verifier_human", "orchestrator"
    }:
        format_rule = (
            "Return exactly one JSON object. The OUTER envelope must be plain JSON "
            "(no markdown, no outer code fences). Phase 2 transition exception: if you "
            "choose the utterance.v1 shape, you MAY use markdown inside the 'body' field "
            "and embed exactly one fenced ```json``` block carrying the legacy payload. "
            "The 'no markdown / no fences' rule still applies to everything outside body.\n"
        )
    else:
        format_rule = (
            "Return exactly one JSON object that matches the enforced schema.\n"
            "Do not use markdown.\n"
            "Do not wrap the JSON in code fences.\n"
        )
    return (
        f"You are running as orch-engine role '{invocation.role}'.\n"
        + format_rule
        + f"{role_guidance}\n"
        f"{scope_guidance}\n"
        f"Objective: {invocation.objective}\n"
        f"Working directory: {Path(invocation.working_directory).resolve()}\n"
        f"Context summary: {_summarize_context(invocation.context)}\n"
        f"Schema path: {schema_path}\n"
        f"Required top-level keys: {', '.join(required_keys)}\n"
        "If a list field has nothing to report, return an empty array.\n"
        "If you are unsure, choose the safest schema-valid response.\n"
    )


def _default_write_scope(invocation: Invocation) -> list[str]:
    if invocation.role in {"builder", "verifier_functional"}:
        return [str(Path(invocation.working_directory).resolve())]
    return []


def _append_retry_prompt(prompt: str, last_error: str | None) -> str:
    error_line = ""
    if last_error:
        reason = last_error.strip()
        if len(reason) > 400:
            reason = reason[:400] + "...(truncated)"
        error_line = f"Previous attempt failed with: {reason}\n"
    return (
        prompt
        + "\nRetry instruction:\n"
        + error_line
        + "Return one JSON object only.\n"
        + "Do not add commentary outside the schema.\n"
        + "Every required field must be present.\n"
        + "Fix the specific problem above in this attempt.\n"
    )


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    return str(value)


def _detect_fatal_failure(exit_code: int, stderr_text: str) -> str | None:
    if exit_code in FATAL_EXIT_CODES:
        return f"exit_code_{exit_code}"
    lowered = stderr_text.lower()
    for marker in FATAL_STDERR_MARKERS:
        if marker in lowered:
            return f"stderr_marker:{marker}"
    return None


def _detect_quota_failure(stderr_text: str) -> str | None:
    """Return a quota marker if the CLI complained about rate/quota limits.

    Checked before the auth/fatal detection so a quota-exceeded error is not
    misclassified as a login problem.
    """
    lowered = stderr_text.lower()
    for marker in QUOTA_STDERR_MARKERS:
        if marker in lowered:
            return f"stderr_marker:{marker}"
    return None


def _classify_parse_error(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, FileNotFoundError):
        return "missing_result_file"
    if "json" in text and "decode" in text:
        return "json_decode"
    if "missing required key" in text:
        return "schema_missing_field"
    if "unexpected keys" in text:
        return "schema_extra_field"
    if "expected " in text:
        return "schema_type_mismatch"
    if "could not extract" in text:
        return "payload_not_found"
    return "parse_error"


def _summarize_context(context: dict[str, object] | None) -> str:
    if not context:
        return "No extra context."
    text = json.dumps(context, ensure_ascii=False, sort_keys=True)
    if len(text) > 2000:
        return text[:2000] + "...(truncated)"
    return text


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def find_payload_candidate(value: Any, required: set[str]) -> dict[str, Any] | None:
    """Re-exported for adapter modules; implementation lives in tools.schema_utils."""
    return _shared_find_payload_candidate(value, required)


def find_first_dict_candidate(value: Any) -> dict[str, Any] | None:
    """Re-exported for adapter modules; implementation lives in tools.schema_utils."""
    return _shared_find_first_dict_candidate(value)


def _normalize_role_payload(role: str, payload: dict[str, Any]) -> dict[str, Any]:
    if role == "planner":
        if isinstance(payload.get("tasks"), list):
            normalized_tasks: list[dict[str, Any]] = []
            for index, item in enumerate(payload.get("tasks", []), start=1):
                if not isinstance(item, dict):
                    normalized_tasks.append(
                        {
                            "id": f"task-{index}",
                            "title": str(item),
                            "acceptance": "Task objective completed.",
                            "priority": "medium",
                            "notes": [],
                        }
                    )
                    continue
                item_done_when = item.get("done_when")
                item_acceptance = ""
                if isinstance(item_done_when, list) and item_done_when:
                    item_acceptance = "; ".join(str(value) for value in item_done_when)
                item_acceptance = item_acceptance or str(
                    item.get("acceptance") or item.get("description") or "Task objective completed."
                )
                item_notes = item.get("notes")
                if not isinstance(item_notes, list):
                    item_notes = []
                normalized_tasks.append(
                    {
                        "id": str(item.get("id") or f"task-{index}"),
                        "title": str(item.get("title") or item.get("task") or f"Task {index}"),
                        "acceptance": item_acceptance,
                        "priority": str(item.get("priority") or "medium"),
                        "notes": [str(value) for value in item_notes],
                    }
                )
            return {
                "summary": str(payload.get("summary") or payload.get("rationale") or "Planner generated a task."),
                "plan_summary": str(payload.get("plan_summary") or payload.get("rationale") or payload.get("summary") or "Planner generated a task."),
                "tasks": normalized_tasks,
                "risks": [str(item) for item in payload.get("risks", [])] if isinstance(payload.get("risks"), list) else [],
            }
        task_title = str(payload.get("task") or payload.get("title") or payload.get("summary") or "Planned task")
        done_when = payload.get("done_when")
        acceptance = ""
        if isinstance(done_when, list) and done_when:
            acceptance = "; ".join(str(item) for item in done_when)
        acceptance = acceptance or str(payload.get("acceptance") or "Task objective completed.")
        notes = payload.get("notes")
        if not isinstance(notes, list):
            next_actions = payload.get("next_actions")
            notes = [str(item) for item in next_actions] if isinstance(next_actions, list) else []
        return {
            "summary": str(payload.get("summary") or payload.get("rationale") or task_title),
            "plan_summary": str(payload.get("plan_summary") or payload.get("rationale") or task_title),
            "tasks": [
                {
                    "id": str(payload.get("id") or "task-001"),
                    "title": task_title,
                    "acceptance": acceptance,
                    "priority": str(payload.get("priority") or "medium"),
                    "notes": notes,
                }
            ],
            "risks": [str(item) for item in payload.get("risks", [])] if isinstance(payload.get("risks"), list) else [],
        }

    if role == "builder":
        files_changed = payload.get("files_changed")
        if not isinstance(files_changed, list):
            alt = payload.get("files") or payload.get("modified_files")
            files_changed = [str(item) for item in alt] if isinstance(alt, list) else []
        artifact_paths = payload.get("artifact_paths")
        if not isinstance(artifact_paths, list):
            alt = payload.get("artifacts") or payload.get("evidence")
            artifact_paths = [str(item) for item in alt] if isinstance(alt, list) else []
        unresolved = payload.get("unresolved")
        if not isinstance(unresolved, list):
            unresolved = payload.get("issues")
        unresolved = [str(item) for item in unresolved] if isinstance(unresolved, list) else []
        self_check = payload.get("self_check")
        self_check_summary = ""
        if isinstance(self_check, dict):
            self_check_summary = str(
                self_check.get("summary")
                or self_check.get("notes")
                or self_check.get("status")
                or payload.get("self_check_summary")
                or ""
            )
            if not unresolved:
                unresolved = _ensure_string_list(self_check.get("unresolved") or self_check.get("issues"))
        else:
            self_check_summary = str(payload.get("self_check_summary") or payload.get("self_check") or "")
        summary = str(payload.get("summary") or payload.get("change_summary") or "Builder completed the requested work.")
        return {
            "summary": summary,
            "change_summary": str(payload.get("change_summary") or summary),
            "files_changed": files_changed,
            "artifact_paths": artifact_paths,
            "self_check": {
                "summary": self_check_summary or summary,
                "unresolved": unresolved,
            },
        }

    if role == "verifier_functional":
        return {
            "summary": str(payload.get("summary") or payload.get("verdict") or "Functional review completed."),
            "result": _normalize_result(payload.get("result") or payload.get("verdict") or payload.get("status")),
            "score": _normalize_score(payload.get("score")),
            "findings": _ensure_string_list(payload.get("findings") or payload.get("issues")),
            "evidence": _ensure_string_list(payload.get("evidence") or payload.get("logs") or payload.get("files_checked")),
            "blocking_issues": _ensure_string_list(payload.get("blocking_issues") or payload.get("critical_issues")),
            "suggested_actions": _ensure_string_list(payload.get("suggested_actions") or payload.get("recommendations") or payload.get("next_actions")),
        }

    if role == "verifier_human":
        return {
            "summary": str(payload.get("summary") or payload.get("verdict") or "Human review completed."),
            "result": _normalize_result(payload.get("result") or payload.get("verdict") or payload.get("status")),
            "score": _normalize_score(payload.get("score")),
            "findings": _ensure_string_list(payload.get("findings") or payload.get("issues") or payload.get("weaknesses")),
            "strengths": _ensure_string_list(payload.get("strengths")),
            "comparison_notes": _ensure_string_list(payload.get("comparison_notes") or payload.get("comparisons")),
            "suggested_actions": _ensure_string_list(payload.get("suggested_actions") or payload.get("recommendations") or payload.get("next_actions")),
        }

    if role == "orchestrator":
        decision = str(payload.get("decision") or payload.get("verdict") or "").strip()
        next_state = str(payload.get("next_state") or payload.get("state") or "").strip()
        return {
            "summary": str(payload.get("summary") or payload.get("rationale") or "Orchestrator judged the cycle."),
            "decision": decision,
            "next_state": next_state,
            "reason": str(payload.get("reason") or payload.get("rationale") or payload.get("summary") or ""),
            "unresolved_items": _ensure_string_list(
                payload.get("unresolved_items")
                or payload.get("unresolved")
                or payload.get("remaining_issues")
            ),
            "recommended_next_action": str(
                payload.get("recommended_next_action")
                or payload.get("next_action")
                or payload.get("recommendation")
                or ""
            ),
        }

    return payload


def _ensure_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _normalize_score(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number < 0:
            return 0.0
        if number > 1:
            return 1.0
        return number
    return 0.0


def _normalize_result(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"pass", "ok", "success", "approve", "approved"}:
        return "pass"
    if text in {"fail", "failed", "error"}:
        return "fail"
    if text in {"block", "blocked"}:
        return "block"
    return "needs_iteration"


def _validate_schema(instance: Any, schema: dict[str, Any], path: str = "root") -> None:
    """Thin internal alias so callers inside adapters keep using `_validate_schema`."""
    _shared_validate_schema(instance, schema, path)


_UTTERANCE_V1_KEYS = frozenset({"speaker", "body", "next_speaker"})
_FENCED_JSON_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
_FENCED_JSON_TILDE_RE = re.compile(r"~~~json\s*\n(.*?)\n~~~", re.DOTALL)


def _extract_fenced_json(body: str) -> dict[str, Any] | None:
    for pattern in (_FENCED_JSON_RE, _FENCED_JSON_TILDE_RE):
        match = pattern.search(body)
        if not match:
            continue
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _coerce_planner_utterance_to_legacy(payload: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 transition: if planner returned a utterance.v1 shape, fold it into the
    legacy planner.result.v1 shape the core pipeline still consumes. Non-utterance
    payloads pass through untouched. D4 / D15~D18 ref: memory/autonomy-redesign-notes.md.
    """
    if not isinstance(payload, dict):
        return payload
    if not _UTTERANCE_V1_KEYS.issubset(payload.keys()):
        return payload
    body = str(payload.get("body") or "")
    fenced = _extract_fenced_json(body) or {}
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    summary_src = fenced.get("summary") or payload.get("summary") or first_line or "planner utterance"
    summary = str(summary_src).strip() or "planner utterance"
    plan_summary_src = fenced.get("plan_summary") or summary
    plan_summary = str(plan_summary_src).strip() or summary
    tasks_raw = fenced.get("tasks")
    tasks: list[dict[str, Any]]
    if isinstance(tasks_raw, list) and tasks_raw:
        tasks = [t for t in tasks_raw if isinstance(t, dict)]
    else:
        tasks = [
            {
                "id": "task-1",
                "title": summary[:120] or "planner utterance",
                "acceptance": "derived from planner utterance body",
                "priority": "medium",
                "notes": [],
            }
        ]
    risks_raw = fenced.get("risks")
    risks = [str(r) for r in risks_raw] if isinstance(risks_raw, list) else []
    return {
        "summary": summary,
        "plan_summary": plan_summary,
        "tasks": tasks,
        "risks": risks,
    }


def _fenced_list(value: Any) -> list[str] | None:
    """Return a list of strings if value is a list, else None (so normalize fallbacks run)."""
    if isinstance(value, list):
        return [str(item) for item in value]
    return None


def _coerce_verifier_functional_utterance_to_legacy(payload: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 mirror for verifier_functional. See _coerce_planner_utterance_to_legacy."""
    if not isinstance(payload, dict):
        return payload
    if not _UTTERANCE_V1_KEYS.issubset(payload.keys()):
        return payload
    body = str(payload.get("body") or "")
    fenced = _extract_fenced_json(body) or {}
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    summary = str(
        fenced.get("summary") or payload.get("summary") or first_line or "Functional review completed."
    ).strip() or "Functional review completed."
    return {
        "summary": summary,
        "result": fenced.get("result"),
        "score": fenced.get("score"),
        "findings": _fenced_list(fenced.get("findings")),
        "evidence": _fenced_list(fenced.get("evidence")),
        "blocking_issues": _fenced_list(fenced.get("blocking_issues")),
        "suggested_actions": _fenced_list(fenced.get("suggested_actions")),
    }


def _coerce_verifier_human_utterance_to_legacy(payload: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 mirror for verifier_human."""
    if not isinstance(payload, dict):
        return payload
    if not _UTTERANCE_V1_KEYS.issubset(payload.keys()):
        return payload
    body = str(payload.get("body") or "")
    fenced = _extract_fenced_json(body) or {}
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    summary = str(
        fenced.get("summary") or payload.get("summary") or first_line or "Human review completed."
    ).strip() or "Human review completed."
    return {
        "summary": summary,
        "result": fenced.get("result"),
        "score": fenced.get("score"),
        "findings": _fenced_list(fenced.get("findings")),
        "strengths": _fenced_list(fenced.get("strengths")),
        "comparison_notes": _fenced_list(fenced.get("comparison_notes")),
        "suggested_actions": _fenced_list(fenced.get("suggested_actions")),
    }


def _coerce_orchestrator_utterance_to_legacy(payload: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 mirror for orchestrator.

    Unlike the verifiers, orchestrator.decision / next_state are enum-valued
    and the core pipeline hard-fails on unknown values. We therefore inject
    safe defaults ('needs_iteration' / 'iterating') when the fenced block is
    missing these keys, rather than letting _normalize_role_payload emit an
    empty string that would fail validation.
    """
    if not isinstance(payload, dict):
        return payload
    if not _UTTERANCE_V1_KEYS.issubset(payload.keys()):
        return payload
    body = str(payload.get("body") or "")
    fenced = _extract_fenced_json(body) or {}
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    summary = str(
        fenced.get("summary") or payload.get("summary") or first_line or "Orchestrator judged the cycle."
    ).strip() or "Orchestrator judged the cycle."
    decision = str(fenced.get("decision") or "").strip() or "needs_iteration"
    next_state = str(fenced.get("next_state") or "").strip() or "iterating"
    reason = str(fenced.get("reason") or summary).strip() or summary
    recommended = str(fenced.get("recommended_next_action") or "").strip()
    return {
        "summary": summary,
        "decision": decision,
        "next_state": next_state,
        "reason": reason,
        "unresolved_items": _fenced_list(fenced.get("unresolved_items")),
        "recommended_next_action": recommended,
    }


_UTTERANCE_COERCERS = {
    "planner": lambda p: _coerce_planner_utterance_to_legacy(p),
    "builder": lambda p: _coerce_builder_utterance_to_legacy(p),
    "verifier_functional": lambda p: _coerce_verifier_functional_utterance_to_legacy(p),
    "verifier_human": lambda p: _coerce_verifier_human_utterance_to_legacy(p),
    "orchestrator": lambda p: _coerce_orchestrator_utterance_to_legacy(p),
}


def _coerce_builder_utterance_to_legacy(payload: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 transition mirror of the planner coercion, for the builder role.

    If the payload is in utterance.v1 shape, extract the legacy builder.result.v1
    fields from a fenced ```json``` block inside body. Missing fields are left to
    _normalize_role_payload downstream (it fills defaults). Pass-through for
    non-utterance payloads.
    """
    if not isinstance(payload, dict):
        return payload
    if not _UTTERANCE_V1_KEYS.issubset(payload.keys()):
        return payload
    body = str(payload.get("body") or "")
    fenced = _extract_fenced_json(body) or {}
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    summary_src = fenced.get("summary") or payload.get("summary") or first_line or "builder utterance"
    summary = str(summary_src).strip() or "builder utterance"
    change_summary_src = fenced.get("change_summary") or summary
    change_summary = str(change_summary_src).strip() or summary
    files_raw = fenced.get("files_changed")
    files_changed = [str(f) for f in files_raw] if isinstance(files_raw, list) else []
    artifacts_raw = fenced.get("artifact_paths")
    artifact_paths = [str(a) for a in artifacts_raw] if isinstance(artifacts_raw, list) else []
    self_check_raw = fenced.get("self_check")
    if isinstance(self_check_raw, dict):
        sc_summary = str(self_check_raw.get("summary") or "ok").strip() or "ok"
        sc_unresolved_raw = self_check_raw.get("unresolved")
        sc_unresolved = (
            [str(item) for item in sc_unresolved_raw]
            if isinstance(sc_unresolved_raw, list)
            else []
        )
        self_check = {"summary": sc_summary, "unresolved": sc_unresolved}
    else:
        self_check = {"summary": "ok", "unresolved": []}
    return {
        "summary": summary,
        "change_summary": change_summary,
        "files_changed": files_changed,
        "artifact_paths": artifact_paths,
        "self_check": self_check,
    }
