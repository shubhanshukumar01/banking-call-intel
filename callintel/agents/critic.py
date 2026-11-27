"""Critic / verifier agent: checks high-risk claims against the cited transcript lines.

Only claims that passed deterministic grounding and are high-risk (compliance
statuses, risk flags, misquotes, sales outcome, churn, vulnerability, summary) are
sent - batched, ~10 per call - so the critic adds 1-3 calls per transcript.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..preprocess.transcript import ROLE_CODE
from .context import AgentDeps, CallContext

if TYPE_CHECKING:
    from ..verify.gate import ClaimRec


class CriticAgent:
    name = "critic"

    def __init__(self, deps: AgentDeps, batch: int = 10) -> None:
        self.d = deps
        self.prompt = deps.prompts.get("critic_agent")
        self.batch = batch

    def _fmt(self, ctx: CallContext, i: int, c: "ClaimRec") -> str:
        cited = []
        for e in c.evidence[:4]:
            ln = ctx.tindex.line(e.line)
            text = ln.text if ln else e.quote
            cited.append(f'L{e.line} {ROLE_CODE.get(e.speaker, "?")}: "{text[:260]}"')
        return f"C{i} claim: {c.text}\n   cited: " + " | ".join(cited)

    async def _run_batch(self, ctx: CallContext, claims: list["ClaimRec"]) -> dict[str, tuple[str, str]]:
        text = "\n".join(self._fmt(ctx, i + 1, c) for i, c in enumerate(claims))
        res = await self.d.gateway.structured("critic", self.prompt.messages(claims=text),
                                              self.d.schemas.CriticOutput)
        out: dict[str, tuple[str, str]] = {}
        for v in res.data.verdicts:  # type: ignore[attr-defined]
            try:
                idx = int(v.claim_id.strip().lstrip("Cc")) - 1
            except ValueError:
                continue
            if 0 <= idx < len(claims):
                out[claims[idx].key] = (v.verdict, v.reason)
        return out

    async def verify(self, ctx: CallContext, claims: list["ClaimRec"]) -> dict[str, tuple[str, str]]:
        batches = [claims[i:i + self.batch] for i in range(0, len(claims), self.batch)]
        results = await asyncio.gather(*(self._run_batch(ctx, b) for b in batches), return_exceptions=True)
        merged: dict[str, tuple[str, str]] = {}
        for r in results:
            if isinstance(r, dict):
                merged.update(r)
        return merged
