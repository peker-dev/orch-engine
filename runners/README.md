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

## 참조 구현

`echo_runner.py` — 외부 의존성 없이 invocation 을 그대로 echo 하는 reference runner. 인터페이스 회귀 검증용.
