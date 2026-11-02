import asyncio
import base64
import json
import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import aiohttp
import av
import folder_paths
import torch
from comfy_api.latest import IO, InputImpl, ui
from PIL import Image

from .configuration import read_api_key
from .interrupts import run_interruptible
from .task_recovery import (
    TaskJournal,
    TaskRecoveryError,
    VideoHTTPError,
    node_recovery_scope,
)

LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
QUERY_RETRY_BASE_SECONDS = 1
TASK_TIMEOUT_SECONDS = 1800
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 300
DEFAULT_PROTOCOL = "doubao"
DEFAULT_MODEL = "doubao-seedance-2.0"
SUPPLIER_FEATURE = "videos"
DEFAULT_BENFEN_GATEWAY = "https://cli-gateway.benfenpay.com"
SUPPLIER_REQUEST_TIMEOUT_SECONDS = 5
SUPPLIER_CACHE_FILENAME = "benpay-video-suppliers.json"
TERMINAL_FAILURE_STATES = {
    "failed",
    "failure",
    "cancelled",
    "canceled",
    "deleted",
    "expired",
}
MAX_IMAGE_BYTES = 30 * 1024 * 1024
IMAGE_MODES = ["first_frame", "first_last_frame", "reference_images", "reference_video"]
VIDEO_RESOLUTION_OPTIONS = ["480p", "720p", "1080p", "480P", "768P"]
DEFAULT_VIDEO_RESOLUTION = "720p"


def _normalise_gateway_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("Benfengate gateway cannot be empty.")
    if "://" not in value:
        value = f"https://{value}"
    return _normalise_base_url(value)


def _supplier_cache_path() -> Path:
    configured_path = os.getenv("BENPAY_SUPPLIER_CACHE_FILE", "").strip()
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
    except (OSError, ValueError):
        return {}
    catalog = _parse_supplier_payload(payload)
    if catalog:
        return catalog
    if isinstance(payload, dict) and all(
        isinstance(value, list) for value in payload.values()
    ):
        return {
            supplier: [
                {
                    "model": item.get("model", ""),
                    "name": item.get("name", item.get("model", "")),
                }
                for item in products
                if isinstance(item, dict)
                and isinstance(item.get("model"), str)
                and item["model"]
            ]
            for supplier, products in payload.items()
            if isinstance(supplier, str) and supplier and isinstance(products, list)
        }
    return {}


def _write_supplier_cache(catalog: dict[str, list[dict[str, str]]]) -> None:
    cache_path = _supplier_cache_path()
    temporary = None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(catalog, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_path.parent,
            prefix=".benpay-catalog-",
            delete=False,
        ) as cache_file:
            temporary = Path(cache_file.name)
            cache_file.write(payload)
        os.replace(temporary, cache_path)
    except (OSError, ValueError):
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
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
        supplier_products = supplier.get("products")
        if not isinstance(supplier_products, list):
            continue
        for product in supplier_products:
            if not isinstance(product, dict):
                continue
            features = product.get("features")
            if not isinstance(features, list) or not any(
                str(feature).lower() == SUPPLIER_FEATURE for feature in features
            ):
                continue
            model = product.get("productCname") or product.get("productSlug")
            if not isinstance(model, str) or not model.strip():
                continue
            products.append(
                {
                    "model": model,
                    "name": str(product.get("verboseName") or model),
                }
            )
        if products:
            catalog[supplier_id.strip()] = products
    return catalog


