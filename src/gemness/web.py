from __future__ import annotations

import json
import queue
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .observer import ObserverHub


class ObserverWebServer:
    def __init__(self, hub: ObserverHub, host: str, port: int) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Observer web server only supports loopback hosts")
        self.hub = hub
        self.httpd = _HubHTTPServer((host, port), _Handler, hub)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="gemini-observer-web", daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


class _HubHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_cls: type[BaseHTTPRequestHandler], hub: ObserverHub) -> None:
        self.hub = hub
        super().__init__(server_address, handler_cls)


class _Handler(BaseHTTPRequestHandler):
    server: _HubHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        parts = _parts(path)
        if len(parts) == 2 and parts[0] == "session":
            session = self.server.hub.sessions.get(parts[1])
            if session is not None and session.conversation_id:
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", f"/conversation/{quote(session.conversation_id)}#run-{quote(session.session_id)}")
                self.end_headers()
                return
            self._html(INDEX_HTML)
            return
        if path == "/" or path.startswith("/sessions/") or path.startswith("/conversation/"):
            self._html(INDEX_HTML)
            return
        if path == "/api/sessions":
            self._json({"sessions": self.server.hub.list_sessions()})
            return
        if path == "/api/conversations":
            self._json({"conversations": self.server.hub.list_conversations()})
            return
        if path == "/api/config":
            self._json({"pause_before_send": self.server.hub.pause_before_send, "redact_raw_by_default": True})
            return
        if path == "/api/events":
            self._sse(raw=_truthy(query.get("raw", ["0"])[0]))
            return
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "sessions":
            session_id = parts[2]
            raw = _truthy(query.get("raw", ["0"])[0])
            if len(parts) == 3:
                self._json(self.server.hub.export_transcript(session_id, raw=raw))
                return
            if len(parts) == 4 and parts[3] == "export":
                data = self.server.hub.export_transcript(session_id, raw=raw)
                body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename=gemini-session-{quote(session_id)}.json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "conversations":
            conversation_id = parts[2]
            raw = _truthy(query.get("raw", ["0"])[0])
            if len(parts) == 3:
                self._json(self.server.hub.export_conversation(conversation_id, raw=raw))
                return
            if len(parts) == 4 and parts[3] == "export":
                data = self.server.hub.export_conversation(conversation_id, raw=raw)
                body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename=gemness-conversation-{quote(conversation_id)}.json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        payload = self._read_json()
        path = parsed.path
        if path == "/api/config":
            if "pause_before_send" in payload:
                self.server.hub.pause_before_send = bool(payload["pause_before_send"])
            self._json({"pause_before_send": self.server.hub.pause_before_send})
            return
        parts = _parts(path)
        if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "interventions":
            session_id = parts[2]
            action = str(payload.get("action") or "")
            instruction = payload.get("instruction")
            prompt = payload.get("prompt")
            session = self.server.hub.sessions.get(session_id)
            if session is None:
                self._json({"error": "unknown session"}, HTTPStatus.NOT_FOUND)
                return
            terminal_statuses = {"completed", "valid", "invalid", "error"}
            should_follow_up = action == "follow_up" or (
                session.status in terminal_statuses and action in {"approve", "add_instruction", "edit_prompt"}
            )
            if should_follow_up:
                follow_up_text = str(instruction or prompt or "").strip()
                if not follow_up_text:
                    self._json({"error": "follow-up instruction required"}, HTTPStatus.BAD_REQUEST)
                    return
                self.server.hub.add_intervention(session_id, action, instruction=instruction)
                self.server.hub.pop_intervention(session_id, {action})
                if self.server.hub.service is None:
                    self._json({"error": "service unavailable"}, HTTPStatus.CONFLICT)
                    return
                child_session_id = self.server.hub.service.start_follow_up(session_id, follow_up_text)
                child = self.server.hub.sessions.get(child_session_id)
                self._json({"child_session_id": child_session_id, "conversation_id": child.conversation_id if child else None})
                return
            intervention = self.server.hub.add_intervention(session_id, action, instruction=instruction, prompt=prompt)
            self._json({"intervention": intervention.to_dict()})
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        return True

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body or "{}")

    def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, *, raw: bool) -> None:
        subscriber = self.server.hub.bus.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    event = subscriber.get(timeout=15)
                    if raw:
                        event = self.server.hub.get_events(event["session_id"], raw=True)[-1]
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.hub.bus.unsubscribe(subscriber)


def _parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def _truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


