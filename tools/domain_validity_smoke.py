"""Smoke tests for domain-pack evaluation validity (golden + counter-examples).

9차 세션 후속 과제(B): 도메인 팩의 `verify_functional` hard-fail 규칙이
pass/fail 판별을 실제로 해내는지를 결정적으로 증명한다. LLM 호출 없이
정적 검사만 수행하므로 회귀 스위트에 바로 편입 가능하다.

## 지원 도메인
- `web`: viewport / lang / img alt / h1 count / HTML parse / responsive evidence
- `music_video`: cross_stage_file_placement / persona_coverage_missing / confirmed_setting_drift
- `investment_research`: missing_report_section / emotional_qualifier / antithesis_insufficient / sources_insufficient
- `unity`: project_version_missing / webgl_incompat_api / missing_script_reference
- `novel`: banned_emotion_adjective / paragraph_too_long / abstract_phrasing

## 샘플 종류

### golden (`validation/golden/<sample_id>/`)
의도된 pass/fail 판별이 기계적으로 재현되는지 증명.

    expected.json: { domain, label: pass|fail, expected_violations: [...] }

- pass: violation 0개여야 함.
- fail: expected_violations 전부 검출 (추가 violation 허용).

### counter_examples (`validation/counter_examples/<sample_id>/`, optional)
실 사용 중 오탐/미탐으로 확인된 known-gap 케이스. 체커 튜닝 전 상태를
박제해 regression 감지용으로 둔다. 빈 폴더면 스모크 skip.

    expected.json: {
        domain, label: false_positive|false_negative,
        expected_violations: [...],   # ground truth (사람이 옳다고 본 결과)
        observed_violations: [...],   # 수집 시점 체커가 낸 결과
        note, discovered_at, source
    }

- label=false_positive (체커가 오탐지): ground truth=clean. reproduced면 녹색
  OK (known gap 유지), checker가 이제 clean이면 RESOLVED 출력 (golden 승격 권장).
- label=false_negative (체커가 미탐지): ground truth=violation 있음.
  checker가 여전히 놓치면 reproduced OK, 이제 잡으면 RESOLVED 출력.

counter_example은 어느 결과든 rc에 영향 없음. resolved는 박제관이 수동으로
golden 으로 옮기도록 안내하는 신호.

Run:
    python -m tools.domain_validity_smoke
    python -m tools.domain_validity_smoke --only web:pass_01
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass


ENGINE_ROOT = Path(__file__).resolve().parent.parent
DOMAINS_ROOT = ENGINE_ROOT / "domains"

# When set by --with-w3c, web checker additionally consults the W3C Nu HTML
# Checker online API. This triggers an outbound HTTPS call per HTML file.
_WITH_W3C = False


# ---------------------------------------------------------------------------
# web domain static checker
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _WebDoc:
    has_html_lang: bool = False
    has_viewport: bool = False
    h1_count: int = 0
    imgs_missing_alt: int = 0
    parse_error: str | None = None
    inline_css: list[str] = field(default_factory=list)


class _WebHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = _WebDoc()
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            if "lang" in attr_map and attr_map["lang"].strip():
                self.doc.has_html_lang = True
        elif tag == "meta":
            if attr_map.get("name", "").strip().lower() == "viewport" and attr_map.get("content"):
                self.doc.has_viewport = True
        elif tag == "h1":
            self.doc.h1_count += 1
        elif tag == "img":
            if "alt" not in attr_map:
                self.doc.imgs_missing_alt += 1
        elif tag == "style":
            self._in_style = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style and data.strip():
            self.doc.inline_css.append(data)


_RESPONSIVE_HINTS = (
    "@media",
    "max-width",
    "min-width",
)


def _has_responsive_evidence(html_text: str, inline_css: list[str]) -> bool:
    haystack = html_text + "\n" + "\n".join(inline_css)
    if any(hint in haystack for hint in _RESPONSIVE_HINTS):
        return True
    # fractional/relative units inside style blocks count as responsive evidence.
    if re.search(r":\s*\d+(?:\.\d+)?\s*(?:%|rem|em|vw|vh)\b", haystack):
        return True
    return False


def _check_web_html(html_text: str) -> list[str]:
    """Run the web domain's hard-fail rubric against one HTML document."""
    parser = _WebHTMLParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return ["html_parse_error"]

    doc = parser.doc
    violations: list[str] = []
    if not doc.has_viewport:
        violations.append("viewport_missing")
    if not doc.has_html_lang:
        violations.append("lang_missing")
    if doc.imgs_missing_alt > 0:
        violations.append("img_alt_missing")
    if doc.h1_count != 1:
        violations.append("h1_count_invalid")
    if not _has_responsive_evidence(html_text, doc.inline_css):
        violations.append("responsive_evidence_missing")
    return violations


