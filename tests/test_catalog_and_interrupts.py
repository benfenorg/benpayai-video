"""Regression tests without importing ComfyUI models or making paid requests."""

import ast
import asyncio
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from uuid import uuid4

import aiohttp
import av

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_SPEC = importlib.util.spec_from_file_location(
    "benpay_test_task_recovery", ROOT / "task_recovery.py"
)
recovery = importlib.util.module_from_spec(RECOVERY_SPEC)
RECOVERY_SPEC.loader.exec_module(recovery)


def load_node_functions(*names):
    names = {
        *names,
        "_resolve_model",
        "_default_model",
        "_create_video_task",
        "_resume_video_task",
        "_query_video_task",
        "_task_payload",
        "_normalise_base_url",
        "_video_url_from_payload",
        "_is_readable_video",
    }
    tree = ast.parse((ROOT / "nodes.py").read_text(encoding="utf-8"))
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    namespace = {
        "asyncio": asyncio,
        "json": json,
        "os": os,
        "tempfile": tempfile,
        "Path": Path,
        "re": re,
        "urlparse": urlparse,
        "uuid4": uuid4,
        "aiohttp": aiohttp,
        "av": av,
        "TaskJournal": recovery.TaskJournal,
        "TaskRecoveryError": recovery.TaskRecoveryError,
        "VideoHTTPError": recovery.VideoHTTPError,
        "TERMINAL_FAILURE_STATES": {"failed", "cancelled", "expired", "deleted"},
        "IO": types.SimpleNamespace(NodeOutput=object),
        "SUPPLIER_FEATURE": "videos",
        "DEFAULT_PROTOCOL": "doubao",
        "DEFAULT_MODEL": "doubao-seedance-2.0",
        "VIDEO_SUPPLIER_CATALOG": {
            "doubao": [{"model": "doubao-seedance-2.0"}],
            "minimax": [{"model": "MiniMax-H3"}],
        },
        "VIDEO_DOWNLOAD_TIMEOUT_SECONDS": 300,
    }
    module = ast.Module(body=definitions, type_ignores=[])
    if "_generate_video" in names:
        temporary = tempfile.TemporaryDirectory(prefix="benpay-task-test-")
        unittest.addModuleCleanup(temporary.cleanup)
        namespace["folder_paths"] = types.SimpleNamespace(
            get_user_directory=lambda: temporary.name,
            get_output_directory=lambda: temporary.name,
        )
    # Execute the real local functions, excluding model imports and startup I/O.
    exec(compile(module, str(ROOT / "nodes.py"), "exec"), namespace)  # noqa: S102
    return namespace


def load_interrupts():
    model_management = types.ModuleType("comfy.model_management")
    model_management.throw_exception_if_processing_interrupted = lambda: None
    spec = importlib.util.spec_from_file_location(
        "benpay_test_interrupts", ROOT / "interrupts.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"comfy.model_management": model_management}):
        spec.loader.exec_module(module)
    module.INTERRUPT_CHECK_SECONDS = 0.01
    return module


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name) / "中文 cache.json"
        self.functions = load_node_functions(
            "_parse_supplier_payload",
            "_read_supplier_cache",
            "_write_supplier_cache",
            "_load_supplier_catalog",
        )
        self.functions["_supplier_cache_path"] = lambda: self.cache

    def test_actual_supplier_field_names_preserve_both_protocols(self):
        payload = {
            "data": {
                "suppliers": [
                    {
                        "supplier": "doubao",
                        "products": [
                            {
                                "productCname": "doubao-seedance-2.0",
                                "verboseName": "豆包",
                                "features": ["videos"],
                            },
                            {"productCname": "text-model", "features": ["chat"]},
                        ],
                    },
                    {
                        "supplier": "minimax",
                        "products": [
                            {
                                "productSlug": "MiniMax-H3",
                                "verboseName": "MiniMax H3",
                                "features": ["videos"],
                            },
                        ],
                    },
                    {"supplier": "invalid", "products": None},
                ]
            }
        }
        self.assertEqual(
            self.functions["_parse_supplier_payload"](payload),
            {
                "doubao": [{"model": "doubao-seedance-2.0", "name": "豆包"}],
                "minimax": [{"model": "MiniMax-H3", "name": "MiniMax H3"}],
            },
        )

    def test_corrupt_utf8_cache_refreshes_from_network(self):
        self.cache.write_bytes(b'{"doubao":[{"name":"\xe7')
        catalog = {"minimax": [{"model": "MiniMax-H3", "name": "MiniMax H3"}]}
        self.functions["_fetch_supplier_catalog"] = lambda: catalog
        self.assertEqual(self.functions["_load_supplier_catalog"](), catalog)
        self.assertEqual(json.loads(self.cache.read_text()), catalog)

    def test_corrupt_utf8_cache_offline_uses_default(self):
        self.cache.write_bytes(b"\xff")
        self.functions["_fetch_supplier_catalog"] = unittest.mock.Mock(
            side_effect=OSError("offline")
        )
        self.assertIn("doubao", self.functions["_load_supplier_catalog"]())

    def test_failed_cache_replace_preserves_previous_contents(self):
        self.cache.write_text('{"old": []}')
        with patch.object(os, "replace", side_effect=OSError("replace failed")):
            self.functions["_write_supplier_cache"]({"new": []})
        self.assertEqual(self.cache.read_text(), '{"old": []}')
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])


