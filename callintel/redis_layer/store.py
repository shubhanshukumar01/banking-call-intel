"""Redis-backed cache, distributed lock, processing manifest and daily metrics."""
from __future__ import annotations

import asyncio
import secrets
from typing import Any

import redis.asyncio as aioredis

from ..utils import dumps, loads, now_iso, today
from .client import Keys

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end
"""


class JsonCache:
    def __init__(self, r: aioredis.Redis) -> None:
        self.r = r

    async def get(self, key: str) -> Any | None:
        raw = await self.r.get(key)
        return None if raw is None else loads(raw)

    async def set(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        await self.r.set(key, dumps(value), ex=ttl_s)


class RedisLock:
    """SET NX PX lock with token-checked release (safe against expiry races)."""

    def __init__(self, r: aioredis.Redis, key: str, ttl_ms: int = 1_800_000) -> None:
        self.r, self.key, self.ttl_ms = r, key, ttl_ms
        self.token = secrets.token_hex(8)
        self.acquired = False

    async def acquire(self) -> bool:
        self.acquired = bool(await self.r.set(self.key, self.token, nx=True, px=self.ttl_ms))
        return self.acquired

    async def release(self) -> None:
        if not self.acquired:
            return
        try:
            await self.r.eval(_RELEASE_LUA, 1, self.key, self.token)
        except Exception:
            # Lua unavailable (fakeredis without lupa): best-effort compare + delete
            if await self.r.get(self.key) == self.token:
                await self.r.delete(self.key)
        self.acquired = False

    async def __aenter__(self) -> "RedisLock":
        await self.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.release()


class Manifest:
    """Per-file processing state keyed by content hash -> idempotent, resumable runs."""

    STATUSES = ("queued", "processing", "done", "needs_review", "failed", "dead_lettered")

    def __init__(self, r: aioredis.Redis, keys: Keys) -> None:
        self.r, self.k = r, keys

    async def register(self, sha: str, file: str, force: bool = False) -> bool:
        """Returns True if the file should be queued (new, or forced, or previously failed)."""
        key = self.k.manifest(sha)
        status = await self.r.hget(key, "status")
        if status in ("queued", "processing", "done", "needs_review") and not force:
            return False
        await self.r.hset(key, mapping={"status": "queued", "file": file, "queued_at": now_iso()})
        await self.r.zadd(self.k.manifest_index, {sha: asyncio.get_running_loop().time()})
        return True

    async def set(self, sha: str, status: str, **fields: Any) -> None:
        mapping = {"status": status, f"{status}_at": now_iso()}
        mapping.update({k: (v if isinstance(v, (str, int, float)) else dumps(v)) for k, v in fields.items()
                        if v is not None})
        await self.r.hset(self.k.manifest(sha), mapping=mapping)

    async def get(self, sha: str) -> dict[str, str]:
        return await self.r.hgetall(self.k.manifest(sha))

    async def counts(self, limit: int = 100_000) -> dict[str, int]:
        shas = await self.r.zrange(self.k.manifest_index, 0, limit)
        counts: dict[str, int] = {}
        if not shas:
            return counts
        pipe = self.r.pipeline(transaction=False)
        for s in shas:
            pipe.hget(self.k.manifest(s), "status")
        for st in await pipe.execute():
            counts[st or "unknown"] = counts.get(st or "unknown", 0) + 1
        return counts


class Metrics:
    def __init__(self, r: aioredis.Redis, keys: Keys) -> None:
        self.r, self.k = r, keys

    async def incr(self, **counters: float) -> None:
        key = self.k.stats(today())
        pipe = self.r.pipeline(transaction=False)
        for name, val in counters.items():
            if val:
                pipe.hincrbyfloat(key, name, float(val))
        pipe.expire(key, 90 * 86400)
        await pipe.execute()

    async def get(self, day: str | None = None) -> dict[str, float]:
        raw = await self.r.hgetall(self.k.stats(day or today()))
        return {k: float(v) for k, v in raw.items()}
