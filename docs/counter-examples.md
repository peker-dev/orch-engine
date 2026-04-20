# Counter-example 관리 규약

`tools/domain_validity_smoke.py`가 `domains/<domain>/validation/counter_examples/`
아래 샘플을 golden set과 별도로 돌려, 체커가 **알려진 오탐/미탐 상태**를
유지하고 있는지 추적한다. LLM 호출 없이 정적 검증만 수행한다.

## 목적

Golden set은 "규칙이 의도대로 판별되는가"를 증명한다. Counter-example은 실
사용 중 튀어나온 **체커 vs 사실 판정의 불일치**를 박제해 다음 튜닝의 숙제로
남긴다. "지금은 이 정도가 한계"라는 것을 명시적으로 인정하고, 체커가 업그레
이드돼 해소되는 순간 smoke 로그로 즉시 알게 한다.

## 폴더 배치

```
domains/<domain>/validation/counter_examples/
  <label>_<seq>_<reason>/
    expected.json
    ...domain files (체커가 스캔)
```

폴더명 권장 포맷: `false_positive_01_contrast_miss` 또는
`false_negative_02_hierarchy_skip` 처럼 label + 순번 + 원인 요약.

폴더가 없거나 비어 있으면 smoke가 counter-example 섹션을 조용히 skip한다.

## `expected.json` 필드

| 필드 | 필수 | 설명 |
|---|---|---|
| `domain` | ✅ | 도메인 ID (web / unity / novel / music_video / investment_research). 폴더 위치와 일치해야 함. |
| `label` | ✅ | `false_positive` 또는 `false_negative` |
| `expected_violations` | ✅ | ground truth. 사람이 판정한 옳은 violation ID 리스트. FP면 `[]`, FN이면 실제 위반 ID들. |
| `observed_violations` | ✅ | 카운터예시 수집 시점에 체커가 실제로 낸 violation ID 리스트. 증거 역할. FP면 당시 체커가 낸 것들, FN면 `[]`. |
| `note` | ✅ | 왜 이게 카운터예시인지 한 줄 설명. 박제관이 나중에 튜닝할 때 참고. |
| `discovered_at` | ✅ | YYYY-MM-DD. 수집 시점. |
| `source` | 권장 | 출처 (`session_log`, `박제관_수동`, `external_report` 등). |

## 라벨 의미

### `false_positive` (체커가 오탐지)
- 사실: 결과물에 문제 없음. `expected_violations: []`.
- 수집 당시 체커: violation을 잘못 냄.

smoke 판정:
- **reproduced**: 체커가 여전히 observed_violations를 superset으로 냄 → 문제
  유지. `OK` 출력, rc 영향 없음.
- **RESOLVED**: 체커가 이제 clean 반환. 튜닝으로 해소됨. 샘플을 golden pass로
  옮기도록 권장 메시지 출력.
- **partial**: 체커 행동이 다른 방식으로 변함. 사람이 봐야 함. `OK` 출력 (rc 영향 없음).

### `false_negative` (체커가 미탐지)
- 사실: 결과물에 실제 위반 있음. `expected_violations: [ID, ...]`.
- 수집 당시 체커: clean 반환 (놓침).

smoke 판정:
- **reproduced**: 체커가 여전히 expected_violations 중 하나도 못 잡음.
- **RESOLVED**: 체커가 expected_violations 전부 잡음. golden fail로 승격 권장.
- **partial**: 일부만 잡음. 사람 확인 필요.

### 공통
- `expected.json` 누락/라벨 오타는 **config error → rc=1** (정비 요구).
- reproduced/resolved/partial은 rc에 영향 없음 (관측용).

## 수집 워크플로

1. 실제 사이클(`run-cycle`)을 돌리다 체커 판정과 사람 판정이 어긋나는 순간 포착.
2. 해당 빌더 산출물 사본을 `counter_examples/<label>_NN_<reason>/` 에 저장
   (필요한 도메인 파일만).
3. `expected.json` 작성 — 당시 체커 결과는 `observed_violations`, 사람 판정은
   `expected_violations`.
4. smoke 돌려 reproduced로 확인.
5. 나중에 체커 튜닝 시 smoke에서 RESOLVED 떴을 때 해당 폴더를 삭제하거나
   `validation/golden/` 으로 이동.

## RESOLVED 후 후속 작업

- `false_positive` RESOLVED → 해당 샘플을 `golden/pass_NN_<reason>/`로 이동,
  `expected.json`을 pass 포맷(`label: pass`, `expected_violations: []`)으로 갱신.
- `false_negative` RESOLVED → `golden/fail_NN_<reason>/`로 이동, `label: fail`
  + 원래의 `expected_violations` 유지.

이렇게 golden set이 시간이 지남에 따라 실제 사례로 확장되며, counter_examples는
미해결 숙제 모음으로 남는다.
