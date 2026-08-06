"""
Runs as its own OS process (spawned by large_brain.py, wrapped in a
systemd --user scope with real system RAM/CPU limits -- see
resource_manager.py) so it gets its own CUDA context. That's what makes
the memory-fraction cap in gpu_manager actually stick -- it's applied
per-process, before AirLLM/torch initialize CUDA, so this worker can
never grow past the VRAM budget left over after the small brain's
reservation.

Two modes, decided by whether the job includes a workspace_dir:
- workspace_dir set: coding_agent.py's tool-calling loop runs, with
  read/write/run-code access scoped to that directory -- this is the
  "code this while I'm away" path.
- workspace_dir absent: the original plain single-shot completion (one
  prompt in, one answer out, no tools) -- kept for any caller that just
  wants a long-form answer, not code execution.

Usage: python -m src.brains.airllm_worker <job_json_path> <result_json_path>
"""
import json
import sys
import time

from src.config import load_config
from src.gpu_manager import get_budget, apply_airllm_memory_cap
from src.utils.logger import get_logger

log = get_logger("airllm_worker")

# Confirmed live: loading a fresh (not-yet-downloaded) model can hit a
# transient race between huggingface_hub's concurrent shard downloader
# (hf_thread_map) and AirLLM's delete_original=True cleanup -- a shard's
# temp file can vanish mid-move if another in-flight request for the same
# blob gets cleaned up underneath it, raising a bare FileNotFoundError.
# Not our bug to fix (it's in the two upstream libraries' interaction),
# and it's the kind of thing a plain retry resolves -- the second attempt
# resumes from whatever was already downloaded/split rather than starting
# over, since split_and_save_layers checks for already-completed layer
# shards before redoing them.
MODEL_LOAD_RETRIES = 3
MODEL_LOAD_RETRY_DELAY_SECONDS = 10


def main():
    job_path, result_path = sys.argv[1], sys.argv[2]

    cfg = load_config()
    budget = get_budget(cfg)
    apply_airllm_memory_cap(budget)  # MUST happen before importing airllm/torch does any CUDA init

    from airllm import AutoModel  # noqa: E402  (deliberately imported after the memory cap)

    with open(job_path) as f:
        job = json.load(f)

    # `or`, not dict.get(key, default): submit_async's job dict always has
    # these keys present (even when the caller didn't pass them, as the
    # default parameter value None), so .get(key, default) never falls
    # through to the config default -- confirmed live, this silently ran
    # a job at full bf16 precision instead of the configured 4bit
    # compression, which would have blown well past the disk budget.
    model_name = job.get("model") or cfg.brains.large.model
    compression = job.get("compression") or cfg.brains.large.get("compression")
    max_new_tokens = job.get("max_new_tokens") or cfg.brains.large.get("max_new_tokens", 2048)
    prompt = job["prompt"]
    workspace_dir = job.get("workspace_dir")

    # AirLLM's own default is device='cuda:0' regardless of what's actually
    # available -- it does NOT auto-detect, and blows up with a bare
    # RuntimeError on any machine without an NVIDIA driver (this reference
    # machine's AMD card included) unless told otherwise explicitly here.
    device = "cuda:0" if budget.airllm_gpu_usable else "cpu"
    log.info("Loading %s via AirLLM (device=%s, compression=%s)...", model_name, device, compression)
    # delete_original=True: AirLLM downloads the full bf16/fp16 checkpoint
    # shard by shard, splits+compresses each into per-layer files, and by
    # default keeps the original shards around afterward -- fine if disk
    # is abundant, but on a tight budget the ORIGINAL download (roughly
    # 2 bytes/param, e.g. ~44GB for a 22B model) plus the compressed
    # output (roughly 0.5 bytes/param at 4bit) can't both fit. With this
    # set, each shard is deleted as soon as its layers are extracted, so
    # only one shard's worth of original data is ever on disk at once
    # alongside the compressed output accumulated so far.
    for attempt in range(1, MODEL_LOAD_RETRIES + 1):
        try:
            model = AutoModel.from_pretrained(model_name, compression=compression, device=device, delete_original=True)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == MODEL_LOAD_RETRIES:
                raise
            log.warning(
                "Model load attempt %d/%d failed (%s: %s) -- retrying in %ds. "
                "Already-downloaded/split layers are reused, not redone.",
                attempt, MODEL_LOAD_RETRIES, type(exc).__name__, exc, MODEL_LOAD_RETRY_DELAY_SECONDS,
            )
            time.sleep(MODEL_LOAD_RETRY_DELAY_SECONDS)

    def generate(text: str, max_tokens: int = max_new_tokens) -> str:
        input_ids = model.tokenizer(text, return_tensors="pt")
        output_ids = model.generate(
            input_ids["input_ids"],
            max_new_tokens=max_tokens,
            use_cache=True,
        )
        # generate() returns the prompt tokens followed by the new ones --
        # slice off the input length, otherwise the reply is the entire
        # prompt echoed back verbatim before the actual answer.
        new_tokens = output_ids[0][input_ids["input_ids"].shape[-1]:]
        return model.tokenizer.decode(new_tokens, skip_special_tokens=True)

    if workspace_dir:
        from src.brains.coding_agent import run_coding_task, MAX_NEW_TOKENS_PER_STEP

        log.info("Running as a coding task in %s...", workspace_dir)
        result = run_coding_task(
            prompt, workspace_dir,
            generate_fn=lambda text: generate(text, max_tokens=MAX_NEW_TOKENS_PER_STEP),
        )
    else:
        log.info("Generating (this can take a while on limited VRAM/CPU)...")
        result = {"ok": True, "text": generate(prompt)}

    with open(result_path, "w") as f:
        json.dump(result, f)
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
