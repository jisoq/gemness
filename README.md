# Gemness Observer

Gemness는 Codex가 Antigravity CLI(`agy`)의 조언을 구하고, 브라우저 기반의 Observer UI를 통해 각 분석 실행(run) 과정을 검사할 수 있도록 지원하는 로컬 MCP(Model Context Protocol) 서버입니다.

이 서버는 다음 8개의 MCP 도구를 제공합니다. Codex 연동의 최우선 흐름은 main agent가 `antigravity reviewer` subagent를 띄우고, 그 subagent가 delegated run owner로서 `start_antigravity`로 백그라운드 run을 시작한 뒤 `await_antigravity_run`으로 결과를 기다려 최종 advisory만 parent에게 돌려주는 방식입니다.

| 도구 | 쉽게 말하면 | 언제 쓰나 | 꼭 필요한가 |
| :--- | :--- | :--- | :--- |
| `antigravity_health` | Gemness, 워크스페이스, Observer, `agy` CLI, Codex host capability가 준비됐는지 확인 | 설치 직후, 연결이 이상할 때, 첫 multi-agent 가능 여부 기록 | 필수 |
| `start_antigravity` | 백그라운드 run을 시작하고 바로 `run_id`를 받음 | delegated run owner인 subagent가 Antigravity 작업을 시작할 때 | 기본 흐름 |
| `await_antigravity_run` | 백그라운드 run 상태나 결과를 확인 | run owner가 `run_id`로 결과를 기다리거나 `timeout_sec=0`으로 즉시 조회 | 기본 흐름 |
| `cancel_antigravity_run` | 실행 중인 백그라운드 run 중단 요청 | 잘못 시작했거나 너무 오래 걸릴 때 | 중단용 |
| `ask_antigravity` | 일반 질문을 시작부터 완료까지 한 번에 처리 | 단순 second opinion이 필요하고 별도 polling이 필요 없을 때 | 편의 도구 |
| `follow_up_antigravity` | 이전 Gemness 실행에 이어서 후속 질문 | 같은 대화의 맥락을 간단히 이어갈 때 | 편의 도구 |
| `ask_antigravity_json` | 답변을 JSON Schema에 맞춰 한 번에 받음 | 자동화, 분류, 구조화된 리뷰 결과가 필요할 때 | 편의 도구 |
| `review_current_diff_with_antigravity` | 현재 워크스페이스 변경사항 리뷰를 한 번에 요청 | 커밋/PR 전 변경 검토 | 편의 도구 |

`start_antigravity`는 `mode`로 작업 종류를 고릅니다. 일반 질문은 `mode="ask"`, JSON 결과는 `mode="json"`, 현재 diff 리뷰는 `mode="review_current_diff"`, 후속 질문은 `mode="follow_up"`을 사용합니다. `ask_antigravity`, `follow_up_antigravity`, `ask_antigravity_json`, `review_current_diff_with_antigravity`는 같은 작업을 blocking final-result 형태로 감싼 편의 도구입니다.

각 루트 도구 호출은 Gemness 실행(run)과 Gemness 대화(conversation)를 생성합니다. main agent는 Antigravity 작업을 직접 오래 점유하지 않고, subagent가 백그라운드 run을 관리한 뒤 정리된 요약, 주요 findings, `observer_url`을 parent에게 반환하는 흐름을 최우선으로 사용합니다. main agent가 reviewer를 띄운 뒤에는 같은 작업에 대해 `start_antigravity`, `await_antigravity_run`, blocking wrapper 도구를 중복 호출하지 않습니다.

---

## 퀵 스타트 (Quick Start)

Gemness는 포터블(portable)한 MCP 설치를 위해 설계되었습니다. Codex는 원격 git 소스로부터 `uvx`를 통해 Gemness를 실행하므로, 로컬 체크아웃 경로, `.venv` 또는 PyPI 패키지 이름에 종속되지 않습니다.

