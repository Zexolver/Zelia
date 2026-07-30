"""Fast, always-available brain for normal conversation and simple commands.

This talks to a local Ollama server. Ollama keeps this model resident in the
VRAM headroom reserved by gpu_manager (see config.yaml: gpu.reserved_for_small_brain_mb),
so it keeps responding instantly even while AirLLM is grinding through a big
project in the background.
"""
import ollama

from src.utils.logger import get_logger

log = get_logger("small_brain")


class SmallBrain:
    def __init__(self, model: str, host: str):
        self.model = model
        self.client = ollama.Client(host=host)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """messages: [{"role": "user"/"assistant"/"system"/"tool", "content": ...}, ...]"""
        response = self.client.chat(model=self.model, messages=messages, tools=tools or [])
        return response["message"]
