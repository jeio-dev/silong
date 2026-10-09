import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

import silong.__main__ as launcher
from silong import status
from silong.app import create_app
from silong.config import Config, ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent

TODAY_TEXT = "Lola ko sa Room 3, hindi makalakad, wala nang gatas ang baby."


def _row(**overrides):
    row = {
        "id": 1,
        "client_id": str(uuid.uuid4()),
        "created_at": "2026-10-09T08:00:00+00:00",
        "reporter": None,
        "raw_text": "synthetic",
        "status": "queued",
        "urgency": None,
        "resolved": 0,
    }
    row.update(overrides)
    return row


class NormalizeHubUrlTest(unittest.TestCase):
    def test_bare_host_and_default_port(self):
        self.assertEqual(status.normalize_hub_url("192.168.1.20"), "http://192.168.1.20/")
        self.assertEqual(status.normalize_hub_url("192.168.1.20:8000"), "http://192.168.1.20:8000/")
        self.assertEqual(
            status.normalize_hub_url("http://192.168.1.20:8000"), "http://192.168.1.20:8000/"
        )
        self.assertEqual(status.normalize_hub_url("  http://192.168.1.20:8000/ "), "http://192.168.1.20:8000/")
        # Default HTTP port is omitted in the normalized form.
        self.assertEqual(status.normalize_hub_url("http://10.0.0.5:80"), "http://10.0.0.5/")

    def test_rejected_values(self):
        for raw in [
            "wherever.local",
            "*.example.com",
            "127.0.0.1:8000",
            "localhost:8000",
            "0.0.0.0:8000",
            "[::1]:8000",
            "::",
            "https://192.168.1.20",
            "http://user:pass@192.168.1.20",
            "http://192.168.1.20/board",
            "http://192.168.1.20?q=1",
            "http://192.168.1.20#frag",
            "http://169.254.1.2",
            "http://224.0.0.1",
            "http://192.168.1.20:0",
            "http://192.168.1.20:99999",
            "",
            "   ",
        ]:
            with self.subTest(raw=raw):
                with self.assertRaises(status.PhoneUrlError):
                    status.normalize_hub_url(raw)


class HubUrlConfigTest(unittest.TestCase):
    def test_absent_override_defaults_to_none(self):
        self.assertIsNone(load_config({}).hub_url)

    def test_override_is_normalized(self):
        config = load_config({"SILONG_HUB_URL": "192.168.1.20"})
        self.assertEqual(config.hub_url, "http://192.168.1.20/")

    def test_invalid_override_raises_config_error(self):
        for raw in ("https://192.168.1.20", "localhost", "192.168.1.20/board"):
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError) as ctx:
                    load_config({"SILONG_HUB_URL": raw})
                self.assertIn("SILONG_HUB_URL", str(ctx.exception))


class ResolvePhoneUrlTest(unittest.TestCase):
    def setUp(self):
        self.config = Config(model=None, whisper="off", port=8000, db_path=Path("x.db"))

    def test_override_wins_verbatim(self):
        config = Config(model=None, whisper="off", port=8000, db_path=Path("x.db"), hub_url="http://192.168.1.20:9000/")
        self.assertEqual(status.resolve_phone_url(config, "localhost:8000"), "http://192.168.1.20:9000/")

    def test_eligible_host_header_is_used(self):
        self.assertEqual(
            status.resolve_phone_url(self.config, "192.168.1.20:8000"), "http://192.168.1.20:8000/"
        )
        self.assertEqual(status.resolve_phone_url(self.config, "10.0.0.5"), "http://10.0.0.5/")

    def test_non_eligible_hosts_give_no_url(self):
        for host in ("localhost:8000", "127.0.0.1:8000", "0.0.0.0:8000", "testserver", "", None, "hub.local"):
            with self.subTest(host=host):
                self.assertIsNone(status.resolve_phone_url(self.config, host or ""))


