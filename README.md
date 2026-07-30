# ZELIA

A local, voice-and-text-controlled personal agent for Manjaro/Arch. No
cloud, no subscription -- everything runs on your machine.

## What she does

- **Always listening** for a wake word (default "hey jarvis" -- see below),
  or press a push-to-talk hotkey instead (better for talking quietly).
  Transcribes what you say (Whisper) and talks back out loud (Piper TTS).
- **Or just type to her.** `zelia-say` (or `python -m src.text_repl` from
  source) opens a terminal chat with the exact same brains/tools/memory as
  voice -- no need to talk at all. Works from any terminal, any time, not
  tied to whichever one started the service. See "Typed input" below.
- **Two brains:**
  - A small, fast model (served by Ollama) handles normal conversation and
    quick commands instantly.
  - AirLLM loads a much larger model for big/quality-sensitive project work
    (e.g. "build a full app for X"), streaming its layers from disk since it
    won't fit in 8GB of VRAM all at once. This runs **in the background** --
    ZELIA keeps answering quick questions while a big job is running, and
    speaks up when it's done.
- **GPU auto-detected, not hardcoded:** `src/gpu_detect.py` figures out
  NVIDIA vs AMD vs nothing at startup and routes each component accordingly
  (see "GPU support" below for what that means per vendor). `config.yaml`
  reserves a chunk of VRAM for the small brain
  (`gpu.reserved_for_small_brain_mb`, default 3GB) and caps AirLLM to
  whatever's left, on hardware where AirLLM gets GPU acceleration at all.
  AirLLM runs in its own subprocess so that cap actually sticks.
- **Nothing runs hidden by default:** shell/git/build commands open a real,
  visible terminal window so you can watch them happen -- the quiet
  background path only kicks in if you explicitly ask for it. File
  read/write/delete are scoped to `agent.workspace_dir`; anything that looks
  destructive (`rm`, `git reset --hard`, overwriting an existing file, etc.)
  makes her ask out loud for a spoken "yes" first
  (`agent.confirm_before_destructive` in config, on by default).
- **Real desktop control, Xorg and Wayland both:** typing, key combos, and
  clicking (via `xdotool` on Xorg, `ydotool` on Wayland -- works regardless
  of compositor), plus OCR-based "find this text on screen and click it" for
  interacting with GUI apps like Godot or a browser. See "Desktop control"
  below.
- **Visible browser, your choice which one:** opens a real browser window
  (Floorp by default) directly to a URL rather than faking clicks into an
  address bar. Say "use X for this" or "always use X" to change it.
- **Second brain:** a local vector database (ChromaDB) that automatically
  stores and embeds every conversation turn -- no manual note-taking. Future
  requests automatically pull back relevant past context. Built for
  efficiency first: ONNX-runtime embeddings and writes happen on a
  background thread so remembering something never adds latency to your
  answer.
- **Sees your screen:** `read_screen_text` OCRs whatever's visible (fast, no
  GPU) and `describe_screen` uses a small local vision model (moondream, via
  Ollama, loaded only when asked) for questions about layout/images.
- **Launches and finds apps:** "show me X" / "open X" matches against your
  installed applications, focuses the window if it's already running, or
  launches it fresh. Falls back to opening a file/folder path, then a URL or
  web search, if X isn't an app.
- **Everything is native, not MCP:** screenshots, OCR, window focus, app
  launching, and desktop control all shell out directly to tools already on
  your machine. No MCP server required for ZELIA to interact with your
  desktop, and no cloud API calls either -- everything above runs locally.
- **Yields to games:** while a game is running (detected by process name or
  GPU usage), new AirLLM big-project jobs queue instead of starting, and
  ZELIA's own process gets reniced down so she doesn't compete for CPU. Wake
  word and quick commands keep working the whole time. See `gaming:` in
  config.yaml.

## Install

**Package (recommended):** see `packaging/README.md`. Build/install the
`zelia` (stable) or `zelia-git` (tracks `main`, for testing) pacman
package, then run `zelia-setup` once as yourself -- it creates `~/.zelia`,
writes `config.yaml`, pulls the models, and starts the systemd `--user`
service.

