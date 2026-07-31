#!/usr/bin/env bash
# ZELIA installer -- Manjaro/Arch only (pacman-based).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== ZELIA installer ==="
echo

# ---------------------------------------------------------------------------
# 1. Install directory
# ---------------------------------------------------------------------------
read -rp "Install directory [default: $HOME/.zelia]: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-$HOME/.zelia}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
mkdir -p "$INSTALL_DIR"
echo "Installing to $INSTALL_DIR"
echo

# ---------------------------------------------------------------------------
# 2. GPU vendor auto-detect (never hardcoded -- see src/gpu_detect.py, which
#    does the same detection at runtime for the running assistant).
# ---------------------------------------------------------------------------
GPU_VENDOR="none"
if command -v lspci >/dev/null 2>&1; then
    if lspci | grep -qi 'vga\|3d\|display' && lspci | grep -i 'vga\|3d\|display' | grep -qi nvidia; then
        GPU_VENDOR="nvidia"
    elif lspci | grep -qi 'vga\|3d\|display' && lspci | grep -i 'vga\|3d\|display' | grep -qi 'amd\|advanced micro devices\|ati'; then
        GPU_VENDOR="amd"
    fi
fi
echo "Detected GPU vendor: $GPU_VENDOR"
echo

# ---------------------------------------------------------------------------
# 3. Session type (Xorg vs Wayland) -- also auto-detected, install both
#    input-control paths regardless since the user may switch sessions.
# ---------------------------------------------------------------------------
SESSION_TYPE="${XDG_SESSION_TYPE:-unknown}"
echo "Detected session type: $SESSION_TYPE (installing support for both Xorg and Wayland regardless)"
echo

# ---------------------------------------------------------------------------
# 4. System packages
# ---------------------------------------------------------------------------
echo "--- Installing system packages (pacman) ---"
PACMAN_PKGS=(
    base-devel
    git
    python
    python-pip
    python-virtualenv
    tesseract
    tesseract-data-eng
    xdotool
    wmctrl
    ydotool
    portaudio
    at-spi2-core
    gobject-introspection
    cairo
    spectacle
)
if [ "$GPU_VENDOR" = "amd" ]; then
    PACMAN_PKGS+=(vulkan-radeon lib32-vulkan-radeon vulkan-icd-loader)
fi
sudo pacman -S --needed --noconfirm "${PACMAN_PKGS[@]}"
echo

# ---------------------------------------------------------------------------
# 5. ydotoold systemd --user service (the Arch ydotool package ships no
#    usable unit) + add the current user to the `input` group it needs.
# ---------------------------------------------------------------------------
echo "--- Setting up ydotoold ---"
YDOTOOLD_BIN="$(command -v ydotoold || echo /usr/bin/ydotoold)"
mkdir -p "$HOME/.config/systemd/user"
sed "s|{{YDOTOOLD_BIN}}|$YDOTOOLD_BIN|g" \
    "$SCRIPT_DIR/systemd/ydotoold.service.template" \
    > "$HOME/.config/systemd/user/ydotoold.service"

if ! groups "$USER" | grep -qw input; then
    sudo usermod -aG input "$USER"
    NEEDS_RELOGIN=1
else
    NEEDS_RELOGIN=0
fi

systemctl --user daemon-reload
systemctl --user enable --now ydotoold.service
echo

# ---------------------------------------------------------------------------
# 6. Ollama (small brain + vision model host)
# ---------------------------------------------------------------------------
echo "--- Installing Ollama ---"
if [ "$GPU_VENDOR" = "amd" ]; then
    # ollama-vulkan is a separate Arch package (adds the Vulkan ggml
    # backend .so alongside plain `ollama`, doesn't replace it) -- works on
    # GPU generations ROCm has dropped (e.g. Polaris/RX 5xx), since Vulkan
    # is vendor/architecture-agnostic.
    sudo pacman -S --needed --noconfirm ollama-vulkan
    # /etc/environment only reaches PAM login sessions, never a
    # system-level systemd unit -- a drop-in override is what actually
    # reaches ollama.service. The Vulkan backend also lives in a
    # subdirectory ollama's dynamic loader needs pointed at explicitly.
    sudo mkdir -p /etc/systemd/system/ollama.service.d
    printf '[Service]\nEnvironment="OLLAMA_VULKAN=1"\nEnvironment="OLLAMA_LIBRARY_PATH=/usr/lib/ollama/vulkan"\n' \
        | sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null
