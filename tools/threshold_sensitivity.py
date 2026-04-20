"""Threshold sensitivity analyzer for `scoring.thresholds`.

B 2단계 (d): `_orchestrator_decision`은 `functional_score >=
functional_pass` AND `human_score >= human_pass` 를 `complete_cycle` 조건으로
쓴다. 이 분석기는 `.orch/runtime/session.json` score_history 를 읽어
threshold 를 ±δ 스위프했을 때 **complete/not-complete 판정이 몇 건 뒤집히는가**
를 집계한다.

## 무엇을 답하는가
- 현재 threshold 가 **견고한가** (대부분 사이클이 threshold 에서 멀리 떨어짐)
- **borderline 사이클** — score 가 threshold ±0.05 안에 있는 경우 나열
- δ 를 ±0.1 까지 움직여도 몇 건이 뒤집히지 않는지 → 안정성 지표

## 한계
- 이 분석기는 **score 수치 자체는 LLM 이 매긴 결과를 주어진 값으로 사용**한다.
  rubric 을 바꾸면 LLM 이 내는 score 분포가 달라진다는 2차 효과는 다루지 않는다.
- 따라서 결과는 "현재 score 분포가 주어졌을 때 threshold 축 민감도" 이다.

## 합성 데이터 지원
실 세션 없이 분석기 자체를 검증하려면 `--synth N` 로 합성 session 데이터를 생성
해 점검할 수 있다. `--synth` 는 `.orch/` 없이 stdin 없이 바로 스위프 한다.

Usage:
    python -m tools.threshold_sensitivity --target <target>
    python -m tools.threshold_sensitivity --target <target> \
        --threshold-functional 0.85 --threshold-human 0.75
    python -m tools.threshold_sensitivity --synth 30 --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass


ENGINE_ROOT = Path(__file__).resolve().parent.parent


@dataclass(slots=True)
class CyclePoint:
    cycle: int
    functional_score: float
    functional_result: str
    human_score: float
    human_result: str
    recorded_decision: str


def _load_common_thresholds() -> tuple[float, float]:
    common_path = ENGINE_ROOT / "domains" / "common" / "common.yaml"
    data = json.loads(common_path.read_text(encoding="utf-8"))
    t = data.get("defaults", {}).get("scoring", {}).get("thresholds", {})
    return float(t.get("functional_pass", 0.7)), float(t.get("human_pass", 0.7))


def _load_session_cycles(target: Path) -> list[CyclePoint]:
    session_path = target / ".orch" / "runtime" / "session.json"
    if not session_path.exists():
        return []
    data = json.loads(session_path.read_text(encoding="utf-8"))
    points: list[CyclePoint] = []
    for entry in data.get("score_history") or []:
        if not isinstance(entry, dict) or "cycle" not in entry:
            continue
        try:
            cycle = int(entry["cycle"])
        except (TypeError, ValueError):
            continue
        points.append(
            CyclePoint(
                cycle=cycle,
                functional_score=float(entry.get("functional_score", 0.0) or 0.0),
                functional_result=str(entry.get("functional_result", "") or ""),
                human_score=float(entry.get("human_score", 0.0) or 0.0),
                human_result=str(entry.get("human_result", "") or ""),
                recorded_decision=str(entry.get("decision", "") or ""),
            )
        )
    return points


def _synth_cycles(n: int, seed: int | None) -> list[CyclePoint]:
    rng = random.Random(seed)
    points: list[CyclePoint] = []
    for i in range(1, n + 1):
        fscore = round(rng.uniform(0.40, 0.99), 3)
        hscore = round(rng.uniform(0.40, 0.99), 3)
        fresult = "pass" if fscore >= 0.8 and rng.random() < 0.85 else "needs_iteration"
        hresult = "pass" if hscore >= 0.75 and rng.random() < 0.85 else "needs_iteration"
        decision = (
            "complete_cycle"
            if fresult == "pass" and hresult == "pass" and fscore >= 0.8 and hscore >= 0.75
            else "needs_iteration"
        )
        points.append(
            CyclePoint(
                cycle=i,
                functional_score=fscore,
                functional_result=fresult,
                human_score=hscore,
                human_result=hresult,
                recorded_decision=decision,
            )
        )
    return points


def _is_complete(
    point: CyclePoint, fpass: float, hpass: float
) -> bool:
    function_ok = point.functional_result == "pass" and point.functional_score >= fpass
    human_ok = point.human_result == "pass" and point.human_score >= hpass
    return function_ok and human_ok


def sweep(
    points: list[CyclePoint],
    base_fpass: float,
    base_hpass: float,
    delta_range: float,
    delta_step: float,
) -> dict:
    """Return per-delta flip counts and borderline cycle listing."""
    if not points:
        return {
            "baseline": {"functional_pass": base_fpass, "human_pass": base_hpass},
            "cycle_count": 0,
            "deltas": [],
            "borderline_cycles": [],
        }

    baseline_completes = {p.cycle: _is_complete(p, base_fpass, base_hpass) for p in points}

    # build delta list in step increments including 0
    deltas: list[float] = []
    n_steps = int(round(delta_range / delta_step))
    for i in range(-n_steps, n_steps + 1):
        deltas.append(round(i * delta_step, 6))

    delta_rows: list[dict] = []
    for d in deltas:
        fpass = base_fpass + d
        hpass = base_hpass + d
        flips_to_incomplete = 0
        flips_to_complete = 0
        flipped_cycles: list[int] = []
        for p in points:
            now = _is_complete(p, fpass, hpass)
            was = baseline_completes[p.cycle]
            if now != was:
                flipped_cycles.append(p.cycle)
                if was and not now:
                    flips_to_incomplete += 1
                else:
                    flips_to_complete += 1
        delta_rows.append(
            {
                "delta": round(d, 4),
                "functional_pass": round(fpass, 4),
                "human_pass": round(hpass, 4),
                "flips_total": flips_to_incomplete + flips_to_complete,
                "flips_to_incomplete": flips_to_incomplete,
                "flips_to_complete": flips_to_complete,
                "flipped_cycles": flipped_cycles,
            }
        )

    borderline: list[dict] = []
    for p in points:
        f_gap = abs(p.functional_score - base_fpass)
        h_gap = abs(p.human_score - base_hpass)
        if min(f_gap, h_gap) <= 0.05:
            borderline.append(
                {
                    "cycle": p.cycle,
                    "functional_score": p.functional_score,
                    "functional_gap": round(p.functional_score - base_fpass, 4),
                    "human_score": p.human_score,
                    "human_gap": round(p.human_score - base_hpass, 4),
                    "recorded_decision": p.recorded_decision,
                }
            )

    # headline stability: percent of cycles that do NOT flip in ±0.05 window
    inner = [r for r in delta_rows if abs(r["delta"]) <= 0.05 + 1e-9]
    max_flip_in_window = max((r["flips_total"] for r in inner), default=0)
    stability = 1.0 - (max_flip_in_window / len(points)) if points else 1.0

    return {
        "baseline": {"functional_pass": base_fpass, "human_pass": base_hpass},
        "cycle_count": len(points),
        "baseline_complete_count": sum(1 for v in baseline_completes.values() if v),
        "stability_in_plus_minus_0_05": round(stability, 4),
        "deltas": delta_rows,
        "borderline_cycles": borderline,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="threshold sensitivity sweep")
    parser.add_argument("--target", help="target project directory (.orch/ root)")
    parser.add_argument("--synth", type=int, default=0, help="generate N synthetic cycles")
    parser.add_argument("--seed", type=int, default=None, help="synth seed")
    parser.add_argument("--threshold-functional", type=float, default=None)
    parser.add_argument("--threshold-human", type=float, default=None)
    parser.add_argument("--delta-range", type=float, default=0.10)
    parser.add_argument("--delta-step", type=float, default=0.01)
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args()

    if args.synth and args.target:
        print("[ERR] use either --target or --synth, not both", file=sys.stderr)
        return 2

    base_fpass, base_hpass = _load_common_thresholds()
    if args.threshold_functional is not None:
        base_fpass = args.threshold_functional
    if args.threshold_human is not None:
        base_hpass = args.threshold_human

    if args.synth:
        points = _synth_cycles(args.synth, args.seed)
        target_label = f"<synth n={args.synth} seed={args.seed}>"
        reports_dir = None
    elif args.target:
        target = Path(args.target).resolve()
        if not (target / ".orch").exists():
            print(f"[ERR] .orch/ not found under {target}", file=sys.stderr)
            return 2
        points = _load_session_cycles(target)
        target_label = str(target)
        reports_dir = target / ".orch" / "reports"
    else:
        print("[ERR] specify --target <path> or --synth N", file=sys.stderr)
        return 2

    result = sweep(points, base_fpass, base_hpass, args.delta_range, args.delta_step)
    result["target"] = target_label

    if reports_dir is not None:
        reports_dir.mkdir(parents=True, exist_ok=True)
        out_path = reports_dir / "threshold_sensitivity.json"
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not args.json_only:
            print(f"[ok] wrote {out_path.relative_to(Path(args.target).resolve())}")

    if args.json_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(
        f"baseline functional_pass={base_fpass} human_pass={base_hpass} "
        f"cycles={result['cycle_count']} "
        f"baseline_complete={result.get('baseline_complete_count', 0)}"
    )
    print(
        f"stability in ±0.05 window: {result.get('stability_in_plus_minus_0_05', 1.0):.2%} "
        "(1.0 = no flips under small threshold perturbation)"
    )

    print("\nper-delta flip counts (δ applied to BOTH thresholds)")
    print("    δ  |  fpass  |  hpass  | flips | to_incomplete | to_complete")
    print("  -----|---------|---------|-------|---------------|------------")
    for row in result["deltas"]:
        print(
            f"  {row['delta']:+.3f} |  {row['functional_pass']:.3f} |  "
            f"{row['human_pass']:.3f} | {row['flips_total']:5d} | "
            f"{row['flips_to_incomplete']:13d} | {row['flips_to_complete']:11d}"
        )

    if result["borderline_cycles"]:
        print(f"\nborderline cycles (score within ±0.05 of a threshold):")
        for b in result["borderline_cycles"]:
            print(
                f"  cycle={b['cycle']:3d} f={b['functional_score']:.3f} (gap {b['functional_gap']:+.3f}) "
                f"h={b['human_score']:.3f} (gap {b['human_gap']:+.3f}) decision={b['recorded_decision']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