def _redact_response(value: object, secrets: list[str]) -> object:
    if isinstance(value, dict):
        private_fields = {
            "authorization",
            "apikey",
            "token",
            "accesstoken",
            "refreshtoken",
            "secret",
            "secretkey",
            "password",
            "cookie",
            "setcookie",
            "sign",
            "signature",
            "csrftoken",
            "prompt",
            "images",
            "referencevideos",
        }
        return {
            key: "[redacted]"
            if key.lower().replace("_", "").replace("-", "") in private_fields
            else _redact_response(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_response(item, secrets) for item in value]
    if isinstance(value, str):
        if value.startswith("data:"):
            return "[redacted media]"
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return re.sub(r"(?i)Bearer\s+[^\s\"'<>]+", "Bearer [redacted]", value)
    return value


def _log_api_response(
    method: str,
    url: str,
    status: int,
    response: object,
    headers: dict[str, str] | None = None,
    request: dict | None = None,
) -> None:
    authorization = (headers or {}).get("Authorization", "")
    secrets = [authorization, authorization.removeprefix("Bearer ")]
    request = request or {}
    prompt = request.get("prompt")
    if isinstance(prompt, str):
        secrets.append(prompt)
    for item in request.get("content", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            secrets.append(item["text"])
    parsed = urlparse(url)
    LOGGER.info(
        "[BenPay] %s %s%s response: HTTP %s JSON=%s",
        method,
        parsed.hostname,
        parsed.path,
        status,
        json.dumps(_redact_response(response, secrets), ensure_ascii=False),
    )


def _fetch_supplier_catalog() -> dict[str, list[dict[str, str]]]:
    request = Request(_supplier_request_url(), headers={"Accept": "application/json"})
    with urlopen(request, timeout=SUPPLIER_REQUEST_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
        _log_api_response("GET", request.full_url, response.status, payload)
    _ensure_success_response(payload, "suppliers")
    catalog = _parse_supplier_payload(payload)
    if not catalog:
        raise RuntimeError("Benfengate returned no video suppliers.")
    return catalog


def _fetch_video_protocol_routes() -> dict[str, dict[str, str]]:
    request = Request(
        _call_config_request_url(), headers={"Accept": "application/json"}
    )
    with urlopen(request, timeout=SUPPLIER_REQUEST_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
        _log_api_response("GET", request.full_url, response.status, payload)
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
        return cached_catalog or {
            DEFAULT_PROTOCOL: [{"model": DEFAULT_MODEL, "name": DEFAULT_MODEL}]
        }
    _write_supplier_cache(catalog)
    return catalog


def _ensure_success_response(payload: object, endpoint_name: str) -> None:
    if not isinstance(payload, dict) or payload.get("code") not in (None, 0):
        raise RuntimeError(f"Benfengate {endpoint_name} request failed.")


def _load_startup_catalog() -> tuple[
    dict[str, list[dict[str, str]]], dict[str, dict[str, str]]
]:
    with ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="benpay-catalog"
    ) as executor:
        catalog_future = executor.submit(_load_supplier_catalog)
        routes_future = executor.submit(_fetch_video_protocol_routes)
        catalog = catalog_future.result()
        try:
            routes = routes_future.result()
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            routes = {}
    routes = {
        protocol: route for protocol, route in routes.items() if protocol in catalog
    }
    return catalog, routes


def _default_api_key() -> str:
    return read_api_key()


def _normalise_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url:
        raise ValueError("BenPay base URL cannot be empty.")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("BenPay URL must use HTTPS.")
    return base_url


VIDEO_SUPPLIER_CATALOG, VIDEO_PROTOCOL_ROUTES = _load_startup_catalog()
VIDEO_PROTOCOL_OPTIONS = list(VIDEO_SUPPLIER_CATALOG) or [DEFAULT_PROTOCOL]
DEFAULT_SELECTED_PROTOCOL = (
    DEFAULT_PROTOCOL
    if DEFAULT_PROTOCOL in VIDEO_PROTOCOL_OPTIONS
    else VIDEO_PROTOCOL_OPTIONS[0]
)


def _default_model(protocol: str) -> str:
    products = VIDEO_SUPPLIER_CATALOG.get(protocol, [])
    if products:
        return products[0]["model"]
    return DEFAULT_MODEL


def _resolve_model(protocol: str, model: str) -> str:
    model = model.strip()
    if not model:
        return _default_model(protocol)
    selected_models = {
        product["model"] for product in VIDEO_SUPPLIER_CATALOG.get(protocol, [])
    }
    if model not in selected_models and any(
        product["model"] == model
        for supplier, products in VIDEO_SUPPLIER_CATALOG.items()
        if supplier != protocol
        for product in products
    ):
        raise ValueError(
            "The selected model belongs to another video provider. "
            "Choose a model for the selected provider."
        )
    # Keep manual model IDs usable when the discovery catalog is incomplete.
    return model


async def _request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict | None = None,
) -> dict:
    async with session.request(
        method, url, headers=headers, json=payload, allow_redirects=False
    ) as response:
        text = await response.text()
        try:
            body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            body = text[:4096]
        _log_api_response(method, url, response.status, body, headers, payload)
        if response.status >= 300:
            # Upstream error bodies can echo Authorization or request credentials.
            parsed = urlparse(url)
            trace_id = getattr(response, "headers", {}).get("X-Trace-Id", "")
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", trace_id):
                trace_id = "unavailable"
            code = body.get("code") if isinstance(body, dict) else None
            LOGGER.error(
                "[BenPay] %s %s%s failed: HTTP %s, code=%s, trace_id=%s",
                method,
                parsed.hostname,
                parsed.path,
                response.status,
                code if type(code) is int else "unavailable",
                trace_id,
            )
            authorization = headers.get("Authorization", "")
            raise VideoHTTPError(
                response.status,
                _redact_response(
                    body, [authorization, authorization.removeprefix("Bearer ")]
                ),
            )
        if not isinstance(body, dict):
            LOGGER.error(
                "[BenPay] %s request returned HTTP %s with non-object JSON content",
                method,
                response.status,
            )
            raise RuntimeError("BenPay returned a non-object JSON response.")
        return body


async def _video_protocol_routes(protocol: str) -> dict[str, str]:
    cached = VIDEO_PROTOCOL_ROUTES.get(protocol)
    if cached is not None:
        return cached
    try:
        routes = await run_interruptible(
            asyncio.to_thread(_fetch_video_protocol_routes)
        )
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError(
            "Could not load BenPay video routes. Check your connection and run the workflow again."
        ) from None
    VIDEO_PROTOCOL_ROUTES.update(routes)
    if protocol not in routes:
        raise ValueError(f"Unsupported video protocol: {protocol}")
    return routes[protocol]


def _is_readable_video(path: Path) -> bool:
    try:
        with av.open(str(path), mode="r") as video:
            if not video.streams.video:
                return False
            return next(video.decode(video=0), None) is not None
    except (av.error.FFmpegError, OSError, ValueError):
        return False


async def _download_video(
    session: aiohttp.ClientSession,
    video_url: str,
    task_id: str,
) -> Path:
    parsed = urlparse(video_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("BenPay returned a video URL that does not use HTTPS.")

    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)[:96] or "video"
    output_dir = Path(folder_paths.get_output_directory())
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"benpay_video_{safe_task_id}_{uuid4().hex[:8]}.mp4"

    try:
        async with session.get(
            video_url,
            timeout=aiohttp.ClientTimeout(total=VIDEO_DOWNLOAD_TIMEOUT_SECONDS),
        ) as response:
            if response.status >= 400:
                raise RuntimeError(
                    f"Video download failed with HTTP {response.status}."
                )
            with output_path.open("wb") as output_file:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    output_file.write(chunk)
        if not await asyncio.to_thread(_is_readable_video, output_path):
            raise RuntimeError(
                "Downloaded content is not a readable video; rerun to download the same task."
            )
    except (Exception, asyncio.CancelledError):
        output_path.unlink(missing_ok=True)
        raise
    return output_path


def _prompt_text(prompt: str) -> str:
    # Legacy ComfyUI text nodes expose a single string as a one-item list.
    if isinstance(prompt, (list, tuple)):
        if len(prompt) != 1:
            raise ValueError(
                "Prompt must be a single text value; connect one text output."
            )
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
    images = (images or []) if image_mode != "text_to_video" else []
    reference_videos = (
        (reference_videos or []) if image_mode == "reference_video" else []
    )
    if protocol == "doubao":
        resolution = resolution.lower()
        if resolution not in {"480p", "720p", "1080p"}:
            raise ValueError("Doubao resolution must be 480p, 720p or 1080p.")
    if protocol == "minimax":
        resolution = resolution.upper()
        if resolution not in {"768P", "480P"}:
            raise ValueError("MiniMax resolution must be 768P or 480P.")
        if image_mode == "reference_video":
            raise ValueError("MiniMax does not support reference_video mode.")
        allowed_ratios = (
            {"16:9", "9:16", "4:3", "3:4", "1:1", "21:9"}
            if image_mode == "text_to_video"
            else {"adaptive"}
        )
        if ratio not in allowed_ratios:
            raise ValueError(
                "MiniMax image-to-video requires adaptive aspect ratio."
                if image_mode != "text_to_video"
                else "Unsupported MiniMax text-to-video aspect ratio."
            )
    if image_mode == "first_frame" and len(images) != 1:
        raise ValueError("first_frame mode requires exactly one image.")
    if image_mode == "first_last_frame" and len(images) != 2:
        raise ValueError("first_last_frame mode requires exactly two images.")
    max_reference_images = 4 if protocol == "doubao" else 5
    if (
        image_mode == "reference_images"
        and not 1 <= len(images) <= max_reference_images
    ):
        raise ValueError(
            f"{protocol} reference_images mode requires 1 to {max_reference_images} images."
        )
    if image_mode == "reference_video" and not reference_videos:
        raise ValueError("reference_video mode requires at least one video URL.")
    if protocol == "doubao" and len(reference_videos) > 4:
        raise ValueError("Doubao supports at most 4 reference videos.")

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
        body = {
            "model": model,
            "prompt": prompt,
            "images": images,
            "metadata": metadata,
        }
        if reference_videos:
            body["reference_videos"] = reference_videos
        return body

    if protocol == "minimax":
        content: list[dict] = [{"type": "text", "text": prompt}]
        if image_mode == "first_frame":
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": images[0]},
                    "role": "first_frame",
                }
            )
        elif image_mode == "first_last_frame":
            content.extend(
                [
                    {
                        "type": "image_url",
                        "image_url": {"url": images[0]},
                        "role": "first_frame",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": images[1]},
                        "role": "last_frame",
                    },
                ]
            )
        elif image_mode == "reference_images":
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": image},
                    "role": "reference_image",
                }
                for image in images
            )
        return {
            "model": model,
            "content": content,
            "resolution": resolution,
            "duration": duration,
            "ratio": ratio,
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
    return next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, str) and candidate
        ),
        None,
    )


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
    recovery_scope: str = "",
    generation: int = 0,
) -> IO.NodeOutput:
    protocol = protocol.strip().lower()
    model = _resolve_model(protocol, model)
    api_key = _default_api_key().strip()
    if not api_key:
        raise ValueError("BenPay API Key 未配置，请重新运行安装脚本。")

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
        images=_image_data_urls(images) if image_mode != "text_to_video" else [],
        reference_videos=(
            _reference_video_urls(reference_video_urls)
            if protocol == "doubao" and image_mode == "reference_video"
            else []
        ),
    )
    journal = TaskJournal(
        Path(folder_paths.get_user_directory()) / "benpay-video-tasks",
        recovery_scope,
        api_key,
        {"protocol": protocol, "body": request_body},
        generation,
    )
    # The OS lock spans creation, polling and download, and releases on cancel.
    with journal:
        record = journal.load()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            if record is None or (
                record["state"] == "creating" and journal.idempotency_key
            ):
                protocol_routes = await _video_protocol_routes(protocol)
                record = await _create_video_task(
                    session, protocol_routes, headers, request_body, journal
                )
            video_path = await _resume_video_task(
                session, headers, journal, record, request_body
            )
    return _video_node_output(video_path)


