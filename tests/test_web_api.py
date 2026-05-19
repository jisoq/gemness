from __future__ import annotations

import json
import re
import shutil
import subprocess
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from gemness.config import GemnessConfig
from gemness.runner import GeminiRunResult
from gemness.tools import GemnessService
from gemness.web import INDEX_HTML


class WebFakeRunner:
    def run(self, prompt, *, model, output_format, session_id, hub, cwd=None, phase=None, **kwargs):
        hub.set_status(session_id, "running", "gemini.started", {"model": model}, role="gemness", phase=phase)
        stdout = json.dumps({"response": "ok"})
        hub.append_event(session_id, "gemini.response", "gemness", {"response": stdout}, phase=phase)
        hub.append_event(session_id, "gemini.exited", "gemness", {"exit_code": 0}, phase=phase)
        return GeminiRunResult.completed(stdout)


def test_observer_api_is_loopback_local_and_exports_redacted_transcript(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, model="fake-model")
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_text("API_KEY=secret-value")
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


def test_observer_api_exports_conversation_without_public_gemini_session_id(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, model="fake-model")
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_text("hello")
        service.follow_up(result["session_id"], "continue")
        base_url = _observer_base(result["observer_url"])

        conversations = _get_json(f"{base_url}/api/conversations")
        conversation_id = conversations["conversations"][0]["conversation_id"]
        exported = _get_json(f"{base_url}/api/conversations/{conversation_id}")
        raw_exported = _get_json(f"{base_url}/api/conversations/{conversation_id}?raw=1")

        assert exported["conversation"]["conversation_id"] == result["conversation_id"]
        assert len(exported["runs"]) == 2
        assert "current_gemini_session_id" not in exported["conversation"]
        assert "gemini_session_id" not in exported["runs"][0]
        assert raw_exported["conversation"]["current_gemini_session_id"].startswith("gemness_")
    finally:
        service.shutdown()


def test_legacy_session_url_redirects_to_conversation_url(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, model="fake-model")
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_text("hello")
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
    service = GemnessService(GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0), runner=WebFakeRunner())
    try:
        result = service.ask_text("hello")
        assert result["observer_url"].startswith("http://127.0.0.1:")
    finally:
        service.shutdown()


def test_completed_session_approve_instruction_creates_follow_up(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, model="fake-model")
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_text("hello")
        base_url = _observer_base(result["observer_url"])
        posted = _post_json(
            f"{base_url}/api/sessions/{result['session_id']}/interventions",
            {"action": "approve", "instruction": "follow this up"},
        )

        assert posted["child_session_id"]
        child = service.hub.get_session(posted["child_session_id"])
        assert child["parent_session_id"] == result["session_id"]
        parent_events = service.hub.get_events(result["session_id"], raw=True)
        assert "intervention.received" in [event["type"] for event in parent_events]
        assert "intervention.applied" in [event["type"] for event in parent_events]
    finally:
        service.shutdown()


def test_observer_ui_uses_korean_labels_and_readable_transcript_renderer() -> None:
    assert "세션 목록" in INDEX_HTML
    assert "대화 기록" in INDEX_HTML
    assert "사용자 개입" in INDEX_HTML
    assert "Agents -> Gemini" in INDEX_HTML
    assert "Gemini -> Agents" in INDEX_HTML
    assert "원본 이벤트 보기" in INDEX_HTML
    assert "function buildConversationTranscript" in INDEX_HTML
    assert "function describeEventAsConversationTurn" in INDEX_HTML
    assert "function preferredLiveSession" in INDEX_HTML
    assert "function shouldHonorRequestedSession" in INDEX_HTML
    assert "function renderSessionGroup" in INDEX_HTML
    assert "function sessionTitle" in INDEX_HTML
    assert "function renderMarkdown" in INDEX_HTML
    assert "function updateInterventionPanel" in INDEX_HTML
    assert "renderConversationTurn" in INDEX_HTML
    assert "buildReadableTranscript" in INDEX_HTML
    assert "isTerminalStatus" in INDEX_HTML
    assert "isBenignGeminiStderr" in INDEX_HTML
    assert "conversation id" in INDEX_HTML
    assert "run id" in INDEX_HTML
    assert "256-color support not detected" in INDEX_HTML
    assert "Ripgrep is not available. Falling back to GrepTool." in INDEX_HTML
    assert "visibleEvents(transcript?.events || []).map(renderEvent)" not in INDEX_HTML
    assert "현재 단계:" in INDEX_HTML
    assert "전송 전" in INDEX_HTML
    assert "실행 중" in INDEX_HTML
    assert "완료 후" in INDEX_HTML
    assert "추가 지시 / follow-up 질문" in INDEX_HTML
    assert "프롬프트 전체 교체" in INDEX_HTML
    assert "중단 후 이 지시로 재시도" in INDEX_HTML
    assert "Gemini -> Agents · 응답 중" in INDEX_HTML
    assert "Live" in INDEX_HTML
    assert "History" in INDEX_HTML