**From source (development):**

```bash
git clone git@github.com:Zexolver/Zelia.git
cd Zelia
./install.sh
```

Asks where to install her (defaults to `~/.zelia`, but you can point it at
a dedicated SSD mount point) and handles everything else: system packages,
Ollama, Python environment, the small conversation model, ZELIA's voice,
and a systemd `--user` service -- it starts and enables that service
itself, so ZELIA is already running by the time the script finishes.

```bash
systemctl --user status zelia     # check she's running
systemctl --user restart zelia    # restart after editing config.yaml
journalctl --user -u zelia -f     # watch logs / see what she's doing
zelia-say                         # or `python -m src.text_repl` from source -- type to her
```

## Wake word (and a faster alternative for quiet moments)

**Default right now: "hey jarvis"**, via openWakeWord. It's fully local, no
account needed, works immediately -- the tradeoff is it's a stock phrase,
not "hey zelia," since openWakeWord doesn't ship custom names.

**"Hey zelia" via Picovoice Porcupine** is wired up and ready to switch to
(`assistant.wake_word_engine: "porcupine"` in config.yaml), but its current
free-tier terms for custom wake words are genuinely unclear as of this
writing -- Picovoice's own GitHub repo (github.com/Picovoice/porcupine)
states personal/non-commercial accounts can train custom x86_64 models for
free, while other current pricing pages describe custom wake words as
Enterprise-only. Worth five minutes to check yourself before relying on it:

1. Free signup (no card) at https://console.picovoice.ai, grab your
   AccessKey.
2. Try training a custom wake word for "hey zelia" targeting Linux/x86_64
   and see whether it lets you export it on a personal account.
3. If it works: download the `.ppn` file to
   `<install_dir>/models/wakeword/hey-zelia.ppn`, paste your AccessKey into
   `assistant.picovoice_access_key`, set `wake_word_engine: "porcupine"`,
   restart (`systemctl --user restart zelia`).
4. If it doesn't (paywalled): no harm done, just stay on openWakeWord.
   Detection itself is fully offline either way once you have a model --
   the account/internet dependency is only for the one-time training step.

**Push-to-talk hotkey -- probably the better fix for whispering:** wake
word detection is an audio classifier tuned for normal speaking volume, so
it gets unreliable when you're talking quietly (e.g. not wanting to wake
someone up). `hotkey.key` in config.yaml (default `KEY_SCROLLLOCK`) arms a
push-to-talk key that skips wake word detection entirely -- press it, then
just talk at whatever volume, and only VAD-based recording + Whisper are
involved, which handle quiet/whispered speech far better than a phrase
detector does. Works identically on Xorg and Wayland (reads raw kernel
input events via evdev, same mechanism/permissions as the desktop-control
`ydotool` setup). Pick a different key by finding its evdev name -- run
`sudo libinput debug-events` and press the key you want, or check
`/usr/include/linux/input-event-codes.h` for the full `KEY_*` list -- then
set `hotkey.key` to that name and restart.

## GPU support

`src/gpu_detect.py` auto-detects your GPU at startup -- nothing is
hardcoded to a specific card. What gets accelerated depends on vendor:

- **NVIDIA:** everything -- Whisper (STT), Ollama (small brain + vision
  model), and AirLLM all use CUDA.
