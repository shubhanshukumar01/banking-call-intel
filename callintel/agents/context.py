"""Per-call state shared by all agents + the evidence registry.

Agents never write free-form quotes in the reduce phase: they cite evidence ids
(F<n> = grounded fact, L<n> = transcript line). The registry resolves ids to
(line, speaker, quote) and reports invalid ids, which makes hallucinated evidence
mechanically detectable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..llm.gateway import LLMGateway
from ..llm.prompts import PromptRegistry
from ..normalize.catalog import ProductCatalog
from ..normalize.normalizer import NormResult, ProductNormalizer
from ..preprocess.transcript import ROLE_CODE, PreparedTranscript
from ..rag.knowledge_base import KnowledgeBase
from ..rag.transcript_index import TranscriptIndex
from ..schemas import Evidence, LLMSchemas, Taxonomy

_ID = re.compile(r"\b([FfLl])\s*-?\s*(\d+)\b")


@dataclass
class AgentDeps:
    settings: Settings
    gateway: LLMGateway
    prompts: PromptRegistry
    schemas: LLMSchemas
    taxonomy: Taxonomy
    catalog: ProductCatalog
    kb: KnowledgeBase
    normalizer: ProductNormalizer


@dataclass
class FactRec:
    id: str
    type: str
    label: str
    value: str
    product: str | None
    number: str | None
    line: int
    speaker: str
    quote: str
    grounding: float
    chunk: int
    source: str = "llm"           # llm | catalog_scan
    product_id: str | None = None
    norm: NormResult | None = None
    number_verified: bool = True

    def fmt(self) -> str:
        extra = f" [product={self.product_id}]" if self.product_id else ""
        num = f" [number={self.number}]" if self.number else ""
        return (f'{self.id} L{self.line} {ROLE_CODE.get(self.speaker, "?")} {self.type}/{self.label}: '
                f'"{self.quote}"{extra}{num}')


class EvidenceRegistry:
    def __init__(self, tindex: TranscriptIndex) -> None:
        self.tindex = tindex
        self.facts: dict[str, FactRec] = {}

    def add(self, fact: FactRec) -> None:
        self.facts[fact.id] = fact

    @staticmethod
    def parse_ids(ids: list[str]) -> list[str]:
        out: list[str] = []
        for raw in ids:
            for m in _ID.finditer(str(raw)):
                tid = f"{m.group(1).upper()}{int(m.group(2))}"
                if tid not in out:
                    out.append(tid)
        return out

    def resolve(self, ids: list[str]) -> tuple[list[Evidence], list[float], list[str]]:
        """-> (evidence, grounding scores, invalid ids)"""
        ev: list[Evidence] = []
        scores: list[float] = []
        invalid: list[str] = []
        seen: set[int] = set()
        for tid in self.parse_ids(ids):
            if tid.startswith("F"):
                f = self.facts.get(tid)
                if not f:
                    invalid.append(tid)
                    continue
                if f.line in seen:
                    continue
                seen.add(f.line)
                ev.append(Evidence(line=f.line, speaker=f.speaker, quote=f.quote))
                scores.append(f.grounding)
            else:
                ln = self.tindex.nearest_line(int(tid[1:]), max_dist=1)
                if not ln:
                    invalid.append(tid)
                    continue
                if ln.no in seen:
                    continue
                seen.add(ln.no)
                quote = ln.text if len(ln.text) <= 220 else ln.text[:217] + "..."
                ev.append(Evidence(line=ln.no, speaker=ln.speaker, quote=quote))
                scores.append(1.0)
        return ev, scores, invalid

    def texts_for(self, evidence: list[Evidence]) -> list[str]:
        out = []
        for e in evidence:
            ln = self.tindex.line(e.line)
            out.append(ln.text if ln else e.quote)
        return out


@dataclass
class CallContext:
    prepared: PreparedTranscript
    tindex: TranscriptIndex
    registry: EvidenceRegistry
    call_type_hint: str = "service"
    product_ids: list[str] = field(default_factory=list)
    categories: set[str] = field(default_factory=set)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def facts(self) -> list[FactRec]:
        return list(self.registry.facts.values())

    def facts_of(self, types: set[str], limit: int) -> list[FactRec]:
        sel = [f for f in self.facts if f.type in types]
        if len(sel) <= limit:
            return sel
        # keep coverage: round-robin across types, preserving call order within each type
        by_type: dict[str, list[FactRec]] = {}
        for f in sel:
            by_type.setdefault(f.type, []).append(f)
        out: list[FactRec] = []
        while len(out) < limit and any(by_type.values()):
            for t in list(by_type):
                if by_type[t] and len(out) < limit:
                    out.append(by_type[t].pop(0))
        return sorted(out, key=lambda f: f.line)
