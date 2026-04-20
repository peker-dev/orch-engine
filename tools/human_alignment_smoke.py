"""Cross-check engine verifier output against 박제관's own judgments.

B 2단계 (c): 자동 verifier 판정(= session.json score_history)과 박제관이 동일
사이클을 직접 본 결과를 비교해 정렬도(alignment rate)를 계산한다. 샘플은
박제관이 `<target>/.orch/human_judgments/cycle-NNN.json` 형식으로 채워 넣는
post-hoc 메모다.

## 판정 파일 포맷
    <target>/.orch/human_judgments/cycle-NNN.json
    {
        "cycle": 3,
        "judged_at": "2026-04-18",
        "judge": "박제관",
        "functional_verdict": "pass" | "needs_iteration" | "rejected",
        "human_verdict":      "pass" | "needs_iteration" | "rejected",
        "notes": "자유 서술"
    }

엔진의 verifier 결과와 박제관 판정을 사이클별로 비교한다. handoff 모드에서
verifier_human 자체가 박제관이었다면 human_verdict는 자기 자신과 비교되므로
자명히 일치 — 그럴 땐 functional_verdict 정렬도만 의미 있음.

## 정렬 분류
- aligned: 자동 판정 == 박제관 판정
- auto_strict: 자동=실패(needs_iteration/rejected) but 박제관=pass (체커가 과엄격)
- auto_lax:    자동=pass but 박제관=실패 (체커가 관대)
- other_drift: 기타 불일치 (needs_iteration ↔ rejected 등)

## 종료 코드
- rc=0: 판정 파일이 있든 없든, 스키마 오류가 없으면 성공
- rc=1: 판정 파일 스키마 오류 (필수 필드 누락, 라벨 오타)
- rc=2: --target 경로 문제

Usage:
    python -m tools.human_alignment_smoke --target <target_path>
    python -m tools.human_alignment_smoke --target <target> --json-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass


VERDICT_VALUES = ("pass", "needs_iteration", "rejected")
JUDGMENT_FILE_RE = re.compile(r"^cycle-(\d{3})\.json$")


@dataclass(slots=True)
class CycleAlignment:
    cycle: int
    auto_functional: str
    auto_human: str
    human_functional: str
    human_human: str
    functional_alignment: str
    human_alignment: str


def _classify(auto_verdict: str, human_verdict: str) -> str:
    if auto_verdict == human_verdict:
        return "aligned"
    if auto_verdict in ("needs_iteration", "rejected") and human_verdict == "pass":
        return "auto_strict"
    if auto_verdict == "pass" and human_verdict in ("needs_iteration", "rejected"):
        return "auto_lax"
    return "other_drift"


def _load_history(target: Path) -> dict[int, dict]:
    session_path = target / ".orch" / "runtime" / "session.json"
    if not session_path.exists():
        return {}
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[int, dict] = {}
    for entry in session.get("score_history") or []:
        if isinstance(entry, dict) and "cycle" in entry:
            try:
                out[int(entry["cycle"])] = entry
            except (TypeError, ValueError):
                continue
    return out


def _load_judgments(target: Path) -> tuple[dict[int, dict], list[str]]:
    root = target / ".orch" / "human_judgments"
    if not root.exists():
        return {}, []

    errors: list[str] = []
    found: dict[int, dict] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_file():
            continue
        m = JUDGMENT_FILE_RE.match(entry.name)
        if not m:
            continue
        cycle_num = int(m.group(1))
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{entry.name}: invalid JSON ({exc})")
            continue

        required = ("functional_verdict", "human_verdict")
        missing = [k for k in required if k not in data]
        if missing:
            errors.append(f"{entry.name}: missing fields {missing}")
            continue

        for field in required:
            if data[field] not in VERDICT_VALUES:
                errors.append(
                    f"{entry.name}: {field}={data[field]!r} not in {VERDICT_VALUES}"
                )
                data = None  # type: ignore[assignment]
                break
        if data is None:
            continue
        found[cycle_num] = data
    return found, errors


def _engine_functional_verdict(entry: dict) -> str:
    raw = str(entry.get("functional_result", "")).strip()
    return raw if raw in VERDICT_VALUES else "unknown"


def _engine_human_verdict(entry: dict) -> str:
    raw = str(entry.get("human_result", "")).strip()
    return raw if raw in VERDICT_VALUES else "unknown"


def compute(target: Path) -> tuple[dict, list[str]]:
    history = _load_history(target)
    judgments, errors = _load_judgments(target)

    alignments: list[CycleAlignment] = []
    for cycle_num in sorted(judgments.keys()):
        engine = history.get(cycle_num)
        if engine is None:
            errors.append(
                f"cycle-{cycle_num:03d}.json: no engine history entry for cycle {cycle_num}"
            )
            continue
        auto_func = _engine_functional_verdict(engine)
        auto_human = _engine_human_verdict(engine)
        human_func = judgments[cycle_num]["functional_verdict"]
        human_human = judgments[cycle_num]["human_verdict"]
        alignments.append(
            CycleAlignment(
                cycle=cycle_num,
                auto_functional=auto_func,
                auto_human=auto_human,
                human_functional=human_func,
                human_human=human_human,
                functional_alignment=_classify(auto_func, human_func),
                human_alignment=_classify(auto_human, human_human),
            )
        )

    def _count(tag: str, attr: str) -> int:
        return sum(1 for a in alignments if getattr(a, attr) == tag)

    total = len(alignments)
    functional_counts = {
        tag: _count(tag, "functional_alignment")
        for tag in ("aligned", "auto_strict", "auto_lax", "other_drift")
    }
    human_counts = {
        tag: _count(tag, "human_alignment")
        for tag in ("aligned", "auto_strict", "auto_lax", "other_drift")
    }

    report = {
        "target": str(target),
        "cycles_with_judgments": total,
        "engine_history_cycles": sorted(history.keys()),
        "functional_alignment": functional_counts,
        "functional_alignment_rate": (functional_counts["aligned"] / total) if total else None,
        "human_alignment": human_counts,
        "human_alignment_rate": (human_counts["aligned"] / total) if total else None,
        "per_cycle": [
            {
                "cycle": a.cycle,
                "auto_functional": a.auto_functional,
                "human_functional": a.human_functional,
                "functional_alignment": a.functional_alignment,
                "auto_human": a.auto_human,
                "human_human": a.human_human,
                "human_alignment": a.human_alignment,
            }
            for a in alignments
        ],
    }
    return report, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="engine vs human verdict alignment")
    parser.add_argument("--target", required=True, help="target project directory (contains .orch/)")
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    if not (target / ".orch").exists():
        print(f"[ERR] .orch/ not found under {target}", file=sys.stderr)
        return 2

    report, errors = compute(target)

    reports_dir = target / ".orch" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "human_alignment.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"[ok] wrote {json_path.relative_to(target)}")
        total = report["cycles_with_judgments"]
        if total == 0:
            print(
                "[info] no human_judgments/*.json recorded — "
                "drop files into .orch/human_judgments/ to compute alignment."
            )
        else:
            fa = report["functional_alignment"]
            ha = report["human_alignment"]
            print(
                f"cycles_judged={total} "
                f"functional_aligned={fa['aligned']}/{total} "
                f"(strict={fa['auto_strict']}, lax={fa['auto_lax']}, drift={fa['other_drift']})"
            )
            print(
                f"                human_aligned={ha['aligned']}/{total} "
                f"(strict={ha['auto_strict']}, lax={ha['auto_lax']}, drift={ha['other_drift']})"
            )

    if errors:
        print("\n[errors]", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
