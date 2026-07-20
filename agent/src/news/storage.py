"""Atomic persistence for the single validated investment-news snapshot."""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.config.paths import get_data_dir
from src.news.models import NewsSnapshot


logger = logging.getLogger(__name__)
MAX_SNAPSHOT_BYTES = 2_000_000
_DISALLOWED_KEYS = frozenset({"api_key", "apikey", "authorization", "credential", "credentials", "password", "secret", "token", "content", "full_content", "raw_content"})


class SnapshotStorageError(RuntimeError):
    """A stable, non-sensitive error surfaced for snapshot I/O failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AtomicSnapshotStore:
    """Read and atomically replace the latest validated news snapshot."""

    def __init__(self, path: Path | None = None, *, max_bytes: int = MAX_SNAPSHOT_BYTES) -> None:
        self.path = path if path is not None else get_data_dir() / "news" / "latest.json"
        self.max_bytes = max_bytes

    def read(self) -> NewsSnapshot | None:
        """Return the current validated snapshot or a stable classification error."""
        try:
            path_stat = self.path.lstat()
        except FileNotFoundError as exc:
            raise SnapshotStorageError("snapshot_unavailable", "news snapshot is unavailable") from exc
        except OSError as exc:
            logger.warning("news snapshot read rejected: %s", type(exc).__name__)
            raise SnapshotStorageError("snapshot_corrupt", "news snapshot is corrupt") from exc
        if not stat.S_ISREG(path_stat.st_mode):
            raise SnapshotStorageError("snapshot_corrupt", "news snapshot is corrupt")

        fd: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags)
            opened_stat = os.fstat(fd)
            if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_size > self.max_bytes:
                raise ValueError("snapshot exceeds maximum size")
            with os.fdopen(fd, "rb") as stream:
                fd = None
                raw = stream.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise ValueError("snapshot exceeds maximum size")
            data = json.loads(raw)
            if _contains_disallowed_keys(data):
                raise ValueError("snapshot has disallowed key")
            return NewsSnapshot.model_validate(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            logger.warning("news snapshot read rejected: %s", type(exc).__name__)
            raise SnapshotStorageError("snapshot_corrupt", "news snapshot is corrupt") from exc
        finally:
            if fd is not None:
                os.close(fd)

    def write(self, snapshot: NewsSnapshot) -> None:
        """Durably replace the snapshot without risking an existing good file."""
        tmp_name: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            payload = _serialize(snapshot, self.max_bytes)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            try:
                os.fchmod(fd, 0o600)
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                    raise PermissionError("temporary snapshot file is not private")
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                os.close(fd) if _fd_is_open(fd) else None
                raise
            _validate_payload(payload, self.max_bytes)
            os.replace(tmp_name, self.path)
            tmp_name = None
            try:
                self._fsync_directory()
            except OSError:
                logger.debug("news snapshot directory sync failed after commit", exc_info=True)
        except (OSError, ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("news snapshot write rejected: %s", type(exc).__name__)
            raise SnapshotStorageError("snapshot_write_failed", "news snapshot could not be saved") from exc
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("news snapshot temporary cleanup failed")

    def _fsync_directory(self) -> None:
        try:
            fd = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            logger.debug("news snapshot directory fsync unsupported", exc_info=True)
        finally:
            try:
                os.close(fd)
            except OSError:
                logger.debug("news snapshot directory close failed", exc_info=True)


def _serialize(snapshot: NewsSnapshot, max_bytes: int) -> bytes:
    payload = json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _validate_payload(payload, max_bytes)
    return payload


def _validate_payload(payload: bytes, max_bytes: int) -> None:
    if len(payload) > max_bytes:
        raise ValueError("snapshot exceeds maximum size")
    data = json.loads(payload)
    if _contains_disallowed_keys(data):
        raise ValueError("snapshot has disallowed key")
    NewsSnapshot.model_validate(data)


def _contains_disallowed_keys(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in _DISALLOWED_KEYS or _contains_disallowed_keys(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_disallowed_keys(item) for item in value)
    return False


def _fd_is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True
