# orch-engine

파일 기반 멀티 에이전트 오케스트레이션 엔진. 한 프로젝트 폴더 안에서 **기획 → 제작 → 기능 검증 → 사람 리뷰** 순서로 여러 AI CLI(Claude CLI, Codex CLI 등)를 이어 돌려 하나의 산출물을 단계적으로 완성합니다.

---

## 개념 한 줄

> 타겟 프로젝트 폴더 안에 `.orch/` 런타임을 심고, 그 안에서 사이클 단위로 다중 AI 역할을 오케스트레이션합니다.

## 4가지 역할

| 역할 | 기본 adapter | 하는 일 |
|---|---|---|
| `planner` | `claude_cli` | 목표를 보고 다음에 할 task를 뽑음 |
| `builder` | `claude_cli` | task를 실제 파일 작업으로 실현 |
| `verifier_functional` | `codex_cli` | 결과물을 기능 기준으로 점검 |
| `verifier_human` | `codex_cli` (기본) / `codex_app` (handoff 모드) | 사용자 관점 품질 리뷰 |

각 역할은 독립 CLI 프로세스로 실행되며, 산출물 / 상태 / 피드백은 모두 `.orch/` 아래 파일로 주고받습니다.

---

## 빠른 시작

### 런처 (권장)

인자 없이 실행하면 메뉴형 launcher가 뜹니다.

```bash
python -m launcher.app
```

또는 Windows에서:

```powershell
.\launcher\start.ps1
```

메뉴:

1. 새 프로젝트 시작
2. 기존 프로젝트 연결 (retrofit)
3. 오케스트레이션된 프로젝트 재개
4. 기존 오케스트레이션 프로젝트 관리 — 상태 보기 / 사이클 1회 실행 / handoff 요청·응답·취소 / 목표 갱신

한 번 연결된 프로젝트는 메뉴 `4`를 통해 모든 후속 작업을 CLI 명령 없이 메뉴에서 처리할 수 있습니다.

### CLI 직접 호출

```bash
# 타겟 프로젝트 초기화
python -m core.app init --target "C:/path/to/project" \
  --domain web --mode greenfield \
  --project-name my-app --goal-summary "프로젝트 한 줄 목표"

# 상태 확인
python -m core.app status --target "C:/path/to/project"

# 한 사이클 실행 (실제 AI 호출 발생)
python -m core.app run-cycle --target "C:/path/to/project"

# handoff lifecycle
python -m core.app handoff-request --target "..." --mode review_only \
  --reason "..." --what-needs-decision "..."
python -m core.app handoff-status  --target "..."
python -m core.app handoff-ingest  --target "..."
python -m core.app handoff-cancel  --target "..."
```

---

## 사이클 진행 로그

`run-cycle` 실행 중 각 단계 전후로 다음과 같은 로그가 실시간으로 출력됩니다. TTY 환경에서는 단계가 오래 걸릴 때 1초 간격으로 heartbeat(점과 경과 시간)이 같은 줄에 덮어써져 "멈춘 것 아닌가" 걱정을 줄입니다.

```
=== 사이클 1 시작 (이전 상태=idle) ===
목표: ...
[사이클 1] [1/12] planner (claude_cli) ... 시작
[사이클 1] [1/12] planner (claude_cli) 완료 6.9s | task=...
[사이클 1] [2/12] builder (claude_cli) ... 시작
[사이클 1] [2/12] builder (claude_cli) 완료 12.6s
[사이클 1] [3/12] verifier_functional (codex_cli) ... 시작
[사이클 1] [3/12] verifier_functional (codex_cli) 완료 23.0s | result=pass score=1.00
[사이클 1] [4/12] verifier_human (codex_cli) ... 시작
[사이클 1] [4/12] verifier_human (codex_cli) 완료 32.1s | result=pass score=1.00
[사이클 1] [5/12] orchestrator (codex_cli) ... 시작
[사이클 1] [5/12] orchestrator (codex_cli) 완료 4.2s | decision=complete_cycle
=== 사이클 1 완료: 결정=complete_cycle 다음 상태=completed ===
```