def _check_web(sample_dir: Path) -> list[str]:
    """Aggregate web violations across all HTML files in a sample."""
    html_files = sorted(sample_dir.rglob("*.html"))
    if not html_files:
        return ["no_html_files"]
    seen: set[str] = set()
    for path in html_files:
        text = path.read_text(encoding="utf-8")
        seen.update(_check_web_html(text))
        if _WITH_W3C:
            seen.update(_check_web_w3c(text))
    return sorted(seen)


def _check_web_w3c(html_text: str) -> list[str]:
    """Call the W3C Nu HTML Checker; report an aggregated error violation.

    Network failures are swallowed and reported as `html_w3c_unreachable`
    rather than `html_w3c_error` so false positives do not occur when the
    host is offline.
    """
    try:
        from tools.w3c_validator_adapter import validate_html  # local import
    except ImportError:
        return ["html_w3c_unreachable"]
    try:
        findings = validate_html(html_text, online=True, timeout=15.0)
    except Exception:  # noqa: BLE001 — any network / parse problem becomes unreachable.
        return ["html_w3c_unreachable"]
    if any(f.get("severity") == "error" for f in findings):
        return ["html_w3c_error"]
    return []


# ---------------------------------------------------------------------------
# music_video domain static checker
# ---------------------------------------------------------------------------


_MV_STAGE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("suno_prompt_", "04_작곡"),
    ("melody_structure_", "04_작곡"),
    ("_lyrics_", "03_작사"),
    ("mv_storyboard_", "05_뮤직비디오"),
    ("final_notes", "06_최종"),
    ("release_plan", "06_최종"),
)

_MV_PERSONAS = ("서정아", "한비트", "윤프로", "채원", "민수")

_GENRE_LINE = re.compile(r"(?:장르|genre)\s*[:：]\s*([^\s(（]+)", re.IGNORECASE)


def _check_music_video(sample_dir: Path) -> list[str]:
    violations: set[str] = set()

    md_files = sorted(sample_dir.rglob("*.md"))

    # Rule 1: cross-stage file placement
    for path in md_files:
        rel_parts = path.relative_to(sample_dir).parts
        filename = path.name
        for pattern, expected_stage in _MV_STAGE_PATTERNS:
            if pattern in filename and expected_stage not in rel_parts:
                violations.add("cross_stage_file_placement")
                break

    # Rule 2: persona coverage on meeting logs
    for path in md_files:
        lower = path.name.lower()
        if lower.startswith("meeting") or "회의" in path.name:
            text = path.read_text(encoding="utf-8")
            if not all(persona in text for persona in _MV_PERSONAS):
                violations.add("persona_coverage_missing")

    # Rule 3: confirmed setting drift (genre)
    overview = sample_dir / "memory" / "project-overview.md"
    if overview.exists():
        overview_text = overview.read_text(encoding="utf-8")
        m = _GENRE_LINE.search(overview_text)
        if m:
            declared = m.group(1).strip()
            for path in md_files:
                if path == overview:
                    continue
                text = path.read_text(encoding="utf-8")
                for hit in _GENRE_LINE.finditer(text):
                    found = hit.group(1).strip()
                    if found and found != declared:
                        violations.add("confirmed_setting_drift")
                        break

    return sorted(violations)