def _creation_error_details(
    reason: str, key: str, body: dict, response: object, headers: dict
) -> str:
    authorization = headers.get("Authorization", "")
    secrets = [authorization, authorization.removeprefix("Bearer ")]
    if isinstance(body.get("prompt"), str):
        secrets.append(body["prompt"])
    for item in body.get("content", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            secrets.append(item["text"])
    return "\n".join(
        [
            str(_redact_response(reason, secrets)),
            f"Idempotency-Key: {key}",
            "request: "
            + json.dumps(_redact_response(body, secrets), ensure_ascii=False),
            "response: "
            + json.dumps(_redact_response(response, secrets), ensure_ascii=False),
        ]
    )


async def _create_video_task(
    session: aiohttp.ClientSession,
    routes: dict,
    headers: dict,
    body: dict,
    journal: TaskJournal,
) -> dict:
    if not journal.idempotency_key:
        raise TaskRecoveryError("Cannot create a task without an idempotency key.")
    headers = {**headers, "Idempotency-Key": journal.idempotency_key}
    journal.save("creating", query_url=routes["query"])
    create_url = _resolve_video_url(routes["create"])
    LOGGER.info(
        "[BenPay] Creating video task at %s; idempotency_key=%s",
        urlparse(create_url).hostname,
        journal.idempotency_key,
    )
    # Send once per Run. A later Run may replay the same key and request body.
    response = None
    try:
        response = await run_interruptible(
            _request_json(session, "POST", create_url, headers, body)
        )
        task = _task_payload(response)
        task_id = response.get("task_id") or task.get("task_id") or task.get("id")
        if not isinstance(task_id, str) or not task_id:
            code = response.get("code")
            LOGGER.error(
                "[BenPay] Create response has no task ID; code=%s",
                code if type(code) is int else "unavailable",
            )
            error = response.get("error")
            reason = "Create response did not contain a task ID."
            if isinstance(error, dict):
                reason = (
                    f"{error.get('code', 'API error')}: {error.get('message', reason)}"
                )
            raise TaskRecoveryError(reason)
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        TimeoutError,
        RuntimeError,
    ) as exc:
        reason = (
            f"HTTP {exc.status}"
            if isinstance(exc, VideoHTTPError)
            else str(exc) or type(exc).__name__
        )
        if isinstance(exc, VideoHTTPError):
            response = exc.response
            if isinstance(response, dict) and isinstance(response.get("error"), dict):
                error = response["error"]
                reason += (
                    f": {error.get('code', 'API error')}: {error.get('message', '')}"
                )
        details = _creation_error_details(
            f"{reason} (creation was not confirmed; no automatic retry)",
            journal.idempotency_key,
            body,
            response,
            headers,
        )
        LOGGER.error("[BenPay] %s", details)
        try:
            journal.save(
                "creating",
                query_url=routes["query"],
                creation_error=details.encode("utf-8")[:8192].decode(
                    "utf-8", errors="ignore"
                ),
            )
        except (OSError, TaskRecoveryError):
            LOGGER.error("[BenPay] Could not save creation error details")
        raise TaskRecoveryError(details) from None
    try:
        return journal.save(
            "pending",
            task_id=task_id,
            query_url=_resolve_video_url(routes["query"], task_id),
        )
    except OSError:
        raise TaskRecoveryError(
            f"Task {task_id} was created but recovery could not be saved. "
            "Check disk access and the remote task before choosing Regenerate."
        ) from None


