# LLM validity smoke — 프레임워크 사용법

정적 체커가 잡을 수 없는 **의미론·런타임 갭** (Lighthouse 점수 추정, vocal
feasibility, exit condition 누락, plot regression, compile error 판정) 을 실제
LLM verifier 에 돌리는 도구. 비용이 들기 때문에 회귀 스위트에는 기본 편입하지
않고 수동 실행한다.

- 구현: `tools/llm_validity_smoke.py`
- 샘플 위치: `domains/<domain>/validation/llm_samples/<sample_id>/`

## 샘플 포맷

```
domains/<domain>/validation/llm_samples/<sample_id>/
    expected.json
    artifact/
        <LLM 이 검토할 파일들>
```

### expected.json

| 필드 | 필수 | 설명 |
|---|---|---|
| `domain` | ✅ | 도메인 ID (폴더 위치와 일치) |
| `label` | ✅ | `pass` 또는 `fail` — ground truth |
| `rubric_focus` | ✅ | 평가 축 이름 (예: `lighthouse_performance`, `vocal_feasibility`, `plot_consistency`). domain.yaml 의 `scoring.dimensions` 에 나오는 값 권장. |
| `expected_keywords` | ⬜ | LLM 응답의 `findings + evidence + blocking_issues + suggested_actions` 텍스트에 부분 매칭돼야 할 키워드 목록. |
| `expected_score_range` | ⬜ | `[lo, hi]` — LLM 응답 `score` 가 이 범위 내에 있어야 함. |
| `notes` | ⬜ | 사람용 메모. |

## 실행

```bash
# dry-run (기본) — scripted fake adapter 로 code path 만 검증, 비용 없음
python -m tools.llm_validity_smoke
python -m tools.llm_validity_smoke --only web

# live — 실제 LLM 호출 (비용 발생)
python -m tools.llm_validity_smoke --live --provider claude --confirm-cost
python -m tools.llm_validity_smoke --live --provider codex --confirm-cost --only unity
```

`--live` 는 `--confirm-cost` 없이는 rc=2 로 거부된다 (실수 방지 가드).

## 판정 로직

한 샘플이 PASS 되려면 세 조건 모두 충족:

1. **label_aligned**: LLM 의 `result` 가 expected.label 과 방향 일치
   - `label=pass` → LLM result == `pass`
   - `label=fail` → LLM result ∈ {`fail`, `needs_iteration`, `block`}
2. **score_in_range**: `expected_score_range` 가 있으면 LLM score 가 그 범위에 있음
3. **keywords_covered**: `expected_keywords` 가 있으면 모든 키워드가 응답 텍스트에 부분 매칭됨

셋 중 하나라도 틀리면 샘플 FAIL → smoke rc=1.

## dry-run 응답 생성 규약

`_scripted_response(expected)` 가 expected.json 을 보고 "의도된" 응답을 만든다:
- `label=pass` → result=`pass`, score = mid(score_range) or 0.9
- `label=fail` → result=`needs_iteration`, score = mid or 0.4
- findings/evidence 에 rubric_focus 와 expected_keywords 가 포함되도록 문자열
  삽입

즉 dry-run 은 **비교 로직·스키마·prompt 조립 경로 검증용**이지 LLM 의 실제
판단력 검증이 아니다. 실제 평가는 `--live` 로.

## 향후

- `_live_response` 는 현재 adapter import 만 되어 있고 invoke 경로가 간단한
  placeholder. 프롬프트를 context 필드로 직접 넣는 구조 — 실제 verifier 포맷
  (invocation schema 맞춰 domain 팩 rubric 주입) 과 정합이 필요하면 엔진의
  `_run_verifier_role` 내부 로직을 참조해 프롬프트 구성 세부를 교체할 것.
- dry-run 통과 == 포맷이 올바르다. 실제 LLM 판단력 증거는 `--live` 결과를
  `counter_examples/` 혹은 `human_judgments/` 로 축적해 교차 검증.
