import json
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

from silong import extract, rules

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "http://ollama.test:11434"

# Synthetic reports only.
TEXT_A = "Lola ko sa Room 3, hindi makalakad, wala nang gatas ang baby."
TEXT_B = "Kailangan ng tubig sa Room 5, tatlong bata."


def fields(**overrides):
    base = {
        "location": "Room 3",
        "in_center": True,
        "on_behalf": False,
        "headcount": 2,
        "vulnerable": ["senior", "infant"],
        "injured": False,
        "injury_note": None,
        "needs": ["milk_formula"],
        "summary": "Grandmother who cannot walk and a baby out of milk in Room 3.",
    }
    base.update(overrides)
    return base


def chat_reply(content):
    """An Ollama /api/chat non-streaming reply whose message content is `content`."""
    if not isinstance(content, str):
        content = json.dumps(content)
    return httpx.Response(
        200, json={"model": "qwen3:4b", "message": {"role": "assistant", "content": content}, "done": True}
    )


class FakeOllama:
    """Records requests and answers each with `handler(request)`."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)

    def client(self):
        return httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(self))

    def body(self, index=-1):
        return json.loads(self.requests[index].content)


class RequestTest(unittest.TestCase):
    def setUp(self):
        self.ollama = FakeOllama(lambda request: chat_reply(fields()))
        self.client = self.ollama.client()
        self.addCleanup(self.client.close)

    def test_request_options(self):
        extract.extract(TEXT_A, "qwen3:4b", client=self.client)
        request = self.ollama.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), f"{BASE_URL}/api/chat")
        body = self.ollama.body()
        self.assertEqual(body["model"], "qwen3:4b")
        self.assertIs(body["stream"], False)
        self.assertIs(body["think"], False)
        self.assertEqual(body["keep_alive"], -1)
        self.assertEqual(body["options"], {"temperature": 0})
        self.assertEqual(body["format"], extract.SCHEMA)

    def test_report_text_is_last_and_prefix_is_fixed(self):
        extract.extract(TEXT_A, "qwen3:4b", client=self.client)
        extract.extract(TEXT_B, "qwen3:4b", client=self.client)
        first, second = self.ollama.body(0)["messages"], self.ollama.body(1)["messages"]
        self.assertEqual(first[-1], {"role": "user", "content": TEXT_A})
        self.assertEqual(second[-1], {"role": "user", "content": TEXT_B})
        self.assertEqual(first[:-1], second[:-1])
        # Byte-identical prefix, so Ollama can reuse its cache.
        self.assertEqual(json.dumps(first[:-1]), json.dumps(second[:-1]))
        self.assertEqual(first[0]["role"], "system")


class PromptTest(unittest.TestCase):
    def test_schema_requires_all_fields_with_prd_enums(self):
        schema = extract.SCHEMA
        self.assertEqual(schema["type"], "object")
        self.assertEqual(len(schema["required"]), 9)
        self.assertEqual(set(schema["required"]), set(rules.FIELD_NAMES))
        self.assertEqual(set(schema["properties"]), set(rules.FIELD_NAMES))
        self.assertIs(schema["additionalProperties"], False)
        properties = schema["properties"]
        self.assertEqual(
            properties["vulnerable"]["items"]["enum"],
            ["senior", "pwd", "infant", "child", "pregnant", "sick"],
        )
        self.assertEqual(
            properties["needs"]["items"]["enum"],
            ["rescue", "medical", "medicine", "food", "water", "milk_formula", "hygiene", "other"],
        )
        for name in ("location", "injury_note"):
            self.assertEqual(properties[name]["type"], ["string", "null"])
        for name in ("in_center", "on_behalf"):
            self.assertEqual(properties[name]["type"], ["boolean", "null"])
        self.assertEqual(properties["headcount"]["type"], ["integer", "null"])
        self.assertEqual(properties["injured"]["type"], "boolean")
        self.assertEqual(properties["summary"]["type"], "string")

    def test_examples_are_valid_alternating_pairs(self):
        examples = extract.PROMPT_PREFIX[1:]
        self.assertIn(len(examples), (4, 6))  # two or three user/assistant pairs
        for user, assistant in zip(examples[::2], examples[1::2]):
            self.assertEqual(user["role"], "user")
            self.assertEqual(assistant["role"], "assistant")
            answer = json.loads(assistant["content"])
            self.assertEqual(rules.validate_fields(answer), answer)
            self.assertLessEqual(len(answer["summary"].split()), rules.SUMMARY_MAX_WORDS)

    def test_examples_cover_planned_cases(self):
        answers = [json.loads(message["content"]) for message in extract.PROMPT_PREFIX[2::2]]
        self.assertTrue(
            any(a["in_center"] is False and a["on_behalf"] is True and "rescue" in a["needs"] for a in answers)
        )
        self.assertTrue(
            any(a["in_center"] is True and rules.classify(a)[0] == "high" for a in answers)
        )
        self.assertTrue(any(a["location"] is None and a["headcount"] is None for a in answers))

    def test_on_behalf_follows_prd_meaning(self):
        # PRD: on_behalf is whether the reporter reports for other people; the
        # reporter's own family members are other people.
        rule = next(line for line in extract.SYSTEM_PROMPT.splitlines() if line.startswith("- on_behalf:"))
        self.assertNotIn("household", rule)
        self.assertIn("family members", rule)
        self.assertIn("false if the reporter is one of the people in need", rule)
        self.assertIn("null if unclear", rule)
        pairs = {
            user["content"]: json.loads(assistant["content"])
            for user, assistant in zip(extract.PROMPT_PREFIX[1::2], extract.PROMPT_PREFIX[2::2])
        }
        # Only the reporter's children and father are in need: reporting for others.
        family = next(text for text in pairs if "anak ko" in text and "tatay ko" in text)
        self.assertIs(pairs[family]["on_behalf"], True)
        # "kami" includes the reporter among the people in need.
        own = next(text for text in pairs if "kami" in text)
        self.assertIs(pairs[own]["on_behalf"], False)


class ReplyTest(unittest.TestCase):
    def run_extract(self, response):
        ollama = FakeOllama(lambda request: response)
        with ollama.client() as client:
            return extract.extract(TEXT_A, "qwen3:4b", client=client)

    def assertFails(self, response, *fragments):
        with self.assertRaises(extract.ExtractionError) as caught:
            self.run_extract(response)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    def test_valid_reply_returns_validated_fields(self):
        result = self.run_extract(
            chat_reply(fields(location="  Room 3 ", needs=["water", "food", "water"]))
        )
        self.assertEqual(result, fields(needs=["food", "water"]))

    def test_long_model_summary_is_truncated(self):
        words = [f"word{index}" for index in range(30)]
        result = self.run_extract(chat_reply(fields(summary=" ".join(words))))
        self.assertEqual(result["summary"], " ".join(words[:20]))

    def test_http_errors_name_the_status_and_cause(self):
        self.assertFails(
            httpx.Response(404, json={"error": "model 'nope:1b' not found"}),
            "HTTP 404",
            "model 'nope:1b' not found",
        )
        self.assertFails(httpx.Response(500, text="llama runner crashed"), "HTTP 500", "llama runner crashed")
        self.assertFails(httpx.Response(503), "HTTP 503", "Service Unavailable")

    def test_reply_that_is_not_json(self):
        self.assertFails(httpx.Response(200, text="<html>proxy</html>"), "not JSON")

    def test_reply_without_message(self):
        self.assertFails(httpx.Response(200, json={"done": True}), "without a message")
        self.assertFails(httpx.Response(200, json=[1, 2]), "without a message")

    def test_message_content_that_is_not_json(self):
        self.assertFails(chat_reply("Sure! Here are the fields: location=Room 3"), "not valid JSON")

    def test_fields_that_fail_validation(self):
        self.assertFails(
            chat_reply(fields(headcount="three")),
            "did not match the report fields",
            "headcount must be an integer",
        )
        self.assertFails(chat_reply(fields(needs=["shelter"])), "did not match", "needs contains 'shelter'")
        missing = fields()
        del missing["summary"]
        self.assertFails(chat_reply(missing), "missing field(s): summary")
        self.assertFails(chat_reply([fields()]), "fields must be an object")


class TransportErrorTest(unittest.TestCase):
    def assertFails(self, error, *fragments):
        def handler(request):
            raise error

        with FakeOllama(handler).client() as client, self.assertRaises(extract.ExtractionError) as caught:
            extract.extract(TEXT_A, "qwen3:4b", client=client)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_connect_error(self):
        self.assertFails(httpx.ConnectError("refused"), f"Ollama is not reachable at {BASE_URL}")

    def test_connect_timeout(self):
        self.assertFails(httpx.ConnectTimeout("slow"), "not reachable", "connection timed out")

    def test_read_timeout(self):
        self.assertFails(httpx.ReadTimeout("slow"), f"Ollama at {BASE_URL} did not reply within 300 s")

    def test_other_transport_error(self):
        self.assertFails(httpx.RemoteProtocolError("peer closed"), "Could not talk to Ollama", "peer closed")

    def test_connection_refused_by_real_socket(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # Port is closed again; the default client reads OLLAMA_HOST.
        with mock.patch.dict("os.environ", {"OLLAMA_HOST": f"127.0.0.1:{port}"}):
            with self.assertRaises(extract.ExtractionError) as caught:
                extract.extract(TEXT_A, "qwen3:4b")
        self.assertEqual(str(caught.exception), f"Ollama is not reachable at http://127.0.0.1:{port}")


class WarmUpTest(unittest.TestCase):
    def test_loads_model_and_keeps_it(self):
        ollama = FakeOllama(lambda request: httpx.Response(200, json={"done": True}))
        with ollama.client() as client:
            self.assertIsNone(extract.warm_up("qwen3:4b", client=client))
        body = ollama.body()
        self.assertEqual(str(ollama.requests[0].url), f"{BASE_URL}/api/chat")
        self.assertEqual(body["model"], "qwen3:4b")
        self.assertEqual(body["keep_alive"], -1)
        self.assertEqual(body["messages"], [])

    def test_failure_raises_extraction_error(self):
        ollama = FakeOllama(lambda request: httpx.Response(404, json={"error": "model not found"}))
        with ollama.client() as client, self.assertRaises(extract.ExtractionError) as caught:
            extract.warm_up("nope:1b", client=client)
        self.assertIn("model not found", str(caught.exception))


class OllamaUrlTest(unittest.TestCase):
    def test_addresses(self):
        cases = {
            None: "http://127.0.0.1:11434",
            "": "http://127.0.0.1:11434",
            "   ": "http://127.0.0.1:11434",
            "172.23.48.1:11434": "http://172.23.48.1:11434",
            "localhost:12000": "http://localhost:12000",
            "hub.local": "http://hub.local:11434",
            "http://172.23.48.1:11434": "http://172.23.48.1:11434",
            "http://172.23.48.1:11434/": "http://172.23.48.1:11434",
            "https://ollama.example": "https://ollama.example",
            "0.0.0.0": "http://127.0.0.1:11434",
            "0.0.0.0:11434": "http://127.0.0.1:11434",
            "http://0.0.0.0:9000": "http://127.0.0.1:9000",
            "::": "http://127.0.0.1:11434",
            "[::]:11434": "http://127.0.0.1:11434",
            "[::1]:11434": "http://[::1]:11434",
        }
        for value, expected in cases.items():
            environ = {} if value is None else {"OLLAMA_HOST": value}
            with self.subTest(value=value):
                self.assertEqual(extract.ollama_url(environ), expected)

    def test_unusable_address(self):
        for value in ("ftp://hub:21", "http://", "hub:port"):
            with self.subTest(value=value), self.assertRaises(extract.ExtractionError) as caught:
                extract.ollama_url({"OLLAMA_HOST": value})
            self.assertIn("OLLAMA_HOST", str(caught.exception))

    def test_reads_process_environment_by_default(self):
        with mock.patch.dict("os.environ", {"OLLAMA_HOST": "10.0.0.5:11434"}):
            self.assertEqual(extract.ollama_url(), "http://10.0.0.5:11434")


class ImportTest(unittest.TestCase):
    def test_import_opens_no_connection(self):
        # A fresh interpreter, so earlier imports cannot hide a connection.
        script = (
            "import socket\n"
            "def refuse(*args, **kwargs):\n"
            "    raise SystemExit('import opened a network connection')\n"
            "socket.socket.connect = refuse\n"
            "socket.socket.connect_ex = refuse\n"
            "socket.create_connection = refuse\n"
            "import silong.extract\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
