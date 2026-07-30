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

A local, voice-controlled personal agent for Manjaro/Arch Linux. No cloud
LLM, no subscription, no MCP servers — everything runs on the user's own
machine. Originally scoped as "a free Claude Code but voice-controlled and
not just for coding" — full agent capability (files, shell, GUI apps, web),
not a chatbot.

**Current name: ZELIA** ("Zexolver's Enhanced Learning & Intelligence
Assistant," female voice). **The codebase still says "ZEUS" everywhere**
(an earlier name) — renaming ZEUS → ZELIA throughout the codebase, configs,
service names, and docs is the first thing to do. Do a careful global
rename (code, strings, filenames like `zeus.service`, default install path
`~/.zeus`, env var `ZEUS_CONFIG`, etc.) — see "Known issues" below, there's
precedent for exactly this kind of rename having been done once already
(Zeya → ZEUS) so check for stray references in comments too, not just
obvious identifiers.

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
- Screen reading: OCR via Tesseract (`screen_tool.read_screen_text`, fast,
  no GPU) and a small local vision model via Ollama (`describe_screen`,
  moondream, loaded on demand).
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

**Install** (`install.sh`): prompts for install directory (supports
pointing at a dedicated drive), auto-detects GPU vendor, installs system
packages + Ollama + Vulkan drivers (if AMD) + ydotool + a
custom-built `ydotoold` systemd `--user` service (the Arch package ships no
usable unit), pulls the small + vision models, sets up
`config/config.yaml` from the template, and creates a `zeus.service`
(pending rename) systemd `--user` unit.

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

## Known issues (fix these)

1. **Full ZEUS → ZELIA rename** needed throughout (see above).
2. **install.sh doesn't auto-start the service.** It prints the
   `systemctl --user start/enable` commands instead of running them. Should
   end with `systemctl --user enable --now <service>` so the install is
   actually done when the script finishes.
3. **install.sh's final wake-word warning is stale.** It was written when
   Porcupine was the default and still talks like there's *no* working wake
   word without Picovoice setup. Should instead say something like: "hey
   jarvis" works right now by default; "hey zelia" via Porcupine is
   optional, check console.picovoice.ai yourself given their unclear
   current free-tier terms (see wake_word.py comments).
4. `press_key`'s Wayland path (`desktop_control.py`, `YDOTOOL_KEYS`) only
   has a handful of key combos mapped — extend as needed.
5. Window focus has no implementation on GNOME/KDE Wayland (no standard API
   for it) — currently just skipped gracefully there.

## Pending features (not yet built)

Roughly in the order the user raised them:

1. **Text-chat input mode.** Type into a text box, get a response back as
   both text and TTS — same agent backend as voice, just a second input
   path, for when the user doesn't want to talk at all. No UI decision made
   yet (options: simple local web UI, a lightweight desktop text widget, a
   terminal REPL — pick something that doesn't add heavy new deps given the
   "detect, don't hardcode" and "keep it lightweight for 8GB VRAM" spirit
   of the rest of the project).
2. **GUI automation is currently generic/best-effort**, not app-specific.
   The user wants to be able to use Godot's editor (and other complex GUI
   apps) through ZELIA — current tools (`find_text_on_screen` + `click_at`
   + `type_text`/`press_key`) are screenshot-and-guess, functional for
   simple things ("click Play," "open the Script tab") but clunky for
   precise work. No accessibility-API (AT-SPI) integration exists yet if
   more precision is wanted later.
3. **Self-diagnostics** — the user wants ZELIA to be able to check her own
   health/logs and self-correct when told to. Currently only possible
   ad-hoc (she *can* run `journalctl --user -u zeus` etc. via her shell
   tools if she reasons to, but there's no dedicated tool or system-prompt
   nudge making this a reliable, proactive behavior).
4. **Claude.ai browser integration** was discussed (open Floorp, drive it
   to claude.ai, type/read via the desktop-control + OCR primitives) but
   not built — no dedicated tool exists for this specific workflow yet,
   would be composed from existing `open_browser` + `type_text`/`press_key`
   + `find_text_on_screen`/`describe_screen` primitives.
5. A genuine Claude API tier (as a third brain option) was discussed and
   explicitly **not** added — the user was told this is the one piece that
   wouldn't be free, and no decision was made either way.

## Config reference

`config/config.yaml.template` → filled in by `install.sh` → `config/config.yaml`.
Key sections: `assistant` (wake word engine/model), `hotkey`, `stt`, `tts`,
`brains` (small/large), `gpu`, `gaming`, `screen`, `desktop`
(default_browser), `agent` (workspace_dir, confirm_before_destructive),
`memory`, `logging`. Read the template's inline comments — they carry a lot
of the "why," not just the "what."

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
