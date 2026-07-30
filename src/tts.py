"""
Text-to-speech using Piper.

Piper runs entirely on CPU and is fast enough to feel real-time, which is why
it was picked over heavier GPU-hungry TTS engines -- it never competes with
the small brain or AirLLM for VRAM.

piper-tts 1.x (the OHF-voice/piper1-gpl rewrite) returns an iterable of
AudioChunk objects from synthesize() rather than writing into a wave.Wave_write
handle the caller supplies -- concatenate the chunks' raw int16 PCM directly
instead of going through the wave module.
"""
import numpy as np
import sounddevice as sd
from piper.config import SynthesisConfig
from piper.voice import PiperVoice

from src.utils.logger import get_logger

log = get_logger("tts")


class TextToSpeech:
    def __init__(self, voice_name: str, models_dir: str, speaking_rate: float = 1.0):
        onnx_path = f"{models_dir}/{voice_name}.onnx"
        config_path = f"{models_dir}/{voice_name}.onnx.json"
        self.voice = PiperVoice.load(onnx_path, config_path=config_path)
        self.speaking_rate = speaking_rate

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        log.info("Speaking: %r", text)
        syn_config = SynthesisConfig(length_scale=1.0 / self.speaking_rate)
        chunks = list(self.voice.synthesize(text, syn_config=syn_config))
        if not chunks:
            return
        audio = np.concatenate(
            [np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16) for chunk in chunks]
        )
        sd.play(audio, samplerate=chunks[0].sample_rate)
        sd.wait()
