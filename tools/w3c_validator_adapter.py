"""Wrapper around the W3C Nu HTML Checker public API.

B 2단계 (g)의 첫 래퍼. `domain_validity_smoke.py`의 웹 도메인 정적 체커
갭 (`html_parse_error`)을 실제 W3C 파서로 메운다. 이 모듈은 **어댑터만**
제공하고 smoke 에 기본 편입하지 않는다 — 네트워크 의존·응답 시간 때문에
회귀 스위트에 강제로 묶으면 불안정해진다.

## 두 가지 모드
- **offline (default)**: 응답을 만들지 않고 `[]` 반환. 실행 환경이 네트워크가
  없거나 온라인 사용을 승인받지 않았을 때 안전하게 no-op.
- **online**: `https://validator.w3.org/nu/?out=json` 로 multipart 없이 단순
  text/html POST. 응답 JSON 의 `messages[]` 를 파싱해 (type, line, text) 로
  정규화한다.

## 정규화 포맷 (반환)
    [
      {"severity": "error"|"warning"|"info",
       "line": int|None,
       "column": int|None,
       "message": str,
       "raw_type": str   # W3C 원본 type
      }, ...
    ]

## CLI
    python -m tools.w3c_validator_adapter --file path/to/index.html
        # offline -> empty result
    python -m tools.w3c_validator_adapter --file path/to/index.html --online
    python -m tools.w3c_validator_adapter --self-check
        # parse a canned API response fixture (no network)

## 향후 편입 계획
- `domain_validity_smoke.py` 웹 체커에 `--with-w3c` 옵션 추가해 online=True 로
  호출. CI 환경이 네트워크 정책 허용할 때만 on.
- `html_parse_error` violation 을 W3C 응답 기반 `html_w3c_{error,warning}` 로
  확장.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass


W3C_ENDPOINT = "https://validator.w3.org/nu/?out=json"
DEFAULT_UA = "orch-engine-w3c-adapter/0.1"
DEFAULT_TIMEOUT = 10.0


def _normalize_messages(payload: dict) -> list[dict]:
    """Flatten the W3C JSON 'messages' field to our compact shape."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    normalized: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        raw_type = str(m.get("type", ""))
        severity = "error"
        if raw_type in ("warning", "info"):
            severity = raw_type
        elif raw_type == "non-document-error":
            severity = "error"
        line = m.get("lastLine")
        col = m.get("lastColumn")
        normalized.append(
            {
                "severity": severity,
                "line": int(line) if isinstance(line, int) else None,
                "column": int(col) if isinstance(col, int) else None,
                "message": str(m.get("message", "")),
                "raw_type": raw_type,
            }
        )
    return normalized


def validate_html(
    html_text: str,
    online: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Return normalized W3C findings for the given HTML content.

    offline mode returns [] unconditionally. online mode performs one HTTPS
    POST and raises on network / HTTP / parse errors.
    """
    if not online:
        return []

    body = html_text.encode("utf-8")
    request = urllib.request.Request(
        W3C_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 (trusted endpoint)
        raw = resp.read()
    payload = json.loads(raw.decode("utf-8"))
    return _normalize_messages(payload)


_SELF_CHECK_FIXTURE = {
    "messages": [
        {
            "type": "error",
            "lastLine": 3,
            "lastColumn": 12,
            "message": "Element \u201cimg\u201d is missing required attribute \u201calt\u201d.",
            "extract": "<img src=a.png>",
        },
        {
            "type": "info",
            "lastLine": 1,
            "lastColumn": 0,
            "message": "Character encoding was not declared.",
        },
        {
            "type": "warning",
            "lastLine": 5,
            "message": "Consider adding a \u201clang\u201d attribute.",
        },
    ]
}


def _self_check() -> list[dict]:
    return _normalize_messages(_SELF_CHECK_FIXTURE)


def _summarize(findings: list[dict]) -> dict:
    summary = {"error": 0, "warning": 0, "info": 0, "total": len(findings)}
    for f in findings:
        key = f.get("severity")
        if key in summary:
            summary[key] += 1
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="W3C Nu HTML Checker adapter")
    parser.add_argument("--file", help="path to HTML file to validate")
    parser.add_argument("--online", action="store_true", help="POST to https://validator.w3.org/nu/ (default: offline no-op)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--self-check", action="store_true", help="parse a canned fixture (no network)")
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        findings = _self_check()
    elif args.file:
        try:
            html_text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[ERR] read failed: {exc}", file=sys.stderr)
            return 2
        try:
            findings = validate_html(html_text, online=args.online, timeout=args.timeout)
        except urllib.error.URLError as exc:
            print(f"[ERR] network failure: {exc}", file=sys.stderr)
            return 1
        except json.JSONDecodeError as exc:
            print(f"[ERR] invalid response JSON: {exc}", file=sys.stderr)
            return 1
    else:
        print("[ERR] specify --file <path> or --self-check", file=sys.stderr)
        return 2

    summary = _summarize(findings)
    payload = {
        "mode": "self-check" if args.self_check else ("online" if args.online else "offline"),
        "summary": summary,
        "findings": findings,
    }

    if args.json_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    mode_label = payload["mode"]
    print(
        f"[{mode_label}] errors={summary['error']} warnings={summary['warning']} "
        f"info={summary['info']} total={summary['total']}"
    )
    for f in findings:
        loc = f"line {f['line']}" if f.get("line") else "-"
        print(f"  [{f['severity']}] {loc}: {f['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
