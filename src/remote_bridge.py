"""
HTTP bridge so a phone (or anything else reachable over Tailscale) can chat
with ZELIA the same way text_repl.py/text_gui.py already do -- this module
is deliberately just another *client* of the existing zelia.sock protocol,
not a new entry point into the agent. Every request opens a fresh
connection to zelia.sock, sends the message, and relays back whatever
line(s) come back before the connection closes -- identical semantics to
every other text client this project already has (see text_input.py's
docstring: for a request that routes to the large brain, that's just the
"working on it" acknowledgement; the eventual answer is spoken aloud, not
delivered over a connection that's most likely already closed by then).

Bound to all interfaces (0.0.0.0) rather than specifically the tailscale0
interface, for simplicity and because the machine's normal LAN is already
behind the home router/NAT -- the bearer token below is what actually
gates access, not the bind address. Text-only for now (matches the
mobile app's v1 scope).
"""
import http.server
import json
import socket
import threading

from src.utils.logger import get_logger

log = get_logger("remote_bridge")

RELAY_TIMEOUT_SECONDS = 300.0  # generous -- large-brain acks are quick, but don't cut off a slow small-brain reply


def _relay(socket_path: str, message: str) -> list[str]:
    lines = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(RELAY_TIMEOUT_SECONDS)
        sock.connect(socket_path)
        sock.sendall((message + "\n").encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                lines.append(line.decode("utf-8", errors="replace"))
    return lines


def _make_handler(socket_path: str, token: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A002 -- quiet; ZELIA's own logger covers what matters below
            pass

        def _reject(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _authorized(self) -> bool:
            if not token:
                return True  # no token configured -- treat as intentionally open (Tailscale-only network is the gate)
            return self.headers.get("Authorization", "") == f"Bearer {token}"

        def do_GET(self):
            if self.path == "/health":
                payload = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self._reject(404)

        def do_POST(self):
            if self.path != "/chat":
                self._reject(404)
                return
            if not self._authorized():
                self._reject(401)
                return

            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._reject(400)
                return

            message = (body.get("message") or "").strip()
            if not message:
                self._reject(400)
                return

            try:
                lines = _relay(socket_path, message)
            except OSError as exc:
                log.error("Could not relay to zelia.sock: %s", exc)
                self._reject(503)
                return

            payload = json.dumps({"reply": "\n".join(lines)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def start(socket_path: str, port: int, token: str) -> None:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _make_handler(socket_path, token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Remote bridge listening on 0.0.0.0:%d (POST /chat, GET /health)", port)
