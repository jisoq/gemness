목표: **Gemness Observer를 더 안정적으로 작동시키고, Codex App/CLI의 MCP 서버 연결을 마무리하며, 일반 Codex 사용 중 `use gemness`라는 트리거 문구로 Gemini CLI MCP 사용을 유도한다.**

---

## 0. 요약

현재 구현은 방향이 맞다. Python 기반 STDIO MCP 서버가 있고, `ask_text`, `ask_json`, `review_current_diff` tool, Gemini CLI headless subprocess runner, Observer UI, intervention API, tests, docs까지 갖춰져 있다.

다만 실제 사용자가 설치해서 Codex App/CLI에서 안정적으로 쓰려면 다음 항목이 더 필요하다.

1. Codex MCP config 예시 수정
2. MCP 서버 `cwd` / workspace root 고정
3. `health_check` tool 추가
4. Gemini CLI path/auth/model/trust 설정 안정화
5. runtime token/transcript/pid 파일 보안 및 packaging 제외
6. JSON schema / repair / stats / error handling 강화
7. MCP smoke test 추가
8. `use gemness` 트리거를 AGENTS.md와 Skill 양쪽에 반영
9. 설치 후 검증 플로우 문서화

---

## 1. 핵심 문제와 우선순위

## P0. Codex MCP config example의 approval mode 값이 잘못됨

현재 `docs/codex-mcp-config.example.toml`:

```toml
default_tools_approval_mode = "on-request"
```

수정해야 한다.

```toml
default_tools_approval_mode = "prompt"
```

Codex MCP tool approval mode는 `auto | prompt | approve` 계열로 맞춰야 한다. `on-request`는 MCP server별 tool approval mode 값으로 쓰면 안 된다.

권장 예시:

```toml
[mcp_servers.gemness]
command = "<ABSOLUTE_PATH_TO_VENV_PYTHON>"
args = ["-m", "gemness.server"]
cwd = "<ABSOLUTE_PATH_TO_GEMNESS_REPO>"
startup_timeout_sec = 10
tool_timeout_sec = 300
required = true
enabled_tools = [
  "health_check",
  "ask_text",
  "ask_json",
  "review_current_diff",
]
default_tools_approval_mode = "prompt"

[mcp_servers.gemness.tools."health_check"]
approval_mode = "approve"

[mcp_servers.gemness.tools."ask_text"]
approval_mode = "approve"

[mcp_servers.gemness.tools."ask_json"]
approval_mode = "prompt"

[mcp_servers.gemness.tools."review_current_diff"]
approval_mode = "prompt"
```

---

## P0. MCP server cwd가 고정되어 있지 않음

현재 `review_current_diff`는 `Path.cwd()` 기준으로 `git diff`를 실행한다.

```python
subprocess.run(
    ["git", "diff", "--no-color", base_ref, "--"],
    cwd=Path.cwd(),
    ...
)
```

Codex가 MCP 서버를 어떤 디렉터리에서 시작하느냐에 따라 엉뚱한 repo를 보거나 `git diff`가 실패할 수 있다.

필수 수정:

- `GemnessConfig.workspace_root` 추가
- `GEMNESS_WORKSPACE_ROOT` 추가
- `GEMNESS_ALLOWED_ROOTS` 추가
- Codex config에 `cwd = "<repo absolute path>"` 추가
- tool input에 optional `cwd` 추가
- `ask_text`, `ask_json`, `review_current_diff`, runner subprocess, git diff 모두 resolved cwd를 사용

권장 resolver:

```python
def resolve_workspace_cwd(requested_cwd: str | None) -> Path:
    candidate = Path(
        requested_cwd
        or config.workspace_root
        or Path.cwd()
    ).expanduser().resolve()

    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"Invalid cwd: {candidate}")

    if config.allowed_roots:
        if not any(candidate == root or root in candidate.parents for root in config.allowed_roots):
            raise ValueError(f"cwd outside allowed roots: {candidate}")

    return candidate
```

---

## P0. runtime token/transcript/pid 파일이 zip에 포함됨

업로드된 zip에는 다음 runtime 파일이 포함되어 있었다.