단계 수 `[N/12]` 의 분모는 하드코딩된 단계 총량이 아니라 **한 사이클 안의 최대 발화 수 상한** 입니다 (Phase 2 P1-5-B 부터). 엔진은 `next_speaker` 지명과 `declare_done → orchestrator 강제 호출` 두 규칙만 따라 다음 화자를 고르며, legacy 체인(planner → builder → verifier_f → verifier_h → orchestrator) 은 adapter 가 utterance.v1 메타를 반환하지 않았을 때의 fallback 입니다.

---

## 상태 머신

```
idle → planning → building → verifying_functional → verifying_human →
  orchestrator →
  { complete_cycle → completed
  | needs_iteration → iterating → (다음 run-cycle 에서 다시 planning)
  | blocked }

verifier_human 이 handoff 모드면:
  ... → verifying_functional → handoff_active → (외부 도구가 응답 작성) →
        handoff-ingest → 결과에 따라 completed / iterating / blocked
```

위 체인은 legacy fallback 순서입니다. Phase 2 P1-5-B 부터 엔진은 각 화자의 utterance.v1 이 지정한 `next_speaker` 를 우선 따르며, `declare_done=true` 선언 직후에는 orchestrator 를 강제 호출합니다. 화자 전환 규칙은 그 두 가지뿐이고, 나머지는 LLM 자율 판단.

주요 정책:

- `RESUMABLE_STATES = {idle, iterating, completed}`: 이 상태에서만 새 사이클 진입 허용
- 사이클 종료 판정은 orchestrator LLM 단일 책임 (Phase 2 P1-5-A 부터 규칙 기반 `max_cycles` / `stop_on_stagnation` escalation 제거)
- 한 사이클 안의 최대 발화 수 `cycle_safety.max_utterances_per_cycle` (기본 100, pathological loop 차단용 큰 안전선; 이전 12 hard cap 은 D5 disagree 재개 흐름을 일찍 끊는 문제로 P0-R 3 에서 100 으로 완화)
- D10 disagree 경고 `cycle_safety.max_consecutive_disagrees` (기본 7): orchestrator 가 같은 cycle 안에서 N회 연속 disagree 시 stdout + `events.jsonl` 에 1회 경고. 엔진은 중단하지 않고 max_utterances 까지 계속 진행 (사용자 stop 이 최종 방어선).
- `handoff_pause_count`: 세션이 handoff 로 일시정지한 횟수 (세션 텔레메트리용 누적 메트릭)
- handoff 응답의 `findings` / `recommended_next_action`은 다음 iterating 사이클의 planner context에 자동 주입

---

## 도메인 팩

`domains/` 아래 5개 팩이 v0.3.1 (D13 v3 슬림화) 상태입니다. 각 팩은 5 역할 자연어 가이드 (`guides/<role>.md`) 로 도메인 지혜를 prompt 에 흘립니다 (`adapters/base.py._render_prompt`). 도메인 자체는 **장르 보편 평가축만** 두고, 프로젝트 자산 (페르소나·로스터·폴더 컨벤션·writing-principles 등) 은 "프로젝트가 정의했으면 그것이 우선" hook 패턴으로 흡수합니다.

| 도메인 | 장르 보편 평가축 |
|---|---|
| `web` | 시맨틱 HTML / WCAG AA / Core Web Vitals / Lighthouse / viewport·lang·alt·heading |
| `unity` | build / MonoBehaviour 라이프사이클 / render pipeline / .meta GUID / WebGL·mobile·console 제약 / Addressables / Localization / GC alloc |
| `novel` | Show-don't-tell / 사이다 cadence / 매 화 훅 / 모바일 가독성 / 떡밥 균형 / 시점 일관성 |
| `music_video` | 스테이지 게이트 / 보컬 feasibility / 정서 일관성 / AI 도구 metadata / 음절-멜로디 정합 / 시각 언어 |
| `investment_research` | 1차 출처 ≥2 / 인과 vs narrative / 반증 ≥3 / exit condition + EV / 밸류에이션 멀티플 / bias 경고 / real-time 거래 추천 금지 |

