"""Bounded, integrity-checked checkpoints for supported worker state only."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid
from typing import Any, Callable, Iterator, Mapping


SCHEMA_VERSION = 1
SUPPORTED_STATE_KEYS = frozenset({"counter", "accumulator"})
MAX_STATE_INTEGER = 2**63 - 1


class CheckpointError(RuntimeError):
    """Base error for checkpoint operations."""


class CheckpointRejectedError(CheckpointError):
    """A malformed, incompatible, corrupt, or oversized checkpoint was rejected."""


class CheckpointBudgetError(CheckpointError):
    """The storage budget cannot accommodate a checkpoint."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def validate_application_state(state: object) -> dict[str, int]:
    if not isinstance(state, dict) or set(state) != SUPPORTED_STATE_KEYS:
        raise CheckpointRejectedError(
            "application state must contain exactly counter and accumulator"
        )
    validated: dict[str, int] = {}
    for name in SUPPORTED_STATE_KEYS:
        value = state[name]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= MAX_STATE_INTEGER
        ):
            raise CheckpointRejectedError(
                f"application state {name} must be a non-negative signed 64-bit integer"
            )
        validated[name] = value
    return validated


class CheckpointManager:
    """Maintain a byte-bounded store with oldest-created-first eviction.

    Active restores are protected from eviction. Checkpoint files use strict
    JSON, a schema version, and SHA-256 over canonical schema fields. Writes use
    a same-directory temporary file, fsync, and atomic replace.
    """

    def __init__(self, storage_dir: str | Path, max_storage_bytes: int) -> None:
        if (
            not isinstance(max_storage_bytes, int)
            or isinstance(max_storage_bytes, bool)
            or max_storage_bytes <= 0
        ):
            raise ValueError("max_storage_bytes must be a positive integer")
        self.storage_dir = Path(storage_dir)
        self.max_storage_bytes = max_storage_bytes
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active_restores: set[str] = set()
        self._events: list[dict[str, Any]] = []

    @staticmethod
    def _filename(checkpoint_id: str) -> str:
        try:
            parsed = uuid.UUID(checkpoint_id)
        except (ValueError, AttributeError) as error:
            raise CheckpointRejectedError("invalid checkpoint ID") from error
        if str(parsed) != checkpoint_id:
            raise CheckpointRejectedError("invalid checkpoint ID")
        return f"{checkpoint_id}.json"

    def _event(self, event: str, **fields: Any) -> None:
        self._events.append(
            {
                "type": "checkpoint_event",
                "event": event,
                "occurred_at_utc": datetime.now(timezone.utc).isoformat(),
                **fields,
            }
        )

    def drain_events(self) -> list[dict[str, Any]]:
        with self._lock:
            events = self._events
            self._events = []
            return events

    def _checkpoint_files(self) -> list[Path]:
        return sorted(self.storage_dir.glob("*.json"))

    def storage_usage_bytes(self) -> int:
        with self._lock:
            return sum(path.stat().st_size for path in self._checkpoint_files())

    def _protected_id(self, path: Path) -> str | None:
        try:
            checkpoint_id = path.stem
            self._filename(checkpoint_id)
            return checkpoint_id
        except CheckpointRejectedError:
            return None

    def _eviction_order(self) -> list[Path]:
        def order(path: Path) -> tuple[str, str]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                created_at = data.get("created_at_utc")
                if isinstance(created_at, str):
                    return created_at, path.name
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
            return "", path.name

        return sorted(self._checkpoint_files(), key=order)

    def _validate_document(
        self,
        document: object,
        expected_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(document, dict):
            raise CheckpointRejectedError("checkpoint must be a JSON object")
        expected_fields = {
            "schema_version",
            "checkpoint_id",
            "worker_id",
            "worker_pid",
            "created_at_utc",
            "application_state",
            "integrity_sha256",
        }
        if set(document) != expected_fields:
            raise CheckpointRejectedError("checkpoint fields do not match schema version 1")
        if (
            not isinstance(document["schema_version"], int)
            or isinstance(document["schema_version"], bool)
            or document["schema_version"] != SCHEMA_VERSION
        ):
            raise CheckpointRejectedError(
                f"unsupported checkpoint schema version: {document['schema_version']}"
            )
        checkpoint_id = document["checkpoint_id"]
        if not isinstance(checkpoint_id, str):
            raise CheckpointRejectedError("checkpoint ID must be a string")
        self._filename(checkpoint_id)
        if expected_id is not None and checkpoint_id != expected_id:
            raise CheckpointRejectedError("checkpoint ID does not match filename")
        worker_id = document["worker_id"]
        worker_pid = document["worker_pid"]
        if not isinstance(worker_id, str) or not worker_id:
            raise CheckpointRejectedError("worker_id must be a non-empty string")
        if (
            not isinstance(worker_pid, int)
            or isinstance(worker_pid, bool)
            or worker_pid <= 0
        ):
            raise CheckpointRejectedError("worker_pid must be a positive integer")
        created_at = document["created_at_utc"]
        if not isinstance(created_at, str):
            raise CheckpointRejectedError("created_at_utc must be a string")
        try:
            parsed_time = datetime.fromisoformat(created_at)
        except ValueError as error:
            raise CheckpointRejectedError("created_at_utc is invalid") from error
        if parsed_time.tzinfo is None:
            raise CheckpointRejectedError("created_at_utc must include a timezone")
        application_state = validate_application_state(
            document["application_state"]
        )
        integrity = document["integrity_sha256"]
        if not isinstance(integrity, str) or len(integrity) != 64:
            raise CheckpointRejectedError("integrity hash is invalid")
        body = {key: document[key] for key in expected_fields if key != "integrity_sha256"}
        expected_hash = hashlib.sha256(_canonical_json(body)).hexdigest()
        if not hmac.compare_digest(integrity, expected_hash):
            raise CheckpointRejectedError("checkpoint integrity verification failed")
        return {**body, "application_state": application_state, "integrity_sha256": integrity}

    def create_checkpoint(
        self,
        worker_id: str,
        worker_pid: int,
        application_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            try:
                if not isinstance(worker_id, str) or not worker_id:
                    raise CheckpointRejectedError("worker_id must be a non-empty string")
                if (
                    not isinstance(worker_pid, int)
                    or isinstance(worker_pid, bool)
                    or worker_pid <= 0
                ):
                    raise CheckpointRejectedError("worker_pid must be a positive integer")
                try:
                    state = validate_application_state(dict(application_state))
                except (TypeError, ValueError) as error:
                    if isinstance(error, CheckpointRejectedError):
                        raise
                    raise CheckpointRejectedError(
                        f"application state is invalid: {error}"
                    ) from error
                body: dict[str, Any] = {
                    "schema_version": SCHEMA_VERSION,
                    "checkpoint_id": str(uuid.uuid4()),
                    "worker_id": worker_id,
                    "worker_pid": worker_pid,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "application_state": state,
                }
                document = {
                    **body,
                    "integrity_sha256": hashlib.sha256(
                        _canonical_json(body)
                    ).hexdigest(),
                }
                payload = _canonical_json(document) + b"\n"
                if len(payload) > self.max_storage_bytes:
                    raise CheckpointBudgetError(
                        f"checkpoint size {len(payload)} exceeds storage budget "
                        f"{self.max_storage_bytes}"
                    )

                evictions: list[Path] = []
                usage = self.storage_usage_bytes()
                for path in self._eviction_order():
                    checkpoint_id = self._protected_id(path)
                    if usage + len(payload) <= self.max_storage_bytes:
                        break
                    if checkpoint_id in self._active_restores:
                        continue
                    evictions.append(path)
                    usage -= path.stat().st_size
                if usage + len(payload) > self.max_storage_bytes:
                    raise CheckpointBudgetError(
                        "storage budget cannot be met without evicting an active restore"
                    )

                fd, temporary_name = tempfile.mkstemp(
                    prefix=".checkpoint-", suffix=".tmp", dir=self.storage_dir
                )
                temporary_path = Path(temporary_name)
                final_path = self.storage_dir / f"{body['checkpoint_id']}.json"
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(payload)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary_path, final_path)
                    for old_path in evictions:
                        old_path.unlink()
                    directory_fd = os.open(self.storage_dir, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    try:
                        final_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
                finally:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass

                result = {
                    **document,
                    "size_bytes": len(payload),
                    "storage_usage_bytes": self.storage_usage_bytes(),
                }
                for old_path in evictions:
                    self._event(
                        "evicted",
                        checkpoint_id=old_path.stem,
                        policy="oldest_created_first",
                    )
                self._event(
                    "created",
                    checkpoint_id=body["checkpoint_id"],
                    worker_id=worker_id,
                    worker_pid=worker_pid,
                    size_bytes=len(payload),
                    storage_usage_bytes=result["storage_usage_bytes"],
                )
                return result
            except CheckpointError as error:
                self._event(
                    "rejected",
                    worker_id=worker_id,
                    worker_pid=worker_pid,
                    reason=str(error),
                )
                raise
            except OSError as error:
                self._event(
                    "rejected",
                    worker_id=worker_id,
                    worker_pid=worker_pid,
                    reason=f"I/O error: {error}",
                )
                raise CheckpointError(f"checkpoint write failed: {error}") from error

    def read_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        with self._lock:
            return self._read_checkpoint_unlocked(
                checkpoint_id, self.storage_usage_bytes()
            )

    def _read_checkpoint_unlocked(
        self, checkpoint_id: str, storage_usage_bytes: int
    ) -> dict[str, Any]:
        try:
            filename = self._filename(checkpoint_id)
            path = self.storage_dir / filename
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as error:
                raise CheckpointRejectedError(
                    f"checkpoint {checkpoint_id} does not exist"
                ) from error
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CheckpointRejectedError(
                    f"checkpoint cannot be read: {error}"
                ) from error
            validated = self._validate_document(document, checkpoint_id)
            try:
                validated["size_bytes"] = path.stat().st_size
            except OSError as error:
                raise CheckpointRejectedError(
                    f"checkpoint size cannot be read: {error}"
                ) from error
            validated["storage_usage_bytes"] = storage_usage_bytes
            return validated
        except CheckpointRejectedError as error:
            self._event(
                "rejected",
                checkpoint_id=checkpoint_id,
                reason=str(error),
            )
            raise

    @contextmanager
    def _active_restore(self, checkpoint_id: str) -> Iterator[None]:
        self._active_restores.add(checkpoint_id)
        try:
            yield
        finally:
            self._active_restores.discard(checkpoint_id)

    def restore_checkpoint(
        self,
        checkpoint_id: str,
        worker_id: str,
        restore_state: Callable[[dict[str, int]], Any],
    ) -> dict[str, Any]:
        with self._lock:
            with self._active_restore(checkpoint_id):
                document = self.read_checkpoint(checkpoint_id)
                if document["worker_id"] != worker_id:
                    self._event(
                        "rejected",
                        checkpoint_id=checkpoint_id,
                        worker_id=worker_id,
                        reason="checkpoint belongs to a different worker",
                    )
                    raise CheckpointRejectedError(
                        "checkpoint belongs to a different worker"
                    )
                try:
                    restored_state = restore_state(document["application_state"])
                except Exception as error:
                    self._event(
                        "rejected",
                        checkpoint_id=checkpoint_id,
                        worker_id=worker_id,
                        reason=f"worker rejected application state: {error}",
                    )
                    raise CheckpointRejectedError(
                        f"worker rejected application state: {error}"
                    ) from error
                self._event(
                    "restored",
                    checkpoint_id=checkpoint_id,
                    worker_id=worker_id,
                    worker_pid=document["worker_pid"],
                    restored_pid=(
                        restored_state[0]
                        if isinstance(restored_state, tuple)
                        else None
                    ),
                )
                return document

    def list_checkpoints(self) -> list[dict[str, Any]]:
        checkpoints = []
        with self._lock:
            storage_usage_bytes = self.storage_usage_bytes()
            for path in self._checkpoint_files():
                checkpoint_id = path.stem
                try:
                    checkpoints.append(
                        self._read_checkpoint_unlocked(
                            checkpoint_id, storage_usage_bytes
                        )
                    )
                except CheckpointRejectedError:
                    continue
        return checkpoints
