# Goal: Gemness Observer & Intervention UI 구현

## 0. 최종 목표

현재 프로젝트의 Gemness 구조를 유지하되, Codex agent가 Gemness를 호출하는 동안 사용자가 브라우저 GUI에서 다음을 할 수 있게 구현한다.

1. Codex/MCP가 Gemini에게 보내는 질문, prompt, schema, diff, repair prompt를 transcript처럼 확인한다.
2. Gemini CLI의 응답, streaming delta, 최종 response, JSON parse 결과, schema validation 결과, repair 결과를 확인한다.
3. 사용자가 실행 중인 Codex ↔ Gemini 세션에 개입할 수 있다.
4. 개입 방식은 안전하고 명시적이어야 한다.
5. 기존 `ask_text`, `ask_json`, `review_current_diff`의 핵심 동작은 유지한다.

이 기능은 Codex의 최종 판단권을 바꾸는 것이 아니라, Gemness 호출을 관찰하고 제어할 수 있게 만드는 로컬 developer tool이다.

---

## 1. 현재 전제 구조

현재 또는 목표 Gemness 구조는 다음과 같다.

```text
User
  ↓
Codex App / Codex CLI
  ↓
Codex agent
  ↓
MCP tools:
  - ask_text
  - ask_json
  - review_current_diff
  ↓
Local Gemness server
  ↓
subprocess:
  gemini -m <model> -p <prompt> --output-format json 또는 stream-json
  ↓
MCP server post-processing:
  - CLI envelope JSON parse
  - response field extract
  - code fence 제거
  - JSON parse
  - JSON Schema validation
  - 1회 repair prompt
  - valid / invalid / error 반환
  ↓
Codex agent
  ↓
User
```

Gemini는 advisory reviewer/consultant로 동작해야 한다. 최종 판단, 코드 수정, 사용자 응답은 Codex agent가 담당한다.

---

## 2. 새로 추가할 핵심 기능

### 2.1 Observer Hub

MCP 서버 내부에 `ObserverHub` 또는 동등한 모듈을 추가한다.

역할:

- MCP tool call마다 `session_id` 생성
- prompt 생성 전후 이벤트 기록
- Gemini subprocess 시작/종료 이벤트 기록
- Gemini streaming output 또는 final response 기록
- JSON validation / repair lifecycle 기록
- user intervention 이벤트 수신
- browser UI로 이벤트 broadcast
- session 상태 저장
- transcript export 지원

권장 구조:

```text
Gemness Server
  ├─ MCP tools
  │   ├─ ask_text
  │   ├─ ask_json
  │   └─ review_current_diff
  │
  ├─ GeminiRunner
  │   ├─ runText()
  │   ├─ runJson()
  │   ├─ runReview()
  │   └─ repairJsonOnce()
  │
  ├─ ObserverHub
  │   ├─ EventBus
  │   ├─ SessionStore
  │   ├─ Redactor
  │   ├─ InterventionQueue
  │   └─ TranscriptExporter
  │
  └─ Local Web UI Server
      ├─ HTTP static assets
      ├─ WebSocket or SSE event stream
      └─ REST/WebSocket intervention API
```

WebSocket을 우선 사용하라. 구현 단순성 때문에 필요하면 SSE + REST POST 조합도 허용한다. 단, intervention까지 고려하면 WebSocket이 더 적합하다.

---

## 3. Browser GUI 요구사항

로컬 브라우저에서 열 수 있는 GUI를 구현한다.

예상 URL 예시:

```text
http://127.0.0.1:56755
```

MCP tool result에는 가능하면 다음 필드를 포함한다.

```json
{
  "observer_url": "http://127.0.0.1:56755/",
  "session_id": "<uuid>"
}
```

### 3.1 화면 구성

GUI는 최소한 다음 영역을 제공한다.

1. Session list
   - 최근 Gemness 호출 목록
   - tool name
   - status
   - model
   - started_at
   - duration
   - valid / invalid / error 여부

