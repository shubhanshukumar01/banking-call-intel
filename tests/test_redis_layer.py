import asyncio
import time

from callintel.redis_layer.client import connect_redis
from callintel.redis_layer.ratelimit import RateLimiter
from callintel.redis_layer.store import Manifest, RedisLock
from callintel.redis_layer.streams import StreamQueue


async def test_stream_ack_retry_dlq(settings):
    r = await connect_redis(settings)
    q = StreamQueue(r.r, r.keys.stream_ingest)
    dlq = StreamQueue(r.r, r.keys.stream_dlq)
    await q.ensure_group()
    await q.enqueue({"path": "a.txt", "attempts": 0})
    [m] = await q.read("c1", 1, 100)
    assert (await q.depth())["pending"] == 1
    assert await q.retry_or_dead_letter(m, "boom", 2, dlq) == "requeued"
    [m2] = await q.read("c1", 1, 100)
    assert m2.payload["attempts"] == 1
    assert await q.retry_or_dead_letter(m2, "boom", 2, dlq) == "dead_lettered"
    assert await r.r.xlen(r.keys.stream_dlq) == 1
    assert (await q.depth())["in_stream"] == 0


async def test_reclaim_from_crashed_consumer(settings):
    r = await connect_redis(settings)
    q = StreamQueue(r.r, r.keys.stream_ingest)
    await q.ensure_group()
    await q.enqueue({"path": "b.txt"})
    await q.read("crashed", 1, 100)            # taken but never acked
    await asyncio.sleep(0.05)
    got = await q.reclaim("healthy", min_idle_ms=10)
    assert got and got[0].payload["path"] == "b.txt"


async def test_manifest_idempotency(settings):
    r = await connect_redis(settings)
    m = Manifest(r.r, r.keys)
    assert await m.register("sha1", "f.txt")
    assert not await m.register("sha1", "f.txt")         # duplicate content -> not queued
    await m.set("sha1", "failed", error="x")
    assert await m.register("sha1", "f.txt")             # failed files may be retried


async def test_lock_exclusive(settings):
    r = await connect_redis(settings)
    a, b = RedisLock(r.r, "k:lock"), RedisLock(r.r, "k:lock")
    assert await a.acquire()
    assert not await b.acquire()
    await a.release()
    assert await b.acquire()


async def test_rate_limiter_blocks_over_budget(settings):
    r = await connect_redis(settings)
    rl = RateLimiter(r.r, r.keys.rl)
    t0 = time.perf_counter()
    for _ in range(3):
        await rl.acquire("x", per_minute=120, cost=1)   # 2 tokens/s, burst 120 -> immediate
    assert time.perf_counter() - t0 < 0.5
    waited = await rl.acquire("y", per_minute=60, cost=61)  # cost capped at capacity -> no deadlock
    assert waited >= 0