class ProcessingInterrupted(Exception):
    pass


class RouteRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.functions = load_node_functions("_video_protocol_routes")
        self.routes = {
            "create": "https://example.com/create",
            "query": "https://example.com/query/{task_id}",
        }
        self.functions["VIDEO_PROTOCOL_ROUTES"] = {}
        self.functions["run_interruptible"] = load_interrupts().run_interruptible

    async def test_failed_lookup_can_recover_without_restart(self):
        fetch = unittest.mock.Mock(
            side_effect=[OSError("offline"), {"doubao": self.routes}]
        )
        self.functions["_fetch_video_protocol_routes"] = fetch
        resolve = self.functions["_video_protocol_routes"]
        with self.assertRaisesRegex(RuntimeError, "Check your connection"):
            await resolve("doubao")
        self.assertEqual(self.functions["VIDEO_PROTOCOL_ROUTES"], {})
        self.assertEqual(await resolve("doubao"), self.routes)
        self.assertEqual(await resolve("doubao"), self.routes)
        self.assertEqual(fetch.call_count, 2)

    async def test_unsupported_protocol_is_distinct_from_network_failure(self):
        self.functions["_fetch_video_protocol_routes"] = lambda: {"doubao": self.routes}
        with self.assertRaisesRegex(ValueError, "Unsupported video protocol: unknown"):
            await self.functions["_video_protocol_routes"]("unknown")

    async def test_wrong_provider_model_is_rejected_before_credentials_or_network(self):
        functions = load_node_functions("_generate_video")
        credentials = unittest.mock.Mock()
        route_lookup = AsyncMock()
        functions.update(
            _default_api_key=credentials,
            _video_protocol_routes=route_lookup,
        )
        with self.assertRaisesRegex(ValueError, "another video provider"):
            await functions["_generate_video"](
                "minimax", "doubao-seedance-2.0", "test", "720p", 5, "16:9", 42
            )
        credentials.assert_not_called()
        route_lookup.assert_not_awaited()

    def test_model_selection_preserves_manual_and_shared_models(self):
        functions = load_node_functions()
        resolve = functions["_resolve_model"]
        self.assertEqual(resolve("minimax", " "), "MiniMax-H3")
        self.assertEqual(resolve("minimax", " MiniMax-H3 "), "MiniMax-H3")
        self.assertEqual(resolve("minimax", "manual-model"), "manual-model")
        functions["VIDEO_SUPPLIER_CATALOG"]["minimax"].append(
            {"model": "doubao-seedance-2.0"}
        )
        self.assertEqual(
            resolve("minimax", "doubao-seedance-2.0"), "doubao-seedance-2.0"
        )

    async def test_task_retries_route_lookup_before_creating_video(self):
        functions = load_node_functions("_generate_video", "_video_protocol_routes")
        fetch = unittest.mock.Mock(
            side_effect=[OSError("offline"), {"doubao": self.routes}]
        )
        request = AsyncMock(side_effect=RuntimeError("request reached"))

        @asynccontextmanager
        async def session(**_kwargs):
            yield object()

        functions.update(
            {
                "VIDEO_PROTOCOL_ROUTES": {},
                "TASK_TIMEOUT_SECONDS": 60,
                "_fetch_video_protocol_routes": fetch,
                "_default_api_key": lambda: "test-key",
                "_build_request_body": lambda *_args, **_kwargs: {},
                "_prompt_text": lambda value: value,
                "_image_data_urls": lambda value: value,
                "_reference_video_urls": lambda value: value,
                "_resolve_video_url": lambda value, *_args: value,
                "_request_json": request,
                "run_interruptible": load_interrupts().run_interruptible,
            }
        )
        generate = functions["_generate_video"]
        with patch.object(aiohttp, "ClientSession", session):
            with self.assertRaisesRegex(RuntimeError, "Check your connection"):
                await generate(
                    "doubao",
                    "test-model",
                    "test",
                    "720p",
                    5,
                    "16:9",
                    42,
                    recovery_scope="test-node",
                )
            request.assert_not_awaited()
            with self.assertRaisesRegex(RuntimeError, "creation was not confirmed"):
                await generate(
                    "doubao",
                    "test-model",
                    "test",
                    "720p",
                    5,
                    "16:9",
                    42,
                    recovery_scope="test-node",
                )
        self.assertEqual(fetch.call_count, 2)
        request.assert_awaited_once()


class InterruptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.interrupts = load_interrupts()
        self.stopped = False

        def check_stop():
            if self.stopped:
                raise ProcessingInterrupted()

        self.interrupts.throw_exception_if_processing_interrupted = check_stop

    async def test_stop_cancels_inflight_network_wait(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def request():
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        operation = asyncio.create_task(self.interrupts.run_interruptible(request()))
        await started.wait()
        self.stopped = True
        with self.assertRaises(ProcessingInterrupted):
            await asyncio.wait_for(operation, 0.5)
        self.assertTrue(cancelled.is_set())

    async def test_already_stopped_does_not_start_request(self):
        self.stopped = True
        request = AsyncMock()
        with self.assertRaises(ProcessingInterrupted):
            await self.interrupts.run_interruptible(request())
        request.assert_not_awaited()

    async def test_request_result_and_error_propagate(self):
        self.assertEqual(
            await self.interrupts.run_interruptible(asyncio.sleep(0, result=42)), 42
        )
        with self.assertRaisesRegex(OSError, "offline"):
            await self.interrupts.run_interruptible(
                AsyncMock(side_effect=OSError("offline"))()
            )

    async def test_download_stop_removes_partial_video(self):
        with tempfile.TemporaryDirectory() as directory:
            started = asyncio.Event()

            async def chunks(_size):
                yield b"partial video"
                started.set()
                await asyncio.sleep(60)

            @asynccontextmanager
            async def response(*_args, **_kwargs):
                yield types.SimpleNamespace(
                    status=200, content=types.SimpleNamespace(iter_chunked=chunks)
                )

            functions = load_node_functions("_download_video")
            functions["folder_paths"] = types.SimpleNamespace(
                get_output_directory=lambda: directory
            )
            download = functions["_download_video"](
                types.SimpleNamespace(get=response),
                "https://example.com/video.mp4",
                "task-1",
            )
            operation = asyncio.create_task(self.interrupts.run_interruptible(download))
            await started.wait()
            self.assertTrue(list(Path(directory).glob("*.mp4")))
            self.stopped = True
            with self.assertRaises(ProcessingInterrupted):
                await asyncio.wait_for(operation, 0.5)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_generation_stops_during_poll_without_more_requests(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def request(_session, method, *_args):
            if method == "POST":
                return {"task_id": "task-1"}
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        @asynccontextmanager
        async def session(**_kwargs):
            yield object()

        functions = load_node_functions("_generate_video", "_video_protocol_routes")
        request_mock = AsyncMock(side_effect=request)
        functions.update(
            {
                "VIDEO_PROTOCOL_ROUTES": {
                    "doubao": {
                        "create": "https://example.com/create",
                        "query": "https://example.com/query",
                    }
                },
                "TASK_TIMEOUT_SECONDS": 60,
                "POLL_INTERVAL_SECONDS": 0,
                "_default_api_key": lambda: "test-key",
                "_build_request_body": lambda *_args, **_kwargs: {},
                "_prompt_text": lambda value: value,
                "_image_data_urls": lambda value: value,
                "_reference_video_urls": lambda value: value,
                "_resolve_video_url": lambda value, *_args: value,
                "_request_json": request_mock,
                "run_interruptible": self.interrupts.run_interruptible,
            }
        )
        with patch.object(aiohttp, "ClientSession", session):
            operation = asyncio.create_task(
                functions["_generate_video"](
                    "doubao",
                    "test-model",
                    "test prompt",
                    "720p",
                    5,
                    "16:9",
                    42,
                    recovery_scope="test-node",
                )
            )
            await asyncio.wait_for(started.wait(), 0.5)
            self.stopped = True
            with self.assertRaises(ProcessingInterrupted):
                await asyncio.wait_for(operation, 0.5)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(request_mock.await_count, 2)