INDEX_HTML = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gemness 관찰자</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --panel-2: #eef2f6;
      --text: #20242a;
      --muted: #5d6875;
      --line: #d8dee7;
      --accent: #126d69;
      --bad: #a62d2d;
      --warn: #956300;
      --good: #157348;
      --agents: #0d6b72;
      --gemini: #6650a4;
      --observer: #59636f;
      --user: #8a5600;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111418;
        --panel: #181d22;
        --panel-2: #222a32;
        --text: #edf1f5;
        --muted: #9da8b4;
        --line: #303943;
        --accent: #43b7ac;
        --bad: #ff7777;
        --warn: #e3b247;
        --good: #61d394;
        --agents: #5dd4dd;
        --gemini: #c6b8ff;
        --observer: #aeb8c4;
        --user: #ffd27d;
      }
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); }
    button, textarea, input { font: inherit; }
    button {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      padding: 7px 10px;
      cursor: pointer;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: white; }
    .layout { display: grid; grid-template-columns: 320px minmax(0, 1fr) 340px; min-height: 100vh; }
    aside, main, .intervention { min-width: 0; padding: 16px; }
    aside { border-right: 1px solid var(--line); background: var(--panel); }
    .intervention { border-left: 1px solid var(--line); background: var(--panel); }
    h1, h2 { margin: 0 0 12px; font-size: 16px; }
    .toolbar, .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .session-list { display: grid; gap: 8px; }
    .session-group { display: grid; gap: 8px; }
    .session-group + .session-group { margin-top: 16px; }
    .session-group-title {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .session {
      width: 100%;
      text-align: left;
      display: grid;
      gap: 4px;
      background: var(--panel-2);
    }
    .session-title { font-weight: 700; overflow-wrap: anywhere; }
    .session.active { outline: 2px solid var(--accent); }
    .meta { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .badge { border-radius: 999px; padding: 2px 8px; font-size: 12px; background: var(--panel); border: 1px solid var(--line); }
    .badge.valid, .badge.completed { color: var(--good); }
    .badge.invalid, .badge.error, .badge.cancelled { color: var(--bad); }
    .badge.repairing, .badge.running, .badge.sending { color: var(--warn); }
    .summary-bar {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 14px;
      display: grid;
      gap: 8px;
    }
    .summary-grid { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; }
    .summary-item { min-width: 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .summary-item strong { color: var(--text); font-weight: 600; }
    .transcript-shell {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px 0;
      overflow: hidden;
    }
    .transcript-flow {
      display: grid;
      gap: 0;
      max-width: 980px;
      margin: 0 auto;
      padding: 0 16px;
    }
    .turn {
      display: grid;
      grid-template-columns: 88px minmax(0, 1fr);
      gap: 14px;
      padding: 10px 0 14px;
      border-bottom: 1px solid color-mix(in srgb, var(--line), transparent 45%);
    }
    .turn:last-child { border-bottom: 0; }
    .turn-time { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; padding-top: 2px; }
    .turn-content { min-width: 0; }
    .turn-title { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; font-weight: 700; }
    .turn-title::before { content: ""; width: 9px; height: 9px; border-radius: 50%; background: var(--observer); flex: 0 0 auto; }
    .turn.agents .turn-title { color: var(--agents); }
    .turn.agents .turn-title::before { background: var(--agents); }
    .turn.gemini .turn-title { color: var(--gemini); }
    .turn.gemini .turn-title::before { background: var(--gemini); }
    .turn.user .turn-title { color: var(--user); }
    .turn.user .turn-title::before { background: var(--user); }
    .turn.error .turn-title { color: var(--bad); }
    .turn.error .turn-title::before { background: var(--bad); }
    .turn.warn .turn-title { color: var(--warn); }
    .turn.warn .turn-title::before { background: var(--warn); }
    .turn-body {
      max-width: 78ch;
      margin: 0;
      line-height: 1.62;
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .turn-body p { margin: 0 0 0.7em; }
    .turn-body p:last-child { margin-bottom: 0; }
    .turn-body h3, .turn-body h4 {
      margin: 0.9em 0 0.35em;
      line-height: 1.35;
      font-size: 15px;
    }
    .turn-body h3:first-child, .turn-body h4:first-child { margin-top: 0; }
    .turn-body ul, .turn-body ol { margin: 0.4em 0 0.75em; padding-left: 1.35em; }
    .turn-body li { margin: 0.2em 0; }
    .turn-body blockquote {
      margin: 0.6em 0;
      padding-left: 0.9em;
      border-left: 3px solid var(--line);
      color: var(--muted);
    }
    .turn-body code {
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 0.92em;
      background: var(--bg);
      border: 1px solid var(--line);
      border-radius: 4px;
      padding: 0.05em 0.3em;
    }
    .turn-body pre {
      margin: 0.6em 0 0.8em;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--bg);
      overflow-x: auto;
    }
    .turn-body pre code {
      display: block;
      border: 0;
      padding: 0;
      background: transparent;
      font-size: 12px;
    }
    .turn-body a { color: var(--accent); }
    .turn-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .turn-meta span { border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; background: var(--bg); overflow-wrap: anywhere; }
    details.raw-event {
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--bg);
    }
    details.raw-event summary { cursor: pointer; padding: 7px 9px; color: var(--muted); font-size: 12px; }
    pre { margin: 0; padding: 10px; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; line-height: 1.45; }
    .prose { margin: 0; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
    .raw-block { border: 1px solid var(--line); border-radius: 6px; background: var(--bg); }
    textarea { width: 100%; min-height: 120px; resize: vertical; background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 8px; }
    label { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; }
    .stack { display: grid; gap: 10px; }
    .help { color: var(--muted); font-size: 12px; line-height: 1.5; margin: 0; }
    .field { display: grid; gap: 5px; }
    .field-label { color: var(--text); font-weight: 700; font-size: 13px; }
    .intervention-status { border: 1px solid var(--line); border-radius: 8px; padding: 9px 10px; background: var(--panel-2); line-height: 1.5; }
    .action-section { border-top: 1px solid var(--line); padding-top: 10px; display: grid; gap: 7px; }
    .action-section h3 { margin: 0; font-size: 13px; }
    .button-grid { display: grid; gap: 7px; }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .findings { display: grid; gap: 8px; margin-bottom: 12px; }
    .finding { border-left: 3px solid var(--accent); padding: 8px 10px; background: var(--panel); }
    .debug-panel { margin-top: 14px; }
    .debug-panel > summary { cursor: pointer; color: var(--muted); padding: 8px 0; }
    @media (max-width: 1100px) {
      .layout { grid-template-columns: 1fr; }
      aside, .intervention { border: 0; border-bottom: 1px solid var(--line); }
    }
    @media (max-width: 700px) {
      aside, main, .intervention { padding: 12px; }
      .turn { grid-template-columns: 1fr; gap: 5px; }
      .turn-time { font-size: 11px; }
      .transcript-flow { padding: 0 12px; }
      .turn-body { max-width: 100%; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <div class="toolbar">
        <h1>세션 목록</h1>
        <button id="refresh">새로고침</button>
      </div>
      <div class="toolbar">
        <label><input id="pause" type="checkbox"> 보내기 전 멈춤</label>
        <label><input id="raw" type="checkbox"> 원본 보기</label>
      </div>
      <div id="sessionList" class="session-list"></div>
    </aside>
    <main>
      <div id="sessionSummary" class="summary-bar">
        <div class="toolbar">
          <h2 id="title">대화 기록</h2>
          <button id="copy">기록 복사</button>
          <button id="export">JSON 내보내기</button>
        </div>
        <div id="summaryGrid" class="summary-grid"></div>
      </div>
      <div id="review" class="findings"></div>
      <section class="transcript-shell" aria-label="대화 기록">
        <div id="transcriptFlow" class="transcript-flow"></div>
      </section>
      <details class="debug-panel">
        <summary>원본 이벤트 / 디버그 정보</summary>
        <div id="debugEvents"></div>
      </details>
    </main>
    <section class="intervention">
      <h2>사용자 개입</h2>
      <div class="stack">
        <div id="interventionStatus" class="intervention-status">세션을 선택하면 현재 단계에서 가능한 개입이 표시됩니다.</div>
        <div class="field">
          <div class="field-label">추가 지시 / follow-up 질문</div>
          <p id="instructionHelp" class="help">전송 전에는 기존 프롬프트 뒤에 붙고, 실행 중에는 중단 후 재시도 지시가 되며, 완료 후에는 후속 질문이 됩니다.</p>
          <textarea id="instructionBox" placeholder="예: 테스트 관점만 다시 봐줘"></textarea>
        </div>
        <div class="field">
          <div class="field-label">프롬프트 전체 교체</div>
          <p class="help">Gemini로 보내기 전 단계에서만 사용합니다. 기존 프롬프트를 이 내용으로 완전히 바꿉니다.</p>
          <textarea id="promptBox" placeholder="전송 전 상태에서만 사용할 전체 프롬프트"></textarea>
        </div>
        <div class="action-section" data-section="before-send">
          <h3>전송 전</h3>
          <p class="help">승인 대기 중인 프롬프트를 보내거나, 보내기 전에 내용을 바꿀 때 사용합니다.</p>
          <div class="button-grid">
            <button class="primary" data-action="approve">승인하고 Gemini에 전송</button>
            <button data-action="edit_prompt">전체 프롬프트로 교체</button>
            <button data-action="add_instruction">추가 지시를 붙임</button>
          </div>
        </div>
        <div class="action-section" data-section="running">
          <h3>실행 중</h3>
          <p class="help">Gemini CLI에는 실시간 주입을 하지 않습니다. 필요하면 현재 실행을 끊고 지시를 포함해 새로 시도합니다.</p>
          <div class="button-grid">
            <button data-action="interrupt_retry">중단 후 이 지시로 재시도</button>
            <button data-action="cancel">세션 취소</button>
          </div>
        </div>
        <div class="action-section" data-section="after-complete">
          <h3>완료 후</h3>
          <p class="help">기존 대화 요약을 이어받아 새 follow-up 세션을 만듭니다.</p>
          <div class="button-grid">
            <button data-action="follow_up">후속 질문으로 이어가기</button>
          </div>
        </div>
      </div>
    </section>
  </div>
  <script>
    const explicitSessionPath = location.pathname.startsWith("/sessions/");
    const explicitConversationPath = location.pathname.startsWith("/conversation/");
    let requestedSessionId = explicitSessionPath ? location.pathname.split("/").filter(Boolean).pop() : "";
    let requestedConversationId = explicitConversationPath ? location.pathname.split("/").filter(Boolean).pop() : "";
    let liveMode = true;
    let currentSessionId = requestedSessionId || "";
    let sessions = [];
    let transcript = null;
    let source = null;
    let pollTimer = null;
    if (explicitSessionPath) canonicalizeDashboardUrl();

    const statusLabels = {
      queued: "대기 중",
      waiting_for_user_approval: "승인 대기",
      sending: "전송 중",
      running: "실행 중",
      repairing: "복구 중",
      valid: "유효함",
      invalid: "유효하지 않음",
      error: "오류",
      cancelled: "취소됨",
      completed: "완료"
    };
    const toolLabels = {
      ask_text: "텍스트 질문",
      ask_json: "JSON 질문",
      review_current_diff: "현재 diff 리뷰"
    };
    const roleLabels = {
      codex_mcp: "Codex/MCP",
      gemness: "Gemini CLI",
      user: "사용자",
      system: "시스템"
    };
    const eventLabels = {
      "session.created": "세션 생성",
      "prompt.rendered": "프롬프트 준비",
      "prompt.redacted": "민감정보 가림",
      "prompt.pending_approval": "전송 승인 대기",
      "prompt.sent": "프롬프트 전송",
      "gemini.started": "Gemini 실행 시작",
      "gemini.delta": "Gemini 응답 조각",
      "gemini.response": "Gemini 응답",
      "gemini.stderr": "Gemini 경고",
      "gemini.stream_error": "Gemini 스트림 오류",
      "gemini.tool_use": "Gemini 도구 요청",
      "gemini.tool_result": "Gemini 도구 결과",
      "gemini.exited": "Gemini 종료",
      "json.extracted": "JSON 후보 추출",
      "json.parse_failed": "JSON 파싱 실패",
      "json.validation_failed": "스키마 검증 실패",
      "json.validation_passed": "스키마 검증 성공",
      "repair.started": "응답 복구 시작",
      "repair.prompt_sent": "복구 프롬프트 전송",
      "repair.response": "복구 응답",
      "repair.validation_passed": "복구 검증 성공",
      "repair.validation_failed": "복구 검증 실패",
      "intervention.received": "개입 수신",
      "intervention.applied": "개입 적용",
      "run.command": "실행 명령",
      "conversation.native_session_rotated": "Gemini native session 회전",
      "session.completed": "세션 완료",
      "session.cancelled": "세션 취소",
      "session.error": "세션 오류"
    };

    const qs = (id) => document.getElementById(id);
    const api = (path) => path;

    function canonicalizeDashboardUrl() {
      if (typeof history !== "undefined" && typeof history.replaceState === "function") {
        history.replaceState(null, "", "/");
      }
    }
    function recoverFromAuthFailure() {
      const target = location.pathname || "/";
      try {
        const key = `gemness-observer-auth-reload:${target}`;
        if (window.sessionStorage?.getItem(key) === "1") return;
        window.sessionStorage?.setItem(key, "1");
      } catch {
        // If storage is unavailable, a single clean reload is still the best recovery path.
      }
      if (typeof location.replace === "function") location.replace(target);
    }
    function markAuthHealthy() {
      try {
        window.sessionStorage?.removeItem(`gemness-observer-auth-reload:${location.pathname || "/"}`);
      } catch {
        // Storage may be disabled in private contexts.
      }
    }
    async function getJson(path) {
      const response = await fetch(api(path));
      if (!response.ok) {
        const message = await response.text();
        if (response.status === 401) recoverFromAuthFailure();
        throw new Error(message);
      }
      markAuthHealthy();
      return await response.json();
    }
    async function postJson(path, body) {
      const response = await fetch(api(path), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
      });
      if (!response.ok) {
        const message = await response.text();
        if (response.status === 401) recoverFromAuthFailure();
        throw new Error(message);
      }
      markAuthHealthy();
      return await response.json();
    }
    async function loadSessions() {
      const data = await getJson("/api/sessions");
      sessions = data.sessions || [];
      if (liveMode) {
        const requested = requestedSessionId
          ? sessions.find((session) => session.session_id === requestedSessionId)
          : requestedConversationId
            ? sessions.find((session) => session.conversation_id === requestedConversationId)
            : null;
        const preferred = preferredLiveSession(sessions);
        currentSessionId = shouldHonorRequestedSession(requested, preferred) ? requested.session_id : preferred?.session_id || "";
        if (sessions.length) {
          requestedSessionId = "";
          requestedConversationId = "";
        }
      } else if (!currentSessionId && sessions[0]) {
        currentSessionId = sessions[0].session_id;
      } else if (currentSessionId && !sessions.some((session) => session.session_id === currentSessionId)) {
        liveMode = true;
        const preferred = preferredLiveSession(sessions);
        currentSessionId = preferred?.session_id || "";
      }
      renderSessions();
      if (currentSessionId) await loadTranscript();
      else renderTranscript();
    }
    function preferredLiveSession(sessionItems) {
      return (sessionItems || []).find((session) => !isTerminalStatus(session.status)) || sessionItems?.[0] || null;
    }
    function shouldHonorRequestedSession(requested, preferred) {
      return !!requested && (!preferred || isTerminalStatus(preferred.status));
    }
    async function loadTranscript() {
      const raw = qs("raw").checked ? "1" : "0";
      const baseTranscript = await getJson(`/api/sessions/${currentSessionId}?raw=${raw}`);
      transcript = await loadConversationBundle(baseTranscript, raw);
      renderTranscript();
    }
    async function loadConversationBundle(baseTranscript, raw) {
      const activeSession = baseTranscript.session || {};
      if (activeSession.conversation_id) {
        const conversation = await getJson(`/api/conversations/${activeSession.conversation_id}?raw=${raw}`);
        const activeRun = (conversation.runs || []).find((run) => run.session_id === activeSession.session_id) || activeSession;
        return {
          session: activeRun,
          conversation: conversation.conversation,
          runs: conversation.runs || [],
          events: (conversation.events || []).sort((a, b) => String(a.ts || "").localeCompare(String(b.ts || ""))),
          related_sessions: conversation.runs || []
        };
      }
      const rootSessionId = activeSession.parent_session_id || activeSession.session_id;
      let rootTranscript = baseTranscript;
      if (activeSession.parent_session_id) {
        rootTranscript = await getJson(`/api/sessions/${activeSession.parent_session_id}?raw=${raw}`);
      }
      const childSessions = sessions
        .filter((session) => session.parent_session_id === rootSessionId)
        .sort((a, b) => String(a.started_at || "").localeCompare(String(b.started_at || "")));
      const childTranscripts = await Promise.all(
        childSessions.map((session) => getJson(`/api/sessions/${session.session_id}?raw=${raw}`).catch(() => null))
      );
      const allEvents = [rootTranscript, ...childTranscripts.filter(Boolean)]
        .flatMap((item) => item.events || [])
        .sort((a, b) => String(a.ts || "").localeCompare(String(b.ts || "")));
      return {
        session: activeSession,
        events: allEvents,
        related_sessions: [rootTranscript.session, ...childTranscripts.filter(Boolean).map((item) => item.session)].filter(Boolean)
      };
    }
    function renderSessions() {
      const liveSessions = sessions.filter((session) => !isTerminalStatus(session.status));
      const historySessions = sessions.filter((session) => isTerminalStatus(session.status));
      qs("sessionList").innerHTML = [
        renderSessionGroup("Live", liveSessions, "실행 중인 세션이 없습니다."),
        renderSessionGroup("History", historySessions, "이전 세션이 없습니다.")
      ].join("");
      document.querySelectorAll("[data-session]").forEach((button) => {
        button.onclick = async () => {
          liveMode = false;
          currentSessionId = button.dataset.session;
          canonicalizeDashboardUrl();
          renderSessions();
          await loadTranscript();
        };
      });
    }
    function renderSessionGroup(label, items, emptyText) {
      const body = items.length ? items.map(renderSessionButton).join("") : `<p class="help">${escapeHtml(emptyText)}</p>`;
      return `<section class="session-group"><div class="session-group-title">${escapeHtml(label)}</div>${body}</section>`;
    }
    function renderSessionButton(s) {
      return `
        <button class="session ${s.session_id === currentSessionId ? "active" : ""}" data-session="${s.session_id}">
          <span><span class="session-title">${escapeHtml(sessionTitle(s))}</span> <span class="badge ${escapeHtml(s.status)}">${escapeHtml(statusLabel(s.status))}</span></span>
          <span class="meta">${escapeHtml(toolLabel(s.tool_name))}</span>
          <span class="meta">${escapeHtml(s.model || "")}</span>
          <span class="meta">${escapeHtml(formatDate(s.started_at))}${s.duration_ms ? ` · ${formatDuration(s.duration_ms)}` : ""}</span>
        </button>
      `;
    }
    function renderTranscript() {
      const session = transcript?.session || {};
      qs("title").textContent = session.session_id ? `${sessionTitle(session)} · ${statusLabel(session.status)}` : "대화 기록";
      renderReview(transcript?.events || []);
      renderSessionSummary(session, transcript?.events || []);
      updateInterventionPanel(session);
      const turns = buildConversationTranscript(visibleEvents(transcript?.events || []));
      qs("transcriptFlow").innerHTML = turns.length ? turns.map(renderConversationTurn).join("") : `<p class="prose">아직 표시할 대화가 없습니다.</p>`;
      qs("debugEvents").innerHTML = (transcript?.events || []).map(renderRawEvent).join("");
      const lastPrompt = [...(transcript?.events || [])].reverse().find((e) => e.type === "prompt.rendered");
      if (lastPrompt && !qs("promptBox").value) qs("promptBox").value = lastPrompt.payload.prompt || "";
    }
    function renderSessionSummary(session, events) {
      const cwd = findCwd(events);
      const conversation = transcript?.conversation || {};
      qs("summaryGrid").innerHTML = [
        ["제목", sessionTitle(session)],
        ["도구", toolLabel(session.tool_name)],
        ["모델", session.model || "알 수 없음"],
        ["상태", statusLabel(session.status)],
        ["시작", formatDate(session.started_at)],
        ["종료", formatDate(session.completed_at)],
        ["cwd", cwd || "기록 없음"],
        ["conversation id", session.conversation_id || conversation.conversation_id || "없음"],
        ["run id", session.run_id || session.session_id || "없음"],
        ["turn", session.turn_index || "없음"],
        ["raw 보기", qs("raw").checked ? "켜짐" : "꺼짐"]
      ].map(([label, value]) => `<span class="summary-item"><strong>${escapeHtml(label)}:</strong> ${escapeHtml(value)}</span>`).join("");
    }
    function renderConversationTurn(turn) {
      const meta = Object.entries(turn.meta || {})
        .filter(([, value]) => value !== undefined && value !== null && value !== "")
        .map(([key, value]) => `<span>${escapeHtml(metaLabel(key))}: ${escapeHtml(formatMetaValue(value))}</span>`)
        .join("");
      return `
        <article class="turn ${escapeHtml(turn.speaker)} ${escapeHtml(turn.severity || "")}">
          <div class="turn-time">${escapeHtml(formatTime(turn.timestamp))}</div>
          <div class="turn-content">
            <div class="turn-title">${escapeHtml(turn.title)}</div>
            <div class="turn-body">${renderMarkdown(turn.body || "")}</div>
            ${meta ? `<div class="turn-meta">${meta}</div>` : ""}
            <details class="raw-event">
              <summary>원본 이벤트 보기</summary>
              <pre>${escapeHtml(JSON.stringify(turn.rawEvent || {}, null, 2))}</pre>
            </details>
          </div>
        </article>
      `;
    }
    function renderRawEvent(event) {
      return `
        <details class="raw-event">
          <summary>${escapeHtml(formatTime(event.ts))} · ${escapeHtml(eventLabel(event.type))}</summary>
          <pre>${escapeHtml(JSON.stringify(event, null, 2))}</pre>
        </details>
      `;
    }
    function visibleEvents(events) {
      if (qs("raw").checked) return events;
      return events.filter((event) => !isBenignGeminiStderr(event));
    }
    function renderReview(events) {
      const completed = [...events].reverse().find((e) => e.type === "session.completed" && e.payload?.result?.data?.findings);
      if (!completed) {
        qs("review").innerHTML = "";
        return;
      }
      const data = completed.payload.result.data;
      qs("review").innerHTML = `
        <div class="finding"><strong>리뷰 결론: ${escapeHtml(verdictLabel(data.verdict))}</strong><br>${escapeHtml(data.summary || "")}</div>
        ${(data.findings || []).map((f) => `
          <div class="finding">
            <strong>${escapeHtml(severityLabel(f.severity || "info"))}: ${escapeHtml(f.title || "")}</strong>
            <div class="meta">${escapeHtml(f.file || "")} ${escapeHtml(f.line_hint || "")}</div>
            <div>${escapeHtml(f.explanation || "")}</div>
          </div>
        `).join("")}
      `;
    }
    async function sendIntervention(action) {
      if (!currentSessionId) return;
      const currentSession = sessions.find((session) => session.session_id === currentSessionId) || transcript?.session || {};
      const body = {action};
      const instruction = qs("instructionBox").value.trim();
      const prompt = qs("promptBox").value.trim();
      if (action === "edit_prompt" && !prompt) {
        alert("프롬프트 전체 교체에는 교체할 프롬프트가 필요합니다.");
        return;
      }
      if (["add_instruction", "interrupt_retry", "follow_up"].includes(action) && !instruction) {
        alert("이 작업에는 추가 지시 / follow-up 질문이 필요합니다.");
        return;
      }
      if (instruction) body.instruction = instruction;
      if (prompt && action === "edit_prompt") body.prompt = prompt;
      const result = await postJson(`/api/sessions/${currentSessionId}/interventions`, body);
      if (result.child_session_id) currentSessionId = result.child_session_id;
      await loadSessions();
    }
    function updateInterventionPanel(session) {
      const status = session.status || "";
      const waiting = status === "waiting_for_user_approval";
      const beforeSend = ["queued", "waiting_for_user_approval"].includes(status);
      const running = ["sending", "running", "repairing"].includes(status);
      const terminal = isTerminalStatus(status);
      const statusText = terminal
        ? "완료된 세션입니다. 추가 지시 / follow-up 질문을 입력하고 후속 질문으로 이어가세요."
        : running
          ? "Gemini가 실행 중입니다. 새 지시는 즉시 주입되지 않으며, 중단 후 재시도로만 반영됩니다."
          : waiting
            ? "전송 승인 대기 중입니다. 전체 프롬프트를 교체하거나 지시를 붙인 뒤 승인할 수 있습니다."
            : beforeSend
              ? "아직 Gemini로 보내기 전입니다. 프롬프트를 조정하거나 취소할 수 있습니다."
              : "현재 단계에서 가능한 개입만 활성화됩니다.";
      qs("interventionStatus").textContent = `현재 단계: ${statusLabel(status)}. ${statusText}`;
      setActionEnabled("approve", waiting);
      setActionEnabled("edit_prompt", beforeSend);
      setActionEnabled("add_instruction", beforeSend);
      setActionEnabled("cancel", beforeSend || running);
      setActionEnabled("interrupt_retry", running);
      setActionEnabled("follow_up", terminal);
      qs("promptBox").disabled = !beforeSend;
      qs("instructionHelp").textContent = terminal
        ? "완료 후에는 이 내용이 새 follow-up 질문으로 사용됩니다."
        : running
          ? "실행 중에는 이 내용이 중단 후 재시도 지시로 사용됩니다."
          : "전송 전에는 이 내용이 기존 프롬프트 뒤에 추가됩니다.";
    }
    function setActionEnabled(action, enabled) {
      document.querySelectorAll(`[data-action="${action}"]`).forEach((button) => {
        button.disabled = !enabled;
      });
    }
    function openEvents() {
      if (source) source.close();
      const raw = qs("raw").checked ? "1" : "0";
      source = new EventSource(api(`/api/events?raw=${raw}`));
      source.onmessage = async () => { await loadSessions(); };
      source.onerror = async () => { await loadSessions().catch(console.error); };
      if (!pollTimer) {
        pollTimer = setInterval(() => { loadSessions().catch(console.error); }, 1500);
        if (typeof pollTimer.unref === "function") pollTimer.unref();
      }
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
      }[char]));
    }
    function renderMarkdown(value) {
      const lines = String(value ?? "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
      const html = [];
      let paragraph = [];
      const flushParagraph = () => {
        if (!paragraph.length) return;
        html.push(`<p>${paragraph.map(renderInlineMarkdown).join("<br>")}</p>`);
        paragraph = [];
      };
      for (let index = 0; index < lines.length; index += 1) {
        const line = lines[index];
        if (/^```/.test(line.trim())) {
          flushParagraph();
          const code = [];
          index += 1;
          while (index < lines.length && !/^```/.test(lines[index].trim())) {
            code.push(lines[index]);
            index += 1;
          }
          html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
          continue;
        }
        if (!line.trim()) {
          flushParagraph();
          continue;
        }
        const heading = line.match(/^(#{1,4})\s+(.+)$/);
        if (heading) {
          flushParagraph();
          const level = heading[1].length <= 2 ? "h3" : "h4";
          html.push(`<${level}>${renderInlineMarkdown(heading[2])}</${level}>`);
          continue;
        }
        if (/^\s*[-*]\s+/.test(line)) {
          flushParagraph();
          const items = [];
          while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
            items.push(lines[index].replace(/^\s*[-*]\s+/, ""));
            index += 1;
          }
          index -= 1;
          html.push(`<ul>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
          continue;
        }
        if (/^\s*\d+[.)]\s+/.test(line)) {
          flushParagraph();
          const items = [];
          while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
            items.push(lines[index].replace(/^\s*\d+[.)]\s+/, ""));
            index += 1;
          }
          index -= 1;
          html.push(`<ol>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ol>`);
          continue;
        }
        if (/^\s*>\s?/.test(line)) {
          flushParagraph();
          const quoted = [];
          while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
            quoted.push(lines[index].replace(/^\s*>\s?/, ""));
            index += 1;
          }
          index -= 1;
          html.push(`<blockquote>${quoted.map(renderInlineMarkdown).join("<br>")}</blockquote>`);
          continue;
        }
        paragraph.push(line);
      }
      flushParagraph();
      return html.join("");
    }
    function renderInlineMarkdown(value) {
      const parts = String(value ?? "").split(/(`[^`\n]+`)/g);
      return parts.map((part) => {
        if (part.startsWith("`") && part.endsWith("`")) {
          return `<code>${escapeHtml(part.slice(1, -1))}</code>`;
        }
        return escapeHtml(part)
          .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_match, label, url) => `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${label}</a>`)
          .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
          .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
          .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
          .replace(/(^|[^\*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
          .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
      }).join("");
    }
    function buildConversationTranscript(events) {
      return (events || [])
        .map((event, index, allEvents) => describeEventAsConversationTurn(event, index, allEvents))
        .filter(Boolean);
    }
    function describeEventAsConversationTurn(event, index = 0, allEvents = []) {
      const payload = event.payload || {};
      switch (event.type) {
        case "session.created":
          return {
            speaker: "observer",
            direction: "system",
            title: "Observer",
            timestamp: event.ts,
            body: `${toolLabel(payload.tool_name || event.tool_name)} run이 ${payload.model || "지정된 모델"}로 시작되었습니다.`,
            meta: { status: payload.status, conversation_id: payload.conversation_id, run_id: payload.run_id || payload.session_id },
            rawEvent: event
          };
        case "run.command":
          return turn(event, "observer", "system", "Observer", "Gemini CLI 실행 argv가 기록되었습니다.", {
            mode: payload.native_resume_used ? "resume" : payload.gemini_session_id ? "session-id" : "legacy",
            fallback: payload.fallback_used ? payload.fallback_reason || "fallback" : ""
          });
        case "conversation.native_session_rotated":
          return turn(event, "observer", "system", "Observer", `Gemini native session을 새로 시작했습니다. 사유: ${payload.reason || "알 수 없음"}.`, {}, "warn");
        case "prompt.rendered":
          if (hasMatchingLaterPromptSent(event, index, allEvents)) return null;
          return {
            speaker: "agents",
            direction: "agents_to_gemini",
            title: "Agents -> Gemini",
            timestamp: event.ts,
            body: payload.prompt || "Gemini에게 보낼 프롬프트가 준비되었습니다.",
            meta: { 단계: "전송 전 초안" },
            rawEvent: event
          };
        case "prompt.redacted":
          return null;
        case "prompt.pending_approval":
          return turn(event, "observer", "system", "Observer", `Gemini로 보내기 전에 사용자 승인을 기다리고 있습니다. 제한 시간은 ${payload.timeout_sec || "설정된"}초입니다.`);
        case "prompt.sent":
          return {
            speaker: "agents",
            direction: "agents_to_gemini",
            title: "Agents -> Gemini",
            timestamp: event.ts,
            body: payload.prompt || "프롬프트가 Gemini CLI로 전송되었습니다.",
            meta: { 단계: "Gemini에 전송됨" },
            rawEvent: event
          };
        case "gemini.started":
          return {
            speaker: "gemini",
            direction: "system",
            title: "Gemini CLI",
            timestamp: event.ts,
            body: "Gemini CLI를 시작했습니다.",
            meta: { 모델: payload.model, cwd: payload.cwd, "output mode": payload.output_format, pid: payload.pid },
            rawEvent: event
          };
        case "gemini.delta":
          if (hasLaterGeminiOutput(event, index, allEvents)) return null;
          return {
            speaker: "gemini",
            direction: "gemini_to_agents",
            title: "Gemini -> Agents · 응답 중",
            timestamp: event.ts,
            body: payload.response || payload.content || "",
            meta: { 단계: "응답 수신 중" },
            rawEvent: event
          };
        case "gemini.response": {
          const parsed = parseGeminiEnvelope(payload.response || "");
          const body = parsed.response ?? payload.response ?? "";
          return {
            speaker: "gemini",
            direction: "gemini_to_agents",
            title: parsed.error ? "Gemini -> Agents · 오류 포함" : "Gemini -> Agents",
            timestamp: event.ts,
            body: body || "Gemini가 빈 응답을 반환했습니다.",
            meta: { stats: parsed.stats, error: parsed.error, 형식: parsed.envelope ? "JSON envelope" : "원문 응답" },
            severity: parsed.error ? "error" : "",
            rawEvent: event
          };
        }
        case "gemini.stderr":
          if (isBenignGeminiStderr(event)) return null;
          return turn(event, "observer", "system", "Observer", "Gemini CLI가 표준 오류 출력에 경고나 진단 메시지를 남겼습니다.", { stderr: payload.stderr }, "warn");
        case "gemini.stream_error":
          return turn(event, "observer", "system", "Observer", `Gemini CLI 스트림 오류가 기록되었습니다.\n${payload.message || ""}`, { severity: payload.severity }, "error");
        case "gemini.tool_use":
          return turn(event, "gemini", "system", "Gemini CLI", `Gemini CLI가 도구 사용을 요청했습니다: ${payload.tool_name || "알 수 없는 도구"}`, { tool_id: payload.tool_id, parameters: payload.parameters });
        case "gemini.tool_result":
          return turn(event, "gemini", "system", "Gemini CLI", `Gemini CLI 도구 실행 결과가 기록되었습니다: ${payload.status || "알 수 없음"}`, { tool_id: payload.tool_id, output: payload.output, error: payload.error });
        case "gemini.exited":
          return turn(event, "gemini", "system", "Gemini CLI", "Gemini CLI 프로세스가 종료되었습니다.", { "exit code": payload.exit_code ?? "없음", 실행시간: formatDuration(payload.duration_ms) });
        case "json.extracted":
          return turn(event, "observer", "system", "Observer", "응답에서 JSON 후보를 추출했습니다.", { stats: payload.stats, error: payload.error });
        case "json.parse_failed":
          return turn(event, "observer", "system", "Observer", `JSON 파싱에 실패했습니다.\n${payload.parse_error || ""}`, {}, "error");
        case "json.validation_failed":
          return turn(event, "observer", "system", "Observer", `JSON schema 검증에 실패했습니다.\n${validationText(payload.validation_errors || [])}`, {}, "error");
        case "json.validation_passed":
          return turn(event, "observer", "system", "Observer", "JSON schema 검증을 통과했습니다.");
        case "repair.started":
          return turn(event, "observer", "system", "Observer", `응답 복구를 1회 시도했습니다.\n${repairReason(payload)}`, {}, "warn");
        case "repair.prompt_sent":
          return turn(event, "agents", "agents_to_gemini", "Agents -> Gemini", payload.prompt || "구조 복구용 프롬프트를 보냈습니다.", { 단계: "응답 복구" });
        case "repair.response":
          return turn(event, "gemini", "gemini_to_agents", "Gemini -> Agents", formatReadable(payload.response || ""), { 단계: "복구 응답" });
        case "repair.validation_passed":
          return turn(event, "observer", "system", "Observer", "복구된 JSON이 schema 검증을 통과했습니다.", { 결과: "복구 성공" });
        case "repair.validation_failed":
          return turn(event, "observer", "system", "Observer", `복구 후에도 schema 검증을 통과하지 못했습니다.\n${validationText(payload.validation_errors || payload.parse_error || [])}`, { 결과: "복구 실패" }, "error");
        case "intervention.received":
          return turn(event, "user", "user", "사용자 개입", `사용자가 "${actionLabel(payload.action)}"을 요청했습니다.${payload.instruction || payload.prompt ? `\n${payload.instruction || payload.prompt}` : ""}`);
        case "intervention.applied":
          return turn(event, "observer", "system", "Observer", `개입이 적용되었습니다: ${actionLabel(payload.action)}.${payload.instruction || payload.prompt ? `\n${payload.instruction || payload.prompt}` : ""}`);
        case "session.completed": {
          const result = payload.result || {};
          return turn(event, "observer", "system", "Observer", `최종 결과: ${statusLabel(result.status || payload.status || "completed")}\n${resultSummaryText(result)}`, {
            repaired: result.repaired,
            repair_attempted: result.repair_attempted,
            repair_succeeded: result.repair_succeeded
          });
        }
        case "session.cancelled":
          return turn(event, "observer", "system", "Observer", `세션이 취소되었습니다. 사유: ${payload.reason || "사용자 요청 또는 재시도"}.`, {}, "warn");
        case "session.error":
          return turn(event, "observer", "system", "Observer", `세션에서 오류가 발생했습니다.\n${payload.message || ""}${payload.stderr_tail ? `\n\n${payload.stderr_tail}` : ""}`, {}, "error");
        default:
          return turn(event, "observer", "system", "Observer", `${eventLabel(event.type)} 이벤트가 기록되었습니다.`);
      }
    }
    function hasMatchingLaterPromptSent(event, index, allEvents) {
      const prompt = event.payload?.prompt;
      if (!prompt) return false;
      return allEvents.slice(index + 1).some((candidate) =>
        candidate.type === "prompt.sent" && candidate.payload?.prompt === prompt
      );
    }
    function hasLaterGeminiOutput(event, index, allEvents) {
      return allEvents.slice(index + 1).some((candidate) =>
        candidate.session_id === event.session_id &&
        (candidate.phase || "") === (event.phase || "") &&
        ["gemini.delta", "gemini.response"].includes(candidate.type)
      );
    }
    function turn(event, speaker, direction, title, body, meta = {}, severity = "") {
      return { speaker, direction, title, timestamp: event.ts, body, meta, severity, rawEvent: event };
    }
    function resultSummaryText(result) {
      if (result.text) return result.text;
      if (result.data) return formatReadable(result.data);
      if (result.raw_response) return formatReadable(result.raw_response);
      if (result.message) return result.message;
      return "세션이 완료되었습니다.";
    }
    function parseGeminiEnvelope(value) {
      if (typeof value !== "string") return { response: "", envelope: null };
      try {
        const parsed = JSON.parse(value);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return { response: value, envelope: null };
        return {
          response: typeof parsed.response === "string" ? parsed.response : typeof parsed.text === "string" ? parsed.text : value,
          stats: parsed.stats,
          error: parsed.error,
          envelope: parsed
        };
      } catch {
        return { response: value, envelope: null };
      }
    }
    function isBenignGeminiStderr(event) {
      if (event.type !== "gemini.stderr") return false;
      const stderr = String(event.payload?.stderr || "");
      const lines = stderr.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
      if (!lines.length) return true;
      return lines.every((line) =>
        line.includes("256-color support not detected") ||
        line.includes("Ripgrep is not available. Falling back to GrepTool.")
      );
    }
    function formatReadable(value) {
      if (typeof value === "string") {
        const parsed = parsePossiblyJson(value);
        if (parsed !== null) return formatReadable(parsed);
        return value;
      }
      if (Array.isArray(value)) {
        if (!value.length) return "없음";
        return value.map((item, index) => `${index + 1}. ${formatReadable(item)}`).join("\n");
      }
      if (value && typeof value === "object") {
        return Object.entries(value).map(([key, item]) => `${fieldLabel(key)}: ${formatReadable(item)}`).join("\n");
      }
      if (value === null || value === undefined || value === "") return "없음";
      return String(value);
    }
    function parsePossiblyJson(value) {
      const trimmed = value.trim();
      if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return null;
      try { return JSON.parse(trimmed); } catch { return null; }
    }
    function validationText(errors) {
      if (typeof errors === "string") return errors;
      if (!Array.isArray(errors) || !errors.length) return "자세한 오류 없음";
      return errors.map((error, index) => `${index + 1}. ${error.message || formatReadable(error)}`).join("\n");
    }
    function repairReason(payload) {
      const pieces = [];
      if (payload.parse_error) pieces.push(`파싱 오류: ${payload.parse_error}`);
      if (payload.validation_errors?.length) pieces.push(`검증 오류:\n${validationText(payload.validation_errors)}`);
      return pieces.join("\n\n") || "응답 구조를 복구해야 합니다.";
    }
    function statusLabel(status) { return statusLabels[status] || status || "알 수 없음"; }
    function toolLabel(tool) { return toolLabels[tool] || tool || "알 수 없는 도구"; }
    function sessionTitle(session) { return session?.title || toolLabel(session?.tool_name); }
    function roleLabel(role) { return roleLabels[role] || role || "알 수 없음"; }
    function eventLabel(type) { return eventLabels[type] || type || "이벤트"; }
    function phaseLabel(phase) { return phase === "repair" ? "복구" : phase === "edited" ? "수정됨" : phase === "appended_instruction" ? "지시 추가됨" : phase; }
    function actionLabel(action) {
      return {
        approve: "전송 승인",
        edit_prompt: "프롬프트 수정",
        add_instruction: "지시 추가",
        cancel: "취소",
        interrupt_retry: "중단 후 다시 시도",
        follow_up: "이어서 질문",
        note: "메모"
      }[action] || action || "알 수 없음";
    }
    function verdictLabel(verdict) {
      return { pass: "통과", needs_work: "수정 필요", unsafe: "위험" }[verdict] || verdict || "알 수 없음";
    }
    function severityLabel(severity) {
      return { critical: "치명", high: "높음", medium: "보통", low: "낮음", info: "정보" }[severity] || severity || "정보";
    }
    function fieldLabel(key) {
      return {
        answer: "답변",
        verdict: "결론",
        summary: "요약",
        findings: "발견 사항",
        recommended_actions: "권장 조치",
        severity: "심각도",
        title: "제목",
        file: "파일",
        line_hint: "위치 힌트",
        explanation: "설명",
        suggested_fix: "제안 수정",
        status: "상태",
        text: "답변",
        data: "데이터",
        message: "메시지"
      }[key] || key;
    }
    function formatDate(value) {
      if (!value) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString("ko-KR", { hour12: false });
    }
    function formatTime(value) {
      if (!value) return "--:--:--";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleTimeString("ko-KR", { hour12: false });
    }
    function formatDuration(ms) {
      if (ms === undefined || ms === null || ms === "") return "알 수 없음";
      if (ms < 1000) return `${ms}ms`;
      return `${(ms / 1000).toFixed(1)}초`;
    }
    function isTerminalStatus(status) {
      return ["completed", "valid", "invalid", "error"].includes(status);
    }
    function findCwd(events) {
      const started = [...events].reverse().find((event) => event.type === "gemini.started" && event.payload?.cwd);
      return started?.payload?.cwd || "";
    }
    function metaLabel(key) { return key; }
    function formatMetaValue(value) {
      if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
      return JSON.stringify(value);
    }
    qs("refresh").onclick = loadSessions;
    qs("raw").onchange = async () => {
      if (qs("raw").checked && !confirm("원본 payload에는 민감한 내용이 포함될 수 있습니다. 이 로컬 토큰으로 원본을 보시겠습니까?")) qs("raw").checked = false;
      openEvents();
      if (currentSessionId) await loadTranscript();
    };
    qs("pause").onchange = async () => { await postJson("/api/config", {pause_before_send: qs("pause").checked}); };
    qs("copy").onclick = async () => {
      if (!transcript) return;
      await navigator.clipboard.writeText(buildReadableTranscript(transcript));
    };
    qs("export").onclick = () => {
      if (!currentSessionId) return;
      const raw = qs("raw").checked ? "1" : "0";
      if (transcript?.conversation?.conversation_id) {
        location.href = api(`/api/conversations/${transcript.conversation.conversation_id}/export?raw=${raw}`);
        return;
      }
      location.href = api(`/api/sessions/${currentSessionId}/export?raw=${raw}`);
    };
    document.querySelectorAll("[data-action]").forEach((button) => {
      button.onclick = () => sendIntervention(button.dataset.action);
    });
    function buildReadableTranscript(data) {
      const session = data.session || {};
      const turns = buildConversationTranscript(visibleEvents(data.events || []));
      const lines = [
        `도구: ${toolLabel(session.tool_name)}`,
        `상태: ${statusLabel(session.status)}`,
        `모델: ${session.model || "알 수 없음"}`,
        `시작: ${formatDate(session.started_at)}`,
        ""
      ];
      for (const turn of turns) {
        lines.push(`[${formatTime(turn.timestamp)}] ${turn.title}`);
        if (turn.body) lines.push(turn.body);
        lines.push("");
      }
      return lines.join("\n").trim();
    }
    window.buildConversationTranscript = buildConversationTranscript;
    window.describeEventAsConversationTurn = describeEventAsConversationTurn;
    window.preferredLiveSession = preferredLiveSession;
    window.parseGeminiEnvelope = parseGeminiEnvelope;
    window.renderMarkdown = renderMarkdown;
    window.renderSessionGroup = renderSessionGroup;
    window.sessionTitle = sessionTitle;
    getJson("/api/config").then((config) => { qs("pause").checked = !!config.pause_before_send; }).catch(console.error);
    loadSessions().then(openEvents).catch(console.error);
  </script>
</body>
</html>
"""
