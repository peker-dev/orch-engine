# Domain validity golden set (unity)

`tools/domain_validity_smoke.py`가 이 폴더의 샘플을 돌려 unity 도메인 팩의
`verify_functional` hard-fail 규칙 중 정적 검사 가능분이 실제로 식별 가능한지
검증합니다.

## Rubric 커버리지 매트릭스

| domain.yaml 규칙 (출처) | Checker violation ID | 샘플로 실증됨 |
|---|---|---|
| `scoring.blocking_failures`: Unity 버전 불일치 | `project_version_missing` (근사) | `fail_03_no_project_version` |
| `scoring.blocking_failures`: WebGL 비호환 API 사용 | `webgl_incompat_api` | `fail_01_webgl_incompat` |
| `scoring.blocking_failures`: Missing Script / Reference | `missing_script_reference` | `fail_02_missing_script` |
| `scoring.blocking_failures`: 컴파일 에러 | — | **범위 외** (Unity/C# 컴파일러 실행 필요) |
| `scoring.blocking_failures`: Build 실패 (대상 플랫폼) | — | **범위 외** (Unity headless build 필요) |
| `scoring.blocking_failures`: 런타임 Null/Exception | — | **범위 외** (PlayMode 실행 필요) |

### 정적 검사 상세

- **`project_version_missing`**: `ProjectSettings/ProjectVersion.txt` 파일 부재. 실제
  "버전 불일치"의 근사치. 엄밀한 mismatch는 외부 런타임 기준 필요.
- **`webgl_incompat_api`**: `.cs` 파일에서 WebGL 런타임에서 동작하지 않는 API 호출을 grep.
  금칙 목록: `File.WriteAllText`, `File.WriteAllBytes`, `File.AppendAllText`,
  `new Thread(`, `Task.Run(`.
- **`missing_script_reference`**: `.prefab` / `.unity` / `.asset` YAML에서
  `m_Script: {fileID: 0` 센티넬 발견. Unity가 missing 참조를 이렇게 직렬화.

### 알려진 갭

- **컴파일 에러**, **Build 실패**, **런타임 Exception**은 Unity 에디터 또는 `-batchmode`
  CLI 실행이 필요해 정적 검사 불가. 장기적으로 `tools/unity_build_runner.py` 래퍼 필요.
- `webgl_incompat_api` 금칙 목록은 보수적 샘플링. 프로젝트가 WebGL 타겟이 아니면 false
  positive일 수 있어 향후 `target_platforms` 컨텍스트와 교차 검증 필요.
- `project_version_missing`은 단순 존재 체크. 실제 버전 호환 여부는 LTS 매트릭스 대조가
  필요.

## Counter-examples (optional)

`validation/counter_examples/` 폴더에 실 사용 중 확인된 오탐(false_positive) /
미탐(false_negative) 샘플을 쌓아 체커 튜닝 숙제로 추적할 수 있습니다.
포맷·라벨 규약·smoke 판정은 [`docs/counter-examples.md`](../../../docs/counter-examples.md)
참조. 폴더가 없으면 smoke는 해당 섹션을 조용히 skip합니다.
