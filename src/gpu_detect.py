"""
Vendor-agnostic GPU detection.

Handles the messy reality that "has a GPU" doesn't mean "has a usable ML
backend" -- older/unsupported cards (this project was built against an RX
580, gfx803/Polaris) show up as real hardware but have no working ROCm
PyTorch path, so several components need to know to fall back to CPU even
though a GPU is physically present.

What's actually accelerated per vendor:
  - NVIDIA: everything (Whisper via CUDA, Ollama via CUDA, AirLLM via CUDA).
  - AMD: Ollama only, via Vulkan (broad compatibility, no ROCm-per-arch
    kernel needed -- works fine on cards ROCm itself has dropped, like
    Polaris). Whisper and AirLLM run on CPU: CTranslate2 (Whisper's backend)
    has no ROCm/Vulkan path, and stock PyTorch wheels aren't compiled with
    kernels for old AMD architectures. See README's GPU section for the
    (fragile, unofficial) path to chase full ROCm acceleration on Polaris
    cards if you want to go down that road yourself.
  - Nothing detected: everything on CPU.
"""
import glob
import os
import subprocess

from src.utils.logger import get_logger

log = get_logger("gpu_detect")

_cached_vendor = None


def _run(cmd) -> str | None:
    try:
        return subprocess.check_output(cmd, text=True, timeout=5, stderr=subprocess.DEVNULL)
    except Exception:
        return None


def detect_vendor(force_refresh: bool = False) -> str:
    """Returns 'nvidia', 'amd', or 'none'."""
    global _cached_vendor
    if _cached_vendor is not None and not force_refresh:
        return _cached_vendor

    if _run(["nvidia-smi", "-L"]):
        _cached_vendor = "nvidia"
        return _cached_vendor

    # AMD: look for the amdgpu kernel driver's per-card sysfs entries rather
    # than depending on rocm-smi being installed (it usually isn't, unless
    # the full ROCm stack was set up).
    if glob.glob("/sys/class/drm/card*/device/vendor"):
        for path in glob.glob("/sys/class/drm/card*/device/vendor"):
            try:
                with open(path) as f:
                    if f.read().strip() == "0x1002":  # AMD's PCI vendor ID
                        _cached_vendor = "amd"
                        return _cached_vendor
            except OSError:
                continue

    lspci = _run(["lspci"])
    if lspci and any(("VGA" in line or "3D" in line) and ("AMD" in line or "ATI" in line) for line in lspci.splitlines()):
        _cached_vendor = "amd"
        return _cached_vendor

    _cached_vendor = "none"
    return _cached_vendor


def detect_total_vram_mb(vendor: str | None = None) -> int:
    vendor = vendor or detect_vendor()

    if vendor == "nvidia":
        out = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
        if out:
            try:
                return int(out.strip().splitlines()[0])
            except ValueError:
                pass

    if vendor == "amd":
        for path in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
            try:
                with open(path) as f:
                    return int(f.read().strip()) // (1024 * 1024)
            except (OSError, ValueError):
                continue
        out = _run(["rocm-smi", "--showmeminfo", "vram", "--json"])
        if out:
            import json
            try:
                data = json.loads(out)
                for card in data.values():
                    total_bytes = card.get("VRAM Total Memory (B)")
                    if total_bytes:
                        return int(total_bytes) // (1024 * 1024)
            except (ValueError, AttributeError):
                pass

    log.warning("Could not determine VRAM for vendor=%s; assuming 8192MB.", vendor)
    return 8192


def resolve_stt_device(vendor: str | None = None) -> str:
    """faster-whisper (CTranslate2) only has a working GPU path on NVIDIA."""
    vendor = vendor or detect_vendor()
    return "cuda" if vendor == "nvidia" else "cpu"


def torch_gpu_usable(vendor: str | None = None) -> bool:
    """Whether AirLLM/torch can realistically get GPU acceleration here."""
    vendor = vendor or detect_vendor()
    if vendor != "nvidia":
        return False
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False