def test_observer_ui_uses_root_api_without_token_query_and_sse_fallback() -> None:
    assert "const api = (path) => path;" in INDEX_HTML
    assert "encodeURIComponent(token)" not in INDEX_HTML
    assert "__GEMNESS_OBSERVER_TOKEN__" not in INDEX_HTML
    assert "source.onerror" in INDEX_HTML
    assert "setInterval(() => { loadSessions().catch(console.error); }, 1500)" in INDEX_HTML


def test_observer_ui_keeps_dashboard_url_instead_of_session_path() -> None:
    assert "let liveMode = true;" in INDEX_HTML
    assert "function canonicalizeDashboardUrl" in INDEX_HTML
    assert 'history.replaceState(null, "", "/");' in INDEX_HTML
    assert "requestedSessionId" in INDEX_HTML
    assert "`/sessions/${currentSessionId}" not in INDEX_HTML


def test_observer_root_serves_live_dashboard_without_token(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, model="fake-model")
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_text("hello")
        base_url = _observer_base(result["observer_url"])

        html_request = Request(f"{base_url}/", headers={"Accept": "text/html"})
        with urlopen(html_request, timeout=2) as response:
            html = response.read().decode("utf-8")

        assert "__GEMNESS_OBSERVER_TOKEN__" not in html
        assert result["observer_url"] == f"{base_url}/"
    finally:
        service.shutdown()


def test_observer_api_ignores_stale_url_token(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, model="fake-model")
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_text("hello")
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
    type: "gemini.response",
    ts: "2026-05-19T03:04:31Z",
    role: "gemness",
    payload: { response: JSON.stringify({ response: "보안 위험은 낮지만 테스트가 부족합니다.", stats: { tokens: { total: 12 } } }) }
  },
  {
    type: "gemini.response",
    ts: "2026-05-19T03:04:32Z",
    role: "gemness",
    payload: { response: JSON.stringify({ response: "부분 응답", error: { message: "auth failed" }, stats: { attempts: 1 } }) }
  },
  {
    type: "gemini.stderr",
    ts: "2026-05-19T03:04:33Z",
    role: "gemness",
    payload: { stderr: "Warning: 256-color support not detected.\nRipgrep is not available. Falling back to GrepTool." }
  },
  {
    type: "intervention.received",
    ts: "2026-05-19T03:05:10Z",
    role: "user",
    payload: { action: "follow_up", instruction: "테스트 관점만 다시 봐줘" }
  },
  {
    type: "session.completed",
    ts: "2026-05-19T03:05:20Z",
    role: "system",
    payload: { result: { status: "completed", text: "최종 요약입니다." } }
  }
];
const turns = buildConversationTranscript(events.filter((event) => !isBenignGeminiStderr(event)));
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

    assert [turn["title"] for turn in turns].count("Agents -> Gemini") == 1
    assert turns[0]["title"] == "Agents -> Gemini"
    assert turns[0]["direction"] == "agents_to_gemini"
    assert turns[0]["body"] == "현재 변경사항을 리뷰해줘."
    assert turns[0]["meta"]["단계"] == "Gemini에 전송됨"
    assert turns[1]["title"] == "Gemini -> Agents"
    assert turns[1]["body"] == "보안 위험은 낮지만 테스트가 부족합니다."
    assert turns[1]["meta"]["stats"]["tokens"]["total"] == 12
    assert turns[2]["title"] == "Gemini -> Agents · 오류 포함"
    assert turns[2]["severity"] == "error"
    assert turns[2]["meta"]["error"]["message"] == "auth failed"
    assert all("256-color support" not in turn["body"] for turn in turns)
    assert turns[3]["title"] == "사용자 개입"
    assert "테스트 관점만 다시 봐줘" in turns[3]["body"]
    assert turns[4]["title"] == "Observer"
    assert "최종 요약입니다." in turns[4]["body"]


