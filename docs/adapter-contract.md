# orch-engine adapter contract v1

## 1. 목적

이 문서는 `core -> adapter -> provider CLI` 경로를 고정하기 위한 첫 계약 문서입니다.

범위:
- `Claude CLI`
- `Codex CLI`
- 역할: `planner`, `builder`, `verifier_functional`, `verifier_human`, `orchestrator`

비범위:
- API/서버형 provider
- GUI handoff 자체 구현
- provider별 고급 tool policy 최적화

---

## 2. 이 문서의 현재 상태

이번 단계에서 이미 확정한 것:
- 엔진 내부 공통 invocation envelope
- 역할별 최종 JSON 응답 스키마
- provider별 비대화형 CLI 호출 방향
- raw stdout/stderr/result artifact 저장 위치
- timeout / retry / normalize 원칙

live probe로 확인 완료한 사실:
- `Claude CLI`의 실제 stdout wrapper shape (§2-1 참조)
- `Claude CLI`의 stdin 입력이 기본 경로로 동작함
- `Codex CLI`의 `-o` 출력 파일이 role payload에 가까운 객체를 직접 담음

엔진 계약과 provider 출력 래퍼의 세부 shape는 모두 코드에 반영 완료.
이 문서는 이후 hardening 과정에서의 변화를 누적하는 살아있는 계약서입니다.

---

## 2-1. live probe 관찰 결과

- `Claude CLI`
  - stdout은 wrapper JSON 한 개를 반환
  - 실제 role payload는 wrapper 내부 `result` 문자열에 JSON 문자열로 들어오는 경우가 확인됨
  - 따라서 adapter는 wrapper 직접 파싱 + `result` 문자열 파싱 둘 다 지원해야 함
- `Codex CLI`
  - `-o <result-file>` 출력 파일은 role payload에 가까운 JSON 객체를 직접 기록
  - stderr에는 plugin sync / featured plugin 관련 경고가 길게 출력될 수 있으나, 종료 코드 0이면 비치명으로 취급 가능
- 공통
  - provider가 schema를 완벽히 따르지 않는 경우가 있어 role별 normalize 단계가 필요함

---

## 3. 문서/토큰 원칙

- 큰 통합설계 문서를 adapter 구현 시 매번 읽지 않습니다.
- adapter 작업의 기본 진입 문서는 이 문서와 역할별 schema 파일입니다.
- provider prompt에는 필요한 파일 경로와 요약만 전달하고, 전체 설계 문서를 통째로 넣지 않습니다.

---

## 4. 엔진 내부 invocation contract

엔진은 adapter에게 아래 구조의 요청을 넘기는 것을 목표로 합니다.

파일:
- `schemas/adapter/invocation.v1.json`

핵심 필드:
- `version`
- `request_id`
- `provider`
- `role`
- `objective`
- `working_directory`
- `mode`
- `context_summary`
- `input_files`
- `write_scope`
- `output_schema_path`
- `timeout_sec`
- `token_budget`

메모:
- JSON envelope는 artifact 저장(`request.json`)과 외부 tool/probe 입력의 SSoT입니다.
- 코드 상의 `Invocation` dataclass는 런타임 최소형(`role / objective / working_directory / context`)으로 유지하고,
  envelope의 나머지 필드(`version`, `mode`, `input_files`, `write_scope`, `output_schema_path`, `timeout_sec` 등)는
  `BaseCliAdapter.invoke` 내부에서 schema/role 기본값으로 생성해 `request.json`에 기록합니다.
- envelope-dataclass 완전 동기화가 필요해질 경우 `Invocation`을 확장하지만, 지금은 MVP 목적상 최소형이 의도된 선택입니다.

---

## 5. 역할별 최종 JSON 응답 contract

모든 provider는 마지막 응답을 **마크다운 없이 JSON 객체 하나**로 반환해야 합니다.

공통 원칙:
- 바깥 envelope 에 fenced code block 금지 (utterance.v1 의 outer JSON 은 plain)
- body 내부에서는 markdown + 정확히 하나의 fenced ```json``` 블록 허용 (P1-5-C 부터)
- 확신이 낮더라도 유효한 JSON 을 우선 반환

공통 스키마:
- `schemas/utterance.v1.json` — 모든 역할의 wire 포맷 (P1-5-C 부터 단일 스키마)
- 각 역할의 구조화 데이터는 body 내부 fenced JSON 블록에서 엔진이 추출
  (`adapters/base.py._coerce_<role>_utterance_to_legacy`)