- **AMD:** Ollama uses Vulkan (`install.sh` installs `vulkan-radeon` and
  configures this automatically), so the small brain and vision model are
  genuinely GPU-accelerated. Whisper and AirLLM run on CPU, because:
  - Whisper's backend (CTranslate2) has no ROCm or Vulkan path at all.
  - AirLLM needs a ROCm-enabled PyTorch build, and official ROCm dropped
    support for older architectures (Polaris/RX 5xx and earlier -- this
    project's reference hardware is an RX 580) some time ago. Standard
    `pip install torch` wheels aren't compiled with kernels for those
    cards, so there's no GPU path for AirLLM out of the box on this kind
    of GPU.
- **Nothing detected:** everything runs on CPU. Slower, especially for
  AirLLM, but functional.

**If you want to chase full ROCm acceleration on an unsupported AMD card
anyway:** there are community-maintained Docker images that compile PyTorch
against specific older ROCm versions with the needed architecture flags
re-enabled (search GitHub for "rocm gfx803 pytorch" if you're on Polaris,
or the equivalent gfx target for your card). These aren't official, aren't
maintained by ZELIA, and have a track record of being version-fussy and
occasionally unstable across kernel updates -- worth it if you want to push
AirLLM's speed further, not necessary for ZELIA to work.

## Desktop control

Nothing runs hidden by default. Shell/git/build commands open a real
terminal window (`run_in_terminal`) so you can watch them happen -- the
quiet/background path (`run_shell_quiet`) only gets used if you explicitly
ask for something to run in the background.

Works on both Xorg and Wayland:

- **Typing, key combos, clicking:** Xorg uses `xdotool`. Wayland uses
  `ydotool`, which works at the kernel `uinput` level, so it doesn't care
  which compositor you're running. `install.sh` installs a `ydotoold`
  systemd `--user` service (the Arch package doesn't ship one) and adds you
  to the `input` group it needs -- **log out and back in once** after
  installing for that group membership to actually take effect, otherwise
  ydotool will silently fail to type/click. Check it's running with
  `systemctl --user status ydotoold`.
- **Finding things to click:** `find_text_on_screen` OCRs the screen and
  returns coordinates for matching text, so ZELIA can click a button or menu
  item she can only see, not reach programmatically -- useful inside
  browsers, Godot, or any GUI app.
- **Window focus:** works normally on Xorg (`wmctrl`). On Wayland it's
  best-effort -- there's no standard cross-compositor API for this, so it's
  wired up for wlroots-based compositors (Sway, Hyprland) and skipped
  gracefully on GNOME/KDE Wayland, where a freshly launched window is
  usually already focused anyway.

**Browser:** Floorp is the default (`desktop.default_browser` in
config.yaml). Say "use Chromium for this" for a one-off switch, or "always
use Firefox" to change the default permanently. `open_browser` launches the
real app directly to a URL rather than simulating clicks into an address
bar, since basically every browser supports that as a command-line argument
-- much more reliable than typing it in.

**Using Godot (or any other complex GUI app):** ZELIA can launch/focus it
(`show_me`), write files directly into a Godot project (GDScript, scenes,
etc. via `write_file`), and interact with the editor UI itself through the
OCR-and-click primitives above. Be realistic about this last part, though --
it's screenshot-and-guess, not a native accessibility integration, so
precise work like dragging nodes around a scene is going to be clunky
compared to you just doing it by hand. It's genuinely useful for "click the
Play button" or "open the Script tab," less so for fine visual editing.

## Tuning the GPU split

If quick commands feel sluggish while a big project is running, raise
`gpu.reserved_for_small_brain_mb`. If big projects are running out of memory,
lower it (they'll just lean more on disk offload and get slower). No restart
needed for the *next* big job -- the value is read fresh each time one is
submitted. (This only matters on hardware where AirLLM gets GPU acceleration
in the first place -- see "GPU support" above.)

## Swapping models

- Small brain: change `brains.small.model` to any Ollama model tag, then
  `ollama pull <tag>`.
- Large brain: change `brains.large.model` to any Hugging Face model id
  AirLLM supports. Bigger = better quality but slower on limited VRAM;
  `brains.large.compression: "4bit"` trades some quality for speed/footprint.

## Safety notes

- All file/shell operations are scoped to `agent.workspace_dir`
  (`<install_dir>/workspace` by default) except explicit shell commands,
  which can touch anything the command specifies -- that's why destructive
  patterns trigger a spoken confirmation. Review `src/agent/tools/shell_tool.py`
  if you want to tighten or loosen that pattern list.
- Set `agent.confirm_before_destructive: false` in config.yaml to disable
  confirmations entirely (not recommended).

## Project layout

```
install.sh                     from-source installer (dev use; packaging/ is the primary install path)
packaging/                     PKGBUILDs (stable + testing/-git), zelia-setup, zelia-say -- see packaging/README.md
requirements.txt
config/config.yaml.template    filled in by install.sh/zelia-setup -> config/config.yaml
systemd/zelia.service.template  filled in by install.sh/zelia-setup -> ~/.config/systemd/user/zelia.service
systemd/ydotoold.service.template  filled in by install.sh/zelia-setup -> ~/.config/systemd/user/ydotoold.service
src/
  main.py                      wake word + hotkey + typed input -> STT/text -> agent -> TTS + text loop
  wake_word.py                 Porcupine ("hey zelia") + openWakeWord listeners
  hotkey_listener.py           push-to-talk key (evdev, Xorg+Wayland alike)
  text_input.py                Unix-socket text channel the main process listens on
  text_repl.py                 terminal client for text_input.py (`python -m src.text_repl` / `zelia-say`)
  stt.py                       faster-whisper
  tts.py                       Piper
  config.py                    config.yaml loader
  gpu_manager.py                VRAM budget between small brain & AirLLM
  gpu_detect.py                 vendor auto-detection (NVIDIA/AMD/none), device resolution
  router.py                    decides "small" vs "large" brain per request
  game_guard.py                detects gaming, drives priority/queuing decisions
  brains/
    small_brain.py             Ollama client
    large_brain.py             spawns AirLLM worker subprocess, async, game-aware queue
    airllm_worker.py           actual AirLLM subprocess entry point
  memory/
    second_brain.py            ChromaDB long-term memory, auto-capture, ONNX embeddings, async writes
  agent/
    agent_loop.py               tool-calling loop + confirmation flow
    tools/
      shell_tool.py             quiet/background shell execution (run_shell_quiet)
      file_tool.py
      browser_tool.py           fetch_url (silent text extraction)
      browser_control.py       open_browser, default/override browser handling
      code_tool.py              runs in a visible terminal by default
      desktop_control.py       visible terminal, typing/clicking, Xorg+Wayland
      screen_tool.py           screenshot + OCR + optional vision model
      app_launcher.py          find/launch/focus apps, "show me X"
```

## Known rough edges (first version)

- Default wake word is "hey jarvis," not "hey zelia" -- see "Wake word (and
  a faster alternative for quiet moments)" above for the Porcupine path (if
  its free tier works out) or just use the push-to-talk hotkey instead.
- The push-to-talk hotkey grabs every input device evdev reports as a
  keyboard (has both a Q and a Z key) -- on unusual setups (multiple
  keyboards, some being misdetected) you may need to narrow
  `_find_keyboards()` in `src/hotkey_listener.py` to a specific device path.
- `press_key`'s Wayland path only knows a small set of key combos so far
  (`src/agent/tools/desktop_control.py`'s `YDOTOOL_KEYS`) -- extend that map
  if you need combos beyond ctrl/alt/shift/super + a handful of keys.
- Window focus on GNOME/KDE Wayland isn't available (see "Desktop control"
  above) -- freshly launched apps are usually already focused, but ZELIA
  can't deliberately switch focus to an already-open window there.
- `fetch_url` is plain HTTP + text extraction; JS-heavy sites need the
  Playwright path wired in (the dependency's already installed).
- AirLLM on an 8GB card, even capped generously, can take minutes per
  response on genuinely large models -- that's the nature of layer-streaming
  from disk, not a bug.
- `app_launcher` matches installed `.desktop` entries by name -- very
  unusually-named apps might need an exact-ish match rather than a loose
  description.
- Game detection is process-name + GPU-usage based, not a hardcoded list of
  every game that exists -- add titles that aren't caught automatically to
  `gaming.extra_process_patterns` in config.yaml.
- On AMD GPUs, the GPU-usage signal for game detection needs `rocm-smi`
  installed to work (most systems won't have it unless you set up ROCm
  yourself) -- process-name matching still works fine without it.
