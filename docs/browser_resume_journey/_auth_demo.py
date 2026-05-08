"""Local HTTP auth demo used by the browser resume tutorial."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SESSION_COOKIE_NAME = "journey_session"
SESSION_COOKIE_VALUE = "demo-session"
LOCAL_STORAGE_KEY = "journey_session_token"
LOCAL_STORAGE_VALUE = "demo-token"
_PORT_FILE = Path(tempfile.gettempdir()) / "journey-browser-resume.port"

_SERVER: ThreadingHTTPServer | None = None
_SERVER_THREAD: threading.Thread | None = None
_BASE_URL: str | None = None
_LOCK = threading.Lock()


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class _AuthDemoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/_health":
            self._send_json(
                {
                    "status": "ok",
                    "service": "journey-browser-resume-demo",
                }
            )
            return
        if self.path == "/login":
            self._send_html(_login_page())
            return
        if self.path == "/dashboard":
            self._send_html(_dashboard_page())
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown page.")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/protected-action":
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown action.")
            return

        cookie_header = self.headers.get("Cookie", "")
        expected_cookie = f"{SESSION_COOKIE_NAME}={SESSION_COOKIE_VALUE}"
        if expected_cookie not in cookie_header:
            self._send_json(
                {"status": "missing session cookie"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return

        self._send_json({"status": "Protected action complete"})

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(
        self,
        payload: dict[str, str],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def ensure_demo_server() -> str:
    global _BASE_URL, _SERVER, _SERVER_THREAD

    with _LOCK:
        if _SERVER is not None and _BASE_URL is not None:
            return _BASE_URL

        port = _load_port()
        if port is None:
            port = _choose_port()
            _PORT_FILE.write_text(str(port), encoding="utf-8")

        try:
            server = _ReusableThreadingHTTPServer(("127.0.0.1", port), _AuthDemoHandler)
        except OSError as exc:
            raise RuntimeError(
                "Could not start the browser resume demo server. "
                "Run reset_demo_state() to choose a fresh port if needed."
            ) from exc

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        _SERVER = server
        _SERVER_THREAD = thread
        _BASE_URL = f"http://127.0.0.1:{port}"
        return _BASE_URL


def shutdown_demo_server() -> None:
    global _BASE_URL, _SERVER, _SERVER_THREAD

    with _LOCK:
        if _SERVER is None:
            return
        _SERVER.shutdown()
        _SERVER.server_close()
        if _SERVER_THREAD is not None:
            _SERVER_THREAD.join(timeout=1)
        _SERVER = None
        _SERVER_THREAD = None
        _BASE_URL = None


def reset_demo_port() -> None:
    _PORT_FILE.unlink(missing_ok=True)


def _load_port() -> int | None:
    if not _PORT_FILE.exists():
        return None
    return int(_PORT_FILE.read_text(encoding="utf-8"))


def _choose_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _login_page() -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>journey auth demo</title>
    <style>
      body {{ font-family: Georgia, serif; margin: 2rem; }}
      button {{ padding: 0.8rem 1.1rem; font-size: 1rem; cursor: pointer; }}
      #status {{ margin-top: 1rem; min-height: 1.5rem; color: #0f5132; }}
    </style>
  </head>
  <body>
    <h1>journey auth demo</h1>
    <p>This login stores one cookie and one localStorage token.</p>
    <button id="login-button">Sign in</button>
    <p id="status">Logged out</p>
    <script>
      const status = document.getElementById("status");
      document.getElementById("login-button").addEventListener("click", () => {{
        document.cookie = "{SESSION_COOKIE_NAME}={SESSION_COOKIE_VALUE}; Path=/";
        window.localStorage.setItem("{LOCAL_STORAGE_KEY}", "{LOCAL_STORAGE_VALUE}");
        status.textContent = "Logged in";
        window.location.assign("/dashboard");
      }});
    </script>
  </body>
</html>
"""


def _dashboard_page() -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>journey protected dashboard</title>
    <style>
      body {{ font-family: Georgia, serif; margin: 2rem; }}
      button {{ padding: 0.8rem 1.1rem; font-size: 1rem; cursor: pointer; }}
      #auth-state, #status {{ margin-top: 1rem; min-height: 1.5rem; }}
      #auth-state {{ color: #0f5132; }}
    </style>
  </head>
  <body>
    <h1>Protected dashboard</h1>
    <p>The protected action stays disabled until both the cookie and localStorage token exist.</p>
    <p id="auth-state">checking</p>
    <button id="protected-action" disabled>Complete protected action</button>
    <p id="status">Waiting</p>
    <script>
      const authState = document.getElementById("auth-state");
      const status = document.getElementById("status");
      const actionButton = document.getElementById("protected-action");

      function hasCookie(name, value) {{
        return document.cookie.split("; ").includes(`${{name}}=${{value}}`);
      }}

      function isAuthenticated() {{
        return (
          hasCookie("{SESSION_COOKIE_NAME}", "{SESSION_COOKIE_VALUE}") &&
          window.localStorage.getItem("{LOCAL_STORAGE_KEY}") === "{LOCAL_STORAGE_VALUE}"
        );
      }}

      function renderAuthState() {{
        const authenticated = isAuthenticated();
        authState.textContent = authenticated ? "authenticated" : "missing browser session";
        actionButton.disabled = !authenticated;
      }}

      actionButton.addEventListener("click", async () => {{
        renderAuthState();
        if (!isAuthenticated()) {{
          status.textContent = "Missing browser session";
          return;
        }}
        const response = await fetch("/api/protected-action", {{ method: "POST" }});
        const payload = await response.json();
        status.textContent = payload.status;
      }});

      renderAuthState();
    </script>
  </body>
</html>
"""
