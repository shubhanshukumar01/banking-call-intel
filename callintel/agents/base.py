"""Agent runtime: structured call + bounded tool loop.

An agent may answer with `tool_requests` (search_transcript / get_lines / search_kb)
when its evidence is insufficient. The runtime executes them deterministically,
appends the results as new citable evidence (L<n> ids) and asks once more with tools
disabled. Bounded by agents.max_tool_rounds, so latency stays predictable.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel

from .context import AgentDeps, CallContext

log = logging.getLogger(__name__)
_RANGE = re.compile(r"(\d+)\s*(?:-|to|\.\.)\s*(\d+)")


class BaseAgent:
    name = "agent"
    prompt_id = ""
    role = "reduce"

    def __init__(self, deps: AgentDeps) -> None:
        self.d = deps
        self.prompt = deps.prompts.get(self.prompt_id)

    async def call(self, ctx: CallContext, schema: type[BaseModel], user_vars: dict[str, Any],
                   feedback: str | None = None) -> BaseModel:
        if feedback:
            user_vars = {**user_vars, "evidence": user_vars.get("evidence", "") +
                         f"\n\nREVIEWER FEEDBACK (fix these, cite valid ids or omit the item):\n{feedback}"}
        res = await self.d.gateway.structured(self.role, self.prompt.messages(**user_vars), schema)
        out = res.data
        rounds = 0
        max_rounds = self.d.settings.agents.max_tool_rounds
        while getattr(out, "tool_requests", None) and rounds < max_rounds:
            rounds += 1
            extra = await self.run_tools(ctx, out.tool_requests[:2])  # type: ignore[attr-defined]
            ctx.stats["tool_calls"] = ctx.stats.get("tool_calls", 0) + len(out.tool_requests[:2])  # type: ignore[attr-defined]
            if not extra:
                break
            user_vars = {**user_vars, "evidence": user_vars.get("evidence", "") +
                         f"\nLOOKUP RESULTS (citable):\n{extra}\n(Lookups are done: return tool_requests as [].)"}
            out = (await self.d.gateway.structured(self.role, self.prompt.messages(**user_vars), schema)).data
        if hasattr(out, "tool_requests"):
            out.tool_requests = []  # type: ignore[attr-defined]
        return out

    async def run_tools(self, ctx: CallContext, requests: list[Any]) -> str:
        lines: list[str] = []
        for req in requests:
            try:
                if req.tool == "search_transcript":
                    lines += [ctx.tindex.fmt(ln) for ln in ctx.tindex.search_lines(req.arg, 3)]
                elif req.tool == "get_lines":
                    m = _RANGE.search(req.arg)
                    if m:
                        a, b = sorted((int(m.group(1)), int(m.group(2))))
                        lines += [ctx.tindex.fmt(ln) for ln in ctx.tindex.range(a, min(b, a + 12), 8)]
                elif req.tool == "search_kb":
                    docs = await self.d.kb.search(req.arg, "playbook", 2)
                    lines += [f"[reference, not evidence] {d.text[:300]}" for d in docs]
            except Exception as exc:  # a bad tool argument must never fail the call
                log.debug("tool %s failed: %s", getattr(req, "tool", "?"), exc)
        return "\n".join(dict.fromkeys(lines))


def fmt_facts(facts: list) -> str:
    return "\n".join(f.fmt() for f in facts) or "(no facts)"
