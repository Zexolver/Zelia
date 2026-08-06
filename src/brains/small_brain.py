"""Fast, always-available brain for normal conversation and simple commands.

This talks to a local Ollama server. Ollama keeps this model resident in the
VRAM headroom reserved by gpu_manager (see config.yaml: gpu.reserved_for_small_brain_mb),
so it keeps responding instantly even while AirLLM is grinding through a big
project in the background.
"""
import ollama

from src.utils.logger import get_logger

log = get_logger("small_brain")

# Ollama's own default keep_alive is 5 minutes -- after that it unloads the
# model, and the next request eats a real reload cost (measured live:
# ~6.2s cold vs ~0.36s warm, a >15x difference). That directly contradicts
# this class's own claim above ("stays resident essentially all the
# time") -- nothing was actually enforcing it. -1 tells Ollama to keep it
# loaded until explicitly told otherwise (or the server restarts), which
# is what "always-available" is supposed to mean here. Doesn't apply to
# the vision model (moondream, used by describe_screen) -- that one stays
# on Ollama's own default short keep_alive on purpose, since it's meant to
# load on demand and unload again, not stay resident.
KEEP_ALIVE = -1

# Never set explicitly before -- Ollama silently defaulted to 4096 tokens
# (confirmed via `ollama ps`'s CONTEXT column). That was fine while
# TOOL_SCHEMAS was small, but confirmed live as a real, serious bug once
# the tool set grew to ~38 entries: the schema JSON alone is large enough
# to overflow a 4096 context, and Ollama's behavior on overflow isn't a
# clean error -- it's silent truncation/corruption of the prompt, which
# showed up as the model calling a completely unrelated tool (or none at
# all) for a request as simple as "say hello", taking ~40s to do it.
# Reproduced directly against ollama.chat() (bypassing this whole
# pipeline) with num_ctx unset vs 8192 -- identical messages/tools, wrong
# tool call vs correct plain-text reply. 16384 gives real headroom beyond
# the 38-tool baseline for future tool growth and multi-round
# conversations (each round appends tool results, which count against the
# same context) -- KV-cache cost scales with this, but a 7B q4 model has
# plenty of room on this 8GB card to afford it.
NUM_CTX = 16384


class SmallBrain:
    def __init__(self, model: str, host: str):
        self.model = model
        self.client = ollama.Client(host=host)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """messages: [{"role": "user"/"assistant"/"system"/"tool", "content": ...}, ...]"""
        response = self.client.chat(
            model=self.model, messages=messages, tools=tools or [], keep_alive=KEEP_ALIVE,
            options={"num_ctx": NUM_CTX},
        )
        return response["message"]