새 도메인 팩을 만들려면 `domains/<도메인>/domain.yaml` (meta 만) + `domains/<도메인>/guides/<role>.md` 5개를 두면 됩니다. `rubric_coherence_smoke` 가 G1~G4 (meta·가이드 존재·최소 라인·헤더) 를 검사합니다.

> **주의**: 도메인 yaml 은 `meta` 블록만 사용합니다. `scoring.weights` / `scoring.thresholds` / `limits.cycle_limits.max_cycles` / `limits.auto_stop_rules.stop_on_stagnation` 같은 구조화 값은 D13 v3 (20차) 에서 폐기되었습니다. 사이클 종료 판정 전권은 orchestrator LLM 에 있습니다.

---

## handoff 모드 (파일 기반 외부 도구 연동)

`workflow.yaml`의 `human_review_mode: handoff`로 전환하면 `verifier_human` 차례에 adapter 호출 대신:

1. 엔진이 `.orch/handoff/request.yaml` 작성
2. 세션을 `handoff_active`로 전환하고 사이클 종료
3. 외부 도구(Codex App 등)가 `request.yaml`을 보고 `.orch/handoff/response.yaml` 작성
4. `handoff-ingest`로 재개 — 응답의 `result`(approved/changes_made/replan_needed/blocked/rejected)에 따라 사이클 분기

상세 규약: `docs/adapter-contract.md` 및 프로젝트 상위 `codex-app-handoff-protocol.md`.

---

## 테스트

회귀 스모크 (엔진 내부 로직만 검증, 실제 AI 호출 없음). 실행 시간 ~5초.

```bash
python -m tools.launcher_smoke          # 런처 메뉴 흐름
python -m tools.cycle_e2e_smoke         # ScriptedAdapter로 run-cycle 전체 흐름
python -m tools.orchestrator_smoke      # orchestrator feedback loop
python -m tools.quota_smoke             # quota-aware wait-and-resume
python -m tools.timeline_smoke          # utterance.v1 timeline append
python -m tools.arbitration_smoke       # orchestrator decision → next_speaker 라우팅
python -m tools.schema_utils_smoke      # utterance.v1 balanced-brace scanner / invariants
python -m tools.domain_validity_smoke   # 도메인 팩 validator 골든 샘플
python -m tools.rubric_coherence_smoke  # 도메인 meta·가이드 정합성
```

실제 AI로 완전 E2E를 돌리려면 프로젝트 루트에서 `tools/adapter_probe.py --live`를 참조.

---

## 요구 사항

- Python 3.11+
- Claude CLI (`claude --version`)
- Codex CLI (`codex --version`)
- Windows / macOS / Linux (현재 주 테스트 환경은 Windows 11)

---

## 폴더 구조

```
orch-engine/
├─ core/              # 엔진 본체 (app.py, state_machine, handoff_manager, runtime_store, ...)
├─ adapters/          # Claude CLI / Codex CLI adapter 구현
├─ launcher/          # 메뉴형 런처 (app.py) + Windows wrapper (start.ps1/bat)
├─ domains/           # 5개 도메인 팩 + common + schema
├─ schemas/           # role 및 adapter invocation JSON schema
├─ templates/         # 타겟 프로젝트의 `.orch/` 템플릿
├─ tools/             # 회귀 스모크 3종 + adapter probe + 기타 유틸
├─ docs/              # adapter-contract.md
├─ yaml.py            # JSON-backed 경량 YAML shim (표준 yaml 미설치 환경 대비)
└─ pyproject.toml
```

---

## 라이선스

MIT License. 자세한 내용은 [LICENSE](LICENSE) 참조.

Copyright (c) 2026 Park JeKwan
