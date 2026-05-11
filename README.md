# orch-engine

오케스트레이션 엔진입니다. `Planner → Builder → Verifier → Orchestrator` 가
파일 기반으로 결과를 주고받으며, Verifier 피드백이 다음 cycle Planner 입력에
반영돼 자동으로 산출물을 개선·검증합니다.

이 폴더는 self-contained 입니다 — `orch-engine/` 만 있어도 동작합니다.

---

## 폴더 구조

```text
orch-engine/
  core/
    app.py        CLI 진입점 (init / run)
    loop.py       역할 실행 루프 + cycle 반복
    store.py      .orch 파일 read/write 유틸
  adapters/
    base.py       Adapter 인터페이스 (역할별 응답 contract)
    scripted.py   ScriptedAdapter (가짜 응답으로 루프 검증용)
  tools/
    mvp_smoke.py  2 사이클 자동 개선 + STOP smoke
  templates/
    orch/         init 시 대상 폴더에 복사되는 .orch 템플릿
```

대상 프로젝트의 상태는 그 폴더 안의 `.orch/` 에 저장됩니다 (config / runtime / tasks /
reviews / artifacts).

---

## 사용 (MVP)

```powershell
# 메뉴 진입 (init / run / 종료)
python -m core.launcher

# CLI 직접 — scripted (기본): 가짜 응답 어댑터로 루프만 검증
python -m core.app init --target <target_path> --goal "<목표 문장>"
python -m core.app run  --target <target_path> --max-cycles 1

# mixed (권장): planner/orchestrator=codex_cli, builder/verifier=claude_cli
python -m core.app init --target <target_path> --goal "<목표 문장>" --profile mixed
python -m core.app run  --target <target_path> --max-cycles 1

# 단일 어댑터 프로필도 있음
python -m core.app init --target <target_path> --goal "..." --profile claude
python -m core.app init --target <target_path> --goal "..." --profile codex

# 빌더 모델만 약화해서 비용 절감 (로직은 동일, 다른 역할은 그대로)
python -m core.app init --target <target_path> --goal "..." --profile mixed --builder-model haiku

# smoke (자동 개선 루프 + STOP, 실제 LLM 호출 없음)
python -m tools.mvp_smoke
```

`--max-cycles` 는 보통 **1** 로 충분합니다 (현재 어댑터·도구 정책에서 cycle 1
통과가 정상). 한 번에 끝나지 않은 경우만 2 이상으로 올립니다.

역할별 어댑터 매핑은 init 후 `<target>/.orch/config/roles.json` 의 `adapters`
필드에서 자유롭게 바꿀 수 있습니다 (고정 룰 아님). 모델은 `models` 필드에 역할별로
저장됩니다 (`--builder-model` 은 그중 `builder` 만 채우는 단축 옵션).

`.orch/STOP` 파일을 만들면 다음 역할 실행 전에 멈춥니다.

---

## 진행 상태

- [x] `.orch/` 템플릿
- [x] store / adapter / loop / app
- [x] ScriptedAdapter + 2 사이클 자동 개선 smoke + STOP 감지
- [x] `claude_cli` / `codex_cli` 어댑터 + 역할별 매핑 + `--profile mixed`
- [x] mixed live 다중 케이스 cycle 1 통과 (make_toc3 / todo_normalize / mini_frontmatter)
- [x] `claude_cli` / `codex_cli` timeout 1회 재시도
- [x] 메뉴형 launcher (`python -m core.launcher`)
- [x] **M0 (사용 가능 최소형) 잠금** — 외부 빈 폴더 init→run→complete+pass