2. Transcript panel
   - Codex/MCP → Gemini prompt
   - Gemini → MCP response
   - streaming delta가 가능하면 실시간 출력
   - JSON schema
   - validation errors
   - repair prompt
   - repair result
   - final normalized result

3. Raw/Parsed toggle
   - raw prompt
   - sanitized prompt
   - raw Gemini response
   - parsed JSON
   - validation result
   - MCP return payload

4. Intervention panel
   - 현재 세션에 사용자 지시 입력
   - queued 상태에서는 prompt 수정 가능
   - running 상태에서는 interrupt & retry 가능
   - completed 상태에서는 follow-up 가능

5. Controls
   - Pause before send
   - Approve send
   - Edit prompt
   - Cancel session
   - Interrupt and retry with user instruction
   - Continue with additional instruction
   - Copy transcript
   - Export transcript JSON

---

## 4. Event schema

MCP 내부 이벤트는 JSONL 또는 SQLite에 저장한다. 우선 구현은 JSONL도 괜찮지만, session 검색과 UI 상태 복원을 고려해 SQLite가 더 좋다. 기존 프로젝트에 DB 의존성이 없으면 JSONL로 시작해도 된다.

이벤트는 최소한 다음 shape를 따른다.

```ts
type ObserverEvent = {
  event_id: string;
  session_id: string;
  parent_session_id?: string;
  ts: string;
  type:
    | "session.created"
    | "prompt.rendered"
    | "prompt.redacted"
    | "prompt.pending_approval"
    | "prompt.sent"
    | "gemini.started"
    | "gemini.delta"
    | "gemini.response"
    | "gemini.stderr"
    | "gemini.exited"
    | "json.extracted"
    | "json.parse_failed"
    | "json.validation_failed"
    | "json.validation_passed"
    | "repair.started"
    | "repair.prompt_sent"
    | "repair.response"
    | "repair.validation_passed"
    | "repair.validation_failed"
    | "intervention.received"
    | "intervention.applied"
    | "session.completed"
    | "session.cancelled"
    | "session.error";

  role: "codex_mcp" | "gemness" | "user" | "system";
  tool_name?: "ask_text" | "ask_json" | "review_current_diff";
  phase?: string;
  payload: Record<string, unknown>;
  redacted?: boolean;
};
```

---

## 5. Session state machine

각 MCP 호출은 다음 상태를 가진다.

```ts
type SessionStatus =
  | "queued"
  | "waiting_for_user_approval"
  | "sending"
  | "running"
  | "repairing"
  | "valid"
  | "invalid"
  | "error"
  | "cancelled"
  | "completed";
```

### 상태별 intervention 동작

#### queued

사용자는 prompt를 볼 수 있고 수정할 수 있다.

허용 동작:

- edit prompt
- approve
- cancel

#### waiting_for_user_approval

민감하거나 큰 요청일 경우 Gemini로 보내기 전에 사용자 승인을 기다린다.

허용 동작:

- approve
- edit then approve
- reject/cancel

#### running

이미 Gemini CLI subprocess가 실행 중인 상태다.

중요: single-shot headless subprocess에는 “이미 실행 중인 prompt 내부에 실시간 주입”이 불가능할 수 있다. 이 경우 다음 방식으로 구현한다.

- `interrupt & retry`
  - 현재 child process를 종료한다.
  - partial transcript를 기록한다.
  - 기존 prompt + partial response + 사용자 intervention을 포함한 새 prompt를 만든다.
  - 새 child process를 실행한다.
  - parent_session_id로 원래 session과 연결한다.

허용 동작:

- cancel
- interrupt & retry with instruction
- mark note only

#### completed / valid / invalid / error

완료된 세션에는 follow-up을 만들 수 있다.

허용 동작:

- follow-up with previous transcript
- copy/export transcript
- create new session from this session

---

## 6. Gemini CLI 실행 방식

가능하면 실시간 관찰을 위해 `--output-format stream-json`을 사용한다. Gemini CLI는 long-running operation 모니터링을 위해 newline-delimited JSON event를 내보내는 `stream-json` output format을 지원하는 것으로 문서화되어 있다.

