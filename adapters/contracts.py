"""역할별 prompt / 응답 JSON Schema (live CLI 어댑터 공용).

claude / codex CLI 가 schema-constrained 출력을 강제하는 데 사용된다.
"""
from __future__ import annotations

import json
from typing import Any


def response_schema(role: str) -> dict[str, Any]:
    if role == "planner":
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["title", "description"],
            "additionalProperties": False,
        }
    if role == "builder":
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "artifacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                        "required": ["name", "summary"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "artifacts"],
            "additionalProperties": False,
        }
    if role == "verifier":
        return {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["pass", "needs_iteration", "blocked"]},
                "summary": {"type": "string"},
                "issues": {"type": "array", "items": {"type": "string"}},
                "improvements": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["verdict", "summary", "issues", "improvements"],
            "additionalProperties": False,
        }
    if role == "orchestrator":
        return {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["complete", "needs_iteration", "blocked"]},
                "reason": {"type": "string"},
            },
            "required": ["decision", "reason"],
            "additionalProperties": False,
        }
    raise KeyError(f"unknown role: {role!r}")


_ROLE_BRIEF = {
    "planner": (
        "You are the Planner of an automated multi-agent improvement loop. "
        "Read the goal and (if present) the previous Verifier review, then write "
        "the next concrete sub-task the Builder should attempt. "
        "If a previous_review exists, the description MUST address its improvements."
    ),
    "builder": (
        "You are the Builder. Carry out the current_task and report what you produced. "
        "Do not invent files you cannot describe. summary and artifacts must reflect real work."
    ),
    "verifier": (
        "You are the Verifier. Compare current_build against current_task and the goal. "
        "Set verdict to 'pass' only if the work meets the goal; otherwise 'needs_iteration' "
        "with specific improvements. Use 'blocked' only if progress is impossible."
    ),
    "orchestrator": (
        "You are the Orchestrator. Read the verifier review and decide one of: "
        "'complete' (verifier passed), 'needs_iteration' (verifier asked for changes), "
        "'blocked' (cannot continue). Keep reason short."
    ),
}


def system_prompt(role: str) -> str:
    brief = _ROLE_BRIEF[role]
    return (
        f"{brief}\n\n"
        "Respond with a SINGLE JSON object that matches the provided schema. "
        "No prose before or after the JSON. No code fences."
    )


def user_prompt(role: str, context: dict[str, Any]) -> str:
    schema = response_schema(role)
    payload = {
        "role": role,
        "context": context,
        "expected_schema": schema,
    }
    return (
        "Inputs (JSON):\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\nReturn JSON only."
    )
