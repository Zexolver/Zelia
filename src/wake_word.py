"""
Always-listening wake word detector. Two backends, picked via
config.yaml's assistant.wake_word_engine:

  - "porcupine": Picovoice Porcupine, supports a real trained "hey zeus"
    phrase (see README's "Custom wake word" section for the ~5 minute setup).
  - "openwakeword": fully local/offline, but only stock phrases are
    available (no "hey zeus" model exists for it out of the box).
"""
import numpy as np
import sounddevice as sd

from src.utils.logger import get_logger

log = get_logger("wake_word")


class PorcupineListener:
    def __init__(self, access_key: str, model_path: str):
        import pvporcupine

        if not access_key:
            raise ValueError(
                "assistant.picovoice_access_key is empty in config.yaml. "
                "Grab a free key from the Picovoice console -- see README's "
                "'Custom wake word' section."
            )
        self.porcupine = pvporcupine.create(access_key=access_key, keyword_paths=[model_path])

    def listen_forever(self, on_wake):
        log.info("Listening for wake word (Porcupine)...")

        def callback(indata, frames, time_info, status):
            if status:
                log.debug("Audio status: %s", status)
            pcm = np.squeeze(indata).astype(np.int16)
            result = self.porcupine.process(pcm)
            if result >= 0:
                log.info("Wake word detected (Porcupine)")
                on_wake()

        with sd.InputStream(
            samplerate=self.porcupine.sample_rate,
            blocksize=self.porcupine.frame_length,
            channels=1,
            dtype="int16",
            callback=callback,
        ):
            while True:
                sd.sleep(100)


class OpenWakeWordListener:
    SAMPLE_RATE = 16000
    FRAME_MS = 80
    FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000)

    def __init__(self, model_name: str, threshold: float = 0.5):
        from openwakeword.model import Model as OWWModel

        self.threshold = threshold
        self.model = OWWModel(wakeword_models=[model_name])

    def listen_forever(self, on_wake):
        log.info("Listening for wake word (openWakeWord, threshold=%.2f)...", self.threshold)

        def callback(indata, frames, time_info, status):
            if status:
                log.debug("Audio status: %s", status)
            audio = np.squeeze(indata).astype(np.int16)
            predictions = self.model.predict(audio)
            for label, score in predictions.items():
                if score >= self.threshold:
                    log.info("Wake word detected (%s, score=%.2f)", label, score)
                    on_wake()

        with sd.InputStream(
            samplerate=self.SAMPLE_RATE,
            blocksize=self.FRAME_SIZE,
            channels=1,
            dtype="int16",
            callback=callback,
        ):
            while True:
                sd.sleep(100)


def build_wake_word_listener(cfg):
    engine = cfg.assistant.get("wake_word_engine", "openwakeword")
    if engine == "porcupine":
        return PorcupineListener(
            access_key=cfg.assistant.get("picovoice_access_key", ""),
            model_path=cfg.assistant.wake_word_model_path,
        )
    return OpenWakeWordListener(
        model_name=cfg.assistant.get("openwakeword_model", "hey_jarvis"),
        threshold=cfg.assistant.get("wake_word_threshold", 0.5),
    )