단, `ask_json`에서는 canonical validation을 위해 최종 response가 필요하다. 따라서 다음 중 안정적인 방식을 선택하라.

### 옵션 A: stream-json 우선

- `stream-json` 이벤트를 UI에 실시간으로 표시한다.
- 최종 message/response를 누적한다.
- 누적된 final response를 기존 `ask_json` pipeline에 넣는다.
- response extraction, code fence removal, JSON parse, schema validation, repair를 수행한다.

### 옵션 B: json 우선

- `--output-format json`으로 canonical response를 받는다.
- UI에는 prompt sent, running, final response 이벤트를 표시한다.
- streaming delta는 제공하지 않는다.
- ask_json 안정성이 더 중요하면 이 방식을 선택한다.

구현 중 실제 Gemini CLI behavior를 확인하고, 더 안정적인 방식을 선택하라. 선택 이유를 docs에 남겨라.

---

## 7. ask_text 요구사항

`ask_text`는 다음을 수행한다.

1. session 생성
2. prompt 생성
3. redaction 수행
4. UI에 prompt 표시
5. 필요 시 approval 대기
6. Gemini CLI 실행
7. streaming 또는 final response 표시
8. 최종 text response를 MCP result로 반환

반환 예시:

```json
{
  "status": "completed",
  "text": "...",
  "session_id": "...",
  "observer_url": "http://127.0.0.1:56755/",
  "stats": {}
}
```

---

## 8. ask_json 요구사항

`ask_json`은 기존 구조를 유지하면서 observer 이벤트를 추가한다.

Pipeline:

```text
prompt 생성
  ↓
observer: prompt.rendered
  ↓
Gemini CLI 실행
  ↓
observer: gemini.response
  ↓
CLI envelope parse
  ↓
response field extract
  ↓
code fence 제거
  ↓
JSON candidate 추출
  ↓
JSON.parse
  ↓
JSON Schema validation
  ↓
valid이면 status=valid
  ↓
실패하면 repair prompt 1회
  ↓
repair 성공이면 status=valid, repaired=true
  ↓
repair 실패면 status=invalid
```

반환 shape는 반드시 다음 의미를 지켜라.

```ts
type AskJsonResult =
  | {
      status: "valid";
      data: unknown;
      raw_response: string;
      repaired: boolean;
      session_id: string;
      observer_url: string;
      stats?: unknown;
      warnings?: string[];
    }
  | {
      status: "invalid";
      raw_response: string;
      repaired: boolean;
      session_id: string;
      observer_url: string;
      parse_error?: string;
      validation_errors?: unknown[];
      repair_raw_response?: string;
      stats?: unknown;
    }
  | {
      status: "error";
      exit_code: number | null;
      session_id: string;
      observer_url: string;
      stderr_tail?: string;
      message: string;
    };
```

중요:

- schema validation 실패는 MCP tool crash가 아니라 `status: "invalid"`로 반환한다.
- subprocess timeout, command not found, Gemini CLI exit error는 `status: "error"`로 반환한다.
- repair는 1회만 수행한다.
- repair prompt는 “새 답변 생성”이 아니라 “기존 응답을 schema에 맞게 구조 복구”하는 역할로 제한한다.

---

## 9. review_current_diff 요구사항

`review_current_diff`는 Gemini에게 shell 권한을 주지 말고 MCP가 직접 diff를 만든다.

1. MCP 서버가 `git diff --no-color <base_ref> --` 실행
2. diff size limit 적용
3. diff를 Gemini prompt에 포함
4. observer에 diff prompt 표시
5. Gemini review 실행
6. 가능하면 JSON schema 기반 review 결과 반환
7. UI에서 finding, severity, file hint를 보기 좋게 렌더링

