from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from typing import Any


class CacheManager:
    """Lightweight in-memory cache and sliding-window rate limiter for single-process free-tier deployments."""

    def __init__(self, default_ttl: int = 300) -> None:
        self.default_ttl = default_ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        self._rate_windows: dict[str, deque[float]] = defaultdict(deque)

    async def connect(self) -> None:
        pass

    async def ping(self) -> dict[str, Any]:
        return {"mode": "in_memory", "cached_keys": len(self._cache)}

    async def get(self, key: str) -> str | None:
        item = self._cache.get(key)
        if item:
            expiry, value = item
            if expiry == 0 or expiry > time.time():
                return str(value) if value is not None else None
            del self._cache[key]
        return None

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        expire_seconds = ttl if ttl is not None else self.default_ttl
        expiry = time.time() + expire_seconds if expire_seconds > 0 else 0
        self._cache[key] = (expiry, value)

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def set_json(self, key: str, data: Any, ttl: int | None = None) -> None:
        raw = json.dumps(data)
        await self.set(key, raw, ttl=ttl)

    async def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    async def delete_pattern(self, pattern: str) -> None:
        prefix = pattern.rstrip("*")
        matching = [k for k in self._cache if k.startswith(prefix)]
        for k in matching:
            self._cache.pop(k, None)

    async def check_rate_limit(self, key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int, float]:
        now = time.time()
        window = self._rate_windows[key]
        while window and window[0] <= now - window_seconds:
            window.popleft()
        if len(window) >= limit:
            retry_after = window_seconds - (now - window[0]) if window else float(window_seconds)
            return False, 0, max(1.0, retry_after)
        window.append(now)
        return True, limit - len(window), 0.0

    async def close(self) -> None:
        self._cache.clear()
        self._rate_windows.clear()
