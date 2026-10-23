import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import aiohttp
import torch
from PIL import Image

import folder_paths
from comfy_api.latest import IO, InputImpl, ui


POLL_INTERVAL_SECONDS = 5
TASK_TIMEOUT_SECONDS = 1800
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 300
DEFAULT_PROTOCOL = "doubao"
DEFAULT_MODEL = "doubao-seedance-2.0"
SUPPLIER_FEATURE = "videos"
DEFAULT_BENFEN_GATEWAY = "https://cli-gateway.benfenpay.com"
SUPPLIER_REQUEST_TIMEOUT_SECONDS = 5
SUPPLIER_CACHE_FILENAME = "benpayai-video-suppliers.json"
TERMINAL_FAILURE_STATES = {"failed", "failure", "cancelled", "deleted"}
MAX_IMAGE_BYTES = 30 * 1024 * 1024
IMAGE_MODES = ["first_frame", "first_last_frame", "reference_images", "reference_video"]


def _normalise_gateway_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("Benfengate gateway cannot be empty.")
    if "://" not in value:
        value = f"https://{value}"
    return _normalise_base_url(value)


def _supplier_cache_path() -> Path:
    configured_path = os.getenv("BENPAYAI_SUPPLIER_CACHE_FILE", "").strip()
    if configured_path:
        return Path(configured_path).expanduser()
    try:
        user_directory = Path(folder_paths.get_user_directory())
    except (AttributeError, OSError):
        user_directory = Path(__file__).resolve().parent
    return user_directory / SUPPLIER_CACHE_FILENAME


def _supplier_request_url() -> str:
    gateway = os.getenv("BENFEN_GATEWAY", DEFAULT_BENFEN_GATEWAY)
    return f"{_normalise_gateway_url(gateway)}/api/v1/benpay-ai/suppliers"


def _call_config_request_url() -> str:
    gateway = os.getenv("BENFEN_GATEWAY", DEFAULT_BENFEN_GATEWAY)
    return f"{_normalise_gateway_url(gateway)}/api/v1/benpay-ai/call-config"


def _read_supplier_cache() -> dict[str, list[dict[str, str]]]:
    try:
        payload = json.loads(_supplier_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    catalog = _parse_supplier_payload(payload)
    if catalog:
        return catalog
    if isinstance(payload, dict) and all(isinstance(value, list) for value in payload.values()):
        return {
            supplier: [
                {"model": item.get("model", ""), "name": item.get("name", item.get("model", ""))}
                for item in products
                if isinstance(item, dict) and isinstance(item.get("model"), str) and item["model"]
            ]
            for supplier, products in payload.items()
            if isinstance(supplier, str) and supplier and isinstance(products, list)
        }
    return {}


def _write_supplier_cache(catalog: dict[str, list[dict[str, str]]]) -> None:
    cache_path = _supplier_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _parse_supplier_payload(payload: object) -> dict[str, list[dict[str, str]]]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict) or not isinstance(payload.get("suppliers"), list):
        return {}
    catalog: dict[str, list[dict[str, str]]] = {}
    for supplier in payload["suppliers"]:
        if not isinstance(supplier, dict):
            continue
        supplier_id = supplier.get("supplier")
        if not isinstance(supplier_id, str) or not supplier_id.strip():
            continue
        products: list[dict[str, str]] = []
        for product in supplier.get("products", []):
            if not isinstance(product, dict):
                continue
            features = product.get("features")
            if not isinstance(features, list) or not any(
                str(feature).lower() == SUPPLIER_FEATURE for feature in features
            ):
                continue
            model = product.get("product_cname") or product.get("product_slug")
            if not isinstance(model, str) or not model.strip():
                continue
            products.append({
                "model": model,
                "name": str(product.get("verbose_name") or model),
            })
        if products:
            catalog[supplier_id.strip()] = products
    return catalog