async def _query_video_task(
    session: aiohttp.ClientSession, url: str, headers: dict, deadline: float
) -> dict:
    for attempt in range(4):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Video task query timed out; rerun to resume.")
        try:
            return await run_interruptible(
                asyncio.wait_for(_request_json(session, "GET", url, headers), remaining)
            )
        except VideoHTTPError as exc:
            if exc.status != 429 and exc.status not in {500, 502, 503, 504}:
                raise
            if attempt == 3:
                raise
        except (
            asyncio.TimeoutError,
            TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
        ):
            if attempt == 3:
                raise
        delay = min(
            QUERY_RETRY_BASE_SECONDS * 2**attempt,
            max(0, deadline - asyncio.get_running_loop().time()),
        )
        await run_interruptible(asyncio.sleep(delay))
    raise RuntimeError("Video query retries exhausted.")


async def _resume_video_task(
    session: aiohttp.ClientSession,
    headers: dict,
    journal: TaskJournal,
    record: dict,
    request_body: dict | None = None,
) -> Path:
    if record["state"] == "creating":
        raise TaskRecoveryError(
            record.get("creation_error")
            or _creation_error_details(
                "Previous creation unconfirmed; no new request sent.",
                journal.idempotency_key or "not recorded",
                request_body or {},
                "not saved by previous version",
                headers,
            )
        )
    task_id = record["task_id"]
    query_url = _normalise_base_url(record["query_url"])
    if record["state"] == "failed":
        raise TaskRecoveryError(
            f"Task {task_id} has ended. Choose Regenerate to start another task."
        )
    output_file = record.get("output_file", "")
    if output_file and Path(output_file).name == output_file:
        output_path = Path(folder_paths.get_output_directory()) / output_file
        if output_path.is_file():
            if output_path.stat().st_size > 0 and await run_interruptible(
                asyncio.to_thread(_is_readable_video, output_path)
            ):
                return output_path
            output_path.unlink(missing_ok=True)
    deadline = asyncio.get_running_loop().time() + TASK_TIMEOUT_SECONDS
    while True:
        try:
            response = await _query_video_task(session, query_url, headers, deadline)
        except VideoHTTPError as exc:
            if exc.status in {404, 410}:
                journal.save(
                    "failed", task_id=task_id, query_url=query_url, status="missing"
                )
                raise TaskRecoveryError(
                    f"Task {task_id} is no longer available. Choose Regenerate to start another."
                ) from None
            raise
        task = _task_payload(response)
        status = str(task.get("status", "")).strip().lower()
        if status in TERMINAL_FAILURE_STATES:
            journal.save("failed", task_id=task_id, query_url=query_url, status=status)
            raise TaskRecoveryError(
                f"Task {task_id} ended with status '{status}'. Choose Regenerate to start another."
            )
        if status in {"succeeded", "success", "completed"}:
            journal.save("succeeded", task_id=task_id, query_url=query_url)
            video_url = _video_url_from_payload(task)
            if not video_url:
                raise RuntimeError(
                    f"Task {task_id} succeeded without a video URL; rerun to resume."
                )
            video_path = await run_interruptible(
                _download_video(session, video_url, task_id)
            )
            journal.save(
                "downloaded",
                task_id=task_id,
                query_url=query_url,
                output_file=video_path.name,
            )
            return video_path
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(
                f"Timed out waiting for task {task_id}; rerun to resume."
            )
        await run_interruptible(asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining)))


