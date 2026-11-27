"""Build the final CallAnalysis JSON from verified agent outputs.

Only claims whose verification status is "ok" survive; failed single-valued fields
fall back to "unclear" (abstain over invent). Every surviving claim carries its
evidence (line, speaker, masked quote) and a computed confidence.
"""
from __future__ import annotations

from collections import Counter
from statistics import mean
from typing import Any

from pydantic import BaseModel

from ..schemas import (AgentPerformance, CallAnalysis, CallProfile, Claim, ComplianceInsights,
                       CrossSellInsight, CustomerInsights, FactOut, Labeled, MisquotedTerm, Normalization,
                       ObjectionInsight, PrivacyInfo, ProcessingInfo, ProductInsight, Quality, QuotedTerm,
                       ReviewItem, RiskFlag, RuleResult, SalesInsights, Sentiment, SourceInfo)
from ..utils import now_iso
from ..verify.gate import ClaimRec, GateReport
from .context import AgentDeps, CallContext

_TIER_RANK = {"exact": 4, "learned": 3, "fuzzy": 2, "llm": 1, "unresolved": 0}


class Assembler:
    def __init__(self, deps: AgentDeps, ctx: CallContext, report: GateReport) -> None:
        self.d = deps
        self.ctx = ctx
        self.report = report
        self.refs = report.by_ref()

    def _get(self, ref: str) -> ClaimRec | None:
        return self.refs.get(ref)

    def _ok(self, ref: str) -> ClaimRec | None:
        c = self.refs.get(ref)
        return c if c is not None and c.status == "ok" else None

    def _labeled(self, ref: str, value: str, fallback: str = "unclear") -> Labeled:
        c = self._get(ref)
        if c is None:  # abstention value that needed no evidence
            return Labeled(value=value, evidence=[], confidence=0.6)
        if c.status != "ok":
            return Labeled(value=fallback, evidence=[], confidence=0.0)
        return Labeled(value=value, evidence=c.evidence, confidence=c.confidence)

    def _claims(self, prefix: str, items: list[Any]) -> list[Claim]:
        out = []
        for i, it in enumerate(items):
            c = self._ok(f"{prefix}[{i}]")
            if c:
                out.append(Claim(text=it.text, evidence=c.evidence, confidence=c.confidence))
        return out

    # ------------------------------------------------------------------ sections
    def _sales(self, s: BaseModel) -> SalesInsights:
        cat = self.d.catalog
        objections = []
        for i, o in enumerate(s.objections):
            c = self._ok(f"sales.objections[{i}]")
            if c:
                objections.append(ObjectionInsight(category=o.category, detail=o.detail, handled=o.handled,
                                                   evidence=c.evidence, confidence=c.confidence))
        cross = []
        for i, x in enumerate(s.cross_sell):
            c = self._ok(f"sales.cross_sell[{i}]")
            if c:
                p = cat.get(x.product_id)
                cross.append(CrossSellInsight(product_id=x.product_id, product_name=p.name if p else x.product_id,
                                              accepted=x.accepted, evidence=c.evidence, confidence=c.confidence))
        pitch = bool(s.pitch_made and self._ok("sales.pitch_made[0]"))
        return SalesInsights(
            pitch_made=pitch,
            sales_outcome=self._labeled("sales.sales_outcome[0]", s.sales_outcome),
            objections=objections, buying_signals=self._claims("sales.buying_signals", s.buying_signals),
            cross_sell=cross, next_steps=self._claims("sales.next_steps", s.next_steps))

    def _customer(self, c: BaseModel) -> tuple[CustomerInsights, Labeled, list[Labeled], Sentiment]:
        sents = self.d.taxonomy.sentiments
        primary = self._labeled("customer.primary_intent[0]", c.primary_intent, "other")
        secondary = []
        for i, it in enumerate(c.secondary_intents):
            cl = self._ok(f"customer.secondary_intents[{i}]")
            if cl:
                secondary.append(Labeled(value=it.intent, evidence=cl.evidence, confidence=cl.confidence))
        a, b = sents.index(c.sentiment_start), sents.index(c.sentiment_end)
        trajectory = "improving" if b > a else "declining" if b < a else "stable"
        ev, _, _ = self.ctx.registry.resolve(c.sentiment_evidence)
        sentiment = Sentiment(start=c.sentiment_start, end=c.sentiment_end, trajectory=trajectory, evidence=ev)
        churn = self._labeled("customer.churn_risk[0]", c.churn_risk) if c.churn_risk in ("medium", "high") \
            else Labeled(value=c.churn_risk, evidence=[], confidence=0.6)
        insights = CustomerInsights(
            needs=self._claims("customer.needs", c.needs),
            pain_points=self._claims("customer.pain_points", c.pain_points), churn_risk=churn,
            vulnerability_indicators=self._claims("customer.vulnerability_indicators", c.vulnerability_indicators))
        return insights, primary, secondary, sentiment

    def _products(self, p: BaseModel) -> list[ProductInsight]:
        cat = self.d.catalog
        facts = self.ctx.facts
        out: dict[str, ProductInsight] = {}
        for i, x in enumerate(p.products):
            if x.product_id not in self.ctx.product_ids:
                continue  # agent named a product never mentioned in this call
            c = self._ok(f"product.products[{i}]")
            if not c or x.product_id in out:
                continue
            out[x.product_id] = self._product(x.product_id, x.role, x.customer_interest, x.outcome,
                                              list(x.key_points)[:3], c.evidence, c.confidence)
        for pid in self.ctx.product_ids:  # detected but not described -> still reported, consistently
            if pid not in out:
                fs = [f for f in facts if f.product_id == pid][:3]
                ev, scores, _ = self.ctx.registry.resolve([f.id for f in fs])
                out[pid] = self._product(pid, "other", "unclear", "unclear", [], ev,
                                         round(mean(scores) * 0.9, 3) if scores else 0.0)
        return list(out.values())

    def _product(self, pid: str, role: str, interest: str, outcome: str, points: list[str], ev, conf) -> ProductInsight:
        p = self.d.catalog.get(pid)
        fs = [f for f in self.ctx.facts if f.product_id == pid]
        mentions = sorted({f.product for f in fs if f.product})[:6]
        norms = [f.norm for f in fs if f.norm]
        best = max(norms, key=lambda r: (_TIER_RANK.get(r.tier, 0), r.confidence), default=None)
        return ProductInsight(product_id=pid, product_name=p.name if p else pid, category=p.category if p else "other",
                              mentions=mentions, role=role, customer_interest=interest, outcome=outcome,
                              key_points=points, evidence=ev, confidence=conf,
                              normalization=Normalization(tier=best.tier if best else "exact",
                                                          confidence=round(best.confidence, 3) if best else 1.0))

    def _compliance(self, k: BaseModel, review: list[ReviewItem]) -> ComplianceInsights:
        kb = self.d.kb
        rules: dict[str, RuleResult] = {}
        for i, r in enumerate(k.rules):
            rule = kb.rule(r.rule_id)
            if r.rule_id in rules or rule is None:
                continue
            c = self._get(f"compliance.rules.{r.rule_id}[0]")
            status, note, ev, conf = r.status, r.note, [], 0.6
            if c is not None and c.status != "ok":
                status, note = "unclear", f"verification failed: {c.reason[:150]}"
            elif c is not None:
                ev, conf = c.evidence, c.confidence
            rules[r.rule_id] = RuleResult(rule_id=r.rule_id, title=rule.title, status=status,
                                          severity=rule.severity_if_missed, note=note, evidence=ev, confidence=conf)
        for rid in self.ctx.stats.get("applicable_rules", []):
            if rid not in rules:
                rule = kb.rule(rid)
                rules[rid] = RuleResult(rule_id=rid, title=rule.title if rule else rid, status="unclear",
                                        severity=rule.severity_if_missed if rule else "medium",
                                        note="not assessed by agent", evidence=[], confidence=0.0)
        risks = []
        for i, f in enumerate(k.risk_flags):
            c = self._ok(f"compliance.risk_flags[{i}]")
            if c:
                risks.append(RiskFlag(flag=f.flag, severity=f.severity, detail=f.detail, evidence=c.evidence,
                                      confidence=c.confidence))
        misq = []
        for i, m in enumerate(k.misquoted_terms):
            c = self._ok(f"compliance.misquoted_terms[{i}]")
            if c:
                misq.append(MisquotedTerm(product_id=m.product_id, term=m.term, stated=m.stated,
                                          reference=m.reference, evidence=c.evidence, numeric_verified=True))
        if k.identity_verification == "performed":
            identity = self._labeled("compliance.identity_verification[0]", k.identity_verification)
        else:
            c = self._get("compliance.identity_verification[0]")
            failed = c is not None and c.status != "ok"   # e.g. "not_performed" contradicted by facts
            identity = Labeled(value="unclear" if failed else k.identity_verification, confidence=0.0 if failed else 0.6)
        return ComplianceInsights(rules=list(rules.values()), risk_flags=risks, identity_verification=identity,
                                  misquoted_terms=misq)

    def _quoted_terms(self) -> list[QuotedTerm]:
        out = []
        for f in self.ctx.facts:
            if f.type == "quoted_term" and f.number:
                ev, _, _ = self.ctx.registry.resolve([f.id])
                out.append(QuotedTerm(product_id=f.product_id, description=f.value, number=f.number, evidence=ev,
                                      numeric_verified=f.number_verified))
        return out[:30]

    # ------------------------------------------------------------------ build
    def build(self, outputs: dict[str, BaseModel], *, usage: Any, stage_ms: dict[str, float], duration_ms: float,
              prompt_versions: dict[str, str], vault_stored: bool) -> CallAnalysis:
        s = self.d.settings
        ctx, p = self.ctx, self.ctx.prepared
        review: list[ReviewItem] = []
        for c in self.report.claims:
            if c.status != "ok" and c.high_risk:
                review.append(ReviewItem(field=c.ref, reason=f"{c.status}: {c.reason[:200]}"))
        for name in ctx.stats.get("agent_failures", []):
            review.append(ReviewItem(field=name, reason="agent failed; section defaulted"))
        if ctx.stats.get("map_failures"):
            review.append(ReviewItem(field="map", reason=f"{ctx.stats['map_failures']} chunk(s) failed extraction"))

        syn = outputs["synthesis"]
        customer, primary, secondary, sentiment = self._customer(outputs["customer"])
        summary_c = self._get("synthesis.summary[0]")
        summary_ok = summary_c is None or summary_c.status == "ok"
        profile = CallProfile(
            call_type=syn.call_type, primary_intent=primary, secondary_intents=secondary,
            resolution_status=self._labeled("synthesis.resolution_status[0]", syn.resolution_status),
            sentiment=sentiment,
            summary=syn.summary if summary_ok else "",
            summary_evidence=summary_c.evidence if (summary_c and summary_ok) else [])

        kept = [c for c in self.report.claims if c.status == "ok" and c.evidence]
        overall = round(mean(c.confidence for c in kept), 3) if kept else 0.0
        status = "needs_review" if (review or (kept and overall < s.agents.min_confidence)) else "validated"
        fact_types = Counter(f.type for f in ctx.facts)
        other_labels = [f.value for f in ctx.facts
                        if f.label == "other" and f.type in ("intent", "objection", "complaint", "risk")][:10]

        return CallAnalysis(
            schema_version=s.app.schema_version,
            call_id=p.call_id,
            source=SourceInfo(file=p.file_name, sha256=p.sha256, raw_lines=p.raw_line_count,
                              kept_lines=len(p.lines), turns=len(p.turns), chunks_total=len(p.chunks),
                              chunks_analyzed=ctx.stats.get("chunks_selected", 0),
                              raw_tokens=p.noise_stats.get("raw_tokens", 0),
                              clean_tokens=p.noise_stats.get("clean_tokens", 0)),
            processing=ProcessingInfo(
                pipeline_version=s.app.pipeline_version, processed_at=now_iso(), duration_ms=duration_ms,
                stage_ms=stage_ms, llm_profile=s.llm.active_profile,
                models={r: self.d.gateway.role_model(r) for r in ("map", "reduce", "critic", "normalizer")},
                prompt_versions=prompt_versions, llm_calls=usage.calls, llm_cache_hits=usage.cache_hits,
                tokens_in=usage.tokens_in, tokens_out=usage.tokens_out, json_repairs=usage.repairs,
                llm_by_role=usage.by_role),
            privacy=PrivacyInfo(pii_masked=dict(p.pii_counts), detectors=p.detectors, vault_stored=vault_stored,
                                output_pii_findings=0),
            call_profile=profile,
            products=self._products(outputs["product"]),
            sales=self._sales(outputs["sales"]),
            customer=customer,
            compliance=self._compliance(outputs["compliance"], review),
            quoted_terms=self._quoted_terms(),
            agent_performance=AgentPerformance(empathy=syn.empathy, clarity=syn.clarity,
                                               product_knowledge=syn.product_knowledge,
                                               compliance_adherence=syn.compliance_adherence, notes=syn.notes),
            domain_insights={
                "fact_type_counts": dict(fact_types),
                "taxonomy_other_values": other_labels,
                "call_type_hint": ctx.call_type_hint,
                "triage": {"chunks_selected": ctx.stats.get("chunks_selected", 0),
                           "chunks_skipped": ctx.stats.get("chunks_skipped", 0)},
                "agent_tool_calls": ctx.stats.get("tool_calls", 0),
                "noise": {k: v for k, v in p.noise_stats.items() if k.startswith("dropped")},
            },
            facts=[FactOut(id=f.id, type=f.type, label=f.label, value=f.value, product_id=f.product_id,
                           number=f.number, line=f.line, speaker=f.speaker, quote=f.quote, grounding=f.grounding,
                           source=f.source) for f in ctx.facts] if s.output.include_facts else [],
            quality=Quality(
                status=status, overall_confidence=overall, claims_total=len(self.report.claims),
                claims_grounded=sum(1 for c in self.report.claims if c.status == "ok"),
                claims_dropped_ungrounded=sum(1 for c in self.report.claims if c.status == "ungrounded"),
                facts_extracted=ctx.stats.get("facts_kept", 0),
                facts_dropped_ungrounded=ctx.stats.get("facts_dropped_ungrounded", 0),
                critic_checked=self.report.critic_checked, critic_rejected=self.report.critic_rejected,
                retries=self.report.retries, review_items=review),
        )
