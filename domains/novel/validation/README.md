# Domain validity golden set (novel)

`tools/domain_validity_smoke.py`가 이 폴더의 샘플을 돌려 novel 도메인 팩의
`verify_functional` hard-fail 규칙 중 정적 검사 가능분이 실제로 식별 가능한지
검증합니다.

## Rubric 커버리지 매트릭스

| domain.yaml 규칙 (출처) | Checker violation ID | 샘플로 실증됨 |
|---|---|---|
| `scoring.blocking_failures`: banned emotion adjective | `banned_emotion_adjective` | `fail_01_emotion_word` |
| `scoring.blocking_failures`: paragraph > 3 lines | `paragraph_too_long` | `fail_02_paragraph_long` |
| `verify_functional.pass_fail_rules`: abstract phrasing | `abstract_phrasing` | `fail_03_abstract_phrasing` |
| `scoring.blocking_failures`: emphasis marker misuse | — | **범위 외** (규약 분기 복잡, 향후 확장) |
| `scoring.blocking_failures`: worldbuilding block dump | — | `paragraph_too_long`이 부분 커버 |
| `scoring.blocking_failures`: confirmed plot point regressed | — | **범위 외** (LLM 의미론 판단) |
| `verify_functional.pass_fail_rules`: character/ability name conflict | — | **범위 외** (LLM 의미론 매칭) |

### 정적 검사 상세

- **`banned_emotion_adjective`**: 금칙 목록(비참하다/슬프다/놀랍다/기뻤다 등) 중 하나라도
  원고 파일에 발견되면 위반. Show-don't-tell 원칙.
- **`paragraph_too_long`**: 원고 본문에서 빈 줄로 구분된 문단 중 비공백·비헤더 줄이
  3개를 초과하면 위반. "1 sentence per line, max 3 lines per paragraph" 준수.
- **`abstract_phrasing`**: 특정 추상 표현 금칙(눈을 읽지 않았다 / 생각하는 것 같은 눈)
  발견 시 위반. 체크 범위: `원고/` 이하 `.md` 파일만.

### 알려진 갭

- **`emphasis_marker_misuse`**: `[...]` 시스템 메시지 / `『...』` 상태·스킬 / `*...*`
  내적 독백 규약을 정적으로 판정하려면 문맥 분류가 필요. 향후 `writing-principles.md`
  rule-extraction 보강.
- **플롯 회귀**, **세계관 충돌**, **캐릭터 명·능력명 정합성**은 `설정/*.md`과의 의미론적
  비교가 필수라 LLM 기반 검증 단계 필요.
- 금칙 어휘 목록은 대표 6개만 등록. 프로젝트별 `writing-principles.md`에서
  확장하는 것이 정석.

## Counter-examples (optional)

`validation/counter_examples/` 폴더에 실 사용 중 확인된 오탐(false_positive) /
미탐(false_negative) 샘플을 쌓아 체커 튜닝 숙제로 추적할 수 있습니다.
포맷·라벨 규약·smoke 판정은 [`docs/counter-examples.md`](../../../docs/counter-examples.md)
참조. 폴더가 없으면 smoke는 해당 섹션을 조용히 skip합니다.