현재 코드와 직접 연결되는 최소 필드 (body 내부 fenced JSON):

### planner
- `summary`
- `plan_summary`
- `tasks[]` (empty array means no runnable work was found)

### builder
- `summary`
- `change_summary`
- `files_changed[]`

### verifier_functional
- `summary`
- `result`
- `score`
- `findings[]`
- `evidence[]`

### verifier_human
- `summary`
- `result`
- `score`
- `findings[]`

### orchestrator
- `summary`
- `decision` (`complete_cycle` / `needs_iteration` / `blocked`)
- `next_state` (`completed` / `iterating` / `blocked`)
- `reason`
- `unresolved_items[]`
- `recommended_next_action`

---

## 6. provider별 호출 계약

## 6-1. Claude CLI

help 기준 확인된 핵심 옵션:
- `-p`
- `--output-format json`
- `--json-schema`
- `--permission-mode`
- `--add-dir`

live probe 결과 반영 권장 호출 형태:

```text
<prompt via stdin> | claude -p --output-format json --json-schema <schema-json-string> --permission-mode <mode> --add-dir <working_directory>
```

`--permission-mode` role별 매핑 (현 구현 `adapters/claude_cli.py`):
- `planner`, `verifier_human`: `dontAsk`
- `builder`, `verifier_functional`: `bypassPermissions`

원칙:
- schema는 파일 경로가 아니라 JSON 문자열로 전달
- `Claude CLI`는 probe 기준 prompt argument 방식이 불안정했고, stdin 입력 경로를 기본으로 사용
- raw stdout은 반드시 파일로 저장 후 파싱
- builder/verifier_functional은 실제 파일 쓰기/명령 실행이 필요하므로 권한을 풀어주되, read-only 성격의 planner/verifier_human은 `dontAsk`로 제한

주의:
- `Claude CLI`는 `--output-format json`을 지원하지만, 실제 stdout wrapper shape는 probe로 확인해야 합니다.
- adapter normalizer는 wrapper가 있더라도 최종 JSON payload만 추출해야 합니다.
- live probe 기준 wrapper JSON 내부 `result` 문자열 파싱을 지원해야 합니다.

## 6-2. Codex CLI

help 기준 확인된 핵심 옵션:
- `exec`
- `-` 를 통한 stdin prompt 입력
- `--cd`
- `--skip-git-repo-check`
- `--output-schema <FILE>`
- `-o <FILE>`
- `--json`

권장 호출 형태:

```text
codex exec - --cd <working_directory> --skip-git-repo-check --sandbox <mode> --output-schema <schema-file> -o <result-file>
```

`--sandbox` role별 매핑 (현 구현 `adapters/codex_cli.py`):
- `planner`, `verifier_human`: `read-only`
- `builder`, `verifier_functional`: `workspace-write`

원칙:
- prompt는 stdin으로 전달
- 최종 메시지는 `-o` 파일에서 읽는 것을 기본 경로로 사용
- `Codex CLI`의 structured output schema는 object의 `properties`와 `required`를 엄격하게 맞춰야 함
- `--json`은 event stream 디버깅용일 때만 사용
- stderr 경고는 길 수 있으므로, 성공 판정은 종료 코드 + result 파일 + schema 검증 기준으로 봅니다.
- planner/verifier_human은 파일 수정 없이 판정만 하므로 `read-only`, 실행/수정이 필요한 builder/verifier_functional에만 `workspace-write` 권한을 허용합니다.

---

## 7. adapter normalize contract

provider 결과는 adapter에서 아래 형태로 normalize합니다.

```text
InvocationResult(
  status="ok" | "error",
  summary="<one-line summary>",
  payload={<role-specific parsed json>}
)
```

정규화 규칙:
- `summary`는 role JSON 내부 `summary`를 우선 사용
- `payload`는 role schema에 맞는 최종 JSON 객체
- provider wrapper/metadata는 `payload`에 그대로 넣지 않음
- raw stdout/stderr/path 정보는 artifact로 별도 저장

공유 유틸리티:
- schema 검증과 payload 후보 탐색 로직은 `tools/schema_utils.py`에 SSoT로 보관 (`validate_schema`, `find_payload_candidate`, `find_first_dict_candidate`).
- `adapters/base.py`와 `tools/adapter_probe.py`는 이 모듈만 호출하여 동일 동작을 보장합니다. base는 외부 caller 호환을 위해 얇은 re-export만 유지.

