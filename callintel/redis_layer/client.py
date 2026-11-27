"""Redis connection + central keyspace definition.

Keyspace (prefix "ci" by default):
    ci:stream:ingest          Redis Stream - file jobs (consumer group "workers")
    ci:stream:dlq             dead-letter stream (max attempts exceeded / egress blocked)
    ci:stream:review          human-review queue (low confidence / unverifiable claims)
    ci:stream:norm_review     unresolved product mentions for alias curation
    ci:manifest:{sha}         hash - per-file processing state (idempotency)
    ci:lock:{sha}             distributed lock while a file is processed
    ci:llm:{hash}             exact-match LLM response cache (content addressed)
    ci:emb:{model}:{hash}     embedding cache
    ci:alias                  hash - learned alias -> canonical product (tier-1 cache)
    ci:rl:{bucket}            token-bucket rate limiter state (RPM / TPM)
    ci:vault:{call_id}        hash - encrypted surrogate -> original PII (optional, TTL)
    ci:stats:{yyyy-mm-dd}     hash - daily counters (files, llm calls, tokens, cache hits)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis

from ..config import Settings

log = logging.getLogger(__name__)
_FAKE_SERVER: Any = None


class Keys:
    def __init__(self, prefix: str = "ci") -> None:
        p = prefix
        self.prefix = p
        self.stream_ingest = f"{p}:stream:ingest"
        self.stream_dlq = f"{p}:stream:dlq"
        self.stream_review = f"{p}:stream:review"
        self.stream_norm_review = f"{p}:stream:norm_review"
        self.alias = f"{p}:alias"
        self.manifest_index = f"{p}:manifest:index"

    def manifest(self, sha: str) -> str:
        return f"{self.prefix}:manifest:{sha}"

    def lock(self, name: str) -> str:
        return f"{self.prefix}:lock:{name}"

    def llm(self, digest: str) -> str:
        return f"{self.prefix}:llm:{digest}"

    def emb(self, model: str, digest: str) -> str:
        return f"{self.prefix}:emb:{model}:{digest}"

    def rl(self, bucket: str) -> str:
        return f"{self.prefix}:rl:{bucket}"

    def vault(self, call_id: str) -> str:
        return f"{self.prefix}:vault:{call_id}"

    def stats(self, day: str) -> str:
        return f"{self.prefix}:stats:{day}"


@dataclass
class RedisHandle:
    r: aioredis.Redis
    keys: Keys
    is_fake: bool

    async def close(self) -> None:
        try:
            await self.r.aclose()
        except Exception:  # pragma: no cover
            pass


async def connect_redis(settings: Settings) -> RedisHandle:
    """Connect to Redis; optionally fall back to in-process fakeredis for local dev."""
    keys = Keys(settings.redis.key_prefix)
    try:
        r = aioredis.from_url(settings.redis.url, decode_responses=True,
                              socket_connect_timeout=2, health_check_interval=30)
        await r.ping()
        return RedisHandle(r, keys, False)
    except Exception as exc:
        if not settings.redis.allow_fakeredis_fallback:
            raise RuntimeError(f"Redis unreachable at {settings.redis.url}: {exc}") from exc
        global _FAKE_SERVER
        import fakeredis

        if _FAKE_SERVER is None:
            _FAKE_SERVER = fakeredis.FakeServer()
            log.warning("Redis unreachable (%s) -> using in-process fakeredis. "
                        "Dev only: state is not shared across processes or restarts.", exc.__class__.__name__)
        r = fakeredis.FakeAsyncRedis(server=_FAKE_SERVER, decode_responses=True)
        return RedisHandle(r, keys, True)