class LocalDateTest(unittest.TestCase):
    PH_TZ = timezone(timedelta(hours=8))

    def test_utc_receipt_crosses_local_midnight(self):
        # 16:30 UTC is 00:30 the next day in PH (+08), 15:30 UTC is the same day.
        self.assertEqual(
            status.local_date_of("2026-10-09T16:30:00+00:00", self.PH_TZ), "2026-10-10"
        )
        self.assertEqual(
            status.local_date_of("2026-10-09T15:30:00+00:00", self.PH_TZ), "2026-10-09"
        )

    def test_naive_timestamp_is_treated_as_utc(self):
        self.assertEqual(status.local_date_of("2026-10-09T16:30:00", self.PH_TZ), "2026-10-10")

    def test_today_local_date(self):
        now = datetime(2026, 10, 9, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(status.today_local_date(now), "2026-10-09")


class BuildStatusTest(unittest.TestCase):
    def test_counts_from_one_snapshot(self):
        rows = [
            _row(id=1, status="queued"),
            _row(id=2, status="extracting"),
            _row(id=3, status="done", urgency="critical"),
            _row(id=4, status="failed"),
            _row(id=5, status="done", urgency="high", resolved=1),
            _row(
                id=6,
                status="done",
                urgency="low",
                created_at="2026-10-07T08:00:00+00:00",
            ),
            _row(
                id=7,
                status="transcribing",
                created_at="2026-10-07T08:00:00+00:00",
            ),
        ]
        result = status.build_status(
            rows,
            hub_url="http://192.168.1.20:8000/",
            model="gemma4:e2b",
            model_state="loaded",
            model_checked_at="2026-10-09T09:00:00+00:00",
            worker_running=True,
            local_date="2026-10-09",
            snapshot_at="2026-10-09T09:01:00+00:00",
        )
        self.assertEqual(result["queue_length"], 1)
        self.assertEqual(result["processing_count"], 2)
        self.assertEqual(result["reports_today"], 5)
        self.assertEqual(
            result["urgency_counts"],
            {"critical": 1, "high": 0, "medium": 0, "low": 1},
        )
        self.assertEqual(result["local_date"], "2026-10-09")
        self.assertEqual(result["snapshot_at"], "2026-10-09T09:01:00+00:00")

    def test_empty_snapshot(self):
        result = status.build_status(
            [],
            hub_url=None,
            model=None,
            model_state="not_configured",
            model_checked_at=None,
            worker_running=False,
            local_date="2026-10-09",
            snapshot_at="2026-10-09T09:01:00+00:00",
        )
        self.assertEqual(result["queue_length"], 0)
        self.assertEqual(result["processing_count"], 0)
        self.assertEqual(result["reports_today"], 0)
        self.assertEqual(
            result["urgency_counts"],
            {"critical": 0, "high": 0, "medium": 0, "low": 0},
        )


def _response(body, status_code=200):
    def handler(request):
        return httpx.Response(status_code, json=body)

    return handler


def _failing(exception):
    def handler(request):
        raise exception

    return handler


class CheckModelTest(unittest.TestCase):
    def run_check(self, model, body=None, *, status_code=200, transport=None, timeout=1.0):
        if transport is None:
            transport = httpx.MockTransport(_response(body or {}, status_code))
        return asyncio.run(
            status.check_model(
                model, transport=transport, base_url="http://127.0.0.1:11434", timeout=timeout
            )
        )

    def test_no_model_is_not_configured_without_probing(self):
        checked = []
        transport = httpx.MockTransport(lambda request: checked.append(request) or httpx.Response(200, json={}))
        state, checked_at = asyncio.run(
            status.check_model(None, transport=transport, base_url="http://127.0.0.1:11434")
        )
        self.assertEqual((state, checked_at), ("not_configured", None))
        self.assertEqual(checked, [])

    def test_exact_loaded_tag_is_loaded(self):
        state, checked_at = self.run_check("gemma4:e2b", {"models": [{"name": "gemma4:e2b"}]})
        self.assertEqual(state, "loaded")
        self.assertIsNotNone(checked_at)

    def test_missing_tag_normalizes_to_latest(self):
        state, _ = self.run_check("gemma4", {"models": [{"name": "gemma4:latest"}]})
        self.assertEqual(state, "loaded")

    def test_other_model_is_not_loaded(self):
        state, _ = self.run_check("gemma4:e2b", {"models": [{"name": "qwen3:4b"}]})
        self.assertEqual(state, "not_loaded")

    def test_empty_model_list_is_not_loaded(self):
        state, _ = self.run_check("gemma4:e2b", {"models": []})
        self.assertEqual(state, "not_loaded")

    def test_http_error_is_unavailable(self):
        state, _ = self.run_check("gemma4:e2b", {"error": "boom"}, status_code=500)
        self.assertEqual(state, "unavailable")

    def test_bad_json_is_unavailable(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))
        state, _ = self.run_check("gemma4:e2b", transport=transport)
        self.assertEqual(state, "unavailable")

    def test_bad_schema_is_unavailable(self):
        for body in ("text", [{"name": "x"}], {"models": {"name": "x"}}):
            with self.subTest(body=body):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
                state, _ = self.run_check("gemma4:e2b", transport=transport)
                self.assertEqual(state, "unavailable")

    def test_non_dict_entries_are_ignored_as_not_loaded(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [1, 2]}))
        state, _ = self.run_check("gemma4:e2b", transport=transport)
        self.assertEqual(state, "not_loaded")

    def test_connect_error_is_unavailable(self):
        transport = httpx.MockTransport(_failing(httpx.ConnectError("refused")))
        state, _ = self.run_check("gemma4:e2b", transport=transport)
        self.assertEqual(state, "unavailable")

    def test_probe_is_bounded_by_timeout(self):
        async def slow(request):
            await asyncio.sleep(5)
            return httpx.Response(200, json={"models": []})

        transport = httpx.MockTransport(slow)
        started = time.monotonic()
        state, _ = self.run_check("gemma4:e2b", transport=transport, timeout=0.2)
        self.assertEqual(state, "unavailable")
        self.assertLess(time.monotonic() - started, 4.0)

    def test_probe_hits_the_api_ps_path_under_the_resolved_base(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"models": []})

        asyncio.run(
            status.check_model(
                "gemma4:e2b",
                transport=httpx.MockTransport(handler),
                base_url="http://192.168.1.9:11434/",
            )
        )
        self.assertEqual(seen, ["http://192.168.1.9:11434/api/ps"])


