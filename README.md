# orch-engine MVP

2026-05 리셋 이후 새로 시작한 오케스트레이션 엔진입니다.
`Planner → Builder → Verifier → Orchestrator` 가 파일 기반으로 결과를 주고받으며
**Verifier 피드백이 다음 cycle Planner 입력에 들어가** 최소 2 사이클 자동 개선되는 것을
증명하는 게 이번 MVP 의 목표입니다.

상세 설계는 상위 폴더의 다음 문서를 참고하세요:

- `../오케스트레이션_MVP_재시작_설계.md`
- `../오케스트레이션_MVP_구현_시작가이드.md`
- `../project-root.md`, `../memory/handoff.md`, `../memory/next-work.md`

기존 구현은 `../orch-engine_legacy/` 에 있고, **참고만** 합니다 (복사 금지).

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
# scripted (기본): 가짜 응답 어댑터로 루프만 검증
python -m core.app init --target <target_path> --goal "<목표 문장>"
python -m core.app run  --target <target_path> --max-cycles 2

# mixed (박제관 선호 매핑): planner/orchestrator=codex_cli, builder/verifier=claude_cli
python -m core.app init --target <target_path> --goal "<목표 문장>" --profile mixed
python -m core.app run  --target <target_path> --max-cycles 2

# 단일 어댑터 프로필도 있음
python -m core.app init --target <target_path> --goal "..." --profile claude
python -m core.app init --target <target_path> --goal "..." --profile codex

# smoke (자동 개선 루프 + STOP, 실제 LLM 호출 없음)
python -m tools.mvp_smoke
```

역할별 어댑터 매핑은 init 후 `<target>/.orch/config/roles.json` 의 `adapters`
필드에서 자유롭게 바꿀 수 있습니다 (고정 룰 아님).

`.orch/STOP` 파일을 만들면 다음 역할 실행 전에 멈춥니다.

---

## 진행 상태

- [x] `.orch/` 템플릿
- [x] store / adapter / loop / app
- [x] ScriptedAdapter + 2 사이클 자동 개선 smoke
- [x] STOP 감지 smoke
- [x] `claude_cli` / `codex_cli` 어댑터 + 역할별 매핑 + `--profile mixed`
- [x] live target 폴더에서 mixed 실측 smoke (live04: cycle 1 needs_iteration → cycle 2 pass)
