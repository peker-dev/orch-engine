"""Post-hoc observation harness for the iterate-and-verify loop.

D 과제: 실제 AI 사이클을 돌렸을 때 **builder가 기존 파일을 부분 수정하는지**
재작성하는지 / 점수가 수렴하는지 / 같은 파일을 반복 터치하는지를 관측한다.
엔진 실행 비용이 들지 않는 post-hoc 분석기다. 사이클을 다 돌린 뒤 대상
`.orch/` 폴더를 읽어 요약 리포트를 생성한다.

## 입력 (이미 엔진이 남기는 것들)
- `.orch/runtime/session.json` → `score_history` (최근 12 cycle의 decision/score)
- `.orch/artifacts/snapshots/cycle-NNN-builder.md` → 사이클별 builder 출력 요약
  (files_changed / unresolved / summary)
- `.orch/artifacts/index.json` → artifact 등록 로그
- `.orch/reviews/functional_latest.json` / `human_latest.json` → 마지막 리뷰

## 출력
- `.orch/reports/cycle_observations.json` — 구조화 데이터
- `.orch/reports/cycle_observations.md` — 사람용 요약

## 계산하는 지표
- cycle_index / decision / functional_score / human_score 궤적
- 사이클별 files_changed 목록
- repeat_files: 직전 사이클에서도 언급된 파일 (iteration-on-same-file 신호)
- new_files: 해당 사이클에서 처음 등장한 파일 (확장 신호)
- rewrite_tendency_hint: 누적적으로 같은 파일 터치 비율

전체 파일 content diff까지는 계산하지 않는다. 파일 단위 "몇 번 만졌는가"
빈도만 집계한다. 더 세밀한 edit-vs-rewrite 비율 측정은 live snapshot
companion(v2)에서 다룰 후보.

## Usage
    python -m tools.cycle_observation_harness --target <target_path>
    python -m tools.cycle_observation_harness --target <target_path> --json-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except AttributeError:
    pass


SNAPSHOT_RE = re.compile(r"cycle-(\d{3})-builder\.md$")
FILES_CHANGED_LINE_RE = re.compile(r"^- Files changed:\s*(.*?)\s*$", re.MULTILINE)
UNRESOLVED_LINE_RE = re.compile(r"^- Unresolved:\s*(.*?)\s*$", re.MULTILINE)
SUMMARY_LINE_RE = re.compile(r"^- Summary:\s*(.*?)\s*$", re.MULTILINE)
TASK_LINE_RE = re.compile(r"^- Task:\s*(.*?)\s*$", re.MULTILINE)

_NONE_MARKERS = {"", "(none)", "(none reported)"}


@dataclass(slots=True)
class CycleObservation:
    cycle: int
    task: str = ""
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    decision: str = ""
    functional_result: str = ""
    functional_score: float = 0.0
    human_result: str = ""
    human_score: float = 0.0
    repeat_files: list[str] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cycle": self.cycle,
            "task": self.task,
            "summary": self.summary,
            "decision": self.decision,
            "functional_result": self.functional_result,
            "functional_score": round(self.functional_score, 4),
            "human_result": self.human_result,
            "human_score": round(self.human_score, 4),
            "files_changed": self.files_changed,
            "unresolved": self.unresolved,
            "repeat_files": self.repeat_files,
            "new_files": self.new_files,
        }


def _parse_list_line(raw: str) -> list[str]:
    raw = raw.strip()
    if raw in _NONE_MARKERS:
        return []
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def _load_snapshot(path: Path) -> tuple[str, str, list[str], list[str]]:
    text = path.read_text(encoding="utf-8")
    task = (TASK_LINE_RE.search(text) or re.match("", "")).group(1) if TASK_LINE_RE.search(text) else ""
    summary = (SUMMARY_LINE_RE.search(text) or re.match("", "")).group(1) if SUMMARY_LINE_RE.search(text) else ""
    files_raw = (FILES_CHANGED_LINE_RE.search(text) or re.match("", "")).group(1) if FILES_CHANGED_LINE_RE.search(text) else ""
    unres_raw = (UNRESOLVED_LINE_RE.search(text) or re.match("", "")).group(1) if UNRESOLVED_LINE_RE.search(text) else ""
    return task, summary, _parse_list_line(files_raw), _parse_list_line(unres_raw)


def _discover_snapshots(target: Path) -> list[tuple[int, Path]]:
    snapshots_dir = target / ".orch" / "artifacts" / "snapshots"
    if not snapshots_dir.exists():
        return []
    found: list[tuple[int, Path]] = []
    for entry in snapshots_dir.iterdir():
        m = SNAPSHOT_RE.search(entry.name)
        if m:
            found.append((int(m.group(1)), entry))
    found.sort(key=lambda x: x[0])
    return found


def _load_history_map(target: Path) -> dict[int, dict]:
    session_path = target / ".orch" / "runtime" / "session.json"
    if not session_path.exists():
        return {}
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    history = session.get("score_history") or []
    out: dict[int, dict] = {}
    for entry in history:
        if isinstance(entry, dict) and "cycle" in entry:
            try:
                cycle_num = int(entry["cycle"])
            except (TypeError, ValueError):
                continue
            out[cycle_num] = entry
    return out


def observe(target: Path) -> dict:
    snapshots = _discover_snapshots(target)
    history_map = _load_history_map(target)

    cycles: list[CycleObservation] = []
    seen_files: set[str] = set()
    per_file_touch_count: Counter[str] = Counter()

    prev_files: set[str] = set()
    for cycle_num, path in snapshots:
        task, summary, files_changed, unresolved = _load_snapshot(path)
        obs = CycleObservation(
            cycle=cycle_num,
            task=task,
            summary=summary,
            files_changed=files_changed,
            unresolved=unresolved,
        )
        entry = history_map.get(cycle_num, {})
        obs.decision = str(entry.get("decision", ""))
        obs.functional_result = str(entry.get("functional_result", ""))
        obs.functional_score = float(entry.get("functional_score", 0.0) or 0.0)
        obs.human_result = str(entry.get("human_result", ""))
        obs.human_score = float(entry.get("human_score", 0.0) or 0.0)

        cur_set = set(files_changed)
        obs.repeat_files = sorted(cur_set & prev_files)
        obs.new_files = sorted(cur_set - seen_files)
        prev_files = cur_set
        seen_files |= cur_set
        for f in files_changed:
            per_file_touch_count[f] += 1

        cycles.append(obs)

    total_file_touches = sum(per_file_touch_count.values())
    unique_files = len(per_file_touch_count)
    repeat_touches = sum(c for c in per_file_touch_count.values() if c > 1) - sum(
        1 for c in per_file_touch_count.values() if c > 1
    )
    rewrite_tendency = None
    if total_file_touches:
        # repeat_share: out of all file touches, how many landed on a file
        # the builder had already touched in a previous cycle.
        repeat_share = repeat_touches / total_file_touches
        rewrite_tendency = round(repeat_share, 4)

    hot_files = per_file_touch_count.most_common(10)

    return {
        "target": str(target),
        "cycle_count": len(cycles),
        "cycles": [c.to_dict() for c in cycles],
        "unique_files_touched": unique_files,
        "total_file_touches": total_file_touches,
        "repeat_touch_share": rewrite_tendency,
        "top_touched_files": [
            {"path": p, "touches": n} for p, n in hot_files
        ],
    }


def _format_markdown(report: dict) -> str:
    lines = ["# Cycle observation report", ""]
    lines.append(f"- Target: `{report['target']}`")
    lines.append(f"- Cycle count: {report['cycle_count']}")
    lines.append(f"- Unique files touched: {report['unique_files_touched']}")
    lines.append(f"- Total file touches: {report['total_file_touches']}")
    if report.get("repeat_touch_share") is not None:
        lines.append(
            f"- Repeat-touch share: {report['repeat_touch_share']:.2%} "
            "(fraction of touches landing on a file from a previous cycle)"
        )
    lines.append("")

    lines.append("## Per-cycle trajectory")
    lines.append("")
    lines.append(
        "| cycle | decision | func_result | func_score | human_result | "
        "human_score | files | repeat | new |"
    )
    lines.append(
        "|------:|----------|-------------|-----------:|--------------|"
        "------------:|------:|-------:|----:|"
    )
    for c in report["cycles"]:
        lines.append(
            f"| {c['cycle']} | {c['decision']} | {c['functional_result']} | "
            f"{c['functional_score']:.2f} | {c['human_result']} | "
            f"{c['human_score']:.2f} | {len(c['files_changed'])} | "
            f"{len(c['repeat_files'])} | {len(c['new_files'])} |"
        )
    lines.append("")

    if report["top_touched_files"]:
        lines.append("## Top touched files")
        lines.append("")
        lines.append("| path | touches |")
        lines.append("|------|--------:|")
        for row in report["top_touched_files"]:
            lines.append(f"| `{row['path']}` | {row['touches']} |")
        lines.append("")

    lines.append("## Interpretation hints")
    lines.append("")
    lines.append(
        "- 높은 repeat-touch share (>0.5) + 점수 상승 → builder가 기존 파일을 반복 터치하며 iteration 中."
    )
    lines.append(
        "- 높은 repeat-touch share + 점수 정체 → 같은 파일을 재작성 중일 가능성. 실제 diff 확인 필요."
    )
    lines.append(
        "- 낮은 repeat-touch share (<0.2) + 점수 정체 → 매 사이클 새 파일 생성 경향. handoff.md D 문제 의심."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="post-hoc cycle observation harness")
    parser.add_argument("--target", required=True, help="target project directory (contains .orch/)")
    parser.add_argument("--json-only", action="store_true", help="skip markdown write, print JSON to stdout")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    if not (target / ".orch").exists():
        print(f"[ERR] .orch/ not found under {target}", file=sys.stderr)
        return 2

    report = observe(target)

    reports_dir = target / ".orch" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "cycle_observations.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    md_path = reports_dir / "cycle_observations.md"
    md_path.write_text(_format_markdown(report), encoding="utf-8")

    print(f"[ok] wrote {json_path.relative_to(target)}")
    print(f"[ok] wrote {md_path.relative_to(target)}")
    print(
        f"cycles={report['cycle_count']} unique_files={report['unique_files_touched']} "
        f"total_touches={report['total_file_touches']} "
        f"repeat_share={report['repeat_touch_share']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