# ---------------------------------------------------------------------------
# investment_research domain static checker
# ---------------------------------------------------------------------------


_IR_REQUIRED_SECTIONS = (
    "포착된 신호",
    "인과관계",
    "종목 분석",
    "투자 분류",
    "리스크 & 반례",
    "최종 판단",
)

_IR_EMOTIONAL_BLOCKLIST = (
    "유망",
    "훌륭",
    "완벽",
    "great",
    "promising",
    "amazing",
)

_IR_URL_RE = re.compile(r"https?://\S+")
_IR_REPORT_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_.+\.md$")
_IR_ANTITHESIS_SECTION_RE = re.compile(
    r"(?:^|\n)#{1,6}\s*리스크\s*&\s*반례[^\n]*\n(.*?)(?=\n#{1,6}\s|\Z)",
    re.DOTALL,
)
_IR_BULLET_RE = re.compile(r"^\s*[-*]\s+", re.MULTILINE)


def _check_investment_research(sample_dir: Path) -> list[str]:
    violations: set[str] = set()

    reports = [
        p for p in sample_dir.rglob("*.md")
        if _IR_REPORT_NAME_RE.match(p.name)
    ]
    if not reports:
        return ["no_report_file"]

    for report in reports:
        text = report.read_text(encoding="utf-8")

        # Rule 1: all 6 required section headers present
        for section in _IR_REQUIRED_SECTIONS:
            if section not in text:
                violations.add("missing_report_section")
                break

        # Rule 2: emotional qualifier blocklist
        for word in _IR_EMOTIONAL_BLOCKLIST:
            if word in text:
                violations.add("emotional_qualifier")
                break

        # Rule 3: antithesis section must have >=3 bullet items
        m = _IR_ANTITHESIS_SECTION_RE.search(text)
        if m:
            bullets = _IR_BULLET_RE.findall(m.group(1))
            if len(bullets) < 3:
                violations.add("antithesis_insufficient")

        # Rule 4: report must cite >=2 URLs
        if len(_IR_URL_RE.findall(text)) < 2:
            violations.add("sources_insufficient")

    return sorted(violations)


# ---------------------------------------------------------------------------
# unity domain static checker
# ---------------------------------------------------------------------------


_UNITY_WEBGL_BANNED = (
    "File.WriteAllText",
    "File.WriteAllBytes",
    "File.AppendAllText",
    "new Thread(",
    "Task.Run(",
)

_UNITY_MISSING_SCRIPT_RE = re.compile(r"m_Script:\s*\{fileID:\s*0\b")
_UNITY_ASSET_SUFFIXES = (".prefab", ".unity", ".asset")


def _check_unity(sample_dir: Path) -> list[str]:
    violations: set[str] = set()

    # Rule 1: ProjectSettings/ProjectVersion.txt must exist
    if not (sample_dir / "ProjectSettings" / "ProjectVersion.txt").exists():
        violations.add("project_version_missing")

    # Rule 2: WebGL-incompatible API usage in .cs files
    for cs in sample_dir.rglob("*.cs"):
        text = cs.read_text(encoding="utf-8", errors="replace")
        if any(banned in text for banned in _UNITY_WEBGL_BANNED):
            violations.add("webgl_incompat_api")
            break

    # Rule 3: Missing Script reference sentinel in Unity asset YAML
    for path in sample_dir.rglob("*"):
        if path.suffix not in _UNITY_ASSET_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _UNITY_MISSING_SCRIPT_RE.search(text):
            violations.add("missing_script_reference")
            break

    return sorted(violations)


# ---------------------------------------------------------------------------
# Sample runner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# novel domain static checker
# ---------------------------------------------------------------------------


_NOVEL_BANNED_EMOTION = (
    "비참하다",
    "비참했다",
    "슬프다",
    "슬펐다",
    "놀랍다",
    "놀라웠다",
    "기뻤다",
)

_NOVEL_BANNED_ABSTRACT = (
    "눈을 읽지 않았다",
    "생각하는 것 같은 눈",
)

