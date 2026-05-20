from __future__ import annotations

import json
import queue
import socket
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
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="Antigravity-observer-web", daemon=True)

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
    allow_reuse_address = False

    def __init__(self, server_address: tuple[str, int], handler_cls: type[BaseHTTPRequestHandler], hub: ObserverHub) -> None:
        self.hub = hub
        super().__init__(server_address, handler_cls)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


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
            self._json({"redact_raw_by_default": True})
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
                self.send_header("Content-Disposition", f"attachment; filename=Antigravity-session-{quote(session_id)}.json")
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

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        payload = self._read_json()
        parts = _parts(parsed.path)
        title = str(payload.get("title") or "")
        try:
            if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                session = self.server.hub.rename_session(parts[2], title)
                self._json({"session": session})
                return
            if len(parts) == 3 and parts[:2] == ["api", "conversations"]:
                conversation = self.server.hub.rename_conversation(parts[2], title)
                self._json({"conversation": conversation})
                return
        except KeyError:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        parts = _parts(parsed.path)
        try:
            if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                self._json(self.server.hub.delete_session(parts[2]))
                return
            if len(parts) == 3 and parts[:2] == ["api", "conversations"]:
                self._json(self.server.hub.delete_conversation(parts[2]))
                return
        except KeyError:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        self._read_json()
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
      --Antigravity: #6650a4;
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
        --Antigravity: #c6b8ff;
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
    .layout { display: grid; grid-template-columns: 340px minmax(0, 1fr); min-height: 100vh; }
    aside, main { min-width: 0; padding: 16px; }
    aside { border-right: 1px solid var(--line); background: var(--panel); }
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
      gap: 8px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px;
    }
    .session-main {
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 0;
      display: grid;
      gap: 4px;
      text-align: left;
      width: 100%;
    }
    .session-actions { display: flex; gap: 6px; flex-wrap: wrap; }
    .session-actions button { padding: 4px 7px; font-size: 12px; background: var(--panel); }
    .session-title { font-weight: 700; overflow-wrap: anywhere; }
    .session.active { outline: 2px solid var(--accent); }
    .meta { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .badge { border-radius: 999px; padding: 2px 8px; font-size: 12px; background: var(--panel); border: 1px solid var(--line); }
    .badge.valid, .badge.completed { color: var(--good); }
    .badge.invalid, .badge.error, .badge.cancelled { color: var(--bad); }
    .badge.repairing, .badge.running, .badge.sending { color: var(--warn); }
    .session-heading { display: flex; align-items: center; gap: 7px; min-width: 0; }
    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      flex: 0 0 auto;
      background: var(--muted);
      box-shadow: 0 0 0 1px color-mix(in srgb, currentColor, transparent 72%);
    }
    .status-dot.live {
      color: var(--good);
      background: var(--good);
      animation: statusPulse 1.15s ease-in-out infinite;
    }
    .status-dot.completed, .status-dot.valid { color: var(--good); background: var(--good); }
    .status-dot.stale, .status-dot.starting, .status-dot.running, .status-dot.sending, .status-dot.repairing, .status-dot.queued, .status-dot.loading {
      color: var(--warn);
      background: var(--warn);
    }
    .status-dot.stale { animation: statusPulse 1.8s ease-in-out infinite; }
    .status-dot.error, .status-dot.invalid, .status-dot.timeout { color: var(--bad); background: var(--bad); }
    .status-dot.cancelled { color: var(--muted); background: var(--muted); }
    @keyframes statusPulse {
      0%, 100% { opacity: 0.45; transform: scale(0.88); box-shadow: 0 0 0 1px color-mix(in srgb, currentColor, transparent 70%); }
      50% { opacity: 1; transform: scale(1); box-shadow: 0 0 0 6px color-mix(in srgb, currentColor, transparent 86%); }
    }
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
    .runtime-signal {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--bg);
      min-width: 0;
    }
    .runtime-copy { display: grid; gap: 4px; min-width: 0; }
    .runtime-title { font-weight: 700; font-size: 13px; }
    .runtime-details { display: flex; flex-wrap: wrap; gap: 6px; }
    .runtime-chip {
      color: var(--muted);
      font-size: 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      background: var(--panel);
      overflow-wrap: anywhere;
    }
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
    .turn.Antigravity .turn-title { color: var(--Antigravity); }
    .turn.Antigravity .turn-title::before { background: var(--Antigravity); }
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
    .loading-message { color: var(--muted); }
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
    label { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; }
    .stack { display: grid; gap: 10px; }
    .help { color: var(--muted); font-size: 12px; line-height: 1.5; margin: 0; }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .findings { display: grid; gap: 8px; margin-bottom: 12px; }
    .finding { border-left: 3px solid var(--accent); padding: 8px 10px; background: var(--panel); }
    .debug-panel { margin-top: 14px; }
    .debug-panel > summary { cursor: pointer; color: var(--muted); padding: 8px 0; }
    @media (max-width: 1100px) {
      .layout { grid-template-columns: 1fr; }
      aside { border: 0; border-bottom: 1px solid var(--line); }
    }
    @media (max-width: 700px) {
      aside, main { padding: 12px; }
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
        <div id="runtimeSignal"></div>
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
    let refreshInFlight = null;
    let transcriptRequestSeq = 0;
    let loadingSessionId = "";
    if (explicitSessionPath) canonicalizeDashboardUrl();

    const statusLabels = {
      queued: "대기 중",
      waiting_for_user_approval: "승인 대기",
      sending: "전송 중",
      running: "실행 중",
      repairing: "복구 중",
      loading: "불러오는 중",
      valid: "유효함",
      invalid: "유효하지 않음",
      error: "오류",
      cancelled: "취소됨",
      completed: "완료"
    };
    const toolLabels = {
      ask_antigravity: "Antigravity 질문",
      ask_antigravity_json: "Antigravity JSON 질문",
      review_current_diff_with_antigravity: "현재 변경 리뷰"
    };
    const roleLabels = {
      codex_mcp: "Codex/MCP",
      gemness: "Antigravity CLI",
      user: "사용자",
      system: "시스템"
    };
    const eventLabels = {
      "session.created": "세션 생성",
      "prompt.rendered": "프롬프트 준비",
      "prompt.redacted": "민감정보 가림",
      "prompt.pending_approval": "전송 승인 대기",
      "prompt.sent": "프롬프트 전송",
      "antigravity.started": "Antigravity 실행 시작",
      "antigravity.model_detected": "Antigravity 모델 감지",
      "antigravity.response": "Antigravity 응답",
      "antigravity.stderr": "Antigravity 경고",
      "antigravity.exited": "Antigravity 종료",
      "json.extracted": "JSON 후보 추출",
      "json.parse_failed": "JSON 파싱 실패",
      "json.validation_failed": "스키마 검증 실패",
      "json.validation_passed": "스키마 검증 성공",
      "repair.started": "응답 복구 시작",
      "repair.prompt_sent": "복구 프롬프트 전송",
      "repair.response": "복구 응답",
      "repair.validation_passed": "복구 검증 성공",
      "repair.validation_failed": "복구 검증 실패",
      "run.command": "실행 명령",
      "conversation.agy_context_rotated": "Antigravity 대화 컨텍스트 회전",
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
    async function requestJson(path, options = {}) {
      const response = await fetch(api(path), options);
      if (!response.ok) {
        const message = await response.text();
        if (response.status === 401) recoverFromAuthFailure();
        throw new Error(message);
      }
      markAuthHealthy();
      return await response.json();
    }
    async function getJson(path) {
      return await requestJson(path);
    }
    async function patchJson(path, body) {
      return await requestJson(path, {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
      });
    }
    async function deleteJson(path) {
      return await requestJson(path, {
        method: "DELETE"
      });
    }
    function hasActiveTextSelection() {
      const selection = typeof window.getSelection === "function" ? window.getSelection() : null;
      return !!selection && !selection.isCollapsed && !!String(selection.toString() || "").trim();
    }
    function refreshDashboard(options = {}) {
      if (options.automatic && hasActiveTextSelection()) return Promise.resolve();
      if (refreshInFlight) return refreshInFlight;
      refreshInFlight = loadSessions(options).finally(() => { refreshInFlight = null; });
      return refreshInFlight;
    }
    async function loadSessions(options = {}) {
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
      if (currentSessionId) await loadTranscript(currentSessionId, { showLoading: !options.automatic });
      else {
        transcript = null;
        loadingSessionId = "";
        renderTranscript();
      }
    }
    function preferredLiveSession(sessionItems) {
      return (sessionItems || []).find((session) => !isTerminalStatus(session.status)) || sessionItems?.[0] || null;
    }
    function shouldHonorRequestedSession(requested, preferred) {
      return !!requested && (!preferred || isTerminalStatus(preferred.status));
    }
    async function loadTranscript(targetSessionId = currentSessionId, options = {}) {
      const targetId = targetSessionId || "";
      if (!targetId) {
        transcript = null;
        loadingSessionId = "";
        renderTranscript();
        return false;
      }
      const requestSeq = ++transcriptRequestSeq;
      const raw = qs("raw").checked ? "1" : "0";
      if (options.showLoading !== false) {
        loadingSessionId = targetId;
        renderLoadingTranscript(targetId);
      }
      try {
        const baseTranscript = await getJson(`/api/sessions/${targetId}?raw=${raw}`);
        const nextTranscript = await loadConversationBundle(baseTranscript, raw);
        if (!isCurrentTranscriptRequest(requestSeq, targetId)) return false;
        transcript = nextTranscript;
        loadingSessionId = "";
        renderTranscript();
        return true;
      } catch (error) {
        if (isCurrentTranscriptRequest(requestSeq, targetId)) {
          loadingSessionId = "";
          renderTranscriptError(targetId, error);
        }
        return false;
      }
    }
    function isCurrentTranscriptRequest(requestSeq, targetSessionId) {
      return requestSeq === transcriptRequestSeq && targetSessionId === currentSessionId;
    }
    function selectedSessionById(sessionId) {
      return (sessions || []).find((session) => session.session_id === sessionId)
        || (transcript?.related_sessions || []).find((session) => session?.session_id === sessionId)
        || (transcript?.session?.session_id === sessionId ? transcript.session : null)
        || null;
    }
    function renderLoadingTranscript(sessionId) {
      const selected = selectedSessionById(sessionId) || {};
      const session = { ...selected, session_id: sessionId, status: selected.status || "loading" };
      qs("title").textContent = `${sessionTitle(session)} · ${statusLabel("loading")}`;
      renderReview([]);
      renderSessionSummary(session, []);
      setInnerHtmlIfChanged("transcriptFlow", `<p class="prose loading-message">대화 기록을 불러오는 중입니다.</p>`);
      setInnerHtmlIfChanged("debugEvents", "");
    }
    function renderTranscriptError(sessionId, error) {
      const selected = selectedSessionById(sessionId) || {};
      const session = { ...selected, session_id: sessionId, status: "error" };
      const message = error?.message || "알 수 없는 오류";
      qs("title").textContent = `${sessionTitle(session)} · 불러오기 실패`;
      renderReview([]);
      renderSessionSummary(session, []);
      setInnerHtmlIfChanged("transcriptFlow", `<p class="prose error">대화 기록을 불러오지 못했습니다.\n${escapeHtml(message)}</p>`);
      setInnerHtmlIfChanged("debugEvents", "");
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
      const conversationItems = groupSessionsByConversation(sessions);
      const liveSessions = conversationItems.filter((session) => !isTerminalStatus(session.status));
      const historySessions = conversationItems.filter((session) => isTerminalStatus(session.status));
      const html = [
        renderSessionGroup("Live", liveSessions, "실행 중인 세션이 없습니다."),
        renderSessionGroup("History", historySessions, "이전 세션이 없습니다.")
      ].join("");
      setInnerHtmlIfChanged("sessionList", html);
    }
    function bindSessionListEvents() {
      qs("sessionList").onclick = async (event) => {
        const target = event.target?.closest ? event.target : event.target?.parentElement;
        if (!target) return;
        const renameButton = target.closest?.("[data-rename-kind]");
        if (renameButton) {
          await renameSessionListItem(renameButton);
          return;
        }
        const deleteButton = target.closest?.("[data-delete-kind]");
        if (deleteButton) {
          await deleteSessionListItem(deleteButton);
          return;
        }
        const sessionButton = target.closest?.("[data-session]");
        if (sessionButton) await selectSession(sessionButton.dataset.session);
      };
    }
    async function selectSession(sessionId) {
      if (!sessionId) return false;
      liveMode = false;
      currentSessionId = sessionId;
      canonicalizeDashboardUrl();
      renderSessions();
      return await loadTranscript(sessionId);
    }
    function groupSessionsByConversation(sessionItems) {
      const groups = new Map();
      for (const session of sessionItems || []) {
        const key = session.conversation_id || session.session_id;
        if (!groups.has(key)) groups.set(key, { conversation_id: session.conversation_id || "", conversation_title: session.conversation_title || "", runs: [] });
        const group = groups.get(key);
        if (session.conversation_title && !group.conversation_title) group.conversation_title = session.conversation_title;
        group.runs.push(session);
      }
      return [...groups.values()]
        .map(conversationListItem)
        .sort((a, b) => String(b.updated_at || b.started_at || "").localeCompare(String(a.updated_at || a.started_at || "")));
    }
    function conversationListItem(group) {
      const runs = [...group.runs].sort(compareSessionOrder);
      const root = runs.find((session) => (session.turn_index || 0) === 1) || runs[0] || {};
      const latest = runs.reduce((best, session) => newerSession(session, best), runs[0] || {});
      const latestLive = runs.filter((session) => !isTerminalStatus(session.status)).reduce((best, session) => newerSession(session, best), null);
      const display = latestLive || latest;
      const turnCount = runs.reduce((count, session) => Math.max(count, Number(session.turn_index) || 0), 0) || runs.length;
      return {
        ...display,
        title: group.conversation_title || root.title || display.title,
        session_id: display.session_id,
        conversation_id: display.conversation_id || group.conversation_id,
        conversation_title: group.conversation_title || "",
        turn_count: turnCount,
        _active: runs.some((session) => session.session_id === currentSessionId),
        _run_session_ids: runs.map((session) => session.session_id)
      };
    }
    function compareSessionOrder(a, b) {
      const turnDiff = (Number(a.turn_index) || 0) - (Number(b.turn_index) || 0);
      if (turnDiff) return turnDiff;
      return String(a.started_at || "").localeCompare(String(b.started_at || ""));
    }
    function newerSession(candidate, current) {
      if (!current) return candidate;
      const byUpdated = String(candidate.updated_at || candidate.started_at || "").localeCompare(String(current.updated_at || current.started_at || ""));
      if (byUpdated > 0) return candidate;
      if (byUpdated < 0) return current;
      return compareSessionOrder(candidate, current) >= 0 ? candidate : current;
    }
    function renderSessionGroup(label, items, emptyText) {
      const body = items.length ? items.map(renderSessionButton).join("") : `<p class="help">${escapeHtml(emptyText)}</p>`;
      return `<section class="session-group"><div class="session-group-title">${escapeHtml(label)}</div>${body}</section>`;
    }
    function renderSessionButton(s) {
      const toolMeta = s.turn_count && s.turn_count > 1 ? `${s.turn_count}턴 · ${toolLabel(s.tool_name)}` : toolLabel(s.tool_name);
      const targetKind = s.conversation_id ? "conversation" : "session";
      const targetId = s.conversation_id || s.session_id;
      const title = sessionTitle(s);
      const deleteDisabled = isTerminalStatus(s.status) ? "" : "disabled";
      const deleteTitle = isTerminalStatus(s.status) ? "기록 제거" : "실행 중인 항목은 제거할 수 없습니다.";
      const telemetry = runTelemetry(s, []);
      return `
        <article class="session ${s._active || s.session_id === currentSessionId ? "active" : ""}">
          <button class="session-main" data-session="${escapeHtml(s.session_id)}">
            <span class="session-heading">${statusDotHtml(telemetry)}<span><span class="session-title">${escapeHtml(title)}</span> <span class="badge ${escapeHtml(s.status)}">${escapeHtml(statusLabel(s.status))}</span></span></span>
            <span class="meta">${escapeHtml(toolMeta)}</span>
            <span class="meta">${escapeHtml(s.model || "")}</span>
            <span class="meta">${escapeHtml(formatDate(s.started_at))}${s.duration_ms ? ` · ${formatDuration(s.duration_ms)}` : ""}</span>
          </button>
          <div class="session-actions">
            <button data-rename-kind="${targetKind}" data-rename-id="${escapeHtml(targetId)}" data-current-title="${escapeHtml(title)}">이름 변경</button>
            <button data-delete-kind="${targetKind}" data-delete-id="${escapeHtml(targetId)}" data-delete-title="${escapeHtml(title)}" title="${escapeHtml(deleteTitle)}" ${deleteDisabled}>제거</button>
          </div>
        </article>
      `;
    }
    async function renameSessionListItem(button) {
      const kind = button.dataset.renameKind;
      const id = button.dataset.renameId;
      const currentTitle = button.dataset.currentTitle || "";
      const nextTitle = prompt("새 이름", currentTitle);
      if (nextTitle === null) return;
      const title = nextTitle.trim();
      if (!title) {
        alert("이름을 입력해주세요.");
        return;
      }
      const path = kind === "conversation" ? `/api/conversations/${id}` : `/api/sessions/${id}`;
      await patchJson(path, { title });
      await loadSessions();
    }
    async function deleteSessionListItem(button) {
      const kind = button.dataset.deleteKind;
      const id = button.dataset.deleteId;
      const title = button.dataset.deleteTitle || "선택한 항목";
      if (!confirm(`"${title}" 기록을 제거할까요? 이 작업은 로컬 transcript 파일도 삭제합니다.`)) return;
      const path = kind === "conversation" ? `/api/conversations/${id}` : `/api/sessions/${id}`;
      await deleteJson(path);
      if (transcript?.conversation?.conversation_id === id || transcript?.session?.session_id === id) {
        currentSessionId = "";
        liveMode = true;
        transcript = null;
      }
      await loadSessions();
    }
    function renderTranscript() {
      const session = transcript?.session || {};
      const openKeys = openRawEventKeys();
      qs("title").textContent = session.session_id ? `${sessionTitle(session)} · ${statusLabel(session.status)}` : "대화 기록";
      renderReview(transcript?.events || []);
      renderSessionSummary(session, transcript?.events || []);
      const turns = buildConversationTranscript(visibleEvents(transcript?.events || []));
      const visibleTurns = displayConversationTurns(turns);
      const transcriptHtml = visibleTurns.length ? visibleTurns.map(renderConversationTurn).join("") : `<p class="prose">아직 표시할 대화가 없습니다.</p>`;
      const debugHtml = (transcript?.events || []).map(renderRawEvent).join("");
      setInnerHtmlIfChanged("transcriptFlow", transcriptHtml);
      setInnerHtmlIfChanged("debugEvents", debugHtml);
      restoreRawEventKeys(openKeys);
    }
    function renderSessionSummary(session, events) {
      const cwd = findCwd(events);
      const conversation = session?.session_id === transcript?.session?.session_id ? transcript?.conversation || {} : {};
      setInnerHtmlIfChanged("runtimeSignal", renderRuntimeSignal(session, events));
      const html = [
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
      setInnerHtmlIfChanged("summaryGrid", html);
    }
    function renderRuntimeSignal(session, events) {
      if (!session?.session_id) return "";
      const telemetry = runTelemetry(session, events);
      const details = telemetry.details.map((item) => `<span class="runtime-chip">${escapeHtml(item)}</span>`).join("");
      return `
        <div class="runtime-signal ${escapeHtml(telemetry.state)}">
          ${statusDotHtml(telemetry)}
          <div class="runtime-copy">
            <div class="runtime-title">${escapeHtml(telemetry.label)}</div>
            <div class="runtime-details">${details}</div>
          </div>
        </div>
      `;
    }
    function renderConversationTurn(turn) {
      const meta = Object.entries(turn.meta || {})
        .filter(([, value]) => value !== undefined && value !== null && value !== "")
        .map(([key, value]) => `<span>${escapeHtml(metaLabel(key))}: ${escapeHtml(formatMetaValue(value))}</span>`)
        .join("");
      const rawKey = rawEventKey(turn.rawEvent, "turn");
      return `
        <article class="turn ${escapeHtml(turn.speaker)} ${escapeHtml(turn.severity || "")}">
          <div class="turn-time">${escapeHtml(formatTime(turn.timestamp))}</div>
          <div class="turn-content">
            <div class="turn-title">${escapeHtml(turn.title)}</div>
            <div class="turn-body">${renderMarkdown(turn.body || "")}</div>
            ${meta ? `<div class="turn-meta">${meta}</div>` : ""}
            <details class="raw-event" data-raw-key="${escapeHtml(rawKey)}">
              <summary>원본 이벤트 보기</summary>
              <pre>${escapeHtml(JSON.stringify(turn.rawEvent || {}, null, 2))}</pre>
            </details>
          </div>
        </article>
      `;
    }
    function renderRawEvent(event) {
      const rawKey = rawEventKey(event, "debug");
      return `
        <details class="raw-event" data-raw-key="${escapeHtml(rawKey)}">
          <summary>${escapeHtml(formatTime(event.ts))} · ${escapeHtml(eventLabel(event.type))}</summary>
          <pre>${escapeHtml(JSON.stringify(event, null, 2))}</pre>
        </details>
      `;
    }
    function rawEventKey(event, scope = "event") {
      const fallback = [event?.session_id || "", event?.type || "", event?.ts || ""].join(":");
      return `${scope}:${event?.event_id || fallback}`;
    }
    function openRawEventKeys() {
      const keys = new Set();
      document.querySelectorAll("details.raw-event[data-raw-key][open]").forEach((element) => {
        if (element.dataset.rawKey) keys.add(element.dataset.rawKey);
      });
      return keys;
    }
    function restoreRawEventKeys(keys) {
      if (!keys || !keys.size) return;
      document.querySelectorAll("details.raw-event[data-raw-key]").forEach((element) => {
        if (keys.has(element.dataset.rawKey)) element.open = true;
      });
    }
    function setInnerHtmlIfChanged(id, html) {
      const element = qs(id);
      if (element.innerHTML === html) return false;
      element.innerHTML = html;
      return true;
    }
    function visibleEvents(events) {
      if (qs("raw").checked) return events;
      return events.filter((event) => !isBenignAntigravityStderr(event));
    }
    function renderReview(events) {
      const completed = [...events].reverse().find((e) => e.type === "session.completed" && e.payload?.result?.data?.findings);
      if (!completed) {
        setInnerHtmlIfChanged("review", "");
        return;
      }
      const data = completed.payload.result.data;
      const html = `
        <div class="finding"><strong>리뷰 결론: ${escapeHtml(verdictLabel(data.verdict))}</strong><br>${escapeHtml(data.summary || "")}</div>
        ${(data.findings || []).map((f) => `
          <div class="finding">
            <strong>${escapeHtml(severityLabel(f.severity || "info"))}: ${escapeHtml(f.title || "")}</strong>
            <div class="meta">${escapeHtml(f.file || "")} ${escapeHtml(f.line_hint || "")}</div>
            <div>${escapeHtml(f.explanation || "")}</div>
          </div>
        `).join("")}
      `;
      setInnerHtmlIfChanged("review", html);
    }
    function openEvents() {
      if (source) source.close();
      const raw = qs("raw").checked ? "1" : "0";
      source = new EventSource(api(`/api/events?raw=${raw}`));
      source.onmessage = async () => { await refreshDashboard({automatic: true}); };
      source.onerror = async () => { await refreshDashboard({automatic: true}).catch(console.error); };
      if (!pollTimer) {
        pollTimer = setInterval(() => { refreshDashboard({automatic: true}).catch(console.error); }, 1500);
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
    function displayConversationTurns(turns) {
      return [...(turns || [])].sort((a, b) => String(b.timestamp || "").localeCompare(String(a.timestamp || "")));
    }
    function describeEventAsConversationTurn(event, index = 0, allEvents = []) {
      const payload = event.payload || {};
      switch (event.type) {
        case "antigravity.heartbeat":
          return null;
        case "session.created":
          return {
            speaker: "observer",
            direction: "system",
            title: "Observer",
            timestamp: event.ts,
            body: `${toolLabel(payload.tool_name || event.tool_name)} run이 ${payload.model || "Antigravity CLI default"}로 시작되었습니다.`,
            meta: { status: payload.status, conversation_id: payload.conversation_id, run_id: payload.run_id || payload.session_id },
            rawEvent: event
          };
        case "run.command":
          return turn(event, "observer", "system", "Observer", "Antigravity CLI 실행 argv가 기록되었습니다.", {
            streaming: payload.streaming === false ? "false" : "",
            fallback: payload.fallback_used ? payload.fallback_reason || "fallback" : ""
          });
        case "antigravity.model_detected":
          return turn(event, "observer", "system", "Observer", `Antigravity CLI가 실제 사용 모델을 보고했습니다: ${payload.model || "알 수 없음"}.`, {
            source: payload.source || "detected"
          });
        case "conversation.agy_context_rotated":
          return turn(event, "observer", "system", "Observer", `Antigravity 대화 컨텍스트를 새로 시작했습니다. 사유: ${payload.reason || "알 수 없음"}.`, {}, "warn");
        case "prompt.rendered":
          if (hasMatchingLaterPromptSent(event, index, allEvents)) return null;
          return {
            speaker: "agents",
            direction: "agents_to_antigravity",
            title: "Agents -> Antigravity",
            timestamp: event.ts,
            body: payload.prompt || "Antigravity에게 보낼 프롬프트가 준비되었습니다.",
            meta: { 단계: "전송 전 초안" },
            rawEvent: event
          };
        case "prompt.redacted":
          return null;
        case "prompt.pending_approval":
          return turn(event, "observer", "system", "Observer", `Antigravity로 보내기 전에 사용자 승인을 기다리고 있습니다. 제한 시간은 ${payload.timeout_sec || "설정된"}초입니다.`);
        case "prompt.sent":
          return {
            speaker: "agents",
            direction: "agents_to_antigravity",
            title: "Agents -> Antigravity",
            timestamp: event.ts,
            body: payload.prompt || "프롬프트가 Antigravity CLI로 전송되었습니다.",
            meta: { 단계: "Antigravity에 전송됨" },
            rawEvent: event
          };
        case "antigravity.started":
          return {
            speaker: "antigravity",
            direction: "system",
            title: "Antigravity CLI",
            timestamp: event.ts,
            body: "Antigravity CLI를 시작했습니다.",
            meta: { 모델: payload.model, cwd: payload.cwd, streaming: payload.streaming, pid: payload.pid },
            rawEvent: event
          };
        case "antigravity.response": {
          const parsed = parseAntigravityEnvelope(payload.response || "");
          const body = parsed.response ?? payload.response ?? "";
          return {
            speaker: "antigravity",
            direction: "antigravity_to_agents",
            title: parsed.error ? "Antigravity -> Agents · 오류 포함" : "Antigravity -> Agents",
            timestamp: event.ts,
            body: body || "Antigravity가 빈 응답을 반환했습니다.",
            meta: { stats: parsed.stats, metadata: parsed.metadata, error: parsed.error, streaming: parsed.metadata?.streaming ?? payload.streaming, 형식: parsed.envelope ? "JSON envelope" : "원문 응답" },
            severity: parsed.error ? "error" : "",
            rawEvent: event
          };
        }
        case "antigravity.stderr":
          if (isBenignAntigravityStderr(event)) return null;
          return turn(event, "observer", "system", "Observer", "Antigravity CLI가 표준 오류 출력에 경고나 진단 메시지를 남겼습니다.", { stderr: payload.stderr }, "warn");
        case "antigravity.exited":
          return turn(event, "antigravity", "system", "Antigravity CLI", "Antigravity CLI 프로세스가 종료되었습니다.", { "exit code": payload.exit_code ?? "없음", 실행시간: formatDuration(payload.duration_ms), auth: payload.auth_status, streaming: payload.streaming });
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
          return turn(event, "agents", "agents_to_antigravity", "Agents -> Antigravity", payload.prompt || "구조 복구용 프롬프트를 보냈습니다.", { 단계: "응답 복구" });
        case "repair.response":
          return turn(event, "antigravity", "antigravity_to_agents", "Antigravity -> Agents", formatReadable(payload.response || ""), { 단계: "복구 응답" });
        case "repair.validation_passed":
          return turn(event, "observer", "system", "Observer", "복구된 JSON이 schema 검증을 통과했습니다.", { 결과: "복구 성공" });
        case "repair.validation_failed":
          return turn(event, "observer", "system", "Observer", `복구 후에도 schema 검증을 통과하지 못했습니다.\n${validationText(payload.validation_errors || payload.parse_error || [])}`, { 결과: "복구 실패" }, "error");
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
    function turn(event, speaker, direction, title, body, meta = {}, severity = "") {
      return { speaker, direction, title, timestamp: event.ts, body, meta, severity, rawEvent: event };
    }
    function runTelemetry(session, events = []) {
      const status = session?.status || "unknown";
      const relevantEvents = (events || []).filter((event) => eventMatchesSession(event, session));
      const heartbeat = latestEvent(relevantEvents, "antigravity.heartbeat");
      const exited = latestEvent(relevantEvents, "antigravity.exited");
      const terminalEvent = latestTerminalEvent(relevantEvents);
      const terminal = isTerminalStatus(status);
      const heartbeatAge = heartbeat ? ageMs(heartbeat.ts) : null;
      const terminalReference = terminalEvent?.ts || exited?.ts || session?.completed_at || "";
      const heartbeatToTerminalMs = terminal && heartbeat ? durationBetween(heartbeat.ts, terminalReference) : null;
      let state = status;
      let label = statusLabel(status);
      if (terminal) {
        state = ["completed", "valid"].includes(status) ? "completed" : ["error", "invalid"].includes(status) ? "error" : status;
      } else if (heartbeat) {
        state = heartbeatAge !== null && heartbeatAge > 15000 ? "stale" : "live";
        label = state === "stale" ? "Heartbeat 지연" : "Live";
      } else if (["running", "sending", "repairing", "queued"].includes(status)) {
        state = status === "running" ? "starting" : status;
      }
      const details = telemetryDetails({ session, status, state, heartbeat, heartbeatAge, heartbeatToTerminalMs, exited });
      return {
        state,
        label,
        details,
        heartbeat,
        heartbeatAge,
        tooltip: [label, ...details].filter(Boolean).join(" · ")
      };
    }
    function telemetryDetails({ session, status, state, heartbeat, heartbeatAge, heartbeatToTerminalMs, exited }) {
      const payload = heartbeat?.payload || {};
      const details = [];
      if (heartbeat) {
        if (isTerminalStatus(status)) {
          details.push(`마지막 heartbeat ${formatTime(heartbeat.ts)}`);
          if (heartbeatToTerminalMs !== null) details.push(`종료 ${formatDuration(heartbeatToTerminalMs)} 전`);
        } else {
          details.push(`최근 heartbeat ${formatAge(heartbeatAge)} 전`);
        }
        if (payload.elapsed_ms !== undefined) details.push(`경과 ${formatDuration(payload.elapsed_ms)}`);
        if (payload.timeout_remaining_ms !== undefined) details.push(`timeout ${formatDuration(payload.timeout_remaining_ms)} 남음`);
        if (payload.pid !== undefined && payload.pid !== null) details.push(`pid ${payload.pid}`);
        if (payload.capture_mode) details.push(`capture ${payload.capture_mode}`);
        if (payload.stdout_bytes !== undefined) details.push(`stdout ${payload.stdout_bytes}B`);
        if (payload.stderr_bytes !== undefined) details.push(`stderr ${payload.stderr_bytes}B`);
        if (payload.last_activity_ms_ago !== undefined) details.push(`마지막 출력 ${formatDuration(payload.last_activity_ms_ago)} 전`);
      } else if (!isTerminalStatus(status)) {
        details.push(state === "queued" ? "실행 대기 중" : state === "loading" ? "대화 기록 로딩 중" : "heartbeat 대기 중");
      }
      if (isTerminalStatus(status)) {
        if (session?.duration_ms !== undefined && session.duration_ms !== null) details.push(`총 ${formatDuration(session.duration_ms)}`);
        if (exited?.payload?.duration_ms !== undefined) details.push(`프로세스 ${formatDuration(exited.payload.duration_ms)}`);
        if (exited?.payload?.exit_code !== undefined) details.push(`exit ${exited.payload.exit_code}`);
      }
      return details.length ? details : [statusLabel(status)];
    }
    function statusDotHtml(telemetry) {
      const state = telemetry?.state || "unknown";
      const title = telemetry?.tooltip || telemetry?.label || statusLabel(state);
      return `<span class="status-dot ${escapeHtml(state)}" title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}"></span>`;
    }
    function latestEvent(events, type) {
      return [...(events || [])].reverse().find((event) => event.type === type) || null;
    }
    function latestTerminalEvent(events) {
      return [...(events || [])].reverse().find((event) =>
        ["session.completed", "session.cancelled", "session.error"].includes(event.type)
      ) || null;
    }
    function eventMatchesSession(event, session) {
      const sessionId = session?.session_id || session?.run_id;
      if (!sessionId) return true;
      const payload = event?.payload || {};
      return event?.session_id === sessionId || payload.run_id === sessionId || payload.session_id === sessionId;
    }
    function ageMs(ts) {
      const time = new Date(ts).getTime();
      if (Number.isNaN(time)) return null;
      return Math.max(0, Date.now() - time);
    }
    function durationBetween(startTs, endTs) {
      const start = new Date(startTs).getTime();
      const end = new Date(endTs).getTime();
      if (Number.isNaN(start) || Number.isNaN(end)) return null;
      return Math.max(0, end - start);
    }
    function formatAge(ms) {
      if (ms === null || ms === undefined) return "알 수 없음";
      return formatDuration(ms);
    }
    function resultSummaryText(result) {
      if (result.text) return result.text;
      if (result.data) return formatReadable(result.data);
      if (result.raw_response) return formatReadable(result.raw_response);
      if (result.message) return result.message;
      return "세션이 완료되었습니다.";
    }
    function parseAntigravityEnvelope(value) {
      if (typeof value !== "string") return { response: "", envelope: null };
      try {
        const parsed = JSON.parse(value);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return { response: value, envelope: null };
        return {
          response: typeof parsed.response === "string" ? parsed.response : typeof parsed.text === "string" ? parsed.text : value,
          stats: parsed.stats,
          metadata: parsed.metadata,
          error: parsed.error,
          envelope: parsed
        };
      } catch {
        return { response: value, envelope: null };
      }
    }
    function isBenignAntigravityStderr(event) {
      if (event.type !== "antigravity.stderr") return false;
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
      return ["completed", "valid", "invalid", "error", "cancelled"].includes(status);
    }
    function findCwd(events) {
      const started = [...events].reverse().find((event) => event.type === "antigravity.started" && event.payload?.cwd);
      return started?.payload?.cwd || "";
    }
    function metaLabel(key) { return key; }
    function formatMetaValue(value) {
      if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
      return JSON.stringify(value);
    }
    qs("refresh").onclick = () => loadSessions();
    qs("raw").onchange = async () => {
      if (qs("raw").checked && !confirm("원본 payload에는 민감한 내용이 포함될 수 있습니다. 이 로컬 토큰으로 원본을 보시겠습니까?")) qs("raw").checked = false;
      openEvents();
      if (currentSessionId) await loadTranscript(currentSessionId);
    };
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
    window.displayConversationTurns = displayConversationTurns;
    window.describeEventAsConversationTurn = describeEventAsConversationTurn;
    window.preferredLiveSession = preferredLiveSession;
    window.groupSessionsByConversation = groupSessionsByConversation;
    window.runTelemetry = runTelemetry;
    window.renderRuntimeSignal = renderRuntimeSignal;
    window.loadTranscript = loadTranscript;
    window.selectSession = selectSession;
    window.parseAntigravityEnvelope = parseAntigravityEnvelope;
    window.renderMarkdown = renderMarkdown;
    window.renderSessionGroup = renderSessionGroup;
    window.sessionTitle = sessionTitle;
    window.hasActiveTextSelection = hasActiveTextSelection;
    window.rawEventKey = rawEventKey;
    bindSessionListEvents();
    loadSessions().then(openEvents).catch(console.error);
  </script>
</body>
</html>
"""
