"""Exercise real HTTP routes without loading ComfyUI, models, or credentials."""

import importlib
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

SOURCE = Path(__file__).resolve().parents[1]
PACKAGE = "benpay_configuration_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(SOURCE)]
sys.modules[PACKAGE] = package
configuration = importlib.import_module(f"{PACKAGE}.configuration")
routes = importlib.import_module(f"{PACKAGE}.configuration_routes")


class ConfigurationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="benpay 中文 ")
        self.addCleanup(directory.cleanup)
        self.config_path = Path(directory.name) / "config.json"
        patcher = patch.object(configuration, "CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        table = web.RouteTableDef()
        routes.register_configuration_routes(table)
        app = web.Application()
        app.add_routes(table)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_model_catalog_matches_backend_without_credentials(self):
        nodes = types.ModuleType(f"{PACKAGE}.nodes")
        nodes.VIDEO_SUPPLIER_CATALOG = {
            "minimax": [{"model": "MiniMax-H3", "name": "MiniMax H3"}]
        }
        with patch.dict(sys.modules, {nodes.__name__: nodes}):
            response = await self.client.get("/benpay-video/models")
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), nodes.VIDEO_SUPPLIER_CATALOG)
        self.assertFalse(self.config_path.exists())

    async def test_first_save_creates_file_and_only_returns_masked_status(self):
        response = await self.client.get("/benpay-video/config-status")
        self.assertFalse((await response.json())["configured"])
        key = "test-private-key-abcd"
        response = await self.client.post("/benpay-video/config", json={"apiKey": key})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"configured": True, "suffix": "abcd"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(key, await response.text())
        self.assertEqual(configuration.read_api_key(), key)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        response = await self.client.get("/benpay-video/config")
        self.assertEqual(response.status, 405)
        self.assertNotIn(key, await response.text())

    async def test_replacing_key_preserves_unknown_fields(self):
        self.config_path.write_text(
            json.dumps(
                {"schemaVersion": 1, "apiKey": "old-key", "custom": {"enabled": True}}
            )
        )
        response = await self.client.post(
            "/benpay-video/config", json={"apiKey": "replacement-test-key"}
        )
        self.assertEqual(response.status, 200)
        value = json.loads(self.config_path.read_text())
        self.assertEqual(value["apiKey"], "replacement-test-key")
        self.assertEqual(value["custom"], {"enabled": True})

    async def test_invalid_requests_never_create_configuration(self):
        for value in [
            None,
            [],
            {},
            {"apiKey": ""},
            {"apiKey": 123},
            {"apiKey": "secret\nheader"},
            {"apiKey": "\ud800"},
            {"apiKey": "x" * 4097},
            {"apiKey": "secret", "path": "/other"},
        ]:
            with self.subTest(value=value):
                response = await self.client.post("/benpay-video/config", json=value)
                self.assertIn(response.status, (400, 415))
                self.assertNotIn("secret", await response.text())
                self.assertFalse(self.config_path.exists())
        response = await self.client.post(
            "/benpay-video/config",
            data=b"x" * 17000,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status, 413)

    async def test_cross_origin_and_non_json_writes_are_rejected(self):
        for origin in ["https://other.example", "null"]:
            response = await self.client.post(
                "/benpay-video/config",
                json={"apiKey": "secret"},
                headers={"Origin": origin},
            )
            self.assertEqual(response.status, 403)
        response = await self.client.post("/benpay-video/config", data="apiKey=secret")
        self.assertEqual(response.status, 415)
        self.assertFalse(self.config_path.exists())

    async def test_corrupt_or_unsupported_configuration_is_preserved(self):
        for original in ["{invalid", '{"schemaVersion":2,"apiKey":"old"}']:
            self.config_path.write_text(original)
            response = await self.client.post(
                "/benpay-video/config", json={"apiKey": "new-secret"}
            )
            self.assertEqual(response.status, 400)
            self.assertEqual(self.config_path.read_text(), original)
            self.assertNotIn("new-secret", await response.text())

    async def test_failed_replace_preserves_old_key_and_cleans_temporary_file(self):
        original = '{"schemaVersion":1,"apiKey":"old-key"}'
        self.config_path.write_text(original)
        with patch.object(configuration.os, "replace", side_effect=PermissionError):
            response = await self.client.post(
                "/benpay-video/config", json={"apiKey": "new-secret"}
            )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.config_path.read_text(), original)
        self.assertEqual(list(self.config_path.parent.glob(".config.json.tmp-*")), [])
        self.assertNotIn("new-secret", await response.text())

    async def test_configuration_symlink_is_not_followed(self):
        destination = self.config_path.with_name("other.json")
        original = '{"schemaVersion":1,"apiKey":"old"}'
        destination.write_text(original)
        try:
            self.config_path.symlink_to(destination)
        except OSError:
            self.skipTest("symbolic links unavailable")
        response = await self.client.post(
            "/benpay-video/config", json={"apiKey": "new-secret"}
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(destination.read_text(), original)

    async def test_errors_follow_each_clients_language(self):
        for locale, expected in [
            ("en", "Enter an API Key."),
            ("zh-CN", "请输入 API Key。"),
            ("zh-TW", "請輸入 API Key。"),
            ("ja", "Enter an API Key."),
        ]:
            response = await self.client.post(
                "/benpay-video/config",
                json={"apiKey": ""},
                headers={"Accept-Language": locale},
            )
            self.assertEqual(response.status, 400)
            self.assertEqual(
                await response.json(), {"error": expected, "code": "keyMissing"}
            )
        self.assertFalse(self.config_path.exists())
