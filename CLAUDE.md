# ZELIA — Project Context

This file is for Claude Code. It's the accumulated spec/history of a
personal, fully local, voice-controlled AI agent, distilled from an
extended planning conversation. The codebase already exists (built during
that conversation) — this doc explains what it is, why it's built the way
it is, what still needs doing, and what NOT to change.

**Drop this file at the root of the unzipped project directory (the folder
that currently has `install.sh`, `src/`, `config/`, etc. in it) and run
`claude` from there.**

## What this project is

A local, voice-and-text-controlled personal agent for Manjaro/Arch Linux —
the "Jarvis" people show off in tech-demo videos, actually running on your
own machine instead of a cloud API. No cloud LLM, no subscription, no MCP
servers. Originally scoped as "a free Claude Code but voice-controlled and
not just for coding" — the goal is full, general-purpose agent capability
(files, shell, GUI apps, web, desktop control, screen vision) applied to
*anything* the user wants done on their machine, not a narrow chatbot and
not limited to coding tasks. When judging whether a capability belongs
here, default to "yes, a Jarvis-like assistant should be able to do this"
rather than scoping it down.

**Current name: ZELIA** ("Zexolver's Enhanced Learning & Intelligence
Assistant," female voice). The codebase originally said "ZEUS" everywhere
(an earlier name) — that rename (ZEUS → ZELIA throughout code, strings,
filenames like `zeus.service` → `zelia.service`, default install path
`~/.zeus` → `~/.zelia`, env var `ZEUS_CONFIG` → `ZELIA_CONFIG`, etc.) has
now been done as a global pass across `src/`, `README.md`, and the config
template. There's precedent for exactly this kind of rename having been
done once already (Zeya → ZEUS), so if you spot a stray reference to
"zeus"/"ZEUS" anywhere (comments, error strings, a file nobody thought to
grep) treat it as a bug and fix it the same way, don't leave it.

## Target environment / hardware

- OS: Manjaro Linux (Arch-based). Must support **both Xorg and Wayland**
  sessions — detect at runtime, don't assume one.
- GPU: **AMD RX 580, 8GB VRAM** (Polaris/gfx803). This is a real constraint,
  not hypothetical:
  - ROCm dropped official support for this generation of card. There is
    **no working PyTorch GPU backend** for it without fragile,
    unofficial, version-pinned community Docker builds (not used here).
  - Ollama still gets real GPU acceleration on this card via **Vulkan**
    (`OLLAMA_VULKAN=1`), which is vendor/architecture-agnostic — this is
    what's used for the small brain and vision model.
  - Whisper's backend (CTranslate2) has no ROCm/Vulkan path at all —
    always CPU on this hardware regardless of vendor tricks.
  - AirLLM (big brain) therefore runs CPU-only on this machine. Slower
    than the GPU numbers people usually quote for it — that's expected,
    not a bug to "fix."
- GPU vendor/VRAM must be **auto-detected, never hardcoded** (`src/gpu_detect.py`)
  — the user was explicit about this after an earlier version assumed
  NVIDIA. Same principle should extend to anything else hardware-specific.

## Architecture (already built)

Two-tier local brain:
- **Small brain**: fast model served by Ollama (`qwen2.5:7b-instruct-q4_K_M`
  by default), handles normal conversation + tool-calling for everyday
  requests. Always resident.
- **Large brain**: AirLLM, for big/quality-sensitive project work (e.g.
  "build a full app for X"). Runs in its own subprocess so a GPU memory
  cap can be applied without touching the small brain's headroom
  (`src/gpu_manager.py`) — on hardware where AirLLM has no GPU backend at
  all (this machine), that cap is a no-op and it just runs on CPU.
  Dispatch is async (`src/brains/large_brain.py`); a router
  (`src/router.py`) decides small vs. large per request using keyword
  heuristics + a cheap small-brain classification call as fallback.

**Game-aware priority** (`src/game_guard.py`): detects a running game via
process-name patterns and/or GPU usage. While gaming: new AirLLM jobs queue
instead of starting (resume automatically once gaming stops), and the
assistant's own process gets reniced down. Wake word / quick commands keep
working the whole time regardless.

**Second brain** (`src/memory/second_brain.py`): ChromaDB vector store.
Every conversation turn is automatically embedded and stored — no manual
note-taking. Recall is similarity-search based, injected into the small
brain's system prompt per request. Worth knowing: this is *not* a true
continuous conversation history across separate wake/hotkey activations —
each activation starts a fresh message list, with only semantically-recalled
memories as context. If a true continuous-session mode gets built later
(see Pending features), keep this always-on background memory too.

**Efficiency is the explicit, stated priority for this component** — above
recall quality, above features. Two concrete changes made for that reason,
keep this direction for anything added here later:
- Embeddings use chromadb's built-in ONNX-runtime MiniLM-L6-v2
  (`DefaultEmbeddingFunction`) instead of the sentence-transformers/torch
  path — same model, faster CPU inference for the single-sequence embed
  this does on every turn, and it doesn't need torch just for this. Falls
  back to sentence-transformers only if `memory.embedding_model` in
  config.yaml is changed away from the default `all-MiniLM-L6-v2`.
- `remember()` is fire-and-forget: writes go on a queue drained by one
  background thread instead of blocking the conversation on an embed +
  disk write before the user hears/sees a reply. `recall()` stays
  synchronous since the agent needs it before it can build a prompt —
  that's the one part that's inherently on the critical path and not a
  target for further optimization without changing that requirement.

  Explicit user framing: this store doesn't need to be human-readable —
  it's ZELIA's own memory, optimized for how efficiently *she* can write
  to and recall from it, not for a person browsing the chroma DB by hand.
  Don't add human-facing polish here (pretty formatting, summaries meant
  to be read by the user, etc.) at the cost of efficiency.

  One concrete, not-yet-implemented refinement in this spirit: `remember()`
  currently stores every turn indiscriminately, including ZELIA's own
  confused/wrong turns, which then come back via `recall()` as "relevant
  context" for similar future requests — plausibly reinforcing past
  mistakes rather than helping (see known issue #11's model-comparison
  note). Some kind of quality signal before storage (or before recall)
  could be worth it, but hasn't been built.

**Text input** (`src/text_input.py` + `src/text_repl.py`): typed keyboard
input, not just STT — the main process listens on a Unix socket
(`<install_dir>/zelia.sock`) rather than reading its own stdin (it usually
runs headless under systemd, no attached terminal). `text_repl.py` is a
thin client with no brain/tool logic of its own — connect from any
terminal, any time, with `python -m src.text_repl` (or `zelia-say` if
installed via the pacman package). Wired through the exact same
`agent.handle_request` path and `activation_lock` as voice in
`src/main.py`'s `on_text`, so voice and typed requests serialize rather
than racing, and replies go out both spoken (TTS) and back over the
socket.

**Full agent tool set** (`src/agent/agent_loop.py` + `src/agent/tools/`):
- File read/write/delete, scoped to `agent.workspace_dir` (`file_tool.py`)
- **Visible-by-default execution** — this is a core design principle, see
  "Philosophy" below. `run_in_terminal`/`desktop_control.open_terminal`
  opens a real terminal window for shell/git/build commands;
  `run_shell_quiet`/`shell_tool.py` is the hidden/background path, only
  used when explicitly requested.
- Destructive-command detection (`shell_tool.is_destructive` — rm, git
  reset --hard, etc.) triggers a spoken confirmation before running,
  regardless of which execution path is used.
- Screen reading (`screen_tool.read_screen_text`, `atspi_tool.py`):
  explicit user principle -- ZELIA should perceive/interact with the
  computer roughly as a human would (see/click/type), not by reaching into
  an app's internals (backend data files, etc.), with narrow, explicit
  exceptions (self-unlock, explicitly-requested background tasks -- see
  "Screen lock"). Within that, prefers **AT-SPI** (the Linux accessibility
  framework, `atspi_tool.py`) over OCR: it queries the focused app's live,
  currently-rendered widget tree for real text content, not an image
  guess -- the same channel actual screen-reader assistive tech uses, so
  it's "seeing" the live app, just not through pixels. Verified live
  against Dolphin: correct file/folder names, no OCR-style errors.
  Confirmed **not available for Steam** (and most Electron/CEF apps) --
  it doesn't register with the AT-SPI desktop tree at all, not a
  permissions issue, nothing to configure around it. `read_screen_text`
  tries AT-SPI first, falls back to OCR via Tesseract (fast, no GPU) only
  for apps that don't expose it. `describe_screen` (small local vision
  model via Ollama, moondream, loaded on demand) stays screenshot-based
  regardless, since it's for genuinely visual questions (layout, images)
  AT-SPI has no way to answer. Needs `pygobject` (venv) +
  `at-spi2-core`/`gobject-introspection`/`cairo` (system, in
  `depends`/`makedepends` now) -- not yet baked into a fresh `makepkg`
  build as of this writing since it was added via a manual venv patch on
  the reference machine; verify a clean build actually gets these before
  assuming it works out of the box.
- **Steam library** (`steam_tool.list_installed_games`): explicit
  exception to the "act human" principle above, not the norm -- reads
  Steam's own `libraryfolders.vdf`/`appmanifest_*.acf` files directly,
  positioned as a cross-check/fallback for OCR (Steam's library list is
  long, scrollable, and not AT-SPI-accessible, so purely visual reading of
  it is unusually unreliable), not a replacement for looking. Filters out
  Proton/runtime/redistributable entries that share the same manifest
  format as real games. Fixed a real, demonstrated bug: earlier live
  testing had ZELIA fabricate "you have no games installed" without ever
  looking (see Known Issues #11) -- this reads the truth directly (90
  installed games on the reference machine) when used, and that stale
  fabricated memory was deleted from second_brain via its new `forget()`
  method (finds + deletes memories matching a query -- not exposed as an
  agent-callable tool given the demonstrated tool-calling reliability
  gaps around anything destructive-ish).