_NOVEL_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def _check_novel(sample_dir: Path) -> list[str]:
    violations: set[str] = set()
    for path in sample_dir.rglob("*.md"):
        rel_parts = path.relative_to(sample_dir).parts
        if "원고" not in rel_parts:
            continue
        text = path.read_text(encoding="utf-8")

        # Rule 1: banned emotion adjectives
        if any(word in text for word in _NOVEL_BANNED_EMOTION):
            violations.add("banned_emotion_adjective")

        # Rule 2: paragraph exceeds 3 non-blank non-header lines
        for paragraph in _NOVEL_PARAGRAPH_SPLIT.split(text):
            non_blank = [
                ln for ln in paragraph.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")
            ]
            if len(non_blank) > 3:
                violations.add("paragraph_too_long")
                break

        # Rule 3: abstract phrasings
        if any(phrase in text for phrase in _NOVEL_BANNED_ABSTRACT):
            violations.add("abstract_phrasing")

    return sorted(violations)


# ---------------------------------------------------------------------------
# Sample runner
# ---------------------------------------------------------------------------


_DOMAIN_CHECKERS = {
    "web": _check_web,
    "music_video": _check_music_video,
    "investment_research": _check_investment_research,
    "unity": _check_unity,
    "novel": _check_novel,
}


@dataclass(slots=True)
class SampleResult:
    domain: str
    sample_id: str
    ok: bool
    message: str
    kind: str = "golden"  # golden | counter_fp | counter_fn


def _discover_samples(domain: str) -> list[Path]:
    root = DOMAINS_ROOT / domain / "validation" / "golden"
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _discover_counter_examples(domain: str) -> list[Path]:
    root = DOMAINS_ROOT / domain / "validation" / "counter_examples"
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _run_sample(domain: str, sample_dir: Path) -> SampleResult:
    sample_id = sample_dir.name
    expected_path = sample_dir / "expected.json"
    if not expected_path.exists():
        return SampleResult(domain, sample_id, False, "missing expected.json")
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    label = expected.get("label")
    expected_violations = set(expected.get("expected_violations", []))
    if label not in ("pass", "fail"):
        return SampleResult(domain, sample_id, False, f"invalid label: {label!r}")

    checker = _DOMAIN_CHECKERS[domain]
    all_violations: set[str] = set(checker(sample_dir))

    if label == "pass":
        if all_violations:
            return SampleResult(
                domain,
                sample_id,
                False,
                f"pass sample produced violations: {sorted(all_violations)}",
            )
        return SampleResult(domain, sample_id, True, "no violations (as expected)")

    # label == "fail"
    if not all_violations:
        return SampleResult(
            domain,
            sample_id,
            False,
            "fail sample produced zero violations — rubric did not discriminate",
        )
    missing = expected_violations - all_violations
    if missing:
        return SampleResult(
            domain,
            sample_id,
            False,
            f"fail sample missing expected violations {sorted(missing)}; got {sorted(all_violations)}",
        )
    return SampleResult(
        domain,
        sample_id,
        True,
        f"violations={sorted(all_violations)} covers expected={sorted(expected_violations)}",
    )