else
    sudo pacman -S --needed --noconfirm ollama
fi
sudo systemctl daemon-reload
sudo systemctl enable ollama 2>/dev/null || true
sudo systemctl restart ollama  # picks up the vulkan override.conf even if ollama was already running
echo

# ---------------------------------------------------------------------------
# 7. Python environment
# ---------------------------------------------------------------------------
echo "--- Setting up Python environment ---"
cp -r "$SCRIPT_DIR/src" "$INSTALL_DIR/src"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
echo

# ---------------------------------------------------------------------------
# 8. Pull models
# ---------------------------------------------------------------------------
echo "--- Pulling models (this can take a while) ---"
ollama pull qwen2.5:7b-instruct-q4_K_M
ollama pull moondream
mkdir -p "$INSTALL_DIR/models/piper" "$INSTALL_DIR/models/wakeword"
if [ ! -f "$INSTALL_DIR/models/piper/en_US-amy-medium.onnx" ]; then
    curl -fsSL -o "$INSTALL_DIR/models/piper/en_US-amy-medium.onnx" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx"
    curl -fsSL -o "$INSTALL_DIR/models/piper/en_US-amy-medium.onnx.json" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json"
fi
echo

# ---------------------------------------------------------------------------
# 9. Config
# ---------------------------------------------------------------------------
echo "--- Writing config ---"
mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/workspace" "$INSTALL_DIR/memory" "$INSTALL_DIR/logs"
if [ -f "$INSTALL_DIR/config/config.yaml" ]; then
    echo "config/config.yaml already exists, leaving it alone."
else
    sed "s|{{INSTALL_DIR}}|$INSTALL_DIR|g" \
        "$SCRIPT_DIR/config/config.yaml.template" \
        > "$INSTALL_DIR/config/config.yaml"
fi
echo

# ---------------------------------------------------------------------------
# 10. systemd --user service for ZELIA herself, started and enabled now so
#     the install is actually finished when this script exits (previously
#     this only printed the systemctl commands -- see CLAUDE.md "Known
#     issues").
# ---------------------------------------------------------------------------
echo "--- Installing ZELIA systemd service ---"
sed "s|{{INSTALL_DIR}}|$INSTALL_DIR|g" \
    "$SCRIPT_DIR/systemd/zelia.service.template" \
    > "$HOME/.config/systemd/user/zelia.service"
systemctl --user daemon-reload
systemctl --user enable --now zelia.service
echo

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "=== Done ==="
echo "GPU: $GPU_VENDOR -- $(
    case "$GPU_VENDOR" in
        nvidia) echo "everything accelerated (Whisper/CUDA, Ollama/CUDA, AirLLM/CUDA)";;
        amd)    echo "Ollama accelerated via Vulkan; Whisper and AirLLM run on CPU (no ROCm/Vulkan path for either)";;
        *)      echo "no GPU detected, everything runs on CPU";;
    esac
)"
echo "ZELIA is running now (systemctl --user status zelia)."
echo
echo "Wake word: \"hey jarvis\" works right now, no setup needed (openWakeWord, fully local)."
echo "A real \"hey zelia\" phrase is possible via Picovoice Porcupine, but their current"
echo "free-tier terms for custom wake words are unclear as of this writing -- check"
echo "console.picovoice.ai yourself before switching assistant.wake_word_engine to"
echo "\"porcupine\" in config.yaml (see wake_word.py and README for details)."
echo
echo "Prefer talking quietly? Press your push-to-talk hotkey (default: Scroll Lock)"
echo "instead of using the wake word -- see README's hotkey section."
echo
if [ "$NEEDS_RELOGIN" = "1" ]; then
    echo "NOTE: you were just added to the 'input' group (needed for ydotool)."
    echo "Log out and back in for that to take effect, otherwise desktop control"
    echo "on Wayland will silently fail to type/click."
fi
echo
echo "Useful commands:"
echo "  systemctl --user status zelia     # check she's running"
echo "  systemctl --user restart zelia    # restart after editing config.yaml"
echo "  journalctl --user -u zelia -f     # watch logs / see what she's doing"
