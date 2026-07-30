"""Speech-to-text using faster-whisper, with simple silence-based recording."""
import queue
import time

import numpy as np
import sounddevice as sd
import webrtcvad
from faster_whisper import WhisperModel

from src.gpu_detect import resolve_stt_device
from src.utils.logger import get_logger

log = get_logger("stt")

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000)
SILENCE_FRAMES_TO_STOP = 25  # ~750ms of silence ends the utterance


class SpeechToText:
    def __init__(self, model_size: str = "small", device: str = "auto", compute_type: str = "int8_float16"):
        if device == "auto":
            device = resolve_stt_device()
            if device == "cpu":
                compute_type = "int8"  # int8_float16 needs a CUDA device
        log.info("Loading Whisper (%s) on device=%s", model_size, device)
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self.vad = webrtcvad.Vad(2)  # 0-3, higher = more aggressive filtering

    def record_utterance(self) -> np.ndarray:
        """Records from the mic until the user stops talking. Returns float32 PCM."""
        q: queue.Queue = queue.Queue()

        def callback(indata, frames, time_info, status):
            q.put(indata.copy())

        frames = []
        silence_run = 0
        heard_speech = False

        with sd.InputStream(
            samplerate=SAMPLE_RATE, blocksize=FRAME_SIZE, channels=1, dtype="int16", callback=callback
        ):
            log.info("Recording...")
            while True:
                chunk = q.get()
                frames.append(chunk)
                is_speech = self.vad.is_speech(chunk.tobytes(), SAMPLE_RATE)
                if is_speech:
                    heard_speech = True
                    silence_run = 0
                else:
                    silence_run += 1

                if heard_speech and silence_run > SILENCE_FRAMES_TO_STOP:
                    break
                if not heard_speech and len(frames) > (SAMPLE_RATE // FRAME_SIZE) * 6:
                    # ~6s of nothing but silence -- give up
                    break

        audio = np.concatenate(frames, axis=0).flatten().astype(np.float32) / 32768.0
        return audio

    def transcribe(self, audio: np.ndarray, redact: bool = False) -> str:
        segments, _info = self.model.transcribe(audio, language="en", beam_size=1)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        # redact=True for anything sensitive (e.g. a spoken password) -- never
        # let it hit the journal in plaintext.
        log.info("Heard: %r", text if not redact else "[redacted]")
        return text

    def listen_and_transcribe(self, redact: bool = False) -> str:
        audio = self.record_utterance()
        return self.transcribe(audio, redact=redact)
