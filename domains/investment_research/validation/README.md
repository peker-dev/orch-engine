# Domain validity golden set (investment_research)

`tools/domain_validity_smoke.py`가 이 폴더의 샘플을 돌려 investment_research
도메인 팩의 `verify_functional` hard-fail 규칙이 실제로 식별 가능한지 검증합니다.

## Rubric 커버리지 매트릭스

| domain.yaml 규칙 (출처) | Checker violation ID | 샘플로 실증됨 |
|---|---|---|
| `verify_functional.pass_fail_rules`: 6 섹션 누락 | `missing_report_section` | `fail_01_missing_section` |
| `scoring.blocking_failures`: emotional qualifier detected | `emotional_qualifier` | `fail_02_emotional` |
| `scoring.blocking_failures`: antithesis count <3 | `antithesis_insufficient` | `fail_03_antithesis_short` |
| `scoring.blocking_failures`: independent source <2 | `sources_insufficient` | `fail_04_sources_short` |
| `verify_functional.pass_fail_rules`: buy recommendation lacks exit condition | — | **범위 외** (의미론적 판단, LLM 필요) |

### 정적 검사 상세

- **`missing_report_section`**: 6개 헤더 문자열(포착된 신호 / 인과관계 / 종목 분석 /
  투자 분류 / 리스크 & 반례 / 최종 판단)이 보고서에 모두 존재해야 함.
- **`emotional_qualifier`**: 금칙어 리스트(유망/훌륭/완벽/great/promising/amazing) 중
  하나라도 발견되면 위반. (좋다는 일상 어휘라 우선 제외 — false positive 높음)
- **`antithesis_insufficient`**: `리스크 & 반례` 섹션 내 bullet(`-` 또는 `*`) 항목이
  3개 미만이면 위반.
- **`sources_insufficient`**: 보고서 전체 URL 개수가 2개 미만이면 위반. (per-signal
  매핑은 의미론 분석 필요 → 현재는 전체 카운트 근사치)

### 알려진 갭

- **`exit_condition_missing`**: "매수" 추천의 출구 조건 유무는 문맥 파싱이 필요.
  단순 문자열 매칭은 오탐률 높아 LLM 기반 판정 단계에서 처리.
- **감정 금칙어 보조 후보** (좋다/promising 유사어)는 false positive 우려로 보수적 리스트만
  사용. 확장 필요 시 프로젝트별 blocklist로 오버라이드 권장.
- 보고서 파일명은 `YYYY-MM-DD_*.md` 패턴만 인식. 다른 패턴은 향후 확장.

## Counter-examples (optional)

`validation/counter_examples/` 폴더에 실 사용 중 확인된 오탐(false_positive) /
미탐(false_negative) 샘플을 쌓아 체커 튜닝 숙제로 추적할 수 있습니다.
포맷·라벨 규약·smoke 판정은 [`docs/counter-examples.md`](../../../docs/counter-examples.md)
참조. 폴더가 없으면 smoke는 해당 섹션을 조용히 skip합니다.
