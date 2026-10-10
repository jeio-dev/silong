"""Optional audio: durable upload, availability gating, and transcribe-then-extract."""

import threading
import time
import unittest
import uuid
from unittest import mock

from silong import db, transcribe
from silong.worker import NO_SPEECH, NO_TRANSCRIBER, Worker
from tests.test_api import ApiTestCase
from tests.test_worker import FIELDS, QueueDbTestCase, wait_until

# Synthetic content only; the "audio" bytes are never decoded by these tests.
AUDIO = b"synthetic-audio-bytes"
TRANSCRIPT = "Na-stranded kami sa bubong sa Purok 4, lima kami."
TEXT = "Room 3 po."


class Recorder:
    """A fake transcriber that records each path it was given."""

    def __init__(self, text=TRANSCRIPT):
        self.text = text
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        return self.text


class AudioApiTest(ApiTestCase):
    def post_audio(self, client, client_id=None, text=None, filename="note.m4a", data=AUDIO):
        files = {"client_id": (None, client_id or str(uuid.uuid4())), "audio": (filename, data, "audio/mp4")}
        if text is not None:
            files["text"] = (None, text)
        return client.post("/api/reports", files=files)

    def test_audio_rejected_and_status_off_when_no_transcriber(self):
        response = self.post_audio(self.client)
        self.assertEqual(response.status_code, 422)
        self.assertIn("type the report", response.json()["detail"])
        self.assertEqual(self.row_count(), 0)
        self.assertFalse(self.client.get("/api/status").json()["audio_available"])

    def test_audio_only_report_is_stored_before_receipt_and_deduplicated(self):
        client = self.open_client(transcribe=Recorder())
        self.assertTrue(client.get("/api/status").json()["audio_available"])
        client_id = str(uuid.uuid4())
        first = self.post_audio(client, client_id)
        self.assertEqual(first.status_code, 200, first.text)
        again = self.post_audio(client, client_id.upper(), data=b"other")
        self.assertEqual(again.json()["id"], first.json()["id"])
        self.assertEqual(self.row_count(), 1)
        row = db.list_reports(self.path)[0]
        self.assertEqual(row["raw_text"], "")
        self.assertEqual(row["audio_path"], client_id + ".m4a")
        self.assertEqual((self.path.parent / "audio" / row["audio_path"]).read_bytes(), AUDIO)
        self.assertEqual(list((self.path.parent / "audio").glob("*.part")), [])
        listed = client.get("/api/reports").json()[0]
        self.assertNotIn("audio_path", listed)

    def test_unknown_suffix_is_stored_as_audio_and_text_is_kept(self):
        client = self.open_client(transcribe=Recorder())
        client_id = str(uuid.uuid4())
        self.post_audio(client, client_id, text=TEXT, filename="../../evil.sh")
        row = db.list_reports(self.path)[0]
        self.assertEqual(row["audio_path"], client_id + ".audio")
        self.assertEqual(row["raw_text"], TEXT)

    def test_empty_and_oversized_audio_are_rejected(self):
        client = self.open_client(transcribe=Recorder())
        self.assertEqual(self.post_audio(client, data=b"").status_code, 422)
        with mock.patch("silong.app.MAX_AUDIO_BYTES", 4):
            self.assertEqual(self.post_audio(client).status_code, 413)
        self.assertEqual(self.row_count(), 0)

    def test_report_needs_text_or_audio(self):
        client = self.open_client(transcribe=Recorder())
        response = client.post("/api/reports", files={"client_id": (None, str(uuid.uuid4())), "text": (None, "  ")})
        self.assertEqual(response.status_code, 422)

    def test_audio_report_is_transcribed_and_extracted_once(self):
        seen = []

        def extract(text):
            seen.append(text)
            return FIELDS

        recorder = Recorder()
        client = self.open_client(model="test-model", extract=extract, transcribe=recorder)
        report_id = self.post_audio(client, text=TEXT).json()["id"]
        self.assertTrue(wait_until(lambda: db.list_reports(self.path)[0]["status"] == "done"))
        listed = client.get("/api/reports").json()[0]
        self.assertEqual(listed["id"], report_id)
        self.assertEqual(listed["transcript"], TRANSCRIPT)
        self.assertIsInstance(listed["asr_ms"], int)
        self.assertEqual(seen, [TEXT + "\n" + TRANSCRIPT])
        self.assertEqual(len(recorder.paths), 1)


class AudioWorkerTest(QueueDbTestCase):
    def insert(self, text="", audio_path="a.m4a", transcript=None):
        row, _ = db.insert_report(self.path, str(uuid.uuid4()), text, None, audio_path)
        if transcript is not None:
            self.set(row["id"], transcript=transcript)
        return row["id"]

    def run_worker(self, extract, transcribe_fn):
        worker = Worker(self.path, extract, idle_wait=0.05, transcribe=transcribe_fn, audio_dir=self.path.parent)
        worker.start()
        self.addCleanup(worker.stop)

    def status_of(self, report_id):
        return next(r for r in db.list_reports(self.path) if r["id"] == report_id)

    def test_existing_transcript_is_reused_on_retry(self):
        recorder = Recorder()
        report_id = self.insert(transcript="kept")
        seen = []
        self.run_worker(lambda text: seen.append(text) or FIELDS, recorder)
        self.assertTrue(wait_until(lambda: self.status_of(report_id)["status"] == "done"))
        self.assertEqual(recorder.paths, [])
        self.assertEqual(seen, ["kept"])

    def test_silent_audio_with_no_text_fails_for_staff(self):
        report_id = self.insert()
        self.run_worker(lambda text: FIELDS, Recorder(text="  "))
        self.assertTrue(wait_until(lambda: self.status_of(report_id)["status"] == "failed"))
        self.assertEqual(self.status_of(report_id)["error"], NO_SPEECH)

    def test_audio_without_transcriber_fails_for_staff(self):
        report_id = self.insert()
        self.run_worker(lambda text: FIELDS, None)
        self.assertTrue(wait_until(lambda: self.status_of(report_id)["status"] == "failed"))
        self.assertEqual(self.status_of(report_id)["error"], NO_TRANSCRIBER)

    def test_transcription_error_marks_failed(self):
        def broken(path):
            raise transcribe.TranscriptionError("Could not transcribe")

        report_id = self.insert()
        self.run_worker(lambda text: FIELDS, broken)
        self.assertTrue(wait_until(lambda: self.status_of(report_id)["status"] == "failed"))
        self.assertEqual(self.status_of(report_id)["error"], "Could not transcribe")


class LoadTest(unittest.TestCase):
    def test_off_loads_nothing(self):
        self.assertIsNone(transcribe.load("off"))

    def test_load_failure_returns_none(self):
        with mock.patch.dict("sys.modules", {"faster_whisper": None}):
            self.assertIsNone(transcribe.load("base"))


if __name__ == "__main__":
    unittest.main()
