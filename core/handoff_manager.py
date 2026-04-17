"""Codex App handoff lifecycle implementation.

Implements the file-based handoff contract described in
`codex-app-handoff-protocol.md`. A handoff is a way for the engine to
pause its automatic write loop and let a human-guided tool (Codex App or
any other editor) take a focused pass. The engine owns lifecycle state —
the external tool only touches declared files and returns a structured
response document.

State progression:
    idle
    -> handoff_active         (engine wrote request.yaml)
    -> handoff_returned       (external tool filled response.yaml)
    -> resume_ready           (engine read and validated the response)
    -> idle (archived)

The `.orch/` layout this module owns:
    .orch/handoff/request.yaml       current request payload
    .orch/handoff/response.yaml      current response (written by Codex App)
    .orch/handoff/status.json        lightweight lifecycle pointer
    .orch/handoff/history/<id>/      archived request + response after close
    .orch/runtime/handoff.json       mirror of status for other components
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from core.runtime_store import RuntimeStore


SUPPORTED_MODES = {
    "review_only",
    "approve_gate",
    "repair_pass",
    "replan_pass",
    "takeover_session",
}

VALID_RESULTS = {"approved", "changes_made", "replan_needed", "blocked", "rejected"}

REQUEST_REQUIRED_FIELDS = (
    "handoff_id",
    "created_at",
    "project_id",
    "mode",
    "reason",
    "goal",
    "what_needs_decision",
    "allowed_edit_scope",
    "blocked_by",
    "recommended_read_order",
    "expected_return_format",
)

RESPONSE_REQUIRED_FIELDS = (
    "handoff_id",
    "completed_at",
    "result",
    "summary",
    "decision",
    "findings",
    "files_changed",
    "artifacts_added",
    "recommended_next_action",
    "resume_condition",
    "remaining_risks",
)


class HandoffError(RuntimeError):
    """Raised when a handoff operation violates the protocol."""


@dataclass(slots=True)
class HandoffRequest:
    project_id: str
    mode: str
    reason: str
    goal: str
    what_needs_decision: str
    allowed_edit_scope: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    recommended_read_order: list[str] = field(default_factory=list)
    expected_return_format: str = "yaml"
    active_tasks: list[dict[str, Any]] = field(default_factory=list)
    latest_plan_summary: str = ""
    latest_functional_review_summary: str = ""
    latest_human_review_summary: str = ""
    artifact_index: list[dict[str, str]] = field(default_factory=list)
    constraints_and_guardrails: list[str] = field(default_factory=list)
    resume_expectation: str = ""


@dataclass(slots=True)
class HandoffStatus:
    active: bool
    state: str  # one of: idle, handoff_active, handoff_returned, resume_ready
    handoff_id: str | None
    mode: str | None
    created_at: str | None
    returned_at: str | None


class HandoffManager:
    """Owns the `.orch/handoff/` lifecycle for a single target project."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)
        self.handoff_root = self.project_root / ".orch" / "handoff"
        self.history_root = self.handoff_root / "history"
        self.status_path = self.handoff_root / "status.json"
        self.request_path = self.handoff_root / "request.yaml"
        self.response_path = self.handoff_root / "response.yaml"
        self.runtime = RuntimeStore(self.project_root)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def status(self) -> HandoffStatus:
        raw = self.runtime.read_json("handoff/status.json", {})
        if not isinstance(raw, dict):
            raw = {}
        return HandoffStatus(
            active=bool(raw.get("active", False)),
            state=str(raw.get("state", "idle")),
            handoff_id=raw.get("handoff_id"),
            mode=raw.get("mode"),
            created_at=raw.get("created_at"),
            returned_at=raw.get("returned_at"),
        )

    def create_request(self, request: HandoffRequest) -> HandoffStatus:
        """Write a handoff request and mark the engine as `handoff_active`."""
        if request.mode not in SUPPORTED_MODES:
            raise HandoffError(
                f"Unsupported handoff mode: {request.mode!r}. "
                f"Must be one of: {sorted(SUPPORTED_MODES)}"
            )
        existing = self.status()
        if existing.active:
            raise HandoffError(
                f"Another handoff is already active (id={existing.handoff_id}). "
                "Ingest or cancel it before opening a new one."
            )

        handoff_id = f"hof-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        created_at = _now_iso()

        payload: dict[str, Any] = {
            "handoff_id": handoff_id,
            "created_at": created_at,
            "project_id": request.project_id,
            "mode": request.mode,
            "reason": request.reason,
            "goal": request.goal,
            "what_needs_decision": request.what_needs_decision,
            "allowed_edit_scope": list(request.allowed_edit_scope),
            "blocked_by": list(request.blocked_by),
            "recommended_read_order": list(request.recommended_read_order),
            "expected_return_format": request.expected_return_format or "yaml",
            "active_tasks": list(request.active_tasks),
            "latest_plan_summary": request.latest_plan_summary,
            "latest_functional_review_summary": request.latest_functional_review_summary,
            "latest_human_review_summary": request.latest_human_review_summary,
            "artifact_index": list(request.artifact_index),
            "constraints_and_guardrails": list(request.constraints_and_guardrails),
            "resume_expectation": request.resume_expectation,
        }

        self.handoff_root.mkdir(parents=True, exist_ok=True)
        self.request_path.write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
        # Reset any leftover response from a previous run so the external tool
        # starts from the template rather than stale content.
        self._reset_response_template(handoff_id)

        new_status = {
            "active": True,
            "state": "handoff_active",
            "handoff_id": handoff_id,
            "mode": request.mode,
            "created_at": created_at,
            "returned_at": None,
        }
        self._persist_status(new_status)
        self.runtime.append_event(
            "handoff_requested",
            {"handoff_id": handoff_id, "mode": request.mode, "reason": request.reason},
        )
        return self.status()

    def mark_returned(self) -> HandoffStatus:
        """Verify that the response file exists and transition to `handoff_returned`."""
        current = self.status()
        if not current.active or current.state != "handoff_active":
            raise HandoffError(
                f"Cannot mark returned from state={current.state!r} "
                f"(active={current.active})."
            )
        if not self.response_path.exists():
            raise HandoffError(
                f"Expected response file not found: {self.response_path}"
            )
        response = self._load_response()
        response_id = response.get("handoff_id")
        if response_id and response_id != current.handoff_id:
            raise HandoffError(
                f"Response handoff_id {response_id!r} does not match active "
                f"request {current.handoff_id!r}."
            )
        returned_at = _now_iso()
        self._persist_status(
            {
                "active": True,
                "state": "handoff_returned",
                "handoff_id": current.handoff_id,
                "mode": current.mode,
                "created_at": current.created_at,
                "returned_at": returned_at,
            }
        )
        self.runtime.append_event(
            "handoff_returned",
            {"handoff_id": current.handoff_id, "returned_at": returned_at},
        )
        return self.status()

    def ingest(self) -> dict[str, Any]:
        """Validate and consume the response, advance to `resume_ready`, archive files.

        The validation must happen *before* any state transition so a malformed
        response leaves the handoff in `handoff_active` and the caller can fix
        `response.yaml` and retry. Advancing state first would strand the
        handoff in `handoff_returned` with stale files still on disk.
        """
        current = self.status()
        if current.state not in {"handoff_active", "handoff_returned"}:
            raise HandoffError(
                f"Cannot ingest handoff from state={current.state!r} "
                f"(expected handoff_active or handoff_returned)."
            )
        if not self.response_path.exists():
            raise HandoffError(
                f"Expected response file not found: {self.response_path}"
            )
        response = self._load_response()
        _validate_response(response, expected_id=current.handoff_id)

        if current.state == "handoff_active":
            # Only after validation do we promote the status. This keeps the
            # on-disk state consistent if the validation above had raised.
            current = self.mark_returned()

        archived = self._archive_current(current.handoff_id)
        self._persist_status(
            {
                "active": False,
                "state": "resume_ready",
                "handoff_id": current.handoff_id,
                "mode": current.mode,
                "created_at": current.created_at,
                "returned_at": current.returned_at,
                "last_result": response.get("result"),
                "archived_path": str(archived.relative_to(self.project_root)),
            }
        )
        self.runtime.append_event(
            "handoff_ingested",
            {
                "handoff_id": current.handoff_id,
                "result": response.get("result"),
                "archived_path": str(archived.relative_to(self.project_root)),
            },
        )
        return response

    def cancel(self, reason: str = "") -> HandoffStatus:
        """Abort an active handoff without ingesting a response."""
        current = self.status()
        if not current.active:
            return current
        archived = self._archive_current(current.handoff_id or "cancelled", cancelled=True)
        self._persist_status(
            {
                "active": False,
                "state": "idle",
                "handoff_id": None,
                "mode": None,
                "created_at": None,
                "returned_at": None,
                "last_cancelled_id": current.handoff_id,
                "archived_path": str(archived.relative_to(self.project_root)),
            }
        )
        self.runtime.append_event(
            "handoff_cancelled",
            {"handoff_id": current.handoff_id, "reason": reason, "archived_path": str(archived.relative_to(self.project_root))},
        )
        return self.status()

    def acknowledge_resume(self) -> HandoffStatus:
        """Move the manager back to `idle` once the engine has consumed the result."""
        current = self.status()
        if current.state not in {"resume_ready", "idle"}:
            raise HandoffError(
                f"Cannot acknowledge resume from state={current.state!r}."
            )
        self._persist_status(
            {
                "active": False,
                "state": "idle",
                "handoff_id": None,
                "mode": None,
                "created_at": None,
                "returned_at": None,
            }
        )
        return self.status()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _persist_status(self, payload: dict[str, Any]) -> None:
        self.runtime.write_json("handoff/status.json", payload)
        # Legacy mirror so UI components that still watch `runtime/handoff.json`
        # see the same state.
        self.runtime.write_json(
            "runtime/handoff.json",
            {
                "active": bool(payload.get("active", False)),
                "mode": payload.get("mode"),
                "handoff_id": payload.get("handoff_id"),
                "state": payload.get("state"),
            },
        )

    def _reset_response_template(self, handoff_id: str) -> None:
        template = {
            "handoff_id": handoff_id,
            "completed_at": None,
            "result": None,
            "summary": None,
            "decision": None,
            "findings": [],
            "files_changed": [],
            "artifacts_added": [],
            "recommended_next_action": None,
            "resume_condition": None,
            "remaining_risks": [],
        }
        self.response_path.write_text(
            yaml.safe_dump(template, sort_keys=False), encoding="utf-8"
        )

    def _load_response(self) -> dict[str, Any]:
        raw = yaml.safe_load(self.response_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise HandoffError("response.yaml must deserialize to a mapping.")
        return raw

    def _archive_current(self, handoff_id: str, *, cancelled: bool = False) -> Path:
        suffix = "-cancelled" if cancelled else ""
        dest = self.history_root / f"{handoff_id}{suffix}"
        dest.mkdir(parents=True, exist_ok=True)
        if self.request_path.exists():
            shutil.copy2(self.request_path, dest / "request.yaml")
            self.request_path.unlink()
        if self.response_path.exists():
            shutil.copy2(self.response_path, dest / "response.yaml")
            self.response_path.unlink()
        return dest


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_response(response: dict[str, Any], *, expected_id: str | None) -> None:
    missing = [key for key in RESPONSE_REQUIRED_FIELDS if key not in response]
    if missing:
        raise HandoffError(f"response.yaml missing required keys: {missing}")
    if expected_id is not None and response["handoff_id"] != expected_id:
        raise HandoffError(
            f"response.handoff_id {response['handoff_id']!r} "
            f"does not match the active handoff id {expected_id!r}."
        )
    result = response.get("result")
    if result not in VALID_RESULTS:
        raise HandoffError(
            f"response.result must be one of {sorted(VALID_RESULTS)}, got {result!r}."
        )
    for list_field in (
        "findings",
        "files_changed",
        "artifacts_added",
        "remaining_risks",
    ):
        if not isinstance(response.get(list_field), list):
            raise HandoffError(f"response.{list_field} must be a list.")


__all__ = [
    "HandoffError",
    "HandoffManager",
    "HandoffRequest",
    "HandoffStatus",
    "REQUEST_REQUIRED_FIELDS",
    "RESPONSE_REQUIRED_FIELDS",
    "SUPPORTED_MODES",
    "VALID_RESULTS",
]
