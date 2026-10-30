"""Configuration tests run without importing ComfyUI or loading any models."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "configuration.py"
SPEC = importlib.util.spec_from_file_location("benpay_configuration", MODULE_PATH)
configuration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configuration)


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="benpay 中文 ")
        self.addCleanup(self.directory.cleanup)
        self.config_path = Path(self.directory.name) / "config.json"

    def write_config(self, value: object) -> None:
        self.config_path.write_text(json.dumps(value), encoding="utf-8")

    def test_reads_local_key_and_reloads_changes(self) -> None:
        for key in ("first-private-key", "replacement-private-key"):
            self.write_config({"schemaVersion": 1, "apiKey": key, "custom": True})
            self.assertEqual(configuration.read_api_key(self.config_path), key)

    def test_rejects_invalid_configuration_without_echoing_secrets(self) -> None:
        cases = [
            [],
            {"schemaVersion": True, "apiKey": "secret"},
            {"schemaVersion": 2, "apiKey": "secret"},
            {"schemaVersion": 1, "apiKey": ""},
            {"schemaVersion": 1, "apiKey": 123},
            {"schemaVersion": 1, "apiKey": "secret\r\nheader"},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.write_config(value)
                with self.assertRaises(ValueError) as caught:
                    configuration.read_api_key(self.config_path)
                self.assertNotIn("secret", str(caught.exception))

    def test_missing_and_corrupt_files_have_actionable_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "comfyui-setup"):
            configuration.read_api_key(self.config_path)
        self.config_path.write_text("{invalid", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "格式无效"):
            configuration.read_api_key(self.config_path)

    def test_ui_status_never_returns_complete_key(self) -> None:
        for key, suffix in (("private-key-abcd", "abcd"), ("short", "")):
            with patch.object(configuration, "read_api_key", return_value=key):
                result = configuration.configuration_status()
            self.assertEqual(result, {"configured": True, "suffix": suffix})
            self.assertNotIn(key, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
