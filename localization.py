"""Read the same locale resources ComfyUI uses for custom-node translations."""

import json
from functools import lru_cache
from pathlib import Path

LOCALES = Path(__file__).resolve().parent / "locales"


def normalize_locale(value: str) -> str:
    locale = value.split(",", 1)[0].split(";", 1)[0].strip().replace("_", "-").lower()
    if locale.startswith(("zh-tw", "zh-hk", "zh-hant")):
        return "zh-TW"
    if locale == "zh" or locale.startswith("zh-"):
        return "zh"
    return "en"


@lru_cache(maxsize=3)
def _messages(locale: str) -> dict[str, str]:
    try:
        value = json.loads((LOCALES / locale / "main.json").read_text(encoding="utf-8"))
        return value["benpayVideo"]
    except (OSError, ValueError, KeyError):
        return {}


def translate(code: str, locale: str = "en") -> str:
    return (
        _messages(normalize_locale(locale)).get(code)
        or _messages("en").get(code)
        or "Could not update the local configuration."
    )
