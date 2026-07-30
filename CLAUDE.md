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
  (`run_in_terminal`, `open_browser`, `show_me`, `focus_window`): captures
  the previously-active window, runs the action, and — only if the user
  was active — restores focus to that previous window afterward. Xorg
  only for now (`xdotool getactivewindow`/`windowactivate`); best-effort
  on Wayland is the same situation as `focus_window` already documented
  below (no cross-compositor way to query/restore a specific window's
  focus outside wlroots-specific tools).
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

Still open:

5. `press_key`'s Wayland path (`desktop_control.py`, `YDOTOOL_KEYS`) only
   has a handful of key combos mapped — extend as needed.
5b. Window focus has no implementation on GNOME/KDE Wayland (no standard
   API for it) — currently just skipped gracefully there.
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
     instead of a proper structured `tool_calls` response, so
     `agent_loop.py`'s `if not tool_calls:` branch treats it as the final
     answer and speaks/sends the raw pseudo-call syntax verbatim.
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

1. **GUI automation is currently generic/best-effort**, not app-specific.
   The user wants to be able to use Godot's editor (and other complex GUI
   apps) through ZELIA — current tools (`find_text_on_screen` + `click_at`
   + `type_text`/`press_key`) are screenshot-and-guess, functional for
   simple things ("click Play," "open the Script tab") but clunky for
   precise work. No accessibility-API (AT-SPI) integration exists yet if
   more precision is wanted later.
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