def _video_node_output(video_path: Path) -> IO.NodeOutput:
    return IO.NodeOutput(
        InputImpl.VideoFromFile(str(video_path)),
        ui=ui.PreviewVideo([ui.SavedResult(video_path.name, "", IO.FolderType.output)]),
    )


class BenpayVideo(IO.ComfyNode):
    @classmethod
    def fingerprint_inputs(cls, **_kwargs) -> float:
        # Always check local credentials/files; TaskJournal handles safe reuse.
        return float("nan")

    @classmethod
    def define_schema(cls) -> IO.Schema:
        return IO.Schema(
            node_id="BenpayVideo",
            display_name="BenPay Text to Video",
            category="video/BenPay",
            description="Create a video from text with BenPay.",
            search_aliases=["BenPay Video"],
            is_output_node=True,
            hidden=[IO.Hidden.unique_id, IO.Hidden.extra_pnginfo],
            inputs=[
                IO.Combo.Input(
                    "protocol",
                    options=VIDEO_PROTOCOL_OPTIONS,
                    default=DEFAULT_SELECTED_PROTOCOL,
                    tooltip="Choose a video provider.",
                ),
                IO.Combo.Input(
                    "model",
                    options=list(
                        dict.fromkeys(
                            product["model"]
                            for products in VIDEO_SUPPLIER_CATALOG.values()
                            for product in products
                        )
                    ),
                    default=_default_model(DEFAULT_SELECTED_PROTOCOL),
                    tooltip="Choose a model for the selected video provider.",
                ),
                IO.String.Input(
                    "prompt",
                    multiline=True,
                    dynamic_prompts=True,
                    default="",
                    tooltip="Text prompt sent to the selected BenPay video protocol.",
                ),
                IO.Combo.Input(
                    "resolution",
                    options=VIDEO_RESOLUTION_OPTIONS,
                    default=(
                        "768P"
                        if DEFAULT_SELECTED_PROTOCOL == "minimax"
                        else DEFAULT_VIDEO_RESOLUTION
                    ),
                    tooltip="Output resolution supported by the current BenPay upstream.",
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
                    options=["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"],
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
                IO.Int.Input(
                    "generation",
                    default=0,
                    min=0,
                    max=0x7FFFFFFF,
                    optional=True,
                    advanced=True,
                    tooltip="Keep unchanged to resume. Use Regenerate to start a new video.",
                ),
            ],
            outputs=[IO.Video.Output()],
        )

    @classmethod
    async def execute(
        cls,
        prompt: str,
        protocol: str = DEFAULT_SELECTED_PROTOCOL,
        model: str = "",
        resolution: str = DEFAULT_VIDEO_RESOLUTION,
        duration: int = 5,
        ratio: str = "16:9",
        seed: int = 42,
        generation: int = 0,
    ) -> IO.NodeOutput:
        return await _generate_video(
            protocol,
            model,
            prompt,
            resolution,
            duration,
            ratio,
            seed,
            recovery_scope=node_recovery_scope(cls.hidden),
            generation=generation,
        )


class BenpayVideoFromImages(IO.ComfyNode):
    @classmethod
    def fingerprint_inputs(cls, **_kwargs) -> float:
        # Always check local credentials/files; TaskJournal handles safe reuse.
        return float("nan")

    @classmethod
    def define_schema(cls) -> IO.Schema:
        return IO.Schema(
            node_id="BenpayVideoFromImages",
            display_name="BenPay Image to Video",
            category="video/BenPay",
            description="Create a video from images or reference videos with BenPay.",
            is_output_node=True,
            hidden=[IO.Hidden.unique_id, IO.Hidden.extra_pnginfo],
            inputs=[
                IO.Combo.Input(
                    "protocol",
                    options=VIDEO_PROTOCOL_OPTIONS,
                    default=DEFAULT_SELECTED_PROTOCOL,
                    tooltip="Choose a video provider.",
                ),
                IO.Combo.Input(
                    "model",
                    options=list(
                        dict.fromkeys(
                            product["model"]
                            for products in VIDEO_SUPPLIER_CATALOG.values()
                            for product in products
                        )
                    ),
                    default=_default_model(DEFAULT_SELECTED_PROTOCOL),
                    tooltip="Choose a model for the selected video provider.",
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
                    tooltip="Connect one image, two ordered frames, or reference images (Doubao: 1-4; MiniMax: 1-5).",
                ),
                IO.String.Input(
                    "reference_video_urls",
                    multiline=True,
                    default="",
                    tooltip="Doubao only: at most 4 HTTPS reference video URLs, one per line.",
                ),
                IO.String.Input(
                    "prompt",
                    multiline=True,
                    dynamic_prompts=True,
                    default="",
                    tooltip="Text prompt sent to the selected BenPay video protocol.",
                ),
                IO.Combo.Input(
                    "resolution",
                    options=VIDEO_RESOLUTION_OPTIONS,
                    default=(
                        "768P"
                        if DEFAULT_SELECTED_PROTOCOL == "minimax"
                        else DEFAULT_VIDEO_RESOLUTION
                    ),
                    tooltip="Output resolution supported by the current BenPay upstream.",
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
                IO.Int.Input(
                    "generation",
                    default=0,
                    min=0,
                    max=0x7FFFFFFF,
                    optional=True,
                    advanced=True,
                    tooltip="Keep unchanged to resume. Use Regenerate to start a new video.",
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
        model: str = "",
        resolution: str = DEFAULT_VIDEO_RESOLUTION,
        duration: int = 5,
        ratio: str = "adaptive",
        seed: int = 42,
        generation: int = 0,
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
            recovery_scope=node_recovery_scope(cls.hidden),
            generation=generation,
        )
