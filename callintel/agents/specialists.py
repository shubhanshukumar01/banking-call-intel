"""Specialist agents (reduce phase). Each reasons over its slice of grounded facts plus
retrieved knowledge (RAG), runs in parallel with the others, and short-circuits
deterministically (no LLM call) when its slice is empty.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from .base import BaseAgent, fmt_facts
from .context import CallContext
from .triage import HINT_TO_CALL_TYPES

# Transcript RAG for the two headline judgements. The map step can miss a single decisive line
# ("I got an SMS about a loan", "call me back next week"), so the owning agent also sees candidate
# lines retrieved by cue phrases; they are citable as L<n> and verified like any other evidence.
INTENT_CUES = re.compile(
    r"\b(?:i want(?:ed)? to|i would like|i'd like|i need|calling (?:about|regarding|for|to)|looking for|"
    r"can you help|want(?:ed)? to know|enquir\w*|inquir\w*|i got an? (?:sms|message|call|email|mail)|"
    r"reason for (?:my|the) call|i lost|i was charged|i did not make|there is a transaction|block)\b", re.I)
# Specific phrases only: bare "go ahead" / "that's all" are conversational filler and would crowd
# the real decision line out of the candidate list.
OUTCOME_CUES = re.compile(
    r"\b(?:go ahead (?:and|with)|apply|not interested|no thanks|call me back|call back|think about it|"
    r"i agree|sign me up|sounds good|let me think|decide|okay fine|don't want|do not want|not now|"
    r"maybe later|send me the details)\b", re.I)


def candidate_lines(ctx: CallContext, rx: re.Pattern, speaker: str, k: int, from_end: bool = False) -> str:
    nos = sorted(ctx.prepared.lines)
    if from_end:
        nos = nos[::-1]
    out = []
    for no in nos:
        ln = ctx.prepared.lines[no]
        if ln.speaker == speaker and rx.search(ln.text):
            out.append(ln)
            if len(out) >= k:
                break
    out.sort(key=lambda x: x.no)
    return "\n".join(ctx.tindex.fmt(ln, 200) for ln in out)


def with_candidates(evidence: str, title: str, cands: str) -> str:
    return f"{evidence}\n{title} (transcript lines, citable as L<n>):\n{cands}" if cands else evidence


def _products_line(ctx: CallContext, catalog) -> str:
    out = []
    for pid in ctx.product_ids:
        p = catalog.get(pid)
        ids = [f.id for f in ctx.facts if f.product_id == pid][:6]
        surface = sorted({f.product for f in ctx.facts if f.product_id == pid and f.product})[:3]
        out.append(f"{pid} = {p.name if p else pid} ({p.category if p else '?'}); said as: "
                   f"{', '.join(repr(s) for s in surface) or '-'}; facts: {','.join(ids) or '-'}")
    return "\n".join(out) or "(none detected)"


class SalesAgent(BaseAgent):
    name = "sales"
    prompt_id = "sales_agent"
    TYPES = {"offer_made", "buying_signal", "objection", "outcome", "next_step", "product_mention", "quoted_term"}

    def empty(self) -> BaseModel:
        return self.d.schemas.SalesOutput(pitch_made=False, pitch_evidence=[], sales_outcome="no_pitch",
                                          outcome_evidence=[], objections=[], buying_signals=[], cross_sell=[],
                                          next_steps=[], tool_requests=[])

    async def run(self, ctx: CallContext, feedback: str | None = None) -> BaseModel:
        facts = ctx.facts_of(self.TYPES, self.d.settings.agents.max_evidence_per_agent)
        if not any(f.type in ("offer_made", "buying_signal", "objection") for f in facts):
            return self.empty()
        labels = sorted({f.label for f in facts if f.type == "objection" and f.label != "other"})
        query = " ".join(["objection"] + labels) if labels else "buying signals cross-sell call closure"
        kb = "\n".join(f"- {d.text[:350]}" for d in await self.d.kb.search(query, "playbook", 2)) or "(none)"
        evidence = with_candidates(fmt_facts(facts), "OUTCOME CANDIDATES",
                                   candidate_lines(ctx, OUTCOME_CUES, "CUSTOMER", 4, from_end=True))
        return await self.call(ctx, self.d.schemas.SalesOutput, {
            "call_type_hint": ctx.call_type_hint, "products": _products_line(ctx, self.d.catalog),
            "kb": kb, "evidence": evidence}, feedback)


class CustomerAgent(BaseAgent):
    name = "customer"
    prompt_id = "customer_agent"
    TYPES = {"intent", "need", "complaint", "sentiment", "objection", "outcome", "next_step", "buying_signal"}

    def empty(self) -> BaseModel:
        return self.d.schemas.CustomerOutput(
            primary_intent="other", primary_intent_evidence=[], secondary_intents=[], needs=[], pain_points=[],
            sentiment_start="neutral", sentiment_end="neutral", sentiment_evidence=[], churn_risk="unclear",
            churn_evidence=[], vulnerability_indicators=[], tool_requests=[])

    async def run(self, ctx: CallContext, feedback: str | None = None) -> BaseModel:
        facts = ctx.facts_of(self.TYPES, self.d.settings.agents.max_evidence_per_agent)
        cands = candidate_lines(ctx, INTENT_CUES, "CUSTOMER", 4)
        if not facts and not cands:
            return self.empty()
        evidence = with_candidates(fmt_facts(facts), "INTENT CANDIDATES", cands)
        return await self.call(ctx, self.d.schemas.CustomerOutput, {"evidence": evidence}, feedback)


class ProductAgent(BaseAgent):
    name = "product"
    prompt_id = "product_agent"
    TYPES = {"product_mention", "offer_made", "quoted_term", "buying_signal", "objection", "outcome", "complaint"}

    async def run(self, ctx: CallContext, feedback: str | None = None) -> BaseModel:
        if not ctx.product_ids:
            return self.d.schemas.ProductOutput(products=[])
        facts = [f for f in ctx.facts_of(self.TYPES, self.d.settings.agents.max_evidence_per_agent)
                 if f.product_id or f.type != "product_mention"]
        return await self.call(ctx, self.d.schemas.ProductOutput, {
            "products": _products_line(ctx, self.d.catalog),
            "kb": self.d.kb.product_reference(ctx.product_ids),
            "evidence": fmt_facts(facts)}, feedback)


class ComplianceAgent(BaseAgent):
    name = "compliance"
    prompt_id = "compliance_agent"
    TYPES = {"compliance", "risk", "identity_check", "quoted_term", "offer_made", "outcome"}

    def applicable_rules(self, ctx: CallContext) -> list:
        call_types = HINT_TO_CALL_TYPES.get(ctx.call_type_hint, {ctx.call_type_hint})
        return self.d.kb.rules_for(call_types, ctx.categories)

    def empty(self) -> BaseModel:
        return self.d.schemas.ComplianceOutput(rules=[], risk_flags=[], identity_verification="unclear",
                                               identity_evidence=[], misquoted_terms=[], tool_requests=[])

    async def run(self, ctx: CallContext, feedback: str | None = None) -> BaseModel:
        rules = self.applicable_rules(ctx)
        facts = ctx.facts_of(self.TYPES, self.d.settings.agents.max_evidence_per_agent)
        if not rules and not facts:
            return self.empty()
        rule_lines = []
        for r in rules:
            # Transcript RAG: candidate lines for each rule, so "met"/"missed" is judged on real words.
            cands = ctx.tindex.search_lines(" ".join(r.search_phrases), 2)
            cand_txt = " | ".join(ctx.tindex.fmt(ln, 160) for ln in cands) or "none found"
            rule_lines.append(f"{r.id}: {r.title} - {r.description}\n  candidate lines: {cand_txt}")
        return await self.call(ctx, self.d.schemas.ComplianceOutput, {
            "call_type_hint": ctx.call_type_hint, "rules": "\n".join(rule_lines) or "(no rules apply)",
            "kb": self.d.kb.product_reference(ctx.product_ids), "evidence": fmt_facts(facts)}, feedback)


class SynthesisAgent(BaseAgent):
    name = "synthesis"
    prompt_id = "synthesis_agent"
    TYPES = {"intent", "outcome", "complaint", "next_step", "need", "offer_made", "buying_signal", "objection"}

    async def run(self, ctx: CallContext, findings: str, feedback: str | None = None) -> BaseModel:
        facts = ctx.facts_of(self.TYPES, 40)
        return await self.call(ctx, self.d.schemas.SynthesisOutput,
                               {"findings": findings, "evidence": fmt_facts(facts)}, feedback)


def findings_digest(outputs: dict[str, BaseModel]) -> str:
    """Compact text digest of specialist outputs for the synthesis agent."""
    lines: list[str] = []
    s = outputs.get("sales")
    if s is not None:
        obj = "; ".join(f"{o.category} ({','.join(o.evidence)})" for o in s.objections) or "none"
        lines.append(f"SALES: pitch_made={s.pitch_made}; outcome={s.sales_outcome} ({','.join(s.outcome_evidence)}); "
                     f"objections: {obj}; next_steps: {'; '.join(n.text for n in s.next_steps) or 'none'}")
    c = outputs.get("customer")
    if c is not None:
        lines.append(f"CUSTOMER: primary_intent={c.primary_intent} ({','.join(c.primary_intent_evidence)}); "
                     f"needs: {'; '.join(n.text for n in c.needs) or 'none'}; "
                     f"pain: {'; '.join(n.text for n in c.pain_points) or 'none'}; "
                     f"sentiment {c.sentiment_start}->{c.sentiment_end}; churn={c.churn_risk}")
    p = outputs.get("product")
    if p is not None and p.products:
        lines.append("PRODUCTS: " + "; ".join(f"{x.product_id} role={x.role} interest={x.customer_interest} "
                                               f"outcome={x.outcome}" for x in p.products))
    k = outputs.get("compliance")
    if k is not None:
        rules = "; ".join(f"{r.rule_id}={r.status}" for r in k.rules) or "none"
        risks = "; ".join(f"{r.flag}({r.severity})" for r in k.risk_flags) or "none"
        lines.append(f"COMPLIANCE: rules: {rules}; risks: {risks}; identity={k.identity_verification}")
    return "\n".join(lines)
