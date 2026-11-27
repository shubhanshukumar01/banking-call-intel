"""Distributed token-bucket rate limiter (RPM + TPM) shared by all worker processes.

Implemented as an atomic Lua script so concurrent workers never over-spend a
provider quota. Falls back to an in-process bucket if Lua is unavailable.
"""
from __future__ import annotations

import asyncio
import logging
import time

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

_LUA = """
local cap  = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now  = tonumber(ARGV[4])
if cost > cap then cost = cap end
local d = redis.call('HMGET', KEYS[1], 't', 'ts')
local tokens = tonumber(d[1])
local ts = tonumber(d[2])
if tokens == nil then tokens = cap; ts = now end
local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(cap, tokens + elapsed * rate)
local wait = 0
if tokens >= cost then
  tokens = tokens - cost
else
  wait = (cost - tokens) / rate
end
redis.call('HSET', KEYS[1], 't', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(cap / rate * 1000) + 1000)
return tostring(wait)
"""


class _LocalBucket:
    def __init__(self, cap: float, rate: float) -> None:
        self.cap, self.rate, self.tokens, self.ts = cap, rate, cap, time.monotonic()

    def take(self, cost: float) -> float:
        cost = min(cost, self.cap)
        now = time.monotonic()
        self.tokens = min(self.cap, self.tokens + (now - self.ts) * self.rate)
        self.ts = now
        if self.tokens >= cost:
            self.tokens -= cost
            return 0.0
        return (cost - self.tokens) / self.rate


class RateLimiter:
    """acquire(bucket, capacity_per_minute, cost) blocks until the cost fits the budget."""

    def __init__(self, r: aioredis.Redis, key_fn) -> None:
        self.r = r
        self.key_fn = key_fn
        self._script = None
        self._local: dict[str, _LocalBucket] = {}
        self._lua_ok = True

    async def acquire(self, bucket: str, per_minute: float | None, cost: float = 1.0) -> float:
        if not per_minute or per_minute <= 0:
            return 0.0
        cap, rate = float(per_minute), float(per_minute) / 60.0
        waited = 0.0
        while True:
            wait = await self._take(bucket, cap, rate, cost)
            if wait <= 0:
                return waited
            wait = min(wait, 30.0)
            waited += wait
            await asyncio.sleep(wait)
            # the deficit is paid by the passage of time; retry the take

    async def _take(self, bucket: str, cap: float, rate: float, cost: float) -> float:
        if self._lua_ok:
            try:
                if self._script is None:
                    self._script = self.r.register_script(_LUA)
                res = await self._script(keys=[self.key_fn(bucket)],
                                         args=[cap, rate, cost, int(time.time() * 1000)])
                return float(res)
            except Exception as exc:  # e.g. fakeredis without lupa
                log.warning("Lua rate limiter unavailable (%s); using per-process limiter", exc)
                self._lua_ok = False
        b = self._local.setdefault(bucket, _LocalBucket(cap, rate))
        return b.take(cost)
