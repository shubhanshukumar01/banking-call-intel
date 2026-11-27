"""Supervisor: the per-call agent graph.

    triage -> map (parallel) -> ground + dedupe -> normalise products -> route
           -> [sales | customer | product | compliance] (parallel) -> synthesis
           -> verification loop (critic + targeted retries, bounded) -> assemble

Plain asyncio instead of a graph framework: the graph is a fixed fan-out/fan-in with
one bounded loop, so explicit code is faster, easier to debug and has no extra
dependency. Each node is an independent agent class, so moving to LangGraph later
is a wiring change, not a rewrite.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel

from ..normalize.catalog import norm_mention
from ..preprocess.transcript import Chunk, PreparedTranscript
from ..rag.transcript_index import TranscriptIndex
from ..utils import Stopwatch
from ..verify.gate import ClaimCollector, GateReport
from ..verify.grounding import ground_quote, number_supported
from .context import AgentDeps, CallContext, EvidenceRegistry, FactRec
from .critic import CriticAgent
from .mapper import MapAgent
from .specialists import (ComplianceAgent, CustomerAgent, ProductAgent, SalesAgent, SynthesisAgent,
                          findings_digest)
from .triage import call_type_hint, score_chunks

log = logging.getLogger(__name__)
_WS = re.compile(r"\s+")


def _n(s: str | None) -> str:
    return _WS.sub(" ", (s or "").lower()).strip(" .,!?\"'")


# Who can utter each fact type. The speaker of every line is known deterministically, so a
# "customer objection" on an AGENT line (e.g. the agent's own APR disclosure) is a labelling error.
SPEAKER_RULES = {
    "intent": {"CUSTOMER"}, "need": {"CUSTOMER"}, "objection": {"CUSTOMER"}, "complaint": {"CUSTOMER"},
    "buying_signal": {"CUSTOMER"}, "sentiment": {"CUSTOMER"},
    "offer_made": {"AGENT"}, "identity_check": {"AGENT"},
}


class MapStageFailed(RuntimeError):
    pass


class Supervisor:
    def __init__(self, deps: AgentDeps) -> None:
        self.d = deps
        self.mapper = MapAgent(deps)
        self.sales = SalesAgent(deps)
        self.customer = CustomerAgent(deps)
        self.product = ProductAgent(deps)
        self.compliance = ComplianceAgent(deps)
        self.synth = SynthesisAgent(deps)
        self.critic = CriticAgent(deps)

    # ------------------------------------------------------------------ main graph
    async def analyze(self, prepared: PreparedTranscript, sw: Stopwatch) -> tuple[dict[str, BaseModel], CallContext, GateReport]:
        tindex = TranscriptIndex(prepared)
        ctx = CallContext(prepared, tindex, EvidenceRegistry(tindex))

        with sw.stage("triage"):
            signals = score_chunks(prepared.chunks, self.d.catalog, self.d.settings.triage)
        selected = [c for c in prepared.chunks if c.selected]
        ctx.stats.update(chunks_selected=len(selected), chunks_skipped=len(prepared.chunks) - len(selected),
                         signal_totals=dict(signals))

        with sw.stage("map"):
            results = await asyncio.gather(*(self.mapper.extract(c, len(prepared.chunks)) for c in selected),
                                           return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if selected and len(errors) == len(selected):
            raise MapStageFailed(f"all {len(selected)} map calls failed: {errors[0]!r}") from errors[0]
        ctx.stats["map_failures"] = len(errors)
        for e in errors[:3]:
            log.warning("[%s] map chunk failed: %r", prepared.call_id, e)

        with sw.stage("ground"):
            self._ingest_facts(ctx, selected, results)
        with sw.stage("normalize"):
            await self._normalize(ctx)
        ctx.call_type_hint = call_type_hint(Counter(f.type for f in ctx.facts), signals)

        with sw.stage("specialists"):
            outputs = await self._run_specialists(ctx)
        with sw.stage("synthesis"):
            outputs["synthesis"] = await self._run_synthesis(ctx, outputs)
        with sw.stage("verify"):
            report = await self._verify(ctx, outputs)
        return outputs, ctx, report

    # ------------------------------------------------------------------ facts
    def _ingest_facts(self, ctx: CallContext, selected: list[Chunk], results: list[Any]) -> None:
        tax = self.d.taxonomy
        th = self.d.settings.agents.grounding_min_score
        tindex = ctx.tindex
        raw: list[FactRec] = []
        dropped = 0
        for chunk, res in zip(selected, results):
            if isinstance(res, BaseException):
                continue
            chunk_lines = [seg.line for t in chunk.turns for seg in t.segments]
            for f in res:
                # prompt rule: quotes are 3-15 words. A 1-2 word quote ("okay", "right") cannot carry an
                # intent/objection/complaint; short confirmations are fine for outcomes and buying signals.
                if len(f.quote.split()) < 3 and f.type not in ("outcome", "buying_signal", "next_step", "identity_check"):
                    dropped += 1
                    continue
                line, score = ground_quote(f.line, f.quote, tindex, chunk_lines)
                if line is None or score < th:
                    dropped += 1
                    continue
                ln = tindex.line(line)
                allowed = SPEAKER_RULES.get(f.type)
                if allowed and ln.speaker in ("AGENT", "CUSTOMER") and ln.speaker not in allowed:
                    dropped += 1
                    ctx.stats["facts_dropped_wrong_speaker"] = ctx.stats.get("facts_dropped_wrong_speaker", 0) + 1
                    continue
                label = f.label if f.label in tax.labels_for(f.type) else "other"
                num_ok = number_supported(f.number, [ln.text]) if (f.type == "quoted_term" and f.number) else True
                raw.append(FactRec("", f.type, label, f.value.strip(), (f.product or "").strip() or None,
                                   (f.number or "").strip() or None, line, ln.speaker, f.quote.strip(),
                                   round(score, 3), chunk.idx, number_verified=num_ok))
        # Deterministic recall booster: exact catalogue aliases the LLM may have skipped
        # (including in triage-skipped chunks). Max 2 per product.
        per_pid: Counter = Counter()
        for ln in ctx.prepared.lines.values():
            for surface, pid, s, e in self.d.catalog.find_mentions(ln.text):
                if self.d.catalog.products[pid].generic or per_pid[pid] >= 2:
                    continue
                per_pid[pid] += 1
                words = ln.text.split()
                pos = len(ln.text[:s].split())
                quote = " ".join(words[max(0, pos - 6): pos + 10])
                raw.append(FactRec("", "product_mention", "other", f"{surface} mentioned", surface, None, ln.no,
                                   ln.speaker, quote, 1.0, -1, source="catalog_scan"))
        kept: list[FactRec] = []
        seen1: set = set()
        seen2: set = set()
        for f in sorted(raw, key=lambda x: (x.line, x.source != "llm", x.type)):
            k1 = (f.type, f.line, _n(f.quote)[:40])
            k2 = (f.type, f.label, norm_mention(f.product or ""), _n(f.value)[:60])
            if f.type == "product_mention":
                k2 = (f.type, f.line, norm_mention(f.product or ""))
            if k1 in seen1 or k2 in seen2:
                continue
            seen1.add(k1)
            seen2.add(k2)
            kept.append(f)
        for i, f in enumerate(kept, start=1):
            f.id = f"F{i}"
            ctx.registry.add(f)
        ctx.stats.update(facts_raw=len(raw) + dropped, facts_dropped_ungrounded=dropped, facts_kept=len(kept))

    async def _normalize(self, ctx: CallContext) -> None:
        items = [(f.product, ctx.tindex.line(f.line).text if ctx.tindex.line(f.line) else "")
                 for f in ctx.facts if f.product]
        if not items:
            return
        results = await self.d.normalizer.normalize_many(items)  # type: ignore[arg-type]
        order: list[str] = []
        for f in sorted(ctx.facts, key=lambda x: x.line):
            if not f.product:
                continue
            r = results.get(norm_mention(f.product))
            if r and r.product_id:
                f.product_id, f.norm = r.product_id, r
                if r.product_id not in order:
                    order.append(r.product_id)
        cat = self.d.catalog
        specific_cats = {cat.products[p].category for p in order if not cat.products[p].generic}
        # a generic id ("credit card") is redundant when a specific product of that category is present
        ctx.product_ids = [p for p in order if not (cat.products[p].generic and cat.products[p].category in specific_cats)]
        ctx.categories = {cat.products[p].category for p in ctx.product_ids}

    # ------------------------------------------------------------------ reduce
    async def _run_specialists(self, ctx: CallContext) -> dict[str, BaseModel]:
        agents = {"sales": self.sales, "customer": self.customer, "product": self.product,
                  "compliance": self.compliance}
        ctx.stats["applicable_rules"] = [r.id for r in self.compliance.applicable_rules(ctx)]
        results = await asyncio.gather(*(a.run(ctx) for a in agents.values()), return_exceptions=True)
        outputs: dict[str, BaseModel] = {}
        for (name, agent), res in zip(agents.items(), results):
            if isinstance(res, BaseException):
                log.warning("[%s] agent %s failed: %r", ctx.prepared.call_id, name, res)
                ctx.stats.setdefault("agent_failures", []).append(name)
                res = agent.empty() if hasattr(agent, "empty") else self.d.schemas.ProductOutput(products=[])
            outputs[name] = res  # type: ignore[assignment]
        return outputs

    def _synthesis_fallback(self, ctx: CallContext) -> BaseModel:
        ct = ctx.call_type_hint if ctx.call_type_hint in self.d.taxonomy.call_types else "other"
        return self.d.schemas.SynthesisOutput(call_type=ct, resolution_status="unclear", resolution_evidence=[],
                                              summary="", summary_evidence=[], empathy=3, clarity=3,
                                              product_knowledge=3, compliance_adherence=3,
                                              notes="synthesis unavailable")

    async def _run_synthesis(self, ctx: CallContext, outputs: dict[str, BaseModel],
                             feedback: str | None = None) -> BaseModel:
        try:
            return await self.synth.run(ctx, findings_digest(outputs), feedback)
        except Exception as exc:
            log.warning("[%s] synthesis failed: %r", ctx.prepared.call_id, exc)
            ctx.stats.setdefault("agent_failures", []).append("synthesis")
            return self._synthesis_fallback(ctx)

    # ------------------------------------------------------------------ verification loop
    async def _verify(self, ctx: CallContext, outputs: dict[str, BaseModel]) -> GateReport:
        cfg = self.d.settings.agents
        report = GateReport()
        verdicts: dict[str, tuple[str, str]] = {}
        collector = ClaimCollector(ctx, self.d.kb)
        agents = {"sales": self.sales, "customer": self.customer, "product": self.product,
                  "compliance": self.compliance}
        for attempt in range(cfg.max_retries + 1):
            claims = collector.collect(outputs)
            if cfg.critic_enabled:
                todo = [c for c in claims if c.status == "ok" and c.high_risk and c.evidence
                        and c.key not in verdicts][:30]
                if todo:
                    got = await self.critic.verify(ctx, todo)
                    verdicts.update(got)
                    report.critic_checked += len(got)
            for c in claims:
                if c.key in verdicts:
                    c.verdict, why = verdicts[c.key]
                    if c.verdict == "unsupported" and c.status == "ok":
                        c.status, c.reason = "unsupported", f"critic: {why}"
            report.claims = claims
            failing = [c for c in claims if c.high_risk and c.status != "ok"]
            if not failing or attempt == cfg.max_retries:
                break
            report.retries += 1
            fb: dict[str, list[str]] = {}
            for c in failing:
                fb.setdefault(c.agent, []).append(f"- {c.field}: \"{c.text[:160]}\" -> {c.status}: {c.reason[:200]}")
            rerun = [a for a in fb if a in agents]
            if rerun:
                res = await asyncio.gather(*(agents[a].run(ctx, feedback="\n".join(fb[a])) for a in rerun),
                                           return_exceptions=True)
                for a, r in zip(rerun, res):
                    if not isinstance(r, BaseException):
                        outputs[a] = r  # type: ignore[assignment]
            if rerun or "synthesis" in fb:
                outputs["synthesis"] = await self._run_synthesis(ctx, outputs, "\n".join(fb.get("synthesis", [])) or None)
        report.critic_rejected = sum(1 for c in report.claims if c.verdict == "unsupported")
        return report
