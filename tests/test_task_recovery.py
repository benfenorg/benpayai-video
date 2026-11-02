"""Exercise task recovery with real disk records and simulated remote requests."""

import ast
import asyncio
import json
import math
import subprocess
import sys
import tempfile
import types
import typing
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from test_catalog_and_interrupts import load_interrupts, load_node_functions, recovery
from video_fixtures import video_bytes


class TaskRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.key = "test-api-key-never-persist-this"
        self.created = []
        self.queries = []
        self.query_errors = []
        self.status = "succeeded"
        self.download_errors = []
        self.functions = load_node_functions(
            "_generate_video",
            "_build_request_body",
            "_resolve_video_url",
            "_prompt_text",
        )
        self.functions.update(
            folder_paths=types.SimpleNamespace(
                get_user_directory=lambda: str(self.directory),
                get_output_directory=lambda: str(self.directory),
            ),
            _default_api_key=lambda: self.key,
            _video_protocol_routes=AsyncMock(
                return_value={
                    "create": "https://example.com/create",
                    "query": "https://example.com/query/{task_id}",
                }
            ),
            _image_data_urls=lambda value: value or [],
            _reference_video_urls=lambda value: value.splitlines() if value else [],
            _request_json=AsyncMock(side_effect=self.request),
            _download_video=AsyncMock(side_effect=self.download),
            _video_node_output=lambda path: path,
            TASK_TIMEOUT_SECONDS=5,
            POLL_INTERVAL_SECONDS=0,
            QUERY_RETRY_BASE_SECONDS=0,
            run_interruptible=load_interrupts().run_interruptible,
        )

        @asynccontextmanager
        async def session(**_kwargs):
            yield object()

        patcher = patch.object(aiohttp, "ClientSession", session)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def request(self, _session, method, url, _headers, _payload=None):
        if method == "POST":
            task_id = f"task-{len(self.created) + 1}"
            self.created.append(task_id)
            return {"task_id": task_id}
        self.queries.append(url)
        if self.query_errors:
            raise self.query_errors.pop(0)
        return {"status": self.status, "video_url": "https://example.com/video.mp4"}

    async def download(self, _session, _url, task_id):
        if self.download_errors:
            raise self.download_errors.pop(0)
        path = self.directory / f"{task_id}.mp4"
        path.write_bytes(video_bytes())
        return path

    async def generate(self, **changes):
        parameters = {
            "protocol": "doubao",
            "model": "doubao-seedance-2.0",
            "prompt": "private prompt",
            "resolution": "720p",
            "duration": 5,
            "ratio": "16:9",
            "seed": 42,
            "recovery_scope": "workflow-1/node-1",
        }
        parameters.update(changes)
        return await self.functions["_generate_video"](**parameters)

    def records(self):
        return list((self.directory / "benpay-video-tasks").glob("*.json"))

    async def test_both_providers_send_key_persisted_before_post_and_regenerate_changes_it(
        self,
    ):
        captured = []

        async def request(session, method, url, headers, payload=None):
            if method == "POST":
                key = headers.get("Idempotency-Key")
                self.assertTrue(key)
                records = [json.loads(path.read_text()) for path in self.records()]
                self.assertTrue(
                    any(
                        record.get("idempotency_key") == key
                        and record["state"] == "creating"
                        for record in records
                    )
                )
                captured.append(key)
            else:
                self.assertNotIn("Idempotency-Key", headers)
            return await self.request(session, method, url, headers, payload)

        self.functions["_request_json"].side_effect = request
        for protocol, model, resolution in [
            ("doubao", "doubao-seedance-2.0", "720p"),
            ("minimax", "MiniMax-H3", "768P"),
        ]:
            options = {"protocol": protocol, "model": model, "resolution": resolution}
            await self.generate(**options)
            await self.generate(**options)
            await self.generate(**options, generation=1)
        self.assertEqual(len(captured), 4)
        self.assertEqual(len(set(captured)), 4)

    async def test_independent_api_invocations_use_different_idempotency_keys(self):
        await self.generate(recovery_scope="")
        await self.generate(recovery_scope="")
        calls = [
            call
            for call in self.functions["_request_json"].call_args_list
            if call.args[1] == "POST"
        ]
        keys = [call.args[3]["Idempotency-Key"] for call in calls]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertFalse(self.records())

    async def test_api_without_identity_runs_independently_without_disk_records(self):
        first = await self.generate(recovery_scope="")
        second = await self.generate(recovery_scope="")
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertNotEqual(first, second)
        self.assertEqual(self.created, ["task-1", "task-2"])
        self.assertFalse((self.directory / "benpay-video-tasks").exists())

    async def test_parallel_api_calls_without_identity_do_not_share_tasks_or_locks(
        self,
    ):
        first, second = await asyncio.gather(
            self.generate(recovery_scope=""), self.generate(recovery_scope="")
        )
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.created), 2)
        self.assertFalse((self.directory / "benpay-video-tasks").exists())

    async def test_api_without_identity_still_retries_queries(self):
        self.query_errors = [recovery.VideoHTTPError(503)]
        await self.generate(recovery_scope="")
        self.assertEqual(self.created, ["task-1"])
        self.assertEqual(len(self.queries), 2)
        self.assertFalse((self.directory / "benpay-video-tasks").exists())

    async def test_query_retries_transient_errors_without_recreating(self):
        for error in [
            recovery.VideoHTTPError(429),
            recovery.VideoHTTPError(503),
            TimeoutError(),
        ]:
            self.query_errors.append(error)
        path = await self.generate()
        self.assertTrue(path.exists())
        self.assertEqual(len(self.queries), 4)
        self.assertEqual(self.created, ["task-1"])

    async def test_asyncio_query_timeout_is_retried_without_recreating_task(self):
        self.query_errors = [asyncio.TimeoutError()]
        path = await self.generate()
        self.assertTrue(path.exists())
        self.assertEqual(len(self.queries), 2)
        self.assertEqual(self.created, ["task-1"])

    async def test_creation_timeout_sends_once_per_run(self):
        self.functions["_request_json"].side_effect = asyncio.TimeoutError()
        with self.assertRaisesRegex(recovery.TaskRecoveryError, "not confirmed"):
            await self.generate()
        with self.assertRaisesRegex(recovery.TaskRecoveryError, "not confirmed"):
            await self.generate()
        self.assertEqual(self.functions["_request_json"].await_count, 2)

    async def test_run_replays_original_key_and_body_after_lost_create_response(self):
        for protocol, model, resolution in (
            ("doubao", "doubao-seedance-2.0", "720p"),
            ("minimax", "MiniMax-H3", "768P"),
        ):
            with self.subTest(protocol=protocol):
                posts = []
                task_id = f"accepted-{protocol}"

                async def request(
                    session,
                    method,
                    url,
                    headers,
                    body=None,
                    *,
                    posts=posts,
                    task_id=task_id,
                ):
                    if method == "POST":
                        posts.append((headers.copy(), body.copy()))
                        if len(posts) == 1:
                            raise aiohttp.ClientConnectionError()
                        return {"task_id": task_id}
                    self.assertTrue(url.endswith(task_id))
                    self.assertNotIn("Idempotency-Key", headers)
                    return {
                        "status": "succeeded",
                        "video_url": "https://example.com/video.mp4",
                    }

                self.functions["_request_json"].side_effect = request
                parameters = {
                    "protocol": protocol,
                    "model": model,
                    "resolution": resolution,
                }
                with self.assertRaises(recovery.TaskRecoveryError):
                    await self.generate(**parameters)
                self.assertEqual(len(posts), 1)
                path = await self.generate(**parameters)
                self.assertTrue(path.exists())
                self.assertEqual(len(posts), 2)
                self.assertEqual(posts[0], posts[1])
                self.assertTrue(
                    posts[1][0]["Idempotency-Key"].startswith("benpay-video-")
                )
                await self.generate(**parameters)
                self.assertEqual(len(posts), 2)

    async def test_creation_error_shows_reason_key_request_and_response(self):
        self.functions["_request_json"].return_value = {
            "error": {
                "code": "upstream_error",
                "message": "Doubao upstream request failed.",
            }
        }
        self.functions["_request_json"].side_effect = None
        with self.assertRaises(recovery.TaskRecoveryError) as caught:
            await self.generate()
        message = str(caught.exception)
        self.assertTrue(
            message.startswith("upstream_error: Doubao upstream request failed.")
        )
        self.assertIn("Idempotency-Key: benpay-video-", message)
        self.assertIn('request: {"model":', message)
        self.assertIn('response: {"error":', message)
        self.assertNotIn(self.key, message)
        self.assertNotIn("private prompt", message)
        self.assertEqual(self.functions["_request_json"].await_count, 1)
        with self.assertRaises(recovery.TaskRecoveryError) as resumed:
            await self.generate()
        self.assertEqual(str(resumed.exception), message)
        self.assertEqual(self.functions["_request_json"].await_count, 2)

        record_path = self.records()[0]
        record = json.loads(record_path.read_text())
        record.pop("creation_error")
        record.pop("idempotency_key")
        record_path.write_text(json.dumps(record))
        with self.assertRaises(recovery.TaskRecoveryError) as legacy:
            await self.generate()
        self.assertIn("Previous creation unconfirmed", str(legacy.exception))
        self.assertIn("Idempotency-Key: not recorded", str(legacy.exception))
        self.assertIn("not saved by previous version", str(legacy.exception))
        self.assertIn('request: {"model":', str(legacy.exception))
        self.assertEqual(self.functions["_request_json"].await_count, 2)

    async def test_http_creation_error_includes_response_without_credentials(self):
        self.functions["_request_json"].side_effect = recovery.VideoHTTPError(
            503, {"error": {"code": "upstream_error", "message": f"Echo {self.key}"}}
        )
        with self.assertRaises(recovery.TaskRecoveryError) as caught:
            await self.generate()
        message = str(caught.exception)
        self.assertTrue(message.startswith("HTTP 503: upstream_error:"))
        self.assertIn('response: {"error":', message)
        self.assertNotIn(self.key, message)
        self.assertEqual(self.functions["_request_json"].await_count, 1)

    async def test_retry_exhaustion_resumes_same_task_after_restart(self):
        self.query_errors = [recovery.VideoHTTPError(503) for _ in range(4)]
        with self.assertRaises(recovery.VideoHTTPError):
            await self.generate()
        self.assertEqual(len(self.queries), 4)
        path = await self.generate()
        self.assertTrue(path.exists())
        self.assertEqual(self.created, ["task-1"])
        self.assertTrue(all(url.endswith("/task-1") for url in self.queries))

    async def test_nonretryable_query_error_preserves_record(self):
        self.query_errors = [recovery.VideoHTTPError(401)]
        with self.assertRaises(recovery.VideoHTTPError):
            await self.generate()
        self.assertEqual(len(self.queries), 1)
        await self.generate()
        self.assertEqual(self.created, ["task-1"])

    async def test_download_failure_recovers_completed_task(self):
        self.download_errors = [OSError("download interrupted")]
        with self.assertRaises(OSError):
            await self.generate()
        self.assertEqual(
            json.loads(self.records()[0].read_text())["state"], "succeeded"
        )
        await self.generate()
        self.assertEqual(self.created, ["task-1"])
        self.assertEqual(self.functions["_download_video"].await_count, 2)

    async def test_completed_local_video_is_reused_and_regeneration_is_explicit(self):
        first = await self.generate()
        queries = len(self.queries)
        self.assertEqual(await self.generate(), first)
        self.assertEqual(len(self.queries), queries)
        second = await self.generate(generation=1)
        self.assertNotEqual(first, second)
        self.assertEqual(self.created, ["task-1", "task-2"])

    async def test_creation_disconnect_sends_once_per_run(self):
        self.functions["_request_json"].side_effect = aiohttp.ClientConnectionError()
        with self.assertRaisesRegex(recovery.TaskRecoveryError, "not confirmed"):
            await self.generate()
        with self.assertRaisesRegex(recovery.TaskRecoveryError, "not confirmed"):
            await self.generate()
        self.assertEqual(self.functions["_request_json"].await_count, 2)
        self.functions["_request_json"].side_effect = self.request
        await self.generate(generation=1)
        self.assertEqual(self.created, ["task-1"])

    async def test_deleted_output_is_downloaded_again_without_creating_task(self):
        path = await self.generate()
        path.unlink()
        restored = await self.generate()
        self.assertTrue(restored.exists())
        self.assertEqual(self.created, ["task-1"])
        self.assertEqual(self.functions["_download_video"].await_count, 2)

    async def test_invalid_cached_video_is_replaced_without_creating_task(self):
        for invalid in [b'{"error":"expired"}', b"<html>error</html>", b""]:
            with self.subTest(content=invalid):
                path = await self.generate()
                path.write_bytes(invalid)
                restored = await self.generate()
                self.assertEqual(restored.read_bytes(), video_bytes())
                self.assertEqual(self.created, ["task-1"])

    async def test_http_200_nonvideo_download_is_cleaned_and_same_task_recovers(self):
        functions = load_node_functions("_download_video")
        functions["folder_paths"] = self.functions["folder_paths"]
        self.functions["_download_video"] = functions["_download_video"]
        for invalid in [
            b'{"error":"expired"}',
            b"<html>error</html>",
            b"",
            video_bytes()[:32],
        ]:
            with self.subTest(content=invalid):
                payload = {"body": invalid}

                async def chunks(_size, payload=payload):
                    yield payload["body"]

                @asynccontextmanager
                async def response(*_args, **_kwargs):
                    yield types.SimpleNamespace(
                        status=200,
                        content=types.SimpleNamespace(iter_chunked=chunks),
                    )

                @asynccontextmanager
                async def session(**_kwargs):
                    yield types.SimpleNamespace(get=response)

                with patch.object(aiohttp, "ClientSession", session):
                    with self.assertRaisesRegex(RuntimeError, "not a readable video"):
                        await self.generate()
                    self.assertEqual(list(self.directory.glob("*.mp4")), [])
                    self.assertEqual(
                        json.loads(self.records()[0].read_text())["state"], "succeeded"
                    )
                    payload["body"] = video_bytes()
                    restored = await self.generate()
                self.assertEqual(restored.read_bytes(), video_bytes())
                self.assertEqual(self.created, ["task-1"])
                restored.unlink()

    async def test_key_change_uses_separate_task_and_switching_back_reuses_original(
        self,
    ):
        original = await self.generate()
        self.key = "another-account-key"
        replacement = await self.generate()
        self.assertNotEqual(original, replacement)
        self.key = "test-api-key-never-persist-this"
        self.assertEqual(await self.generate(), original)
        self.assertEqual(self.created, ["task-1", "task-2"])
        self.assertEqual(self.functions["_download_video"].await_count, 2)

    async def test_node_parameters_and_credentials_are_isolated(self):
        await self.generate()
        await self.generate(recovery_scope="workflow-2/node-1")
        await self.generate(prompt="different prompt")
        self.key = "another-account-key"
        await self.generate()
        self.key = "test-api-key-never-persist-this"
        await self.generate()
        self.assertEqual(len(self.created), 4)

    async def test_image_contents_and_reference_urls_affect_fingerprint(self):
        await self.generate(
            image_mode="first_frame", images=["data:image/png;base64,AAA"]
        )
        await self.generate(
            image_mode="first_frame", images=["data:image/png;base64,BBB"]
        )
        await self.generate(
            image_mode="reference_video",
            reference_video_urls="https://example.com/1.mp4",
        )
        await self.generate(
            image_mode="reference_video",
            reference_video_urls="https://example.com/2.mp4",
        )
        self.assertEqual(len(self.created), 4)
        for path in self.records():
            payload = path.read_text()
            self.assertNotIn(self.key, payload)
            self.assertNotIn("private prompt", payload)
            self.assertNotIn("data:image", payload)
            self.assertNotIn("https://example.com/1.mp4", payload)

    async def test_terminal_task_requires_regeneration(self):
        self.status = "failed"
        with self.assertRaisesRegex(recovery.TaskRecoveryError, "Regenerate"):
            await self.generate()
        with self.assertRaisesRegex(recovery.TaskRecoveryError, "Regenerate"):
            await self.generate()
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(self.created, ["task-1"])
        self.status = "succeeded"
        await self.generate(generation=1)
        self.assertEqual(len(self.created), 2)

    async def test_missing_task_is_not_silently_replaced(self):
        self.query_errors = [recovery.VideoHTTPError(404)]
        with self.assertRaisesRegex(recovery.TaskRecoveryError, "Regenerate"):
            await self.generate()
        with self.assertRaisesRegex(recovery.TaskRecoveryError, "Regenerate"):
            await self.generate()
        self.assertEqual(self.created, ["task-1"])

    async def test_parallel_runs_are_blocked_and_cancelled_task_can_resume(self):
        started = asyncio.Event()
        original = self.functions["_request_json"].side_effect

        async def blocked_request(session, method, *args):
            if method == "GET":
                started.set()
                await asyncio.sleep(60)
            return await original(session, method, *args)

        self.functions["_request_json"].side_effect = blocked_request
        running = asyncio.create_task(self.generate())
        try:
            await asyncio.wait_for(started.wait(), 1)
            with self.assertRaisesRegex(
                recovery.TaskRecoveryError, "already has a running"
            ):
                await self.generate()
        finally:
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
        self.functions["_request_json"].side_effect = original
        await self.generate()
        self.assertEqual(self.created, ["task-1"])

    async def test_corrupt_record_does_not_create_another_task(self):
        await self.generate()
        self.records()[0].write_text("{}")
        with self.assertRaises(recovery.TaskRecoveryError):
            await self.generate()
        self.assertEqual(self.created, ["task-1"])

    async def test_saved_query_address_survives_routing_changes(self):
        self.download_errors = [OSError("download interrupted")]
        with self.assertRaises(OSError):
            await self.generate()
        self.functions["_video_protocol_routes"].return_value["query"] = (
            "https://example.com/new/{task_id}"
        )
        await self.generate()
        self.assertTrue(self.queries[-1].endswith("/query/task-1"))
        self.assertEqual(self.created, ["task-1"])

    async def test_recovery_does_not_require_route_discovery(self):
        self.download_errors = [OSError("download interrupted")]
        with self.assertRaises(OSError):
            await self.generate()
        lookup = AsyncMock(side_effect=RuntimeError("route discovery unavailable"))
        self.functions["_video_protocol_routes"] = lookup
        result = await self.generate()
        self.assertTrue(result.exists())
        self.assertEqual(await self.generate(), result)
        lookup.assert_not_awaited()
        self.assertEqual(self.created, ["task-1"])

    async def test_pending_task_resumes_with_saved_route_when_discovery_is_down(self):
        self.query_errors = [recovery.VideoHTTPError(503) for _ in range(4)]
        with self.assertRaises(recovery.VideoHTTPError):
            await self.generate()
        lookup = AsyncMock(side_effect=RuntimeError("route discovery unavailable"))
        self.functions["_video_protocol_routes"] = lookup
        await self.generate()
        lookup.assert_not_awaited()
        self.assertEqual(self.created, ["task-1"])
        self.assertTrue(all(url.endswith("/query/task-1") for url in self.queries))
        with self.assertRaisesRegex(RuntimeError, "route discovery unavailable"):
            await self.generate(generation=1)
        self.assertEqual(self.created, ["task-1"])

    async def test_invalid_provider_inputs_stop_before_task_creation(self):
        for changes in [
            {"protocol": "minimax", "model": "MiniMax-H3", "resolution": "720p"},
            {"image_mode": "reference_images", "images": ["image"] * 5},
            {
                "image_mode": "reference_video",
                "reference_video_urls": "\n".join(
                    f"https://example.com/{index}.mp4" for index in range(5)
                ),
            },
            {
                "protocol": "minimax",
                "model": "MiniMax-H3",
                "ratio": "adaptive",
                "image_mode": "reference_video",
                "reference_video_urls": "https://example.com/reference.mp4",
            },
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await self.generate(**changes)
        self.functions["_request_json"].assert_not_awaited()
        self.functions["_video_protocol_routes"].assert_not_awaited()
        self.assertEqual(self.created, [])
        self.assertEqual(self.records(), [])

    async def test_unused_materials_are_not_validated_or_sent(self):
        image_converter = self.functions["_image_data_urls"]
        self.functions["_reference_video_urls"] = load_node_functions(
            "_reference_video_urls"
        )["_reference_video_urls"]
        for protocol, model in [
            ("doubao", "doubao-seedance-2.0"),
            ("minimax", "MiniMax-H3"),
        ]:
            with self.subTest(protocol=protocol):
                await self.generate(
                    protocol=protocol,
                    model=model,
                    resolution="768P" if protocol == "minimax" else "720p",
                    ratio="adaptive",
                    image_mode="first_frame",
                    images=["image-data"],
                    reference_video_urls="invalid stale URL",
                )
                body = self.functions["_request_json"].call_args_list[-2].args[-1]
                self.assertNotIn("reference_videos", body)
        self.functions["_image_data_urls"] = unittest.mock.Mock(
            side_effect=ValueError("unused image must not be converted")
        )
        await self.generate(images=object(), reference_video_urls="invalid stale URL")
        self.functions["_image_data_urls"].assert_not_called()
        self.functions["_image_data_urls"] = image_converter


class RecoveryIdentityTests(unittest.TestCase):
    def test_idempotency_identity_is_stable_and_preserves_legacy_records(self):
        with tempfile.TemporaryDirectory() as directory:
            options = (
                Path(directory),
                "node-scope",
                "private-key",
                {"prompt": "private prompt"},
                0,
            )
            journal = recovery.TaskJournal(*options)
            same = recovery.TaskJournal(*options)
            self.assertEqual(journal.idempotency_key, same.idempotency_key)
            self.assertNotEqual(
                journal.idempotency_key,
                recovery.TaskJournal(*options[:-1], 1).idempotency_key,
            )
            with journal:
                record = journal.save(
                    "creating", query_url="https://example.com/query/{task_id}"
                )
            with same:
                self.assertEqual(
                    same.load()["idempotency_key"], record["idempotency_key"]
                )
                del record["idempotency_key"]
                same.path.write_text(json.dumps(record))
            legacy = recovery.TaskJournal(*options)
            with legacy:
                self.assertNotIn("idempotency_key", legacy.load())
                self.assertIsNone(legacy.idempotency_key)
                saved = legacy.save(
                    "pending",
                    task_id="old-task",
                    query_url="https://example.com/query/old-task",
                )
                self.assertNotIn("idempotency_key", saved)

    def test_corrupt_idempotency_key_blocks_recovery(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            recovery.TaskJournal(Path(directory), "scope", "key", {}, 0) as journal,
        ):
            record = journal.save("creating", query_url="https://example.com/query")
            record["idempotency_key"] = "different-key"
            journal.path.write_text(json.dumps(record))
            with self.assertRaisesRegex(
                recovery.TaskRecoveryError, "idempotency key mismatch"
            ):
                journal.load()

    def test_both_nodes_invalidate_execution_cache_on_each_invocation(self):
        source = Path(__file__).resolve().parents[1] / "nodes.py"
        tree = ast.parse(source.read_text())
        for name in ("BenpayVideo", "BenpayVideoFromImages"):
            with self.subTest(node=name):
                node = next(
                    item
                    for item in tree.body
                    if isinstance(item, ast.ClassDef) and item.name == name
                )
                hook = next(
                    item
                    for item in node.body
                    if isinstance(item, ast.FunctionDef)
                    and item.name == "fingerprint_inputs"
                )
                namespace = {}
                exec(  # noqa: S102 - load the real cache hook without model imports.
                    compile(
                        ast.Module(body=[hook], type_ignores=[]), str(source), "exec"
                    ),
                    namespace,
                )
                fingerprint = namespace["fingerprint_inputs"].__func__
                first = fingerprint(object, prompt="same prompt", seed=42)
                second = fingerprint(object, prompt="same prompt", seed=42)
                self.assertTrue(math.isnan(first))
                self.assertTrue(math.isnan(second))
                self.assertNotEqual([first], [second])

    def test_recovery_module_import_does_not_require_typing_self(self):
        import importlib.util

        source = Path(__file__).resolve().parents[1] / "task_recovery.py"
        spec = importlib.util.spec_from_file_location(
            "recovery_python310_check", source
        )
        module = importlib.util.module_from_spec(spec)
        # Python 3.10 has no typing.Self. Exercise imports with the same absence.
        saved = typing.__dict__.pop("Self", None)
        try:
            spec.loader.exec_module(module)
            self.assertTrue(callable(module.TaskJournal))
        finally:
            if saved is not None:
                typing.Self = saved

    def test_task_lock_excludes_another_process_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            source = str(Path(__file__).resolve().parents[1])
            code = (
                "import sys; from pathlib import Path; "
                f"sys.path.insert(0, {source!r}); "
                "from task_recovery import TaskJournal; "
                f"journal = TaskJournal(Path({directory!r}), 'scope', 'key', {{}}, 0); "
                "journal.__enter__(); journal.__exit__()"
            )
            with recovery.TaskJournal(Path(directory), "scope", "key", {}, 0):
                blocked = subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn(b"already has a running task", blocked.stderr)
            released = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(released.returncode, 0, released.stderr)

    def test_workflow_and_node_identity_are_stable_and_separate(self):
        workflow = {
            "id": "workflow-A",
            "nodes": [{"id": 1, "properties": {"benpayRecoveryId": "uuid-1"}}],
        }
        hidden = types.SimpleNamespace(
            unique_id="1", extra_pnginfo={"workflow": workflow}
        )
        first = recovery.node_recovery_scope(hidden)
        self.assertEqual(first, recovery.node_recovery_scope(hidden))
        workflow["id"] = "workflow-B"
        self.assertNotEqual(first, recovery.node_recovery_scope(hidden))
        self.assertEqual(recovery.node_recovery_scope(None), "")
        for metadata in [None, {}, {"workflow": None}]:
            self.assertEqual(
                recovery.node_recovery_scope(
                    types.SimpleNamespace(unique_id="1", extra_pnginfo=metadata)
                ),
                "",
            )


class RequestPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_response_json_is_logged_with_nested_errors_and_redaction(
        self,
    ):
        functions = load_node_functions("_request_json")
        body = {
            "data": {"taskId": "task-123", "status": "queued"},
            "error": {
                "code": "upstream_error",
                "message": "Echo private-key and private prompt",
            },
            "apiKey": "other-private-key",
            "secret_key": "private-secret",
            "prompt": "private prompt",
            "images": ["private-media"],
        }

        @asynccontextmanager
        async def request(*_args, **_kwargs):
            yield types.SimpleNamespace(
                status=200, text=AsyncMock(return_value=json.dumps(body))
            )

        with self.assertLogs("benpay_test.nodes", level="INFO") as logged:
            result = await functions["_request_json"](
                types.SimpleNamespace(request=request),
                "POST",
                "https://example.com/create?token=private-query-token",
                {"Authorization": "Bearer private-key"},
                {"prompt": "private prompt"},
            )
        self.assertEqual(result, body)
        output = "\n".join(logged.output)
        self.assertIn('"taskId": "task-123"', output)
        self.assertIn('"code": "upstream_error"', output)
        self.assertIn("HTTP 200", output)
        for secret in (
            "private-key",
            "private-secret",
            "private prompt",
            "private-media",
            "private-query-token",
        ):
            self.assertNotIn(secret, output)

    async def test_http_errors_are_typed_and_do_not_follow_redirects_or_echo_credentials(
        self,
    ):
        functions = load_node_functions("_request_json")
        for status in (302, 429, 503):
            calls = []

            @asynccontextmanager
            async def request(
                *args, response_status=status, recorded_calls=calls, **kwargs
            ):
                recorded_calls.append((args, kwargs))
                yield types.SimpleNamespace(
                    status=response_status,
                    headers={"X-Trace-Id": "test-trace-123"},
                    text=AsyncMock(return_value='{"error":"Bearer private-key"}'),
                )

            with (
                self.assertLogs("benpay_test.nodes", level="ERROR") as logged,
                self.assertRaises(recovery.VideoHTTPError) as caught,
            ):
                await functions["_request_json"](
                    types.SimpleNamespace(request=request),
                    "GET",
                    "https://example.com/query/task-1?token=private-query-token",
                    {"Authorization": "Bearer private-key"},
                )
            self.assertEqual(caught.exception.status, status)
            self.assertNotIn("private-key", str(caught.exception))
            self.assertIn(f"HTTP {status}", "\n".join(logged.output))
            self.assertIn("test-trace-123", "\n".join(logged.output))
            self.assertNotIn("private-key", "\n".join(logged.output))
            self.assertNotIn("private-query-token", "\n".join(logged.output))
            self.assertEqual(len(calls), 1)
            self.assertFalse(calls[0][1]["allow_redirects"])
