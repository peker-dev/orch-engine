# Runners (Option C — external standard runner)

이 디렉터리는 **LLM 이 아니라 코드가 직접 외부 프로그램을 돌리는 검수자** 들을 둡니다. 유니티 batchmode, 라이트하우스 CLI, 자체 테스트 하니스 같은 게 여기 옵니다.

## 언제 쓰는가

도메인이 검수에 결정적인 외부 도구가 필요할 때. 예를 들어 "유니티 빌드가 실제로 켜지고 PlayMode 가 통과했는가" 같은 검증은 LLM 이 sandbox 안에서 하기 무거우므로, 엔진이 직접 spawn 하는 runner 로 분리합니다.

배경 설계는 `memory/option-c-notes.md` 참조.

## 새 runner 추가 절차

1. `runners/<name>.py` 새 모듈 생성. `BaseRunnerAdapter` 상속한 클래스 + `provider_id = "<name>"` + `RUNNER = <Class>()` 모듈 레벨 인스턴스.
2. 도메인 측: `domains/<id>/roles.yaml` 의 entry 에 `default_provider: <name>` + `next_speaker_default: <다음 발언자 id>` 명시.
3. 작업 폴더 측: `.orch/config/roles.yaml` 의 매핑값을 `<name>` 으로.
4. `tools/cycle_e2e_smoke.py` 시나리오 1개 추가 권장.

## 인터페이스 요약

```python
from runners.base import BaseRunnerAdapter, RunnerResult


class MyRunner(BaseRunnerAdapter):
    provider_id = "my_runner"
    default_timeout_sec = 60

    def run(self, invocation):
        # 외부 프로그램 spawn + 결과 회수.
        # exit_code 0 = pass, 그 외 = fail (verdict 로 명시 override 가능).
        return RunnerResult(
            exit_code=0,
            summary="my runner passed",
            stdout_excerpt="...",
            artifact_paths=[".orch/runtime/my_log.txt"],
        )


RUNNER = MyRunner()
```

엔진은 `BaseRunnerAdapter.invoke()` 가 utterance.v1 envelope 자동 합성. 한 발화 안에서 결과가 verifier_functional 형태(result/score/findings/evidence/blocking_issues/suggested_actions)로 흘러 들어갑니다.

## 예약 provider 이름

`claude_cli`, `codex_cli`, `codex_app` 은 LLM CLI adapter 전용. runner 모듈 이름으로 쓰면 충돌해서 엔진이 명시 거부합니다.

## 라이브 검증 절차 (박제관 PC, unity_batchmode 처음 실행)

옵션 C 의 sandbox 회귀 (dry-run 시나리오) 와 별도로, 박제관님 PC 에서 실제 유니티가 켜지고 실행되는지 한 번 검증해야 옵션 A (`416279a` codex sandbox 격상) 되돌림으로 넘어갈 수 있어요. 처음 실행 절차는 다음과 같습니다.

### 1) 도메인 측 설정

작업할 유니티 도메인의 `domains/<domain_id>/roles.yaml` 에 검수자 추가 (없으면 신설):

```yaml
roles:
  - id: verifier_unity_play
    family: verifier
    display: 유니티 PlayMode 검수자
    default_provider: unity_batchmode
    next_speaker_default: verifier_human
    runner_config:
      unity_executable: "C:/Program Files/Unity/Hub/Editor/<버전>/Editor/Unity.exe"
      unity_method: "<클래스>.<메서드>"   # 예: "OrchSmoke.RunPlay"
      # project_subpath: "<상대 경로>"  # 옵션 — working dir 자체이면 생략
      # extra_args: ["-customArg", "value"]
      # timeout_sec: 600  # 옵션, 기본 600초
```

`unity_executable` 은 절대 경로 또는 PATH 안 이름. 미설정 시 환경변수 `UNITY_EDITOR_PATH` 사용. 둘 다 없으면 dry-run 으로 자동 분기.

### 2) 작업 폴더 측 설정

타겟 프로젝트의 `.orch/config/roles.yaml` 매핑에 한 줄 추가:

```json
{
  "roles": {
    "planner": "claude_cli",
    "builder": "claude_cli",
    "verifier_functional": "codex_cli",
    "verifier_unity_play": "unity_batchmode",
    "verifier_human": "codex_cli",
    "orchestrator": "codex_cli"
  }
}
```

### 3) Unity 측 메서드 준비

`runner_config.unity_method` 가 가리키는 정적 메서드가 유니티 프로젝트 안에 있어야 합니다. 가장 단순한 형태:

```csharp
using UnityEditor;
using UnityEngine;

public static class OrchSmoke {
    public static void RunPlay() {
        Debug.Log("OrchSmoke: hello from batchmode");
        EditorApplication.Exit(0);  // 0 = 성공, 0 외 = 실패
    }
}
```

### 4) 실행

```bash
cd "<OneDrive>/#작업/2026_AI작업/오케스트레이션/orch-engine"
python -m core.app run-cycle --target "<유니티 프로젝트 경로>"
```

### 5) 성공 판정 기준

- `rc=0` 으로 사이클 종료.
- `.orch/reviews/verifier_unity_play_latest.json` 의 `result == "pass"`.
- `.orch/runtime/unity_logs/unity_<timestamp>_verifier_unity_play.log` 안에 `Exiting batchmode successfully` (또는 `Exiting Unity successfully`).
- timeline 의 verifier_unity_play 발화가 다음 발언자 (verifier_human 또는 orchestrator) 까지 정상 라우팅.

### 6) 실패 시 대응

- 로그 파일에 컴파일 에러 / 예외 / abort 마커 — 메서드 코드 또는 유니티 프로젝트 자체 문제. 박제관님 측에서 수정.
- `result="fail"` 인데 로그에 success marker 가 보임 — `runners/unity_log_parser.py` 의 패턴이 박제관 환경 출력과 어긋남. 패턴을 박제관님 환경에 맞춰 보강.
- subprocess 시동 자체가 실패 (`unity executable not found` 같은 fatal) — 절대 경로 / PATH / `UNITY_EDITOR_PATH` 재확인.
- timeout — `runner_config.timeout_sec` 늘리거나 메서드 작업량 줄이기.

라이브 검증 통과되면 옵션 A 격상 되돌림 작업 (다음 stride) 으로 진입할 수 있어요.

---

## 참조 구현

`echo_runner.py` — 외부 의존성 없이 invocation 을 그대로 echo 하는 reference runner. 인터페이스 회귀 검증용.

`unity_batchmode.py` — 옵션 C 다음 stride. Unity 를 batchmode 로 spawn 하고 로그 파일을 회수해 verdict 산출. Unity 미설치 환경에서는 `dry_run` 모드로 인자만 빌드 + fake pass — 박제관 PC 라이브 검증과 sandbox 회귀를 분리. 도메인 `roles.yaml` entry 의 `runner_config` 자유 dict 에서 다음 키 읽음:
- `unity_executable` (절대 경로 또는 PATH 안 이름; 미설정 시 `UNITY_EDITOR_PATH` 환경변수, 둘 다 없으면 dry-run).
- `unity_method` (필수, executeMethod 인자).
- `project_subpath` (옵션).
- `extra_args` (옵션, list[str]).
- `timeout_sec` (옵션, 기본 600).