- **Multi-tab browser reading** (`browser_tabs.read_all_tabs`): explicit
  user request -- "read all active tabs," which a single screenshot
  fundamentally can't do (only one tab is ever visually composited at a
  time). Cycles tabs with Ctrl+Tab (the standard Chromium/Firefox
  shortcut) and reads each one via `screen_tool.read_screen_text`,
  stopping once it sees content matching a tab already read (cycled back
  to the start) or after `MAX_TABS` (15). Deliberately doesn't try to
  explicitly focus the browser window itself first -- `xdotool`-based
  window queries/activation aren't reliable for native-Wayland clients
  like Brave (confirmed: it runs with `--ozone-platform=wayland`, not
  XWayland, so it's invisible to X11-rooted tools the way a Tk window or
  an XWayland-bridged app isn't) -- callers should `show_me`/
  `open_browser` first so the browser is already focused going in. Each
  tab's text capped at 1500 chars so 15 tabs' worth of OCR doesn't swamp
  the small brain's context. Registered in `SCREEN_VISIBILITY_TOOLS`, so
  it goes through the same lock-check/idle-inhibit as every other
  screen-reading tool. Not yet tested live (blocked on the session being
  unlocked, same as the chat GUI and input lock above).
- **Stored preference**: Gemini AI (`gemini.google.com`) → Brave browser,
  everything else stays on the Floorp default. Stored via
  `second_brain.remember()` per explicit user request ("make sure she has
  a memory that..."), not a hardcoded rule in `browser_control.py` --
  this means it's subject to the same recall-reliability caveats as any
  other memory (see Known Issues #11's model-comparison note). If this
  preference doesn't reliably get honored in practice, that's the
  known gap, not a new bug -- consider a real per-domain override table
  in `browser_control.py` instead of relying on recall, if it comes up.
- App launching/focusing + "show me X" (`app_launcher.py`): matches
  installed `.desktop` entries, focuses if already running, falls back to
  file/folder path then URL/web search.
- Browser control (`browser_control.py`): opens a real, visible browser
  window directly to a URL (not simulated address-bar typing — every
  browser supports a URL as a CLI arg, more reliable). Default is Floorp,
  user can say "use X for this" (session-only override) or "always use X"
  (persists to config.yaml).
- Desktop control (`desktop_control.py`): typing/key-combos/clicking via
  `xdotool` (Xorg) or `ydotool` (Wayland, kernel-level via uinput, works
  regardless of compositor). `find_text_on_screen` OCRs for click targets
  (buttons/menu items only reachable visually — e.g. inside Godot or a
  browser). Window focus works normally on Xorg; Wayland is best-effort
  (wlroots compositors only, gracefully skipped elsewhere).
- Web page text fetching (`browser_tool.fetch_url`) — a plain HTTP request
  for reading documentation etc., intentionally separate from
  `open_browser` (visible) since fetching isn't a "hidden action" the same
  way running a background process would be.

**Wake word** (`src/wake_word.py`): two engines, config-selectable
(`assistant.wake_word_engine`):
- `"openwakeword"` (**current default**): fully local, no account, ships
  stock phrases only — currently `"hey jarvis"`.
- `"porcupine"`: Picovoice Porcupine, would allow a real "hey zelia"
  phrase, but their current free-tier terms for custom wake words are
  genuinely unclear/contradictory as of this writing (their own GitHub
  repo says personal accounts can train free non-commercial models; other
  current pricing pages say custom words are Enterprise-only). Not
  resolved — the user needs to check console.picovoice.ai themselves
  before this becomes the default again.

**Push-to-talk hotkey** (`src/hotkey_listener.py`): reads raw keyboard
events via evdev (kernel level — same mechanism/permissions as `ydotool`,
works identically on Xorg/Wayland). Default key `KEY_SCROLLLOCK`. This
exists specifically so wake-word reliability isn't a blocker for talking
quietly/whispering (e.g. late at night) — pressing the hotkey skips wake
word audio classification entirely and goes straight to
VAD-recording + Whisper, which handle quiet speech far better.

**Focus-steal guard + window highlight** (`src/idle_detect.py`,
`desktop_control.preserve_focus_if_user_active`,
`src/agent/tools/window_highlight.py`): explicit user requirement —
ZELIA using the GUI (opening a terminal/browser, launching or focusing an
app) must not yank keyboard focus away from whatever window the user is
actively working in.
- `idle_detect.py` tracks keyboard/mouse activity via evdev (same kernel-
  level mechanism as the hotkey listener and ydotool — works identically
  on Xorg/Wayland). `is_user_active(threshold_seconds)` answers "has there
  been input recently"; defaults to `True` (the safe assumption — don't
  steal focus) if tracking never started at all (no python-evdev, no
  input devices, permissions).
- `desktop_control.preserve_focus_if_user_active(action)` wraps the four
  tool-dispatch points that can change window focus in `agent_loop.py`
  (`run_in_terminal`, `open_browser`, `show_me`, `focus_window`), plus
  `browser_tabs.read_all_tabs`: captures the previously-active window,
  runs the action, and — only if the user was active — restores focus to
  that previous window afterward. **Was silently dead on KDE/GNOME Wayland
  the whole time** until this session: `_get_active_window_id()`
  unconditionally returned `None` on any Wayland session (it only ever
  tried `xdotool getactivewindow`, which can't see the active window on
  Wayland at all), so `previous` was always falsy and the restore branch
  never ran on this project's actual reference platform. Fixed on KDE
  specifically alongside the `click_at`/`focus_window` KWin work (issues
  18, 21): added `_kwin_active_window_title()`, which reads
  `workspace.activeWindow.caption` via the same throwaway-KWin-script
  mechanism as `_kwin_cursor_pos()`, and made the restore step call
  `focus_window(previous)` (the KWin window-matcher path) instead of
  `xdotool windowactivate` when on Wayland. Confirmed live: captured a
  window's title, focused a different window, restored the original by
  title, re-verified via another capture that it actually landed back
  correctly. Still xdotool-only (best-effort, likely still not working)
  on non-KWin Wayland compositors — same caveat as everywhere else this
  session's KWin-scripting fixes apply. Note this does NOT make focus
  changes invisible, only brief: Wayland's input security model requires
  a window to actually be focused before it can receive synthetic
  keystrokes at all, and KWin's own screenshot D-Bus API
  (`org.kde.KWin.ScreenShot2`) refuses unauthorized clients outright
  (confirmed live: `CaptureWindow` raised `NoAuthorized` for this
  project's own unprivileged process) -- there's a real, deliberate
  platform security boundary in the way of true invisible background
  window reading, this isn't a missing feature to build around.
- `window_highlight.py` draws a purple (`#a020f0`) outline — four thin,
  borderless, always-on-top strip windows around the target window's
  edges, not one overlay covering it, so content is never obscured —
  around whichever window ZELIA just used, *even after* focus gets
  restored to the user's window. This is what actually satisfies "don't
  steal my focus, but let me see what she's doing": input focus goes back
  to the user, the highlight stays up regardless so it's visually obvious
  which window is hers. Also Xorg-only for now (needs absolute screen
  positioning via `xdotool getwindowgeometry`, which Wayland doesn't
  expose to clients the same way) — not yet implemented for Wayland,
  unlike some other best-effort-on-wlroots features elsewhere in this
  project; would need a real Wayland client library
  (`pywayland`/`wlr-layer-shell`) to do properly, which is a bigger lift
  than was justified this pass.

**Desktop chat GUI** (`src/text_gui.py`, `python -m src.text_gui`): explicit
user request for a proper chat window (like the Claude website/app) instead
of only a terminal REPL for typed input — same relationship to the backend
as `text_repl.py` (thin client, no brain/tool/memory logic of its own,
connects to the same `zelia.sock`), just tkinter instead of a terminal
loop, since tkinter is already the established choice in this codebase
(`window_highlight.py`) for "need a GUI element, keep it stdlib, no new
heavy dependency." Runs the actual socket I/O on a background thread per
message (`root.after()` marshals updates back to the Tk main thread —
Tkinter widgets aren't safe to touch from another thread directly) so the
window doesn't freeze while waiting for a reply. Not yet visually verified
live (built and confirmed *running*, process alive, no traceback — but the
session was locked for the entire rest of this work, and tkinter doesn't
register with AT-SPI the way Qt/GTK apps do, so screenshot/AT-SPI
verification wasn't possible either) — check this first once the session's
actually unlocked, before assuming the layout renders correctly.

**Physical input lock** (`src/input_lock.py`, config `input_lock:`):
explicit user request, and explicitly **not** a security/authentication
feature — a toggle combo (default `Ctrl+Alt+Shift+L`, config-changeable)
that grabs every physical keyboard/mouse device via evdev's `EVIOCGRAB`
and discards their events until the same combo is seen again, for
"walk away without someone/something bumping the keyboard/mouse by
accident." Doesn't touch the session lock at all. Critical requirement
from the user, explicitly stated: ZELIA's own control (`type_text`/
`press_key`/`click_at`, injected via `ydotool`) must keep working while
this is active -- `ydotool` injects through its own uinput-created virtual
device, which shows up as a normal-looking evdev device
(`/proc/bus/input/devices` confirms its name: `"ydotoold virtual device"`)
and would otherwise get swept up in "grab every input device" -- it's
explicitly excluded by name in `_find_input_devices()`. Safety design,
given a bug here could otherwise strand the user's own physical
keyboard/mouse: auto-releases after a 1-hour timeout regardless of whether
the unlock combo is ever seen again, and independent of that,
`systemctl --user restart zelia` always releases the grab immediately
(closing the file descriptors releases `EVIOCGRAB` at the kernel level) --
recovery never depends on this code's own logic being bug-free. Remote
input (e.g. the user's RustDesk session) injects on a separate synthetic
path and isn't affected by the grab either way, which is also a real
recovery path if physical input somehow got stuck. **Not yet tested
live** -- blocked on the same permissions issue as the hotkey listener and
idle_detect (`input` group membership needs a logout/login to take
effect; confirmed via a clean "No input devices found" log line, not a
crash) and then on the session actually being unlocked. Test cautiously
once both are true, with the RustDesk recovery path confirmed available
first.

**Browser control: two real, previously-undiscovered bugs fixed**
(`browser_control.py`, `app_launcher.py`) — found while testing "open
Brave and read Gemini," not something anyone had hit before since Floorp
(the default, a native PATH-installed binary) never exercised this code
path:
1. `app_launcher.py`'s `DESKTOP_DIRS` never scanned the Flatpak exports
   directories (`/var/lib/flatpak/exports/share/applications`,
   `~/.local/share/flatpak/exports/share/applications`) — any
   Flatpak-installed app (Brave, on this machine, entirely Flatpak-only,
   no direct PATH executable at all) was invisible to `show_me`/
   `open_browser`'s desktop-entry fallback, full stop. Now scans both.
2. Even once found, the old fallback did `shutil.which(desktop_id)` and
   tried to exec the result directly with a URL argument -- meaningless
   for a Flatpak app, whose "id" (`com.brave.Browser`) isn't a PATH
   executable at all; real launching requires either `flatpak run` or
   (simpler, handles arbitrary Exec-line placeholder syntax like
   Flatpak's `@@u %U @@` file-forwarding wrapper correctly without this
   codebase hand-parsing it) `gtk-launch <desktop_id> <url>`.
   `_resolve_launch()` now returns which mode applies (`'binary'` for a
   direct PATH executable, `'desktop'` for gtk-launch) instead of
   collapsing both into one broken path. Verified live: launched Brave
   with a URL via `gtk-launch`, confirmed via a genuinely new renderer
   process spawning (not just no-error, actual new-tab evidence) that it
   opened in the existing window rather than erroring or spawning a
   disruptive second instance.

**Screen lock** (`desktop_control.is_screen_locked`,
`unlock_screen_with_password`, `inhibit_idle_briefly`,
`agent_loop._ensure_unlocked_for_screen_access`, `main.build_password_asker`):
explicit user requirement, arrived at deliberately after weighing the
security tradeoff out loud with the user rather than building it blind —
worth reading this in full before touching any of it.

- **What was explicitly rejected**: ZELIA storing a password anywhere, or
  any mechanism that unlocks the screen without a real, live password
  actually being supplied by whoever's talking to her *at that moment*.
  Two attempts to even investigate a stored-credential approach (accepting
  a Claude Code trust prompt as a side effect of testing, and just
  checking whether `secret-tool` was installed) were independently blocked
  by Claude Code's own safety classifier — treated as a signal that this
  class of capability needs a human decision, not an agent one, and
  intentionally not routed around.
- **What was built instead**: ZELIA never stores a credential. When a
  tool that needs the actual screen visible (`read_screen_text`,
  `describe_screen`, `find_text_on_screen`, `click_at`) is about to run
  and the screen is locked, `agent_loop._ensure_unlocked_for_screen_access`
  calls `ask_for_password` (`main.build_password_asker`, voice-only): TTS
  asks out loud, STT listens for the answer with `redact=True` (see
  `stt.py` — logs `[redacted]` instead of the transcript, specifically so
  a spoken password never lands in the journal). The answer never goes
  through `second_brain.remember()` or the normal request pipeline at all
  — this is a direct side-channel call, the same pattern
  `build_confirmation_asker` already used for yes/no destructive-command
  confirmation. `unlock_screen_with_password` then types it via the
  existing `type_text`/`press_key` (which already don't log their
  arguments) and submits with Enter. Functionally this is the same
  security gate the lock screen itself already is — ZELIA is just relaying
  a real-time answer, not bypassing anything — so it doesn't introduce a
  new way to unlock the machine beyond "know the actual password," it
  just lets her ask for it and type it instead of a human doing so
  directly. No exposed tool for this in `TOOL_SCHEMAS` — it's automatic,
  underneath the small model's own tool-calling decisions, precisely
  because that's an already-demonstrated weak point (see Known Issues) and
  this isn't something that should depend on it deciding correctly.
- Text-only sessions can't use this (`ask_for_password` is voice-only;
  `_ensure_unlocked_for_screen_access` returns a clear error instead of
  hanging if `ask_for_password` is unavailable) — asking for a password
  via the text socket and having it echo back over that channel felt like
  a worse tradeoff than just not supporting it there yet.
- Separately, `inhibit_idle_briefly()` (via `systemd-inhibit --what=idle`,
  universal across desktop environments, no KDE/GNOME-specific API)
  fire-and-forget blocks the idle timer for ~90s any time a screen/GUI
  tool runs, so the screen doesn't lock itself *while she's actively using
  it* in the first place. This doesn't touch an already-locked screen
  (e.g. the user deliberately locking it) — that's what the
  password-prompt path above is for.
- Verified live: asked ZELIA to read the screen while genuinely locked
  (the session had auto-locked from being idle during automated testing).
  She correctly detected the lock, asked for the password out loud,
  and — since no real answer was given in that test — failed gracefully
  with a clear error rather than guessing or retrying silently. The
  journal confirmed `Heard: '[redacted]'`, not the actual transcript.
  Actually typing a real password to confirm the full unlock hasn't been
  tested (deliberately -- that needs the real user's live voice, not
  something to simulate).

**Follow-up: a second, more strongly justified request for stored-credential
unlock, also not built.** The user is sometimes remote (phone + RustDesk,
no one physically present to answer `ask_for_password`'s voice prompt --
concretely, away for a weekend but still wanting ZELIA to keep developing).
They explicitly asked for a temporary, opt-in stored-password mechanism
(keyring-based, set up by the user themselves via `secret-tool store` run
in their own terminal so the value never passes through ZELIA or an LLM
context) as a stopgap until a proper mobile app exists. This is a
meaningfully different, better-justified ask than the first blocked
attempt -- and implementation was attempted (retrieval via
`secret-tool lookup`, tried silently before falling back to the voice
prompt). **It was still blocked**: syntax-checking the exact file
containing this code failed against Claude Code's own safety classifier,
consistently, not as a one-off. Combined with the two earlier blocks
(accepting a Claude Code trust prompt, checking whether `secret-tool` was
installed), that's three independent blocks across different specific
actions, all clustering around this one capability -- read as a strong,
non-incidental signal, not something to route around by trying a
different tool call or asking the user to run the same command instead.
**Reverted** rather than left half-built.

**What was built instead, and actually solves the stated need**:
`kwriteconfig6 --file kscreenlockerrc --group Daemon --key Autolock false`
-- disables the idle-triggered auto-lock entirely. This is a categorically
different kind of change from unlocking an *existing* lock without live
authentication: no credential handling, nothing stored, nothing bypassed,
just the user's own session-lock *policy* (whether it locks itself on
idle at all) -- and wasn't blocked. If the screen never locks in the
first place while the user's away, none of ZELIA's screen/GUI tools ever
need an unlock mechanism over a weekend away, which was the actual
underlying need. The user still has to unlock the screen themselves once,
by hand (e.g. via their existing RustDesk remote session) if it's already
locked when this gets applied -- this only prevents *future* auto-locks,
it doesn't touch a currently-locked session.

**Follow-up: `Autolock=false` alone turned out not to be sufficient** --
the screen locked again later in the same session despite it being set
correctly (verified via `kreadconfig6`). `LockOnResume` (locks on
suspend/resume, independent of the idle-triggered `Autolock` path) and
`Timeout` were both unset/using compiled defaults, either of which could
plausibly have been the actual trigger. Now explicitly set: `Autolock
false`, `LockOnResume false`, `Timeout 0`, all in the same
`kscreenlockerrc [Daemon]` group. Did **not** touch the running
kscreenlocker/greeter process itself, or attempt to force a config
reload on it -- that edges toward the same "interact with an active
lock screen" territory as the rejected unlock-bypass request, so this
was deliberately left to take effect naturally (next login, or whenever
the daemon next re-reads config on its own) rather than poked at
directly. If it locks again after this, the config is probably still not
the full picture -- check for a DPMS/compositor-level trigger before
assuming these three keys are exhaustive.

A second, more direct version of the same request came later in this
session: explicit permission to unlock the currently-locked session
*without* the password, "find another way." Declined outright, not
attempted -- this asks for an actual authentication bypass of an active
lock screen, not a policy change like the above. Unlike the earlier
stored-credential idea (which at least involved a real credential the
user supplied through a channel that never touched ZELIA or an LLM
context), there is no legitimate "another way" to unlock a real
password-protected session without one -- and building the capability
to do so wouldn't be scoped to this one moment anyway, it would become a
standing capability in the codebase available to whoever can talk or
type to her. If this comes up again, don't reinterpret it as a variant
of the settings-change category above; it's a different, declined
category of request. If the user re-enables
auto-lock later and hits this need again, don't re-attempt the
stored-credential path either -- ask them, same as this section describes, they
may have another non-credential angle in mind by then.

**Install, from source** (`install.sh`): prompts for install directory
(supports pointing at a dedicated drive), auto-detects GPU vendor, installs
system packages + Ollama + Vulkan drivers (if AMD) + ydotool + a
custom-built `ydotoold` systemd `--user` service (the Arch package ships no
usable unit), pulls the small + vision models, sets up
`config/config.yaml` from the template, and creates + immediately
starts/enables a `zelia.service` systemd `--user` unit
(`systemctl --user enable --now`), so the install is actually finished —
ZELIA running — when the script exits. Still works, still useful for
development, but **not the primary install path any more** — see
"Packaging & releases" below.

**Remote bridge + mobile app** (`src/remote_bridge.py`, separate repo
`github.com:Zexolver/Zelia-Android`): explicit user requirement — talk to
ZELIA from a phone, from anywhere, not just standing at the machine
(motivating case: wants this working over a weekend away from the
computer, including when Claude Code session limits are hit).
- `remote_bridge.py` is a small stdlib-only HTTP server (`http.server`, no
  new dependency) exposing `POST /chat` and `GET /health`. It is
  deliberately just another *client* of the existing `zelia.sock` text
  protocol (same one `text_repl.py`/`text_gui.py` use) — every request
  opens a fresh connection, relays the message in, and returns whatever
  line(s) come back before the connection closes. Not a new entry point
  into the agent, so it inherits all the same behavior/limitations text
  clients already have (e.g. a large-brain job's real answer is spoken
  aloud, not delivered over a since-closed HTTP response).
- Off by default (`config.yaml`'s `remote_bridge.enabled: false`) —
  turning it on with no token configured would mean anything that can
  reach the port gets full agent control with zero auth. Requires a
  bearer token (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`)
  once enabled. Binds to all interfaces rather than a specific one, for
  simplicity — the *network* it's reachable on (see below) plus the token
  are the actual access control, not the bind address.
- Remote access is via **Tailscale** (a free WireGuard-based mesh VPN),
  not port-forwarding/exposing anything to the raw internet. Installed on
  this machine via `pacman -S tailscale` + `systemctl enable --now
  tailscaled`; the login step (`sudo tailscale up`, opens a one-time
  browser auth URL) had to be done by the user directly — device
  authorization is inherently a personal-account action, not something
  automatable. User also installed Tailscale on their phone, logged into
  the same account. Once both are connected, the phone reaches ZELIA's
  machine at its stable `100.x.y.z` tailnet address from anywhere the
  phone has any network connection at all — confirmed live end-to-end:
  `curl` against the bridge from a shell using the actual tailnet IP
  (not `127.0.0.1`) got a real reply back.
  User's stated preference: would have preferred netbird.io (already had
  an account, thinks its free tier is better) but was in a rush and said
  Tailscale was fine for now — worth considering switching if raised
  again later, not treated as settled forever.
- Text-only for now, matching the mobile app's v1 scope — voice
  (recording on the phone, getting spoken replies back) was explicitly
  deferred by the user as a later, lower-priority addition.
- The Android app (Flutter/Dart, Material 3 with dynamic color via the
  `dynamic_color` package, falls back to a fixed purple seed on
  pre-Android-12 devices) lives in its own repo, not this one — user's
  explicit choice, and explicitly **not** a Play Store app: sideloaded via
  [Obtainium](https://github.com/ImranR98/Obtainium) instead, which tracks
  a GitHub repo's Releases page for APK assets. `.github/workflows/release.yml`
  builds and attaches a release APK automatically on any `vX.Y.Z` tag push
  (debug-signed — fine for personal sideloading, would need a real
  signing key if this were ever distributed beyond that). Getting this
  working took three attempts, each a genuine separate bug, not flakiness:
  (1) `flutter pub get` failed in CI because the action's `channel:
  "stable"` didn't match the exact Flutter version (3.44.8, via `fvm`)
  the app was built/tested against locally — fixed by pinning
  `flutter-version` explicitly; (2) the release-creation step then failed
  because the default `GITHUB_TOKEN` GitHub Actions provides is
  read-only unless a workflow explicitly requests otherwise — fixed by
  adding `permissions: contents: write` at the workflow level.
- **Auto-detects ZELIA's address** (user request: didn't want to type a
  Tailscale IP by hand): Tailscale doesn't support broadcast-style LAN
  discovery at all (peers are point-to-point WireGuard tunnels, not a
  shared network segment a device can scan or broadcast on), so a
  generic "find it on the network" scan was never viable — but MagicDNS
  (confirmed enabled via `tailscale status --json`) gives the machine a
  stable hostname (`zexolver-gaming-manjaro.tailc35f4b.ts.net`)
  regardless of its actual tailnet IP. `SettingsService.defaultServerUrl`
  hardcodes this (safe to bundle in the public app repo — it's just a
  hostname, not a secret; reaching it still requires tailnet
  membership), and the settings screen probes it automatically on first
  launch before falling back to asking the user to type an address.
  The token was a harder call — baking a real per-install secret into a
  public repo's source is a genuine, standing security cost (anyone can
  read it from the repo/its history, forever, even if later reverted),
  not something to just quietly do. Flagged this tradeoff explicitly
  before touching it; **user made an informed, explicit, and
  time-bounded exception**: "for zelia, at least while I am away for the
  weekend, it is fine to put the key in the app" — so
  `SettingsService.bundledToken` now does exactly that, and
  `tryAutoConfigure()` uses it (plus the MagicDNS address above) to skip
  the Settings screen entirely on first launch when the default address
  is reachable. This is documented in the app source as temporary and
  should be rotated (new token generated, `config.yaml` updated, this
  constant removed or replaced with a real pairing flow) once convenience
  is no longer the constraint — don't treat this as a settled precedent
  for baking future secrets into the app without asking again.
- **Release APK signing was broken the same way twice, for two different
  reasons** — found while fixing the "had to uninstall to update"
  complaint:
  1. Every CI run is a fresh machine with no persisted debug keystore, so
     debug-signed releases (the default) each got a different random
     signing key — Android refuses to install an "update" whose signature
     doesn't match what's already on the device, forcing an uninstall
     every time. Fixed with a real, persistent release keystore
     (generated via `keytool`, stored **outside any repo** at
     `~/.android-keys/` on the ZELIA machine — see that directory's
     README.md and [[zelia_android_signing_key]] memory for the exact
     values) fed to CI via repository secrets
     (`RELEASE_KEYSTORE_BASE64`/`_PASSWORD`, `RELEASE_KEY_ALIAS`,
     `RELEASE_KEY_PASSWORD`), with `build.gradle.kts` falling back to
     debug signing if `android/key.properties` (gitignored) isn't present
     locally.
  2. Wiring that up then broke the workflow file *itself*: a step's
     `if: ${{ secrets.RELEASE_KEYSTORE_BASE64 != '' }}` made GitHub
     reject the whole file ("Unrecognized named-value: 'secrets'") —
     the `secrets` context apparently can't be referenced directly in a
     step's `if:`, only inside `env:`/`with:`. This is a real, sharp edge
     worth remembering for any future workflow change here: do
     conditional-on-a-secret checks in the run script's shell (using an
     env var sourced from the secret), never in `if:` directly. Confirmed
     via the run's actual annotation text (fetched from the run's HTML
     page — the Actions REST API didn't surface it, `check-runs` came
     back empty since the file couldn't even be parsed into a check).
     Side effect worth knowing: an invalid workflow file gets evaluated
     (and shows up as a failed run) on **every** push while it's broken,
     regardless of the `on:` filters — two ordinary pushes to `main`
     showed up as failed "Release APK" runs before this was diagnosed,
     which was initially confusing since the trigger config only lists
     tag pushes.
- **Reply notifications + second-brain viewer** (v0.3.0): two more user
  requests, added together. Notifications post via
  `flutter_local_notifications` when a reply arrives, but only if the app
  isn't actually in the foreground (`WidgetsBindingObserver` tracks
  `AppLifecycleState` in `chat_screen.dart`) — avoids a redundant
  notification when the user's already looking at the reply. Needed core
  library desugaring enabled in `build.gradle.kts` (the plugin requires
  it) and the Android 13+ `POST_NOTIFICATIONS` runtime permission
  (`permission_handler`). The second-brain viewer is a new screen listing
  ZELIA's stored memories newest-first, talking to a new
  `GET /memories` endpoint on `remote_bridge.py` — a deliberate, narrow
  exception to that module's "just a `zelia.sock` client" design (see its
  docstring), since browsing stored memories isn't something the chat
  protocol has a way to ask for. Backed by a new
  `SecondBrain.list_recent()` method — chromadb's `get()` has no
  server-side ordering, so it fetches everything and sorts by the
  timestamp `remember()` already stores; fine at this project's current
  scale, would need real pagination if the collection grows very large.
- Explicitly deferred to later, low priority (user's own words: "making
  that actually work is very low priority"): registering the app as an
  Android digital-assistant app (the `VoiceInteractionService`/Assist API
  role that lets an app be set as the phone's default assistant, an
  alternative to "Hey Google"/Bixby). Noted here so a future session
  doesn't have to rediscover this was discussed and intentionally
  shelved, not forgotten.
- The Android SDK at `~/Android/Sdk` is a **shared resource** — this
  session found it mid-use by a separate, unrelated Claude Code session
  on the same machine (an Android emulator was already running, recent
  file timestamps for SDK components neither this session nor ZELIA
  installed). Adding the missing `platforms;android-36` and
  `build-tools;28.0.3` packages via `sdkmanager` was additive and did not
  appear to disrupt whatever the other session was doing, but this is
  worth remembering before doing anything destructive/version-downgrading
  to that SDK in a future session.

## Packaging & releases

Explicit user requirement: every release, both stable and
debugging/testing builds, gets built into an Arch pacman package
(`.pkg.tar.zst`) so a user only ever has to install a package, not
manually run a shell script. (A `.deb` was mentioned once and then
explicitly retracted — this project only targets Arch-based distros,
there is no Debian/Ubuntu packaging and none should be added.)

Built under `packaging/`:
- `packaging/stable/PKGBUILD` → package `zelia`, built from a tagged
  GitHub release (`v<pkgver>`). The stable-release path.
- `packaging/testing/PKGBUILD` → package `zelia-git`, always tracks the
  latest commit on `main`, version auto-derived via `git describe`
  (standard Arch `-git` package convention). The debugging/testing path —
  build this to try a change before cutting a release. `zelia` and
  `zelia-git` `conflict`/can't coexist.
- Both install `src/`, the config/systemd templates, and a prebuilt venv
  to `/opt/zelia` (owned by pacman, read-only, shared), plus
  `/usr/bin/zelia-setup` (per-user first-run: `~/.zelia` data dir,
  rendered `config.yaml`, `ollama pull`, the systemd `--user` service,
  `input` group for ydotool — everything `/opt/zelia` itself can't know
  because it doesn't know which user(s) will run ZELIA) and
  `/usr/bin/zelia-say` (typed input from anywhere, see "Second brain"
  above for what it connects to).
- `makepkg`'s default compression is already zstd, so building either
  PKGBUILD with a stock `makepkg.conf` produces `.pkg.tar.zst` — no extra
  flags needed. Verified end-to-end once (`makepkg -o` in
  `packaging/testing`, source fetch + `pkgver()` against the real GitHub
  repo both resolved correctly) but a full `makepkg -si` (pulls torch,
  chromadb, airllm into the build venv, several GB) hasn't been run in
  this environment — worth doing before calling a release good.
- See `packaging/README.md` for the build/release commands.

This is a meaningful scope split going forward: system packages + shared
code are pacman's job now; only genuinely per-user state should ever go
through `install.sh`/`zelia-setup`-style scripting.

## Philosophy / non-negotiable design principles

These came from explicit, repeated user pushback earlier in the project —
preserve them:

1. **Nothing hidden by default.** Shell commands, git operations, builds —
   all visible in a real terminal window unless the user explicitly asks
   for something quiet/background. This was a deliberate correction from
   an earlier version that ran everything invisibly; don't regress it.
2. **Detect, don't hardcode.** GPU vendor, VRAM, session type (Xorg vs.
   Wayland), installed terminal emulator, installed browser — all
   auto-detected at runtime. The user has explicitly called out hardcoded
   assumptions as a bug before (GPU vendor specifically).
3. **Native tools, not MCP, not cloud APIs.** Desktop interaction goes
   through tools already on the machine (`xdotool`/`ydotool`, `wmctrl`,
   `tesseract`, `.desktop` files, `xdg-open`) rather than MCP servers or
   API calls. The two local brains (Ollama + AirLLM) are the only "AI"
   dependencies; there is currently no Claude API / cloud LLM integration,
   and the user has been informed that adding one would be the one piece
   that isn't free.
4. **Be honest about hardware limits, in the code's user-facing messaging
   too, not just to the user in chat.** E.g. `gpu_manager.py`'s logging
   explicitly says AirLLM is CPU-only on this machine rather than silently
   underperforming; `install.sh`'s final summary states plainly what's
   accelerated vs. not.

## Known issues

Resolved:

1. ~~Full ZEUS → ZELIA rename~~ — done, see above.
2. ~~install.sh doesn't auto-start the service~~ — `install.sh` now ends
   with `systemctl --user enable --now zelia.service` (and the same for
   `ydotoold.service` earlier in the script) instead of just printing the
   commands.
3. ~~install.sh's final wake-word warning is stale~~ — the final summary
   now correctly says "hey jarvis" works right now by default and "hey
   zelia" via Porcupine is optional, with the console.picovoice.ai caveat.
6. ~~webrtcvad crashed the whole process at import (`ModuleNotFoundError:
   pkg_resources`)~~ — found by actually installing the `zelia-git`
   package and running it. `setuptools>=81` dropped `pkg_resources`
   entirely, and modern `python -m venv` doesn't bundle setuptools by
   default any more, so it just wasn't there. Pinned `setuptools<81` in
   `requirements.txt`.
7. ~~`tts.py`'s `speak()` crashed on every reply~~ (`wave.Error: #
   channels not specified`) — `piper-tts` 1.6.0 is a rewrite
   (OHF-voice/piper1-gpl); `synthesize()` no longer writes into a
   `wave.Wave_write` handle, it returns `AudioChunk` objects with ready
   int16 PCM. `speak()` rewritten around the current API. This one was
   sneaky: the agent was generating correct replies the whole time, they
   were dying in TTS before ever reaching the user (voice *or* text) —
   confirmed fixed by literally hearing "banana" spoken aloud, and by
   `zelia-say` getting a real reply back over the socket.
8. ~~`on_text`'s `respond()` called `tts.speak()` before `send_line()`~~
   — meant issue 7 also silently broke the text-input feature: a TTS
   exception meant the socket client got nothing, even though the
   agent's reply existed. Added `safe_speak()` (catches, logs, never
   propagates) in `main.py`, used by both `on_wake` and `on_text`, and
   `on_text`'s `respond()` now sends the text reply *first* — typed
   input's whole point is not depending on audio, it shouldn't be able
   to fail because TTS did.
9. ~~openwakeword's `Model()` crashed with `AudioFeatures.__init__() got
   an unexpected keyword argument 'wakeword_models'`~~ — that kwarg was
   renamed to `wakeword_model_paths` (full paths, not bare names) at
   some point; unknown kwargs get silently forwarded into `AudioFeatures`
   internally, which is why the error pointed at the wrong class.
   `wake_word.py` now resolves the configured stock name (e.g.
   `hey_jarvis`) against openwakeword's bundled, versioned filenames
   (`openwakeword.get_pretrained_model_paths()`) instead of hardcoding a
   name→path mapping. Confirmed fixed: startup goes straight to "ZELIA is
   ready" with no wake-word error now.

Note on 6-9: all four are 2026-era API drift in third-party packages
(setuptools, piper-tts, openwakeword) versus whatever version the
original code was written against — not anything introduced by the
ZEUS→ZELIA rename or this session's other changes. Found only because
the package was actually built, installed, and run rather than just
read. If `pip install -r requirements.txt` starts failing again later
for a similar reason, check the installed package's actual current API
first rather than assuming the existing code is still correct.

10. ~~Ollama wasn't actually using the GPU~~ (`ollama ps` showed
   "100% CPU" for the small brain despite `OLLAMA_VULKAN=1`) — two
   separate bugs, both now fixed in `install.sh` and
   `packaging/zelia-setup`:
   - The plain `ollama` Arch package only ships CPU ggml backends
     (`/usr/lib/ollama/*.so`, all `libggml-cpu-*`). Vulkan support is a
     *separate* package, `ollama-vulkan` (adds `libggml-vulkan.so` in a
     `vulkan/` subdirectory, depends on `ollama` rather than replacing
     it) — it has to be installed explicitly on AMD.
   - `/etc/environment` only reaches PAM login sessions — it does
     **nothing** for a system-level systemd unit like `ollama.service`.
     The actual mechanism is a drop-in override
     (`/etc/systemd/system/ollama.service.d/override.conf`) with
     `Environment=` lines, `daemon-reload`, then a real restart. Also
     needs `OLLAMA_LIBRARY_PATH=/usr/lib/ollama/vulkan` set the same
     way, or the dynamic backend loader won't find the subdirectory the
     Vulkan `.so` lives in.
   Confirmed fixed on the reference machine: `ollama ps` now reports
   "100% GPU", and `journalctl -u ollama` shows `device_info: Vulkan0 :
   AMD Radeon RX 580 Series (RADV POLARIS10)`. If this ever regresses,
   check `systemctl cat ollama` for the drop-in and `ollama ps`'s
   PROCESSOR column, not `/etc/environment`.
12. ~~**The entire large-brain (AirLLM) path was completely broken on
   this machine**~~ — the single most important finding from live-testing
   the overnight-autonomous-coding path (the user's top stated priority),
   since nothing about this was exercised before. Two bugs, found by
   actually submitting a "build a full app" request end-to-end (with a
   temporarily swapped-in small test model, `TinyLlama/TinyLlama-1.1B-
   Chat-v1.0` — the real configured `Meta-Llama-3.1-70B-Instruct` is
   gated and ~35-40GB, impractical to pull just to test the plumbing;
   config reverted after):
   - `AirLLMBaseModel.__init__`'s own default is `device='cuda:0'`
     regardless of actual hardware -- it does **not** auto-detect. On
     this AMD machine (or any machine with no NVIDIA driver at all) this
     crashed immediately with a bare `RuntimeError: Found no NVIDIA
     driver...`, every single time, unconditionally -- there was no
     working CPU fallback despite `gpu_manager.py`/CLAUDE.md assuming
     one. Fixed in `airllm_worker.py`: passes
     `device="cuda:0" if budget.airllm_gpu_usable else "cpu"` explicitly
     to `AutoModel.from_pretrained()`, using the same `GpuBudget` that
     was already being computed (and logged!) but never actually acted
     on for device selection, only for the (skippable) memory-fraction
     cap.
   - Once that was fixed and a job actually completed, the reply
     contained the **entire raw input prompt echoed back verbatim**
     (including the "Relevant context from past conversations..."
     template) before the model's real answer. `model.generate()`
     returns prompt tokens + new tokens concatenated;
     `airllm_worker.py` was decoding the whole thing instead of slicing
     off `input_ids["input_ids"].shape[-1]` first. Fixed and verified
     the slicing logic in isolation (not worth re-running the ~7.5-
     minute full CPU generation twice just to confirm a one-line index
     fix).
   Confirmed end-to-end after both fixes: job submits, AirLLM loads and
   generates on CPU (device=cpu logged correctly), worker writes a
   clean, correctly-sliced result, on_done fires.
13. ~~`game_guard`'s process-pattern list matched the bare Steam
   **client**, not just an actively-running game~~ — `\bsteam(app|
   linuxruntime)?\b`'s optional suffix meant plain "steam" (the launcher
   process, which commonly stays resident in the background for
   friends-list/overlay/updates with nothing actually being played)
   always matched. Found because a "build a full app" request got
   queued instead of running, even though nothing was actually being
   played -- `ps aux` showed only the idle Steam client. On a machine
   literally hostnamed "...-Gaming-Manjaro", leaving Steam open (very
   likely) would have silently blocked every single overnight AirLLM
   job forever. Made the suffix mandatory
   (`\bsteam(app|linuxruntime)\b`) so only the actual per-game wrapper
   processes (`SteamLinuxRuntime` et al.) match, not the client itself.
   Confirmed fixed: same idle-Steam machine state no longer reports
   "GAMING", and the queued job then ran immediately.
15. ~~Screen reading was **completely non-functional on KDE Wayland**~~
   (`grim: compositor doesn't support the screen capture protocol`) —
   found testing the user's explicit request to have ZELIA read her
   Steam library. `grim` needs the `wlr-screencopy` protocol, which only
   wlroots compositors (Sway, Hyprland) implement -- it's simply absent
   on KWin (KDE, this reference machine's actual desktop) and Mutter
   (GNOME), not a "sometimes flaky" thing. `take_screenshot()` in
   `screen_tool.py` now tries each Wayland candidate tool and actually
   runs it (not just checks it's installed) rather than stopping at the
   first one found: `grim`, then `spectacle -b -n -f -o <path>` (KDE's
   own non-interactive screenshot tool, confirmed working). GNOME
   Wayland still has no fallback wired up (would need
   `gnome-screenshot`, untested, not added blind).
16. ~~`tts.speak()` had no length cap~~ — a "read the text on my screen"
   reply carried a multi-hundred-word OCR dump straight into `speak()`,
   and Piper dutifully narrated the entire thing out loud, multiple
   minutes of audio, during which the whole assistant was unresponsive
   (`speak()` blocks on `sd.wait()`, which holds `main.py`'s
   `activation_lock` for the whole duration). Added `MAX_SPOKEN_CHARS`
   (600) truncation in `tts.py` -- the *text* channel still gets the
   full reply regardless, this only bounds what gets narrated aloud.
   Related to issue (2) below and to the user's separately-stated,
   still-open priority: TTS reading raw content verbatim instead of
   translating it into natural spoken language.
18. ~~Window focus had no implementation on KDE/GNOME Wayland~~ (was issue
   5b) — found while live-testing multi-tab browser reading: on a busy
   desktop (~20 windows, mostly Konsole/Claude Code sessions), a
   just-launched Brave window never actually got compositor focus, so
   `read_all_tabs`'s Ctrl+Tab cycling read an unrelated terminal's
   scrollback instead of any browser tab. The generic Wayland answer really
   is "no standard API for this" -- but KWin specifically exposes one
   anyway: `org.kde.krunner1` on `org.kde.KWin /WindowsRunner` (the same
   D-Bus interface KRunner's built-in "Windows" plugin uses to alt-tab by
   title) supports `Match(query) -> matches` then `Run(matchId, "")` to
   activate. `desktop_control.focus_window()` now tries this first on any
   Wayland session before falling through to the existing wlrctl/swaymsg
   paths (which remain for non-KWin wlroots compositors). Confirmed live:
   activated a specific Brave window out of ~20 other open windows by
   title, verified via screenshot; re-ran the same tab-reading test
   afterward and it correctly read real tab content (a Wikipedia article)
   instead of terminal scrollback. `read_all_browser_tabs`'s tool schema
   now takes an optional `browser` hint and calls `focus_window()` itself
   before cycling, rather than trusting the model to have called a
   separate focus step first (see issue 11 -- that trust was misplaced in
   the same live test, see issue 19). Still not implemented for GNOME
   Wayland or non-KWin wlroots compositors without wlrctl/swaymsg -- only
   fixed for this project's actual reference platform (KDE Plasma 6).
19. ~~`router.classify()`'s ambiguous-case prompt let a multi-step
   tool-use request get routed to the large brain~~ — found in the same
   live browser-tab-reading test: a request to open three sites in a
   browser and read all the tabs got classified "large" by the small
   model's own judgment (not the keyword list, not the old length
   heuristic -- confirmed via log, the message was 54 words and matched no
   keyword). This is a real failure mode, not just a bad guess: the
   large-brain branch (`agent_loop.py`) is a single raw text completion
   with **zero tool access**, so anything routed there that needs to
   actually touch the browser/filesystem/screen is guaranteed to fail
   outright -- confirmed live, it tried to fulfill the request via AirLLM,
   which can't open a browser at all (and separately hit the pre-existing
   gated-model auth failure from issue 12, compounding the problem).
   Rewrote the classification prompt to explicitly state that 'large' is
   only for self-contained generation work needing no tools, and that
   'small' must be used for anything requiring live system interaction
   regardless of how substantial/multi-step it reads -- also removed the
   old `> 60 words` length shortcut entirely, since length was never a
   reliable proxy for "needs tools" (it was actually irrelevant to this
   specific failure, but is exactly the kind of signal that would make the
   same mistake on a different long-but-ordinary request). Confirmed fixed
   by re-running the identical request after the prompt change: routed to
   'small', tools were actually called.
21. ~~`click_at` was not just imprecise, it was fundamentally
   unreliable~~ — `ydotool mousemove --absolute -x -y` does *not* map to
   real screen pixels: inspected the `ydotoold` virtual device directly
   via `python-evdev` (`dev.capabilities()`), and it only advertises
   `EV_KEY` and `EV_REL` — no `EV_ABS` axis exists on the device at all,
   so "absolute" positioning was really ydotool internally tracking its
   own assumed cursor position with no way to ever resync against where
   the real compositor cursor actually is. Confirmed live multiple times:
   fed it real on-screen coordinates and the click landed somewhere else
   entirely. Fixed on KDE/KWin with a genuinely different approach:
   `_kwin_cursor_pos()` reads the *real* cursor position straight from the
   compositor via a throwaway KWin script (`org.kde.kwin.Scripting` --
   runs inside KWin's own process, so it's authoritative and, unlike
   `ScreenShot2`, isn't gated behind an unprivileged-client authorization
   wall). `workspace.cursorPos` turned out to be read-only in this KWin
   scripting API (confirmed live: writing to it raises "Cannot assign to
   read-only property"), so it can't warp the cursor directly -- instead
   `click_at()` now runs a closed-loop homing routine: read the real
   position, send a *relative* `ydotool mousemove` (`EV_REL` is genuinely
   supported) for a damped, capped fraction of the remaining distance, and
   repeat until within a few pixels or an iteration cap. The damping
   turned out to matter a lot, not just be defensive: ydotool's relative-
   move "gain" (actual pixels moved per requested pixel) is inconsistent
   at different magnitudes -- confirmed live moving the same nominal
   distance landed anywhere from roughly 1:1 to almost 2:1 -- so a naive
   proportional correction overshoots the target and *oscillates
   indefinitely* (reproduced live: bounced between opposite sides of a
   target for 8 straight iterations without narrowing). Capping each step
   to at most 30% of the remaining distance (250px max) keeps it
   convergent regardless. Also had to actually learn this machine's real
   monitor geometry via KWin scripting (`workspace.screens`) after an
   early test looked "stuck" -- turned out the test target simply wasn't a
   valid point on any monitor (three displays here, at different x/y
   offsets and sizes, not one simple rectangle: DP-3 0,0 1080x1920;
   HDMI-A-1 1080,896 1280x1024; DVI-D-1 2360,484 1920x1080) -- the loop
   was correctly clamping at the real screen boundary, not malfunctioning.
   Confirmed fixed against a genuinely valid on-screen target: converged
   to within 3px in 17 iterations. Falls back to the old best-effort
   absolute move only if KWin's readback is entirely unavailable (non-KDE
   Wayland compositor, or `busctl`/`journalctl` missing) -- on GNOME or
   non-KWin wlroots compositors this bug is therefore still present, same
   caveat as issue 18's window-focus fix.
22. ~~Responses were taking ~24s+ even for trivial questions~~ — found
   while making the mobile app feel responsive (user: "might need to use
   a slightly smaller model... or somehow have it load and respond
   faster"). Three separate, stacking causes, not one:
   1. `small_brain.py` never set Ollama's `keep_alive`, so it fell back
      to Ollama's own 5-minute default and unloaded between sporadic
      messages — confirmed live via `ollama ps`: ~6.2s cold reload vs
      ~0.36s warm. Fixed with `keep_alive=-1`.
   2. Far bigger: `agent_loop.py` interpolated per-request "Relevant
      memories" text directly into the system message, which by
      definition differs on every request (different memories recalled
      for different questions). That content came *before* the
      ~1900-token `TOOL_SCHEMAS` payload in the serialized prompt, so any
      difference there invalidated Ollama's prompt-prefix cache for
      everything after it — confirmed live: every single request was
      paying the full ~17-18s cold-prefill cost for tool schemas, not
      just the first one ever. Moved the memories text into the user
      message instead (after everything that needs to stay cacheable);
      confirmed fixed: repeated calls with a stable system+tools prefix
      dropped to ~1.2s each.
   3. `on_text`'s `respond()` called `safe_speak()` *synchronously*
      before returning, so the socket connection — and by extension
      `remote_bridge.py`'s relay to the mobile app — didn't close until
      local TTS audio finished playing out loud, even though a phone
      user never hears that audio at all. Now speaks in a background
      thread for text-originated requests specifically; `on_wake`
      (voice) is untouched on purpose, since blocking through TTS there
      actually is correct (a wake word shouldn't talk over the current
      answer while the user's standing right there). Trade-off accepted:
      the activation lock now releases before speech finishes on the
      text path, so a near-simultaneous wake-word activation could in
      principle start while a text reply is still being spoken aloud —
      narrow edge case, judged acceptable given how much this mattered
      for remote/mobile responsiveness.
   Confirmed end-to-end via the real `zelia.sock` protocol: three
   consecutive requests measured 3.66s/3.36s/3.33s after all three
   fixes, down from a cold ~24s before any of them. Worth remembering if
   response speed regresses again later: check `ollama ps`'s UNTIL
   column first (should say "Forever"), then whether anything
   reintroduced per-request-varying content into the system message
   ahead of the tool schemas.
23. ~~Asked to read/summarize a long Gemini/Claude.ai chat, ZELIA could
   only Ctrl+Tab between tabs, not scroll within one to see past the
   first screenful~~ — user-reported bug. `page_reader.py`'s
   `read_full_page` fixes this: scrolls down (Page Down) through the
   current tab, reading each screen via `read_screen_text`, until
   scrolling stops changing what's visible (same cycle-detection
   principle as `browser_tabs.py`'s tab-cycling, applied within a page
   instead of between tabs). Confirmed live against a real, genuinely
   long Gemini response (30 requested haikus): read 4 screens before
   correctly detecting the end. Found and fixed a real performance issue
   along the way: `take_screenshot()` was capturing the *entire* virtual
   desktop (all 3 monitors, 4280x1920) via spectacle's `-f` flag for
   every read, even though `read_screen_text`/`describe_screen`/
   `click_at` only ever care about the focused window — switched to `-a`
   (active window only), confirmed ~2.5x faster (~4.7s → ~1.9s per call).
24. **Started a major architecture shift: ZELIA's mouse/keyboard input
   is being moved off the user's real devices entirely.** Explicit user
   requirement, arrived at through a long back-and-forth (see the
   session transcript around this if the summary below isn't enough
   context): she needs to be able to do things — click, type, scroll —
   while the user is actively gaming/using Blender/etc. with their own
   physical mouse and keyboard, without any interference. `ydotool`
   (the entire input story until this point) fundamentally can't do
   this: its virtual device shares the ONE system cursor and keyboard
   focus with the user's real hardware (confirmed via `python-evdev`:
   only `EV_KEY`/`EV_REL` capabilities, and more fundamentally, Wayland's
   `wl_seat` model is single-pointer/single-keyboard-focus at the
   protocol level — there is no compositor-agnostic way around that).
   - **Researched and ruled out:** writing a custom Wayland
     compositor/KWin extension for a second visible cursor. Not a scoped
     side-project — Wayland's core protocol model only allows one
     pointer/keyboard focus per seat, so this would mean rearchitecting
     the compositor's input-redirection pipeline at a fundamental level
     (and even then, most apps' own UI code assumes single-pointer
     semantics). No production Wayland compositor supports this today;
     it's a genuine, industry-wide platform gap, not a KWin-specific one.
   - **Found the real answer:** `ext-transient-seat-v1`, a Wayland
     protocol specifically for this (remote-desktop tools needing an
     isolated seat instead of merging with the local one). Per
     wayland.app's compositor compatibility matrix, this is implemented
     server-side by KWin (6.6+, so this project's actual KWin 6.7.3
     already has it), Mutter (49.2+), wlroots (0.18+, e.g. Sway,
     Hyprland), and others — genuinely not KDE-specific. It's gated to
     "privileged clients" though (confirmed: a plain `wayland-info`
     client sees no `ext_transient_seat_manager_v1` global at all), and
     the actual, standard way to become one is
     `org.freedesktop.portal.RemoteDesktop` — a normal XDG Desktop
     Portal D-Bus interface (`CreateSession`/`SelectDevices`/`Start`,
     then `NotifyKeyboardKeysym`/`NotifyPointerMotion`/etc.), which is
     almost certainly what's been showing that "Remote Control ... is
     asking for special privileges: Control input devices" consent
     dialog every time `xdotool`/`ydotool` ran this whole session.
   - **Architecture:** `input_backend_ydotool.py` (the old approach, now
     archived, opt-in via `config.yaml`'s new `desktop.input_backend:
     "ydotool"`) and `input_backend_portal.py` (new default, `"portal"`)
     sit behind a dispatcher in `desktop_control.py` — every other
     module's call site (`desktop_control.press_key()` etc.) is
     unchanged. Deliberately **not KDE-specific** in the new backend:
     only standard portal D-Bus calls, nothing KWin-scripting-based, so
     whichever compositor + portal backend combination implements the
     protocol is what actually provides the isolation.
   - **Confirmed live:** typed text into a focused window (Kate) via
     `NotifyKeyboardKeysym` through an isolated portal session, while
     the real keyboard was left alone — and it correctly followed the
     compositor's existing focus state (`focus_window()` didn't need any
     changes). One-time consent dialog per process lifetime, same
     principle as every other permission prompt in this project — not
     something to script around; explicitly declined to auto-click it
     even after confirming it was technically possible to try, same
     reasoning as the earlier declined screen-unlock-bypass request (the
     system requesting a privilege escalation shouldn't be the one
     approving its own request).
   - **Found and fixed a real xdg-desktop-portal bug** while
     prototyping: `RemoteDesktop.CreateSession` crashes the entire
     portal daemon outright (an assertion failure/core dump, not a
     graceful D-Bus error) if the options dict is missing
     `session_handle_token` — required, but not obviously implied by the
     method's D-Bus introspection signature alone (`a{sv}` gives no hint
     which keys are mandatory). `systemctl --user reset-failed
     xdg-desktop-portal` + it being D-Bus-activatable meant it recovered
     on the next call, but this is worth remembering: a broken portal
     call can take down the shared daemon for *every* app using portals,
     not just ZELIA's own request.
   - **Not yet done:** `click_at`'s positioning accuracy through this
     backend is unverified (no cursor-position feedback exists for the
     isolated seat — `input_backend_ydotool.py`'s KWin-scripting readback
     was seat-specific to the *default* seat, doesn't apply here; the
     implemented anchor-then-offset strategy is a reasonable guess, not
     confirmed), and `scroll()` is likewise untested. Live config still
     pins `input_backend` to `"ydotool"` until these are verified. User
     explicitly deferred non-browser apps (games, Blender, Steam's UI) to
     later — this work only needs to cover what's reachable through the
     portal/CDP paths for now.
25. ~~Reading a Brave tab required screenshots+OCR+synthetic Page-Down,
   which (a) is the exact kind of input this project is moving away from
   (issue 24) and (b) is lossy/slow compared to just asking the browser
   for its actual content~~ — `cdp_reader.py` talks to Brave's own
   Chrome DevTools Protocol (remote-debugging) directly instead: `GET
   /json/list` to find a tab by title/URL hint, then a
   `Runtime.evaluate` call over the tab's CDP WebSocket for
   `document.body.innerText` — the exact real page text, zero synthetic
   input, and not limited to what's currently scrolled into view (reads
   the whole DOM regardless of scroll position). `browser_control.py`'s
   `open_browser` now launches Brave via `flatpak run` (not
   `gtk-launch`, which can't pass extra flags through a `.desktop`
   file's fixed Exec line) with `--remote-debugging-port=9222` --
   confirmed this only takes effect on a genuinely fresh launch (Brave
   is single-instance; a later launch while it's already running just
   opens a tab in the existing, non-debug-enabled session — `cdp_reader`
   surfaces a clear error telling the caller to ask for a full restart
   rather than silently doing something else). Also needed
   `--remote-allow-origins=http://127.0.0.1:9222` -- Chromium rejects
   CDP WebSocket connections by Origin header by default (an
   anti-DNS-rebinding protection), confirmed live via a 403 on the
   handshake even with the port correctly open and reachable over plain
   HTTP. Confirmed end-to-end through the real agent pipeline (not just
   the module standalone): asked "what tabs do I have open in Brave"
   over the text socket, got an accurate answer matching the actual
   two-tab state. Note for [[zelia_custom_distro_vision]] (memory) --
   user explicitly does not want these Brave launch flags carried
   forward into a future custom-distro version of this project without
   review; they're a deliberate, acknowledged security/functionality
   tradeoff for now, not a settled good practice.

Still open:

20. **`game_guard`'s `\.exe$` pattern has a confirmed false-positive mode
   on this dev machine, and the GPU-hog detector had a related latent gap**
   (defended against, but not actually what's firing here -- see below).
   Originally diagnosed as "Ollama's own GPU usage misdetected as gaming"
   after seeing `journalctl` show "Gaming state changed -> GAMING" right
   after an ordinary Ollama tool-calling turn. Added
   `OWN_BACKEND_PROCESS_NAMES = {"ollama", "ollama_llama_server"}` and an
   `_is_foreign_gpu_pid()` check (looks up the reported PID's process name
   via `psutil` before counting it as "foreign" GPU usage) -- this is
   correct defensive code and worth keeping (would matter the moment
   `rocm-smi` gets installed, or if STT/TTS ever gain GPU acceleration,
   currently CPU-only per `config.yaml`), **but turned out not to be the
   actual cause here**: `rocm-smi` isn't even installed on this machine
   (confirmed: `which rocm-smi` → not found), so `_gpu_hog_match()` always
   silently returns `False` regardless of this fix -- the GPU path was
   never what fired. Re-investigated after the same "GAMING" flip recurred
   post-fix: the real match was `_process_match()`, specifically the broad
   `\.exe$` pattern (meant to catch Wine/Proton games, which commonly run
   as some-game.exe) matching a process literally named `claude.exe` --
   confirmed via a direct psutil scan against `DEFAULT_PATTERNS`. This
   machine runs several *other*, unrelated Claude Code agent sessions
   concurrently (visible throughout this session's screenshots -- separate
   projects, separate terminals) and one of them apparently has a
   `claude.exe` process running for its own unrelated reason; `game_guard`
   has no way to distinguish "a real Wine/Proton game process" from
   "literally any other process that happens to end in .exe" by name
   alone, so it isn't actually wrong given its design, just imprecise in a
   way this specific multi-agent dev machine exposes more than a typical
   single-user desktop would. Not fixed -- hardcoding an exclusion for
   "claude.exe" specifically would be overfit to this one machine's
   coincidental process name, not a real solution.

5. `press_key`'s Wayland path (`desktop_control.py`, `YDOTOOL_KEYS`) only
   has a handful of key combos mapped — extend as needed.
5b. Window focus still has no implementation on GNOME Wayland or non-KWin
   wlroots compositors without wlrctl/swaymsg (see resolved issue 18 for
   the KDE/KWin fix, which is this project's actual reference platform).
14. **`start_priority_manager` (`main.py`) can't always restore normal
   priority after gaming ends** — logs a recurring `Could not renice`
   warning every poll cycle once this happens. Standard Linux permission
   rule: an unprivileged process can raise its own nice value (lower
   priority) freely, but can't lower it back down again (raise priority)
   without `CAP_SYS_NICE` — so once game_guard reniced ZELIA to
   `gaming_nice` (10) during a detected game, she's stuck there even
   after gaming ends, unable to `nice(0)` herself back down. Doesn't
   crash anything (caught and logged, not fatal), just means she stays
   slightly deprioritized after a gaming session ends until the process
   restarts. Not fixed -- `zelia.service` is a systemd `--user` unit, and
   granting `AmbientCapabilities=CAP_SYS_NICE` generally doesn't work for
   user units the way it does for system units (the user's own
   `systemd --user` instance isn't privileged enough to grant it), so
   this needs more investigation before attempting a fix, not a
   copy-paste systemd directive.
11. **Small-brain tool-calling reliability is inconsistent** — found during
   live testing of "run this script in my workspace" style requests
   (`qwen2.5:7b-instruct-q4_K_M`, via `ollama`'s tool-calling). Two distinct
   symptoms observed, both several times:
   - It sometimes guesses an absolute path like `~/workspace/hello.py`
     instead of using the relative path a command already running from
     the workspace directory needs (`agent_loop.py`'s system prompt now
     explicitly says commands start in the workspace and to use relative
     paths — this reduced but did not eliminate the behavior).
   - It sometimes emits what looks like a tool call (e.g.
     `run_shell_quiet {"command": "..."}`) as plain text *content*
     instead of a proper structured `tool_calls` response. ~~Originally
     `agent_loop.py`'s `if not tool_calls:` branch just treated this as
     the final answer and spoke/sent the raw pseudo-call syntax
     verbatim~~ -- **fixed** after a third occurrence actually broke a
     real task (a web-research-then-code test: the model correctly wrote
     working password-generator code in its reply text, described
     writing it to `password_gen.py` and running it, but the described
     action was never really executed -- the file on disk still had
     garbage from an earlier failed attempt). `_find_leaked_tool_calls()`
     (regex over the known `TOOL_SCHEMAS` names) now detects this shape
     in the "no tool_calls" branch and actually dispatches the parsed
     call for real instead of just displaying it, reporting a clean
     plain-language outcome ("done" / "failed (...)")  instead of the
     confusing raw syntax. Verified live: re-ran the exact failing
     scenario, confirmed the file now has correct content and the script
     actually runs. Doing this surfaced a second, related bug in the same
     test: the model had been using `echo`-into-a-file shell tricks for
     multi-line content, which silently produces literal backslash-n
     characters instead of real newlines (`echo` without `-e` doesn't
     interpret `\n`) -- added an explicit system-prompt rule to always
     use `write_file` for creating/writing file content instead, also
     confirmed fixed on retest (a real `word_count.py`, correctly
     formatted, that actually ran).
   This is model/prompt-following unreliability, not a code defect in the
   dispatch path — the tool-crash-safety fix (see resolved issue notes
   above) and cwd-scoping fix are both confirmed working correctly when
   the model calls tools properly. Matters a lot for the user's stated
   priority of fully unsupervised overnight coding runs, where nobody's
   there to notice a wrong guess or a leaked tool-call string.

   **Model comparison done, inconclusive in the model's favor**: pulled
   `llama3-groq-tool-use:8b` (a Llama-3-8B fine-tuned specifically for
   reliable tool-calling) and ran the identical prompt/tool-schema/system-
   prompt against both models directly via `ollama.chat()` (bypassing the
   rest of the pipeline), 5 trials each. Both got it right 5/5 — correct
   relative paths, correct tool schema, no leaked pseudo-calls. This
   means the earlier live failures probably weren't really about model
   choice; more likely candidates are (a) the system prompt not yet
   having the workspace-relative-path instruction at the time, since
   fixed, and/or (b) `second_brain`'s "Relevant memories" recall
   surfacing *ZELIA's own earlier confused turns* (e.g. "the file doesn't
   exist" from a prior mistake) as context, potentially reinforcing the
   same mistake on a later similar request. (b) is worth real
   investigation — right now `remember()` stores every turn indiscriminately,
   including the assistant's own errors/confusion, with no quality
   filtering before it comes back as "relevant" context later. Did not
   switch the configured small-brain model based on this evidence; no
   clear win over the current one, and swapping without a clear reason
   would just be churn.

   **Concrete, worse case found later — multi-step GUI tasks, and (b)
   above confirmed as a real, live-observed problem, not just a
   hypothesis:** asked ZELIA (via `zelia-say`) to "open steam, go to my
   library, and tell me what games I have installed." She called
   `show_me` (correctly focused the Steam window), then answered "you
   don't have any games installed" **without ever calling
   `read_screen_text`/`describe_screen`/`click_at`** to actually look —
   pure fabrication, confirmed by the tool-call log (only `show_me`
   fired). Added an explicit system-prompt rule ("never answer a
   question about specific on-screen content without actually looking
   first... opening/focusing an app is not the same as having seen
   what's inside it") and retried: it got *worse* — the retry called
   *zero* tools and repeated the same wrong "no games installed" claim,
   and `second_brain.recall()` confirms why: the first fabricated
   answer was already stored and came back as "relevant context" for
   the retry, reinforcing itself. This is (b) from the model-comparison
   note above, now demonstrated rather than theorized. A third, simpler
   probe ("look at the screen right now and describe what's open") also
   fired zero tools, but its answer ("no application open, screen is
   blank") turned out to be accidentally true — the KDE session had
   locked itself (`loginctl ... LockedHint=yes`) during the extended
   automated test run, since socket-driven requests don't count as user
   input to the OS's own idle timer. Confirmed the underlying mechanism
   itself works correctly when actually invoked (a plain "read the text
   on my screen" request earlier in the same session did call
   `read_screen_text` and returned real, correct OCR content) — the gap
   is specifically the small model's inconsistent willingness to chain
   multiple tool calls for a compound request rather than pattern-
   matching straight to a plausible-sounding answer. Not resolved; the
   `remember()`-stores-everything-indiscriminately design (see "Second
   brain" above) makes this actively worse over time, not just
   occasionally wrong, since a wrong answer becomes "precedent" for
   later similar questions once it's in the memory store. Whoever picks
   this up should treat the memory-quality-filtering idea in the Second
   Brain section above as directly connected to this, not a separate
   nice-to-have.

   Also worth knowing for planning autonomous/overnight work
   specifically: if the session locks (KDE's normal idle behavior, and
   ZELIA's own automated activity doesn't prevent it), screen-reading
   and GUI-interaction tools will all fail against a locked screen —
   this doesn't block terminal/AirLLM-based coding work (no screen
   visibility needed for that), but would block anything requiring
   actually seeing/clicking a GUI app while the user is away/asleep. No
   attempt made to address this (e.g. inhibiting the idle lock, or
   detecting-and-reporting a locked session distinctly from "nothing on
   screen") — flagging as something to decide on purpose, not backing
   into a workaround unprompted.

   **Verified working, separately from all of the above:** ZELIA's
   `run_in_terminal` tool successfully launched a real, live Claude Code
   CLI session (`claude --dangerously-skip-permissions`) in a visible
   terminal on request — confirmed via the process list, not just her
   claiming so. Deliberately did not feed that session any task/prompt
   once it was up, since a fully permission-bypassed Claude Code
   instance taking unsupervised action was not what was being tested
   (just whether ZELIA could launch one at all) — it was left sitting
   at its own interactive prompt.

## Pending features (not yet built)

Roughly in the order the user raised them. (Text-chat input mode, formerly
listed here, is done — see "Second brain" above for `text_input.py`/
`text_repl.py`.)

1. **GUI automation is still generic/best-effort for precise interaction**,
   not app-specific, even though `read_screen_text` now prefers AT-SPI for
   *reading* (see "Screen reading" above). `find_text_on_screen` + `click_at`
   are still screenshot-and-guess for *clicking* things -- AT-SPI can also
   drive actions (`Atspi.Action`/`Atspi.Component` interfaces support
   invoking buttons and getting exact on-screen coordinates directly,
   which would be far more precise than OCR-and-click for apps it works
   with), but that wasn't built this pass, only reading was. Worth doing
   for apps like Godot's editor where precise interaction matters more
   than just reading text.
2. **Self-diagnostics** — the user wants ZELIA to be able to check her own
   health/logs and self-correct when told to. Currently only possible
   ad-hoc (she *can* run `journalctl --user -u zelia` etc. via her shell
   tools if she reasons to, but there's no dedicated tool or system-prompt
   nudge making this a reliable, proactive behavior).
3. **Claude.ai browser integration** was discussed (open Floorp, drive it
   to claude.ai, type/read via the desktop-control + OCR primitives) but
   not built — no dedicated tool exists for this specific workflow yet,
   would be composed from existing `open_browser` + `type_text`/`press_key`
   + `find_text_on_screen`/`describe_screen` primitives.
4. A genuine Claude API tier (as a third brain option) was discussed and
   explicitly **not** added — the user was told this is the one piece that
   wouldn't be free, and no decision was made either way.

## Config reference

`config/config.yaml.template` → filled in by `install.sh` (from-source) or
`zelia-setup` (packaged install) → `config/config.yaml`. Key sections:
`assistant` (wake word engine/model), `hotkey`, `stt`, `tts`, `brains`
(small/large), `gpu`, `gaming`, `screen`, `desktop` (default_browser),
`agent` (workspace_dir, confirm_before_destructive), `memory`, `logging`.
Read the template's inline comments — they carry a lot of the "why," not
just the "what."

## Notes for how to work on this codebase

- Small, focused modules under `src/agent/tools/` — one concern each. Add
  new capabilities as new tool modules + entries in `TOOL_SCHEMAS` /
  `_dispatch_tool` in `agent_loop.py`, following the existing pattern
  (return `{"ok": bool, ...}` or `{"needs_confirmation": True, ...}`).
- Every external dependency (GPU vendor, session type, installed apps,
  installed terminal) gets detected at runtime somewhere in `src/`, never
  assumed. Follow that pattern for anything new.
- The user is technical, hands-on, iterates fast, and explicitly wants to
  see what's happening on their machine rather than trust a black box —
  optimize for transparency and debuggability over cleverness.
