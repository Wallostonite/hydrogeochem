"""Cache port with a Redis adapter and an in-process fallback.

Upstream data is immutable once published, so caching is pure win: it removes the
USGS services from the critical path of every page render and keeps us well inside
their fair-use expectations.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Protocol

from ..logging import get_logger

log = get_logger(__name__)


class Cache(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any, ttl_s: int) -> None: ...


def cache_key(namespace: str, payload: dict[str, Any]) -> str:
    """Stable key from a canonicalised payload; ordering and whitespace never matter."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"hgc:{namespace}:{hashlib.sha256(blob.encode()).hexdigest()[:32]}"


class InMemoryCache:
    """Bounded LRU-ish cache. Used in tests and as the degraded mode when Redis is down."""

    def __init__(self, max_entries: int = 512) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max = max_entries

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.time():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        if len(self._data) >= self._max:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.time() + ttl_s, value)


class RedisCache:
    """Redis adapter. Cache failures are logged and swallowed: a cache is never load-bearing."""

    def __init__(self, url: str) -> None:
        import redis  # imported lazily so the domain stays importable without redis

        self._client = redis.Redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0)

    def ping(self) -> None:
        """Force a real connection. redis-py connects lazily, so construction alone never
        proves the server is reachable; build_cache calls this to decide on a fallback."""
        self._client.ping()

    def get(self, key: str) -> Any | None:
        try:
            raw = self._client.get(key)
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the request
            log.warning("cache_get_failed", extra={"error": str(exc)})
            return None
        return json.loads(raw) if raw else None

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        try:
            self._client.setex(key, ttl_s, json.dumps(value, default=str))
        except Exception as exc:  # noqa: BLE001
            log.warning("cache_set_failed", extra={"error": str(exc)})


def build_cache(redis_url: str | None) -> Cache:
    if not redis_url:
        return InMemoryCache()
    try:
        cache = RedisCache(redis_url)
        cache.ping()  # verify the server is actually reachable, not just the URL parseable
        return cache
    except Exception as exc:  # noqa: BLE001
        # No Redis (e.g. a single-container deploy)? Fall back to an in-process cache rather
        # than a Redis that silently fails every op, which is effectively no caching at all.
        log.warning("redis_unavailable_using_memory_cache", extra={"error": str(exc)})
        return InMemoryCache()
