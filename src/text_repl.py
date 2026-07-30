"""
Type to ZELIA instead of talking to her. A thin keyboard/screen client for
the socket src/text_input.py opens in the main process -- this file has no
brain/tool/memory logic of its own, it just connects, sends a line, and
prints back whatever comes over the wire.

Run with:  python -m src.text_repl
(needs ZELIA already running -- systemctl --user status zelia)
"""
import os
import socket
import sys

from src.config import load_config
from src.text_input import socket_path


def send(path: str, text: str) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(path)
        sock.sendall((text + "\n").encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                print(f"zelia> {line.decode('utf-8', errors='replace')}")


def main() -> None:
    cfg = load_config()
    path = socket_path(cfg.install_dir)
    if not os.path.exists(path):
        print(f"ZELIA isn't running (no socket at {path}).")
        print("Start her first: systemctl --user start zelia")
        sys.exit(1)

    print("Typing to ZELIA. Ctrl+C or Ctrl+D to quit.")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        try:
            send(path, text)
        except (ConnectionError, OSError) as exc:
            print(f"Lost connection to ZELIA: {exc}")
            break


if __name__ == "__main__":
    main()