```text
.codex/live-observer-followup-transcripts/*.jsonl
.codex/live-observer-followup-transcripts/observer-token.txt
.codex/live-observer-real-transcripts/*.jsonl
.codex/live-observer-real-transcripts/observer-token.txt
.codex/live-observer-real.pid
.codex/live-observer-real.out
.codex/live-observer-real.err
.gemness/transcripts/observer-token.txt
```

이 파일들은 repo, 배포 zip, package, 커밋에 포함되면 안 된다. 특히 `observer-token.txt`는 observer API 접근 token이므로 반드시 제외해야 한다.

필수 수정:

- `.gitignore` 추가
- packaging/export script에서 runtime 파일 제외
- token file permission `0600` 적용
- token이 포함된 `observer_url`을 raw transcript event에 저장하지 않게 변경
- raw transcript persistence를 기본 비활성화하거나 강하게 문서화

권장 `.gitignore`:

```gitignore
# Python
__pycache__/
*.py[cod]
.venv/
*.egg-info/
.pytest_cache/

# Gemness observer runtime files
.gemness/
.codex/live-observer*/
.codex/live-observer*.json
.codex/live-observer*.pid
.codex/live-observer*.out
.codex/live-observer*.err
**/observer-token.txt
**/*.jsonl

# Local secrets
.env
.env.*
*.pem
*.key
```

---

## P1. `--skip-trust` 기본값이 true임

현재 설정:

```python
gemini_skip_trust: bool = _bool_env("GEMNESS_GEMINI_SKIP_TRUST", True)
```

권장 수정:

```python
gemini_skip_trust: bool = _bool_env("GEMNESS_GEMINI_SKIP_TRUST", False)
```

이 MCP는 Gemini를 advisory reviewer로 쓰는 구조다. 기본적으로 Gemini에게 workspace trust bypass를 주지 말고, 필요한 경우만 사용자가 명시적으로 켜도록 해야 한다.

README/docs의 기본값도 바꾼다.

```bash
GEMNESS_GEMINI_SKIP_TRUST=false
```

---

## P1. `health_check` tool이 없음

Codex 연결 마무리에는 `health_check` tool이 매우 중요하다. 사용자는 MCP가 연결됐는지, Gemini CLI가 resolve되는지, observer가 켜졌는지, cwd가 올바른지 빠르게 확인해야 한다.

추가할 tool:

```text
health_check
```

입력 schema:

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "cwd": { "type": "string" },
    "check_gemini": { "type": "boolean", "default": true }
  }
}
```

반환 예시:

```json
{
  "status": "ok",
  "server": {
    "name": "gemness",
    "version": "0.1.0",
    "python": "...",
    "executable": "..."
  },
  "mcp": {
    "transport": "stdio",
    "tools": [
      "health_check",
      "ask_text",
      "ask_json",
      "review_current_diff"
    ]
  },
  "workspace": {
    "cwd": "...",
    "is_git_repo": true,
    "allowed": true
  },
  "gemini": {
    "command": "...",
    "resolved": "...",
    "model": "gemini-3.1-pro-preview",
    "version": "...",
    "skip_trust": false,
    "approval_mode": "plan"
  },
  "observer": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 12345,
    "url": "http://127.0.0.1:12345"
  },
  "warnings": []
}
```

`health_check`는 비싼 모델 호출을 하지 않는다. `gemini --version` 또는 command resolution만 확인한다.

---

## P1. Gemini CLI output envelope 처리가 미흡함

`GeminiRunResult.stats`가 거의 항상 `{}`로 남는다. `extract_cli_response()`가 envelope를 반환하지만 stats/error가 tool result에 충분히 반영되지 않는다.

수정:

- `envelope["stats"]`를 tool result stats에 반영
- `envelope["error"]`가 있으면 exit code 0이어도 `status=error` 또는 warning 처리
- raw stdout은 observer event에 남기되, parsed response/stats/error는 별도 payload로 기록

---

## P1. 실시간 CLI-like transcript가 아직 final-response 중심임

현재 runner는 stdout/stderr를 끝까지 읽은 뒤 `gemini.response`를 기록한다. 브라우저에서 “CLI처럼 직접 읽기”를 원한다면 streaming event가 필요하다.

수정 방향:

- `GEMNESS_OUTPUT_MODE=json` 기본 유지
- `GEMNESS_OUTPUT_MODE=stream-json` 옵션 추가
- stream-json에서는 stdout을 line-by-line으로 읽고 JSONL event를 observer에 즉시 broadcast
- `ask_json`은 final result를 누적한 뒤 기존 schema validation pipeline 적용
- stream-json이 불안정하거나 미지원이면 json final-only mode로 fallback
- UI에는 현재 mode를 표시

---

## P1. `base_ref` validation 부족

`review_current_diff`는 shell string을 쓰지는 않지만, `base_ref`가 `-`로 시작하면 git option처럼 해석될 수 있다.

추가:

```python
def validate_base_ref(base_ref: str) -> str:
    ref = base_ref.strip()
    if not ref:
        raise ValueError("base_ref is required")
    if ref.startswith("-"):
        raise ValueError("base_ref must not start with '-'")
    if any(ch.isspace() for ch in ref):
        raise ValueError("base_ref must not contain whitespace")
    if len(ref) > 200:
        raise ValueError("base_ref too long")
    return ref
