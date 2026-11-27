"""Map agent: one LLM call per selected chunk -> typed atomic facts with line + verbatim quote.

A single multi-aspect extraction per chunk (instead of one pass per specialist over
raw text) keeps LLM calls ~= number of selected chunks. Specialists then reason over
a compact, de-duplicated fact list rather than 50k+ tokens of transcript.
"""
from __future__ import annotations

from typing import Any

from ..preprocess.transcript import Chunk
from .context import AgentDeps


class MapAgent:
    name = "map"

    def __init__(self, deps: AgentDeps) -> None:
        self.d = deps
        tax = deps.taxonomy
        # Static part rendered once -> byte-identical system prompt across all chunks (prefix cache).
        self.prompt = deps.prompts.get("map_extract").with_static(
            max_facts=deps.settings.agents.max_facts_per_chunk, fact_type_guide=tax.fact_type_guide())

    async def extract(self, chunk: Chunk, total: int) -> list[Any]:
        msgs = self.prompt.messages(chunk_no=chunk.idx + 1, chunk_total=total, line_start=chunk.line_start,
                                    line_end=chunk.line_end, text=chunk.render())
        res = await self.d.gateway.structured("map", msgs, self.d.schemas.MapOutput)
        return list(res.data.facts)[: self.d.settings.agents.max_facts_per_chunk]  # type: ignore[attr-defined]
