"""Hallucination gate.

Layers (cheapest first):
  1. constrained decoding  - enums / JSON schema at generation time (gateway)
  2. evidence-by-reference - every claim must cite F/L ids that exist (registry)
  3. span grounding        - fact quotes fuzzy-matched to the cited source line (map stage)
  4. numeric fidelity      - figures in misquote claims must appear in the cited lines
  5. contradiction probe   - a "missed" disclosure is re-searched in the transcript
  6. critic agent          - high-risk claims judged against the cited text
  7. targeted retry        - only the agents owning failed high-risk claims are re-asked
  8. abstain               - still failing -> claim dropped / set to "unclear" + review item
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from pydantic import BaseModel

from ..agents.context import CallContext, EvidenceRegistry
from ..rag.knowledge_base import KnowledgeBase
from ..schemas import Evidence
from .grounding import confidence, number_supported, quote_score


@dataclass
class ClaimRec:
    key: str
    agent: str
    field: str
    text: str
    ids: list[str]
    evidence: list[Evidence]
    grounding: float
    invalid: list[str]
    needs_evidence: bool
    high_risk: bool
    severity: str = "medium"
    verdict: str | None = None
    reason: str = ""
    status: str = "ok"            # ok | ungrounded | unsupported | contradicted
    hint: str = ""
    ref: str = ""                 # "agent.field[i]" - position in the final outputs (for assembly)

    @property
    def confidence(self) -> float:
        if not self.needs_evidence and not self.evidence:
            return 0.6  # abstention-type values ("unclear", "no_pitch"): neutral confidence
        return confidence(self.grounding, len(self.evidence), self.verdict)


def ckey(agent: str, field_: str, text: str, ids: list[str]) -> str:
    return f"{agent}|{field_}|{text.strip().lower()[:160]}|{','.join(sorted(EvidenceRegistry.parse_ids(ids)))}"


class ClaimCollector:
    NO_EVIDENCE_VALUES = {"unclear", "no_pitch", "not_applicable", "other", "neutral", "low", "none"}

    def __init__(self, ctx: CallContext, kb: KnowledgeBase) -> None:
        self.ctx = ctx
        self.reg = ctx.registry
        self.kb = kb
        self.claims: list[ClaimRec] = []
        self._pos: dict[str, int] = {}

    def _add(self, agent: str, field_: str, text: str, ids: list[str], *, needs: bool = True,
             high: bool = False, severity: str = "medium", number: str | None = None) -> ClaimRec:
        ev, scores, invalid = self.reg.resolve(ids)
        slot = f"{agent}.{field_}"
        i = self._pos.get(slot, 0)
        self._pos[slot] = i + 1
        c = ClaimRec(ckey(agent, field_, text, ids), agent, field_, text, list(ids), ev,
                     mean(scores) if scores else 0.0, invalid, needs, high, severity, ref=f"{slot}[{i}]")
        if needs and not ev:
            c.status = "ungrounded"
            c.reason = f"no valid evidence ids (invalid: {invalid})" if invalid else "no evidence cited"
        elif number and ev and not number_supported(number, self.reg.texts_for(ev)):
            c.status = "ungrounded"
            c.reason = f"figure '{number}' not found in cited lines"
        self.claims.append(c)
        return c

    def collect(self, outputs: dict[str, BaseModel]) -> list[ClaimRec]:
        self.claims = []
        self._pos = {}
        s = outputs.get("sales")
        if s is not None:
            if s.pitch_made:
                self._add("sales", "pitch_made", "The agent pitched or offered a product", s.pitch_evidence)
            if s.sales_outcome not in self.NO_EVIDENCE_VALUES:
                self._add("sales", "sales_outcome", f"Sales outcome of the call: {s.sales_outcome}",
                          s.outcome_evidence, high=True)
            for o in s.objections:
                self._add("sales", "objections", f"Customer objection ({o.category}): {o.detail}", o.evidence)
            for b in s.buying_signals:
                self._add("sales", "buying_signals", b.text, b.evidence)
            for x in s.cross_sell:
                self._add("sales", "cross_sell", f"Agent cross-sold {x.product_id}; accepted: {x.accepted}", x.evidence)
            for n in s.next_steps:
                self._add("sales", "next_steps", n.text, n.evidence)
        c = outputs.get("customer")
        if c is not None:
            # high-risk: the headline analytics field, so the critic checks that the cited lines really
            # express this intent (quotes can be verbatim yet irrelevant, e.g. "Can you repeat that?")
            self._add("customer", "primary_intent", f"Customer's main reason for calling: {c.primary_intent}",
                      c.primary_intent_evidence, needs=c.primary_intent != "other", high=c.primary_intent != "other")
            for it in c.secondary_intents:
                self._add("customer", "secondary_intents", f"Customer also wanted: {it.intent}", it.evidence)
            for n in c.needs:
                self._add("customer", "needs", n.text, n.evidence)
            for n in c.pain_points:
                self._add("customer", "pain_points", n.text, n.evidence)
            if c.churn_risk in ("medium", "high"):
                self._add("customer", "churn_risk", f"Customer churn risk is {c.churn_risk}", c.churn_evidence,
                          high=c.churn_risk == "high")
            for v in c.vulnerability_indicators:
                self._add("customer", "vulnerability_indicators", f"Customer vulnerability: {v.text}", v.evidence,
                          high=True)
        p = outputs.get("product")
        if p is not None:
            for x in p.products:
                self._add("product", "products", f"{x.product_id}: role={x.role}, interest={x.customer_interest}, "
                          f"outcome={x.outcome}", x.evidence, high=x.outcome == "accepted")
        k = outputs.get("compliance")
        if k is not None:
            for r in k.rules:
                rule = self.kb.rule(r.rule_id)
                title = rule.title if rule else r.rule_id
                if r.status == "met":
                    self._add("compliance", f"rules.{r.rule_id}", f"Rule '{title}' was satisfied: {r.note}",
                              r.evidence, high=True, severity=rule.severity_if_missed if rule else "medium")
                elif r.status == "missed":
                    cl = self._add("compliance", f"rules.{r.rule_id}", f"Rule '{title}' was missed: {r.note}",
                                   r.evidence, needs=False, severity=rule.severity_if_missed if rule else "medium")
                    self._probe_missed(cl, rule)
                else:
                    self._add("compliance", f"rules.{r.rule_id}", f"Rule '{title}': {r.status}", r.evidence, needs=False)
            for f in k.risk_flags:
                self._add("compliance", "risk_flags", f"Risk ({f.flag}, {f.severity}): {f.detail}", f.evidence,
                          high=True, severity=f.severity)
            if k.identity_verification == "performed":
                self._add("compliance", "identity_verification", "The agent verified the customer's identity",
                          k.identity_evidence, high=True)
            elif k.identity_verification == "not_performed":
                cl = self._add("compliance", "identity_verification", "The agent did not verify the customer's identity",
                               k.identity_evidence, needs=False)
                checks = [f for f in self.ctx.facts if f.type == "identity_check"]
                if checks:  # contradiction probe 1: verification facts exist
                    cl.status, cl.high_risk = "contradicted", True
                    cl.hint = cl.reason = ("identity-check facts exist: " +
                                           "; ".join(f'{f.id} L{f.line} "{f.quote[:80]}"' for f in checks[:3]))
                else:       # contradiction probe 2: verification phrases spoken by the agent
                    self._probe_missed(cl, self.kb.rule("ID_VERIFICATION"), speaker="AGENT")
            for m in k.misquoted_terms:
                self._add("compliance", "misquoted_terms",
                          f"Agent stated {m.term} as {m.stated} for {m.product_id}; reference is {m.reference}",
                          m.evidence, high=True, number=m.stated)
        y = outputs.get("synthesis")
        if y is not None:
            self._add("synthesis", "summary", f"Call summary: {y.summary}", y.summary_evidence,
                      needs=bool(y.summary.strip()), high=bool(y.summary.strip()))
            if y.resolution_status != "unclear":
                self._add("synthesis", "resolution_status", f"Resolution status: {y.resolution_status}",
                          y.resolution_evidence)
        return self.claims

    def _probe_missed(self, claim: ClaimRec, rule, speaker: str | None = None) -> None:
        """A 'missed' disclosure is contradicted if one of the rule's phrases is clearly present."""
        if rule is None or not rule.search_phrases:
            return
        for ln in self.ctx.tindex.search_lines(" ".join(rule.search_phrases), 4):
            if speaker and ln.speaker != speaker:
                continue
            if any(quote_score(ph, ln.text) >= 0.9 for ph in rule.search_phrases):
                claim.status = "contradicted"
                claim.hint = f'L{ln.no} appears to satisfy {rule.id}: "{ln.text[:160]}"'
                claim.reason = claim.hint
                claim.high_risk = True
                return


@dataclass
class GateReport:
    claims: list[ClaimRec] = field(default_factory=list)
    critic_checked: int = 0
    critic_rejected: int = 0
    retries: int = 0

    def by_ref(self) -> dict[str, ClaimRec]:
        return {c.ref: c for c in self.claims}
