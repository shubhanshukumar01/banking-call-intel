"""Engine: long-lived per-process container of all dependencies + `process_file()`.

Created once per worker process (models, recognisers, indexes and connections are
reused across thousands of files). `process_file` is idempotent: content hash ->
manifest -> distributed lock -> skip if already done.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..agents.assemble import Assembler
from ..agents.context import AgentDeps
from ..agents.supervisor import Supervisor
from ..config import Settings
from ..llm.egress import EgressBlocked
from ..llm.gateway import LLMGateway, usage_scope
from ..llm.prompts import PromptRegistry
from ..normalize.catalog import ProductCatalog
from ..normalize.normalizer import ProductNormalizer
from ..output.sinks import OutputSink
from ..preprocess.pii import RegexRecognizer, build_recognizers, find_pii
from ..preprocess.transcript import prepare_transcript
from ..rag.knowledge_base import KnowledgeBase
from ..redis_layer.client import RedisHandle, connect_redis
from ..redis_layer.store import Manifest, Metrics, RedisLock
from ..redis_layer.streams import StreamQueue
from ..redis_layer.vault import PIIVault
from ..schemas import CallAnalysis, LLMSchemas, ReviewItem, Taxonomy
from ..utils import Stopwatch, sha256_file

log = logging.getLogger(__name__)
_SKIP_SCAN = {"source", "processing", "privacy", "call_id", "schema_version"}


class NonRetryableError(RuntimeError):
    """Errors that must go straight to the DLQ (e.g. egress guard block)."""


class Engine:
    def __init__(self, settings: Settings, redis: RedisHandle) -> None:
        self.s = settings
        self.redis = redis

    @classmethod
    async def create(cls, settings: Settings, redis: RedisHandle | None = None) -> "Engine":
        self = cls(settings, redis or await connect_redis(settings))
        s = settings
        self.prompts = PromptRegistry(s.path(s.paths.prompts_file))
        self.taxonomy = Taxonomy.load(s.path(s.paths.taxonomy_file))
        self.catalog = ProductCatalog(s.path(s.paths.products_file))
        self.kb = KnowledgeBase(s, self.catalog)
        self.schemas = LLMSchemas(self.taxonomy, self.catalog.ids, self.kb.rule_ids, s.agents.max_facts_per_chunk)
        self.gateway = LLMGateway(s, self.redis)
        self.normalizer = ProductNormalizer(s, self.catalog, self.redis, self.gateway, self.prompts, self.schemas)
        self.cheap, self.ner = build_recognizers(s.privacy.detectors, s.privacy.presidio_model,
                                                 s.privacy.gliner_model, s.privacy.gliner_threshold)
        self.vault = PIIVault(self.redis.r, self.redis.keys, s.privacy.vault)
        self.manifest = Manifest(self.redis.r, self.redis.keys)
        self.metrics = Metrics(self.redis.r, self.redis.keys)
        self.review = StreamQueue(self.redis.r, self.redis.keys.stream_review, maxlen=100_000)
        self.sink = OutputSink(s)
        self.deps = AgentDeps(s, self.gateway, self.prompts, self.schemas, self.taxonomy, self.catalog, self.kb,
                              self.normalizer)
        self.supervisor = Supervisor(self.deps)
        self.dense = False
        await self._maybe_enable_dense()
        return self

    async def _maybe_enable_dense(self) -> None:
        mode = self.s.rag.dense_enabled
        if mode is False or str(mode).lower() == "false":
            return
        try:
            ok, why = await self.gateway.ping_role("embedding")
            if not ok:
                raise RuntimeError(why)
            await self.kb.enable_dense(self.gateway.embed)
            await self.normalizer.enable_dense(self.gateway.embed)
            self.dense = True
        except Exception as exc:
            if str(mode).lower() == "true":
                raise
            log.info("dense retrieval disabled (embedding role unavailable: %s); using BM25 + fuzzy", exc)

    async def close(self) -> None:
        await self.gateway.close()
        await self.redis.close()

    # ------------------------------------------------------------------ output PII scan
    def _scan_output(self, analysis: CallAnalysis) -> tuple[CallAnalysis, int]:
        rec = RegexRecognizer()
        found = 0

        def walk(node: Any, top: bool = False) -> Any:
            nonlocal found
            if isinstance(node, dict):
                return {k: (v if (top and k in _SKIP_SCAN) else walk(v)) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(x) for x in node]
            if isinstance(node, str):
                spans = find_pii(node, rec)
                if spans:
                    found += len(spans)
                    for sp in sorted(spans, key=lambda x: x.start, reverse=True):
                        node = node[:sp.start] + f"[REDACTED_{sp.etype}]" + node[sp.end:]
                return node
            return node

        data = walk(analysis.model_dump(mode="json"), top=True)
        if not found:
            return analysis, 0
        out = CallAnalysis.model_validate(data)
        out.privacy.output_pii_findings = found
        out.quality.status = "needs_review"
        out.quality.review_items.append(ReviewItem(field="output", reason=f"{found} PII span(s) redacted in output"))
        return out, found

    # ------------------------------------------------------------------ main
    async def process_file(self, path: str | Path, *, sha: str | None = None, force: bool = False) -> dict[str, Any]:
        p = Path(path)
        sha = sha or await asyncio.to_thread(sha256_file, p)
        call_id = sha[:16]
        lock = RedisLock(self.redis.r, self.redis.keys.lock(sha), ttl_ms=self.s.workers.visibility_timeout_s * 1000)
        if not await lock.acquire():
            return {"call_id": call_id, "status": "locked_elsewhere"}
        try:
            m = await self.manifest.get(sha)
            if m.get("status") in ("done", "needs_review") and not force:
                return {"call_id": call_id, "status": "skipped_already_done", "output": m.get("output")}
            await self.manifest.set(sha, "processing", file=str(p), worker=f"{os.getpid()}")
            raw = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
            sw = Stopwatch()
            with usage_scope(call_id) as usage:
                with sw.stage("preprocess"):
                    prepared = await asyncio.to_thread(
                        prepare_transcript, raw, call_id=call_id, file_name=p.name, sha256=sha, settings=self.s,
                        cheap=self.cheap, ner=self.ner, protected_spans=self.catalog.protected_spans)
                outputs, ctx, report = await self.supervisor.analyze(prepared, sw)
                vault_stored = False
                if self.vault.enabled:
                    await self.vault.store(call_id, prepared.pii_mapping)
                    vault_stored = True
                prepared.pii_mapping.clear()  # raw values are not needed past this point
                stage_ms = {**{f"pre_{k}": v for k, v in prepared.timings_ms.items()}, **sw.marks}
                analysis = Assembler(self.deps, ctx, report).build(
                    outputs, usage=usage, stage_ms=stage_ms, duration_ms=sw.total_ms(),
                    prompt_versions=self.prompts.versions(), vault_stored=vault_stored)
            if self.s.output.scan_output_for_pii:
                analysis, _ = self._scan_output(analysis)
            out_path = await asyncio.to_thread(self.sink.write, analysis)
            status = "done" if analysis.quality.status == "validated" else "needs_review"
            if status == "needs_review":
                await self.review.enqueue({"call_id": call_id, "file": p.name, "output": out_path,
                                           "items": [r.model_dump() for r in analysis.quality.review_items][:20]})
            await self.manifest.set(sha, status, output=out_path, duration_ms=analysis.processing.duration_ms,
                                    call_id=call_id)
            await self.metrics.incr(files_done=1, files_needs_review=int(status == "needs_review"),
                                    lines_processed=prepared.raw_line_count)
            return {"call_id": call_id, "status": status, "output": out_path,
                    "duration_ms": analysis.processing.duration_ms, "llm_calls": usage.calls,
                    "cache_hits": usage.cache_hits, "chunks": f"{ctx.stats.get('chunks_selected')}/{len(prepared.chunks)}",
                    "facts": ctx.stats.get("facts_kept"), "confidence": analysis.quality.overall_confidence}
        except EgressBlocked as exc:
            await self.manifest.set(sha, "failed", error=f"egress_blocked: {exc}")
            await self.metrics.incr(files_failed=1, egress_blocked=1)
            raise NonRetryableError(str(exc)) from exc
        except Exception as exc:
            await self.manifest.set(sha, "failed", error=re.sub(r"\s+", " ", repr(exc))[:500])
            await self.metrics.incr(files_failed=1)
            raise
        finally:
            await lock.release()
