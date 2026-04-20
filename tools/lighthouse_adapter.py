"""Wrapper around the Lighthouse CLI / JSON report for the web domain.

B 2단계 (g) 두 번째 래퍼. web 도메인 `scoring.dimensions.lighthouse_performance`
soft-fail 판정을 실제 수치로 증빙하기 위한 어댑터. W3C 래퍼와 동일한
offline / self-check / online 패턴.

## 모드
- **offline (default)**: no-op. 정책/환경이 허용되지 않을 때 안전하게 빈 결과.
- **self-check**: 내장 fixture JSON 파싱으로 code path 검증.
- **from-report**: 기존 Lighthouse JSON 리포트(`--report path.json`) 를 파싱.
- **online**: `lighthouse` CLI 를 subprocess 로 호출 (Node + `npm i -g lighthouse`
  필요). `--url <url> --online` 필수.

## 출력 (정규화)
    {
      "mode": "offline" | "self-check" | "from-report" | "online",
      "categories": {
        "performance": float|None,
        "accessibility": float|None,
        "best-practices": float|None,
        "seo": float|None,
        "pwa": float|None    # present if Lighthouse included
      },
      "below_threshold": ["category", ...]   # score < 0.9 (Lighthouse 권장)
      "findings": [ { "id": audit_id, "score": float|None,
                      "title": str, "description": str? }, ... ]
    }

## 실행
    python -m tools.lighthouse_adapter --self-check
    python -m tools.lighthouse_adapter --report path/to/lighthouse.json
    python -m tools.lighthouse_adapter --url http://localhost:3000 --online
        # 옵션: --preset desktop|mobile (default mobile)

## 향후
- `domain_validity_smoke.py` 의 web 체커에 `--with-lighthouse --report-dir <d>`
  옵션으로 연결. 샘플 폴더 안에 `lighthouse.json` 이 이미 있으면 그걸 from-report
  로 소비 → `lighthouse_performance_low` violation 산출.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass


DEFAULT_CATEGORY_THRESHOLD = 0.9
DEFAULT_TIMEOUT = 120.0
_ALLOWED_PRESETS = ("mobile", "desktop")


def _normalize_report(payload: dict) -> dict:
    """Flatten a Lighthouse JSON report to our compact shape."""
    categories_raw = payload.get("categories") or {}
    categories: dict[str, float | None] = {}
    for key, value in categories_raw.items():
        if not isinstance(value, dict):
            continue
        score = value.get("score")
        categories[key] = float(score) if isinstance(score, (int, float)) else None

    below: list[str] = []
    for key, score in categories.items():
        if score is not None and score < DEFAULT_CATEGORY_THRESHOLD:
            below.append(key)

    # Collect audits that FAILED (score == 0) or are notably low (<0.5).
    audits_raw = payload.get("audits") or {}
    findings: list[dict] = []
    for audit_id, audit in audits_raw.items():
        if not isinstance(audit, dict):
            continue
        score = audit.get("score")
        if not isinstance(score, (int, float)):
            continue
        if score < 0.5:
            findings.append(
                {
                    "id": audit_id,
                    "score": float(score),
                    "title": str(audit.get("title", "")),
                    "description": str(audit.get("description", "")).strip() or None,
                }
            )
    findings.sort(key=lambda x: (x["score"], x["id"]))

    return {
        "categories": categories,
        "below_threshold": sorted(below),
        "findings": findings,
    }


_SELF_CHECK_FIXTURE = {
    "categories": {
        "performance": {"score": 0.72, "title": "Performance"},
        "accessibility": {"score": 0.95, "title": "Accessibility"},
        "best-practices": {"score": 0.88, "title": "Best Practices"},
        "seo": {"score": 0.90, "title": "SEO"},
    },
    "audits": {
        "first-contentful-paint": {"score": 0.45, "title": "First Contentful Paint"},
        "largest-contentful-paint": {"score": 0.30, "title": "Largest Contentful Paint"},
        "total-blocking-time": {"score": 0.25, "title": "Total Blocking Time"},
        "color-contrast": {"score": 1.0, "title": "Background and foreground colors..."},
    },
}


def run_cli(
    url: str, preset: str = "mobile", timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """Invoke the lighthouse CLI via subprocess and return the parsed JSON.

    Raises FileNotFoundError if `lighthouse` is not on PATH.
    Raises subprocess.CalledProcessError on non-zero exit.
    """
    if preset not in _ALLOWED_PRESETS:
        raise ValueError(f"preset must be one of {_ALLOWED_PRESETS}")
    if shutil.which("lighthouse") is None and shutil.which("lighthouse.cmd") is None:
        raise FileNotFoundError("lighthouse CLI not on PATH (try `npm i -g lighthouse`)")

    with tempfile.TemporaryDirectory(prefix="lh-adapter-") as tmp:
        out_path = Path(tmp) / "report.json"
        cmd = [
            "lighthouse",
            url,
            "--output=json",
            f"--output-path={out_path}",
            "--chrome-flags=--headless",
            f"--preset={preset}",
            "--quiet",
        ]
        subprocess.run(cmd, timeout=timeout, check=True)
        return json.loads(out_path.read_text(encoding="utf-8"))


def from_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Lighthouse CLI / report adapter")
    parser.add_argument("--url", help="target URL (with --online)")
    parser.add_argument("--online", action="store_true", help="invoke lighthouse CLI")
    parser.add_argument("--report", help="parse existing lighthouse JSON report file")
    parser.add_argument("--self-check", action="store_true", help="use embedded fixture")
    parser.add_argument("--preset", default="mobile", choices=_ALLOWED_PRESETS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args()

    mode = "offline"
    payload: dict = {}

    if args.self_check:
        mode = "self-check"
        payload = _SELF_CHECK_FIXTURE
    elif args.report:
        mode = "from-report"
        try:
            payload = from_report(Path(args.report))
        except OSError as exc:
            print(f"[ERR] read failed: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"[ERR] invalid report JSON: {exc}", file=sys.stderr)
            return 1
    elif args.online:
        if not args.url:
            print("[ERR] --online requires --url <url>", file=sys.stderr)
            return 2
        mode = "online"
        try:
            payload = run_cli(args.url, args.preset, args.timeout)
        except FileNotFoundError as exc:
            print(f"[ERR] {exc}", file=sys.stderr)
            return 1
        except subprocess.CalledProcessError as exc:
            print(f"[ERR] lighthouse exit {exc.returncode}", file=sys.stderr)
            return 1
        except json.JSONDecodeError as exc:
            print(f"[ERR] invalid report JSON from lighthouse: {exc}", file=sys.stderr)
            return 1
    else:
        # offline default: do nothing, emit empty result
        mode = "offline"

    normalized = _normalize_report(payload) if payload else {
        "categories": {},
        "below_threshold": [],
        "findings": [],
    }
    result = {"mode": mode, **normalized}

    if args.json_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    cats = result["categories"]
    below = result["below_threshold"]
    findings = result["findings"]
    print(f"[{mode}] categories={len(cats)} below_0.9={len(below)} low_audits={len(findings)}")
    for key, val in cats.items():
        marker = " *" if key in below else ""
        print(f"  {key:20s} score={val if val is not None else 'N/A'}{marker}")
    if findings:
        print("  low audits:")
        for f in findings[:10]:
            print(f"    - {f['id']} score={f['score']} — {f['title']}")
        if len(findings) > 10:
            print(f"    ... +{len(findings) - 10} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
