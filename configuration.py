"""Read local node credentials without exposing them to workflows."""

import json
import os
import stat
import tempfile
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
MAX_CONFIG_BYTES = 1024 * 1024
MAX_KEY_BYTES = 4096
CONFIG_LOCK = threading.Lock()


class ConfigurationError(ValueError):
    """The on-disk configuration cannot be used for a video request."""

    def __init__(self, message: str, code: str = "configurationError") -> None:
        super().__init__(message)
        self.code = code


def _read_configuration(config_path: Path, allow_missing: bool = False) -> dict:
    try:
        info = config_path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ConfigurationError(
                "BenPay config.json 必须是普通文件，不能是链接。", "configFileType"
            )
        with config_path.open("rb") as config_file:
            raw = config_file.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError as exc:
        if allow_missing:
            return {}
        raise ConfigurationError(
            "BenPay 尚未配置，请点击节点的“配置 API Key”，或运行 comfyui-setup。",
            "configMissing",
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            "无法读取 BenPay config.json，请检查文件权限。", "configRead"
        ) from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigurationError("BenPay config.json 超出大小限制。", "configTooLarge")
    try:
        config = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ConfigurationError(
            "BenPay config.json 格式无效，请重新运行安装脚本。", "configInvalid"
        ) from exc
    if not isinstance(config, dict):
        raise ConfigurationError(
            "BenPay config.json 顶层必须是 JSON 对象。", "configObject"
        )
    version = config.get("schemaVersion", 1 if allow_missing else None)
    if type(version) is not int or version != 1:
        raise ConfigurationError(
            "BenPay config.json 版本不受支持，请更新节点或重新配置。", "configVersion"
        )
    if "apiKey" in config and not isinstance(config["apiKey"], str):
        raise ConfigurationError(
            "BenPay config.json 的 apiKey 必须是字符串。", "configKeyType"
        )
    return config


def _validate_api_key(api_key: object) -> str:
    if not isinstance(api_key, str) or not api_key.strip():
        raise ConfigurationError(
            "BenPay API Key 未配置，请点击节点的“配置 API Key”。", "keyMissing"
        )
    try:
        size = len(api_key.encode("utf-8"))
    except UnicodeError:
        raise ConfigurationError(
            "BenPay API Key 格式无效，请重新配置。", "keyInvalid"
        ) from None
    if size > MAX_KEY_BYTES:
        raise ConfigurationError("BenPay API Key 超出长度限制。", "keyTooLarge")
    if any(character in api_key for character in ("\r", "\n", "\x00")):
        raise ConfigurationError("BenPay API Key 格式无效，请重新配置。", "keyInvalid")
    return api_key.strip()


def read_api_key(config_path: Path | None = None) -> str:
    """Load the key for each task so configuration changes take effect immediately."""
    config = _read_configuration(config_path or CONFIG_PATH)
    return _validate_api_key(config.get("apiKey"))


def save_api_key(api_key: object, config_path: Path | None = None) -> None:
    """Save only local configuration, never a workflow or CLI credential bundle."""
    key = _validate_api_key(api_key)
    path = config_path or CONFIG_PATH
    with CONFIG_LOCK:
        config = _read_configuration(path, allow_missing=True)
        config.update(schemaVersion=1, apiKey=key)
        payload = (json.dumps(config, indent=2) + "\n").encode("utf-8")
        if len(payload) > MAX_CONFIG_BYTES:
            raise ConfigurationError(
                "BenPay config.json 超出大小限制。", "configTooLarge"
            )
        _replace_configuration(path, payload)


def _replace_configuration(path: Path, payload: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".config.json.tmp-", dir=path.parent, delete=False
        ) as config_file:
            temporary = Path(config_file.name)
            config_file.write(payload)
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temporary, path)
    except OSError:
        raise ConfigurationError(
            "保存 BenPay config.json 失败，请检查节点目录权限后重试。", "configSave"
        ) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def configuration_status() -> dict[str, object]:
    """Return only non-sensitive configuration information for the UI."""
    try:
        api_key = read_api_key()
    except ConfigurationError:
        return {"configured": False, "suffix": ""}
    # Never reveal an entire short key, even through a supposedly masked value.
    return {"configured": True, "suffix": api_key[-4:] if len(api_key) > 8 else ""}