임의의 디렉터리에서 아래 명령어를 실행하십시오:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness bootstrap-codex
```

`bootstrap-codex` 명령어는 다음 작업을 수행합니다:
- 사용자의 Codex 설정 파일에 `[mcp_servers.gemness]` 블록을 추가/갱신합니다.
- `use gemness` 트리거 동작을 담은 `gemness` skill을 설치/갱신합니다.
- `agy --version` 명령어를 통해 Antigravity CLI가 사용 가능한지 확인합니다.
- 설정된 MCP stdio 명령어로 스모크 테스트(smoke test)를 실행하여 동작 여부를 검증합니다.

설치가 완료되면 Codex를 재시작하고 다음과 같이 요청해 보세요:

```text
use gemness health check
```

자세한 설치 단계는 [INSTALL.md](INSTALL.md)를 참고해 주십시오.

---

## Antigravity CLI

공식 문서를 참고하여 Antigravity CLI를 설치하십시오:

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
```

CLI가 정상적으로 설치되었는지 확인하기 위해 아래 스모크 테스트를 실행합니다:

```powershell
agy --help
agy -p "Return exactly: GEMNESS_AGY_HEALTHCHECK"
```

Windows 환경에서 Gemness는 우선 시스템 `PATH`에서 `agy`를 검색하고, 찾지 못할 경우 `%LOCALAPPDATA%\agy\bin\agy.exe` 경로를 확인합니다. Windows 환경에서는 Antigravity CLI가 stdout/stderr 대신 콘솔 버퍼에 직접 출력하므로, Gemness는 항상 `pywinpty`로 콘솔 출력을 캡처합니다.

모델 선택은 Gemness 실행 시 전달하는 인자가 아니라 Antigravity CLI 고유의 설정 영역입니다. 모델을 변경하려면 Antigravity CLI 설정 파일을 수정하거나 `/model` 슬래시 명령어를 사용하십시오. (예: `Gemini 3.5 Flash` 등의 모델은 사용자가 Antigravity CLI에서 지정하는 옵션이며, Gemness의 `--model` 인자로 전달되지 않습니다.)

---

## 실행 (Run)

아래 명령어를 사용하여 MCP 서버를 독립적으로 실행할 수 있습니다:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness start-mcp-server
```

MCP 서버는 stdio를 통해 통신합니다. 기본적으로 MCP 프로세스가 시작되는 즉시 백그라운드에서 Observer 웹 서버도 함께 시작되므로, `ask_antigravity` 도구가 호출되기 전이라도 브라우저에서 `http://127.0.0.1:56755` 주소를 열어 대기 상태를 확인할 수 있습니다.

---

## Codex 연동 (Connect to Codex)

