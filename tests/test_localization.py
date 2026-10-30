"""Validate native ComfyUI locale resources and safe language fallback."""

import ast
import importlib.util
import json
import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "benpay_localization", SOURCE / "localization.py"
)
localization = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(localization)


class LocalizationTests(unittest.TestCase):
    def test_simplified_traditional_english_and_fallback(self):
        for value, locale in [
            ("en-US", "en"),
            ("zh-CN", "zh"),
            ("zh", "zh"),
            ("zh-TW", "zh-TW"),
            ("zh-Hant-HK", "zh-TW"),
            ("ja", "en"),
            ("../../config.json", "en"),
        ]:
            self.assertEqual(localization.normalize_locale(value), locale)
        self.assertEqual(
            localization.translate("configureKey", "en"), "Configure API Key"
        )
        self.assertEqual(localization.translate("configureKey", "zh"), "配置 API Key")
        self.assertEqual(
            localization.translate("configureKey", "zh-TW"), "設定 API Key"
        )

    def test_every_message_and_placeholder_exists_in_all_three_languages(self):
        catalogs = {
            locale: json.loads((SOURCE / "locales" / locale / "main.json").read_text())[
                "benpayVideo"
            ]
            for locale in ["en", "zh", "zh-TW"]
        }
        for locale, messages in catalogs.items():
            self.assertEqual(set(messages), set(catalogs["en"]), locale)
            for key, message in messages.items():
                self.assertTrue(message.strip(), (locale, key))
                self.assertEqual(
                    re.findall(r"\{\w+\}", message),
                    re.findall(r"\{\w+\}", catalogs["en"][key]),
                    (locale, key),
                )

    def test_native_node_translation_keys_match_real_node_schemas(self):
        tree = ast.parse((SOURCE / "nodes.py").read_text())
        expected = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in (
                "BenpayVideo",
                "BenpayVideoFromImages",
            ):
                expected[node.name] = {
                    call.args[0].value
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "Input"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                }
        for locale in ["en", "zh", "zh-TW"]:
            nodes = json.loads(
                (SOURCE / "locales" / locale / "nodeDefs.json").read_text()
            )
            self.assertEqual(set(nodes), set(expected))
            for name, inputs in expected.items():
                self.assertEqual(set(nodes[name]["inputs"]), inputs, (locale, name))
                self.assertTrue(nodes[name]["display_name"])

    def test_all_configuration_error_codes_have_translations(self):
        tree = ast.parse((SOURCE / "configuration.py").read_text())
        for call in ast.walk(tree):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "ConfigurationError"
            ):
                self.assertEqual(len(call.args), 2)
                code = ast.literal_eval(call.args[1])
                for locale in ["en", "zh", "zh-TW"]:
                    self.assertIn(code, localization._messages(locale))
