"""Static coherence checks for the 5 domain packs' rubric sections.

B 2단계 (e): `scoring` / `verify_functional` / `verify_human` / `guardrails`
섹션이 서로 일관적이고 스키마 규약을 지키는지 LLM 없이 정적 점검한다.

Errors (any -> rc=1):
    E1  meta.domain_id matches directory name
    E2  scoring.dimensions set equals keys(scoring.weights)
    E3  sum(scoring.weights.values()) in [0.99, 1.01]
    E4  scoring.thresholds.{functional_pass, human_pass} both in (0, 1]
    E5  required non-empty arrays present (blocking_failures /
        verify_functional.required_checks / verify_functional.pass_fail_rules /
        verify_human.approval_rules)
    E6  verify_functional.pass_fail_rules contains >=1 "hard fail" clause
    E7  limits.cycle_limits.max_cycles is a positive int
        (schema presence only — engine no longer enforces this value since
         P1-5-A; kept so domain packs declare a sane budget for humans.)
    E8  no duplicate entries (whitespace-normalized, case-insensitive) inside
        the structured list arrays listed in _DUP_CHECK_PATHS

Warnings (printed, do not flip rc):
    W1  blocking_failures and pass_fail_rules share zero tokens — one side
        may have been edited without syncing the other

Run:
    python -m tools.rubric_coherence_smoke
    python -m tools.rubric_coherence_smoke --only web,novel
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass


ENGINE_ROOT = Path(__file__).resolve().parent.parent
DOMAINS_ROOT = ENGINE_ROOT / "domains"

DOMAINS = ("web", "unity", "novel", "music_video", "investment_research")

_DUP_CHECK_PATHS: tuple[tuple[str, ...], ...] = (
    ("scoring", "blocking_failures"),
    ("verify_functional", "required_checks"),
    ("verify_functional", "pass_fail_rules"),
    ("verify_human", "approval_rules"),
    ("guardrails", "forbidden_actions"),
    ("builder", "execution_rules"),
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9\uAC00-\uD7A3]{2,}")


@dataclass(slots=True)
class DomainReport:
    domain: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _get(obj: dict, path: tuple[str, ...], default=None):
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _load_domain(domain: str) -> dict:
    path = DOMAINS_ROOT / domain / "domain.yaml"
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _check_meta(domain: str, data: dict, report: DomainReport) -> None:
    declared = _get(data, ("meta", "domain_id"))
    if declared != domain:
        report.errors.append(
            f"E1 meta.domain_id={declared!r} but folder name is {domain!r}"
        )


def _check_scoring(data: dict, report: DomainReport) -> None:
    dims = _get(data, ("scoring", "dimensions"))
    weights = _get(data, ("scoring", "weights"))
    thresholds = _get(data, ("scoring", "thresholds"))

    if not isinstance(dims, list) or not dims:
        report.errors.append("E5 scoring.dimensions missing or empty")
        dims = []
    if not isinstance(weights, dict) or not weights:
        report.errors.append("E5 scoring.weights missing or empty")
        weights = {}

    if dims and weights:
        dims_set = set(dims)
        weight_keys = set(weights.keys())
        if dims_set != weight_keys:
            missing = dims_set - weight_keys
            extra = weight_keys - dims_set
            bits = []
            if missing:
                bits.append(f"missing weight for {sorted(missing)}")
            if extra:
                bits.append(f"extra weight key {sorted(extra)}")
            report.errors.append("E2 " + "; ".join(bits))

    if weights:
        try:
            total = sum(float(v) for v in weights.values())
        except (TypeError, ValueError):
            report.errors.append("E3 scoring.weights has non-numeric values")
            total = 0.0
        else:
            if not (0.99 <= total <= 1.01):
                report.errors.append(
                    f"E3 sum(scoring.weights)={total:.4f} not in [0.99, 1.01]"
                )

    if not isinstance(thresholds, dict):
        report.errors.append("E4 scoring.thresholds missing")
    else:
        for key in ("functional_pass", "human_pass"):
            val = thresholds.get(key)
            if not isinstance(val, (int, float)) or not (0.0 < float(val) <= 1.0):
                report.errors.append(
                    f"E4 scoring.thresholds.{key}={val!r} not in (0, 1]"
                )


def _check_required_arrays(data: dict, report: DomainReport) -> None:
    required = (
        ("scoring", "blocking_failures"),
        ("verify_functional", "required_checks"),
        ("verify_functional", "pass_fail_rules"),
        ("verify_human", "approval_rules"),
    )
    for path in required:
        val = _get(data, path)
        if not isinstance(val, list) or not val:
            report.errors.append(f"E5 {'.'.join(path)} missing or empty")


def _check_hard_fail_clause(data: dict, report: DomainReport) -> None:
    rules = _get(data, ("verify_functional", "pass_fail_rules")) or []
    if isinstance(rules, list) and not any(
        "hard fail" in (r or "").lower() for r in rules if isinstance(r, str)
    ):
        report.errors.append(
            "E6 verify_functional.pass_fail_rules has no 'hard fail' clause"
        )


def _check_cycle_limits(data: dict, report: DomainReport) -> None:
    # Schema presence check only. Since P1-5-A (Phase 2) the engine no longer
    # reads this value — cycle continuation is decided by the orchestrator
    # LLM. We keep the field in domain packs so human authors declare a
    # roughly sane budget, and this smoke keeps it from silently drifting
    # to a non-positive value.
    max_cycles = _get(data, ("limits", "cycle_limits", "max_cycles"))
    if not isinstance(max_cycles, int) or max_cycles <= 0:
        report.errors.append(
            f"E7 limits.cycle_limits.max_cycles={max_cycles!r} not a positive int"
        )


def _check_duplicates(data: dict, report: DomainReport) -> None:
    for path in _DUP_CHECK_PATHS:
        items = _get(data, path)
        if not isinstance(items, list):
            continue
        seen: dict[str, int] = {}
        for idx, raw in enumerate(items):
            if not isinstance(raw, str):
                continue
            key = _normalize(raw)
            if not key:
                continue
            if key in seen:
                report.errors.append(
                    f"E8 duplicate in {'.'.join(path)}: item #{idx} "
                    f"duplicates #{seen[key]} ({raw!r})"
                )
            else:
                seen[key] = idx


def _check_blocking_vs_passfail_tokens(data: dict, report: DomainReport) -> None:
    blocking = _get(data, ("scoring", "blocking_failures")) or []
    pass_fail = _get(data, ("verify_functional", "pass_fail_rules")) or []
    if not isinstance(blocking, list) or not isinstance(pass_fail, list):
        return
    blocking_tokens: set[str] = set()
    for item in blocking:
        if isinstance(item, str):
            blocking_tokens |= _tokenize(item)
    pass_fail_tokens: set[str] = set()
    for item in pass_fail:
        if isinstance(item, str):
            pass_fail_tokens |= _tokenize(item)
    if blocking_tokens and pass_fail_tokens and not (blocking_tokens & pass_fail_tokens):
        report.warnings.append(
            "W1 scoring.blocking_failures and verify_functional.pass_fail_rules "
            "share zero tokens — sections may be out of sync"
        )


_CHECKS = (
    _check_meta,
    _check_scoring,
    _check_required_arrays,
    _check_hard_fail_clause,
    _check_cycle_limits,
    _check_duplicates,
    _check_blocking_vs_passfail_tokens,
)


def _run_domain(domain: str) -> DomainReport:
    report = DomainReport(domain=domain)
    try:
        data = _load_domain(domain)
    except FileNotFoundError:
        report.errors.append("domain.yaml not found")
        return report
    except json.JSONDecodeError as exc:
        report.errors.append(f"domain.yaml invalid JSON: {exc}")
        return report

    _check_meta(domain, data, report)
    for check in _CHECKS[1:]:
        check(data, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="rubric coherence static checks for domain packs"
    )
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated domain IDs (default: all five).",
    )
    args = parser.parse_args()

    if args.only:
        wanted = [t.strip() for t in args.only.split(",") if t.strip()]
        domains = [d for d in DOMAINS if d in wanted]
        missing = [d for d in wanted if d not in DOMAINS]
        if missing:
            print(
                f"[WARN] unknown domains ignored: {missing}",
                file=sys.stderr,
            )
    else:
        domains = list(DOMAINS)

    if not domains:
        print("no domains selected", file=sys.stderr)
        return 2

    reports = [_run_domain(d) for d in domains]

    print("\nRubric coherence smoke")
    print("----------------------")
    error_count = 0
    warn_count = 0
    for r in reports:
        status = "OK  " if r.ok else "FAIL"
        print(f"  {status}  {r.domain}")
        for err in r.errors:
            print(f"    [ERROR] {err}")
            error_count += 1
        for warn in r.warnings:
            print(f"    [warn]  {warn}")
            warn_count += 1
    total = sum(1 for r in reports if r.ok)
    print(
        f"{total}/{len(reports)} domains clean "
        f"({error_count} errors, {warn_count} warnings)."
    )
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
