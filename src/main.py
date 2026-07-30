"""
ZEUS's main loop:

  wake word -> record -> transcribe -> agent (tools + routing) -> speak

Run with:  python -m src.main
(install.sh sets up a systemd --user service that does exactly this)
"""
import os
import sys
import threading
import time

from src.config import load_config
from src.utils.logger import get_logger
from src.wake_word import build_wake_word_listener
from src.hotkey_listener import start_hotkey_listener
from src.stt import SpeechToText
from src.tts import TextToSpeech
from src.brains.small_brain import SmallBrain
from src.brains.large_brain import LargeBrain
from src.memory.second_brain import SecondBrain
from src.agent.agent_loop import AgentLoop
from src.game_guard import GameGuard

log = get_logger("main")


def build_confirmation_asker(stt: SpeechToText, tts: TextToSpeech):
    def ask_confirmation(question: str) -> bool:
        tts.speak(question + " Say yes or no.")
        answer = stt.listen_and_transcribe().lower()
        return any(word in answer for word in ["yes", "yeah", "yep", "confirm", "go ahead", "do it"])
    return ask_confirmation


def start_priority_manager(game_guard: GameGuard, normal_nice: int, gaming_nice: int, poll_seconds: float = 5.0):
    """Lowers ZEUS's own process priority while a game is running, restores it after."""
    try:
        import psutil
        proc = psutil.Process(os.getpid())
    except ImportError:
        log.warning("psutil not available; skipping dynamic priority management.")
        return

    def loop():
        current = normal_nice
        while True:
            gaming = game_guard.is_gaming()
            target = gaming_nice if gaming else normal_nice
            if target != current:
                try:
                    proc.nice(target)
                    current = target
                    log.info("Adjusted ZEUS's process priority (nice=%s) for %s", target, "gaming" if gaming else "normal use")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not renice: %s", exc)
            time.sleep(poll_seconds)

    threading.Thread(target=loop, daemon=True).start()


def main():
    cfg = load_config()

    log.info("Starting ZEUS (install dir: %s)", cfg.install_dir)

    stt = SpeechToText(
        model_size=cfg.stt.model_size,
        device=cfg.stt.device,
        compute_type=cfg.stt.compute_type,
    )
    tts = TextToSpeech(
        voice_name=cfg.tts.voice,
        models_dir=f"{cfg.install_dir}/models/piper",
        speaking_rate=cfg.tts.speaking_rate,
    )
    small_brain = SmallBrain(model=cfg.brains.small.model, host=cfg.brains.small.host)

    game_guard = GameGuard(
        extra_patterns=cfg.gaming.get("extra_process_patterns", []),
        poll_interval_seconds=cfg.gaming.get("poll_interval_seconds", 5.0),
    )
    game_guard.start_background_poll()
    start_priority_manager(
        game_guard,
        normal_nice=cfg.gaming.get("normal_nice", 0),
        gaming_nice=cfg.gaming.get("gaming_nice", 10),
    )

    large_brain = LargeBrain(game_guard=game_guard)
    second_brain = SecondBrain(
        db_path=cfg.memory.db_path,
        embedding_model=cfg.memory.embedding_model,
        top_k=cfg.memory.retrieval_top_k,
    )

    agent = AgentLoop(
        small_brain=small_brain,
        large_brain=large_brain,
        second_brain=second_brain,
        workspace_dir=cfg.agent.workspace_dir,
        ask_confirmation=build_confirmation_asker(stt, tts),
        vision_model=cfg.screen.get("vision_model", "moondream"),
        ollama_host=cfg.brains.small.host,
        default_browser=cfg.desktop.get("default_browser", "floorp"),
        config_path=os.environ.get("ZEUS_CONFIG", f"{cfg.install_dir}/config/config.yaml"),
    )

    activation_lock = threading.Lock()

    def on_wake():
        if not activation_lock.acquire(blocking=False):
            log.info("Already handling a request -- ignoring this activation.")
            return
        try:
            tts.speak("Yes?")
            user_text = stt.listen_and_transcribe()
            if not user_text:
                return
            agent.handle_request(
                user_text,
                speak=tts.speak,
                remember_and_reply_when_done=tts.speak,
            )
        finally:
            activation_lock.release()

    if cfg.hotkey.get("enabled", True):
        start_hotkey_listener(cfg.hotkey.get("key", "KEY_SCROLLLOCK"), on_wake)

    try:
        listener = build_wake_word_listener(cfg)
    except Exception as exc:
        log.error("Could not start the wake word engine: %s", exc)
        log.error("See README.md, section 'Custom wake word', to finish setting up 'hey zeus'.")
        sys.exit(1)

    log.info("ZEUS is ready. Say '%s', or press your push-to-talk hotkey.", cfg.assistant.wake_word)
    try:
        listener.listen_forever(on_wake)
    except KeyboardInterrupt:
        log.info("Shutting down.")
        sys.exit(0)


if __name__ == "__main__":
    main()
