import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

import silong.__main__ as launcher
from silong import db, rules
from silong.app import create_app
from silong.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent

# Synthetic reports only.
TEXT_A = "Lola ko sa Room 3, hindi makalakad, wala nang gatas ang baby."
TEXT_B = "Kailangan ng tubig sa Room 5, tatlong bata."


def _no_network(*args, **kwargs):
    raise AssertionError("request handling must not open network connections")


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.status, response.read()


def _post_report(port, client_id, text):
    body = urllib.parse.urlencode({"client_id": client_id, "text": text}).encode()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/reports", body, timeout=5) as response:
        return json.load(response)


@contextmanager
def _running_hub(db_path, port, **extra_env):
    """Run a real `python -m silong` until it serves, then stop it on exit.

    The yielded object's `output` holds the server log once the block ends.
    Inherited SILONG_MODEL and OLLAMA_HOST are dropped, so a hub only reaches
    a model when the test passes one in `extra_env`.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("SILONG_MODEL", "OLLAMA_HOST")}
    env.update(SILONG_PORT=str(port), SILONG_DB=str(db_path), **extra_env)
    proc = subprocess.Popen(
        [sys.executable, "-m", "silong"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    hub = SimpleNamespace(proc=proc, output="")
    try:
        deadline = time.monotonic() + 15
        while proc.poll() is None and time.monotonic() < deadline:
            try:
                _get(port, "/api/reports")
                break
            except OSError:
                time.sleep(0.1)
        yield hub
    finally:
        proc.terminate()
        hub.output, _ = proc.communicate(timeout=15)


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "reports.db"
        # Any outbound connection (Ollama, Whisper downloads) fails the test.
        for name in ("connect", "connect_ex"):
            patcher = mock.patch.object(socket.socket, name, _no_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = self.open_client()

    def open_client(self, model=None, **app_options):
        """Open the app; no worker runs unless a test passes a model (with a fake `extract`)."""
        config = Config(model=model, whisper="small", port=8000, db_path=self.path)
        client = TestClient(create_app(config, **app_options))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def post(self, client=None, **fields):
        # Send as multipart, as the phone form does.
        files = {name: (None, value) for name, value in fields.items()}
        return (client or self.client).post("/api/reports", files=files)

    def row_count(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]


class ReceiptTest(ApiTestCase):
    def test_valid_text_is_acknowledged_after_storage(self):
        client_id = str(uuid.uuid4())
        response = self.post(client_id=client_id, text=TEXT_A, reporter="Ana")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"id", "status"})
        self.assertEqual(body["status"], "queued")
        stored = db.list_reports(self.path)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], body["id"])
        self.assertEqual(stored[0]["client_id"], client_id)
        self.assertEqual(stored[0]["raw_text"], TEXT_A)
        self.assertEqual(stored[0]["reporter"], "Ana")

    def test_original_text_is_stored_unmodified(self):
        text = "  <script>alert(1)</script>\n" + TEXT_A + "  "
        self.post(client_id=str(uuid.uuid4()), text=text)
        self.assertEqual(db.list_reports(self.path)[0]["raw_text"], text)

    def test_blank_reporter_is_stored_as_absent(self):
        self.post(client_id=str(uuid.uuid4()), text=TEXT_A, reporter="   ")
        self.assertIsNone(db.list_reports(self.path)[0]["reporter"])

    def test_url_encoded_form_is_accepted(self):
        response = self.client.post(
            "/api/reports", data={"client_id": str(uuid.uuid4()), "text": TEXT_A}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.row_count(), 1)

    def test_new_uuids_receive_distinct_ids(self):
        ids = [self.post(client_id=str(uuid.uuid4()), text=TEXT_A).json()["id"] for _ in range(3)]
        self.assertEqual(len(set(ids)), 3)

    def test_repeated_uuid_returns_original_report(self):
        client_id = str(uuid.uuid4())
        first = self.post(client_id=client_id, text=TEXT_A, reporter="Ana").json()
        second = self.post(client_id=client_id, text=TEXT_B, reporter="Ben")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first)
        stored = db.list_reports(self.path)
        self.assertEqual(len(stored), 1)
        self.assertEqual((stored[0]["raw_text"], stored[0]["reporter"]), (TEXT_A, "Ana"))

    def test_uuid_case_does_not_create_a_duplicate(self):
        client_id = str(uuid.uuid4())
        first = self.post(client_id=client_id, text=TEXT_A).json()
        second = self.post(client_id=client_id.upper(), text=TEXT_A).json()
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(self.row_count(), 1)

    def test_same_uuid_returns_same_id_after_app_is_recreated(self):
        client_id = str(uuid.uuid4())
        first = self.post(client_id=client_id, text=TEXT_A).json()
        self.client.__exit__(None, None, None)

        restarted = self.open_client()
        second = self.post(restarted, client_id=client_id, text=TEXT_B)

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first)
        listed = restarted.get("/api/reports").json()
        self.assertEqual([(r["id"], r["raw_text"]) for r in listed], [(first["id"], TEXT_A)])


class ValidationTest(ApiTestCase):
    def assertRejected(self, response):
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn("id", response.json())
        self.assertIsInstance(response.json()["detail"], str)
        self.assertEqual(self.row_count(), 0)

    def test_missing_or_blank_text_is_rejected(self):
        client_id = str(uuid.uuid4())
        for fields in [{}, {"text": ""}, {"text": " \n\t "}]:
            with self.subTest(fields=fields):
                self.assertRejected(self.post(client_id=client_id, **fields))

    def test_name_only_submission_is_rejected(self):
        self.assertRejected(self.post(client_id=str(uuid.uuid4()), reporter="Ana"))

    def test_missing_or_malformed_uuid_is_rejected(self):
        bad_ids = [
            None,
            "",
            "not-a-uuid",
            "123",
            str(uuid.uuid4()).replace("-", ""),
            "{" + str(uuid.uuid4()) + "}",
            str(uuid.uuid4()) + "\n",
            " " + str(uuid.uuid4()),
            "g" + str(uuid.uuid4())[1:],
        ]
        for client_id in bad_ids:
            with self.subTest(client_id=client_id):
                fields = {"text": TEXT_A}
                if client_id is not None:
                    fields["client_id"] = client_id
                self.assertRejected(self.post(**fields))

    def test_audio_attachment_is_rejected(self):
        response = self.client.post(
            "/api/reports",
            data={"client_id": str(uuid.uuid4()), "text": TEXT_A},
            files={"audio": ("report.webm", b"synthetic-bytes", "audio/webm")},
        )
        self.assertRejected(response)

    def test_audio_only_submission_is_rejected(self):
        response = self.client.post(
            "/api/reports",
            data={"client_id": str(uuid.uuid4())},
            files={"audio": ("report.webm", b"synthetic-bytes", "audio/webm")},
        )
        self.assertRejected(response)

    def test_text_sent_as_a_file_is_rejected(self):
        response = self.client.post(
            "/api/reports",
            data={"client_id": str(uuid.uuid4())},
            files={"text": ("report.txt", TEXT_A.encode(), "text/plain")},
        )
        self.assertRejected(response)


class StorageFailureTest(ApiTestCase):
    def assertNotAcknowledged(self, response):
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("id", response.json())
        self.assertIn("try again", response.json()["detail"])

    def test_database_error_returns_503_without_acknowledgement(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "CREATE TRIGGER fail_insert BEFORE INSERT ON reports"
                " BEGIN SELECT RAISE(ABORT, 'disk unavailable'); END"
            )
        with self.assertLogs("silong.app", "ERROR"):
            response = self.post(client_id=str(uuid.uuid4()), text=TEXT_A)
        self.assertNotAcknowledged(response)
        self.assertEqual(self.row_count(), 0)

    def test_locked_database_returns_503_without_acknowledgement(self):
        with closing(db.connect(self.path)) as holder, mock.patch.object(db, "BUSY_TIMEOUT", 0.1):
            holder.execute("BEGIN EXCLUSIVE")
            with self.assertLogs("silong.app", "ERROR"):
                response = self.post(client_id=str(uuid.uuid4()), text=TEXT_A)
            holder.execute("ROLLBACK")
        self.assertNotAcknowledged(response)
        self.assertEqual(self.row_count(), 0)

    def test_list_failure_returns_503(self):
        with mock.patch.object(db, "list_reports", side_effect=sqlite3.OperationalError("io")):
            with self.assertLogs("silong.app", "ERROR"):
                response = self.client.get("/api/reports")
        self.assertNotAcknowledged(response)


class ListTest(ApiTestCase):
    def test_empty_list(self):
        response = self.client.get("/api/reports")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_lists_originals_in_explicit_unclassified_state(self):
        client_id = str(uuid.uuid4())
        report_id = self.post(client_id=client_id, text=TEXT_A, reporter="Ana").json()["id"]

        [report] = self.client.get("/api/reports").json()

        self.assertEqual(report["id"], report_id)
        self.assertEqual(report["client_id"], client_id)
        self.assertEqual(report["raw_text"], TEXT_A)
        self.assertEqual(report["reporter"], "Ana")
        self.assertEqual(report["status"], "queued")
        self.assertTrue(report["created_at"].endswith("+00:00"))
        for key in ("fields", "urgency", "urgency_reason", "asr_ms", "llm_ms", "error", "transcript"):
            with self.subTest(key=key):
                self.assertIn(key, report)
                self.assertIsNone(report[key])
        self.assertIs(report["edited"], False)
        self.assertIs(report["resolved"], False)
        self.assertNotIn("fields_json", report)
        self.assertNotIn("audio_path", report)

    def test_stored_fields_are_returned_as_json(self):
        report_id = self.post(client_id=str(uuid.uuid4()), text=TEXT_A).json()["id"]
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "UPDATE reports SET fields_json = ?, resolved = 1 WHERE id = ?",
                ('{"headcount": 3}', report_id),
            )
        [report] = self.client.get("/api/reports").json()
        self.assertEqual(report["fields"], {"headcount": 3})
        self.assertIs(report["resolved"], True)

    def test_unclassified_reports_oldest_receipt_first(self):
        ids = [self.post(client_id=str(uuid.uuid4()), text=t).json()["id"] for t in (TEXT_A, TEXT_B)]
        listed = self.client.get("/api/reports").json()
        self.assertEqual([r["id"] for r in listed], ids)


# Synthetic staff-entered fields for a Medium report.
MEDIUM_FIELDS = {
    "location": "Room 5",
    "in_center": True,
    "on_behalf": False,
    "headcount": 3,
    "vulnerable": [],
    "injured": False,
    "injury_note": None,
    "needs": ["water"],
    "summary": "Family in Room 5 needs drinking water.",
}


class StaffApiTestCase(ApiTestCase):
    def new_report(self, text=TEXT_A, client=None):
        return self.post(client, client_id=str(uuid.uuid4()), text=text).json()["id"]

    def patch(self, report_id, body=None, client=None, **kwargs):
        if body is not None:
            kwargs["json"] = body
        return (client or self.client).patch(f"/api/reports/{report_id}", **kwargs)

    def retry(self, report_id, client=None):
        return (client or self.client).post(f"/api/reports/{report_id}/retry")

    def listed(self, report_id, client=None):
        return next(r for r in (client or self.client).get("/api/reports").json() if r["id"] == report_id)

    def set_columns(self, report_id, **columns):
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(f"UPDATE reports SET {assignments} WHERE id = ?", (*columns.values(), report_id))

    def assertProblem(self, response, status, *phrases):
        self.assertEqual(response.status_code, status, response.text)
        detail = response.json()["detail"]
        self.assertIsInstance(detail, str)
        for phrase in phrases:
            self.assertIn(phrase, detail)


class StaffEditTest(StaffApiTestCase):
    def test_edit_recomputes_urgency_and_marks_edited_and_done(self):
        report_id = self.new_report()

        medium = self.patch(report_id, {"fields": MEDIUM_FIELDS})
        self.assertEqual(medium.status_code, 200, medium.text)
        view = medium.json()
        self.assertEqual((view["urgency"], view["urgency_reason"]), ("medium", "Medium: needs water"))
        self.assertEqual((view["status"], view["edited"], view["error"]), ("done", True, None))
        self.assertEqual(view["fields"], MEDIUM_FIELDS)
        self.assertEqual(view["raw_text"], TEXT_A)

        injured = self.patch(report_id, {"fields": dict(MEDIUM_FIELDS, injured=True)}).json()
        self.assertEqual(
            (injured["urgency"], injured["urgency_reason"]), ("critical", "Critical: injury reported")
        )

        rescue = dict(MEDIUM_FIELDS, needs=["rescue"])
        inside = self.patch(report_id, {"fields": rescue}).json()
        self.assertEqual(inside["urgency_reason"], "Critical: rescue needed")
        outside = self.patch(report_id, {"fields": dict(rescue, in_center=False)}).json()
        self.assertEqual(
            (outside["urgency"], outside["urgency_reason"]),
            ("critical", "Critical: rescue needed outside center"),
        )
        self.assertEqual(self.listed(report_id), outside)

    def test_edit_stores_cleaned_fields(self):
        report_id = self.new_report()
        messy = dict(MEDIUM_FIELDS, location="  ", summary=None, needs=["water", "food", "water"])
        view = self.patch(report_id, {"fields": messy}).json()
        self.assertEqual(
            (view["fields"]["location"], view["fields"]["summary"], view["fields"]["needs"]),
            (None, "", ["food", "water"]),
        )

    def test_edit_survives_app_restart(self):
        report_id = self.new_report()
        saved = self.patch(report_id, {"fields": dict(MEDIUM_FIELDS, injured=True)}).json()
        self.client.__exit__(None, None, None)

        client = self.open_client()
        self.assertEqual(self.listed(report_id, client), saved)

    def test_failed_or_queued_report_can_be_completed_by_hand(self):
        failed = self.new_report(TEXT_A)
        self.set_columns(failed, status="failed", error="Ollama is not reachable")
        queued = self.new_report(TEXT_B)

        for report_id in (failed, queued):
            with self.subTest(report_id=report_id):
                view = self.patch(report_id, {"fields": MEDIUM_FIELDS}).json()
                self.assertEqual(
                    (view["status"], view["error"], view["edited"], view["urgency"]),
                    ("done", None, True, "medium"),
                )
        self.assertEqual(self.retry(failed).status_code, 409)

    def test_fields_and_resolved_together(self):
        report_id = self.new_report()
        view = self.patch(report_id, {"fields": MEDIUM_FIELDS, "resolved": True}).json()
        self.assertEqual((view["urgency"], view["resolved"]), ("medium", True))


class ResolveTest(StaffApiTestCase):
    def test_resolve_and_reopen_persist_and_move_between_sections(self):
        older = self.new_report(TEXT_A)
        newer = self.new_report(TEXT_B)
        for report_id in (older, newer):
            self.patch(report_id, {"fields": MEDIUM_FIELDS})

        resolved = self.patch(older, {"resolved": True})
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertIs(resolved.json()["resolved"], True)
        self.assertEqual(resolved.json()["urgency"], "medium")
        self.client.__exit__(None, None, None)

        client = self.open_client()
        listed = client.get("/api/reports").json()
        self.assertEqual([(r["id"], r["resolved"]) for r in listed], [(newer, False), (older, True)])

        reopened = self.patch(older, {"resolved": False}, client=client).json()
        self.assertIs(reopened["resolved"], False)
        self.assertEqual([r["id"] for r in client.get("/api/reports").json()], [older, newer])

    def test_any_report_can_be_resolved(self):
        report_id = self.new_report()
        self.set_columns(report_id, status="failed", error="Model reply did not match")
        view = self.patch(report_id, {"resolved": True}).json()
        self.assertEqual((view["resolved"], view["status"], view["edited"]), (True, "failed", False))


class RetryTest(StaffApiTestCase):
    def test_retry_requeues_failed_report_and_clears_error(self):
        report_id = self.new_report()
        self.set_columns(report_id, status="failed", error="Ollama is not reachable")

        response = self.retry(report_id)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["status"], response.json()["error"]), ("queued", None))
        self.assertEqual(self.listed(report_id), response.json())

    def test_retry_of_other_states_is_409_and_changes_nothing(self):
        for status in ("queued", "extracting", "done"):
            with self.subTest(status=status):
                report_id = self.new_report()
                self.set_columns(report_id, status=status)
                before = self.listed(report_id)
                self.assertProblem(self.retry(report_id), 409, "Only a failed report", status)
                self.assertEqual(self.listed(report_id), before)


class StaffValidationTest(StaffApiTestCase):
    def test_invalid_bodies_are_422_with_a_string_detail(self):
        report_id = self.new_report()
        over_limit = " ".join(["salita"] * 21)
        missing = {k: v for k, v in MEDIUM_FIELDS.items() if k != "summary"}
        cases = [
            ("not JSON", {"content": b"resolved=true"}, "JSON object"),
            ("empty body", {"content": b""}, "JSON object"),
            ("empty object", {"body": {}}, "JSON object"),
            ("list", {"body": [MEDIUM_FIELDS]}, "JSON object"),
            ("unknown key", {"body": {"urgency": "low"}}, "urgency"),
            ("resolved not bool", {"body": {"resolved": "yes"}}, "resolved"),
            ("fields not object", {"body": {"fields": None}}, "fields must be an object"),
            ("missing field", {"body": {"fields": missing}}, "summary"),
            ("extra field", {"body": {"fields": dict(MEDIUM_FIELDS, urgency="low")}}, "urgency"),
            ("headcount text", {"body": {"fields": dict(MEDIUM_FIELDS, headcount="3")}}, "headcount"),
            ("negative headcount", {"body": {"fields": dict(MEDIUM_FIELDS, headcount=-1)}}, "headcount"),
            ("bad need", {"body": {"fields": dict(MEDIUM_FIELDS, needs=["boat"])}}, "needs"),
            ("in_center text", {"body": {"fields": dict(MEDIUM_FIELDS, in_center="no")}}, "in_center"),
            ("long summary", {"body": {"fields": dict(MEDIUM_FIELDS, summary=over_limit)}}, "20 words"),
        ]
        before = self.listed(report_id)
        for name, request, phrase in cases:
            with self.subTest(name):
                body = request.pop("body", None)
                self.assertProblem(self.patch(report_id, body, **request), 422, phrase)
                request["body"] = body
        self.assertEqual(self.listed(report_id), before)

    def test_unknown_id_is_404(self):
        self.new_report()
        for path in ("999", "0", "-1", "99999999999999999999", "-9223372036854775809"):
            with self.subTest(path=path):
                self.assertProblem(self.patch(path, {"resolved": True}), 404, "No report")
                self.assertProblem(self.patch(path, {"fields": MEDIUM_FIELDS}), 404, "No report")
                self.assertProblem(self.retry(path), 404, "No report")

    def test_storage_error_is_503_and_changes_nothing(self):
        report_id = self.new_report()
        self.set_columns(report_id, status="failed", error="Ollama is not reachable")
        before = self.listed(report_id)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "CREATE TRIGGER fail_update BEFORE UPDATE ON reports"
                " BEGIN SELECT RAISE(ABORT, 'disk unavailable'); END"
            )
        for name, send in (
            ("fields", lambda: self.patch(report_id, {"fields": MEDIUM_FIELDS})),
            ("resolved", lambda: self.patch(report_id, {"resolved": True})),
            ("retry", lambda: self.retry(report_id)),
        ):
            with self.subTest(name):
                with self.assertLogs("silong.app", "ERROR"):
                    self.assertProblem(send(), 503, "try again")
        self.assertEqual(self.listed(report_id), before)


class BoardOrderTest(StaffApiTestCase):
    def test_list_order_follows_urgency_then_unclassified_then_resolved(self):
        def classify(report_id, resolved=None, **changes):
            body = {"fields": dict(MEDIUM_FIELDS, **changes)}
            if resolved is not None:
                body["resolved"] = resolved
            self.assertEqual(self.patch(report_id, body).status_code, 200)

        low = self.new_report()
        classify(low, needs=[])
        unclassified_old = self.new_report()
        critical_old = self.new_report()
        classify(critical_old, needs=["rescue"])
        high = self.new_report()
        classify(high, vulnerable=["infant"], needs=["milk_formula"])
        medium = self.new_report()
        classify(medium)
        critical_new = self.new_report()
        classify(critical_new, injured=True)
        resolved_old = self.new_report()
        classify(resolved_old, resolved=True, needs=["rescue"])
        unclassified_new = self.new_report()
        resolved_new = self.new_report()
        self.assertEqual(self.patch(resolved_new, {"resolved": True}).status_code, 200)

        listed = self.client.get("/api/reports").json()

        self.assertEqual(
            [r["id"] for r in listed],
            [
                critical_old,
                critical_new,
                high,
                medium,
                low,
                unclassified_old,
                unclassified_new,
                resolved_new,
                resolved_old,
            ],
        )


class PageTest(ApiTestCase):
    ASSETS = {
        "/static/report.js": "javascript",
        "/static/board.js": "javascript",
        "/static/style.css": "text/css",
    }

    def test_report_form_page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        page = response.text
        self.assertIn('<textarea id="text" name="text"', page)
        self.assertIn('<input id="reporter" name="reporter"', page)
        self.assertIn('<label for="text">', page)
        self.assertIn('<label for="reporter">', page)
        self.assertIn('src="/static/report.js"', page)
        self.assertIn('href="/static/style.css"', page)
        # Audio arrives in phase 04; the form offers no audio control yet.
        self.assertNotIn("audio", page.lower())
        self.assertNotIn('type="file"', page)

    def test_board_page(self):
        response = self.client.get("/board")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn('src="/static/board.js"', response.text)
        self.assertIn('href="/static/style.css"', response.text)

    def test_static_assets_are_served_locally(self):
        for path, content_type in self.ASSETS.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response.headers["content-type"])

    def test_missing_asset_and_traversal_are_not_served(self):
        self.assertEqual(self.client.get("/static/missing.js").status_code, 404)
        self.assertEqual(self.client.get("/static/../app.py").status_code, 404)
        self.assertEqual(self.client.get("/static/%2e%2e/app.py").status_code, 404)

    def test_pages_use_no_remote_assets_or_unsafe_rendering(self):
        sources = {path: self.client.get(path).text for path in ("/", "/board", *self.ASSETS)}
        for path, source in sources.items():
            with self.subTest(path=path):
                self.assertNotIn("http://", source)
                self.assertNotIn("https://", source)
                self.assertNotIn("//cdn", source)
                self.assertNotIn("innerHTML", source)
                self.assertNotIn("insertAdjacentHTML", source)
                self.assertNotIn("serviceWorker", source)
        # crypto.randomUUID needs a secure context; the hub serves plain HTTP.
        self.assertNotIn("randomUUID(", sources["/static/report.js"])
        self.assertIn("crypto.getRandomValues", sources["/static/report.js"])

    def test_board_polls_every_two_seconds(self):
        self.assertIn("const POLL_MS = 2000;", self.client.get("/static/board.js").text)

    def test_unclassified_status_labels_carry_no_urgency_word(self):
        script = self.client.get("/static/board.js").text
        block = re.search(r"const STATUS_LABELS = \{(.*?)\};", script, re.S)
        self.assertIsNotNone(block)
        labels = re.findall(r'"([^"]*)"', block.group(1))
        self.assertEqual(len(labels), 3)
        for label in labels:
            with self.subTest(label=label):
                self.assertNotRegex(label, r"(?i)\b(critical|high|medium|low)\b")

    def test_board_edit_form_matches_the_field_rules(self):
        script = self.client.get("/static/board.js").text
        for name, expected in (("VULNERABLE", rules.VULNERABLE), ("NEEDS", rules.NEEDS)):
            with self.subTest(name=name):
                block = re.search(rf"const {name} = \[(.*?)\];", script)
                self.assertIsNotNone(block)
                self.assertEqual(tuple(re.findall(r'"([^"]*)"', block.group(1))), expected)
        block = re.search(r"const BLANK_FIELDS = \{(.*?)\};", script, re.S)
        self.assertIsNotNone(block)
        blank = json.loads("{" + re.sub(r"(\w+):", r'"\1":', block.group(1)).rstrip().rstrip(",") + "}")
        self.assertEqual(rules.validate_fields(blank), blank)

    def test_board_has_three_sections_with_unclassified_first(self):
        page = self.client.get("/board").text
        positions = [page.find(f'<ol id="{name}"') for name in ("unclassified", "triaged", "resolved")]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))
        self.assertRegex(page, r'<details class="resolved-section">\s*<summary>')


class StartupTest(unittest.TestCase):
    def test_startup_creates_database_and_keeps_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reports.db"
            config = Config(model=None, whisper="off", port=8000, db_path=path)
            with TestClient(create_app(config)) as client:
                self.assertTrue(path.exists())
                client.post("/api/reports", data={"client_id": str(uuid.uuid4()), "text": TEXT_A})
            with TestClient(create_app(config)) as client:
                self.assertEqual(len(client.get("/api/reports").json()), 1)

    def test_app_does_not_load_model_or_transcription_clients(self):
        code = (
            "import sys, silong.app, silong.__main__; "
            "print(sorted(m for m in ('faster_whisper', 'httpx', 'qrcode', 'ollama') if m in sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "[]")


class LauncherTest(unittest.TestCase):
    def test_serves_on_all_interfaces_with_configured_port_in_one_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"SILONG_PORT": "8123", "SILONG_DB": str(Path(tmp) / "hub.db")}
            with mock.patch.dict("os.environ", env), mock.patch.object(launcher.uvicorn, "run") as run:
                self.assertEqual(launcher.main(), 0)

        run.assert_called_once()
        (app,), kwargs = run.call_args
        self.assertEqual(kwargs, {"host": "0.0.0.0", "port": 8123, "workers": 1})
        self.assertEqual(app.state.config.db_path, Path(tmp) / "hub.db")

    def test_real_launch_serves_in_one_process_despite_web_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = _free_port()
            with _running_hub(Path(tmp) / "hub.db", port, WEB_CONCURRENCY="2") as hub:
                status = _get(port, "/api/reports")[0]

        self.assertEqual(status, 200, hub.output)
        self.assertIn(f"Started server process [{hub.proc.pid}]", hub.output)
        self.assertIn(f"Uvicorn running on http://0.0.0.0:{port}", hub.output)

    def test_invalid_config_exits_with_message(self):
        with mock.patch.dict("os.environ", {"SILONG_PORT": "abc"}), mock.patch.object(
            launcher.uvicorn, "run"
        ) as run, mock.patch("sys.stderr") as stderr:
            self.assertEqual(launcher.main(), 2)
        run.assert_not_called()
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("SILONG_PORT", written)


class RestartIntegrationTest(unittest.TestCase):
    def test_report_survives_hub_restart_and_repeat_keeps_one_row(self):
        client_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hub.db"
            port = _free_port()
            with _running_hub(path, port) as hub:
                first = _post_report(port, client_id, TEXT_A)
                board_status, board_page = _get(port, "/board")
                listed = json.loads(_get(port, "/api/reports")[1])
            self.assertIn("Application shutdown complete", hub.output)

            with _running_hub(path, port) as hub:
                repeat = _post_report(port, client_id, TEXT_B)
                after = json.loads(_get(port, "/api/reports")[1])
            self.assertIn("Application shutdown complete", hub.output)

        self.assertEqual(first, {"id": first["id"], "status": "queued"})
        self.assertEqual(board_status, 200)
        self.assertIn(b"board.js", board_page)
        self.assertEqual([(r["id"], r["raw_text"]) for r in listed], [(first["id"], TEXT_A)])
        self.assertEqual(repeat, first)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0], listed[0])


if __name__ == "__main__":
    unittest.main()
