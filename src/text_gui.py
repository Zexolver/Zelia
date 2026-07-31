"""
A desktop chat window for ZELIA -- type to her without needing STT, like
chatting on the Claude website/app instead of a terminal. Same relationship
to the backend as text_repl.py: no brain/tool/memory logic of its own, just
a GUI front-end for the socket src/text_input.py opens in the main process.

Run with:  python -m src.text_gui
(needs ZELIA already running -- systemctl --user status zelia)
"""
import os
import socket
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

from src.config import load_config
from src.text_input import socket_path

BG = "#1e1e2e"
PANEL_BG = "#252537"
YOU_COLOR = "#89b4fa"
ZELIA_COLOR = "#cba6f7"
ERROR_COLOR = "#f38ba8"
TEXT_COLOR = "#e0e0e8"
MUTED_COLOR = "#7a7a8c"


class ChatWindow:
    def __init__(self, root: tk.Tk, socket_path_: str):
        self.root = root
        self.socket_path = socket_path_
        self.pending = False

        root.title("ZELIA")
        root.configure(bg=BG)
        root.geometry("560x680")

        body_font = tkfont.Font(family="sans-serif", size=11)
        label_font = tkfont.Font(family="sans-serif", size=10, weight="bold")

        self.history = tk.Text(
            root, bg=PANEL_BG, fg=TEXT_COLOR, insertbackground=TEXT_COLOR,
            font=body_font, wrap="word", state="disabled", relief="flat",
            padx=12, pady=12, borderwidth=0,
        )
        self.history.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.history.tag_configure("you_label", foreground=YOU_COLOR, font=label_font)
        self.history.tag_configure("zelia_label", foreground=ZELIA_COLOR, font=label_font)
        self.history.tag_configure("error_label", foreground=ERROR_COLOR, font=label_font)
        self.history.tag_configure("body", foreground=TEXT_COLOR)
        self.history.tag_configure("status", foreground=MUTED_COLOR, font=(body_font.actual("family"), 9, "italic"))

        input_row = tk.Frame(root, bg=BG)
        input_row.pack(fill="x", padx=10, pady=(0, 10))

        # The non-expanding sibling (the button) must be packed BEFORE the
        # expand=True one (the entry) -- packing them the other way around
        # left the button collapsed to a 1x1, unmapped widget when input_row
        # shares its parent with another expand=True widget (self.history
        # above). Confirmed via isolated reproduction: this is a real Tk
        # pack() geometry quirk, not a one-off rendering glitch -- found by
        # actually screenshotting the running GUI, not just eyeballing the
        # code.
        self.send_button = tk.Button(
            input_row, text="Send", command=self._send, bg=YOU_COLOR, fg=BG,
            font=label_font, relief="flat", padx=16, activebackground=ZELIA_COLOR,
        )
        self.send_button.pack(side="right", fill="y", padx=(8, 0))

        self.entry = tk.Text(input_row, height=3, bg=PANEL_BG, fg=TEXT_COLOR, insertbackground=TEXT_COLOR,
                              font=body_font, wrap="word", relief="flat", padx=10, pady=8)
        self.entry.pack(side="left", fill="both", expand=True)
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<Shift-Return>", lambda e: None)  # allow shift+enter for a literal newline
        self.entry.focus_set()

        if not os.path.exists(self.socket_path):
            self._append("ZELIA isn't running", "error_label",
                          f"No socket at {self.socket_path}. Start her first: systemctl --user start zelia", "body")

    def _append(self, label: str, label_tag: str, text: str, body_tag: str) -> tuple[str, str]:
        """Returns (block_start, block_end) indices -- including the blank
        separator line before the label -- so a caller can later delete
        exactly this block (used to replace the "…" pending placeholder)."""
        self.history.configure(state="normal")
        block_start = self.history.index("end-1c")
        if block_start != "1.0":
            self.history.insert("end", "\n\n")
        self.history.insert("end", label + "\n", label_tag)
        self.history.insert("end", text, body_tag)
        block_end = self.history.index("end-1c")
        self.history.configure(state="disabled")
        self.history.see("end")
        return block_start, block_end

    def _on_enter(self, event) -> str:
        if event.state & 0x0001:  # Shift held -- let the default newline behavior happen
            return ""
        self._send()
        return "break"

    def _send(self) -> None:
        if self.pending:
            return
        text = self.entry.get("1.0", "end").strip()
        if not text:
            return
        self.entry.delete("1.0", "end")
        self._append("You", "you_label", text, "body")
        self._pending_block = self._append("ZELIA", "zelia_label", "…", "status")
        self.pending = True
        self.send_button.configure(state="disabled")
        threading.Thread(target=self._worker, args=(text,), daemon=True).start()

    def _worker(self, text: str) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(self.socket_path)
                sock.sendall((text + "\n").encode("utf-8"))
                sock.shutdown(socket.SHUT_WR)
                buf = b""
                first = True
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        reply = line.decode("utf-8", errors="replace")
                        self.root.after(0, self._on_reply, reply, first)
                        first = False
        except (ConnectionError, OSError) as exc:
            self.root.after(0, self._on_error, str(exc))
        finally:
            self.root.after(0, self._on_done)

    def _on_reply(self, reply: str, first: bool) -> None:
        if first:
            # Replace the "…" placeholder (exact block, separator included)
            # with the real first reply.
            start, end = self._pending_block
            self.history.configure(state="normal")
            self.history.delete(start, end)
            self.history.configure(state="disabled")
            self._append("ZELIA", "zelia_label", reply, "body")
        else:
            self._append("ZELIA", "zelia_label", reply, "body")

    def _on_error(self, message: str) -> None:
        start, end = self._pending_block
        self.history.configure(state="normal")
        self.history.delete(start, end)
        self.history.configure(state="disabled")
        self._append("Connection lost", "error_label", message, "body")

    def _on_done(self) -> None:
        self.pending = False
        self.send_button.configure(state="normal")


def main() -> None:
    cfg = load_config()
    path = socket_path(cfg.install_dir)

    root = tk.Tk()
    ChatWindow(root, path)
    root.mainloop()


if __name__ == "__main__":
    main()