```

테스트:

- `HEAD` pass
- `origin/main...HEAD` pass
- `--no-index` fail
- `HEAD -- path` fail

---

## P1. JSON Schema 자체 validation 필요

`ask_json`에서 모델 호출 전에 schema 자체가 유효한 JSON Schema인지 확인한다.

```python
from jsonschema import Draft202012Validator, SchemaError

try:
    Draft202012Validator.check_schema(schema)
except SchemaError as exc:
    return {
        "status": "error",
        "message": f"Invalid JSON Schema: {exc.message}",
        ...
    }
```

---

## P1. repair 상태 semantics 개선

현재 repair가 실패한 invalid 결과에서 `repaired: false`만 보면 repair를 시도했는지 알기 어렵다.

권장 필드:

```json
{
  "status": "invalid",
  "repaired": false,
  "repair_attempted": true,
  "repair_succeeded": false
}
```

첫 시도 성공:

```json
{
  "status": "valid",
  "repaired": false,
  "repair_attempted": false,
  "repair_succeeded": false
}
```

repair 성공:

```json
{
  "status": "valid",
  "repaired": true,
  "repair_attempted": true,
  "repair_succeeded": true
}
```

---

## P1. `use gemness` 트리거 패치 필요

사용자가 일반 Codex 사용 중 다음처럼 말하면:

```text
use gemness
use gemness: 이 변경사항 리뷰해줘
use gemness and cross-check this architecture
```

Codex agent가 Gemness를 사용하도록 유도해야 한다.

중요: 이건 하드 enforcement가 아니라 agent guidance다. 실제 권한/보안은 MCP config의 `enabled_tools`, approval mode, sandbox, local server 설정으로 잡는다.

가장 안정적인 반영 방식은 **AGENTS.md + Skill**을 같이 쓰는 것이다.

- AGENTS.md: 항상 읽히는 기본 규칙
- Skill: trigger phrase 기반 reusable workflow

---

## 3. `use gemness` 반영 상세 지시

## 3.1 AGENTS.md 패치

현재 `AGENTS.md`에 아래 block을 추가한다. 중복 삽입을 피하려면 marker를 사용한다.

```md
<!-- gemness-trigger:start -->

## Gemness / Gemness trigger

When the user says **"use gemness"** or clearly asks to use Gemness, treat it as an explicit request to consult Gemini CLI through the local Gemness server.

Expected behavior:

1. Prefer the MCP tools exposed by the `gemness` server.
2. If `health_check` is available, call it first when connection status is uncertain.
3. Choose the tool based on the task:
   - Use `review_current_diff` for current git diff review.
   - Use `ask_json` when a structured JSON result is needed.
   - Use `ask_text` for general second opinion, architecture critique, debugging advice, or cross-checking.
4. Treat Gemini output as advisory, not authoritative.
5. Verify Gemini's suggestions before applying them.
6. Summarize what Gemini said and what was accepted, rejected, or left unverified.
7. If the MCP server or tool is unavailable, say that Gemness is not connected and provide the next setup step instead of silently skipping Gemini.
8. Do not send secrets, private keys, credentials, or raw `.env` values to Gemini.