def _fetch_supplier_catalog() -> dict[str, list[dict[str, str]]]:
    request = Request(_supplier_request_url(), headers={"Accept": "application/json"})
    with urlopen(request, timeout=SUPPLIER_REQUEST_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    _ensure_success_response(payload, "suppliers")
    catalog = _parse_supplier_payload(payload)
    if not catalog:
        raise RuntimeError("Benfengate returned no video suppliers.")
    return catalog


def _fetch_video_protocol_routes() -> dict[str, dict[str, str]]:
    request = Request(_call_config_request_url(), headers={"Accept": "application/json"})
    with urlopen(request, timeout=SUPPLIER_REQUEST_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    _ensure_success_response(payload, "call-config")
    data = payload.get("data") if isinstance(payload, dict) else None
    protocols = data.get("videoProtocols") if isinstance(data, dict) else None
    if not isinstance(protocols, dict):
        raise RuntimeError("Benfengate call-config returned no videoProtocols.")
    routes: dict[str, dict[str, str]] = {}
    for protocol, config in protocols.items():
        if not isinstance(protocol, str) or not isinstance(config, dict):
            continue
        create_task = config.get("createTask")
        query_task = config.get("queryTask")
        create_url = create_task.get("url") if isinstance(create_task, dict) else None
        query_url = query_task.get("url") if isinstance(query_task, dict) else None
        if isinstance(create_url, str) and isinstance(query_url, str):
            routes[protocol] = {
                "create": _normalise_base_url(create_url),
                "query": _normalise_base_url(query_url),
            }
    if not routes:
        raise RuntimeError("Benfengate call-config returned no usable video routes.")
    return routes


def _load_supplier_catalog() -> dict[str, list[dict[str, str]]]:
    cached_catalog = _read_supplier_cache()
    try:
        catalog = _fetch_supplier_catalog()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
        return cached_catalog or {DEFAULT_PROTOCOL: [{"model": DEFAULT_MODEL, "name": DEFAULT_MODEL}]}
    _write_supplier_cache(catalog)
    return catalog


def _ensure_success_response(payload: object, endpoint_name: str) -> None:
    if not isinstance(payload, dict) or payload.get("code") not in (None, 0):
        raise RuntimeError(f"Benfengate {endpoint_name} request failed.")


def _load_startup_catalog() -> tuple[dict[str, list[dict[str, str]]], dict[str, dict[str, str]]]:
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="benpayai-catalog") as executor:
        catalog_future = executor.submit(_load_supplier_catalog)
        routes_future = executor.submit(_fetch_video_protocol_routes)
        catalog = catalog_future.result()
        try:
            routes = routes_future.result()
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            routes = {}
    routes = {protocol: route for protocol, route in routes.items() if protocol in catalog}
    return catalog, routes


def _default_api_key() -> str:
    return os.getenv("BENPAYAI_API_KEY", "")


def _normalise_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url:
        raise ValueError("BenpayAI base URL cannot be empty.")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("BenpayAI URL must use HTTPS.")
    return base_url


VIDEO_SUPPLIER_CATALOG, VIDEO_PROTOCOL_ROUTES = _load_startup_catalog()
VIDEO_PROTOCOL_OPTIONS = list(VIDEO_SUPPLIER_CATALOG) or [DEFAULT_PROTOCOL]
DEFAULT_SELECTED_PROTOCOL = (
    DEFAULT_PROTOCOL if DEFAULT_PROTOCOL in VIDEO_PROTOCOL_OPTIONS else VIDEO_PROTOCOL_OPTIONS[0]
)


def _default_model(protocol: str) -> str:
    products = VIDEO_SUPPLIER_CATALOG.get(protocol, [])
    if products:
        return products[0]["model"]
    return DEFAULT_MODEL


def _error_message(body: object) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:512]
        message = body.get("message")
        if isinstance(message, str) and message:
            return message[:512]
    return str(body)[:512]


async def _request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict | None = None,
) -> dict:
    async with session.request(method, url, headers=headers, json=payload) as response:
        text = await response.text()
        try:
            body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            body = text[:4096]
        if response.status >= 400:
            raise RuntimeError(f"BenpayAI returned HTTP {response.status}: {_error_message(body)}")
        if not isinstance(body, dict):
            raise RuntimeError("BenpayAI returned a non-object JSON response.")
        return body


async def _download_video(
    session: aiohttp.ClientSession,
    video_url: str,
    task_id: str,
) -> Path:
    parsed = urlparse(video_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("BenpayAI returned a video URL that does not use HTTPS.")

    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)[:96] or "video"
    output_dir = Path(folder_paths.get_output_directory())
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"benpayai_video_{safe_task_id}_{uuid4().hex[:8]}.mp4"

    try:
        async with session.get(
            video_url,
            timeout=aiohttp.ClientTimeout(total=VIDEO_DOWNLOAD_TIMEOUT_SECONDS),
        ) as response:
            if response.status >= 400:
                raise RuntimeError(f"Video download failed with HTTP {response.status}.")
            with output_path.open("wb") as output_file:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    output_file.write(chunk)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return output_path


