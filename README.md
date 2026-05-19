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
    mvp_smoke.py  2 사이클 자동 개선 + STOP smoke (그 외 실패 경로 smoke 도 같은 폴더)
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

# CLI 직접 — scripted (기본): 가짜 응답 어댑터로 루프만 검증 (cycle 2 에서 pass)
python -m core.app init --target <target_path> --goal "<목표 문장>"
python -m core.app run  --target <target_path> --max-cycles 2

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

위 명령은 **PowerShell 기준**으로 검증되었습니다. bash / cmd / WSL 에서는 따옴표·경로 인용 차이로 막힐 수 있으니, 그때는 PowerShell 사용을 권장합니다.

`--max-cycles` 는 live 어댑터 (mixed / claude / codex) 에서는 보통 **1** 로
충분합니다 (현재 어댑터·도구 정책에서 cycle 1 통과가 정상). scripted 는 2 사이클
데모 어댑터라 **2** 가 필요합니다. 한 번에 끝나지 않은 경우만 더 올립니다.

역할별 어댑터 매핑은 init 후 `<target>/.orch/config/roles.json` 의 `adapters`
필드에서 자유롭게 바꿀 수 있습니다 (고정 룰 아님). 모델은 `models` 필드에 역할별로
저장됩니다 (`--builder-model` 은 그중 `builder` 만 채우는 단축 옵션).

`run` 종료 시 마지막 줄에 결과 위치가 절대경로로 출력됩니다 (`artifacts: <target>/.orch/artifacts`).

`run` 이 실패하면 stderr 에 `실패: ...` 1줄 + `다음 조치: ...` 1줄을 출력하고 exit
code `2` 로 종료합니다. 어댑터 CLI 미설치 / timeout 재시도까지 실패 / 어댑터 호출 오류
3종이 같은 포맷입니다.

`init` 도 실패 시 같은 포맷으로 출력합니다 (기존 `.orch/` 존재 / 폴더 권한 거부 등, exit code `2`).

`init` 은 대상 폴더에 이미 `.orch/` 가 있으면 덮어쓰지 않고 중단합니다. 다시
초기화하려면 사용자가 `.orch/` 를 직접 삭제한 뒤 `init` 을 다시 실행하세요.

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
