from __future__ import annotations

import json
import re
import shutil
import subprocess
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from gemness.config import DEFAULT_MODEL_LABEL, GemnessConfig
from gemness.runner import AgyRunResult
from gemness.tools import GemnessService
from gemness.web import INDEX_HTML


class WebFakeRunner:
    def run(self, prompt, *, session_id, hub, cwd=None, phase=None, **kwargs):
        hub.set_status(session_id, "running", "antigravity.started", {"model": DEFAULT_MODEL_LABEL, "streaming": False}, role="gemness", phase=phase)
        stdout = json.dumps({"response": "ok", "metadata": {"streaming": False, "run_id": session_id}})
        hub.append_event(session_id, "antigravity.response", "gemness", {"response": stdout, "streaming": False}, phase=phase)
        hub.append_event(session_id, "antigravity.exited", "gemness", {"exit_code": 0, "streaming": False}, phase=phase)
        return AgyRunResult.completed(stdout, metadata={"streaming": False, "run_id": session_id})


def test_observer_api_is_loopback_local_and_exports_redacted_transcript(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, workspace_root=tmp_path)
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_antigravity("API_KEY=secret-value")
        base_url = _observer_base(result["observer_url"])

        sessions = _get_json(f"{base_url}/api/sessions")
        redacted = _get_json(f"{base_url}/api/sessions/{result['session_id']}/export")
        raw = _get_json(f"{base_url}/api/sessions/{result['session_id']}/export?raw=1")

        assert sessions["sessions"][0]["session_id"] == result["session_id"]
        assert "secret-value" not in json.dumps(redacted)
        assert "API_KEY=[REDACTED]" in json.dumps(redacted)
        assert "API_KEY=secret-value" in json.dumps(raw)
    finally:
        service.shutdown()


def test_observer_api_exports_conversation_without_public_agy_conversation_id(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, workspace_root=tmp_path)
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_antigravity("hello")
        service.follow_up_antigravity(result["session_id"], "continue")
        base_url = _observer_base(result["observer_url"])

        conversations = _get_json(f"{base_url}/api/conversations")
        conversation_id = conversations["conversations"][0]["conversation_id"]
        exported = _get_json(f"{base_url}/api/conversations/{conversation_id}")
        raw_exported = _get_json(f"{base_url}/api/conversations/{conversation_id}?raw=1")

        assert exported["conversation"]["conversation_id"] == result["conversation_id"]
        assert len(exported["runs"]) == 2
        assert "current_agy_conversation_id" not in exported["conversation"]
        assert "agy_conversation_id" not in exported["runs"][0]
        assert raw_exported["conversation"]["current_agy_conversation_id"].startswith("gemness_")
    finally:
        service.shutdown()


def test_legacy_session_url_redirects_to_conversation_url(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, workspace_root=tmp_path)
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_antigravity("hello")
        base_url = _observer_base(result["observer_url"])
        request = Request(f"{base_url}/session/{result['session_id']}", headers={"Accept": "text/html"}, method="GET")
        opener = build_opener(_NoRedirect)
        try:
            opener.open(request, timeout=2)
        except Exception as exc:
            response = exc
        else:
            raise AssertionError("Expected redirect")

        assert response.code == 302
        assert f"/conversation/{result['conversation_id']}#run-{result['session_id']}" in response.headers["Location"]
    finally:
        service.shutdown()


def test_observer_server_binds_loopback(tmp_path) -> None:
    service = GemnessService(GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, workspace_root=tmp_path), runner=WebFakeRunner())
    try:
        result = service.ask_antigravity("hello")
        assert result["observer_url"].startswith("http://127.0.0.1:")
    finally:
        service.shutdown()


