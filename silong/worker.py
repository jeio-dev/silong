"""Background worker: extracts queued reports one at a time, oldest first.

One daemon thread in the hub process. The reports table is the queue: a report
is claimed by moving it to `extracting`, then saved as `done` with fields and a
code-computed urgency, or marked `failed` with a staff-readable message. A
result is discarded if staff edited or changed the report meanwhile.
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from silong import db, rules
from silong.db import PathLike
from silong.extract import ExtractionError
from silong.transcribe import TranscriptionError

logger = logging.getLogger(__name__)

UNEXPECTED_ERROR = "Unexpected processing error; retry or complete by hand"
NO_TRANSCRIBER = "Audio transcription is unavailable on this hub; listen on the hub or complete by hand"
NO_SPEECH = "No speech was heard in the audio; listen on the hub or complete by hand"
# Fallback poll interval; receipt and retry wake the worker immediately.
IDLE_WAIT = 2.0
JOIN_TIMEOUT = 5.0


class Worker:
    """Run `extract(text) -> fields` over queued reports in `db_path`.

    `warm_up()`, if given, is called once when the thread starts; any failure
    is only logged. A report with audio is first transcribed with
    `transcribe(path) -> text` (audio files live in `audio_dir`); typed text and
    transcript are then joined for one extraction.
    """

    def __init__(
        self,
        db_path: PathLike,
        extract: Callable[[str], dict],
        warm_up: Optional[Callable[[], None]] = None,
        *,
        idle_wait: float = IDLE_WAIT,
        transcribe: Optional[Callable[[str], str]] = None,
        audio_dir: Optional[PathLike] = None,
    ):
        self.db_path = db_path
        self.extract = extract
        self.warm_up = warm_up
        self.transcribe = transcribe
        self.audio_dir = audio_dir
        self.idle_wait = idle_wait
        self._wake = threading.Event()
        self._stop = threading.Event()
        # Daemon, so a stuck Ollama request cannot block process exit.
        self._thread = threading.Thread(target=self._run, name="silong-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wake(self) -> None:
        """Check the queue now instead of after the idle wait."""
        self._wake.set()

    def stop(self, timeout: float = JOIN_TIMEOUT) -> bool:
        """Ask the thread to exit and wait up to `timeout` seconds; return whether it did."""
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        if self._thread.is_alive():
            logger.warning("Worker did not stop within %.0f s; leaving it to exit with the process", timeout)
            return False
        return True

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _idle(self) -> None:
        self._wake.wait(self.idle_wait)
        self._wake.clear()

    def _run(self) -> None:
        if self.warm_up is not None:
            try:
                self.warm_up()
            except Exception:
                logger.exception("Model warm-up failed; reports will still be attempted")
        while not self._stop.is_set():
            try:
                row = db.claim_next(self.db_path)
            except sqlite3.Error:
                logger.exception("Could not claim the next report; will try again")
                self._idle()
                continue
            if row is None:
                self._idle()
                continue
            self._process(row)

    def _input_text(self, row: dict) -> Optional[str]:
        """The text to extract from: typed text plus any transcript, transcribing
        (and storing the transcript) first when needed. None means the report
        was marked failed or the worker is stopping."""
        text = (row["raw_text"] or "").strip()
        if not row["audio_path"]:
            return row["raw_text"]
        transcript = row["transcript"]
        if transcript is None:
            if self.transcribe is None or self.audio_dir is None:
                self._retrying_write(db.mark_failed, row["id"], NO_TRANSCRIBER)
                return None
            started = time.monotonic()
            transcript = self.transcribe(str(Path(self.audio_dir) / row["audio_path"]))
            asr_ms = round((time.monotonic() - started) * 1000)
            if not self._retrying_write(db.save_transcript, row["id"], transcript, asr_ms):
                return None
        combined = "\n".join(part for part in (text, transcript.strip()) if part)
        if not combined:
            self._retrying_write(db.mark_failed, row["id"], NO_SPEECH)
            return None
        return combined

    def _process(self, row: dict) -> None:
        report_id = row["id"]
        try:
            text = self._input_text(row)
            if text is None:
                return
            started = time.monotonic()
            fields = self.extract(text)
            llm_ms = round((time.monotonic() - started) * 1000)
            urgency, reason = rules.classify(fields)
        except (ExtractionError, TranscriptionError) as exc:
            logger.warning("Report %s failed extraction: %s", report_id, exc)
            self._retrying_write(db.mark_failed, report_id, str(exc))
            return
        except Exception:
            logger.exception("Unexpected error while processing report %s", report_id)
            self._retrying_write(db.mark_failed, report_id, UNEXPECTED_ERROR)
            return
        saved = self._retrying_write(db.save_extraction, report_id, fields, urgency, reason, llm_ms)
        if saved is False:
            logger.info("Discarded result for report %s: changed while extracting", report_id)

    def _retrying_write(self, write: Callable, *args) -> Optional[bool]:
        """Run a guarded write, retrying the same write on database errors until
        it succeeds or the worker stops (returns None then; restart recovery
        requeues the report)."""
        while True:
            try:
                return write(self.db_path, *args)
            except sqlite3.Error:
                logger.exception("Could not store the result for report %s; will try again", args[0])
                if self._stop.wait(self.idle_wait):
                    return None