def _prompt_text(prompt: str) -> str:
    # Legacy ComfyUI text nodes expose a single string as a one-item list.
    if isinstance(prompt, (list, tuple)):
        if len(prompt) != 1:
            raise ValueError("Prompt must be a single text value; connect one text output.")
        prompt = prompt[0]
    if not isinstance(prompt, str):
        raise TypeError("Prompt must be a text string.")
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Prompt cannot be empty.")
    return prompt


def _image_data_urls(images) -> list[str]:
    if images is None:
        return []
    if images.ndim != 4:
        raise ValueError("Images must be a batch of images.")
    return [_image_to_data_url(image) for image in images]


def _image_to_data_url(image) -> str:
    pixels = (
        image.detach().clamp(0, 1).mul(255).round().to(dtype=torch.uint8).cpu().numpy()
    )
    if pixels.ndim != 3 or pixels.shape[2] not in {1, 3, 4}:
        raise ValueError("Images must have one, three, or four channels.")
    if pixels.shape[2] == 1:
        pixels = pixels[:, :, 0]
    output = BytesIO()
    Image.fromarray(pixels).save(output, format="PNG")
    image_bytes = output.getvalue()
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("An input image exceeds the 30 MB image limit.")
    return f"data:image/png;base64,{base64.b64encode(image_bytes).decode('ascii')}"


def _reference_video_urls(value: str) -> list[str]:
    urls = [line.strip() for line in (value or "").splitlines() if line.strip()]
    for video_url in urls:
        parsed = urlparse(video_url)
        if video_url.startswith("data:"):
            continue
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Reference video URLs must use HTTPS or a data URL.")
    return urls


def _build_request_body(
    protocol: str,
    model: str,
    prompt: str,
    resolution: str,
    duration: int,
    ratio: str,
    seed: int,
    image_mode: str = "text_to_video",
    images: list[str] | None = None,
    reference_videos: list[str] | None = None,
) -> dict:
    images = images or []
    reference_videos = reference_videos or []
    if image_mode == "first_frame" and len(images) != 1:
        raise ValueError("first_frame mode requires exactly one image.")
    if image_mode == "first_last_frame" and len(images) != 2:
        raise ValueError("first_last_frame mode requires exactly two images.")
    if image_mode == "reference_images" and not 1 <= len(images) <= 5:
        raise ValueError("reference_images mode requires one to five images.")
    if image_mode == "reference_video" and not reference_videos:
        raise ValueError("reference_video mode requires at least one video URL.")

    if protocol == "doubao":
        mode = {
            "text_to_video": "t2v",
            "first_frame": "i2v_first_frame",
            "first_last_frame": "i2v_first_last_frame",
            "reference_images": "multi_ref",
            "reference_video": "multi_ref_vid",
        }[image_mode]
        metadata = {
            "resolution": resolution,
            "ratio": ratio,
            "duration": duration,
            "seed": seed,
            "watermark": False,
            "mode": mode,
        }
        body = {"model": model, "prompt": prompt, "images": images, "metadata": metadata}
        if reference_videos:
            body["reference_videos"] = reference_videos
        return body

    if protocol == "minimax":
        content: list[dict] = [{"type": "text", "text": prompt}]
        if image_mode == "first_frame":
            content.append({"type": "image_url", "image_url": {"url": images[0]}, "role": "first_frame"})
        elif image_mode == "first_last_frame":
            content.extend([
                {"type": "image_url", "image_url": {"url": images[0]}, "role": "first_frame"},
                {"type": "image_url", "image_url": {"url": images[1]}, "role": "last_frame"},
            ])
        elif image_mode == "reference_images":
            content.extend(
                {"type": "image_url", "image_url": {"url": image}, "role": "reference_image"}
                for image in images
            )
        elif image_mode == "reference_video":
            content.extend(
                {"type": "video_url", "video_url": {"url": video}, "role": "reference_video"}
                for video in reference_videos
            )
        return {
            "model": model,
            "content": content,
            "resolution": resolution,
            "duration": duration,
            "ratio": "adaptive" if image_mode != "text_to_video" else ratio,
            "seed": seed,
            "aigc_watermark": False,
        }
    raise ValueError(f"Unsupported video protocol: {protocol}")


def _resolve_video_url(route: str, task_id: str | None = None) -> str:
    route = route.replace("{task_id}", task_id or "")
    return _normalise_base_url(route)


