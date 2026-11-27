"""Work queue on Redis Streams consumer groups (replaces Celery/ARQ: one moving part).

Guarantees: at-least-once delivery; crashed consumers' messages are reclaimed via
XAUTOCLAIM after `visibility_timeout`; explicit retry counter; DLQ after max attempts.
Combined with the per-file manifest + lock this gives effectively-once outputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from ..utils import dumps, loads, now_iso


@dataclass
class Message:
    id: str
    payload: dict[str, Any]


class StreamQueue:
    def __init__(self, r: aioredis.Redis, stream: str, group: str = "workers", maxlen: int = 200_000) -> None:
        self.r = r
        self.stream = stream
        self.group = group
        self.maxlen = maxlen

    async def ensure_group(self) -> None:
        try:
            await self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, payload: dict[str, Any]) -> str:
        return await self.r.xadd(self.stream, {"data": dumps(payload)}, maxlen=self.maxlen, approximate=True)

    @staticmethod
    def _parse(entries: Any) -> list[Message]:
        out: list[Message] = []
        for msg_id, fields in entries or []:
            if not fields:  # deleted while pending
                continue
            out.append(Message(msg_id, loads(fields["data"])))
        return out

    async def read(self, consumer: str, count: int = 1, block_ms: int = 3000) -> list[Message]:
        resp = await self.r.xreadgroup(self.group, consumer, {self.stream: ">"}, count=count, block=block_ms)
        msgs: list[Message] = []
        for _stream, entries in resp or []:
            msgs.extend(self._parse(entries))
        return msgs

    async def reclaim(self, consumer: str, min_idle_ms: int, count: int = 10) -> list[Message]:
        """Take over messages a crashed/stuck consumer never acknowledged."""
        try:
            res = await self.r.xautoclaim(self.stream, self.group, consumer, min_idle_time=min_idle_ms,
                                          start_id="0-0", count=count)
        except ResponseError:
            return []
        entries = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else []
        return self._parse(entries)

    async def ack(self, msg_id: str) -> None:
        pipe = self.r.pipeline(transaction=False)
        pipe.xack(self.stream, self.group, msg_id)
        pipe.xdel(self.stream, msg_id)
        await pipe.execute()

    async def retry_or_dead_letter(self, msg: Message, error: str, max_attempts: int,
                                   dlq: "StreamQueue") -> str:
        attempts = int(msg.payload.get("attempts", 0)) + 1
        payload = {**msg.payload, "attempts": attempts, "last_error": error[:500], "failed_at": now_iso()}
        if attempts >= max_attempts:
            await dlq.enqueue(payload)
            outcome = "dead_lettered"
        else:
            await self.enqueue(payload)
            outcome = "requeued"
        await self.ack(msg.id)
        return outcome

    async def depth(self) -> dict[str, int]:
        """Backlog = entries in stream (acked ones are deleted); pending = in-flight."""
        length = await self.r.xlen(self.stream)
        pending = 0
        try:
            info = await self.r.xpending(self.stream, self.group)
            pending = int(info.get("pending", 0)) if isinstance(info, dict) else int(info[0])
        except ResponseError:
            pass
        return {"in_stream": int(length), "pending": pending, "waiting": max(0, int(length) - pending)}