Trigger phrases include:

- `use gemness`
- `Use Gemness`
- `gemness로 확인`
- `Gemness로 리뷰`
- `use gemness to review`
- `use gemness for a second opinion`

<!-- gemness-trigger:end -->
```

## 3.2 Skill 추가

Codex Skills를 지원하는 환경에서는 repository-local 또는 user-local skill을 추가한다.

권장 repo-local 경로:

```text
.agents/skills/gemness/SKILL.md
```

내용:

```md
---
name: gemness
// 아래 description은 일부 UI에서 한 줄로 보일 수 있으므로 trigger words를 앞에 둔다.
description: use gemness, gemness, Gemness로 확인, Gemini second opinion 요청 시 local gemness MCP server를 사용해 Gemini CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Gemini CLI through the local MCP server.

## Procedure

1. If connection status is uncertain and `health_check` exists, call it first.
2. Select the right tool:
   - `review_current_diff` for git diff review.
   - `ask_json` for schema-constrained structured output.
   - `ask_text` for general second opinion or reasoning review.
3. Include only the necessary context.
4. Do not include secrets or credentials.
5. Treat Gemini's result as advisory.
6. Verify before applying changes.
7. Report back with:
   - what Gemness/Gemini said,
   - what you accepted,
   - what you rejected,
   - what remains uncertain.

## Failure behavior

If the MCP tools are unavailable, do not pretend Gemness was used. State that the `gemness` MCP server is not connected and suggest running the MCP health check or checking Codex MCP configuration.
```

주의: YAML front matter 안에서 `//` comment는 실제 YAML이 아니므로 코드 생성 시 제거한다. 실제 파일은 다음처럼 작성한다.

```md
---
name: gemness
description: use gemness, gemness, Gemness로 확인, Gemini second opinion 요청 시 local gemness MCP server를 사용해 Gemini CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Gemini CLI through the local MCP server.

## Procedure

1. If connection status is uncertain and `health_check` exists, call it first.
2. Select the right tool:
   - `review_current_diff` for git diff review.
   - `ask_json` for schema-constrained structured output.
   - `ask_text` for general second opinion or reasoning review.
3. Include only the necessary context.
4. Do not include secrets or credentials.
5. Treat Gemini's result as advisory.
6. Verify before applying changes.
7. Report back with:
   - what Gemness/Gemini said,
   - what you accepted,
   - what you rejected,
   - what remains uncertain.

## Failure behavior

If the MCP tools are unavailable, do not pretend Gemness was used. State that the `gemness` MCP server is not connected and suggest running the MCP health check or checking Codex MCP configuration.
```

## 3.3 설치 helper 추가

사용자가 이 MCP를 설치할 때 trigger를 자동 설치할 수 있도록 script를 추가한다.

권장 파일:

```text
scripts/install_gemness_trigger.py
```

기능:

```bash
python scripts/install_gemness_trigger.py --scope project
python scripts/install_gemness_trigger.py --scope user
python scripts/install_gemness_trigger.py --scope both
```

동작:

- `--scope project`
  - 현재 repo의 `AGENTS.md`에 marker block upsert
  - `.agents/skills/gemness/SKILL.md` 생성
- `--scope user`
  - `~/.codex/AGENTS.md`에 marker block upsert
  - `~/.agents/skills/gemness/SKILL.md` 생성
- `--scope both`
  - 둘 다 적용

중복 방지:

```text
<!-- gemness-trigger:start -->
...
<!-- gemness-trigger:end -->
```

이미 block이 있으면 replace/update한다.

테스트:

- temp dir에 AGENTS.md 없는 상태에서 생성
- 기존 AGENTS.md가 있을 때 append
- marker block이 있을 때 replace
- user scope는 `HOME`/`CODEX_HOME` temp로 monkeypatch
- skill front matter가 valid YAML-ish 형태인지 확인

---

## 4. MCP 연결 마무리 작업

### 4.1 Codex config template 정리

