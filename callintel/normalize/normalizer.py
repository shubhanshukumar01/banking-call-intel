"""3-tier product normaliser: surface mention -> canonical product id.

  T0  exact      alias dictionary from products.yaml                 (~1 us, in-process)
  T1  learned    Redis hash of aliases resolved earlier by T2/T3      (<1 ms, shared by all workers)
  T2  fuzzy+RAG  edit-distance scoring (accept on high score+margin); candidates for T3
                 come from hybrid retrieval (BM25 + optional embeddings, RRF)
  T3  LLM        enum-constrained pick among the top-k candidates or NONE

Every T2/T3 resolution is written back to T1, so the slow path runs once per new
alias across the whole fleet. Unresolved mentions go to a curation stream.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from ..config import Settings
from ..llm.gateway import LLMGateway
from ..llm.prompts import PromptRegistry
from ..rag.retrieval import Doc, HybridIndex
from ..redis_layer.client import RedisHandle
from ..redis_layer.streams import StreamQueue
from ..schemas import LLMSchemas
from ..utils import dumps, loads, now_iso
from .catalog import ProductCatalog, norm_mention

log = logging.getLogger(__name__)

_GENERIC_ONLY = {"it", "that", "this", "one", "the one", "product", "scheme", "offer", "plan", "thing", "them"}
# A real product mention names a product type. Phrases like "debit transaction" or "late fee"
# are not products and must not be fuzzy/LLM-matched into one.
_HEAD_NOUNS = {"card", "cards", "loan", "loans", "account", "deposit", "fd", "insurance", "policy", "plan", "sip",
               "fund", "funds", "mortgage", "cover", "overdraft", "emi"}


@dataclass
class NormResult:
    mention: str
    product_id: str | None
    confidence: float
    tier: str  # exact | learned | fuzzy | llm | unresolved

    def as_json(self) -> dict:
        return {"product_id": self.product_id, "confidence": round(self.confidence, 3), "tier": self.tier}


class ProductNormalizer:
    def __init__(self, settings: Settings, catalog: ProductCatalog, redis: RedisHandle,
                 gateway: LLMGateway | None, prompts: PromptRegistry | None, schemas: LLMSchemas | None) -> None:
        self.s = settings
        self.cfg = settings.rag.normalizer
        self.catalog = catalog
        self.redis = redis
        self.gateway = gateway
        self.prompt = prompts.get("normalizer_agent") if prompts else None
        self.schemas = schemas
        self.review = StreamQueue(redis.r, redis.keys.stream_norm_review, maxlen=50_000)
        self._choices: list[str] = []
        self._owner: list[str] = []
        for pid, alias in ((pid, norm_mention(a)) for pid, p in catalog.products.items() for a in p.all_names):
            if alias:
                self._choices.append(alias)
                self._owner.append(pid)
        docs = [Doc(pid, f"{p.name} {p.category.replace('_', ' ')} {' '.join(p.aliases)} {p.description}",
                    {"product_id": pid}) for pid, p in catalog.products.items()]
        self.index = HybridIndex(docs)
        self._local: dict[str, NormResult] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        generic_words = {"card", "credit", "debit", "loan", "account", "the", "a", "and", "one", "new", "another"}
        self._alias_tokens = {tok for a in self._choices for tok in a.split() if len(tok) >= 3} - generic_words

    def _looks_like_product(self, n: str) -> bool:
        toks = set(n.split())
        return bool(toks & _HEAD_NOUNS) or bool(toks & self._alias_tokens)

    async def enable_dense(self, embed_fn) -> None:
        await self.index.enable_dense(embed_fn)

    # ------------------------------------------------------------------ scoring
    def _fuzzy(self, n: str, limit: int = 25) -> list[tuple[str, float]]:
        """Best score per product: max(ratio, token_sort_ratio); generic products slightly penalised."""
        best: dict[str, float] = {}
        for alias, score, idx in process.extract(n, self._choices, scorer=fuzz.ratio, limit=limit):
            pid = self._owner[idx]
            s = max(score, fuzz.token_sort_ratio(n, alias)) / 100.0
            if self.catalog.products[pid].generic:
                s -= 0.03
            best[pid] = max(best.get(pid, 0.0), s)
        return sorted(best.items(), key=lambda x: -x[1])

    async def candidates(self, n: str, k: int) -> list[tuple[str, float]]:
        fz = self._fuzzy(n)
        hits = await self.index.search(n, k)
        ordered: dict[str, float] = {}
        for pid, s in fz[:k]:
            ordered[pid] = s
        for h in hits:
            ordered.setdefault(h.doc.meta["product_id"], 0.0)
        return list(ordered.items())[: max(k, 1)]

    # ------------------------------------------------------------------ main API
    async def normalize(self, mention: str, context: str = "") -> NormResult:
        n = norm_mention(mention)
        if not n or n in _GENERIC_ONLY:
            return NormResult(mention, None, 0.0, "unresolved")
        if n in self.catalog.alias_map:
            return NormResult(mention, self.catalog.alias_map[n], 1.0, "exact")
        if not self._looks_like_product(n):
            return NormResult(mention, None, 0.0, "unresolved")
        if n in self._local:
            r = self._local[n]
            return NormResult(mention, r.product_id, r.confidence, r.tier)
        if n in self._inflight:  # same mention being resolved concurrently -> share result
            r = await asyncio.shield(self._inflight[n])
            return NormResult(mention, r.product_id, r.confidence, r.tier)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[n] = fut
        try:
            res = await self._resolve(n, mention, context)
            fut.set_result(res)
            self._local[n] = res
            return res
        except BaseException as exc:
            fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(n, None)
            if not fut.done():
                fut.cancel()
            elif fut.exception() is not None:
                pass  # retrieved to avoid "exception never retrieved" warnings

    async def _resolve(self, n: str, mention: str, context: str) -> NormResult:
        raw = await self.redis.r.hget(self.redis.keys.alias, n)
        if raw:
            d = loads(raw)
            return NormResult(mention, d.get("product_id"), float(d.get("confidence", 0.8)), "learned")
        fz = self._fuzzy(n)
        if fz:
            top = fz[0][1]
            second = fz[1][1] if len(fz) > 1 else 0.0
            if top >= self.cfg.t2_accept_score and top - second >= self.cfg.t2_margin:
                res = NormResult(mention, fz[0][0], top, "fuzzy")
                await self._learn(n, res)
                return res
        if self.cfg.t3_llm_enabled and self.gateway and self.prompt and self.schemas:
            cands = await self.candidates(n, self.cfg.t3_top_k)
            if cands:
                ids = tuple(pid for pid, _ in cands)
                lines = []
                for pid in ids:
                    p = self.catalog.products[pid]
                    lines.append(f"{pid}: {p.name} ({p.category}); aka {', '.join(p.aliases[:6])}")
                schema = self.schemas.normalizer_output(ids)
                try:
                    r = await self.gateway.structured(
                        "normalizer", self.prompt.messages(mention=mention[:80], context=context[:300],
                                                           candidates="\n".join(lines)), schema)
                    pid = r.data.product_id  # type: ignore[attr-defined]
                    if pid != "NONE":
                        res = NormResult(mention, pid, 0.8, "llm")
                        await self._learn(n, res)
                        return res
                except Exception as exc:  # normalisation must never fail the call
                    log.warning("T3 normaliser failed for a mention: %s", exc)
        await self.review.enqueue({"mention": n, "context": context[:300], "at": now_iso(),
                                   "candidates": [pid for pid, _ in fz[:3]]})
        return NormResult(mention, None, 0.0, "unresolved")

    async def _learn(self, n: str, res: NormResult) -> None:
        await self.redis.r.hset(self.redis.keys.alias, n, dumps({**res.as_json(), "learned_at": now_iso()}))

    async def normalize_many(self, items: list[tuple[str, str]]) -> dict[str, NormResult]:
        """items: [(mention, context)] -> {norm_mention: NormResult}"""
        uniq: dict[str, tuple[str, str]] = {}
        for m, ctx in items:
            uniq.setdefault(norm_mention(m), (m, ctx))
        keys = list(uniq)
        results = await asyncio.gather(*(self.normalize(*uniq[k]) for k in keys))
        return dict(zip(keys, results))
