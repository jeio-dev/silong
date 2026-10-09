import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from silong import bench, extract, rules

BASE_URL = "http://ollama.test:11434"
MODEL = "qwen3:4b"


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.reports = bench.load_fixtures()

    def test_five_reports_with_unique_ids(self):
        self.assertEqual(len(self.reports), 5)
        self.assertEqual(len({report["id"] for report in self.reports}), 5)

    def test_expected_fields_are_valid_and_classify_to_expected_priority(self):
        for report in self.reports:
            with self.subTest(report["id"]):
                expected = report["expected"]
                self.assertEqual(rules.validate_fields(expected["fields"]), expected["fields"])
                self.assertEqual(rules.classify(expected["fields"]), (expected["urgency"], expected["reason"]))

    def test_covers_the_planned_cases(self):
        reasons = [report["expected"]["reason"] for report in self.reports]
        self.assertIn("Critical: rescue needed outside center", reasons)
        self.assertIn("Critical: injury reported", reasons)
        self.assertEqual({report["expected"]["urgency"] for report in self.reports},
                         {"critical", "high", "medium", "low"})
        outside = self.reports[reasons.index("Critical: rescue needed outside center")]
        self.assertIs(outside["expected"]["fields"]["on_behalf"], True)
        injury = self.reports[reasons.index("Critical: injury reported")]
        self.assertIs(injury["expected"]["fields"]["in_center"], True)

    def test_on_behalf_follows_who_is_in_need_not_where_the_reporter_is(self):
        # extract.SYSTEM_PROMPT: true when the reporter is not one of the people
        # in need (family members are other people), false when they are.
        by_id = {report["id"]: report for report in self.reports}
        expected = {
            "outside-rescue-for-others": True,  # a neighboring family
            "injury-in-center": False,  # "Dalawa kaming nahiwa ... kamay ko"
            "vulnerable-supply-need": True,  # "tatlo kami dito", but only lolo needs medicine
            "other-need": False,  # "walo kami dito" all lack blankets
            "no-need": False,
        }
        self.assertIn("kami", by_id["vulnerable-supply-need"]["text"])
        for report_id, on_behalf in expected.items():
            with self.subTest(report_id):
                self.assertIs(by_id[report_id]["expected"]["fields"]["on_behalf"], on_behalf)

    def test_reports_are_not_the_prompt_examples(self):
        example_texts = {message["content"] for message in extract.EXAMPLES if message["role"] == "user"}
        for report in self.reports:
            self.assertNotIn(report["text"], example_texts)


class CompareTest(unittest.TestCase):
    def test_location_normalized_and_partial(self):
        self.assertEqual(bench.compare("location", "Room 7, evacuation center", "room 7 evacuation center."), "match")
        self.assertEqual(bench.compare("location", "Room 7, evacuation center", "Room 7"), "partial")
        self.assertEqual(bench.compare("location", "Room 7", "Room 8"), "no")
        self.assertEqual(bench.compare("location", None, None), "match")
        self.assertEqual(bench.compare("location", "Room 7", None), "no")

    def test_injury_note_scores_presence_and_summary_is_not_scored(self):
        self.assertEqual(bench.compare("injury_note", "cut hand", "bleeding hand"), "match")
        self.assertEqual(bench.compare("injury_note", None, "cut"), "no")
        self.assertEqual(bench.compare("summary", "a", "b"), "not scored")

    def test_other_fields_are_exact(self):
        self.assertEqual(bench.compare("headcount", 4, 4), "match")
        self.assertEqual(bench.compare("in_center", None, False), "no")
        self.assertEqual(bench.compare("needs", ["rescue"], ["rescue", "food"]), "no")


