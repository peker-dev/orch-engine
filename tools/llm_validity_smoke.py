"""LLM-based validity smoke framework (dry-run by default).

B 2단계 (f): 정적 체커로 잡을 수 없는 **의미론·런타임 갭** (Lighthouse 점수,
vocal feasibility, exit condition, plot regression, 컴파일 에러 등) 을 LLM
verifier 로 판정하도록 하는 프레임워크. LLM 호출은 비용이 들기에 기본은
dry-run (scripted fake adapter) 이고 `--live` 는 `--confirm-cost` 플래그와
함께만 동작한다.

## 샘플 구조
    domains/<domain>/validation/llm_samples/<sample_id>/
        expected.json
        artifact/           # LLM 이 검토할 실제 파일들
            ...

### expected.json
    {
      "domain": "web",
      "label": "pass" | "fail",
      "rubric_focus": "lighthouse_performance",  # 평가 축
      "expected_keywords": ["contrast", "<=80"], # finding/evidence 문자열에
                                                  # 들어가야 할 키워드 (부분 매칭)
      "expected_score_range": [0.0, 0.6],        # optional, 수치 밴드
      "notes": "..."
    }

## 판정 로직
- `result` == expected label → label_aligned=True
- score in expected_score_range (있으면) → score_in_range=True
- expected_keywords 모두 findings+evidence 텍스트에 부분 매칭 → keywords_covered=True
- 세 가지 모두 만족해야 해당 샘플 PASS. 하나라도 틀리면 FAIL 로 집계.

## dry-run 동작
scripted-adapter 가 expected.json 을 보고 "의도된" 응답을 구성해 돌려준다.
이건 프롬프트 구성 / 스키마 / 비교 로직 code path 를 검증할 뿐 LLM 품질은
검증하지 않는다.

## live 동작
`--live --provider claude|codex --confirm-cost` 요구. 실제 CLI adapter 를
invoke 해 응답 받는다. **비용 발생.**

## CLI
    python -m tools.llm_validity_smoke                      # dry-run, 전체 도메인
    python -m tools.llm_validity_smoke --only web
    python -m tools.llm_validity_smoke --live --provider claude --confirm-cost
"""

from __future__ import annotations

import argparse
import json
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


@dataclass(slots=True)
class SampleOutcome:
    domain: str
    sample_id: str
    ok: bool
    label_aligned: bool = False
    score_in_range: bool = True   # True if no range constraint
    keywords_covered: bool = True  # True if no keyword constraint
    message: str = ""
    raw_response: dict = field(default_factory=dict)