기본 review schema 예시:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["verdict", "summary", "findings", "recommended_actions"],
  "properties": {
    "verdict": {
      "type": "string",
      "enum": ["pass", "needs_work", "unsafe"]
    },
    "summary": {
      "type": "string"
    },
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["severity", "title", "explanation"],
        "properties": {
          "severity": {
            "type": "string",
            "enum": ["critical", "high", "medium", "low", "info"]
          },
          "title": { "type": "string" },
          "file": { "type": "string" },
          "line_hint": { "type": "string" },
          "explanation": { "type": "string" },
          "suggested_fix": { "type": "string" }
        }
      }
    },
    "recommended_actions": {
      "type": "array",
      "items": { "type": "string" }
    }
  }
}
```

---

## 10. 보안 및 privacy 요구사항

이 기능은 로컬 개발 도구이므로 기본적으로 안전하게 닫혀 있어야 한다.

필수 요구사항:

1. Web UI server는 기본적으로 `127.0.0.1`에만 bind한다.
2. 외부 네트워크 interface에 bind하지 않는다.
3. UI 접속은 loopback-only `127.0.0.1:56755` 대시보드를 사용한다.
4. `observer_url`은 세션별 URL이나 token URL이 아니라 고정 대시보드 URL을 반환한다.
5. secret redaction을 구현한다.
6. redaction 대상:
   - API keys
   - Bearer tokens
   - private keys
   - GitHub tokens
   - Google credentials
   - `.env` 값으로 보이는 key-value
7. redacted transcript와 raw transcript를 구분한다.
8. 기본 UI는 redacted view를 보여준다.
9. raw view가 필요하면 명시적 toggle을 요구한다.
10. OpenAI/Codex의 숨겨진 system/developer instructions 또는 내부 추론을 노출하려고 하지 않는다.
11. UI에는 MCP가 실제 Gemini에게 보낸 prompt와 Gemini가 반환한 output만 표시한다.
12. Gemini가 직접 파일을 수정하거나 shell을 실행하는 workflow는 기본 구현 범위에서 제외한다.

---

## 11. 사용자가 개입하는 방식

브라우저 UI에서 사용자는 다음 방식으로 개입할 수 있어야 한다.

### 11.1 Edit before send

Gemini에게 보내기 전 prompt를 사용자가 수정할 수 있다.

- 상태: queued 또는 waiting_for_user_approval
- 동작:
  - prompt draft 수정
  - approve
  - Gemini 실행

### 11.2 Add instruction before send

기존 prompt는 유지하고, 사용자의 추가 지시를 append한다.

예:

```text
User intervention:
Focus more on race conditions and data loss risk.
```

### 11.3 Interrupt and retry

실행 중인 Gemini subprocess를 중단하고, 새 prompt로 재시작한다.

새 prompt에는 다음이 포함되어야 한다.

- original task
- original prompt summary
- partial transcript, available한 경우
- user intervention
- “continue/re-answer with the intervention applied” instruction

### 11.4 Follow-up after completion

완료된 session에 후속 질문을 붙일 수 있다.

- 새 session 생성
- parent_session_id 설정
- 이전 transcript 요약 포함
- 사용자의 후속 지시 포함

---

## 12. 구현 스택 지침

프로젝트의 기존 스택을 먼저 확인하라.

- 이미 TypeScript MCP 서버라면 TypeScript로 구현하라.
- 이미 Python MCP 서버라면 Python으로 구현하라.
- Web UI는 기존 frontend stack이 있으면 재사용하라.
- 기존 frontend가 없으면 최소 의존성으로 구현하라.
  - 권장: Vite + React + TypeScript
  - 더 단순한 구현이 적합하면 vanilla HTML/TS도 허용
- 무거운 framework나 auth system은 도입하지 말라.
- local-only devtool로 충분하다.

---

## 13. 테스트 요구사항

최소한 다음 테스트를 추가하라.

### 13.1 Unit tests

- code fence 제거
- JSON candidate extraction
- JSON parse 실패 처리
- schema validation pass/fail
- repair 1회 제한
- event creation
- redaction
- session state transition
- intervention queue

### 13.2 Integration tests

Gemini CLI를 실제로 호출하지 말고 fake runner를 사용한다.

테스트 케이스:

1. ask_text happy path
2. ask_json valid JSON
3. ask_json fenced JSON
4. ask_json invalid → repair success
5. ask_json invalid → repair fail → status invalid
6. subprocess error → status error
7. review_current_diff with fake diff
8. queued prompt edit
9. running interrupt & retry
10. completed follow-up

### 13.3 Manual test script

문서에 다음 manual flow를 남겨라.

1. MCP 서버 시작
2. Web UI 열기
3. ask_text 호출
4. transcript 표시 확인
5. ask_json 호출
6. validation result 확인
7. review_current_diff 호출
8. running 중 interrupt & retry 확인
9. completed session follow-up 확인
10. transcript export 확인

---

## 14. Docs 요구사항

다음 문서를 작성하거나 업데이트하라.

1. `README.md` 또는 관련 docs
   - Gemness observer 기능 소개
   - 실행 방법
   - observer URL 확인 방법
   - intervention 사용법
   - security notes

2. `docs/gemini-observer.md`
   - architecture diagram
   - event schema
   - session state machine
   - intervention semantics
   - known limitations

3. Codex/MCP 설정 예시
   - `enabled_tools`
   - `tool_timeout_sec`
   - `default_tools_approval_mode`
   - observer server env vars

환경변수 예시:

```bash
GEMNESS_MODEL=gemini-3.1-pro-preview
GEMNESS_OBSERVER_ENABLED=true
GEMNESS_OBSERVER_HOST=127.0.0.1
GEMNESS_OBSERVER_PORT=56755
GEMNESS_OBSERVER_START_ON_INIT=true
GEMNESS_TRANSCRIPT_DIR=.gemness/transcripts
GEMNESS_REDACT_RAW_BY_DEFAULT=true
```

---

## 15. Checkpoint 방식으로 진행하라

이 작업은 `/goal`로 장시간 진행할 것이므로 반드시 checkpoint마다 요구사항을 스스로 검증하고 넘어가라.

각 checkpoint가 끝날 때마다 다음 파일을 업데이트하라.

```text
.codex/gemness-progress.md
```

각 checkpoint 기록 형식:

````md
## Checkpoint N: <name>

### Implemented
- ...

### Requirement self-check
- [ ] Existing MCP tools still work
- [ ] Observer event emitted
- [ ] Browser UI reflects state
- [ ] Intervention behavior tested
- [ ] Security/redaction considered
- [ ] Tests added or updated
- [ ] Docs updated if relevant

### Commands run
```bash
...
```

### Result
- Pass / Fail / Blocked

### Next action
- ...
````

Fail 또는 Blocked면 바로 다음 checkpoint로 넘어가지 말고, 먼저 수정하거나 명확한 blocker를 기록하라.

---

## 16. Required checkpoints

### Checkpoint 1: Repo inspection and implementation plan

해야 할 일:

- 기존 MCP server 구조 파악
- 기존 Gemini runner 파악
- 기존 ask_text / ask_json / review_current_diff 유무 확인
- test framework 확인
- frontend stack 유무 확인
- 가장 작은 구현 계획 작성

검증:

- 변경 전 구조 요약
- 구현 대상 파일 목록 작성
- 위험 요소 기록

### Checkpoint 2: Observer event model and session store

해야 할 일:

- session_id 생성
- event schema 구현
- SessionStore 구현
- EventBus 구현
- transcript persistence 구현
- redaction 기본 구현

검증:

- unit test 통과
- fake session event 기록 가능
- redacted view 확인

### Checkpoint 3: Gemini runner instrumentation

해야 할 일:

- ask_text에 observer event 추가
- ask_json에 observer event 추가
- review_current_diff에 observer event 추가
- Gemini subprocess lifecycle event 기록
- validation/repair event 기록

검증:

- 기존 MCP tool result shape가 깨지지 않아야 함
- session_id와 observer_url이 result에 포함되어야 함
- fake runner integration tests 통과

### Checkpoint 4: Browser UI

해야 할 일:

- local web server 추가
- session list 구현
- transcript panel 구현
- raw/parsed toggle 구현
- validation/repair 표시 구현
- status badge 구현

검증:

- 브라우저에서 session list 확인
- 새 event가 UI에 반영됨
- completed session 복원 가능

### Checkpoint 5: Intervention API

해야 할 일:

- queued prompt edit
- approve/cancel
- running interrupt & retry
- completed follow-up
- intervention events 기록

검증:

- fake runner로 running interrupt & retry 테스트
- parent_session_id 연결 확인
- intervention 내용이 새 prompt에 반영됨
- 원래 session은 cancelled 또는 superseded로 표시됨

### Checkpoint 6: Security hardening

해야 할 일:

- 127.0.0.1 bind 기본값 확인
- loopback-only dashboard URL 구현
- redaction 강화
- raw transcript toggle 보호
- transcript 저장 위치 확인
- external bind 방지

검증:

- loopback dashboard에서 token 없이 UI/API 접근 가능
- redaction test 통과
- raw view 기본 비활성화

### Checkpoint 7: Final tests and docs

해야 할 일:

- 전체 test suite 실행
- manual test 문서 작성
- architecture docs 작성
- config examples 작성
- known limitations 작성

검증:

- all tests pass
- docs complete
- acceptance criteria 충족

---

## 17. Acceptance criteria

작업은 다음 조건을 모두 만족해야 완료로 간주한다.

1. 기존 Gemness tools가 계속 동작한다.
2. 각 Gemness 호출마다 고유 session_id가 생성된다.
3. 각 tool result에 observer_url이 포함된다.
4. 브라우저 GUI에서 session list를 볼 수 있다.
5. 브라우저 GUI에서 Codex/MCP → Gemini prompt를 볼 수 있다.
6. 브라우저 GUI에서 Gemini response를 볼 수 있다.
7. ask_json의 parse/validation/repair 결과를 볼 수 있다.
8. review_current_diff의 diff review 결과를 볼 수 있다.
9. queued 또는 approval 상태에서 prompt를 수정할 수 있다.
10. running 상태에서 interrupt & retry를 할 수 있다.
11. completed 상태에서 follow-up session을 만들 수 있다.
12. transcript export가 가능하다.
13. secret redaction이 기본 적용된다.
14. Web UI는 기본적으로 127.0.0.1에만 bind한다.
15. observer API는 loopback-only 서버에서 token 없이 접근할 수 있다.
16. unit tests와 integration tests가 추가된다.
17. docs가 작성된다.
18. `.codex/gemness-progress.md`에 checkpoint별 self-check가 기록된다.

---

## 18. Non-goals

이번 작업에서 하지 말아야 할 것:

1. Gemini가 직접 repo 파일을 수정하게 만들지 말라.
2. Gemini에게 shell command 실행 권한을 기본 제공하지 말라.
3. 외부 네트워크에서 observer UI에 접근 가능하게 만들지 말라.
4. user intervention을 Codex 내부 hidden reasoning 접근 기능으로 구현하지 말라.
5. Codex의 최종 판단권을 Gemini로 넘기지 말라.
6. repair retry를 2회 이상 늘리지 말라.
7. 기존 MCP tools의 public API를 불필요하게 깨지 말라.

---

## 19. Known limitation을 문서화하라

문서에 반드시 다음 한계를 설명하라.

- headless Gemini subprocess가 이미 실행 중인 경우, 사용자의 intervention을 같은 process 내부에 즉시 주입하지 못할 수 있다.
- 이 경우 구현은 `interrupt & retry` 방식으로 동작한다.
- streaming이 Gemini CLI version 또는 output format에 따라 제한될 수 있다.
- ask_json은 정확한 structured output을 보장하기 위해 final response 기반 validation을 우선할 수 있다.
- Observer UI는 MCP가 Gemini에게 실제 보낸 prompt와 Gemini output을 보여주는 도구이며, Codex의 내부 hidden reasoning을 보여주는 도구가 아니다.

---

## 20. Final response requirement

작업 완료 후 최종 응답에는 다음을 포함하라.

1. 구현 요약
2. 주요 변경 파일
3. 실행 방법
4. observer UI 접속 방법
5. intervention 사용 방법
6. 테스트 결과
7. 남은 한계 또는 follow-up 제안