def _run_counter_sample(domain: str, sample_dir: Path) -> SampleResult:
    sample_id = sample_dir.name
    expected_path = sample_dir / "expected.json"
    if not expected_path.exists():
        return SampleResult(
            domain, sample_id, False, "missing expected.json", kind="counter_fp"
        )
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    label = expected.get("label")
    if label not in ("false_positive", "false_negative"):
        return SampleResult(
            domain,
            sample_id,
            False,
            f"invalid counter-example label: {label!r}",
            kind="counter_fp",
        )
    expected_violations = set(expected.get("expected_violations", []))
    observed_violations = set(expected.get("observed_violations", []))

    checker = _DOMAIN_CHECKERS[domain]
    current: set[str] = set(checker(sample_dir))

    kind = "counter_fp" if label == "false_positive" else "counter_fn"

    if label == "false_positive":
        # Ground truth: clean. checker historically flagged. Reproduced if
        # checker still reports a superset of the observed violations.
        if current and observed_violations and observed_violations <= current:
            return SampleResult(
                domain,
                sample_id,
                True,
                f"reproduced false_positive (checker still flags {sorted(current)})",
                kind=kind,
            )
        if not current:
            return SampleResult(
                domain,
                sample_id,
                True,
                "RESOLVED: checker now clean — promote to golden/pass",
                kind=kind,
            )
        return SampleResult(
            domain,
            sample_id,
            True,
            f"partial: checker behavior shifted, current={sorted(current)} "
            f"observed={sorted(observed_violations)}",
            kind=kind,
        )

    # label == "false_negative"
    # Ground truth: has violations. checker historically missed. Reproduced
    # if checker still misses expected_violations (none of them present).
    still_missing = expected_violations - current
    if expected_violations and still_missing == expected_violations:
        return SampleResult(
            domain,
            sample_id,
            True,
            f"reproduced false_negative (checker still misses {sorted(still_missing)})",
            kind=kind,
        )
    if expected_violations and not still_missing:
        return SampleResult(
            domain,
            sample_id,
            True,
            "RESOLVED: checker now catches all expected — promote to golden/fail",
            kind=kind,
        )
    return SampleResult(
        domain,
        sample_id,
        True,
        f"partial: checker behavior shifted, current={sorted(current)} "
        f"expected={sorted(expected_violations)}",
        kind=kind,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="domain validity golden-set smoke")
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated '<domain>:<sample_id>' filters (default: all).",
    )
    parser.add_argument(
        "--with-w3c",
        action="store_true",
        help="Web checker additionally calls the W3C Nu HTML Checker online API. "
        "Outbound HTTPS per HTML file — off by default.",
    )
    args = parser.parse_args()

    global _WITH_W3C
    _WITH_W3C = bool(args.with_w3c)

    filters: set[tuple[str, str]] = set()
    for token in (t.strip() for t in args.only.split(",")):
        if not token:
            continue
        if ":" not in token:
            print(f"--only expects '<domain>:<sample_id>', got {token!r}", file=sys.stderr)
            return 2
        domain, sample_id = token.split(":", 1)
        filters.add((domain.strip(), sample_id.strip()))

    golden_results: list[SampleResult] = []
    counter_results: list[SampleResult] = []
    for domain in _DOMAIN_CHECKERS:
        samples = _discover_samples(domain)
        if not samples:
            print(f"[WARN] no golden samples found for domain={domain}", file=sys.stderr)
        else:
            for sample_dir in samples:
                if filters and (domain, sample_dir.name) not in filters:
                    continue
                golden_results.append(_run_sample(domain, sample_dir))

        counters = _discover_counter_examples(domain)
        for sample_dir in counters:
            if filters and (domain, sample_dir.name) not in filters:
                continue
            counter_results.append(_run_counter_sample(domain, sample_dir))

    print("\nDomain validity smoke")
    print("---------------------")
    passed = 0
    for r in golden_results:
        status = "OK  " if r.ok else "FAIL"
        print(f"  {status}  {r.domain}:{r.sample_id} — {r.message}")
        if r.ok:
            passed += 1
    print(f"{passed}/{len(golden_results)} golden samples passed.")

    if counter_results:
        print("\nCounter-examples (known-gap tracker)")
        print("------------------------------------")
        resolved = 0
        reproduced = 0
        counter_errors = 0
        for r in counter_results:
            status = "OK  " if r.ok else "FAIL"
            print(f"  {status}  {r.domain}:{r.sample_id} [{r.kind}] — {r.message}")
            if not r.ok:
                counter_errors += 1
            elif r.message.startswith("RESOLVED"):
                resolved += 1
            else:
                reproduced += 1
        print(
            f"counter-examples: {reproduced} reproduced, {resolved} RESOLVED "
            f"(promote to golden), {counter_errors} config errors."
        )
        # counter-example config errors fail the smoke; reproduced/resolved do not.
        if counter_errors:
            return 1

    return 0 if golden_results and passed == len(golden_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