def _discover_llm_samples(domain: str) -> list[Path]:
    root = DOMAINS_ROOT / domain / "validation" / "llm_samples"
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _load_artifact_files(artifact_dir: Path) -> dict[str, str]:
    """Read every text file under artifact/ into a {relpath: content} map."""
    files: dict[str, str] = {}
    if not artifact_dir.exists():
        return files
    for path in sorted(artifact_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            files[str(path.relative_to(artifact_dir))] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # skip binaries
            continue
    return files


def _build_prompt(domain: str, rubric_focus: str, artifact_files: dict[str, str]) -> str:
    """Assemble a verifier prompt. Live adapter will receive this as input."""
    header = (
        f"You are the {domain} functional verifier. Evaluate the following "
        f"artifact against the rubric axis '{rubric_focus}'. Respond with a "
        f"utterance.v1 envelope (speaker=verifier_functional) and place the "
        f"structured review (result/score/findings/...) in a fenced ```json``` "
        f"block inside body."
    )
    body_parts = [header, "", "=== ARTIFACT FILES ==="]
    for rel, content in artifact_files.items():
        body_parts.append(f"\n--- {rel} ---\n{content}")
    return "\n".join(body_parts)


def _scripted_response(expected: dict) -> dict:
    """Dry-run: build a response that satisfies expected.json so comparison
    logic can be exercised without a real LLM."""
    label = expected.get("label", "pass")
    focus = expected.get("rubric_focus", "")
    keywords = expected.get("expected_keywords") or []
    score_range = expected.get("expected_score_range")

    if score_range and isinstance(score_range, list) and len(score_range) == 2:
        score = (float(score_range[0]) + float(score_range[1])) / 2
    else:
        score = 0.9 if label == "pass" else 0.4

    findings = [f"scripted verifier note about {focus}"]
    findings.extend(f"keyword {kw} observed" for kw in keywords)
    evidence = [f"evidence for {focus}"]
    blocking = [] if label == "pass" else ["scripted blocking issue"]
    result_value = "pass" if label == "pass" else "needs_iteration"
    return {
        "summary": f"scripted dry-run verdict for {focus}",
        "result": result_value,
        "score": round(score, 3),
        "findings": findings,
        "evidence": evidence,
        "blocking_issues": blocking,
        "suggested_actions": [] if label == "pass" else ["scripted suggestion"],
    }


def _live_response(domain: str, prompt: str, provider: str) -> dict:
    """Placeholder for live LLM invocation. Wiring into the engine adapters
    happens here when --live is approved for cost spend."""
    from adapters.claude_cli import ClaudeCliAdapter  # noqa: WPS433 import-in-function
    from adapters.codex_cli import CodexCliAdapter  # noqa: WPS433
    from adapters.base import Invocation  # noqa: WPS433

    adapter = (
        ClaudeCliAdapter() if provider == "claude" else CodexCliAdapter()
    )
    invocation = Invocation(
        role="verifier_functional",
        objective=f"LLM validity probe for {domain}",
        working_directory=str(ENGINE_ROOT),
        context={"prompt": prompt},
    )
    result = adapter.invoke(invocation)
    return result.payload or {}


def _text_blob(payload: dict) -> str:
    parts: list[str] = [str(payload.get("summary", ""))]
    for key in ("findings", "evidence", "blocking_issues", "suggested_actions"):
        value = payload.get(key)
        if isinstance(value, list):
            parts.extend(str(x) for x in value)
    return "\n".join(parts).lower()


def _compare(expected: dict, payload: dict) -> SampleOutcome:
    label = expected.get("label", "pass")
    result = str(payload.get("result", ""))
    # pass-label aligns with "pass"; fail-label aligns with "fail"/"needs_iteration"/"block"
    if label == "pass":
        label_aligned = result == "pass"
    else:
        label_aligned = result in ("fail", "needs_iteration", "block")

    score_in_range = True
    score_range = expected.get("expected_score_range")
    if isinstance(score_range, list) and len(score_range) == 2:
        try:
            lo, hi = float(score_range[0]), float(score_range[1])
            score = float(payload.get("score", -1))
            score_in_range = lo <= score <= hi
        except (TypeError, ValueError):
            score_in_range = False

    keywords_covered = True
    keywords = expected.get("expected_keywords")
    if isinstance(keywords, list) and keywords:
        blob = _text_blob(payload)
        missing = [kw for kw in keywords if str(kw).lower() not in blob]
        keywords_covered = not missing

    ok = label_aligned and score_in_range and keywords_covered
    bits = []
    if not label_aligned:
        bits.append(f"label mismatch (result={result!r}, expected {label!r})")
    if not score_in_range:
        bits.append(f"score out of range ({payload.get('score')} not in {score_range})")
    if not keywords_covered:
        bits.append("expected keywords missing")
    msg = "all checks passed" if ok else "; ".join(bits)

    return SampleOutcome(
        domain="",
        sample_id="",
        ok=ok,
        label_aligned=label_aligned,
        score_in_range=score_in_range,
        keywords_covered=keywords_covered,
        message=msg,
        raw_response=payload,
    )


def _run_sample(
    domain: str,
    sample_dir: Path,
    mode: str,
    provider: str,
) -> SampleOutcome:
    expected_path = sample_dir / "expected.json"
    if not expected_path.exists():
        return SampleOutcome(
            domain=domain,
            sample_id=sample_dir.name,
            ok=False,
            message="missing expected.json",
        )
    expected = json.loads(expected_path.read_text(encoding="utf-8"))

    artifact_files = _load_artifact_files(sample_dir / "artifact")
    rubric_focus = str(expected.get("rubric_focus", ""))
    prompt = _build_prompt(domain, rubric_focus, artifact_files)

    if mode == "dry_run":
        payload = _scripted_response(expected)
    else:
        payload = _live_response(domain, prompt, provider)

    outcome = _compare(expected, payload)
    outcome.domain = domain
    outcome.sample_id = sample_dir.name
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM validity smoke framework")
    parser.add_argument("--only", default="", help="Comma-separated domain IDs")
    parser.add_argument("--live", action="store_true", help="Invoke real LLM adapter (costs money)")
    parser.add_argument("--provider", choices=["claude", "codex"], default="claude")
    parser.add_argument("--confirm-cost", action="store_true", help="Required with --live")
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args()

    mode = "dry_run"
    if args.live:
        if not args.confirm_cost:
            print(
                "[ERR] --live requires --confirm-cost (explicit cost acknowledgement)",
                file=sys.stderr,
            )
            return 2
        mode = "live"

    if args.only:
        wanted = [t.strip() for t in args.only.split(",") if t.strip()]
        domains = [d for d in DOMAINS if d in wanted]
    else:
        domains = list(DOMAINS)

    all_outcomes: list[SampleOutcome] = []
    for domain in domains:
        samples = _discover_llm_samples(domain)
        for sample_dir in samples:
            all_outcomes.append(_run_sample(domain, sample_dir, mode, args.provider))

    if args.json_only:
        print(json.dumps(
            {
                "mode": mode,
                "sample_count": len(all_outcomes),
                "samples": [
                    {
                        "domain": o.domain,
                        "sample_id": o.sample_id,
                        "ok": o.ok,
                        "label_aligned": o.label_aligned,
                        "score_in_range": o.score_in_range,
                        "keywords_covered": o.keywords_covered,
                        "message": o.message,
                    }
                    for o in all_outcomes
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0 if all(o.ok for o in all_outcomes) else 1

    print(f"\nLLM validity smoke (mode={mode})")
    print("-" * 34)
    if not all_outcomes:
        print(
            "[info] no llm_samples found — add "
            "domains/<d>/validation/llm_samples/<sample_id>/ to use this smoke."
        )
        return 0

    passed = 0
    for o in all_outcomes:
        status = "OK  " if o.ok else "FAIL"
        print(f"  {status}  {o.domain}:{o.sample_id} — {o.message}")
        if o.ok:
            passed += 1
    print(f"{passed}/{len(all_outcomes)} LLM samples passed.")
    return 0 if passed == len(all_outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