`docs/codex-mcp-config.example.toml`을 다음 형태로 바꾼다.

```toml
# Example Codex MCP configuration for Gemness / Gemness Observer.
# Replace placeholders with absolute local paths.

[mcp_servers.gemness]
command = "<ABSOLUTE_PATH_TO_REPO>/.venv/bin/python"
args = ["-m", "gemness.server"]
cwd = "<ABSOLUTE_PATH_TO_REPO>"
startup_timeout_sec = 10
tool_timeout_sec = 300
required = true
enabled_tools = [
  "health_check",
  "ask_text",
  "ask_json",
  "review_current_diff",
]
default_tools_approval_mode = "prompt"

[mcp_servers.gemness.tools."health_check"]
approval_mode = "approve"

[mcp_servers.gemness.tools."ask_text"]
approval_mode = "approve"

[mcp_servers.gemness.tools."ask_json"]
approval_mode = "prompt"

[mcp_servers.gemness.tools."review_current_diff"]
approval_mode = "prompt"

[mcp_servers.gemness.env]
PYTHONPATH = "<ABSOLUTE_PATH_TO_REPO>/src"
GEMNESS_MODEL = "gemini-3.1-pro-preview"
GEMNESS_OBSERVER_ENABLED = "true"
GEMNESS_OBSERVER_HOST = "127.0.0.1"
GEMNESS_OBSERVER_PORT = "0"
GEMNESS_TRANSCRIPT_DIR = ".gemness/transcripts"
GEMNESS_REDACT_RAW_BY_DEFAULT = "true"
GEMNESS_PAUSE_BEFORE_SEND = "false"
GEMNESS_TOOL_TIMEOUT_SEC = "120"
GEMNESS_COMMAND = "<ABSOLUTE_PATH_TO_GEMINI_CLI>"
GEMNESS_GEMINI_SKIP_TRUST = "false"
GEMNESS_GEMINI_APPROVAL_MODE = "plan"
GEMNESS_WORKSPACE_ROOT = "<ABSOLUTE_PATH_TO_REPO>"
GEMNESS_ALLOWED_ROOTS = "<ABSOLUTE_PATH_TO_REPO>"
```

Windows 예시는 별도로 둔다.

```toml
command = "C:\\path\\to\\repo\\.venv\\Scripts\\python.exe"
PYTHONPATH = "C:\\path\\to\\repo\\src"
GEMNESS_COMMAND = "C:\\Users\\YOU\\AppData\\Roaming\\npm\\gemini.cmd"
```

macOS/Linux 예시는 별도로 둔다.

```toml
command = "/Users/YOU/dev/gemness/.venv/bin/python"
PYTHONPATH = "/Users/YOU/dev/gemness/src"
GEMNESS_COMMAND = "/opt/homebrew/bin/gemini"
```

### 4.2 setup script 추가

권장 파일:

```text
scripts/print_codex_config.py
```

기능:

- 현재 repo absolute path 탐지
- `.venv` python path 탐지
- `gemini` command path 탐지
- OS별 path escape 적용
- paste-ready `config.toml` 출력
- `--scope user`면 `~/.codex/config.toml`에 merge option 제공하되, 기본은 print-only

### 4.3 smoke test 추가

권장 파일:

```text
scripts/mcp_smoke_test.py
```

검증 순서:

1. subprocess로 MCP server 실행
2. JSON-RPC `initialize`
3. `notifications/initialized`
4. `tools/list`
5. tool 목록 확인
6. `health_check` call
7. fake mode 또는 `--real` 옵션으로 `ask_text` call

명령 예시:

```bash
PYTHONPATH=src python scripts/mcp_smoke_test.py -- python -m gemness.server
```

real Gemini 호출은 별도 flag로만 수행한다.

```bash
PYTHONPATH=src python scripts/mcp_smoke_test.py --real -- python -m gemness.server
```

---

## 5. 총괄 구현 지시문

아래 내용을 `docs/goals/gemness-stabilization-and-connect.goal.md`로 저장하고 `/goal`에서 참조한다.