class StatusApiTestCase(unittest.TestCase):
    def make_client(self, hub_url=None, model=None):
        config = Config(model=model, whisper="off", port=8000, db_path=self.path, hub_url=hub_url)
        client = TestClient(create_app(config))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "reports.db"

    def insert(self, **columns):
        defaults = {
            "client_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "raw_text": TODAY_TEXT,
            "status": "queued",
        }
        defaults.update(columns)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            placeholders = ", ".join(defaults)
            conn.execute(
                f"INSERT INTO reports ({placeholders}) VALUES ({', '.join('?' for _ in defaults)})",
                tuple(defaults.values()),
            )

    def test_status_contract_without_model(self):
        client = self.make_client(hub_url="http://192.168.1.20:8000/")
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual(
            set(body),
            {
                "hub_url",
                "model",
                "model_state",
                "model_checked_at",
                "worker_running",
                "audio_available",
                "queue_length",
                "processing_count",
                "reports_today",
                "local_date",
                "urgency_counts",
                "snapshot_at",
            },
        )
        self.assertEqual(body["hub_url"], "http://192.168.1.20:8000/")
        self.assertIsNone(body["model"])
        self.assertEqual(body["model_state"], "not_configured")
        self.assertIsNone(body["model_checked_at"])
        self.assertIs(body["worker_running"], False)
        self.assertIs(body["audio_available"], False)
        self.assertEqual(body["local_date"], status.today_local_date())
        self.assertTrue(body["snapshot_at"].endswith("+00:00"))

    def test_status_counts_reflect_stored_rows(self):
        client = self.make_client()
        local_noon_utc = (
            datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
        )
        two_days_ago = (
            (datetime.now().astimezone() - timedelta(days=2))
            .replace(hour=12, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
        )
        self.insert(status="queued")
        self.insert(status="extracting")
        self.insert(status="done", urgency="critical", fields_json="{}")
        self.insert(status="failed")
        self.insert(status="done", urgency="high", resolved=1, fields_json="{}")
        self.insert(status="done", urgency="low", created_at=two_days_ago, fields_json="{}")

        body = client.get("/api/status").json()

        self.assertEqual(body["queue_length"], 1)
        self.assertEqual(body["processing_count"], 1)
        self.assertEqual(body["reports_today"], 5)
        self.assertEqual(body["urgency_counts"]["critical"], 1)
        self.assertEqual(body["urgency_counts"]["high"], 0)  # resolved: excluded
        self.assertEqual(body["urgency_counts"]["low"], 1)  # classified but not today

    def test_status_uses_eligible_host_header_without_override(self):
        client = self.make_client()
        body = client.get("/api/status", headers={"host": "192.168.1.7:9000"}).json()
        self.assertEqual(body["hub_url"], "http://192.168.1.7:9000/")
        body = client.get("/api/status", headers={"host": "localhost:8000"}).json()
        self.assertIsNone(body["hub_url"])

    def test_status_storage_failure_returns_503(self):
        client = self.make_client()

        async def fake_check(model):
            return ("not_configured", None)

        with mock.patch.object(status, "check_model", side_effect=fake_check), mock.patch(
            "silong.app.db.list_reports", side_effect=sqlite3.OperationalError("io")
        ):
            with self.assertLogs("silong.app", "ERROR"):
                response = client.get("/api/status")
        self.assertEqual(response.status_code, 503)
        self.assertIn("try again", response.json()["detail"])

    def test_qr_endpoint(self):
        client = self.make_client(hub_url="http://192.168.1.20:8000/")
        response = client.get("/api/qr")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("image/svg+xml", response.headers["content-type"])
        body = response.content
        self.assertIn(b"<svg", body)
        self.assertIn(b"<path", body)
        # Self-contained: no remote references, and the plaintext URL is not echoed.
        self.assertNotIn(b"href=", body)
        self.assertNotIn(b"http://192.168.1.20", body)

    def test_qr_passes_the_resolved_url_to_the_factory(self):
        client = self.make_client(hub_url="http://192.168.1.20:8080/")
        with mock.patch.object(status, "make_qr_svg", return_value=b"<svg/>") as factory:
            response = client.get("/api/qr")
        factory.assert_called_once_with("http://192.168.1.20:8080/")
        self.assertEqual(response.content, b"<svg/>")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_qr_uses_host_header_when_no_override(self):
        client = self.make_client()
        with mock.patch.object(status, "make_qr_svg", return_value=b"<svg/>") as factory:
            response = client.get("/api/qr", headers={"host": "192.168.1.30:7000"})
        factory.assert_called_once_with("http://192.168.1.30:7000/")
        self.assertEqual(response.status_code, 200)

    def test_qr_409_without_a_phone_url(self):
        client = self.make_client()
        for headers in ({"host": "localhost:8000"}, {"host": "127.0.0.1:8000"}):
            with self.subTest(headers=headers):
                response = client.get("/api/qr", headers=headers)
                self.assertEqual(response.status_code, 409)
                self.assertIn("SILONG_HUB_URL", response.json()["detail"])


class HeldStatusProbeTest(unittest.TestCase):
    """Receipt must succeed while the status model-probe is held (Design 8)."""

    def _hold_server(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        held = []

        def run():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                held.append(conn)  # never answered, never closed

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return server, held

    def test_receipt_completes_while_status_probe_is_held(self):
        server, held = self._hold_server()
        try:
            ollama_port = server.getsockname()[1]
            with tempfile.TemporaryDirectory() as tmp:
                port = _free_port()
                env = {k: v for k, v in os.environ.items() if k not in ("SILONG_MODEL", "OLLAMA_HOST")}
                env.update(
                    SILONG_MODEL="gemma4:e2b",
                    OLLAMA_HOST=f"http://127.0.0.1:{ollama_port}",
                    SILONG_PORT=str(port),
                    SILONG_DB=str(Path(tmp) / "hub.db"),
                )
                proc = subprocess.Popen(
                    [sys.executable, "-m", "silong"],
                    cwd=REPO_ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 15
                    while proc.poll() is None and time.monotonic() < deadline:
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/reports", timeout=5):
                                break
                        except OSError:
                            time.sleep(0.1)

                    # Start a status request; its probe is held for up to ~1 s.
                    status_result = {}

                    def get_status():
                        try:
                            with urllib.request.urlopen(
                                f"http://127.0.0.1:{port}/api/status", timeout=10
                            ) as response:
                                status_result["body"] = json.load(response)
                        except Exception as error:  # pragma: no cover - diagnostic
                            status_result["error"] = repr(error)

                    thread = threading.Thread(target=get_status)
                    thread.start()

                    time.sleep(0.15)
                    started = time.monotonic()
                    body = urllib.parse.urlencode(
                        {"client_id": str(uuid.uuid4()), "text": TODAY_TEXT}
                    ).encode()
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/reports", body, timeout=5
                    ) as response:
                        receipt = json.load(response)
                    elapsed = time.monotonic() - started

                    self.assertIn("id", receipt)
                    self.assertLess(elapsed, 0.9, "receipt was delayed by the held status probe")

                    thread.join(timeout=10)
                    self.assertEqual(status_result["body"]["model_state"], "unavailable")
                    self.assertIsNotNone(status_result["body"]["model_checked_at"])
                    self.assertIs(status_result["body"]["worker_running"], True)
                finally:
                    proc.terminate()
                    proc.communicate(timeout=15)
        finally:
            server.close()
            for conn in held:
                conn.close()


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


if __name__ == "__main__":
    unittest.main()