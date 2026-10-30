"""Local configuration endpoints; responses never contain API Key values."""

import asyncio
import json
from urllib.parse import urlsplit

from aiohttp import web

from .configuration import ConfigurationError, configuration_status, save_api_key
from .localization import translate

MAX_REQUEST_BYTES = 16 * 1024


async def get_configuration_status(_request: web.Request) -> web.Response:
    return _json_response(configuration_status())


async def get_video_models(_request: web.Request) -> web.Response:
    from .nodes import VIDEO_SUPPLIER_CATALOG

    return _json_response(VIDEO_SUPPLIER_CATALOG)


async def save_configuration(request: web.Request) -> web.Response:
    origin = request.headers.get("Origin")
    try:
        different_origin = origin is not None and (
            urlsplit(origin).scheme not in ("http", "https")
            or urlsplit(origin).netloc.lower() != request.host.lower()
        )
    except ValueError:
        different_origin = True
    if different_origin or request.headers.get("Sec-Fetch-Site") == "cross-site":
        return _error_response(request, "requestOrigin", 403)
    if request.content_type != "application/json":
        return _error_response(request, "requestJson", 415)
    body = bytearray()
    async for chunk in request.content.iter_chunked(4096):
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BYTES:
            return _error_response(request, "requestTooLarge", 413)
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return _error_response(request, "requestInvalid", 400)
    if not isinstance(value, dict) or set(value) != {"apiKey"}:
        return _error_response(request, "requestFields", 400)
    try:
        await asyncio.to_thread(save_api_key, value["apiKey"])
    except ConfigurationError as exc:
        return _error_response(request, exc.code, 400)
    return _json_response(configuration_status())


def _error_response(request: web.Request, code: str, status: int) -> web.Response:
    locale = request.headers.get("Accept-Language", "en")
    return _json_response({"error": translate(code, locale), "code": code}, status)


def _json_response(value: dict, status: int = 200) -> web.Response:
    return web.json_response(
        value, status=status, headers={"Cache-Control": "no-store"}
    )


def register_configuration_routes(routes: web.RouteTableDef) -> None:
    routes.get("/benpay-video/models")(get_video_models)
    routes.get("/benpay-video/config-status")(get_configuration_status)
    routes.post("/benpay-video/config")(save_configuration)