def test_observer_api_renames_and_deletes_conversation(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, workspace_root=tmp_path)
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_antigravity("hello")
        base_url = _observer_base(result["observer_url"])
        renamed = _patch_json(f"{base_url}/api/conversations/{result['conversation_id']}", {"title": "수정한 대화 이름"})
        sessions = _get_json(f"{base_url}/api/sessions")
        deleted = _delete_json(f"{base_url}/api/conversations/{result['conversation_id']}")
        sessions_after_delete = _get_json(f"{base_url}/api/sessions")

        assert renamed["conversation"]["title"] == "수정한 대화 이름"
        assert sessions["sessions"][0]["conversation_title"] == "수정한 대화 이름"
        assert sessions["sessions"][0]["title"] == "수정한 대화 이름"
        assert deleted == {"conversation_id": result["conversation_id"], "deleted_runs": 1}
        assert sessions_after_delete["sessions"] == []
    finally:
        service.shutdown()


def test_observer_ui_uses_korean_labels_and_readable_transcript_renderer() -> None:
    assert "세션 목록" in INDEX_HTML
    assert "대화 기록" in INDEX_HTML
    assert "사용자 개입" not in INDEX_HTML
    assert "Agents -> Antigravity" in INDEX_HTML
    assert "Antigravity -> Agents" in INDEX_HTML
    assert "원본 이벤트 보기" in INDEX_HTML
    assert "이름 변경" in INDEX_HTML
    assert "제거" in INDEX_HTML
    assert "function buildConversationTranscript" in INDEX_HTML
    assert "function describeEventAsConversationTurn" in INDEX_HTML
    assert "function preferredLiveSession" in INDEX_HTML
    assert "function shouldHonorRequestedSession" in INDEX_HTML
    assert "function groupSessionsByConversation" in INDEX_HTML
    assert "function renderSessionGroup" in INDEX_HTML
    assert "function renameSessionListItem" in INDEX_HTML
    assert "function deleteSessionListItem" in INDEX_HTML
    assert "function sessionTitle" in INDEX_HTML
    assert "function renderMarkdown" in INDEX_HTML
    assert "function updateInterventionPanel" not in INDEX_HTML
    assert "renderConversationTurn" in INDEX_HTML
    assert "buildReadableTranscript" in INDEX_HTML
    assert "isTerminalStatus" in INDEX_HTML
    assert "isBenignAntigravityStderr" in INDEX_HTML
    assert "runtimeSignal" in INDEX_HTML
    assert "function runTelemetry" in INDEX_HTML
    assert "function selectSession" in INDEX_HTML
    assert "function bindSessionListEvents" in INDEX_HTML
    assert ".status-dot.live" in INDEX_HTML
    assert "conversation id" in INDEX_HTML
    assert "run id" in INDEX_HTML
    assert "256-color support not detected" in INDEX_HTML
    assert "Ripgrep is not available. Falling back to GrepTool." in INDEX_HTML
    assert "visibleEvents(transcript?.events || []).map(renderEvent)" not in INDEX_HTML
    assert "현재 단계:" not in INDEX_HTML
    assert "보내기 전 멈춤" not in INDEX_HTML
    assert "추가 지시 / follow-up 질문" not in INDEX_HTML
    assert "프롬프트 전체 교체" not in INDEX_HTML
    assert "중단 후 이 지시로 재시도" not in INDEX_HTML
    assert "/interventions" not in INDEX_HTML
    assert "streaming" in INDEX_HTML
    assert "Live" in INDEX_HTML
    assert "History" in INDEX_HTML


def test_observer_ui_uses_root_api_without_token_query_and_sse_fallback() -> None:
    assert "const api = (path) => path;" in INDEX_HTML
    assert "encodeURIComponent(token)" not in INDEX_HTML
    assert "__GEMNESS_OBSERVER_TOKEN__" not in INDEX_HTML
    assert "source.onerror" in INDEX_HTML
    assert "refreshDashboard({automatic: true})" in INDEX_HTML
    assert "setInterval(() => { refreshDashboard({automatic: true}).catch(console.error); }, 1500)" in INDEX_HTML


def test_observer_ui_keeps_dashboard_url_instead_of_session_path() -> None:
    assert "let liveMode = true;" in INDEX_HTML
    assert "function canonicalizeDashboardUrl" in INDEX_HTML
    assert 'history.replaceState(null, "", "/");' in INDEX_HTML
    assert "requestedSessionId" in INDEX_HTML
    assert "`/sessions/${currentSessionId}" not in INDEX_HTML


