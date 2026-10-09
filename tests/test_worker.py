import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest import mock

from silong import db, rules
from silong.extract import ExtractionError
from silong.worker import UNEXPECTED_ERROR, Worker
from tests.test_api import REPO_ROOT, ApiTestCase, _free_port, _get, _post_report, _running_hub

# Synthetic reports only.
TEXT_A = "Lola ko sa Room 3, hindi makalakad, wala nang gatas ang baby."
TEXT_B = "Kailangan ng tubig sa Room 5, tatlong bata."
TEXT_C = "Na-stranded kami sa bubong sa Purok 4, lima kami, kailangan ng rescue."

FIELDS = rules.validate_fields(
    {
        "location": "Room 3",
        "in_center": True,
        "on_behalf": True,
        "headcount": 2,
        "vulnerable": ["senior", "infant"],
        "injured": False,
        "injury_note": None,
        "needs": ["milk_formula"],
        "summary": "Senior cannot walk; infant has no milk formula.",
    }
)
EDITED_FIELDS = dict(FIELDS, needs=["rescue"], summary="Edited by staff.")


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def fake_extract(text):
    return FIELDS


class HeldExtractor:
    """A fake extractor that blocks until released, recording what it was given."""

    def __init__(self, fields=FIELDS):
        self.fields = fields
        self.started = threading.Event()
        self.release = threading.Event()
        self.texts = []

    def __call__(self, text):
        self.texts.append(text)
        self.started.set()
        if not self.release.wait(10):
            raise AssertionError("held extractor was never released")
        return self.fields


class QueueDbTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "reports.db"
        db.init_db(self.path)

    def insert(self, text=TEXT_A):
        return db.insert_report(self.path, str(uuid.uuid4()), text)[0]["id"]

    def set(self, report_id, **columns):
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(f"UPDATE reports SET {assignments} WHERE id = ?", (*columns.values(), report_id))

    def row(self, report_id):
        return next(r for r in db.list_reports(self.path) if r["id"] == report_id)


class QueueDbTest(QueueDbTestCase):
    def test_claim_takes_oldest_queued_and_clears_error(self):
        first, second = self.insert(TEXT_A), self.insert(TEXT_B)
        self.set(first, error="old message")

        claimed = db.claim_next(self.path)

        self.assertEqual(claimed["id"], first)
        self.assertEqual(claimed["status"], "extracting")
        self.assertIsNone(claimed["error"])
        self.assertEqual(self.row(second)["status"], "queued")
        self.assertEqual(db.claim_next(self.path)["id"], second)
        self.assertIsNone(db.claim_next(self.path))

    def test_claim_skips_reports_not_queued(self):
        for status in ("done", "failed", "extracting"):
            self.set(self.insert(), status=status)
        self.assertIsNone(db.claim_next(self.path))

    def test_save_stores_result_only_while_extracting_and_unedited(self):
        saved, edited, changed = self.insert(), self.insert(), self.insert()
        for report_id in (saved, edited, changed):
            self.set(report_id, status="extracting")
        self.set(edited, edited=1, fields_json=json.dumps(EDITED_FIELDS))
        self.set(changed, status="failed", error="Ollama is not reachable")

        self.assertTrue(db.save_extraction(self.path, saved, FIELDS, "high", "High: infant", 1234))
        self.assertFalse(db.save_extraction(self.path, edited, FIELDS, "high", "High: infant", 1))
        self.assertFalse(db.save_extraction(self.path, changed, FIELDS, "high", "High: infant", 1))

        row = self.row(saved)
        self.assertEqual(
            (row["status"], json.loads(row["fields_json"]), row["urgency"], row["urgency_reason"], row["llm_ms"]),
            ("done", FIELDS, "high", "High: infant", 1234),
        )
        self.assertEqual(json.loads(self.row(edited)["fields_json"]), EDITED_FIELDS)
        self.assertEqual(self.row(changed)["status"], "failed")

    def test_mark_failed_only_while_extracting(self):
        extracting, done = self.insert(), self.insert()
        self.set(extracting, status="extracting")
        self.set(done, status="done", edited=1)

        self.assertTrue(db.mark_failed(self.path, extracting, "Model reply was not valid JSON"))
        self.assertFalse(db.mark_failed(self.path, done, "Model reply was not valid JSON"))

        self.assertEqual(
            (self.row(extracting)["status"], self.row(extracting)["error"]),
            ("failed", "Model reply was not valid JSON"),
        )
        self.assertEqual((self.row(done)["status"], self.row(done)["error"]), ("done", None))

    def test_recovery_requeues_interrupted_reports_and_keeps_everything_else(self):
        extracting, transcribing, failed, done = (self.insert() for _ in range(4))
        self.set(extracting, status="extracting", resolved=1, fields_json=json.dumps(FIELDS), llm_ms=5)
        self.set(transcribing, status="transcribing", asr_ms=7)
        self.set(failed, status="failed", error="Ollama is not reachable")
        self.set(done, status="done", edited=1, fields_json=json.dumps(EDITED_FIELDS))
        before = {r["id"]: r for r in db.list_reports(self.path)}

        self.assertEqual(db.recover_interrupted(self.path), 2)

        after = {r["id"]: r for r in db.list_reports(self.path)}
        for report_id in (extracting, transcribing):
            self.assertEqual(after[report_id], dict(before[report_id], status="queued"))
        for report_id in (failed, done):
            self.assertEqual(after[report_id], before[report_id])


