"""Static coherence checks for the 5 domain packs.

Runs in **dual mode** (D13 / 19차 Phase 2 P0-E):

* Legacy domain pack (no `guides/` dir): the original yaml-body coherence
  checks (E1~E8 + W1) still run. This keeps the four un-migrated domain
  packs (investment_research / music_video / novel / unity) green while
  D13 rolls out one domain at a time.
* Migrated domain pack (has `guides/` dir): yaml-body checks are skipped;
  guide md presence + minimum substance checks (G1~G4) run instead. The
  domain's `domain.yaml` should contain only `meta`.

Errors (any -> rc=1):

  Legacy mode (no guides/):
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

  Migrated mode (has guides/):
    G1  meta.domain_id matches directory name
    G2  all 5 role guide files present (planner / builder / verifier_functional /
        verifier_human / orchestrator)
    G3  each guide is at least _MIN_GUIDE_LINES (40) non-empty lines —
        blocks empty stubs from sneaking in
    G4  each guide has at least one '# ' H1 and one '## ' H2 header —
        the agreed style is "header + paragraph + bullet mixed", not raw prose

Warnings (printed, do not flip rc):

  Legacy mode:
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

_REQUIRED_GUIDE_ROLES = (
    "planner",
    "builder",
    "verifier_functional",
    "verifier_human",
    "orchestrator",
)
_MIN_GUIDE_LINES = 40

_DUP_CHECK_PATHS: tuple[tuple[str, ...], ...] = (
    ("scoring", "blocking_failures"),
    ("verify_functional", "required_checks"),
    ("verify_functional", "pass_fail_rules"),
    ("verify_human", "approval_rules"),
    ("guardrails", "forbidden_actions"),
    ("builder", "execution_rules"),
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣]{2,}")


@dataclass(slots=True)
class DomainReport:
    domain: str
    mode: str = "legacy"  # "legacy" or "migrated"
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


def _has_guides_dir(domain: str) -> bool:
    return (DOMAINS_ROOT / domain / "guides").is_dir()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _check_meta(domain: str, data: dict, report: DomainReport) -> None:
    declared = _get(data, ("meta", "domain_id"))
    if declared != domain:
        code = "G1" if report.mode == "migrated" else "E1"
        report.errors.append(
            f"{code} meta.domain_id={declared!r} but folder name is {domain!r}"
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


_LEGACY_CHECKS = (
    _check_scoring,
    _check_required_arrays,
    _check_hard_fail_clause,
    _check_cycle_limits,
    _check_duplicates,
    _check_blocking_vs_passfail_tokens,
)


def _check_guide_files(domain: str, report: DomainReport) -> None:
    """Migrated-mode checks G2~G4: guide presence, minimum lines, headers."""
    guides_root = DOMAINS_ROOT / domain / "guides"
    for role in _REQUIRED_GUIDE_ROLES:
        guide_path = guides_root / f"{role}.md"
        if not guide_path.exists():
            report.errors.append(f"G2 missing guide: guides/{role}.md")
            continue
        try:
            text = guide_path.read_text(encoding="utf-8")
        except OSError as exc:
            report.errors.append(f"G2 cannot read guides/{role}.md: {exc}")
            continue
        non_empty_lines = [line for line in text.splitlines() if line.strip()]
        if len(non_empty_lines) < _MIN_GUIDE_LINES:
            report.errors.append(
                f"G3 guides/{role}.md has {len(non_empty_lines)} non-empty lines, "
                f"minimum is {_MIN_GUIDE_LINES}"
            )
        has_h1 = any(line.startswith("# ") for line in non_empty_lines)
        has_h2 = any(line.startswith("## ") for line in non_empty_lines)
        if not has_h1 or not has_h2:
            missing = []
            if not has_h1:
                missing.append("'# ' H1")
            if not has_h2:
                missing.append("'## ' H2")
            report.errors.append(
                f"G4 guides/{role}.md missing required headers: {', '.join(missing)}"
            )


def _run_domain(domain: str) -> DomainReport:
    migrated = _has_guides_dir(domain)
    report = DomainReport(domain=domain, mode="migrated" if migrated else "legacy")
    try:
        data = _load_domain(domain)
    except FileNotFoundError:
        report.errors.append("domain.yaml not found")
        return report
    except json.JSONDecodeError as exc:
        report.errors.append(f"domain.yaml invalid JSON: {exc}")
        return report

    _check_meta(domain, data, report)
    if migrated:
        _check_guide_files(domain, report)
    else:
        for check in _LEGACY_CHECKS:
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
        print(f"  {status}  {r.domain}  [{r.mode}]")
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
