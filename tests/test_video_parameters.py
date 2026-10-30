"""Validate protocol request parameters without loading models or creating tasks."""

import ast
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "nodes.py"
tree = ast.parse(SOURCE.read_text())
# Load the real pure request builder without ComfyUI's model imports
# or startup HTTP requests. No protocol implementation is copied into the tests.
definitions = [
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "_build_request_body"
]
namespace = {}
exec(  # noqa: S102 - executes only the local pure function under test.
    compile(ast.Module(body=definitions, type_ignores=[]), str(SOURCE), "exec"),
    namespace,
)
build_request_body = namespace["_build_request_body"]


class VideoParameterTests(unittest.TestCase):
    def test_doubao_reference_video_limit(self):
        for count in [4, 5]:
            videos = [f"https://example.com/{index}.mp4" for index in range(count)]
            if count == 4:
                body = build_request_body(
                    "doubao",
                    "test-model",
                    "prompt",
                    "720p",
                    5,
                    "adaptive",
                    42,
                    image_mode="reference_video",
                    reference_videos=videos,
                )
                self.assertEqual(body["reference_videos"], videos)
            else:
                with self.assertRaisesRegex(ValueError, "at most 4"):
                    build_request_body(
                        "doubao",
                        "test-model",
                        "prompt",
                        "720p",
                        5,
                        "adaptive",
                        42,
                        image_mode="reference_video",
                        reference_videos=videos,
                    )

    def test_non_video_modes_do_not_send_stale_reference_videos(self):
        for protocol in ["doubao", "minimax"]:
            body = build_request_body(
                protocol,
                "test-model",
                "prompt",
                "768P" if protocol == "minimax" else "720p",
                5,
                "adaptive",
                42,
                image_mode="first_frame",
                images=["image-data"],
                reference_videos=["stale-disabled-url"],
            )
            self.assertNotIn("reference_videos", body)
            if protocol == "minimax":
                self.assertEqual(
                    [item["type"] for item in body["content"]], ["text", "image_url"]
                )

    def test_reference_image_limits_follow_each_provider(self):
        for protocol, maximum in [("doubao", 4), ("minimax", 5)]:
            for count in [0, maximum, maximum + 1]:
                with self.subTest(protocol=protocol, count=count):
                    arguments = (
                        protocol,
                        "test-model",
                        "prompt",
                        "768P" if protocol == "minimax" else "720p",
                        5,
                        "adaptive",
                        42,
                    )
                    options = {
                        "image_mode": "reference_images",
                        "images": [f"image-{index}" for index in range(count)],
                    }
                    if count == maximum:
                        body = build_request_body(*arguments, **options)
                        if protocol == "doubao":
                            self.assertEqual(len(body["images"]), maximum)
                        else:
                            self.assertEqual(len(body["content"]) - 1, maximum)
                    else:
                        with self.assertRaisesRegex(ValueError, f"1 to {maximum}"):
                            build_request_body(*arguments, **options)

    def test_reference_video_is_supported_only_for_doubao(self):
        videos = ["https://example.com/reference.mp4"]
        with self.assertRaisesRegex(
            ValueError, "MiniMax does not support reference_video"
        ):
            build_request_body(
                "minimax",
                "MiniMax-H3",
                "prompt",
                "768P",
                5,
                "adaptive",
                42,
                image_mode="reference_video",
                reference_videos=videos,
            )
        body = build_request_body(
            "doubao",
            "doubao-seedance-2.0",
            "prompt",
            "720p",
            5,
            "adaptive",
            42,
            image_mode="reference_video",
            reference_videos=videos,
        )
        self.assertEqual(body["metadata"]["mode"], "multi_ref_vid")
        self.assertEqual(body["reference_videos"], videos)

    def test_minimax_image_modes_require_adaptive_without_silent_rewrite(self):
        for mode, images in [
            ("first_frame", ["image-1"]),
            ("first_last_frame", ["image-1", "image-2"]),
            ("reference_images", ["image-1"]),
        ]:
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "requires adaptive"):
                    build_request_body(
                        "minimax",
                        "MiniMax-H3",
                        "prompt",
                        "768P",
                        5,
                        "9:16",
                        42,
                        image_mode=mode,
                        images=images,
                    )
                body = build_request_body(
                    "minimax",
                    "MiniMax-H3",
                    "prompt",
                    "768P",
                    5,
                    "adaptive",
                    42,
                    image_mode=mode,
                    images=images,
                )
                self.assertEqual(body["ratio"], "adaptive")

    def test_minimax_text_to_video_supports_all_six_documented_ratios(self):
        for ratio in ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"]:
            body = build_request_body(
                "minimax", "MiniMax-H3", "prompt", "768P", 5, ratio, 42
            )
            self.assertEqual(body["ratio"], ratio)
        with self.assertRaises(ValueError):
            build_request_body(
                "minimax", "MiniMax-H3", "prompt", "768P", 5, "adaptive", 42
            )

    def test_doubao_uses_lowercase_720p_and_five_seconds(self):
        body = build_request_body(
            "doubao", "doubao-seedance-2.0", "test prompt", "720p", 5, "16:9", 42
        )
        self.assertEqual(body["metadata"]["resolution"], "720p")
        self.assertEqual(body["metadata"]["duration"], 5)

    def test_image_to_video_preserves_resolution_and_duration(self):
        body = build_request_body(
            "doubao",
            "doubao-seedance-2.0",
            "test prompt",
            "720p",
            5,
            "adaptive",
            42,
            image_mode="first_frame",
            images=["data:image/png;base64,test"],
        )
        self.assertEqual(body["metadata"]["mode"], "i2v_first_frame")
        self.assertEqual(body["metadata"]["resolution"], "720p")
        self.assertEqual(body["metadata"]["duration"], 5)

    def test_minimax_uses_documented_768P_and_five_seconds(self):
        body = build_request_body(
            "minimax", "MiniMax-H3", "test prompt", "768P", 5, "16:9", 42
        )
        self.assertEqual(body["resolution"], "768P")
        self.assertEqual(body["duration"], 5)

    def test_doubao_768p_is_preserved(self):
        body = build_request_body(
            "doubao", "doubao-seedance-2.0", "test prompt", "768p", 5, "16:9", 42
        )
        self.assertEqual(body["metadata"]["resolution"], "768p")

    def test_minimax_normalizes_only_documented_resolutions(self):
        for resolution in ["480P", "768P", "480p", "768p"]:
            with self.subTest(resolution=resolution):
                body = build_request_body(
                    "minimax", "MiniMax-H3", "prompt", resolution, 5, "16:9", 42
                )
                self.assertEqual(body["resolution"], resolution.upper())
        for resolution in ["720p", "1080p", "custom-resolution"]:
            with self.subTest(resolution=resolution), self.assertRaisesRegex(
                ValueError, "768P or 480P"
            ):
                build_request_body(
                    "minimax", "MiniMax-H3", "prompt", resolution, 5, "16:9", 42
                )

    def test_resolution_is_passed_to_upstream_without_extra_restrictions(self):
        body = build_request_body(
            "doubao", "test-model", "test prompt", "custom-resolution", 5, "16:9", 42
        )
        self.assertEqual(body["metadata"]["resolution"], "custom-resolution")