Codex에 연동할 때도 동일하게 bootstrap 명령어를 사용합니다:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness bootstrap-codex
```

생성되는 MCP 설정의 기본 구조는 다음과 같습니다:

- `command = "uvx"`
- `args = ["--from", "git+https://github.com/jisoq/gemness", "gemness", "start-mcp-server"]`
- `default_tools_approval_mode = "prompt"`
- `GEMNESS_AGY_TIMEOUT = "600"`

`use gemness` 트리거 가이던스는 Codex main agent가 Gemness MCP 도구를 직접 오래 점유하지 않도록 설계되어 있습니다. main agent는 reviewer subagent에 Antigravity 검토를 위임하고, reviewer subagent가 delegated run owner로서 `start_antigravity`와 `await_antigravity_run`으로 백그라운드 run을 관리한 뒤 정제된 요약, 주요 findings, `observer_url`을 parent에게 반환합니다.

Gemness health check는 작업당 main agent나 reviewer subagent 중 한 주체만 맡습니다. main agent가 health와 host capability를 확인한 뒤 reviewer를 띄우는 경우, reviewer prompt에 `Gemness health handoff`로 cwd, health status, `codex_host.multi_agent.available`, `antigravity_health` 호출 여부를 넘깁니다. reviewer는 같은 cwd의 `ok` 또는 `warning` handoff를 받으면 `antigravity_health`를 반복하지 않고 바로 `start_antigravity`부터 진행합니다. handoff가 없고 reviewer가 첫 Gemness 실행 주체라면 reviewer가 health를 한 번만 확인하고 이어서 작업합니다.

첫 `use gemness health check`에서는 Codex host의 subagent/spawn/delegation 도구 가능 여부를 먼저 확인한 뒤, `antigravity_health`의 `codex_multi_agent_available`과 `codex_multi_agent_evidence` 인자로 그 결과를 기록합니다. 이 결과는 `~/.gemness/codex-host-capabilities.json`에 저장되며, 이후 다른 레포에서 Gemness를 사용할 때도 같은 Codex host capability로 재사용됩니다. 캐시가 available이면 main agent는 매번 재탐색하지 않고 reviewer subagent 흐름을 먼저 사용합니다.

reviewer subagent가 백그라운드 run을 관리하는 동안 main agent는 빈손으로 기다리지 않습니다. main agent는 관련 코드와 diff를 읽고, 테스트나 검증 루틴을 준비하거나 실행하고, reviewer 조언을 받았을 때 적용 여부를 판단할 수 있도록 비중복 작업을 먼저 진행합니다.

main agent가 reviewer를 spawn할 때는 `delegated_run handoff`에 cwd, task, mode, 필요한 schema 또는 parent session id, parent-generated `delegation_id`를 담습니다. reviewer는 이 `delegation_id`를 `start_antigravity`의 `idempotency_key`로 그대로 넘겨 같은 위임 작업이 중복 시작되지 않게 합니다. reviewer subagent는 추가 subagent를 다시 spawn하지 않습니다.

reviewer subagent의 기본 spawn profile은 `model="gpt-5.4-mini"`, `reasoning_effort="high"`입니다. reviewer는 Gemness 실행과 결과 요약을 맡고, 핵심 reasoning은 Antigravity CLI가 수행한다는 전제의 경량 프로파일입니다.

main agent가 delegated Gemness run을 takeover할 수 있는 경우는 reviewer spawn 실패, reviewer의 명시적 실패나 타임아웃, reviewer가 `run_id`만 반환하고 종료된 경우, 사용자가 main 직접 실행을 명시적으로 요청한 경우입니다. takeover 시에는 새 run을 시작하지 않고 기존 `run_id`나 session identifier를 이용해 await, cancel, follow-up만 수행합니다.

특정 경로의 Antigravity CLI 실행 파일을 고정하여 사용하려면 다음과 같이 실행하십시오:

```powershell
uvx --from git+https://github.com/jisoq/gemness gemness bootstrap-codex --agy-command "$env:LOCALAPPDATA\agy\bin\agy.exe"
```

---

## Observer UI

웹 브라우저를 통해 실시간 라이브 Observer에 접속할 수 있습니다:

```text
http://127.0.0.1:56755
```

대시보드에는 세션/대화 목록이 표시되며, 가장 최근에 실행된 세션이 자동으로 활성화되어 추적됩니다. 대시보드를 통해 프롬프트, Antigravity 실행 시작, 최종 출력, stderr 진단 로그, JSON 추출 결과, 스키마 유효성 검증(validation) 및 실패 시 복구(repair) 시도 과정, 리뷰 결과 등을 편리하게 모니터링할 수 있습니다.
또한 세션 목록에서 완료된 로컬 대화 기록의 이름을 변경(rename)하거나 삭제(delete)하는 등의 세션 관리 작업이 가능합니다. Antigravity의 텍스트 출력은 프로세스 실행이 완료된 후 한꺼번에 캡처되므로, 메타데이터는 `streaming=false` 상태로 기록됩니다. 실행 중에는 `antigravity.heartbeat` 이벤트가 주기적으로 기록되고, Observer 기본 화면은 이를 채팅 메시지로 누적하지 않고 상태 LED와 runtime telemetry로 요약합니다. 원본 이벤트 / 디버그 정보 패널에서는 elapsed time, timeout까지 남은 시간, pid, capture mode, stdout/stderr byte count, last activity age를 확인할 수 있습니다.

---

## 기본 Background Run Workflow

subagent가 Antigravity 검토를 맡을 때는 아래 흐름을 최우선으로 사용합니다:

먼저 health owner를 정합니다. main agent가 `Gemness health handoff`를 넘겼다면 subagent는 같은 cwd에서 health를 재확인하지 않습니다.

1. parent가 넘긴 `delegation_id`를 `idempotency_key`로 사용해 `start_antigravity`를 호출합니다.
   - 일반 질문: `mode="ask"`, `prompt`
   - JSON 결과: `mode="json"`, `prompt`, `schema`
   - 현재 diff 리뷰: `mode="review_current_diff"`, 선택적으로 `base_ref`
   - 후속 질문: `mode="follow_up"`, `parent_session_id`, `prompt`
2. 반환된 `run_id`와 `observer_url`을 보관합니다.
3. `await_antigravity_run(run_id, timeout_sec=5)`처럼 짧은 대기 호출을 반복해 완료 상태를 확인합니다. 즉시 상태만 보려면 `timeout_sec=0`을 사용합니다.
4. 필요하면 `event_cursor`를 넘겨 새 이벤트만 조회합니다.
5. 중단이 필요하면 `cancel_antigravity_run(run_id)`을 호출합니다.

`start_antigravity`는 선택적으로 `idempotency_key`를 받을 수 있습니다. Codex reviewer 흐름에서는 parent가 만든 `delegation_id`를 `idempotency_key`로 재사용합니다. 같은 key가 같은 도구/cwd/workspace fingerprint scope로 다시 들어오면 기존 run을 재사용하여 중복 실행을 줄입니다. cwd가 다르거나 workspace fingerprint가 달라지면 같은 key라도 별도 run으로 처리합니다.

---

## Multi-agent host capability and token observability

Gemness는 main agent가 Antigravity 실행에 오래 묶이지 않도록 `start_antigravity` / `await_antigravity_run` 기반의 start-await 흐름을 제공합니다. 기본 구조는 reviewer subagent가 delegated run owner로 Antigravity run을 관리하고, parent에게는 full Antigravity answer를 그대로 붙여넣지 않고 concise advisory로 요약해 보고하는 방식입니다.

Codex host의 multi-agent 가능 여부는 사용자 단위 캐시 파일에 기록됩니다. `antigravity_health`는 이 캐시를 `codex_host` 필드로 반환하며, 아직 기록되지 않은 경우에는 health 응답에 `not_recorded` 상태를 표시합니다. Gemness MCP 서버는 Codex host의 spawn 도구 목록을 직접 볼 수 없으므로, 첫 판정은 main agent가 host tool discovery로 수행하고 Gemness가 그 결과를 저장합니다.

작업 실행 중 health check는 handoff 계약으로 중복을 피합니다. main agent가 health owner이면 reviewer subagent는 health 결과를 신뢰하고 Antigravity run 관리에 집중합니다. reviewer가 health owner이면 reviewer가 한 번 확인한 뒤 같은 작업 안에서 main agent에게 재확인을 요구하지 않습니다.

작업 실행 중 run ownership도 handoff 계약으로 중복을 피합니다. reviewer가 delegated run owner이면 main agent는 같은 작업에 대해 `start_antigravity`나 `await_antigravity_run`을 호출하지 않고, 코드 읽기, diff 확인, 테스트 준비처럼 Gemness run과 겹치지 않는 일을 수행합니다.

Gemness 자체는 full result를 숨기거나 삭제하지 않습니다. 완료된 `await_antigravity_run` 응답의 `result`에는 기존처럼 전체 `text` 또는 JSON `data`가 남아 있고, `summary`, `budget`, `observer_url`, `session_id`, `run_id`도 함께 포함됩니다. MCP는 임의로 `text`나 JSON findings를 잘라 토큰을 절약하지 않습니다.

각 run result와 완료된 await payload에는 `budget` 객체가 포함됩니다. 주요 필드는 `prompt_chars`, `prompt_est_tokens`, `response_chars`, `response_est_tokens`, `raw_stdout_bytes`, `result_chars`, `result_est_tokens`, `duration_ms`, `response_mode`, `estimate_method`, `truncated`입니다. Antigravity CLI envelope에 token stats가 있으면 그 값을 우선 사용하고, 없으면 `ceil(chars / 4)`로 추정합니다. 이 값은 정확한 과금 수치가 아니라, 다중 LLM 사용에서 중복/낭비를 발견하기 위한 관측용 추정치입니다.

각 run에는 `request_fingerprint`, `workspace_fingerprint`, `workspace_fingerprint_degraded`도 기록됩니다. git 워크스페이스에서는 HEAD sha, porcelain status hash, diff hash를 조합해 workspace fingerprint를 만들지만 raw diff는 MCP 응답이나 public Observer payload에 저장하지 않습니다. 현재 request fingerprint는 기록용이며, 자동 dedupe는 기본 비활성화입니다. 향후 실험을 위해 `GEMNESS_ENABLE_AUTO_DEDUPE=false` 플래그 이름만 예약되어 있고, 이번 동작에서는 같은 fingerprint라도 run을 자동 재사용하지 않습니다. 기존 `idempotency_key` 기반 중복 방지는 계속 우선 적용되지만, degraded workspace fingerprint에서는 완료된 이전 세션을 디스크에서 재사용하지 않고 실행 중인 동일 scope run만 중복 방지 대상으로 삼습니다.

---

## 대화 관리 (Conversation Management)

이전 실행의 문맥을 이어서 대화를 진행하려면 `start_antigravity`에 `mode="follow_up"`과 `parent_session_id`를 넘깁니다. 짧은 후속 질문을 한 번에 처리하고 싶을 때는 편의 도구인 `follow_up_antigravity`를 사용할 수 있습니다. Observer UI는 기본적으로 읽기 전용(read-mostly) 화면으로 설계되어 있어, UI 상에서 대기 중인 프롬프트를 수정할 수는 없습니다. 실행 중인 하위 프로세스 중단은 MCP 도구 `cancel_antigravity_run(run_id)`이 담당합니다. 대시보드 내 세션 목록 편집 기능은 로컬 대화 기록 정리(이름 변경 및 삭제) 목적으로만 제공됩니다.

---

## 보안 (Security)

- Observer 웹 서버는 오직 `127.0.0.1`, `localhost`, `::1`와 같은 로컬 루프백(loopback) 주소에만 바인딩됩니다.
- API, SSE 이벤트 스트림, 데이터 내보내기(export), 세션 이름 변경 및 삭제 등의 모든 엔드포인트는 로컬 요청에만 응답합니다.
- 대화 기록 내 프롬프트 및 응답 텍스트는 UI 및 API 상에서 기본적으로 민감 정보 필터링(redacted)이 적용되어 표시됩니다.
- 민감 정보 필터링이 해제된 원본 데이터(raw transcript)를 내보내려면 API 호출 시 명시적으로 `raw=1` 쿼리 파라미터를 추가해야 합니다.
- MCP 도구 응답은 raw transcript 전체를 반환하지 않습니다. 최종 advisory result, 구조화된 data/findings, 요약, `observer_url` 위주로 반환하며 Antigravity 진행 문구와 장황한 내부 출력은 Observer 쪽으로 격리합니다.
- Observer에 기록되는 `run.command` 및 결과 metadata의 `agy -p <prompt>` 형태 argv는 prompt 본문을 `[PROMPT_REDACTED]`로 대체합니다.
- Gemness는 대량의 컨텍스트 전달 수단(courier)으로 사용되어서는 안 됩니다. Antigravity CLI가 직접 로컬 워크스페이스를 파악하고 탐색할 수 있으므로, 프롬프트에 방대한 diff 파일, 코드 덤프, 로그 텍스트 등을 직접 복사해서 붙여넣지 마십시오.
- `review_current_diff_with_antigravity` 도구는 git 워크스페이스에서만 current diff 리뷰를 시작합니다. non-git cwd이거나 git 변경 파일 목록을 만들 수 없으면 `diff_unavailable_*` 오류로 명시적으로 실패합니다. Gemness는 raw diff 텍스트를 인자로 실어 보내지 않고, cwd/workspace root/base ref/changed file list만 scope로 전달합니다. 최종 JSON advisory에는 `review_scope`가 포함되며, Gemness는 이 scope가 요청 cwd와 변경 파일 목록에 맞는지 검증합니다. 로컬 Antigravity CLI가 접근해서는 안 되는 기밀(secrets)이 포함된 워크스페이스에서는 해당 도구를 실행하지 마십시오.

---

## 환경 변수 (Environment)

Gemness는 아래 환경 변수들을 지원하며, `.env` 파일 또는 시스템 환경 변수를 통해 커스텀 설정이 가능합니다:

| 환경 변수명 | 기본값 | 설명 |
| :--- | :--- | :--- |
| `GEMNESS_AGY_COMMAND` | `agy` | 실행할 Antigravity CLI 명령어 이름 또는 절대 경로 |
| `GEMNESS_AGY_TIMEOUT` | `600` | Antigravity CLI 실행 제한 시간(초) |
| `GEMNESS_AGY_HEALTH_TIMEOUT` | `20` | `antigravity_health` 호출 시 CLI 헬스 체크 제한 시간(초) |
| `GEMNESS_AGY_CAPTURE_MODE` | `winpty` | CLI 출력 캡처 모드. Windows에서는 항상 `pywinpty` 캡처를 사용하며, 기존 `auto`/`pipe` 값도 `winpty`로 정규화 |
| `GEMNESS_AGY_HEARTBEAT_INTERVAL` | `5` | 실행 중 `antigravity.heartbeat` 기록 간격(초) |
| `GEMNESS_AGY_CONCURRENCY_LIMIT` | `4` | 동시에 실행할 Antigravity background run 수 |
| `GEMNESS_ENABLE_AUTO_DEDUPE` | `false` | 향후 request fingerprint 기반 자동 dedupe 실험을 위한 예약 플래그. 현재 기본 동작은 기록만 수행 |
| `GEMNESS_OBSERVER_ENABLED` | `true` | Observer 웹 서버 활성화 여부 |
| `GEMNESS_OBSERVER_HOST` | `127.0.0.1` | Observer 웹 서버 호스트 바인딩 주소 (루프백 주소만 허용) |
| `GEMNESS_OBSERVER_PORT` | `56755` | Observer 웹 서버 포트 번호 |
| `GEMNESS_OBSERVER_START_ON_INIT` | `true` | MCP 서버 구동 시 Observer 자동 시작 여부 |
| `GEMNESS_TRANSCRIPT_DIR` | `~/.gemness/transcripts` | 로컬 대화 기록(transcripts)이 영구 저장될 디렉터리 경로 |
| `GEMNESS_CODEX_HOST_CAPABILITIES_FILE` | `~/.gemness/codex-host-capabilities.json` | Codex host multi-agent 가능 여부 캐시 파일 |
| `GEMNESS_REDACT_RAW_BY_DEFAULT` | `true` | 대화 기록의 UI/API 노출 시 민감 정보 자동 가림 처리 여부 |

자세한 설명과 예시는 [docs/antigravity-observer.md](docs/antigravity-observer.md) 및 [docs/codex-mcp-config.example.toml](docs/codex-mcp-config.example.toml) 문서를 참고하십시오.

---

## 테스트 실행 (Tests)

로컬 개발 환경에서 테스트 코드를 실행하려면 아래 명령어를 실행하십시오:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
python -m pytest -q -p no:cacheprovider
```