def _task_payload(response: dict) -> dict:
    payload = dict(response)
    for key in ("task", "data"):
        value = response.get(key)
        if isinstance(value, dict):
            payload.update(value)
    return payload


def _video_url_from_payload(payload: dict) -> str | None:
    candidates = [
        payload.get("video_url"),
        payload.get("output_url"),
        payload.get("result_url"),
    ]
    content = payload.get("content")
    if isinstance(content, dict):
        candidates.extend([content.get("url"), content.get("video_url")])
    nested_data = payload.get("data")
    if isinstance(nested_data, dict):
        candidates.append(nested_data.get("result_url"))
        nested_content = nested_data.get("content")
        if isinstance(nested_content, dict):
            candidates.append(nested_content.get("video_url"))
    return next((candidate for candidate in candidates if isinstance(candidate, str) and candidate), None)


async def _generate_video(
    protocol: str,
    model: str,
    prompt: str,
    resolution: str,
    duration: int,
    ratio: str,
    seed: int,
    image_mode: str = "text_to_video",
    images=None,
    reference_video_urls: str = "",
) -> IO.NodeOutput:
    protocol = protocol.strip().lower()
    protocol_routes = VIDEO_PROTOCOL_ROUTES.get(protocol)
    if protocol_routes is None:
        raise ValueError(f"Unsupported video protocol: {protocol}")
    model = model.strip() or _default_model(protocol)
    api_key = _default_api_key().strip()
    if not api_key:
        raise ValueError("BENPAYAI_API_KEY must be set before starting ComfyUI.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    request_body = _build_request_body(
        protocol,
        model,
        _prompt_text(prompt),
        resolution,
        duration,
        ratio,
        seed,
        image_mode=image_mode,
        images=_image_data_urls(images),
        reference_videos=_reference_video_urls(reference_video_urls),
    )
    timeout = aiohttp.ClientTimeout(total=60)
    deadline = asyncio.get_running_loop().time() + TASK_TIMEOUT_SECONDS

    async with aiohttp.ClientSession(timeout=timeout) as session:
        create_response = await _request_json(
            session,
            "POST",
            _resolve_video_url(protocol_routes["create"]),
            headers,
            request_body,
        )
        task_id = create_response.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            task = _task_payload(create_response)
            task_id = task.get("task_id") or task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError("BenpayAI create response did not include task_id.")

        while True:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for BenpayAI task {task_id}.")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            query_response = await _request_json(
                session,
                "GET",
                _resolve_video_url(protocol_routes["query"], task_id),
                headers,
            )
            task = _task_payload(query_response)
            status = str(task.get("status", "")).lower()
            if status in TERMINAL_FAILURE_STATES:
                raise RuntimeError(f"{protocol} task {task_id} ended with status '{status}'.")
            if status not in {"succeeded", "success", "completed"}:
                continue

            video_url = _video_url_from_payload(task)
            if not isinstance(video_url, str) or not video_url:
                raise RuntimeError(f"{protocol} task {task_id} succeeded without a video URL.")
            video_path = await _download_video(session, video_url, task_id)
            return IO.NodeOutput(
                InputImpl.VideoFromFile(str(video_path)),
                ui=ui.PreviewVideo([
                    ui.SavedResult(video_path.name, "", IO.FolderType.output),
                ]),
            )


