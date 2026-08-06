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
import re

import numpy as np
import sounddevice as sd
from piper.config import SynthesisConfig
from piper.voice import PiperVoice

from src.utils.logger import get_logger

log = get_logger("tts")

# Piper's grapheme-to-phoneme step reads "Zelia" as "Zee-lee-ah" -- explicit
# user correction: the name should sound like "Zelya". Respelling before
# synthesis (not changing the actual name anywhere else -- display text,
# logs, the brand itself, all stay "ZELIA") is the simplest fix that works
# regardless of which TTS engine is doing the phonemization; the Android
# app's flutter_tts does the same respelling for the same reason.
_NAME_PRONUNCIATION = re.compile(r"\bzelia\b", re.IGNORECASE)


def _respell_for_pronunciation(text: str) -> str:
    return _NAME_PRONUNCIATION.sub("Zelya", text)


# Piper reads markdown syntax literally -- "# Heading" comes out as
# "hash heading", "- item" as "dash item", "**bold**" as "asterisk
# asterisk bold asterisk asterisk". Explicit user correction: replies
# often contain markdown (headers, bullet lists, bold/links) since
# that's how the model naturally formats a text reply, but none of that
# punctuation should be read aloud. This is regex-based cleanup, not a
# real markdown parser -- good enough for speech, not meant to be exact.
_MD_HR = re.compile(r"^\s*([-*_])\1{2,}\s*$", re.MULTILINE)
_MD_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_BOLD_ITALIC = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)(.+?)\1")
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)


def _strip_markdown_for_speech(text: str) -> str:
    text = _MD_HR.sub("", text)
    text = _MD_HEADER.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _MD_BOLD_ITALIC.sub(r"\2", text)
    text = _MD_BULLET.sub("", text)
    return text

# Hard cap on how much text ever gets synthesized aloud in one go. Found
# necessary live: a "read the text on my screen" reply carried the full OCR
# dump straight into speak(), and Piper dutifully spoke the entire multi-
# hundred-word wall of text out loud -- multiple minutes of audio, during
# which the whole assistant is unresponsive (speak() blocks on sd.wait(),
# which holds main.py's activation_lock the whole time). The *text* channel
# (zelia-say / journalctl) still gets the full reply regardless -- this only
# limits what gets spoken, since a voice reply that long is bad UX even
# ignoring the lockup.
MAX_SPOKEN_CHARS = 600


def _truncate_for_speech(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    if len(text) <= limit:
        return text
    cutoff = text.rfind(" ", 0, limit)
    if cutoff <= 0:
        cutoff = limit
    return text[:cutoff].rstrip() + "... I'll spare you the rest out loud, it's all in the text reply."


# The char cap above bounds total *duration* but not *pace* -- a reply
# enumerating dozens of items (e.g. "list my Steam games") still crams
# dozens of short items into those 600 chars, which is exactly the
# "sounds rushed" complaint, just capped a bit sooner. Explicit user
# report: this recurred after the char cap alone had incidentally masked
# it once by coincidence (a reply that happened to summarize to only a
# few items) -- fixed for real here by capping *item count*, not just
# characters, for both markdown list syntax and inline comma-separated
# enumerations (an LLM asked to "list" something doesn't always use
# markdown bullets).
_SUFFIX = "I'll spare you the rest out loud, it's all in the text reply."
MAX_SPOKEN_LIST_ITEMS = 5
_LIST_ITEM_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# A run of at least this many comma-separated pieces on one line reads as
# an inline list ("Portal 2, Half-Life, Team Fortress 2, ...") rather than
# ordinary prose, which rarely strings together this many commas.
_COMMA_LIST_MIN_ITEMS = 10


def _cap_bullet_list_for_speech(text: str, limit: int = MAX_SPOKEN_LIST_ITEMS) -> str:
    lines = text.split("\n")
    item_idxs = [i for i, line in enumerate(lines) if _LIST_ITEM_LINE.match(line)]
    if len(item_idxs) <= limit:
        return text
    cutoff = item_idxs[limit]
    remaining = len(item_idxs) - limit
    kept = "\n".join(lines[:cutoff]).rstrip()
    return f"{kept}\n...and {remaining} more. {_SUFFIX}"


def _cap_comma_list_for_speech(text: str, limit: int = MAX_SPOKEN_LIST_ITEMS) -> str:
    out_lines = []
    for line in text.split("\n"):
        parts = line.split(", ")
        if len(parts) >= _COMMA_LIST_MIN_ITEMS:
            remaining = len(parts) - limit
            line = f"{', '.join(parts[:limit])}, and {remaining} more. {_SUFFIX}"
        out_lines.append(line)
    return "\n".join(out_lines)


class TextToSpeech:
    def __init__(self, voice_name: str, models_dir: str, speaking_rate: float = 1.0):
        onnx_path = f"{models_dir}/{voice_name}.onnx"
        config_path = f"{models_dir}/{voice_name}.onnx.json"
        self.voice = PiperVoice.load(onnx_path, config_path=config_path)
        self.speaking_rate = speaking_rate

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        text = _cap_bullet_list_for_speech(text)
        text = _cap_comma_list_for_speech(text)
        text = _truncate_for_speech(text)
        log.info("Speaking: %r", text)
        spoken_text = _respell_for_pronunciation(_strip_markdown_for_speech(text))
        syn_config = SynthesisConfig(length_scale=1.0 / self.speaking_rate)
        chunks = list(self.voice.synthesize(spoken_text, syn_config=syn_config))
        if not chunks:
            return
        audio = np.concatenate(
            [np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16) for chunk in chunks]
        )
        sd.play(audio, samplerate=chunks[0].sample_rate)
        sd.wait()
