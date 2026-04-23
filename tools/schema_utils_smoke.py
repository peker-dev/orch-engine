"""Schema utility smoke — prose-prefix tolerant JSON extraction.

These scenarios lock in the extractor behaviour that recovers an utterance.v1
envelope even when the LLM prepends conversational prose to the required JSON
block (a habit observed in 18차 Live 실측, 2026-04-23). The balanced-brace
scanner must also respect JSON string literals so a fenced ```json``` block
embedded inside `body` does not confuse envelope discovery.

Scenarios:

  1. prose_prefix_envelope — "Done!\\n\\n{speaker,body,next_speaker}" yields
     the envelope dict.
  2. fast_path_pure_json — strings that are already pure JSON still work.
  3. claude_cli_wrapper — the full `{"type":"result","result":"<prose>{...}"}`
     wrapper as produced by Claude CLI surfaces the envelope.
  4. body_has_fenced_json — when the envelope's own body contains a fenced
     ```json``` block, the balanced-brace scan picks the envelope (not the
     inner block) because it tracks string-literal escapes.
  5. first_dict_fallback — find_first_dict_candidate also tolerates prose
     prefix and returns the first balanced dict after the prose.
  6. no_json_returns_none — a purely conversational string yields None.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from adapters.base import _check_utterance_invariants  # noqa: E402
from tools.schema_utils import (  # noqa: E402
    find_first_dict_candidate,
    find_payload_candidate,
)

UTTERANCE_REQUIRED = {"speaker", "body", "next_speaker"}


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    message: str


def _scenario_prose_prefix_envelope() -> ScenarioResult:
    text = (
        "3개 파일 생성 완료했습니다. 수용 기준 마커가 일치합니다.\n\n"
        '{"speaker":"builder","body":"## summary","next_speaker":"verifier_functional"}'
    )
    found = find_payload_candidate(text, UTTERANCE_REQUIRED)
    if found is None:
        return ScenarioResult(
            "prose_prefix_envelope",
            False,
            "envelope not extracted from prose-prefixed string",
        )
    if found.get("speaker") != "builder" or found.get("next_speaker") != "verifier_functional":
        return ScenarioResult(
            "prose_prefix_envelope",
            False,
            f"wrong envelope extracted: {found}",
        )
    return ScenarioResult(
        "prose_prefix_envelope",
        True,
        "Korean prose prefix stripped; utterance.v1 envelope recovered",
    )


def _scenario_fast_path_pure_json() -> ScenarioResult:
    text = '{"speaker":"planner","body":"b","next_speaker":"builder"}'
    found = find_payload_candidate(text, UTTERANCE_REQUIRED)
    if found is None or found.get("speaker") != "planner":
        return ScenarioResult(
            "fast_path_pure_json",
            False,
            f"pure JSON fast path failed: got {found!r}",
        )
    return ScenarioResult(
        "fast_path_pure_json",
        True,
        "pure-JSON strings still parsed directly (no regression)",
    )


def _scenario_claude_cli_wrapper() -> ScenarioResult:
    wrapper = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": (
            "Completed successfully.\n\n"
            '{"speaker":"builder","body":"done","next_speaker":"verifier_functional"}'
        ),
    }
    found = find_payload_candidate(wrapper, UTTERANCE_REQUIRED)
    if found is None:
        return ScenarioResult(
            "claude_cli_wrapper",
            False,
            "envelope not lifted out of Claude CLI wrapper `result` string",
        )
    if found.get("next_speaker") != "verifier_functional":
        return ScenarioResult(
            "claude_cli_wrapper",
            False,
            f"wrong envelope extracted: {found}",
        )
    return ScenarioResult(
        "claude_cli_wrapper",
        True,
        "Claude CLI wrapper + prose prefix yields correct envelope",
    )


def _scenario_body_has_fenced_json() -> ScenarioResult:
    # The envelope's body contains a fenced ```json``` block with its own
    # {"change_summary":...}. The balanced-brace scanner must NOT stop at
    # the inner `{` because it sits inside a JSON string literal.
    body = (
        "## Summary\\n\\nDid the work.\\n\\n"
        "```json\\n"
        '{\\"change_summary\\":\\"did X\\",\\"files_changed\\":[\\"a.html\\"],'
        '\\"artifact_paths\\":[\\"a.html\\"],'
        '\\"self_check\\":{\\"summary\\":\\"ok\\",\\"unresolved\\":[]}}'
        "\\n```"
    )
    text = (
        "Note: earlier attempt missed the envelope shape, retrying.\n\n"
        f'{{"speaker":"builder","body":"{body}","next_speaker":"verifier_functional"}}'
    )
    found = find_payload_candidate(text, UTTERANCE_REQUIRED)
    if found is None:
        return ScenarioResult(
            "body_has_fenced_json",
            False,
            "scanner failed when envelope body embedded fenced JSON block",
        )
    if found.get("speaker") != "builder" or "change_summary" in found:
        return ScenarioResult(
            "body_has_fenced_json",
            False,
            f"scanner picked inner fenced block, not envelope: {list(found.keys())}",
        )
    return ScenarioResult(
        "body_has_fenced_json",
        True,
        "balanced-brace scan respects string literals; envelope wins over body-inner JSON",
    )


def _scenario_first_dict_fallback() -> ScenarioResult:
    text = 'Here is a result:\n\n{"foo":1,"bar":[2,3]}'
    found = find_first_dict_candidate(text)
    if found is None or found.get("foo") != 1:
        return ScenarioResult(
            "first_dict_fallback",
            False,
            f"first_dict fallback did not recover dict from prose: {found!r}",
        )
    return ScenarioResult(
        "first_dict_fallback",
        True,
        "find_first_dict_candidate also tolerant of prose prefix",
    )


def _scenario_arbitration_invariant() -> ScenarioResult:
    # Valid: orchestrator + arbitration=agree.
    try:
        _check_utterance_invariants({
            "speaker": "orchestrator",
            "body": "b",
            "next_speaker": "__end__",
            "arbitration": "agree",
        })
    except ValueError as exc:
        return ScenarioResult(
            "arbitration_invariant",
            False,
            f"orchestrator+agree wrongly rejected: {exc}",
        )

    # Valid: missing arbitration on non-orchestrator is fine.
    try:
        _check_utterance_invariants({
            "speaker": "planner",
            "body": "b",
            "next_speaker": "builder",
        })
    except ValueError as exc:
        return ScenarioResult(
            "arbitration_invariant",
            False,
            f"planner without arbitration wrongly rejected: {exc}",
        )

    # Invalid: non-orchestrator speaker with arbitration set must raise.
    try:
        _check_utterance_invariants({
            "speaker": "verifier_human",
            "body": "b",
            "next_speaker": "orchestrator",
            "arbitration": "agree",
        })
    except ValueError:
        return ScenarioResult(
            "arbitration_invariant",
            True,
            "engine blocks arbitration on non-orchestrator speaker (allOf replacement works)",
        )
    return ScenarioResult(
        "arbitration_invariant",
        False,
        "non-orchestrator arbitration passed silently — invariant not enforced",
    )


def _scenario_no_json_returns_none() -> ScenarioResult:
    text = "This is just prose, no JSON here at all."
    if find_payload_candidate(text, UTTERANCE_REQUIRED) is not None:
        return ScenarioResult(
            "no_json_returns_none",
            False,
            "find_payload_candidate returned a dict for prose-only input",
        )
    if find_first_dict_candidate(text) is not None:
        return ScenarioResult(
            "no_json_returns_none",
            False,
            "find_first_dict_candidate returned a dict for prose-only input",
        )
    return ScenarioResult(
        "no_json_returns_none",
        True,
        "prose-only strings yield None (no false positives)",
    )


SCENARIOS: dict[str, Callable[[], ScenarioResult]] = {
    "prose_prefix_envelope": _scenario_prose_prefix_envelope,
    "fast_path_pure_json": _scenario_fast_path_pure_json,
    "claude_cli_wrapper": _scenario_claude_cli_wrapper,
    "body_has_fenced_json": _scenario_body_has_fenced_json,
    "first_dict_fallback": _scenario_first_dict_fallback,
    "arbitration_invariant": _scenario_arbitration_invariant,
    "no_json_returns_none": _scenario_no_json_returns_none,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="schema_utils prose-prefix smoke")
    parser.add_argument("--only", default="", help="Comma-separated scenario names")
    args = parser.parse_args()

    wanted = [n.strip() for n in args.only.split(",") if n.strip()] or list(SCENARIOS)
    unknown = [n for n in wanted if n not in SCENARIOS]
    if unknown:
        print(f"Unknown scenarios: {unknown}")
        return 2

    results: list[ScenarioResult] = []
    for name in wanted:
        print(f"\n=== {name} ===")
        try:
            result = SCENARIOS[name]()
        except Exception as exc:  # noqa: BLE001
            result = ScenarioResult(name, False, f"scenario raised: {exc!r}")
        results.append(result)
        status = "OK" if result.ok else "FAIL"
        print(f"[{status}] {name}: {result.message}")

    print("\nSummary\n-------")
    for r in results:
        status = "OK  " if r.ok else "FAIL"
        print(f"  {status}  {r.name}: {r.message}")
    passed = sum(1 for r in results if r.ok)
    total = len(results)
    print(f"{passed}/{total} scenarios passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
