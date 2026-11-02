"""Persist video task identities without request bodies or API key values."""

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from uuid import uuid4


class TaskRecoveryError(RuntimeError):
    """Recovery cannot safely continue or start another task."""


class VideoHTTPError(RuntimeError):
    def __init__(self, status: int, response: object = None) -> None:
        super().__init__(f"BenPay returned HTTP {status}.")
        self.status = status
        self.response = response


def fingerprint(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def node_recovery_scope(hidden: object) -> str:
    node_id = str(getattr(hidden, "unique_id", "") or "")
    metadata = getattr(hidden, "extra_pnginfo", None) or {}
    if not node_id or not isinstance(metadata, dict):
        return ""
    workflow = metadata.get("workflow", {})
    if not isinstance(workflow, dict):
        return ""
    for node in workflow.get("nodes", []):
        if str(node.get("id")) == node_id:
            identity = node.get("properties", {}).get("benpayRecoveryId")
            if identity:
                return fingerprint([workflow.get("id", ""), identity, node_id])
    if node_id and workflow.get("id"):
        return fingerprint([workflow["id"], node_id])
    return ""


class TaskJournal:
    """Persist only when a stable node scope is available."""

    def __init__(
        self,
        directory: Path,
        scope: str,
        api_key: str,
        request: dict,
        generation: int,
    ) -> None:
        if type(generation) is not int or generation < 0:
            raise TaskRecoveryError("Invalid task recovery identity or generation.")
        self.persistent = bool(scope)
        self.directory = directory
        self.identity = {
            "schemaVersion": 1,
            "scope": fingerprint(scope),
            "credential": fingerprint(api_key),
            "request": fingerprint(request),
            "generation": generation,
        }
        self.path = directory / f"{fingerprint(self.identity)}.json"
        self.idempotency_key = "benpay-video-" + (
            fingerprint(self.identity) if self.persistent else uuid4().hex
        )
        self.lock_path = (
            directory / f"{fingerprint([scope, self.identity['credential']])}.lock"
        )
        self.lock_file = None

    def __enter__(self) -> "TaskJournal":  # noqa: PYI034 - support Python 3.10
        if not self.persistent:
            return self
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise TaskRecoveryError("Task recovery directory must be a real directory.")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        self.lock_file = os.fdopen(descriptor, "r+b")
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise TaskRecoveryError("Invalid task lock file.")
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    self.lock_file.write(b"\0")
                    self.lock_file.flush()
                self.lock_file.seek(0)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            self.lock_file = None
            raise TaskRecoveryError(
                "This node already has a running task. Wait before running it again."
            ) from None
        except Exception:
            self.lock_file.close()
            self.lock_file = None
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        if self.lock_file is not None:
            # Closing the descriptor also releases the OS lock after interruption.
            self.lock_file.close()
            self.lock_file = None

    def load(self) -> dict | None:
        if not self.persistent:
            return None
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise TaskRecoveryError(
                    "Invalid task recovery file; no new task was created."
                )
            with self.path.open("rb") as stream:
                raw = stream.read(16385)
            value = json.loads(raw)
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError):
            raise TaskRecoveryError(
                "Task recovery file is corrupt; no new task was created."
            ) from None
        if not isinstance(value, dict) or any(
            value.get(key) != expected for key, expected in self.identity.items()
        ):
            raise TaskRecoveryError("Task recovery identity mismatch.")
        if "idempotency_key" in value:
            if value["idempotency_key"] != self.idempotency_key:
                raise TaskRecoveryError("Task recovery idempotency key mismatch.")
        else:
            # Older tasks were sent without a key; do not claim one retroactively.
            self.idempotency_key = None
        if value.get("state") not in {
            "creating",
            "pending",
            "succeeded",
            "downloaded",
            "failed",
        }:
            raise TaskRecoveryError("Invalid task recovery state.")
        if value["state"] != "creating" and (
            not isinstance(value.get("task_id"), str) or not value["task_id"]
        ):
            raise TaskRecoveryError("Task recovery record has no task ID.")
        if not isinstance(value.get("query_url"), str) or not value["query_url"]:
            raise TaskRecoveryError("Task recovery record has no query address.")
        if "output_file" in value and not isinstance(value["output_file"], str):
            raise TaskRecoveryError("Invalid recovered output filename.")
        if "creation_error" in value and not isinstance(value["creation_error"], str):
            raise TaskRecoveryError("Invalid recovered creation error.")
        return value

    def save(self, state: str, **fields: str) -> dict:
        if set(fields) - {
            "task_id",
            "query_url",
            "output_file",
            "status",
            "creation_error",
        }:
            raise TaskRecoveryError("Unexpected task recovery fields.")
        value = {**self.identity, "state": state, **fields}
        if self.idempotency_key is not None:
            value["idempotency_key"] = self.idempotency_key
        payload = (json.dumps(value, indent=2) + "\n").encode("utf-8")
        if len(payload) > 16384:
            raise TaskRecoveryError("Task recovery record is too large.")
        if not self.persistent:
            return value
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.directory, prefix=".task-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return value
