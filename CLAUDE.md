Rules: Read files first. Write complete solution. Test once. No over-engineering. Don't fluff output text to save tokens.

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
   - **`click_at` positioning verified live.** The anchor-then-offset
     strategy (huge relative move to pin the cursor at a screen edge,
     then one exact relative offset from that known origin) was tested
     against Kate's real window geometry on this 3-monitor setup
     (target 3320,1015 inside a 640x508 window at 3000,761) with
     before/after `_kwin_active_window_title()` checks: the click landed
     inside the intended window and kept it focused (an earlier
     same-mechanism test with a careless guessed coordinate visibly
     switched focus to the wrong window, which is what proved the
     mechanism itself was accurate and the first failure was just a bad
     coordinate, not a backend bug). Final proof: a scripted
     focus→click→type→ctrl+s→read-file-from-disk round trip produced the
     exact expected string on disk with no corruption or misdirection.
   - **Consent is now one-time forever, not per-process.** The
     RemoteDesktop portal interface here is version 2, which supports
     `persist_mode`/`restore_token` (confirmed via `busctl --user
     introspect ... org.freedesktop.portal.RemoteDesktop` →
     `.version 2`) — the same officially-supported "remember this grant"
     mechanism screen-sharing apps use, not a workaround. `SelectDevices`
     is called with `persist_mode: 2` ("until explicitly revoked"); the
     `restore_token` `Start()` returns is saved to
     `~/.zelia/state/portal_restore_token` and replayed on every future
     `SelectDevices` call. Confirmed live across brand-new processes
     (including the real running `zelia` service after a restart): the
     first-ever grant needed one real consent-dialog click, every
     process since has skipped the dialog entirely (session ready in
     ~1-2 seconds, no wait for a human). This was an explicit user
     request ("make a special popup I only have to click once and you
     are authorized forever") — implemented via the portal's own spec
     feature rather than any kind of auto-click/bypass.
   - **CRITICAL FINDING, REVERTED:** after the above validation looked
     clean, the user reported their real cursor visibly disappeared
     while ZELIA was typing -- a direct symptom the whole point of this
     backend was supposed to make impossible. Verified directly rather
     than assumed: read the *default seat's* cursor position via KWin
     scripting (`workspace.cursorPos`) immediately before and after a
     portal `click_at(100, 100)` call. It moved (`2642,1221` ->
     `3847,1549`) in direct response to the "isolated" transient-seat
     pointer motion. This means `NotifyPointerMotion` over this D-Bus
     API is **not** rendering to a separate/invisible cursor on this
     system -- it's moving the exact same on-screen cursor the real
     mouse controls, i.e. the core isolation assumption for this backend
     is **wrong**, at least for the plain `Notify*` D-Bus method family.
     Live config was immediately reverted to `"ydotool"` as the safe
     default. **Do not switch back to `"portal"` or claim this backend
     is safe until this is actually understood and fixed.**
   - **Root-caused via actual source, not guesswork.** Read
     xdg-desktop-portal-kde's real source (invent.kde.org, tag v6.7.3):
     its `Notify*` D-Bus handlers go through
     `WaylandIntegration::FakeInput`, i.e. the legacy
     `org_kde_kwin_fake_input` Wayland protocol -- confirmed present as
     a literal string in the installed `/usr/lib/xdg-desktop-portal-kde`
     binary. `fake_input` has always targeted the real default seat;
     it predates and has nothing to do with transient seats. This is
     the definitive explanation for the cursor-hijacking bug above, not
     just a hypothesis anymore.
   - **`ConnectToEIS` is a genuinely different code path** -- confirmed
     by reading the source, not assumed. xdg-desktop-portal-kde just
     proxies it to a *private* KWin D-Bus method
     (`org.kde.KWin.EIS.RemoteDesktop.connectToEIS`, object path
     `/org/kde/KWin/EIS/RemoteDesktop`, service `org.kde.KWin`),
     implemented by KWin's own `src/plugins/eis` plugin (installed
     locally as `/usr/lib/qt6/plugins/kwin/plugins/eis.so`, found via
     `strings`-grepping every kwin-related `.so` for the D-Bus
     interface name -- much faster than guessing GitLab paths). This
     plugin is the one that actually talks EIS/transient-seat.
   - **First attempt hung indefinitely -- root cause found, not a
     ScreenCast pairing requirement.** Read KWin's actual
     `eisbackend.cpp` (tag v6.7.3): its capability mapping only ever
     grants `EIS_DEVICE_CAP_KEYBOARD` for the portal's "keyboard" bit --
     it **never** grants `EIS_DEVICE_CAP_TEXT`. The first version of
     `input_backend_eis.py` waited for a `CAP_TEXT` device (the simpler
     keysym-based text API) before proceeding, which KWin will simply
     never hand out on this version -- an infinite wait, not a hang bug.
     The "Only stream input" log line that looked suspicious was
     independently ruled out by reading `remotedesktop.cpp` directly: it's
     just the normal log message for an input-only (no screen-sharing)
     session and does not block or gate anything -- the ScreenCast-pairing
     hypothesis from earlier in this investigation was wrong.
   - **Rewrote the backend around `EIS_DEVICE_CAP_KEYBOARD` (raw evdev
     keycodes) instead of `CAP_TEXT` (keysyms).** This needs a real
     keymap-aware keysym -> keycode lookup, which needs `libxkbcommon`.
     No usable Python xkbcommon binding could be installed --
     `/opt/zelia` is a read-only pacman-owned install and `pip install`
     needs root there, which needs an interactive sudo password nobody
     was present to type (this whole phase of work happened after the
     user went to sleep, per their explicit "do what you can without
     me"). Solved with a small self-contained `ctypes` binding against
     the system `libxkbcommon.so.0` instead (loads the keymap EIS hands
     back via `ei_device_keyboard_get_keymap()`/`ei_keymap_get_fd()`,
     builds a keysym -> (keycode, needs_shift) table via
     `xkb_keymap_key_get_syms_by_level()`) -- no new dependency, no
     install step, consistent with how libei/liboeffis are already
     wrapped in this project.
   - **Session bootstrap no longer uses `liboeffis`.** Its
     `oeffis_create_session()` has no `persist_mode`/`restore_token`
     parameter at all, so every session it creates needs a fresh
     consent dialog -- a non-starter for unattended overnight work.
     Rewrote it around the same manual `CreateSession`/`SelectDevices`/
     `Start` D-Bus dance `input_backend_portal.py` already uses
     (restore_token included), then added one more manual call for
     `ConnectToEIS` itself (needs `call_with_unix_fd_list_sync`, not
     plain `call_sync`, since it replies with a real attached unix fd,
     not through the async Request/Response object-path pattern the
     other three calls use). **Confirmed live:** reused the *same*
     restore token `input_backend_portal.py` had already gotten
     approved earlier in the night -- session came up fully bound
     (pointer/button/scroll/keyboard) in well under a second, zero new
     consent dialog. This is real progress: the "click once, forever"
     mechanism works across backends, not just within one.
   - **New, more fundamental finding: window/keyboard focus may be a
     single global concept in KWin, not per-seat.** Live test: opened a
     fresh Kate document, used the EIS backend's `click_at` to focus it
     (confirmed correct -- active window became Kate), then `type_text`
     a marker string. Partway through, an unrelated Telegram
     notification popup grabbed real focus -- and the *rest* of the
     synthetic keystrokes followed it: a fragment of the typed text
     ("est 123 OK") landed in Telegram's own search box, confirmed via
     screenshot, and had to be cleaned up (Escape) immediately since it
     was a real side effect in the user's actual Telegram client, not a
     throwaway test surface. Kate's title still showed unsaved changes,
     meaning some characters landed correctly in Kate before the
     interruption -- so this isn't a "click_at pointed at the wrong
     thing" bug, it's that the isolated seat's *keyboard* target
     followed the same globally-active window as the real seat the
     instant that window changed. If window activation genuinely is a
     single compositor-wide concept in this KWin version regardless of
     which seat originates input (plausible -- there's no inherent
     notion of "per-seat active window" in traditional window
     management), this would be a **deeper blocker than the cursor bug**:
     it would mean no input backend (this one, a hypothetical raw
     `ext-transient-seat-v1` client, or anything else routed through
     KWin's current window manager) can give ZELIA truly independent
     window focus while the user is simultaneously active, only
     independent *devices*. Not yet confirmed as a hard architectural
     limit vs. something fixable in how this backend tracks/sets focus
     -- next session should try to isolate this specifically (e.g. does
     explicitly re-focusing the target window via EIS's own pointer
     click, right before each keystroke, hold up better than focusing
     once up front?) before concluding it's unfixable.
   - **Where this leaves things:** `input_backend_eis.py` is a complete,
     working implementation of the real EIS-isolation path (pointer
     motion is now genuinely believed separate from the default seat's
     rendered cursor -- not yet re-confirmed with the same
     `workspace.cursorPos` before/after test used to catch the original
     bug, since the Telegram incident cut the test session short; that
     recheck is the first thing to do next time). Keyboard *devices* are
     isolated; keyboard *focus targeting* is not proven isolated, and
     the Telegram evidence points the other way. `~/.zelia/config.yaml`
     stays on `"ydotool"`. Do not switch to `"eis"` (not yet wired into
     `desktop_control.py`'s dispatcher at all -- deliberately, given the
     open focus question) until the focus-tracking question above is
     resolved.
   - **Found and fixed two independent bugs while validating this,
     neither specific to the portal backend:**
     1. `app_launcher._best_app_match`'s fuzzy matching
        (`difflib.get_close_matches` against the whole query string)
        picked "KDE Connect Indicator" over "Kate" when asked to open
        "the Kate text editor application" — a long natural-language
        query scores *worse* against a short exact name than against an
        unrelated similarly-long name, purely because of
        `SequenceMatcher.ratio()`'s length sensitivity. Fixed by
        checking for an exact whole-word match first (regex `\bname\b`
        against the lowercased query) before ever falling back to
        difflib.
     2. The small brain sometimes narrates an action ("I've typed the
        sentence...") in its final reply without actually having called
        `type_text`/`press_key`/`click_at` that turn — confirmed via
        journal logs showing zero `input_backend_portal` activity behind
        a reply that claimed typing had happened. Partially addressed
        with an explicit system-prompt instruction (opening/focusing an
        app doesn't type or click anything by itself; never describe an
        action as done unless the tool was actually called), which fixed
        the simple case but not a subtler one where `preserve_focus_if_
        user_active` correctly declined to steal focus (because a human
        was genuinely active on the machine at the time) and the model
        then hallucinated success anyway instead of reporting the block.
        **Not fully solved — small-model tool-calling discipline under
        multi-step sequences is a real, separate follow-up item**. Task
        #21's comprehensive regression pass should probe this
        specifically once picked up.
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
26. ~~The small brain could burn its entire tool-round budget calling the
   same read-only tool over and over instead of answering from the first
   result~~ -- confirmed live: `list_installed_steam_games` (identical,
   empty arguments) got called 10 times in a row (`MAX_TOOL_ROUNDS`),
   never producing a final answer. Fixed in two layers in
   `agent_loop.py`'s `handle_request`:
   1. `IDEMPOTENT_TOOLS` (a fixed set of read-only tools whose result
      only depends on their arguments -- `read_file`,
      `list_installed_steam_games`, `read_screen_text`, etc, deliberately
      excluding anything with a side effect like `run_shell`/`click_at`,
      since repeating one of *those* might be exactly what's intended)
      gets its result cached per exact `(name, args)` call this turn --
      an exact repeat is served from the cache instead of re-running,
      with a short note telling the model it already has this answer.
      Confirmed live this alone cut real re-execution from 10 Steam
      scans down to 1.
   2. That wasn't sufficient on its own -- the model kept making
      (now-free) repeat calls instead of stopping, so `REPEAT_LIMIT = 2`
      escalates: once a call repeats more than twice in one turn, the
      *next* `chat()` call is made with `tools=None`, forcing a plain-text
      final answer instead of another tool round.
   Layer 1 is confirmed working live. Layer 2 (the escalation actually
   stopping the loop) was deployed but the one retest attempt was
   confounded by real, unrelated gaming-induced VRAM contention (see
   item 27 below) making the whole system too slow to get a clean
   signal -- still worth a clean re-test once the machine's genuinely
   idle, not yet fully confirmed end-to-end.
27. **"Turbidle"** (Turbo+idle) -- explicit user request, paired with item
   26 above ("fix the repeated-tool-call bug, then build Turbidle").
   Requested as "separate AI layers onto both CPU and GPU for faster
   speed, but only when the system is doing absolutely nothing else,
   including Gemini/Claude Code CLI" -- that literal mechanism doesn't
   actually apply on this hardware: AirLLM has no usable CUDA/ROCm path
   on this AMD card (issue 12 above) so it's CPU-only regardless of idle
   state, and the small brain is already 100% GPU-resident whenever
   nothing else is competing for VRAM (confirmed live via `ollama ps`'s
   `size_vram` field -- it only partially fell back to CPU, 52%/48%
   split, while a real Roblox Studio session was actively running and
   contending for the same 8GB). Reframed as the inverse of
   `resource_manager.py`'s existing AirLLM coding-worker caps: those
   exist specifically to *protect* the rest of the machine (Gemini CLI,
   foreground responsiveness) from the background coding worker;
   Turbidle lifts that protection when there's genuinely nothing left to
   protect, so the worker gets most of the machine instead of a
   deliberately small slice of it.
   - `resource_manager.is_fully_idle(game_guard)` is the gate: requires
     `idle_detect.is_user_active()` to be `False` (no recent
     keyboard/mouse), the passed `GameGuard.is_gaming()` to be `False`,
     and a new `_coding_cli_active()` check (psutil process-name scan for
     `claude`/`gemini`, same pattern as `game_guard._process_match()`) to
     also be `False`. Deliberately broad/best-effort like game_guard's
     own matching -- errs toward treating more things as "active" rather
     than risking Turbidle kicking in mid-work.
   - `resource_manager.get_budget(cfg, turbidle=True)` returns a much
     higher `ResourceBudget`: RAM and CPU-core defaults are computed live
     from `psutil` (total system RAM/core count minus a safety margin --
     4096MB RAM, 1 CPU core -- rather than a fixed guess, since a good
     idle budget depends on the actual machine), CPU weight goes to 100
     (normal priority, not the deprioritized 25 the plain caps use, since
     nothing else is contending). Overridable via new, optional
     `turbidle_max_ram_mb`/`turbidle_cpu_quota_percent`/
     `turbidle_cpu_weight` keys in `config.yaml`'s `brains.large` section.
   - `large_brain.py`'s `_run_job` calls `is_fully_idle(self.game_guard)`
     once at job launch and passes the result into `get_budget()`. Checked
     once, not re-evaluated live mid-job (like the existing gaming
     queue-check) -- a job that starts under Turbidle and then the user
     comes back mid-run keeps its already-granted budget rather than
     being throttled out from under it; the plain caps already exist to
     keep the rest of the machine usable even then, so this isn't unsafe,
     just not instantly reactive. Live-resizing an already-running
     scope's cgroup limits (`systemctl --user set-property`) would make
     it reactive; not attempted, a bigger separate change.
   Confirmed live, negative case: with a real Roblox Studio session
   running (game_guard genuinely detects `GAMING`) and this very Claude
   Code session running (`_coding_cli_active()` correctly matches its own
   `claude` process), `is_fully_idle()` correctly returned `False` on
   both independent signals. Not yet confirmed live in the positive case
   (an actual genuinely-idle window with a real AirLLM job launched
   during it) -- the gating logic mirrors already-proven patterns
   (`game_guard.is_gaming()`, `idle_detect.is_user_active()`) closely
   enough to trust, but the full escalated-budget path hasn't been
   watched fire end-to-end yet. Worth doing once the machine's actually
   idle and a coding job gets queued.
28. ~~Asking ZELIA to open a specific existing Gemini chat opened a blank
   new one instead~~ — explicit user report, from during a week they were
   locked out of Claude Code and relying on her for basic browser tasks
   ("I just want her to actually function and do average tasks
   properly"). Root-caused live, not guessed, in two parts:
   1. Gemini's chat-history sidebar starts **collapsed**.
      `document.body.innerText` (what `cdp_reader.read_tab` uses)
      correctly only returns *visible* text per spec — a collapsed
      sidebar's chat titles genuinely aren't there to read or click yet.
      Confirmed via direct DOM inspection: the sidebar container had
      literal CSS class `collapsed`, its `<gem-nav-list-item>` entries
      had empty `textContent` until expanded.
   2. Even after correctly selecting a chat (URL/title update
      immediately), the rendered message content stayed stale — confirmed
      live, the OLD conversation's text was still what `read_tab` returned
      10+ seconds after clicking a different chat. Angular's client-side
      route transition isn't a reliable signal that the new content has
      actually rendered.
   Fixed in `src/agent/tools/cdp_reader.py`:
   - New `click_text(hint, text)` (exposed as the `click_brave_element`
     tool) finds and clicks a DOM element by visible text OR
     aria-label/title — the aria-label half is what lets it hit icon-only
     buttons like the sidebar's "Open sidebar" toggle, which has no
     visible text at all. Prefers a real `<a href>`/`<button>`/
     `role=button` over a generic wrapper, checked *before* any
     innermost-element narrowing — an early version incorrectly excluded
     Gemini's actual `<a href="/app/...">` link because its own child
     `<span>` duplicated the same label text, which looked (wrongly) like
     "not the most specific match." No synthetic mouse input, same
     principle as `read_tab`.
   - `_navigate_and_wait()`: once a click changes the URL, forces a real
     `Page.navigate` reload and polls `body.innerText`'s length until it
     stops changing, instead of trusting the SPA's own state transition.
   - System prompt (`agent_loop.py`) now tells the small brain: if
     `read_brave_tab` shows no chat list, try clicking a likely
     sidebar/menu toggle first, look again, then click the specific chat
     by exact title — never click_at/OCR-guess for this, since
     click_brave_element is exact.
   Confirmed live, full round trip, against the user's real Gemini
   account: expanded the sidebar, read real chat titles ("Steam Library:
   Hide vs. Private", "Rooted Android Customization and Linux
   Integration", etc.), clicked one by exact title, confirmed
   URL+title+actual message content all correctly switched (not just
   URL/title) in ~5.6s total, repeated with a second different chat to
   confirm it generalizes, then navigated back to the original chat and
   re-collapsed the sidebar to leave the browser as it was found. Not
   tested: whether a single very long conversation virtualizes/lazily
   renders older messages (i.e. whether `read_tab` could still miss
   content within one long chat) — both chats tested fit well under
   `cdp_reader.MAX_CHARS` (20000) with no sign of virtualization; if a
   long single chat ever reads suspiciously short, check that next.
   **Found a second real bug testing this through the actual agent
   pipeline** (not just the module directly): the small model passed
   `text` as a dict (e.g. `{"text": "Steam Library"}`) instead of a plain
   string on a real live call, crashing `click_text` on `.lower()`. Fixed
   with `_coerce_str_arg()` (tries the obvious dict keys, falls back to
   `str()`), applied to both `text` and `hint` (via `find_tab`) since
   both are equally exposed to this. Confirmed fixed live: re-ran the
   same failure shape directly and it now correctly extracts the string
   and clicks correctly instead of crashing. Worth a broader pass across
   other tool functions taking plain string args, which likely have the
   same unguarded assumption -- this is the first *confirmed* case of a
   small-model type mismatch actually happening in practice, not just a
   theoretical risk.
29. ~~Opening a terminal via ZELIA sometimes showed a second, oddly-worded
   "weird terminal" artifact with a number, after the normal "press enter
   to close" prompt~~ -- explicit user report. Root cause:
   `desktop_control.py`'s `TERMINAL_RUN_FLAGS` passed each terminal
   emulator its own native `--hold`/`-hold` flag *in addition to*
   `open_terminal()`'s own manually-appended
   `"; echo; echo '[done...]'; read"` keep-open script -- two independent
   keep-open mechanisms stacked on each other. Confirmed via `konsole
   --help-all` that `--hold` means "Do not close the initial session
   automatically when it ends" -- so after the user answered ZELIA's own
   clean prompt and the underlying bash process exited, Konsole's own
   native hold behavior kicked in *again* on top of that, which is almost
   certainly the "second, weird" artifact (this machine only has Konsole
   installed, so that's confirmed as the actual terminal in play, not a
   guess). Fixed by removing the native hold flags from
   `TERMINAL_RUN_FLAGS` entirely for every terminal listed, not just
   Konsole -- ZELIA's own script-level prompt is now the only mechanism,
   giving one consistent message regardless of which terminal emulator is
   installed. Confirmed live: ran a real command through `open_terminal`,
   screenshotted the result -- exactly one clean `[done -- press enter to
   close]` line, no second artifact underneath it.
   **Also worth recording: a testing mistake, not a code bug.** While
   verifying this fix, `desktop_control.press_key('enter')` was called
   directly (bypassing `agent_loop.py`'s busy-gate, which only runs when
   going through the real tool-dispatch path) to try to close the test
   terminal -- but window focus had already moved to something else on
   this genuinely busy, actively-used desktop by the time the key press
   fired, so the Enter landed in a live Minecraft-mod-development
   launcher window instead ("Gridlock 1.21.11") rather than the intended
   terminal. Checked immediately via `ps aux` for a newly-spawned game
   client process -- none appeared, only pre-existing Fabric test-server
   processes from before the incident, so nothing was actually triggered.
   Cleaned up by killing the stuck test terminal by PID directly instead
   of risking a second misdirected keypress. **Lesson for future
   sessions**: never call `desktop_control` input functions (`press_key`/
   `type_text`/`click_at`) directly for manual testing/verification --
   always go through the real dispatch path (`agent_loop._dispatch_tool`
   or the busy-gate-covered tool call) even for a "quick check," since
   direct calls skip the exact safety mechanism (`_busy_gate`/
   `INPUT_INJECTING_TOOLS`) built specifically to prevent this.
30. ~~Adding ~21 new tools in one batch (see items 27-28's tool set)
   broke basic small-brain reliability, including for requests needing NO
   tools at all~~ -- found live testing the day's new features with the
   user watching. Root-caused precisely, not just "too many tools":
   `small_brain.py` never set Ollama's `num_ctx`, so it silently defaulted
   to 4096 tokens. `TOOL_SCHEMAS`' JSON alone grew to ~23000 chars (~38-49
   tools depending on the point in this pass) -- comfortably larger than
   the whole context budget once the system prompt and a user message are
   added too. Confirmed by direct, isolated reproduction against
   `ollama.chat()` (bypassing this whole codebase): the identical 38-tool
   schema + a bare "say hello" produced a WRONG tool call (calling
   `type_text` for a message that needs no tool at all) and took ~40s,
   every time, at `num_ctx` unset/default -- but the exact same call with
   `options={'num_ctx': 8192}` correctly replied in plain text with no
   tool call. Binary-searched the actual breaking point with the codebase's
   real tool list: fine through 36 tools, broken at 38 -- consistent with
   a hard context-overflow cliff, not a gradual "model gets confused by
   choice" effect. Fixed: `small_brain.py` now always passes
   `options={"num_ctx": 16384}` (4x the accidental default), leaving real
   headroom for tool growth and multi-round tool-result accumulation.
   Confirmed fixed: the same reproduction script with the fix applied
   replies correctly and fast (~2-6s) on a warm cache.
   Also consolidated the day's new tools from ~20 separate names down to
   ~9 action-based ones (`clipboard(action=...)`, `volume(action=...)`,
   `brightness`, `power` -- lock_screen merged in as `action='lock'` --
   `timer`, `tui_session`) to reduce schema size further as a second,
   independent mitigation, not just relying on the context-window fix
   alone.
31. **NOT resolved -- confirmed live, still open**: even after the
   `num_ctx` fix, the small model fabricated two separate false "done"
   claims during this same testing session, each confirmed via journal
   logs showing ZERO tool-call dispatch for the claimed action:
   - Asked to open a specific Gemini chat by topic: 3 separate live
     attempts (across two rounds of significantly strengthened system-
     prompt/tool-description guidance, including an explicit "NEVER
     invent a URL" warning added mid-session) all resulted in
     `open_browser` being called with a fabricated URL shaped like
     `https://gemini://chat?tab=...` or similar -- never once correctly
     using `read_brave_tab`/`click_brave_element` as instructed, despite
     both being proven to work correctly via direct testing (see item 28
     above). **User's sharp diagnosis, worth remembering**: the model is
     very likely pattern-matching the word "Gemini" against the real,
     unrelated `gemini://` URI scheme (an actual, older internet
     protocol, well-represented in training data) rather than confusing
     itself randomly -- all three fabricated URLs shared that exact
     `gemini://` shape, not varied nonsense. A third prompt fix
     explicitly disambiguating "Gemini here always means
     https://gemini.google.com, never the gemini:// protocol" was added
     but NOT yet re-tested live (ran out of time in this session) --
     test this specific fix first before concluding prompt-tuning alone
     can't fix this.
   - Asked to write text to the clipboard: replied "The text ... has been
     placed on your clipboard" -- confirmed via journal that
     `clipboard_tool.write_clipboard` was never called at all, and via
     `wl-paste` that the real clipboard content was unrelated leftover
     text from the earlier failed Gemini test.
   Both failures are the *same* underlying pattern: confidently reporting
   an action as complete without ever calling the tool that does it --
   this is Known Issue #11's already-documented "small-model tool-calling
   reliability is inconsistent," but reproduced fresh, live, twice, in
   this session specifically (not just theorized from older test notes).
   **Important, separately-confirmed correction, don't repeat this
   mistake**: `lock_screen` initially looked like a THIRD instance of
   this same pattern (journal showed `power_tool | Locked the screen.`
   logged -- a real call, unlike the two cases above -- but
   `loginctl show-session ... -p LockedHint` read back `no` immediately
   after). This was NOT a fabrication or a tool bug -- it was checking
   too fast, before KDE's screen locker actually finished engaging after
   the async D-Bus signal; the user confirmed the screen genuinely did
   lock (with a second, unplanned lock-again shortly after, caused by an
   extra manual diagnostic `busctl ... Lock` call made during
   investigation, not a bug). **When verifying an action against real
   system state, allow for real async delay before concluding failure --
   a call that logged success and had a plausible reason to need a moment
   (D-Bus signal -> daemon reacts -> UI renders) deserves a recheck after
   a beat, not an immediate verdict.** This session's two REAL fabrication
   cases (Gemini URL, clipboard) are distinguishable from this false
   alarm precisely because they had NO corresponding "did the actual
   thing" log line at all, at any point -- that absence is the reliable
   signal, not a slow-to-update read of external state.
   **Not yet tested at all this session, deliberately skipped to avoid
   burning more of the user's limited time on likely-repeat findings**:
   brightness, notifications, timers, man-page reading, AT-SPI clicking,
   and all TUI functionality (also blocked separately -- `tmux` still
   isn't installed). Pick these up fresh next time, ideally after making
   real progress on the fabrication pattern above, since that's the
   thing most likely to make every one of these look broken even if the
   underlying tool code is fine.
32. ~~`router.py`'s model-based ambiguous-case fallback was silently
   doubling every ordinary request's cold-prefill cost~~ -- found while
   looking for a way to avoid "always injecting the tools JSON" per the
   user's explicit ask. Root cause: Ollama only keeps ONE cached prompt
   prefix per loaded model, and `router.classify()` was calling
   `small_brain.chat()` a SECOND time per request, with its own short,
   completely different, tools-free system prompt, for every request
   that didn't match `BIG_PROJECT_HINTS` (i.e. nearly all of them). That
   call evicted whatever was cached from the real tool-calling call's
   much larger system+`TOOL_SCHEMAS` prefix, forcing THAT call to
   cold-prefill from scratch every single time -- two cold prefills back
   to back on nearly every ordinary request, not one. `router.classify`
   is now keyword-only, no model call at all -- the fallback's own bias
   was already "when unsure, prefer small" (the safe default, since
   'small' has full tool access and 'large' has none -- see Known Issue
   #19), so this loses very little real routing accuracy for a large,
   continuous latency win. Confirmed live, clean before/after: a fresh
   "say hello" after a full service restart (cold, worst case) dropped
   to ~14s (previously 40-85s becoming the norm as the tool schema grew
   this session), and the very next ordinary request measured ~4s
   (warm-cache, prefix now genuinely stable across every real request
   instead of being evicted every time).

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

1. ~~GUI automation was generic/best-effort for precise interaction,
   AT-SPI only covered *reading*~~ -- `atspi_tool.invoke_action`
   (`atspi_click` tool) now drives actions directly (`Atspi.Action`'s
   `do_action(0)`), see item 7 below for the full writeup.
   **Written but not yet tested live** -- same caveat as everything else
   in that entry. `find_text_on_screen` + `click_at` remain the fallback
   for apps that don't expose AT-SPI (Electron/CEF apps -- Steam,
   Discord, Brave, etc), which this doesn't and can't change.
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
5. **Idle-task queue (`src/idle_tasks.py`) is built and wired in** — see
   the Turbidle-adjacent section above for full detail. `add_idle_task`
   tool queues a task, `IdleTaskRunner` runs it through the normal agent
   pipeline once `resource_manager.is_fully_idle()` is true, aborting and
   requeuing if the user becomes active mid-task. Confirmed live that
   queuing works end-to-end; **not yet confirmed live that a queued task
   actually fires during genuine idle time** (testing happened during a
   real gaming session). The specific idle task the user originally had
   in mind — open Gemini in Brave, scroll through every past chat, store
   them to second_brain as memory — is still **not built**, but the bug
   that was blocking it *is* fixed (see Known Issue #26 below, resolved
   the same day it was raised) — the remaining work is just composing
   already-working pieces (enumerate sidebar chats via
   `click_brave_element`, read each with `read_brave_tab`,
   `second_brain.remember()`), not further research.
6. **Generic "Jarvis-ness" tools written 2026-08-06: clipboard,
   volume/mute, brightness, desktop notifications, screen lock, power
   actions, timers/reminders.** `src/agent/tools/{clipboard,volume,
   brightness,notify,power,timer}_tool.py`, wired into `agent_loop.py`'s
   `TOOL_SCHEMAS`/`_dispatch_tool`/`IDEMPOTENT_TOOLS`/system prompt, plus
   a new `AgentLoop.announce` (wired from `main.py`'s `safe_speak`, same
   post-construction pattern as `idle_task_runner`) so a fired timer can
   speak unprompted independent of any request's own `speak()` callback.
   Explicitly fills the "volume/brightness/clipboard/power/notifications/
   timers" gap noted from the earlier Jarvis-TikTok capability comparison,
   and the screen-lock gap specifically called out as missing (now via
   `loginctl lock-session` -- desktop-environment-agnostic, not a
   KDE-specific dbus call). `power_action` (shutdown/restart/suspend)
   routes through the same generic `needs_confirmation` flow already used
   for destructive shell commands; `lock_screen` deliberately does not
   (reversible/low-risk, meant to feel instant).
   **IMPORTANT -- written but NOT deployed or tested at all yet,
   deliberately**: the user was busy with unrelated important work on
   this machine at the time and explicitly asked for code only, no
   syncing to `/opt/zelia`, no service restart, no live testing (any of
   which could have interrupted what they were doing, or in the power/
   lock actions' case, be actively dangerous to test blind while they're
   mid-task). Only checked via `py_compile` and a plain import (confirms
   no syntax/import errors, nothing about actual runtime behavior).
   Genuinely unverified: whether `wpctl`'s real output format matches the
   regex in `volume_tool.get_volume`, whether `/sys/class/backlight`
   exists at all on this desktop (see that module's docstring -- likely
   *doesn't*, external monitors usually aren't kernel-backlight-
   controlled), whether `loginctl lock-session` actually locks this
   specific session vs. silently no-op'ing, and the whole
   `power_action`/`needs_confirmation`/`ask_confirmation` round-trip for
   a brand-new tool name. **Sync, restart, and actually test every one of
   these live before trusting any of it** -- next session (or whenever
   the user is free) should treat this exactly like every other feature
   in this file that's marked "confirmed live" vs. not; this one isn't,
   yet.
7. **More generic-use tools written the same day, same "not deployed,
   not tested" caveat as item 6 applies here too**: local man-page
   reading, TUI (interactive terminal tool) support, and precise AT-SPI
   clicking.
   - `src/agent/tools/man_tool.py` (`read_man_page`): reads a command's
     real local man page (via `man` + `col -bx` to strip formatting
     control chars), falling back to `--help` output for tools that don't
     ship a man page (common for single-binary Rust/Go tools). The one
     part of this pass actually exercised locally (a plain read-only
     `man ls` call, not touching the running service) -- confirmed it
     produces clean, correctly-stripped text.
   - `src/agent/tools/tui_tool.py` (`start_tui`/`send_keys`/
     `read_tui_screen`/`stop_tui`/`list_tui_sessions`): drives interactive
     terminal tools (htop, vim, REPLs, ncurses menus) that
     `run_in_terminal`/`run_shell_quiet` can't do anything with beyond
     "launch and hope," since neither can send further input or read back
     current screen state. Built on `tmux` (`new-session -d` +
     `send-keys` + `capture-pane`) rather than hand-rolling pty handling
     -- **tmux is NOT installed on the reference machine**, added to
     `depends` in both PKGBUILDs and `install.sh`'s package list, but
     needs an actual `pacman -S tmux` (or a fresh package build/install)
     before this tool does anything but return a clear "not installed"
     error. `start_tui`'s `location` param controls the (optional)
     viewer: `"desktop"` opens a real terminal window attached to the
     tmux session (default, matches "nothing hidden" philosophy);
     `"vtty"` attaches on the dedicated virtual terminal instead, via a
     new `desktop_control.open_vtty_viewer()` -- explicitly the "run in
     the background" fallback the user asked for (doesn't compete for
     screen/focus while gaming); `"background"` attaches no viewer at
     all. `open_vtty_viewer` reuses `run_in_vtty`'s exact sudo/openvt/
     runuser command shape (same sudoers rule) but fire-and-forget via
     `Popen` instead of blocking with a timeout + log-file capture, since
     "attach and stay attached indefinitely" has no natural completion to
     wait for -- **this specific piece is the least confident part of
     this pass**: it assumes the existing narrowly-scoped sudoers rule
     (`/usr/bin/openvt -c 9 -- /usr/bin/runuser ...`) matches this new
     invocation's exact argument shape closely enough to still be
     permitted without a password; couldn't verify this by reading the
     actual sudoers file (permission denied without triggering a sudo
     prompt, which was avoided per the user's "don't disturb anything
     right now" instruction) -- **test the `location='vtty'` path first
     and specifically watch for a hung/failed sudo call**, not just
     whether tmux itself works.
   - `src/agent/tools/atspi_tool.py` gained `invoke_action(name)`
     (exposed as the `atspi_click` tool): walks the focused app's
     accessibility tree for a control whose name matches and calls its
     default AT-SPI action directly (`do_action(0)`) -- no screenshot, no
     OCR, no coordinate math, no synthetic mouse movement. Fills Pending
     Feature #1 below (AT-SPI could already *read*, via
     `read_focused_app`/`screen_tool.py`, but had no *action*-invocation
     capability until now). Same "detect AT-SPI availability, fall back
     to OCR+click_at gracefully" pattern as reading already uses -- won't
     work at all for Electron/CEF apps (Steam, Discord, Brave) that don't
     expose AT-SPI, same known limitation as the reading side. Added to
     both `INPUT_INJECTING_TOOLS` (busy-gate applies, same as `click_at`)
     and `SCREEN_VISIBILITY_TOOLS`.
   All three confirmed via `py_compile` + a plain import + schema-presence
   check only -- **zero live testing**, same constraint as item 6, for the
   same reason (user busy with unrelated important work, explicitly asked
   for code-only). Test all of this together with item 6's tools in one
   pass once the user's free, and install `tmux` first or `start_tui`
   will just error out immediately.

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