class WorkerTest(QueueDbTestCase):
    def start(self, extract, warm_up=None):
        worker = Worker(self.path, extract, warm_up, idle_wait=0.05)
        worker.start()
        self.addCleanup(worker.stop)
        return worker

    def wait_for_status(self, report_id, *statuses):
        self.assertTrue(
            wait_until(lambda: self.row(report_id)["status"] in statuses),
            f"report {report_id} stayed {self.row(report_id)['status']}",
        )
        return self.row(report_id)

    def test_processes_oldest_first_one_at_a_time_with_measured_time(self):
        ids = [self.insert(text) for text in (TEXT_A, TEXT_B, TEXT_C)]
        seen = []
        active = []

        def extract(text):
            active.append(text)
            # The report being extracted is the only one in that state.
            seen.append((text, [r["status"] for r in db.list_reports(self.path)], len(active)))
            time.sleep(0.05)
            active.remove(text)
            return FIELDS

        self.start(extract)
        for report_id in ids:
            self.wait_for_status(report_id, "done")

        self.assertEqual([text for text, _, _ in seen], [TEXT_A, TEXT_B, TEXT_C])
        self.assertEqual(
            [statuses for _, statuses, _ in seen],
            [["extracting", "queued", "queued"], ["done", "extracting", "queued"], ["done", "done", "extracting"]],
        )
        self.assertEqual({concurrent for _, _, concurrent in seen}, {1})
        urgency, reason = rules.classify(FIELDS)
        for report_id in ids:
            row = self.row(report_id)
            self.assertEqual(json.loads(row["fields_json"]), FIELDS)
            self.assertEqual((row["urgency"], row["urgency_reason"], row["error"]), (urgency, reason, None))
            self.assertIsInstance(row["llm_ms"], int)
            self.assertGreaterEqual(row["llm_ms"], 50)
            self.assertEqual(row["edited"], 0)

    def test_extraction_error_marks_failed_and_keeps_original(self):
        report_id = self.insert(TEXT_A)

        def extract(text):
            raise ExtractionError("Ollama is not reachable at http://127.0.0.1:11434")

        with self.assertLogs("silong.worker", "WARNING"):
            self.start(extract)
            row = self.wait_for_status(report_id, "failed")

        self.assertEqual(row["error"], "Ollama is not reachable at http://127.0.0.1:11434")
        self.assertEqual(row["raw_text"], TEXT_A)
        for column in ("fields_json", "urgency", "urgency_reason", "llm_ms"):
            self.assertIsNone(row[column])

    def test_unexpected_error_marks_failed_and_next_report_is_processed(self):
        broken, fine = self.insert(TEXT_A), self.insert(TEXT_B)

        def extract(text):
            if text == TEXT_A:
                raise RuntimeError("synthetic bug")
            return FIELDS

        with self.assertLogs("silong.worker", "ERROR") as logs:
            self.start(extract)
            self.wait_for_status(fine, "done")

        self.assertEqual((self.row(broken)["status"], self.row(broken)["error"]), ("failed", UNEXPECTED_ERROR))
        self.assertIn("synthetic bug", "\n".join(logs.output))

    def test_result_after_staff_edit_is_discarded(self):
        report_id = self.insert(TEXT_A)
        held = HeldExtractor()
        self.start(held)
        self.assertTrue(held.started.wait(5))
        self.set(report_id, status="done", edited=1, fields_json=json.dumps(EDITED_FIELDS),
                 urgency="critical", urgency_reason="Critical: rescue needed")

        with self.assertLogs("silong.worker", "INFO") as logs:
            held.release.set()
            self.assertTrue(wait_until(lambda: any("Discarded" in line for line in logs.output)))

        row = self.row(report_id)
        self.assertEqual(json.loads(row["fields_json"]), EDITED_FIELDS)
        self.assertEqual((row["status"], row["urgency"], row["llm_ms"]), ("done", "critical", None))

    def test_claim_database_error_does_not_stop_worker(self):
        report_id = self.insert()
        real_claim = db.claim_next
        calls = []

        def flaky_claim(path):
            calls.append(path)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_claim(path)

        with mock.patch.object(db, "claim_next", flaky_claim), self.assertLogs("silong.worker", "ERROR"):
            self.start(fake_extract)
            self.wait_for_status(report_id, "done")
        self.assertGreaterEqual(len(calls), 2)

    def test_save_failure_is_retried_and_result_stored(self):
        report_id = self.insert()
        real_save = db.save_extraction
        attempts = []

        def flaky_save(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return real_save(*args)

        with mock.patch.object(db, "save_extraction", flaky_save), self.assertLogs("silong.worker", "ERROR"):
            self.start(fake_extract)
            row = self.wait_for_status(report_id, "done")

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(json.loads(row["fields_json"]), FIELDS)

    def test_warm_up_runs_once_and_its_failure_is_only_logged(self):
        report_id = self.insert()
        warm_up = mock.Mock(side_effect=ExtractionError("Ollama is not reachable"))

        with self.assertLogs("silong.worker", "ERROR"):
            self.start(fake_extract, warm_up)
            self.wait_for_status(report_id, "done")
        warm_up.assert_called_once_with()

    def test_wake_skips_the_idle_wait(self):
        worker = Worker(self.path, fake_extract, idle_wait=30)
        worker.start()
        self.addCleanup(worker.stop)
        time.sleep(0.1)  # Let the worker find the queue empty and start waiting.
        report_id = self.insert()
        worker.wake()
        self.wait_for_status(report_id, "done")

    def test_stop_joins_the_thread(self):
        worker = self.start(fake_extract)
        self.assertTrue(worker.is_alive())
        self.assertTrue(worker.stop())
        self.assertFalse(worker.is_alive())


class AppWorkerTest(ApiTestCase):
    def open_with(self, extract, **options):
        return self.open_client(model="test-model", extract=extract, **options)

    def post_text(self, client, text=TEXT_A):
        files = {"client_id": (None, str(uuid.uuid4())), "text": (None, text)}
        return client.post("/api/reports", files=files)

    def listed(self, client, report_id):
        return next(r for r in client.get("/api/reports").json() if r["id"] == report_id)

    def test_no_model_starts_no_worker(self):
        self.assertIsNone(self.client.app.state.worker)
        self.assertNotIn("silong-worker", [t.name for t in threading.enumerate()])
        report_id = self.post(client_id=str(uuid.uuid4()), text=TEXT_A).json()["id"]
        time.sleep(0.2)
        self.assertEqual(self.listed(self.client, report_id)["status"], "queued")

    def test_receipt_is_prompt_while_extraction_is_held(self):
        held = HeldExtractor()
        client = self.open_with(held)
        try:
            first = self.post_text(client, TEXT_A).json()["id"]
            self.assertTrue(held.started.wait(5))
            started = time.monotonic()
            response = self.post_text(client, TEXT_B)
            elapsed = time.monotonic() - started

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "queued")
            self.assertLess(elapsed, 1.0)
            self.assertEqual(self.listed(client, first)["status"], "extracting")
            self.assertEqual(self.listed(client, response.json()["id"])["status"], "queued")
        finally:
            held.release.set()
        self.assertTrue(wait_until(lambda: self.listed(client, first)["status"] == "done"))
        view = self.listed(client, first)
        self.assertEqual(view["fields"], FIELDS)
        self.assertEqual((view["urgency"], view["urgency_reason"]), rules.classify(FIELDS))
        self.assertIsInstance(view["llm_ms"], int)

    def test_injected_extract_skips_default_warm_up_and_shutdown_joins(self):
        client = self.open_with(fake_extract)
        worker = client.app.state.worker
        self.assertIsNone(worker.warm_up)
        self.assertTrue(worker.is_alive())
        client.__exit__(None, None, None)
        self.assertFalse(worker.is_alive())

    def test_restart_recovers_interrupted_reports_and_processes_them(self):
        ids = [self.post(client_id=str(uuid.uuid4()), text=t).json()["id"] for t in (TEXT_A, TEXT_B)]
        self.client.__exit__(None, None, None)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE reports SET status = 'extracting', resolved = 1 WHERE id = ?", (ids[0],))
            conn.execute("UPDATE reports SET status = 'transcribing' WHERE id = ?", (ids[1],))

        client = self.open_with(fake_extract)

        for report_id in ids:
            self.assertTrue(wait_until(lambda: self.listed(client, report_id)["status"] == "done"))
        first = self.listed(client, ids[0])
        self.assertIs(first["resolved"], True)
        self.assertEqual((first["raw_text"], first["fields"]), (TEXT_A, FIELDS))

    def test_lifespan_without_model_imports_no_worker_or_client(self):
        code = (
            "import asyncio, sys, tempfile, pathlib\n"
            "from silong.app import create_app\n"
            "from silong.config import Config\n"
            "async def main():\n"
            "    with tempfile.TemporaryDirectory() as tmp:\n"
            "        app = create_app(Config(None, 'off', 8000, pathlib.Path(tmp) / 'r.db'))\n"
            "        async with app.router.lifespan_context(app):\n"
            "            print(app.state.worker, sorted(m for m in ('silong.worker', 'silong.extract', 'httpx')"
            " if m in sys.modules))\n"
            "asyncio.run(main())\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "None []")


class RealHubWorkerTest(unittest.TestCase):
    def test_unreachable_ollama_marks_report_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            port, closed_port = _free_port(), _free_port()
            env = {"SILONG_MODEL": "test-model", "OLLAMA_HOST": f"127.0.0.1:{closed_port}"}
            with _running_hub(Path(tmp) / "hub.db", port, **env) as hub:
                ack = _post_report(port, str(uuid.uuid4()), TEXT_A)

                def report():
                    return json.loads(_get(port, "/api/reports")[1])[0]

                failed = wait_until(lambda: report()["status"] == "failed", timeout=10)
                final = report()

        self.assertEqual(ack, {"id": ack["id"], "status": "queued"})
        self.assertTrue(failed, hub.output)
        self.assertIn("not reachable", final["error"])
        self.assertEqual(final["raw_text"], TEXT_A)
        self.assertIsNone(final["urgency"])


if __name__ == "__main__":
    unittest.main()
