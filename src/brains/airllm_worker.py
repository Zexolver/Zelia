"""
Runs as its own OS process (spawned by large_brain.py) so it gets its own
CUDA context. That's what makes the memory-fraction cap in gpu_manager
actually stick -- it's applied per-process, before AirLLM/torch initialize
CUDA, so this worker can never grow past the VRAM budget left over after the
small brain's reservation.

Usage: python -m src.brains.airllm_worker <job_json_path> <result_json_path>
"""
import json
import sys

from src.config import load_config
from src.gpu_manager import get_budget, apply_airllm_memory_cap
from src.utils.logger import get_logger

log = get_logger("airllm_worker")


def main():
    job_path, result_path = sys.argv[1], sys.argv[2]

    cfg = load_config()
    budget = get_budget(cfg)
    apply_airllm_memory_cap(budget)  # MUST happen before importing airllm/torch does any CUDA init

    from airllm import AutoModel  # noqa: E402  (deliberately imported after the memory cap)

    with open(job_path) as f:
        job = json.load(f)

    model_name = job.get("model") or cfg.brains.large.model
    compression = job.get("compression", cfg.brains.large.get("compression"))
    max_new_tokens = job.get("max_new_tokens", cfg.brains.large.get("max_new_tokens", 2048))
    prompt = job["prompt"]

    log.info("Loading %s via AirLLM (compression=%s)...", model_name, compression)
    model = AutoModel.from_pretrained(model_name, compression=compression)

    input_ids = model.tokenizer(prompt, return_tensors="pt")
    log.info("Generating (this can take a while on limited VRAM)...")
    output_ids = model.generate(
        input_ids["input_ids"],
        max_new_tokens=max_new_tokens,
        use_cache=True,
    )
    text = model.tokenizer.decode(output_ids[0], skip_special_tokens=True)

    with open(result_path, "w") as f:
        json.dump({"ok": True, "text": text}, f)
    log.info("Done, result written to %s", result_path)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log.exception("AirLLM worker failed")
        result_path = sys.argv[2]
        with open(result_path, "w") as f:
            json.dump({"ok": False, "error": str(exc)}, f)
        raise