```text
/goal Stabilize Gemness Gemness Observer, finish Codex MCP connection, and add the "use gemness" trigger workflow by following @docs/goals/gemness-stabilization-and-connect.goal.md. Keep working in checkpoints and stop only when all acceptance criteria pass.
```

---

# Goal: Gemness Gemness Observer 안정화 및 연결 완료

## Objective

Stabilize the existing Gemness Observer project so that users can install it, connect it to Codex App/CLI as an MCP server, verify it with a health check, and trigger Gemini CLI usage by saying `use gemness` during ordinary Codex use.

Do not rewrite the project. Patch the current implementation.

## Non-goals

- Do not give Gemini direct file editing permission by default.
- Do not give Gemini direct shell execution permission by default.
- Do not expose observer UI outside loopback by default.
- Do not persist secrets or observer tokens in committed/packageable files.
- Do not claim Gemness was used if the MCP tool was unavailable.

## Required checkpoints

After every checkpoint, update:

```text
.codex/gemness-stabilization-progress.md
```

Use this format:

```md
## Checkpoint N: <name>

### Implemented
- ...

### Requirement self-check
- [ ] Existing MCP tools still work
- [ ] Codex config remains valid
- [ ] MCP smoke test passes or blocker recorded
- [ ] Workspace/cwd handling checked
- [ ] Observer URL behavior checked
- [ ] Token/transcript safety checked
- [ ] `use gemness` trigger checked
- [ ] Tests added or updated
- [ ] Docs updated

### Commands run
```bash
...
```

### Result
- Pass / Fail / Blocked

### Next action
- ...
```

If a checkpoint fails, fix the blocker before moving to the next checkpoint.

---

## Checkpoint 1: Baseline and test harness

1. Run existing tests with plugin autoload disabled.
2. Confirm current tool list.
3. Confirm runtime files present in the extracted zip are ignored or removed from tracked state.
4. Record baseline in `.codex/gemness-stabilization-progress.md`.

