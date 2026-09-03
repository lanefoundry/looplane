from __future__ import annotations

import json

import pytest

from looplane.startup_cache import (
    CACHE_SCHEMA_VERSION,
    _cache_dir,
    _safe_filename,
    cached_scan,
)


def test_cached_scan_computes_once_within_process(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    calls = []

    def compute():
        calls.append(1)
        return ("a", "b")

    first = cached_scan("k", CACHE_SCHEMA_VERSION, compute)
    second = cached_scan("k", CACHE_SCHEMA_VERSION, compute)
    assert tuple(first) == tuple(second) == ("a", "b")
    assert calls == [1]


def test_different_key_recomputes(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    calls = []

    def compute():
        calls.append(1)
        return 1

    cached_scan("k1", CACHE_SCHEMA_VERSION, compute)
    cached_scan("k2", CACHE_SCHEMA_VERSION, compute)
    assert calls == [1, 1]


def test_version_change_invalidates(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    calls = []

    def compute():
        calls.append(1)
        return "v"

    cached_scan("k", "v1", compute)
    # same key, different version -> miss
    cached_scan("k", "v2", compute)
    assert calls == [1, 1]


def test_ttl_expiry_recomputes(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    calls = []

    def compute():
        calls.append(1)
        return "v"

    cached_scan("k", CACHE_SCHEMA_VERSION, compute, ttl_seconds=0)
    # ttl_seconds=0 -> always expired
    cached_scan("k", CACHE_SCHEMA_VERSION, compute, ttl_seconds=0)
    assert calls == [1, 1]


def test_corrupt_cache_is_not_backfilled(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # Write a corrupt entry directly.
    path = _cache_dir() / _safe_filename(f"{CACHE_SCHEMA_VERSION}:k")
    path.write_text("{not valid json", encoding="utf-8")

    calls = []

    def compute():
        calls.append(1)
        return "ok"

    assert cached_scan("k", CACHE_SCHEMA_VERSION, compute) == "ok"
    assert calls == [1]


def test_compute_failure_is_not_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    calls = []

    def compute():
        calls.append(1)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cached_scan("k", CACHE_SCHEMA_VERSION, compute)
    with pytest.raises(RuntimeError):
        cached_scan("k", CACHE_SCHEMA_VERSION, compute)
    # two separate computations, failure never written to disk
    assert calls == [1, 1]
    path = _cache_dir() / _safe_filename(f"{CACHE_SCHEMA_VERSION}:k")
    assert not path.exists()


def test_single_flight_shares_one_computation(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    import threading

    started = threading.Event()
    release = threading.Event()
    counter = {"n": 0}

    def compute():
        with threading.Lock():
            counter["n"] += 1
        started.wait()  # hold so both threads enter the lock window
        release.wait()
        return "x"

    def work():
        cached_scan("k", CACHE_SCHEMA_VERSION, compute)

    t1 = threading.Thread(target=work)
    t2 = threading.Thread(target=work)
    t1.start()
    t2.start()
    # Give both threads time to reach the lock, then release.
    started.set()
    release.set()
    t1.join()
    t2.join()
    assert counter["n"] == 1


def test_cache_file_roundtrips_value(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cached_scan("k", CACHE_SCHEMA_VERSION, lambda: [1, 2, 3])
    path = _cache_dir() / _safe_filename(f"{CACHE_SCHEMA_VERSION}:k")
    data = json.loads(path.read_text())
    assert data["version"] == CACHE_SCHEMA_VERSION
    assert data["value"] == [1, 2, 3]


def test_cache_file_has_private_permissions(monkeypatch, tmp_path):
    import stat

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cached_scan("k", CACHE_SCHEMA_VERSION, lambda: "secret")
    path = _cache_dir() / _safe_filename(f"{CACHE_SCHEMA_VERSION}:k")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_cache_dir_has_private_permissions(monkeypatch, tmp_path):
    import stat

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cached_scan("k", CACHE_SCHEMA_VERSION, lambda: "v")
    cache = _cache_dir()
    assert stat.S_IMODE(cache.stat().st_mode) & 0o077 == 0


def test_eviction_removes_oldest_entries(monkeypatch, tmp_path):
    from looplane.startup_cache import _MAX_CACHE_ENTRIES

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for i in range(_MAX_CACHE_ENTRIES + 5):
        cached_scan(f"k{i}", CACHE_SCHEMA_VERSION, lambda: i)
    entries = list(_cache_dir().glob("*.json"))
    assert len(entries) <= _MAX_CACHE_ENTRIES