def test_conversation_transcript_shows_latest_stream_delta_until_final_response(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const liveEvents = [
  {
    session_id: "s1",
    type: "gemini.delta",
    ts: "2026-05-19T03:04:20Z",
    role: "gemness",
    payload: { content: "첫", response: "첫" }
  },
  {
    session_id: "s1",
    type: "gemini.delta",
    ts: "2026-05-19T03:04:21Z",
    role: "gemness",
    payload: { content: " 응답", response: "첫 응답" }
  }
];
const liveTurns = buildConversationTranscript(liveEvents);
const finalTurns = buildConversationTranscript([
  ...liveEvents,
  {
    session_id: "s1",
    type: "gemini.response",
    ts: "2026-05-19T03:04:22Z",
    role: "gemness",
    payload: { response: JSON.stringify({ response: "첫 응답" }) }
  }
]);
console.log(JSON.stringify({
  live: liveTurns.map((turn) => ({ title: turn.title, body: turn.body })),
  final: finalTurns.map((turn) => ({ title: turn.title, body: turn.body }))
}));
"""
    script_path = tmp_path / "delta-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(completed.stdout.strip().splitlines()[-1])

    assert data["live"] == [{"title": "Gemini -> Agents · 응답 중", "body": "첫 응답"}]
    assert data["final"] == [{"title": "Gemini -> Agents", "body": "첫 응답"}]


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
console.log(JSON.stringify({ picked: picked.session_id, fallback: fallback.session_id }));
"""
    script_path = tmp_path / "live-picker-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(completed.stdout.strip().splitlines()[-1])

    assert data == {"picked": "running", "fallback": "latest"}


def test_session_list_groups_live_history_and_uses_session_title(tmp_path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for Observer UI rendering tests"

    script = _extract_index_script() + r"""
const liveHtml = renderSessionGroup("Live", [
  { session_id: "live", status: "running", title: "Observer UX 정리", tool_name: "ask_text", model: "m" }
], "");
const historyHtml = renderSessionGroup("History", [
  { session_id: "done", status: "completed", tool_name: "ask_json", model: "m" }
], "");
console.log(JSON.stringify({
  liveHasTitle: liveHtml.includes("Observer UX 정리"),
  liveHasGroup: liveHtml.includes("Live"),
  historyHasFallback: historyHtml.includes("JSON 질문"),
  historyHasGroup: historyHtml.includes("History"),
  directTitle: sessionTitle({ title: "짧은 제목", tool_name: "ask_text" }),
  fallbackTitle: sessionTitle({ tool_name: "review_current_diff" })
}));
"""
    script_path = tmp_path / "session-list-title-test.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(script_path)], capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(completed.stdout.strip().splitlines()[-1])

    assert data == {
        "liveHasTitle": True,
        "liveHasGroup": True,
        "historyHasFallback": True,
        "historyHasGroup": True,
        "directTitle": "짧은 제목",
        "fallbackTitle": "현재 diff 리뷰",
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

    assert turns[0]["title"] == "Agents -> Gemini"
    assert turns[0]["body"] == "아직 승인 전인 초안입니다."
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


def _get_json(url: str):
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers={"Accept": "application/json", "Content-Type": "application/json"}, method="POST")
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
  json: async () => String(path).includes("/api/config") ? { pause_before_send: false } : { sessions: [] },
  text: async () => ""
});
"""
        + match.group("script")
    )