Commands:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider
```

If no smoke test exists yet, note that it will be created in Checkpoint 4.

---

## Checkpoint 2: Codex config and documentation fixes

Patch:

- `docs/codex-mcp-config.example.toml`
- `README.md`
- `docs/gemini-observer.md`

Required changes:

1. Replace `default_tools_approval_mode = "on-request"` with `"prompt"`.
2. Add `cwd`, `startup_timeout_sec`, `tool_timeout_sec`, and `required` examples.
3. Add `health_check` to planned enabled tools.
4. Change default `GEMNESS_GEMINI_SKIP_TRUST` docs to `false`.
5. Replace hard-coded personal paths with placeholders.
6. Add Windows and macOS/Linux config snippets.
7. Add a clear “How to verify in Codex” section:
   - run `codex mcp list`
   - open Codex TUI and run `/mcp`
   - ask Codex: `use gemness: run health check`

---

## Checkpoint 3: Runtime file safety

Add `.gitignore` and remove runtime artifacts from tracked/packageable outputs.

Required ignore entries:

```gitignore
__pycache__/
*.py[cod]
.venv/
*.egg-info/
.pytest_cache/
.gemness/
.codex/live-observer*/
.codex/live-observer*.json
.codex/live-observer*.pid
.codex/live-observer*.out
.codex/live-observer*.err
**/observer-token.txt
**/*.jsonl
.env
.env.*
*.pem
*.key
```

Patch `ObserverHub` so tokenized observer URLs are not persisted in raw transcript events. Persist a tokenless path/URL in events and include the tokenized URL only in MCP tool return payload.

Set token file permission to `0600` when possible.

Add tests for:

- token file permission if supported
- public redaction still works
- raw session event does not store `token=` in `observer_url`

---

## Checkpoint 4: Add `health_check`

Add tool in `server.py` and service layer.

Expose in `tools/list`:

```text
health_check
```

Return read-only diagnostics:

- server version
- Python executable
- configured tools
- observer status
- transcript dir writable
- workspace cwd
- allowed roots
- git repo status
- Gemini CLI command path
- Gemini CLI version or resolution result
- model
- trust mode
- warnings

Do not call a model in health check.

Add tests:

- tool appears in `tools/list`
- tool call returns structured result
- missing Gemini command returns warning/error, not crash
- observer URL/token behavior remains valid

---

## Checkpoint 5: Workspace root and cwd hardening

Add config:

```bash
GEMNESS_WORKSPACE_ROOT
GEMNESS_ALLOWED_ROOTS
```

Add optional `cwd` field to tool input schemas:

- `health_check`
- `ask_text`
- `ask_json`
- `review_current_diff`

Apply resolved cwd to:

- Gemini subprocess
- git diff subprocess
- health check

Add tests:

- default workspace root used
- requested cwd under allowed root accepted
- cwd outside allowed root rejected
- review_current_diff uses configured cwd, not arbitrary `Path.cwd()`

---

## Checkpoint 6: Runner and JSON robustness

Patch Gemini runner and JSON pipeline.

Required:

1. Resolve `GEMNESS_COMMAND` robustly.
2. Return clear error if Gemini CLI cannot be found.
3. Preserve required auth/proxy env vars.
4. Parse `response`, `stats`, and `error` from CLI JSON envelope.
5. Treat CLI envelope `error` as tool `status=error` or explicit warning.
6. Validate JSON Schema before Gemini call.
7. Add `repair_attempted` and `repair_succeeded` fields.
8. Keep one repair attempt maximum.

Add tests for all of the above.

---

## Checkpoint 7: Diff review safety

Add `validate_base_ref()`.

Reject:

- empty ref
- refs starting with `-`
- refs containing whitespace/control characters
- refs longer than 200 chars

Add tests:

- `HEAD` pass
- `origin/main...HEAD` pass
- `--no-index` fail
- `HEAD -- path` fail

---

## Checkpoint 8: `use gemness` trigger workflow

Implement both AGENTS.md and Skill support.

### AGENTS.md

Add or update marker block:

```md
<!-- gemness-trigger:start -->

## Gemness / Gemness trigger

When the user says **"use gemness"** or clearly asks to use Gemness, treat it as an explicit request to consult Gemini CLI through the local Gemness server.

Expected behavior:

1. Prefer the MCP tools exposed by the `gemness` server.
2. If `health_check` is available, call it first when connection status is uncertain.
3. Choose the tool based on the task:
   - Use `review_current_diff` for current git diff review.
   - Use `ask_json` when a structured JSON result is needed.
   - Use `ask_text` for general second opinion, architecture critique, debugging advice, or cross-checking.
4. Treat Gemini output as advisory, not authoritative.
5. Verify Gemini's suggestions before applying them.
6. Summarize what Gemini said and what was accepted, rejected, or left unverified.
7. If the MCP server or tool is unavailable, say that Gemness is not connected and provide the next setup step instead of silently skipping Gemini.
8. Do not send secrets, private keys, credentials, or raw `.env` values to Gemini.

Trigger phrases include:

- `use gemness`
- `Use Gemness`
- `gemness로 확인`
- `Gemness로 리뷰`
- `use gemness to review`
- `use gemness for a second opinion`

<!-- gemness-trigger:end -->
```

### Skill

Create:

```text
.agents/skills/gemness/SKILL.md
```

with:

```md
---
name: gemness
description: use gemness, gemness, Gemness로 확인, Gemini second opinion 요청 시 local gemness MCP server를 사용해 Gemini CLI에게 advisory review를 요청한다. 코드 변경 리뷰, JSON 구조화 응답, 아키텍처/디버깅 교차검증에 사용한다.
---

# Gemness Skill

Use this skill when the user says `use gemness`, mentions Gemness, or asks to consult Gemini CLI through the local MCP server.

## Procedure

1. If connection status is uncertain and `health_check` exists, call it first.
2. Select the right tool:
   - `review_current_diff` for git diff review.
   - `ask_json` for schema-constrained structured output.
   - `ask_text` for general second opinion or reasoning review.
3. Include only the necessary context.
4. Do not include secrets or credentials.
5. Treat Gemini's result as advisory.
6. Verify before applying changes.
7. Report back with what Gemness/Gemini said, what was accepted, what was rejected, and what remains uncertain.

## Failure behavior

If the MCP tools are unavailable, do not pretend Gemness was used. State that the `gemness` MCP server is not connected and suggest running the MCP health check or checking Codex MCP configuration.
```

### Install helper

Create:

```text
scripts/install_gemness_trigger.py
```

Supports:

```bash
python scripts/install_gemness_trigger.py --scope project
python scripts/install_gemness_trigger.py --scope user
python scripts/install_gemness_trigger.py --scope both
```

Behavior:

- project scope updates repo `AGENTS.md` and `.agents/skills/gemness/SKILL.md`
- user scope updates `~/.codex/AGENTS.md` and `~/.agents/skills/gemness/SKILL.md`
- use marker upsert to avoid duplicate blocks
- never overwrite unrelated AGENTS.md content

Tests:

- creates files when missing
- appends marker block when AGENTS.md exists
- replaces marker block on rerun
- writes valid skill file
- supports temp HOME/CODEX_HOME in tests

---

## Checkpoint 9: MCP smoke test and install helper

Add:

```text
scripts/mcp_smoke_test.py
scripts/print_codex_config.py
```

`mcp_smoke_test.py` must verify:

1. server launches
2. `initialize` works
3. `notifications/initialized` is accepted
4. `tools/list` includes all expected tools
5. `health_check` works
6. optional `--real` can call `ask_text`

`print_codex_config.py` must print paste-ready TOML using detected paths.

---

## Checkpoint 10: Final verification

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider
PYTHONPATH=src python scripts/mcp_smoke_test.py -- python -m gemness.server
python scripts/install_gemness_trigger.py --scope project
```

Manual Codex verification:

1. Install editable package.
2. Add MCP config to `~/.codex/config.toml` or `.codex/config.toml`.
3. Start Codex.
4. Run `/mcp` and confirm `gemness` is active.
5. Ask: `use gemness: run health check`.
6. Ask: `use gemness: give me a second opinion on this architecture`.
7. Make a tiny git diff and ask: `use gemness: review current diff`.
8. Open returned `observer_url`.
9. Confirm transcript and intervention UI work.

---

## Acceptance criteria

Complete only when all are true.

- [ ] Tests pass with documented stable command.
- [ ] MCP smoke test passes.
- [ ] `health_check` exists and works.
- [ ] Codex config example uses `default_tools_approval_mode = "prompt"`.
- [ ] Codex config example includes `cwd`.
- [ ] Codex config example includes all exposed tools in `enabled_tools`.
- [ ] `GEMNESS_GEMINI_SKIP_TRUST` defaults to false.
- [ ] Runtime token/transcript/pid/out/err files are ignored and not packaged.
- [ ] Tokenized observer URLs are not persisted in raw transcript events.
- [ ] Workspace root / cwd / allowed roots are implemented.
- [ ] `review_current_diff` uses resolved cwd.
- [ ] unsafe `base_ref` is rejected.
- [ ] invalid JSON Schema is caught before model call.
- [ ] CLI envelope `stats` and `error` are handled.
- [ ] repair result includes `repair_attempted` and `repair_succeeded`.
- [ ] `use gemness` trigger block exists in AGENTS.md.
- [ ] `.agents/skills/gemness/SKILL.md` exists.
- [ ] install helper can install/update the trigger without duplicating blocks.
- [ ] README documents how to install, connect, verify, and use `use gemness`.
- [ ] Final response includes exact commands run and their results.

---

## Final response requirements

When Codex finishes the goal, it must answer with:

1. 변경 요약
2. 변경 파일 목록
3. 테스트 명령과 결과
4. MCP smoke test 명령과 결과
5. 최종 Codex config.toml 예시
6. `use gemness` 사용법
7. Observer UI 확인 방법
8. 보안상 남은 주의점
9. 아직 구현하지 않은 streaming/live-injection 한계가 있다면 명시

---

## 6. 공식 문서 참고 링크

- Codex MCP: https://developers.openai.com/codex/mcp
- Codex config reference: https://developers.openai.com/codex/config-reference
- Codex AGENTS.md: https://developers.openai.com/codex/guides/agents-md
- Codex Skills: https://developers.openai.com/codex/skills
- Codex `/goal`: https://developers.openai.com/codex/use-cases/follow-goals
- Gemini CLI headless: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md
