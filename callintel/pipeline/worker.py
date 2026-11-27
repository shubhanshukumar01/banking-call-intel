"""Ingestion + workers.

Level-1 parallelism: N OS processes (CPU-bound masking runs truly parallel, no GIL).
Level-2 parallelism: each process handles M files concurrently on one asyncio loop;
inside a file, chunk/agent LLM calls are concurrent too, bounded by the gateway's
per-provider semaphores and the shared Redis rate limiter.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import os
import socket
import time
from pathlib import Path
from typing import Any

from ..config import Settings, load_settings
from ..redis_layer.client import RedisHandle, connect_redis
from ..redis_layer.store import Manifest
from ..redis_layer.streams import Message, StreamQueue
from ..utils import now_iso, setup_logging, sha256_file
from .engine import Engine, NonRetryableError

log = logging.getLogger(__name__)


def queues(redis: RedisHandle, settings: Settings) -> tuple[StreamQueue, StreamQueue]:
    k = redis.keys
    return (StreamQueue(redis.r, k.stream_ingest, maxlen=settings.redis.stream_maxlen),
            StreamQueue(redis.r, k.stream_dlq, maxlen=settings.redis.stream_maxlen))


async def enqueue_folder(settings: Settings, redis: RedisHandle, input_dir: str | None = None,
                         force: bool = False, recursive: bool = False) -> dict[str, int]:
    """Scan a folder, de-duplicate by SHA-256 via the manifest, enqueue new files."""
    folder = settings.path(input_dir or settings.paths.input_dir)
    q, _ = queues(redis, settings)
    await q.ensure_group()
    manifest = Manifest(redis.r, redis.keys)
    files = sorted(folder.rglob("*.txt") if recursive else folder.glob("*.txt"))
    now = time.time()
    counts = {"found": len(files), "queued": 0, "skipped_duplicate_or_done": 0, "skipped_unstable": 0}
    for f in files:
        if now - f.stat().st_mtime < settings.workers.stable_file_age_s:
            counts["skipped_unstable"] += 1  # still being written
            continue
        sha = await asyncio.to_thread(sha256_file, f)
        if await manifest.register(sha, str(f), force=force):
            await q.enqueue({"path": str(f.resolve()), "sha": sha, "attempts": 0, "force": force,
                             "enqueued_at": now_iso()})
            counts["queued"] += 1
        else:
            counts["skipped_duplicate_or_done"] += 1
    return counts


async def watch_folder(settings: Settings, input_dir: str | None, interval_s: float = 10.0) -> None:
    redis = await connect_redis(settings)
    try:
        while True:
            c = await enqueue_folder(settings, redis, input_dir)
            if c["queued"]:
                log.info("watcher queued %d new file(s)", c["queued"])
            await asyncio.sleep(interval_s)
    finally:
        await redis.close()


async def worker_loop(settings: Settings, *, concurrency: int, exit_when_idle: bool,
                      engine: Engine | None = None, name: str | None = None) -> dict[str, int]:
    own_engine = engine is None
    engine = engine or await Engine.create(settings)
    q, dlq = queues(engine.redis, settings)
    await q.ensure_group()
    consumer = name or f"{socket.gethostname()}-{os.getpid()}"
    stats = {"done": 0, "needs_review": 0, "requeued": 0, "dead_lettered": 0, "skipped": 0}
    tasks: set[asyncio.Task] = set()
    vis_ms = settings.workers.visibility_timeout_s * 1000
    last_reclaim = 0.0
    idle = 0

    async def handle(msg: Message) -> None:
        path = msg.payload.get("path", "")
        try:
            res = await engine.process_file(path, sha=msg.payload.get("sha"), force=bool(msg.payload.get("force")))
            await q.ack(msg.id)
            st = res.get("status", "")
            key = st if st in stats else "skipped"
            stats[key] += 1
            log.info("%s %s %s", st.upper(), Path(path).name,
                     {k: v for k, v in res.items() if k not in ("status", "output")})
        except NonRetryableError as exc:
            await dlq.enqueue({**msg.payload, "error": str(exc)[:500], "failed_at": now_iso(), "non_retryable": True})
            await q.ack(msg.id)
            await engine.manifest.set(msg.payload.get("sha", "?"), "dead_lettered", error=str(exc)[:300])
            stats["dead_lettered"] += 1
            log.error("DLQ (non-retryable) %s: %s", Path(path).name, exc)
        except Exception as exc:  # retry with counter, then DLQ
            outcome = await q.retry_or_dead_letter(msg, repr(exc), settings.workers.max_attempts, dlq)
            if outcome == "dead_lettered":
                await engine.manifest.set(msg.payload.get("sha", "?"), "dead_lettered", error=repr(exc)[:300])
            stats[outcome] += 1
            log.error("%s %s: %r", outcome.upper(), Path(path).name, exc)

    try:
        while True:
            free = concurrency - len(tasks)
            if free <= 0:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                continue
            msgs: list[Message] = []
            if time.monotonic() - last_reclaim > 30:
                msgs += await q.reclaim(consumer, vis_ms, free)
                last_reclaim = time.monotonic()
            if len(msgs) < free:
                block = 200 if tasks else settings.workers.block_ms
                msgs += await q.read(consumer, free - len(msgs), block)
            for m in msgs:
                t = asyncio.create_task(handle(m))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            if msgs or tasks:
                idle = 0
                if not msgs:
                    await asyncio.wait(tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                continue
            idle += 1
            if exit_when_idle and idle >= 2:
                depth = await q.depth()
                if depth["in_stream"] == 0:
                    break
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if own_engine:
            await engine.close()
    return stats


def _process_entry(config_path: str | None, profile: str | None, idx: int, concurrency: int,
                   exit_when_idle: bool) -> None:
    settings = load_settings(config_path, profile)
    setup_logging(settings.app.log_level)
    asyncio.run(worker_loop(settings, concurrency=concurrency, exit_when_idle=exit_when_idle,
                            name=f"{socket.gethostname()}-w{idx}-{os.getpid()}"))


def run_worker_pool(settings: Settings, processes: int, concurrency: int, exit_when_idle: bool) -> None:
    if processes <= 1:
        asyncio.run(worker_loop(settings, concurrency=concurrency, exit_when_idle=exit_when_idle))
        return
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_process_entry, name=f"worker-{i}",
                         args=(settings.config_path, settings.llm.active_profile, i, concurrency, exit_when_idle))
             for i in range(processes)]
    for p in procs:
        p.start()
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()


async def run_local(settings: Settings, input_dir: str | None, concurrency: int, force: bool) -> dict[str, Any]:
    """Ingest + process in one process (works with fakeredis; ideal for laptops)."""
    engine = await Engine.create(settings)
    try:
        counts = await enqueue_folder(settings, engine.redis, input_dir, force=force)
        log.info("ingest: %s", counts)
        t0 = time.perf_counter()
        stats = await worker_loop(settings, concurrency=concurrency, exit_when_idle=True, engine=engine)
        stats["wall_s"] = round(time.perf_counter() - t0, 1)  # type: ignore[assignment]
        return {"ingest": counts, "work": stats}
    finally:
        await engine.close()
