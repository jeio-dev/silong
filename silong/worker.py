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
from typing import Callable, Optional

from silong import db, rules
from silong.db import PathLike
from silong.extract import ExtractionError

logger = logging.getLogger(__name__)

UNEXPECTED_ERROR = "Unexpected processing error; retry or complete by hand"
# Fallback poll interval; receipt and retry wake the worker immediately.
IDLE_WAIT = 2.0
JOIN_TIMEOUT = 5.0


class Worker:
    """Run `extract(text) -> fields` over queued reports in `db_path`.

    `warm_up()`, if given, is called once when the thread starts; any failure
    is only logged.
    """

    def __init__(
        self,
        db_path: PathLike,
        extract: Callable[[str], dict],
        warm_up: Optional[Callable[[], None]] = None,
        *,
        idle_wait: float = IDLE_WAIT,
    ):
        self.db_path = db_path
        self.extract = extract
        self.warm_up = warm_up
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

    def _process(self, row: dict) -> None:
        report_id = row["id"]
        started = time.monotonic()
        try:
            fields = self.extract(row["raw_text"])
            llm_ms = round((time.monotonic() - started) * 1000)
            urgency, reason = rules.classify(fields)
        except ExtractionError as exc:
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
