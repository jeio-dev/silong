import dataclasses
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from silong.config import Config, ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


class DefaultsTest(unittest.TestCase):
    def test_empty_environment_uses_defaults(self):
        config = load_config({})
        self.assertEqual(
            config,
            Config(model=None, whisper="off", port=8000, db_path=Path("silong.db")),
        )

    def test_blank_values_use_defaults(self):
        config = load_config(
            {"SILONG_MODEL": " ", "SILONG_WHISPER": "", "SILONG_PORT": "", "SILONG_DB": "  "}
        )
        self.assertEqual(config, load_config({}))

    def test_absent_model_is_allowed(self):
        config = load_config({"SILONG_PORT": "8000"})
        self.assertIsNone(config.model)

    def test_reads_os_environ_when_no_mapping_given(self):
        with mock.patch.dict(os.environ, {"SILONG_PORT": "8123"}):
            self.assertEqual(load_config().port, 8123)


class ValuesTest(unittest.TestCase):
    def test_all_values_are_read(self):
        config = load_config(
            {
                "SILONG_MODEL": "qwen3:4b",
                "SILONG_WHISPER": "small",
                "SILONG_PORT": "9000",
                "SILONG_DB": "/tmp/hub/reports.db",
            }
        )
        self.assertEqual(config.model, "qwen3:4b")
        self.assertEqual(config.whisper, "small")
        self.assertEqual(config.port, 9000)
        self.assertEqual(config.db_path, Path("/tmp/hub/reports.db"))

    def test_whisper_choices_are_case_insensitive(self):
        for raw, expected in [("base", "base"), ("OFF", "off"), (" Small ", "small")]:
            with self.subTest(raw=raw):
                self.assertEqual(load_config({"SILONG_WHISPER": raw}).whisper, expected)

    def test_port_bounds_are_accepted(self):
        self.assertEqual(load_config({"SILONG_PORT": "1"}).port, 1)
        self.assertEqual(load_config({"SILONG_PORT": "65535"}).port, 65535)

    def test_config_is_immutable(self):
        config = load_config({})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.port = 9000

    def test_environment_is_not_mutated(self):
        environ = {"SILONG_WHISPER": "BASE"}
        load_config(environ)
        self.assertEqual(environ, {"SILONG_WHISPER": "BASE"})


class InvalidValuesTest(unittest.TestCase):
    def test_invalid_port_values(self):
        for raw in ["abc", "80.5", "-1", "0", "65536", "8000x", "+8000", "²"]:
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError) as ctx:
                    load_config({"SILONG_PORT": raw})
                self.assertIn("SILONG_PORT", str(ctx.exception))

    def test_invalid_whisper_values(self):
        for raw in ["medium", "large", "on", "1"]:
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError) as ctx:
                    load_config({"SILONG_WHISPER": raw})
                message = str(ctx.exception)
                self.assertIn("SILONG_WHISPER", message)
                self.assertIn("small, base, off", message)


class OptionalDependenciesTest(unittest.TestCase):
    def test_base_dependencies_import(self):
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
        import python_multipart  # noqa: F401
        import uvicorn  # noqa: F401

    def test_config_does_not_load_optional_or_model_dependencies(self):
        code = (
            "import sys, silong.config; "
            "print(sorted(m for m in ('faster_whisper', 'qrcode', 'httpx') if m in sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
