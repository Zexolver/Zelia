"""
Keeps AirLLM from eating the whole GPU -- on hardware where AirLLM actually
gets GPU acceleration in the first place (see gpu_detect.py: that's NVIDIA
only in this project's setup; on AMD, AirLLM runs on CPU regardless of this
budget, since there's no usable ROCm PyTorch path for older cards).

Zelia's small/fast brain (served by Ollama) and the STT model stay resident
on the GPU basically all the time so she can keep responding to quick
questions and simple commands. AirLLM only gets whatever VRAM is left over
after that reservation, and it runs in its own subprocess with a hard
memory-fraction cap, so a big background project can never starve the rest
of the system -- worst case it just runs slower or falls back to more
CPU/disk offload.
"""
from dataclasses import dataclass

from src.gpu_detect import detect_vendor, detect_total_vram_mb, torch_gpu_usable
from src.utils.logger import get_logger

log = get_logger("gpu_manager")


@dataclass
class GpuBudget:
    vendor: str
    total_mb: int
    reserved_for_small_brain_mb: int
    airllm_gpu_usable: bool

    @property
    def available_for_airllm_mb(self) -> int:
        return max(self.total_mb - self.reserved_for_small_brain_mb, 512)

    @property
    def airllm_fraction(self) -> float:
        """Fraction of total VRAM AirLLM is allowed to claim (0-1)."""
        return round(self.available_for_airllm_mb / self.total_mb, 3)


def get_budget(cfg) -> GpuBudget:
    vendor = detect_vendor()
    total = cfg.gpu.get("total_vram_mb") or detect_total_vram_mb(vendor)
    reserved = cfg.gpu.get("reserved_for_small_brain_mb", 3072)
    usable = torch_gpu_usable(vendor)

    budget = GpuBudget(vendor=vendor, total_mb=total, reserved_for_small_brain_mb=reserved, airllm_gpu_usable=usable)

    if usable:
        log.info(
            "GPU budget (%s): %sMB total, %sMB reserved for small brain, "
            "AirLLM capped to %sMB (%.0f%%)",
            vendor, budget.total_mb, budget.reserved_for_small_brain_mb,
            budget.available_for_airllm_mb, budget.airllm_fraction * 100,
        )
    else:
        log.info(
            "GPU vendor detected: %s. AirLLM has no usable GPU backend here, "
            "so big-project jobs will run on CPU (slower, but functional). "
            "The small brain + vision model still get GPU acceleration via "
            "Ollama separately.",
            vendor,
        )
    return budget


def apply_airllm_memory_cap(budget: GpuBudget) -> None:
    """
    Call this at the top of the AirLLM worker process (see
    src/brains/large_brain.py) before any CUDA/torch call happens.
    No-ops cleanly if there's no usable GPU backend for AirLLM on this machine.
    """
    if not budget.airllm_gpu_usable:
        log.info("Skipping GPU memory cap -- AirLLM will run on CPU on this machine.")
        return

    import torch
    torch.cuda.set_per_process_memory_fraction(budget.airllm_fraction, device=0)
    log.info("Applied CUDA memory fraction cap of %.0f%% to this process.", budget.airllm_fraction * 100)
