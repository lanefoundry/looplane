"""Versioned, single-flight, disk-backed cache for startup scans.

Startup "scans" (network/model discovery, capability probes) must not re-run on
every invocation. Results are cached to a versioned JSON file keyed by a
caller-supplied config hash.

- **Versioned**: a ``version`` string is embedded in every entry; a schema
  change invalidates all old entries without manual cache clearing.
- **Single-flight**: concurrent calls for the same key share one in-flight
  computation (per-process lock), preventing a stampede / race.
- **Safe**: a missing, expired, or corrupt entry triggers ``compute()`` and is
  never backfilled with a failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

CACHE_SCHEMA_VERSION = "v1"

_MISS = object()

_REGISTRY_LOCK = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    path = root / "rivumi" / "startup"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"


def _lock_for(key: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _load(key: str, version: str, ttl_seconds: float | None) -> object:
    path = _cache_dir() / _safe_filename(key)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return _MISS
    try:
        data = json.loads(raw)
    except (ValueError, OSError):
        return _MISS
    if not isinstance(data, dict) or data.get("version") != version:
        return _MISS
    if "value" not in data:
        return _MISS
    if ttl_seconds is not None:
        try:
            if time.time() - float(data.get("ts", 0.0)) > ttl_seconds:
                return _MISS
        except (TypeError, ValueError):
            return _MISS
    return data["value"]


def _store(key: str, version: str, value: object) -> None:
    path = _cache_dir() / _safe_filename(key)
    payload = json.dumps(
        {"version": version, "ts": time.time(), "value": value},
        separators=(",", ":"),
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def cached_scan(
    key: str,
    version: str,
    compute: Callable[[], T],
    *,
    ttl_seconds: float | None = 300.0,
) -> T:
    """Return a cached ``compute()`` result keyed by ``key`` + ``version``.

    Single-flight: concurrent calls for the same key share one computation. A
    missing/expired/corrupt entry triggers ``compute()`` and caches the result.
    ``compute`` failures propagate and are never written to the cache.
    """

    cache_key = f"{version}:{key}"
    with _lock_for(cache_key):
        hit = _load(cache_key, version, ttl_seconds)
        if hit is not _MISS:
            return hit  # type: ignore[return-value]
        value = compute()
        _store(cache_key, version, value)
        return value


def read_entry(key: str, version: str) -> tuple[float, object] | None:
    """Read one entry ignoring its TTL -- the stale-while-revalidate escape hatch.

    Returns ``(fetched_at_epoch, value)`` so callers can decide staleness
    themselves (show stale data instantly, refresh in the background). ``None``
    when the entry is missing, corrupt, or written under a different version.
    """

    cache_key = f"{version}:{key}"
    with _lock_for(cache_key):
        path = _cache_dir() / _safe_filename(cache_key)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        data = json.loads(raw)
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict) or data.get("version") != version:
        return None
    if "value" not in data:
        return None
    try:
        fetched_at = float(data.get("ts", 0.0))
    except (TypeError, ValueError):
        fetched_at = 0.0
    return fetched_at, data["value"]


def write_entry(key: str, version: str, value: object) -> None:
    """Store one entry; I/O failures are swallowed like ``cached_scan``."""

    cache_key = f"{version}:{key}"
    with _lock_for(cache_key):
        _store(cache_key, version, value)
