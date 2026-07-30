"""
Local text input for ZELIA -- a typed-keyboard alternative to the wake
word/hotkey, so she's usable without saying a word out loud.

Implemented as a Unix domain socket the main process listens on, rather
than main.py reading its own stdin, because ZELIA normally runs headless
under systemd (no attached terminal to type into). src/text_repl.py is the
small client that actually reads your keyboard and connects here -- run it
from any terminal, any time, whether or not that terminal started her.

Protocol: one line in per connection (the request), one or more lines out
(every reply for that request, as it happens), then the connection closes.
For a request that routes to the large brain, that's just the "working on
it" acknowledgement -- the eventual answer is spoken aloud via TTS same as
it would be for a voice request, not delivered back over a socket that's
most likely already closed by then. See src/main.py's on_text handler.
"""
import os
import socket
import threading

from src.utils.logger import get_logger

log = get_logger("text_input")

SOCKET_NAME = "zelia.sock"


def socket_path(install_dir: str) -> str:
    return os.path.join(install_dir, SOCKET_NAME)


def start_text_input_server(install_dir: str, on_text) -> None:
    """
    on_text: fn(text: str, send_line: fn(str) -> None) -> None
    """
    path = socket_path(install_dir)
    if os.path.exists(path):
        os.remove(path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o600)  # local user only -- this is a full agent control channel
    server.listen(5)

    def handle_conn(conn: socket.socket) -> None:
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            text = buf.decode("utf-8", errors="replace").split("\n", 1)[0].strip()
            if not text:
                return

            def send_line(reply: str) -> None:
                try:
                    conn.sendall((reply + "\n").encode("utf-8"))
                except OSError:
                    pass  # client already disconnected -- fine, she still spoke it aloud

            on_text(text, send_line)

    def accept_loop() -> None:
        log.info("Text input socket listening at %s", path)
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handle_conn, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
