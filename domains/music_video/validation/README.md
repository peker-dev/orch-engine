# Domain validity golden set (music_video)

`tools/domain_validity_smoke.py`가 이 폴더의 샘플을 돌려 music_video 도메인 팩의
`verify_functional` hard-fail 규칙이 실제로 식별 가능한지 검증합니다.

## Rubric 커버리지 매트릭스

| domain.yaml 규칙 (출처) | Checker violation ID | 샘플로 실증됨 |
|---|---|---|
| `verify_functional.pass_fail_rules`: cross-stage file placement | `cross_stage_file_placement` | `fail_01_cross_stage` |
| `verify_functional.pass_fail_rules`: persona missing from meeting | `persona_coverage_missing` | `fail_02_missing_persona` |
| `verify_functional.pass_fail_rules`: confirmed setting silently changed | `confirmed_setting_drift` | `fail_03_genre_drift` |
| `scoring.blocking_failures`: vocal infeasible for PD | — | **범위 외** (LLM/사람 판단) |
| `scoring.blocking_failures`: consent-less voice cloning source | — | **범위 외** (승인 로그 메타검사 필요) |

### 정적 검사 상세

- **`cross_stage_file_placement`**: 파일명 패턴 ↔ 지정 스테이지 폴더 매핑.
  예: `suno_prompt_*` → `04_작곡/`, `*_lyrics_*` → `03_작사/`, `mv_storyboard_*` → `05_뮤직비디오/`.
- **`persona_coverage_missing`**: `meeting_*` 또는 파일명에 `회의`가 들어간 `.md`는
  5 페르소나(서정아 / 한비트 / 윤프로 / 채원 / 민수) 전부 언급해야 함.
- **`confirmed_setting_drift`**: `memory/project-overview.md`가 선언한 장르와
  다른 스테이지 파일의 장르 선언이 불일치하면 위반.

### 알려진 갭

- **`vocal_infeasible`**: PD의 음역·난이도 판단이 필요해 정적 검사 불가. LLM 기반 검증 단계 필요.
- **`consent-less_voice_cloning`**: 승인 로그 메타데이터 검사가 필요하며 현재 스코프 외.
- **Theme drift**: 장르 외 "테마" 정합성은 의미론 비교라 정적으로는 근사치만 가능. 향후 확장.

## Counter-examples (optional)

`validation/counter_examples/` 폴더에 실 사용 중 확인된 오탐(false_positive) /
미탐(false_negative) 샘플을 쌓아 체커 튜닝 숙제로 추적할 수 있습니다.
포맷·라벨 규약·smoke 판정은 [`docs/counter-examples.md`](../../../docs/counter-examples.md)
참조. 폴더가 없으면 smoke는 해당 섹션을 조용히 skip합니다.
