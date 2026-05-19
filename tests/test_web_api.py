from __future__ import annotations

import json
import re
import shutil
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gemness.config import GemnessConfig
from gemness.runner import GeminiRunResult
from gemness.tools import GemnessService
from gemness.web import INDEX_HTML


class WebFakeRunner:
    def run(self, prompt, *, model, output_format, session_id, hub, cwd=None, phase=None):
        hub.set_status(session_id, "running", "gemini.started", {"model": model}, role="gemness", phase=phase)
        stdout = json.dumps({"response": "ok"})
        hub.append_event(session_id, "gemini.response", "gemness", {"response": stdout}, phase=phase)
        hub.append_event(session_id, "gemini.exited", "gemness", {"exit_code": 0}, phase=phase)
        return GeminiRunResult.completed(stdout)


def test_observer_api_requires_token_and_exports_redacted_transcript(tmp_path) -> None:
    config = GemnessConfig(transcript_dir=tmp_path, observer_enabled=True, observer_port=0, model="fake-model")
    service = GemnessService(config, runner=WebFakeRunner())
    try:
        result = service.ask_text("API_KEY=secret-value")
        base_url = result["observer_url"].split("/sessions/")[0]
        token = result["observer_url"].split("token=", 1)[1]

        try:
            urlopen(f"{base_url}/api/sessions", timeout=2)
        except HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("Expected tokenless API call to fail")

        redacted = _get_json(f"{base_url}/api/sessions/{result['session_id']}/export?token={token}")
        raw = _get_json(f"{base_url}/api/sessions/{result['session_id']}/export?token={token}&raw=1")

        assert "secret-value" not in json.dumps(redacted)
        assert "API_KEY=[REDACTED]" in json.dumps(redacted)
        assert "API_KEY=secret-value" in json.dumps(raw)
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
        base_url = result["observer_url"].split("/sessions/")[0]
        token = result["observer_url"].split("token=", 1)[1]
        posted = _post_json(
            f"{base_url}/api/sessions/{result['session_id']}/interventions?token={token}",
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
    assert "function renderMarkdown" in INDEX_HTML
    assert "function updateInterventionPanel" in INDEX_HTML
    assert "renderConversationTurn" in INDEX_HTML
    assert "buildReadableTranscript" in INDEX_HTML
    assert "isTerminalStatus" in INDEX_HTML
    assert "isBenignGeminiStderr" in INDEX_HTML
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
