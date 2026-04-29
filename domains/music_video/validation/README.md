# Domain validity golden set (music_video)

`tools/domain_validity_smoke.py`가 이 폴더의 샘플을 돌려 music_video 도메인 팩의
`verify_functional` hard-fail 규칙이 실제로 식별 가능한지 검증합니다.

## Rubric 커버리지 매트릭스

장르 보편 평가축 중 **정적으로 판정 가능한** 항목만 fixture 로 박제. 페르소나/PD/리뷰 인원 같은 프로젝트 자산 차원 룰은 정적 fixture 가 아니라 프로젝트의 가이드 / `.orch/` 자료에 정의 (도메인 v3 hook 패턴).

| 평가축 | Checker violation ID | 샘플로 실증됨 |
|---|---|---|
| 스테이지 폴더 ↔ 파일 배치 정합 | `cross_stage_file_placement` | `fail_01_cross_stage` |
| 확정 장르가 후속 스테이지에서 무단 변경 | `confirmed_setting_drift` | `fail_03_genre_drift` |
| 보컬 feasibility (음역·난이도) | — | **범위 외** (LLM/사람 판단) |
| 보이스 클로닝 동의 / 출처 추적 | — | **범위 외** (승인 로그 메타검사 필요) |

### 정적 검사 상세

- **`cross_stage_file_placement`**: 파일명 패턴 ↔ 지정 스테이지 폴더 매핑.
  예: `suno_prompt_*` → `04_작곡/`, `*_lyrics_*` → `03_작사/`, `mv_storyboard_*` → `05_뮤직비디오/`.
- **`confirmed_setting_drift`**: `memory/project-overview.md`가 선언한 장르와
  다른 스테이지 파일의 장르 선언이 불일치하면 위반.

### 알려진 갭

- **`vocal_infeasible`**: 음역·난이도 판단이 필요해 정적 검사 불가. LLM 기반 검증 단계.
- **`consent-less_voice_cloning`**: 승인 로그 메타데이터 검사가 필요하며 현재 스코프 외.
- **Theme drift**: 장르 외 "테마" 정합성은 의미론 비교라 정적으로는 근사치만 가능. 향후 확장.

## Counter-examples (optional)

`validation/counter_examples/` 폴더에 실 사용 중 확인된 오탐(false_positive) /
미탐(false_negative) 샘플을 쌓아 체커 튜닝 숙제로 추적할 수 있습니다.
포맷·라벨 규약·smoke 판정은 [`docs/counter-examples.md`](../../../docs/counter-examples.md)
참조. 폴더가 없으면 smoke는 해당 섹션을 조용히 skip합니다.