---

## 8. timeout / retry / error policy

초기 기본값:
- `planner`: 180초
- `builder`: 600초
- `verifier_functional`: 300초
- `verifier_human`: 240초
- `orchestrator`: 120초

retry 규칙:
- JSON parse 실패: 1회 재시도
- schema 불일치: 1회 재시도
- timeout: role별 1회 재시도 가능
- 명령어 없음 / 인증 실패 / 권한 실패: 재시도 없이 즉시 오류 처리 (`AdapterFatalError`)

현 구현의 에러 class 목록 (`adapters/base.py`가 `error.json`에 기록):
- `timeout`
- `non_zero_exit`
- `fatal_process_failure` (auth/missing binary/denied sandbox → 재시도 없음)
- `json_decode`
- `schema_missing_field`
- `schema_extra_field`
- `schema_type_mismatch`
- `payload_not_found`
- `missing_result_file`
- `parse_error`

재시도 시 추가 지시 (`_append_retry_prompt`):
- 직전 시도의 에러 메시지를 그대로 재시도 프롬프트에 삽입 (최대 400자)
- "Return one JSON object only."
- "Do not add commentary outside the schema."
- "Every required field must be present."
- "Fix the specific problem above in this attempt."

Codex stderr 처리:
- Codex CLI는 plugin-sync / featured-plugin 관련 비치명 경고를 stderr로 많이 출력합니다.
- stderr는 성공 판정의 입력이 아닙니다. 성공 판정은 `exit code 0 + result 파일 유효 + schema 통과` 기준입니다.
- 단 stderr에 인증 실패/샌드박스 거부 등 `FATAL_STDERR_MARKERS`가 포함되고 exit code도 0이 아니면 fatal로 승격합니다.

Token preflight 상태:
- 현 구현은 `allow / warn / block` 3상태입니다 (`core/app.py:_run_token_preflight`).
- 템플릿 기본값 `round_budget_tokens=60000`, `warn_at_ratio=0.75`, `output_reserve_per_role=1200`.

---

## 9. artifact 저장 규칙

실제 adapter 실행 또는 probe 실행 시 아래 산출물을 남깁니다.

권장 파일:
- `request.json`
- `prompt.txt`
- `schema.json`
- `command.json`
- `stdout.txt`
- `stderr.txt`
- `normalized.json`

실제 run-cycle 연동 시 저장 위치 (현 구현):
- 타겟 프로젝트 `.orch/runtime/adapter_runs/<run_id>/attempt-NN/...`
- `<run_id>` 형식: `<provider_id>-<role>-<YYYYmmdd-HHMMSS>-<uuid8>`
- attempt 단위 디렉토리를 두어 1차/재시도 산출물을 구분

probe 기본 위치:
- `orch-engine/.tmp/adapter_probes/<provider>-<role>-<timestamp>/...`
- probe는 `attempt-NN` 디렉토리를 사용하지 않고 run 디렉토리 최상위에 artifact를 그대로 남깁니다.

---

## 10. probe 성공 기준 / 모드

`tools/adapter_probe.py`는 두 모드를 가집니다.

**dry-run** (기본, `--live` 미지정):
- adapter를 호출하지 않고 stage 디렉토리에 `request.json` / `prompt.txt` / `schema.json` / `command.json`만 기록.
- 명령어/요청 envelope/스키마가 정상 구성되는지 빠르게 확인하기 위한 모드.

**live** (`--live` 지정):
- 실제 `ClaudeCliAdapter` / `CodexCliAdapter`의 `invoke`를 호출.
- 따라서 retry / normalize / schema 검증 / fatal 분류 / artifact 기록이 전부 production 경로와 동일하게 적용됨.
- raw 산출물은 adapter가 `<working_dir>/.orch/runtime/adapter_runs/<run_id>/attempt-NN/`에 기록.
- probe stage 디렉토리에는 `normalized.json`(InvocationResult 요약)만 추가로 남김.

성공 기준 (live 모드):
1. adapter `invoke`가 `AdapterFatalError` / `AdapterExecutionError` 없이 완료
2. `InvocationResult.status == "ok"` 이고 `payload`가 role schema 통과
3. attempt-NN 디렉토리에 `request.json` / `prompt.txt` / `schema.json` / `command.json` / `stdout.txt` / `stderr.txt` / `normalized.json` 이 남음
4. 실패 시 `error.json`에 `class` + `message`가 함께 기록
