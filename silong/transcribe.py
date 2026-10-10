"""Optional local speech-to-text with faster-whisper on the CPU (int8).

`load(size)` returns a `transcribe(path) -> text` callable, or None when
transcription is off or cannot load; the hub then keeps running text-only.
"""

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class TranscriptionError(Exception):
    """Audio could not be transcribed; the message is shown to staff."""


def load(size: str) -> Optional[Callable[[str], str]]:
    """Load Whisper `size` (`small`/`base`), or return None for `off` or any failure."""
    if size == "off":
        return None
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(size, device="cpu", compute_type="int8")
    except Exception:
        logger.exception("Whisper %s could not load; audio reports are disabled", size)
        return None

    def transcribe(path: str) -> str:
        try:
            # Taglish: let Whisper detect the language rather than forcing one.
            segments, _info = model.transcribe(path, beam_size=1, vad_filter=True)
            return " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as error:
            raise TranscriptionError("Could not transcribe the audio; listen on the hub or complete by hand") from error

    logger.info("Whisper %s loaded; audio reports accepted", size)
    return transcribe