class BenpayAIVideo(IO.ComfyNode):
    @classmethod
    def define_schema(cls) -> IO.Schema:
        return IO.Schema(
            node_id="BenpayAIVideo",
            display_name="BenpayAI Video 文生视频",
            category="video/BenPay",
            description="Generate a video through a BenpayAI video protocol.",
            search_aliases=["BenpayAI Video"],
            is_output_node=True,
            inputs=[
                IO.Combo.Input(
                    "protocol",
                    options=VIDEO_PROTOCOL_OPTIONS,
                    default=DEFAULT_SELECTED_PROTOCOL,
                    tooltip="Video protocol supplied by Benfengate.",
                ),
                IO.String.Input(
                    "model",
                    default=_default_model(DEFAULT_SELECTED_PROTOCOL),
                    tooltip="Model identifier. You can enter a model manually.",
                ),
                IO.String.Input(
                    "prompt",
                    multiline=True,
                    dynamic_prompts=True,
                    default="",
                    force_input=True,
                    tooltip="Text prompt sent to the selected BenpayAI video protocol.",
                ),
                IO.Combo.Input(
                    "resolution",
                    options=["768P"],
                    default="768P",
                    tooltip="Output resolution supported by the current BenpayAI upstream.",
                ),
                IO.Int.Input(
                    "duration",
                    default=5,
                    min=4,
                    max=15,
                    step=1,
                    display_mode=IO.NumberDisplay.slider,
                    tooltip="Requested video duration in seconds.",
                ),
                IO.Combo.Input(
                    "ratio",
                    options=["16:9", "4:3", "1:1"],
                    default="16:9",
                ),
                IO.Int.Input(
                    "seed",
                    default=42,
                    min=0,
                    max=0x7FFFFFFFFFFFFFFF,
                    control_after_generate=True,
                    tooltip="Random seed sent to the selected video protocol.",
                ),
            ],
            outputs=[IO.Video.Output()],
        )

    @classmethod
    async def execute(
        cls,
        prompt: str,
        protocol: str = DEFAULT_SELECTED_PROTOCOL,
        model: str = DEFAULT_MODEL,
        resolution: str = "768P",
        duration: int = 5,
        ratio: str = "16:9",
        seed: int = 42,
    ) -> IO.NodeOutput:
        return await _generate_video(protocol, model, prompt, resolution, duration, ratio, seed)


class BenpayAIVideoFromImages(IO.ComfyNode):
    @classmethod
    def define_schema(cls) -> IO.Schema:
        return IO.Schema(
            node_id="BenpayAIVideoFromImages",
            display_name="BenpayAI Video 图片生成视频",
            category="video/BenPay",
            description="Generate a video from images or reference videos through a BenpayAI video protocol.",
            is_output_node=True,
            inputs=[
                IO.Combo.Input(
                    "protocol",
                    options=VIDEO_PROTOCOL_OPTIONS,
                    default=DEFAULT_SELECTED_PROTOCOL,
                    tooltip="Video protocol supplied by Benfengate.",
                ),
                IO.String.Input(
                    "model",
                    default=_default_model(DEFAULT_SELECTED_PROTOCOL),
                    tooltip="Model identifier. You can enter a model manually.",
                ),
                IO.Combo.Input(
                    "image_mode",
                    options=IMAGE_MODES,
                    default="first_frame",
                    tooltip="Select first frame, first and last frame, reference images, or reference video.",
                ),
                IO.Image.Input(
                    "images",
                    optional=True,
                    tooltip="Connect one image, two ordered frames, or one to five reference images.",
                ),
                IO.String.Input(
                    "reference_video_urls",
                    multiline=True,
                    default="",
                    tooltip="One HTTPS reference video URL per line.",
                ),
                IO.String.Input(
                    "prompt",
                    multiline=True,
                    dynamic_prompts=True,
                    default="",
                    force_input=True,
                    tooltip="Text prompt sent to the selected BenpayAI video protocol.",
                ),
                IO.Combo.Input(
                    "resolution",
                    options=["768P"],
                    default="768P",
                    tooltip="Output resolution supported by the current BenpayAI upstream.",
                ),
                IO.Int.Input(
                    "duration",
                    default=5,
                    min=4,
                    max=15,
                    step=1,
                    display_mode=IO.NumberDisplay.slider,
                    tooltip="Requested video duration in seconds.",
                ),
                IO.Combo.Input(
                    "ratio",
                    options=["16:9", "9:16", "4:3", "3:4", "1:1", "21:9", "adaptive"],
                    default="adaptive",
                ),
                IO.Int.Input(
                    "seed",
                    default=42,
                    min=0,
                    max=0x7FFFFFFFFFFFFFFF,
                    control_after_generate=True,
                    tooltip="Random seed sent to the selected video protocol.",
                ),
            ],
            outputs=[IO.Video.Output()],
        )

    @classmethod
    async def execute(
        cls,
        prompt: str,
        images=None,
        reference_video_urls: str = "",
        image_mode: str = "first_frame",
        protocol: str = DEFAULT_SELECTED_PROTOCOL,
        model: str = DEFAULT_MODEL,
        resolution: str = "768P",
        duration: int = 5,
        ratio: str = "adaptive",
        seed: int = 42,
    ) -> IO.NodeOutput:
        return await _generate_video(
            protocol,
            model,
            prompt,
            resolution,
            duration,
            ratio,
            seed,
            image_mode=image_mode,
            images=images,
            reference_video_urls=reference_video_urls,
        )