def test_observer_root_serves_live_dashboard_without_token(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, workspace_root=tmp_path)
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_antigravity("hello")
        base_url = _observer_base(result["observer_url"])

        html_request = Request(f"{base_url}/", headers={"Accept": "text/html"})
        with urlopen(html_request, timeout=2) as response:
            html = response.read().decode("utf-8")

        assert "__GEMNESS_OBSERVER_TOKEN__" not in html
        assert result["observer_url"] == f"{base_url}/"
    finally:
        service.shutdown()


def test_observer_api_ignores_stale_url_token(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, workspace_root=tmp_path)
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_antigravity("hello")
        base_url = _observer_base(result["observer_url"])
        request = Request(f"{base_url}/api/sessions?token=stale-token", headers={"Accept": "application/json"})
        with urlopen(request, timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))

        assert data["sessions"][0]["session_id"] == result["session_id"]
    finally:
        service.shutdown()


def test_conversation_transcript_view_model_handles_core_events(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const events = [
  {
    type: "prompt.rendered",
    ts: "2026-05-19T03:04:17Z",
    role: "codex_mcp",
    payload: { prompt: "현재 변경사항을 리뷰해줘." }
  },
  {
    type: "prompt.sent",
    ts: "2026-05-19T03:04:18Z",
    role: "codex_mcp",
    payload: { prompt: "현재 변경사항을 리뷰해줘." }
  },
  {
    type: "antigravity.response",
    ts: "2026-05-19T03:04:31Z",
    role: "gemness",
    payload: { response: JSON.stringify({ response: "보안 위험은 낮지만 테스트가 부족합니다.", stats: { tokens: { total: 12 } } }) }
  },
  {
    type: "antigravity.response",
    ts: "2026-05-19T03:04:32Z",
    role: "gemness",
    payload: { response: JSON.stringify({ response: "부분 응답", error: { message: "auth failed" }, stats: { attempts: 1 } }) }
  },
  {
    type: "antigravity.stderr",
    ts: "2026-05-19T03:04:33Z",
    role: "gemness",
    payload: { stderr: "Warning: 256-color support not detected.\nRipgrep is not available. Falling back to GrepTool." }
  },
  {
    type: "session.completed",
    ts: "2026-05-19T03:05:20Z",
    role: "system",
    payload: { result: { status: "completed", text: "최종 요약입니다." } }
  }
];
const turns = buildConversationTranscript(events.filter((event) => !isBenignAntigravityStderr(event)));
console.log(JSON.stringify(turns.map((turn) => ({
  title: turn.title,
  speaker: turn.speaker,
  direction: turn.direction,
  body: turn.body,
  meta: turn.meta,
  severity: turn.severity || ""
}))));
"""
    script_path = tmp_path / "conversation-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    turns = json.loads(completed.stdout.strip().splitlines()[-1])

    assert [turn["title"] for turn in turns].count("Agents -> Antigravity") == 1
    assert turns[0]["title"] == "Agents -> Antigravity"
    assert turns[0]["direction"] == "agents_to_antigravity"
    assert turns[0]["body"] == "현재 변경사항을 리뷰해줘."
    assert turns[0]["meta"]["단계"] == "Antigravity에 전송됨"
    assert turns[1]["title"] == "Antigravity -> Agents"
    assert turns[1]["body"] == "보안 위험은 낮지만 테스트가 부족합니다."
    assert turns[1]["meta"]["stats"]["tokens"]["total"] == 12
    assert turns[2]["title"] == "Antigravity -> Agents · 오류 포함"
    assert turns[2]["severity"] == "error"
    assert turns[2]["meta"]["error"]["message"] == "auth failed"
    assert all("256-color support" not in turn["body"] for turn in turns)
    assert turns[3]["title"] == "Observer"
    assert "최종 요약입니다." in turns[3]["body"]


def test_observer_displays_newest_conversation_turn_first(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const events = [
  {
    type: "prompt.sent",
    ts: "2026-05-19T03:04:18Z",
    role: "codex_mcp",
    payload: { prompt: "처음 질문" }
  },
  {
    type: "antigravity.response",
    ts: "2026-05-19T03:04:31Z",
    role: "gemness",
    payload: { response: JSON.stringify({ response: "중간 답변" }) }
  },
  {
    type: "session.completed",
    ts: "2026-05-19T03:05:20Z",
    role: "system",
    payload: { result: { status: "completed", text: "최종 요약" } }
  }
];
const chronological = buildConversationTranscript(events);
const visible = displayConversationTurns(chronological);
console.log(JSON.stringify({
  chronological: chronological.map((turn) => turn.body),
  visible: visible.map((turn) => turn.body)
}));
"""
    script_path = tmp_path / "newest-first-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(completed.stdout.strip().splitlines()[-1])

    assert data["chronological"][0] == "처음 질문"
    assert data["visible"][0].startswith("최종 결과")
    assert "최종 요약" in data["visible"][0]
    assert data["visible"][-1] == "처음 질문"


def test_conversation_transcript_omits_duplicate_completed_result_summary(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const events = [
  {
    session_id: "run_1",
    type: "antigravity.response",
    ts: "2026-05-19T03:04:31Z",
    role: "gemness",
    payload: { response: JSON.stringify({ response: "동일한 최종 답변입니다." }) }
  },
  {
    session_id: "run_1",
    type: "session.completed",
    ts: "2026-05-19T03:05:20Z",
    role: "system",
    payload: { result: { status: "completed", text: "동일한 최종 답변입니다." } }
  }
];
const turns = buildConversationTranscript(events);
console.log(JSON.stringify(turns.map((turn) => ({ title: turn.title, body: turn.body, meta: turn.meta }))));
"""
    script_path = tmp_path / "duplicate-completed-summary-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    turns = json.loads(completed.stdout.strip().splitlines()[-1])

    assert turns[0]["title"] == "Antigravity -> Agents"
    assert turns[0]["body"] == "동일한 최종 답변입니다."
    assert turns[1]["title"] == "Observer"
    assert turns[1]["body"] == "최종 결과: 완료"
    assert turns[1]["meta"]["duplicate_result_summary"] == "omitted"


def test_conversation_transcript_renders_non_streaming_response_metadata(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const turns = buildConversationTranscript([
  {
    session_id: "s1",
    type: "antigravity.response",
    ts: "2026-05-19T03:04:22Z",
    role: "gemness",
    payload: { response: JSON.stringify({ response: "첫 응답", metadata: { streaming: false, auth_status: "ok" } }), streaming: false }
  }
]);
console.log(JSON.stringify(turns.map((turn) => ({ title: turn.title, body: turn.body, meta: turn.meta }))));
"""
    script_path = tmp_path / "response-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    turns = json.loads(completed.stdout.strip().splitlines()[-1])

    assert turns[0]["title"] == "Antigravity -> Agents"
    assert turns[0]["body"] == "첫 응답"
    assert turns[0]["meta"]["metadata"]["streaming"] is False


def test_conversation_transcript_renders_response_preview_without_raw_response(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const turns = buildConversationTranscript([
  {
    session_id: "s1",
    type: "antigravity.response",
    ts: "2026-05-19T03:04:22Z",
    role: "gemness",
    payload: {
      response_preview: "2026년 5월 22일입니다.",
      stdout_artifact: { kind: "text", name: "stdout.txt", bytes: 28, encoding: "utf-8" },
      metadata: { streaming: false, auth_status: "ok" },
      streaming: false
    }
  }
]);
console.log(JSON.stringify(turns.map((turn) => ({ body: turn.body, meta: turn.meta }))));
"""
    script_path = tmp_path / "preview-response-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    turns = json.loads(completed.stdout.strip().splitlines()[-1])

    assert turns[0]["body"] == "2026년 5월 22일입니다."
    assert turns[0]["meta"]["형식"] == "stdout preview"
    assert turns[0]["meta"]["artifact"]["name"] == "stdout.txt"


def test_heartbeat_events_update_runtime_signal_without_chat_turns(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const heartbeat = {
  event_id: "heartbeat-1",
  session_id: "run_1",
  type: "antigravity.heartbeat",
  ts: new Date().toISOString(),
  role: "gemness",
  payload: {
    run_id: "run_1",
    elapsed_ms: 3210,
    timeout_remaining_ms: 96000,
    pid: 123,
    capture_mode: "winpty",
    stdout_bytes: 42,
    stderr_bytes: 0,
    last_activity_ms_ago: 120
  }
};
const otherRunHeartbeat = {
  ...heartbeat,
  event_id: "heartbeat-2",
  session_id: "run_2",
  payload: { ...heartbeat.payload, run_id: "run_2", pid: 999 }
};
const completedHeartbeat = {
  ...heartbeat,
  ts: "2026-05-19T03:04:18.000Z"
};
const exited = {
  session_id: "run_1",
  type: "antigravity.exited",
  ts: "2026-05-19T03:04:22.560Z",
  payload: { duration_ms: 4560, exit_code: 0 }
};
const completed = {
  session_id: "run_1",
  type: "session.completed",
  ts: "2026-05-19T03:04:22.560Z",
  payload: { status: "completed" }
};
const runningSession = { session_id: "run_1", status: "running" };
const completedSession = { session_id: "run_1", status: "completed", duration_ms: 5000 };
const turns = buildConversationTranscript([heartbeat]);
const telemetry = runTelemetry(runningSession, [otherRunHeartbeat, heartbeat]);
const completedTelemetry = runTelemetry(completedSession, [completedHeartbeat, exited, completed]);
const signalHtml = renderRuntimeSignal(runningSession, [otherRunHeartbeat, heartbeat]);
const debugHtml = renderRawEvent(heartbeat);
console.log(JSON.stringify({
  turnCount: turns.length,
  state: telemetry.state,
  label: telemetry.label,
  details: telemetry.details,
  completedState: completedTelemetry.state,
  completedDetails: completedTelemetry.details,
  completedUsesFrozenHeartbeat: completedTelemetry.details.some((item) => item.startsWith("마지막 heartbeat")),
  completedUsesTerminalDelta: completedTelemetry.details.includes("종료 4.6초 전"),
  completedHasLiveRecency: completedTelemetry.details.some((item) => item.startsWith("최근 heartbeat")),
  signalHasLiveDot: signalHtml.includes("status-dot live"),
  signalHasPid: signalHtml.includes("pid 123"),
  signalUsesActiveRun: !signalHtml.includes("pid 999"),
  debugKeepsRawHeartbeat: debugHtml.includes("antigravity.heartbeat")
}));
"""
    script_path = tmp_path / "heartbeat-runtime-signal-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(completed.stdout.strip().splitlines()[-1])

    assert data["turnCount"] == 0
    assert data["state"] == "live"
    assert data["label"] == "Live"
    assert "pid 123" in data["details"]
    assert "경과 3.2초" in data["details"]
    assert data["completedState"] == "completed"
    assert data["completedUsesFrozenHeartbeat"] is True
    assert data["completedUsesTerminalDelta"] is True
    assert data["completedHasLiveRecency"] is False
    assert "프로세스 4.6초" in data["completedDetails"]
    assert data["signalHasLiveDot"] is True
    assert data["signalHasPid"] is True
    assert data["signalUsesActiveRun"] is True
    assert data["debugKeepsRawHeartbeat"] is True


def test_live_session_picker_prefers_newest_non_terminal_session(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const picked = preferredLiveSession([
  { session_id: "done", status: "completed" },
  { session_id: "running", status: "running" },
  { session_id: "queued", status: "queued" }
]);
const fallback = preferredLiveSession([
  { session_id: "latest", status: "completed" },
  { session_id: "older", status: "error" }
]);
console.log(JSON.stringify({ picked: picked.session_id, fallback: fallback.session_id, cancelledTerminal: isTerminalStatus("cancelled") }));
"""
    script_path = tmp_path / "live-picker-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(completed.stdout.strip().splitlines()[-1])

    assert data == {"picked": "running", "fallback": "latest", "cancelledTerminal": True}


def test_session_list_groups_live_history_and_uses_session_title(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
currentSessionId = "run_1";
const grouped = groupSessionsByConversation([
  { session_id: "run_1", conversation_id: "conv_a", conversation_title: "이름 바꾼 대화", status: "completed", title: "Observer UX 정리", tool_name: "ask_antigravity", model: "m", started_at: "2026-05-19T01:00:00Z", updated_at: "2026-05-19T01:00:01Z", turn_index: 1 },
  { session_id: "run_2", conversation_id: "conv_a", status: "completed", title: "후속 질문", tool_name: "ask_antigravity", model: "m", started_at: "2026-05-19T01:01:00Z", updated_at: "2026-05-19T01:01:01Z", turn_index: 2 },
  { session_id: "run_3", conversation_id: "conv_b", status: "completed", tool_name: "ask_antigravity_json", model: "m", started_at: "2026-05-19T01:02:00Z", updated_at: "2026-05-19T01:02:01Z", turn_index: 1 },
  { session_id: "run_4", conversation_id: "conv_c", status: "running", title: "실시간 확인", tool_name: "ask_antigravity", model: "m", started_at: "2026-05-19T01:03:00Z", updated_at: "2026-05-19T01:03:01Z", turn_index: 1 }
]);
const liveHtml = renderSessionGroup("Live", grouped.filter((session) => !isTerminalStatus(session.status)), "");
const historyHtml = renderSessionGroup("History", grouped.filter((session) => isTerminalStatus(session.status)), "");
const convA = grouped.find((session) => session.conversation_id === "conv_a");
console.log(JSON.stringify({
  liveHasTitle: liveHtml.includes("실시간 확인"),
  liveHasGroup: liveHtml.includes("Live"),
  groupedCount: grouped.length,
  convATurnCount: convA.turn_count,
  convAUsesLatestRun: convA.session_id === "run_2",
  convAUsesConversationTitle: sessionTitle(convA) === "이름 바꾼 대화",
  historyButtonCount: (historyHtml.match(/data-session=/g) || []).length,
  historyHasRename: historyHtml.includes("이름 변경") && historyHtml.includes('data-rename-kind="conversation"'),
  historyHasDelete: historyHtml.includes("제거") && historyHtml.includes('data-delete-kind="conversation"'),
  historyShowsTurns: historyHtml.includes("2턴 · Antigravity 질문"),
  historyActiveFromAnyRun: historyHtml.includes("session active"),
  historyHasFallback: historyHtml.includes("JSON 질문"),
  historyHasGroup: historyHtml.includes("History"),
  directTitle: sessionTitle({ title: "짧은 제목", tool_name: "ask_antigravity" }),
  fallbackTitle: sessionTitle({ tool_name: "review_current_diff_with_antigravity" })
}));
"""
    script_path = tmp_path / "session-list-title-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(completed.stdout.strip().splitlines()[-1])

    assert data == {
        "liveHasTitle": True,
        "liveHasGroup": True,
        "groupedCount": 3,
        "convATurnCount": 2,
        "convAUsesLatestRun": True,
        "convAUsesConversationTitle": True,
        "historyButtonCount": 2,
        "historyHasRename": True,
        "historyHasDelete": True,
        "historyShowsTurns": True,
        "historyActiveFromAnyRun": True,
        "historyHasFallback": True,
        "historyHasGroup": True,
        "directTitle": "짧은 제목",
        "fallbackTitle": "현재 변경 리뷰",
    }


def test_conversation_transcript_keeps_unsent_prompt_draft(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const events = [
  {
    type: "prompt.rendered",
    ts: "2026-05-19T03:04:17Z",
    role: "codex_mcp",
    payload: { prompt: "아직 승인 전인 초안입니다." }
  },
  {
    type: "prompt.pending_approval",
    ts: "2026-05-19T03:04:18Z",
    role: "system",
    payload: { timeout_sec: 300 }
  }
];
const turns = buildConversationTranscript(events);
console.log(JSON.stringify(turns.map((turn) => ({ title: turn.title, body: turn.body, meta: turn.meta }))));
"""
    script_path = tmp_path / "draft-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    turns = json.loads(completed.stdout.strip().splitlines()[-1])

    assert turns[0]["title"] == "Agents -> Antigravity"
    assert turns[0]["body"] == "아직 승인 전인 초안입니다."
    assert turns[0]["meta"]["단계"] == "전송 전 초안"


def test_conversation_transcript_keeps_previous_run_rendered_prompt_when_later_run_sends_ref(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const events = [
  {
    session_id: "run_1",
    type: "prompt.rendered",
    ts: "2026-05-19T03:04:17Z",
    role: "codex_mcp",
    payload: { prompt: "첫 번째 실행 초안입니다." }
  },
  {
    session_id: "run_2",
    type: "prompt.sent",
    ts: "2026-05-19T03:04:18Z",
    role: "codex_mcp",
    payload: { prompt_ref: "prompt.rendered", prompt_preview: "두 번째 실행 전송" }
  }
];
const turns = buildConversationTranscript(events);
console.log(JSON.stringify(turns.map((turn) => ({ title: turn.title, body: turn.body, meta: turn.meta }))));
"""
    script_path = tmp_path / "cross-run-prompt-ref-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    turns = json.loads(completed.stdout.strip().splitlines()[-1])

    assert turns[0]["body"] == "첫 번째 실행 초안입니다."
    assert turns[0]["meta"]["단계"] == "전송 전 초안"


def test_markdown_renderer_formats_transcript_body_safely(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const html = renderMarkdown([
  "# 제목",
  "**강조**와 `코드`",
  "- 첫 항목",
  "- [링크](https://example.com/path?q=1)",
  "<script>alert(1)</script>"
].join("\n"));
console.log(JSON.stringify({ html }));
"""
    script_path = tmp_path / "markdown-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    html = json.loads(completed.stdout.strip().splitlines()[-1])["html"]

    assert "<h3>제목</h3>" in html
    assert "<strong>강조</strong>" in html
    assert "<code>코드</code>" in html
    assert "<li>첫 항목</li>" in html
    assert '<a href="https://example.com/path?q=1" target="_blank" rel="noreferrer">링크</a>' in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>" not in html


def test_observer_ui_pauses_automatic_refresh_while_text_is_selected(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
window.getSelection = () => ({ isCollapsed: false, toString: () => "선택한 대화 내용" });
const active = hasActiveTextSelection();
window.getSelection = () => ({ isCollapsed: true, toString: () => "" });
const inactive = hasActiveTextSelection();
console.log(JSON.stringify({ active, inactive }));
"""
    script_path = tmp_path / "selection-refresh-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(completed.stdout.strip().splitlines()[-1])

    assert data == {"active": True, "inactive": False}


def test_session_selection_discards_stale_transcript_responses(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
(async () => {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const elements = new Map();
  document.getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, { id, checked: false, value: "", innerHTML: "", textContent: "", onclick: null, onchange: null });
    return elements.get(id);
  };
  document.querySelectorAll = () => [];
  const response = (payload) => ({ ok: true, json: async () => payload, text: async () => "" });
  const deferred = () => {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
  };
  const oldRequest = deferred();
  const newRequest = deferred();
  global.fetch = async (path) => {
    const url = String(path);
    if (url.includes("/api/sessions/run_old")) return await oldRequest.promise;
    if (url.includes("/api/sessions/run_new")) return await newRequest.promise;
    return response({ sessions: [] });
  };

  const oldSelection = selectSession("run_old");
  const loadingTitle = elements.get("title").textContent;
  const loadingFlow = elements.get("transcriptFlow").innerHTML;
  const newSelection = selectSession("run_new");
  newRequest.resolve(response({
    session: { session_id: "run_new", title: "새 세션", status: "completed", tool_name: "ask_antigravity" },
    events: [{ type: "prompt.sent", ts: "2026-05-19T03:04:20Z", role: "codex_mcp", payload: { prompt: "새 본문" } }]
  }));
  await newSelection;
  const titleAfterNew = elements.get("title").textContent;
  const flowAfterNew = elements.get("transcriptFlow").innerHTML;

  oldRequest.resolve(response({
    session: { session_id: "run_old", title: "이전 세션", status: "completed", tool_name: "ask_antigravity" },
    events: [{ type: "prompt.sent", ts: "2026-05-19T03:04:18Z", role: "codex_mcp", payload: { prompt: "이전 본문" } }]
  }));
  await oldSelection;
  console.log(JSON.stringify({
    loadingTitle,
    loadingFlow,
    titleAfterNew,
    flowAfterNew,
    finalTitle: elements.get("title").textContent,
    finalFlow: elements.get("transcriptFlow").innerHTML
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    script_path = tmp_path / "stale-selection-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(completed.stdout.strip().splitlines()[-1])

    assert "불러오는 중" in data["loadingTitle"]
    assert "대화 기록을 불러오는 중입니다." in data["loadingFlow"]
    assert data["titleAfterNew"] == "새 세션 · 완료"
    assert "새 본문" in data["flowAfterNew"]
    assert data["finalTitle"] == "새 세션 · 완료"
    assert "새 본문" in data["finalFlow"]
    assert "이전 본문" not in data["finalFlow"]


def test_observer_ui_marks_and_restores_raw_event_details(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const event = {
  event_id: "event-1",
  session_id: "run-1",
  type: "prompt.sent",
  ts: "2026-05-19T03:04:18Z",
  role: "codex_mcp",
  payload: { prompt: "원본 이벤트 상태 보존" }
};
const turnHtml = renderConversationTurn(describeEventAsConversationTurn(event));
const debugHtml = renderRawEvent(event);
const opened = { dataset: { rawKey: "turn:event-1" }, open: true };
const closed = { dataset: { rawKey: "turn:event-2" }, open: false };
document.querySelectorAll = (selector) => selector.includes("[open]") ? [opened] : [opened, closed];
const keys = openRawEventKeys();
opened.open = false;
restoreRawEventKeys(keys);
console.log(JSON.stringify({
  turnHasKey: turnHtml.includes('data-raw-key="turn:event-1"'),
  debugHasKey: debugHtml.includes('data-raw-key="debug:event-1"'),
  restored: opened.open,
  closedStayedClosed: !closed.open
}));
"""
    script_path = tmp_path / "raw-details-state-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(completed.stdout.strip().splitlines()[-1])

    assert data == {
        "turnHasKey": True,
        "debugHasKey": True,
        "restored": True,
        "closedStayedClosed": True,
    }


def _get_json(url: str):
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def _patch_json(url: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers={"Accept": "application/json", "Content-Type": "application/json"}, method="PATCH")
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def _delete_json(url: str):
    request = Request(url, headers={"Accept": "application/json"}, method="DELETE")
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def _observer_base(observer_url: str) -> str:
    parsed = urlparse(observer_url)
    return f"{parsed.scheme}://{parsed.netloc}"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _extract_index_script() -> str:
    match = re.search(r"<script>\n(?P<script>.*?)\n  </script>", INDEX_HTML, re.DOTALL)
    assert match is not None
    return (
        """
global.window = global;
global.location = { search: "", pathname: "/" };
global.document = {
  getElementById(id) {
    return { id, checked: false, value: "", innerHTML: "", textContent: "", onclick: null, onchange: null };
  },
  querySelectorAll() { return []; }
};
global.navigator = { clipboard: { writeText: async () => {} } };
global.confirm = () => false;
global.EventSource = function EventSource() {};
global.fetch = async (path) => ({
  ok: true,
  json: async () => ({ sessions: [] }),
  text: async () => ""
});
"""
        + match.group("script")
    )