class FakeOllama:
    """Answers Ollama's API: one model loaded until unloaded, replies per report text."""

    def __init__(self, replies):
        self.replies = replies  # report text -> reply content string
        self.loaded = ["gemma4:e2b"]
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.2"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{
                "name": MODEL, "size": 2497293931, "digest": "359d7dd4bcda0000",
                "details": {"parameter_size": "4.0B", "quantization_level": "Q4_K_M"}}]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"name": name, "size": 100, "size_vram": 0, "context_length": 4096} for name in self.loaded]})
        body = json.loads(request.content)
        if path == "/api/generate":
            self.loaded.remove(body["model"])
            return httpx.Response(200, json={"done": True})
        if path == "/api/chat":
            self.loaded = [body["model"]]
            if not body["messages"]:
                return httpx.Response(200, json={"done": True})
            content = self.replies[body["messages"][-1]["content"]]
            return httpx.Response(200, json={"message": {"role": "assistant", "content": content}, "done": True})
        return httpx.Response(404)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.reports = bench.load_fixtures()
        replies = {report["text"]: json.dumps(report["expected"]["fields"]) for report in self.reports}
        # One report gets a wrong headcount, one a reply that is not JSON.
        wrong = dict(self.reports[2]["expected"]["fields"], headcount=30)
        replies[self.reports[2]["text"]] = json.dumps(wrong)
        replies[self.reports[3]["text"]] = "not json"
        self.ollama = FakeOllama(replies)
        self.ticks = iter(range(0, 10_000_000, 1500))
        self.client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(self.ollama))
        self.addCleanup(self.client.close)

    def run_bench(self):
        return bench.run(MODEL, self.client, self.reports,
                         clock=lambda: next(self.ticks) / 1000, sleep=lambda s: None,
                         windows=lambda: None,
                         local=lambda: {"os": "Test OS", "cpu": "Test CPU", "cpus": 8,
                                        "total_kb": 1024 * 1024, "available_kb": 512 * 1024})

    def test_unloads_loaded_models_before_warm_up(self):
        result = self.run_bench()
        self.assertEqual(result["unload"], {"before": ["gemma4:e2b"], "after": []})
        paths = [request.url.path for request in self.ollama.requests]
        unload = paths.index("/api/generate")
        self.assertLess(unload, paths.index("/api/chat"))
        self.assertEqual(json.loads(self.ollama.requests[unload].content),
                         {"model": "gemma4:e2b", "keep_alive": 0})

    def test_records_results_and_failures(self):
        result = self.run_bench()
        rows = result["reports"]
        self.assertEqual(len(rows), 5)
        self.assertIsNone(result["warm_up_error"])
        self.assertEqual(result["cold_load_ms"], 1500)
        self.assertTrue(all(row["llm_ms"] == 1500 for row in rows))
        self.assertEqual(rows[0]["urgency"], "critical")
        self.assertEqual(rows[2]["matches"]["headcount"], "no")
        self.assertEqual(rows[3]["error"], "Model reply was not valid JSON")
        self.assertIsNone(rows[3]["urgency"])

    def test_render_shows_sources_priorities_matches_and_times(self):
        text = bench.render(self.run_bench())
        for report in self.reports:
            self.assertIn(report["text"], text)
        self.assertIn("version 0.40.2", text)
        self.assertIn("2,497,293,931 bytes, 4.0B, Q4_K_M", text)
        self.assertIn("Loaded before unload (`/api/ps`): gemma4:e2b; after unload: none", text)
        self.assertIn("Windows memory before warm-up: not recorded", text)
        self.assertIn("Cold load (one warm-up request): 1,500 ms (1.5 s)", text)
        self.assertIn("| 3 | vulnerable-supply-need | high | high | 7/8 | 1,500 |", text)
        self.assertIn("| 4 | other-need | medium | failed: Model reply was not valid JSON | failed | 1,500 |", text)
        self.assertIn("| headcount | 3 | 30 | no |", text)
        self.assertIn("Priority matched: 4/5. Scored fields matched: 31/40. Failed extractions: 1.", text)
        self.assertIn("- Actual: **failed**: Model reply was not valid JSON", text)

    def test_main_appends_runs_to_out_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "MODELS.md"
            transport = httpx.MockTransport(self.ollama)
            real_client = httpx.Client

            def client(**kwargs):
                kwargs.pop("transport", None)
                return real_client(transport=transport, **kwargs)

            with mock.patch.object(bench.httpx, "Client", client), \
                    mock.patch.object(bench, "windows_info", return_value=None), \
                    mock.patch.dict("os.environ", {"OLLAMA_HOST": BASE_URL}), \
                    mock.patch("sys.stdout"):
                self.assertEqual(bench.main(["--model", MODEL, "--out", str(out)]), 0)
                self.assertEqual(bench.main(["--model", MODEL, "--out", str(out)]), 0)
            text = out.read_text(encoding="utf-8")
        self.assertEqual(text.count("# 02 Five-report model test"), 1)
        self.assertEqual(text.count(f"## `{MODEL}`, run "), 2)

    def test_main_reports_unreachable_ollama(self):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        real_client = httpx.Client
        with mock.patch.object(bench.httpx, "Client",
                               lambda **kw: real_client(transport=httpx.MockTransport(refuse), **kw)), \
                mock.patch.dict("os.environ", {"OLLAMA_HOST": BASE_URL}), \
                mock.patch("sys.stderr") as stderr:
            self.assertEqual(bench.main(["--model", MODEL]), 1)
        self.assertIn("Could not use Ollama", "".join(call.args[0] for call in stderr.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
