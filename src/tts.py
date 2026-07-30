"""
Text-to-speech using Piper.

Piper runs entirely on CPU and is fast enough to feel real-time, which is why
it was picked over heavier GPU-hungry TTS engines -- it never competes with
the small brain or AirLLM for VRAM.
"""
import io
import wave

import sounddevice as sd
import numpy as np
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
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            self.voice.synthesize(text, wav_file, length_scale=1.0 / self.speaking_rate)
        buf.seek(0)
        with wave.open(buf, "rb") as wav_file:
            audio = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype=np.int16)
            sd.play(audio, samplerate=wav_file.getframerate())
            sd.wait()
