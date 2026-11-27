"""Data contracts.

1. LLM output models - built at start-up from taxonomy.yaml / products.yaml so every
   categorical field is a JSON-schema enum. The decoder can only emit allowed values,
   which is what makes 1,000s of calls comparable.
2. CallAnalysis - the final per-call JSON ("fixed core + extensible domain_insights").
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, create_model


# ============================================================================ taxonomy
class Taxonomy(BaseModel):
    call_types: list[str]
    intents: list[str]
    objections: list[str]
    complaints: list[str]
    risk_flags: list[str]
    sales_outcomes: list[str]
    resolution_statuses: list[str]
    sentiments: list[str]
    churn_risk: list[str]
    product_roles: list[str]
    product_interest: list[str]
    product_outcomes: list[str]
    rule_statuses: list[str]
    yes_no_partial: list[str]
    identity_verification: list[str]
    severity: list[str]
    fact_types: dict[str, str]

    @classmethod
    def load(cls, path: str | Path) -> "Taxonomy":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.model_validate(yaml.safe_load(fh))

    def labels_for(self, fact_type: str) -> list[str]:
        return {
            "product_mention": self.product_roles,
            "intent": self.intents,
            "objection": self.objections,
            "complaint": self.complaints,
            "risk": self.risk_flags,
            "outcome": ["accepted", "declined", "resolved", "escalated", "other"],
        }.get(fact_type, ["other"])

    def fact_type_guide(self) -> str:
        lines = []
        for ft, desc in self.fact_types.items():
            labels = self.labels_for(ft)
            lab = "other" if labels == ["other"] else "|".join(labels)
            lines.append(f"- {ft}: {desc}. label: {lab}")
        return "\n".join(lines)

    def all_labels(self) -> list[str]:
        out: set[str] = {"other"}
        for ft in self.fact_types:
            out.update(self.labels_for(ft))
        return sorted(out)


def _L(values: list[str] | tuple) -> Any:
    return Literal[tuple(values)]  # type: ignore[valid-type]


Score = Literal[1, 2, 3, 4, 5]


class ToolRequest(BaseModel):
    tool: Literal["search_transcript", "get_lines", "search_kb"]
    arg: str


class TextClaim(BaseModel):
    text: str
    evidence: list[str]


class CriticVerdict(BaseModel):
    claim_id: str
    verdict: Literal["supported", "partially_supported", "unsupported"]
    reason: str


class CriticOutput(BaseModel):
    verdicts: list[CriticVerdict]


class LLMSchemas:
    """Enum-constrained output models for every agent."""

    def __init__(self, tax: Taxonomy, product_ids: list[str], rule_ids: list[str], max_facts: int = 15) -> None:
        self.tax = tax
        P = _L(product_ids)
        # field order = generation order: decide type/label, cite line, copy quote, then summarise
        self.Fact = create_model(
            "Fact", type=(_L(list(tax.fact_types)), ...), label=(_L(tax.all_labels()), ...), line=(int, ...),
            quote=(str, ...), value=(str, ...), product=(Optional[str], None), number=(Optional[str], None))
        # maxItems is enforced by grammar decoders (Ollama) during generation, never by validation:
        # an over-long answer from a provider that ignores it is truncated, not rejected.
        self.MapOutput = create_model("MapOutput", facts=(list[self.Fact], Field(
            ..., json_schema_extra={"maxItems": max_facts})))

        Objection = create_model("ObjectionOut", category=(_L(tax.objections), ...), detail=(str, ...),
                                 handled=(_L(tax.yes_no_partial), ...), evidence=(list[str], ...))
        CrossSell = create_model("CrossSellOut", product_id=(P, ...), accepted=(_L(tax.yes_no_partial), ...),
                                 evidence=(list[str], ...))
        self.SalesOutput = create_model(
            "SalesOutput", pitch_made=(bool, ...), pitch_evidence=(list[str], ...),
            sales_outcome=(_L(tax.sales_outcomes), ...), outcome_evidence=(list[str], ...),
            objections=(list[Objection], ...), buying_signals=(list[TextClaim], ...),
            cross_sell=(list[CrossSell], ...), next_steps=(list[TextClaim], ...),
            tool_requests=(list[ToolRequest], ...))

        Intent = create_model("IntentOut", intent=(_L(tax.intents), ...), evidence=(list[str], ...))
        self.CustomerOutput = create_model(
            "CustomerOutput", primary_intent=(_L(tax.intents), ...), primary_intent_evidence=(list[str], ...),
            secondary_intents=(list[Intent], ...), needs=(list[TextClaim], ...), pain_points=(list[TextClaim], ...),
            sentiment_start=(_L(tax.sentiments), ...), sentiment_end=(_L(tax.sentiments), ...),
            sentiment_evidence=(list[str], ...), churn_risk=(_L(tax.churn_risk), ...),
            churn_evidence=(list[str], ...), vulnerability_indicators=(list[TextClaim], ...),
            tool_requests=(list[ToolRequest], ...))

        ProductItem = create_model(
            "ProductItemOut", product_id=(P, ...), role=(_L(tax.product_roles), ...),
            customer_interest=(_L(tax.product_interest), ...), outcome=(_L(tax.product_outcomes), ...),
            key_points=(list[str], ...), evidence=(list[str], ...))
        self.ProductOutput = create_model("ProductOutput", products=(list[ProductItem], ...))

        Rule = create_model("RuleOut", rule_id=(_L(rule_ids), ...), status=(_L(tax.rule_statuses), ...),
                            evidence=(list[str], ...), note=(str, ...))
        Risk = create_model("RiskOut", flag=(_L(tax.risk_flags), ...), severity=(_L(tax.severity), ...),
                            detail=(str, ...), evidence=(list[str], ...))
        Misquote = create_model("MisquoteOut", product_id=(P, ...), term=(str, ...), stated=(str, ...),
                                reference=(str, ...), evidence=(list[str], ...))
        self.ComplianceOutput = create_model(
            "ComplianceOutput", rules=(list[Rule], ...), risk_flags=(list[Risk], ...),
            identity_verification=(_L(tax.identity_verification), ...), identity_evidence=(list[str], ...),
            misquoted_terms=(list[Misquote], ...), tool_requests=(list[ToolRequest], ...))

        self.SynthesisOutput = create_model(
            "SynthesisOutput", call_type=(_L(tax.call_types), ...),
            resolution_status=(_L(tax.resolution_statuses), ...), resolution_evidence=(list[str], ...),
            summary=(str, ...), summary_evidence=(list[str], ...), empathy=(Score, ...), clarity=(Score, ...),
            product_knowledge=(Score, ...), compliance_adherence=(Score, ...), notes=(str, ...))

        self.CriticOutput = CriticOutput

    @lru_cache(maxsize=512)
    def normalizer_output(self, candidates: tuple[str, ...]) -> type[BaseModel]:
        return create_model("NormalizerOutput", product_id=(_L(list(candidates) + ["NONE"]), ...))


# ============================================================================ final output
class Evidence(BaseModel):
    line: int
    speaker: str
    quote: str


class Claim(BaseModel):
    text: str
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = 0.0


class Labeled(BaseModel):
    value: str
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = 0.0


class SourceInfo(BaseModel):
    file: str
    sha256: str
    raw_lines: int
    kept_lines: int
    turns: int
    chunks_total: int
    chunks_analyzed: int
    raw_tokens: int
    clean_tokens: int


class ProcessingInfo(BaseModel):
    pipeline_version: str
    processed_at: str
    duration_ms: float
    stage_ms: dict[str, float]
    llm_profile: str
    models: dict[str, str]
    prompt_versions: dict[str, str]
    llm_calls: int
    llm_cache_hits: int
    tokens_in: int
    tokens_out: int
    json_repairs: int
    llm_by_role: dict[str, dict[str, float]] = Field(default_factory=dict)


class PrivacyInfo(BaseModel):
    pii_masked: dict[str, int]
    detectors: list[str]
    vault_stored: bool
    output_pii_findings: int


class Sentiment(BaseModel):
    start: str
    end: str
    trajectory: str
    evidence: list[Evidence] = Field(default_factory=list)


class CallProfile(BaseModel):
    call_type: str
    primary_intent: Labeled
    secondary_intents: list[Labeled] = Field(default_factory=list)
    resolution_status: Labeled
    sentiment: Sentiment
    summary: str
    summary_evidence: list[Evidence] = Field(default_factory=list)


class Normalization(BaseModel):
    tier: str
    confidence: float


class ProductInsight(BaseModel):
    product_id: str
    product_name: str
    category: str
    mentions: list[str]
    role: str
    customer_interest: str
    outcome: str
    key_points: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = 0.0
    normalization: Normalization


class ObjectionInsight(BaseModel):
    category: str
    detail: str
    handled: str
    evidence: list[Evidence]
    confidence: float


class CrossSellInsight(BaseModel):
    product_id: str
    product_name: str
    accepted: str
    evidence: list[Evidence]
    confidence: float


class SalesInsights(BaseModel):
    pitch_made: bool
    sales_outcome: Labeled
    objections: list[ObjectionInsight] = Field(default_factory=list)
    buying_signals: list[Claim] = Field(default_factory=list)
    cross_sell: list[CrossSellInsight] = Field(default_factory=list)
    next_steps: list[Claim] = Field(default_factory=list)


class CustomerInsights(BaseModel):
    needs: list[Claim] = Field(default_factory=list)
    pain_points: list[Claim] = Field(default_factory=list)
    churn_risk: Labeled
    vulnerability_indicators: list[Claim] = Field(default_factory=list)


class RuleResult(BaseModel):
    rule_id: str
    title: str
    status: str
    severity: str
    note: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = 0.0


class RiskFlag(BaseModel):
    flag: str
    severity: str
    detail: str
    evidence: list[Evidence]
    confidence: float


class MisquotedTerm(BaseModel):
    product_id: str
    term: str
    stated: str
    reference: str
    evidence: list[Evidence]
    numeric_verified: bool


class ComplianceInsights(BaseModel):
    rules: list[RuleResult] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    identity_verification: Labeled
    misquoted_terms: list[MisquotedTerm] = Field(default_factory=list)


class QuotedTerm(BaseModel):
    product_id: Optional[str]
    description: str
    number: str
    evidence: list[Evidence]
    numeric_verified: bool


class AgentPerformance(BaseModel):
    empathy: Optional[int] = None
    clarity: Optional[int] = None
    product_knowledge: Optional[int] = None
    compliance_adherence: Optional[int] = None
    notes: str = ""


class ReviewItem(BaseModel):
    field: str
    reason: str


class FactOut(BaseModel):
    """Atomic grounded fact from the map step (masked quote, original line number)."""
    id: str
    type: str
    label: str
    value: str
    product_id: Optional[str] = None
    number: Optional[str] = None
    line: int
    speaker: str
    quote: str
    grounding: float
    source: str


class Quality(BaseModel):
    status: Literal["validated", "needs_review"]
    overall_confidence: float
    claims_total: int
    claims_grounded: int
    claims_dropped_ungrounded: int
    facts_extracted: int
    facts_dropped_ungrounded: int
    critic_checked: int
    critic_rejected: int
    retries: int
    review_items: list[ReviewItem] = Field(default_factory=list)


class CallAnalysis(BaseModel):
    schema_version: str
    call_id: str
    source: SourceInfo
    processing: ProcessingInfo
    privacy: PrivacyInfo
    call_profile: CallProfile
    products: list[ProductInsight] = Field(default_factory=list)
    sales: SalesInsights
    customer: CustomerInsights
    compliance: ComplianceInsights
    quoted_terms: list[QuotedTerm] = Field(default_factory=list)
    agent_performance: AgentPerformance
    domain_insights: dict[str, Any] = Field(default_factory=dict)
    facts: list[FactOut] = Field(default_factory=list)
    quality: Quality
