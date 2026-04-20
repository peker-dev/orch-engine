# Domain validity golden set (web)

`tools/domain_validity_smoke.py`가 이 폴더의 샘플을 돌려 web 도메인 팩의
`verify_functional` hard-fail 규칙이 실제로 식별 가능한지 검증합니다.

## 구조

```
golden/
  <sample_id>/
    index.html        # 샘플 페이지 (다중 파일 가능)
    expected.json     # { domain, label: pass|fail, expected_violations: [...] }
```

## Rubric 커버리지 매트릭스

web `domain.yaml`의 hard-fail 규칙 → checker violation ID → 샘플 증명 여부.

| domain.yaml 규칙 (출처) | Checker violation ID | 샘플로 실증됨 |
|---|---|---|
| `scoring.blocking_failures`: meta viewport 누락 | `viewport_missing` | `fail_01_missing_viewport` |
| `scoring.blocking_failures`: html lang 누락 | `lang_missing` | `fail_03_missing_lang` |
| `scoring.blocking_failures`: img alt 누락 | `img_alt_missing` | `fail_02_missing_alt` |
| `scoring.blocking_failures`: h1 2개 이상 또는 누락 | `h1_count_invalid` | `fail_04_no_h1` + `fail_05_two_h1` |
| `scoring.blocking_failures`: HTML 파싱 에러 | `html_parse_error` | **갭** (아래 참조) |
| `scoring.blocking_failures`: 반응형 증거 없음 | `responsive_evidence_missing` | `fail_06_no_responsive` |
| `scoring.blocking_failures`: WCAG critical 위반 | — | **범위 외** (LLM/툴 필요) |
| `verify_functional`: 내부 링크 유효 | — | **미구현** (향후 확장) |
| `verify_functional`: 외부 CDN 승인 | — | **미구현** (향후 확장) |
| `verify_functional`: h2 이하 heading 계층 | — | **미구현** (향후 확장) |
| `verify_functional`: Lighthouse 점수 / 대비비 / 감정 수식어 | — | **범위 외** (soft fail, LLM 판단) |

### 알려진 갭

- **`html_parse_error`**: Python `html.parser`는 관용적이라 대부분의 오표기를 그냥 통과시킴.
  진짜 파싱 검증은 W3C validator CLI를 불러야 하며 현재 정적 체커는 severe 예외만 잡음.
  장기적으로는 `tools/html_validator_adapter.py` 같은 래퍼로 확장 권장.
- **Soft fail** (Lighthouse, WCAG 대비비, 감정 수식어)은 정적 체커 범위 외. LLM 기반
  verifier가 담당하며, 향후 별도 "LLM validity smoke"에서 교차 검증 예정.

## 샘플 추가 규칙

- pass 샘플: `expected_violations: []`
- fail 샘플: 예상되는 violation ID를 모두 나열 (검사기가 추가 violation을 반환해도 OK,
  단 expected 목록은 서브셋이어야 함)
- 샘플 폴더명은 의도를 반영 (`fail_XX_<reason>` 권장)

## Counter-examples (optional)

`validation/counter_examples/` 폴더에 실 사용 중 확인된 오탐(false_positive) /
미탐(false_negative) 샘플을 쌓아 체커 튜닝 숙제로 추적할 수 있습니다.
포맷·라벨 규약·smoke 판정은 [`docs/counter-examples.md`](../../../docs/counter-examples.md)
참조. 폴더가 없으면 smoke는 해당 섹션을 조용히 skip합니다.
