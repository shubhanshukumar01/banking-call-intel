"""Retrieval primitives: BM25 (lexical) + optional dense embeddings, fused with RRF.

The bank's knowledge base and product catalogue are small (hundreds to a few
thousand passages), so an in-process index answers in well under a millisecond and
avoids running a separate vector database. Embeddings are cached in Redis.
For large corpora, swap HybridIndex for Redis 8 vector search or Qdrant behind
the same `search()` signature.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

_TOKEN = re.compile(r"[a-z0-9%]+")
STOPWORDS = set("""a an the and or but if of to in on at for from with by is are was were be been it this that
these those i you he she we they me my your our their his her its do does did have has had not no so as can
could would should will just very please okay ok yes yeah um uh""".split())


def _stem(t: str) -> str:
    for suf, min_len in (("ing", 6), ("ed", 5), ("es", 5), ("s", 4)):
        if len(t) >= min_len and t.endswith(suf):
            return t[: -len(suf)]
    return t


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.lens = [len(d) for d in docs]
        self.avg = (sum(self.lens) / self.n) if self.n else 0.0
        self.post: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, d in enumerate(docs):
            for term, tf in Counter(d).items():
                self.post[term].append((i, tf))
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.post.items()}

    def search(self, query: str | list[str], k: int = 10) -> list[tuple[int, float]]:
        q = tokenize(query) if isinstance(query, str) else query
        scores: dict[int, float] = defaultdict(float)
        for term in set(q):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, tf in self.post[term]:
                denom = tf + self.k1 * (1 - self.b + self.b * self.lens[i] / (self.avg or 1))
                scores[i] += idf * tf * (self.k1 + 1) / denom
        return sorted(scores.items(), key=lambda x: -x[1])[:k]


@dataclass
class Doc:
    id: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hit:
    doc: Doc
    score: float


class HybridIndex:
    """BM25 + (optional) dense cosine, Reciprocal Rank Fusion (k=60)."""

    def __init__(self, docs: list[Doc]) -> None:
        self.docs = docs
        self.bm25 = BM25([tokenize(d.text) for d in docs])
        self.emb: np.ndarray | None = None
        self._embed: Callable | None = None

    async def enable_dense(self, embed_fn: Callable) -> None:
        """embed_fn: async (list[str]) -> L2-normalised np.ndarray."""
        if self.docs:
            self.emb = await embed_fn([d.text for d in self.docs])
            self._embed = embed_fn

    async def search(self, query: str, k: int = 5, where: Callable[[Doc], bool] | None = None) -> list[Hit]:
        if not self.docs or not query.strip():
            return []
        pool = max(k * 5, 20)
        fused: dict[int, float] = defaultdict(float)
        for rank, (i, _s) in enumerate(self.bm25.search(query, pool)):
            fused[i] += 1.0 / (60 + rank)
        if self.emb is not None and self._embed is not None:
            qv = (await self._embed([query]))[0]
            sims = self.emb @ qv
            for rank, i in enumerate(np.argsort(-sims)[:pool]):
                fused[int(i)] += 1.0 / (60 + rank)
        ranked = sorted(fused.items(), key=lambda x: -x[1])
        out: list[Hit] = []
        for i, s in ranked:
            d = self.docs[i]
            if where is None or where(d):
                out.append(Hit(d, s))
            if len(out) >= k:
                break
        return out
