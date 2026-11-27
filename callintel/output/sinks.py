"""Output sinks: atomic per-call JSON, per-process JSONL, and flattened BI tables
(Parquet, CSV fallback) for Power BI / warehouse loading."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..config import Settings
from ..schemas import CallAnalysis
from ..utils import atomic_write_text, dumps, loads, today


class OutputSink:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.root = settings.path(settings.paths.output_dir)

    def write(self, analysis: CallAnalysis) -> str:
        day = today()
        data = analysis.model_dump(mode="json")
        path = self.root / "calls" / day / f"{analysis.call_id}.json"
        if self.s.output.write_per_call_json:
            atomic_write_text(path, dumps(data, indent=True))
        if self.s.output.write_jsonl:
            jl = self.root / "jsonl" / f"insights_{day}_{os.getpid()}.jsonl"
            jl.parent.mkdir(parents=True, exist_ok=True)
            with open(jl, "a", encoding="utf-8") as fh:  # one writer per process -> no interleaving
                fh.write(dumps(data) + "\n")
        return str(path)


def _ev_lines(ev: list[dict]) -> str:
    return ",".join(str(e["line"]) for e in ev)


def flatten(call: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    cid = call["call_id"]
    cp = call["call_profile"]
    q = call["quality"]
    pr = call["processing"]
    rows: dict[str, list[dict[str, Any]]] = {k: [] for k in
                                             ("calls", "products", "objections", "compliance_rules", "risk_flags",
                                              "quoted_terms", "intents", "facts")}
    for f in call.get("facts", []):
        rows["facts"].append({"call_id": cid, **{k: f[k] for k in ("id", "type", "label", "value", "product_id",
                                                                    "number", "line", "speaker", "quote")}})
    rows["calls"].append({
        "call_id": cid, "file": call["source"]["file"], "processed_at": pr["processed_at"],
        "call_type": cp["call_type"], "primary_intent": cp["primary_intent"]["value"],
        "resolution_status": cp["resolution_status"]["value"], "sentiment_start": cp["sentiment"]["start"],
        "sentiment_end": cp["sentiment"]["end"], "sentiment_trajectory": cp["sentiment"]["trajectory"],
        "sales_pitch_made": call["sales"]["pitch_made"], "sales_outcome": call["sales"]["sales_outcome"]["value"],
        "churn_risk": call["customer"]["churn_risk"]["value"],
        "identity_verification": call["compliance"]["identity_verification"]["value"],
        "n_products": len(call["products"]), "n_objections": len(call["sales"]["objections"]),
        "n_risk_flags": len(call["compliance"]["risk_flags"]),
        "agent_empathy": call["agent_performance"]["empathy"], "agent_clarity": call["agent_performance"]["clarity"],
        "agent_product_knowledge": call["agent_performance"]["product_knowledge"],
        "agent_compliance_adherence": call["agent_performance"]["compliance_adherence"],
        "quality_status": q["status"], "overall_confidence": q["overall_confidence"],
        "raw_lines": call["source"]["raw_lines"], "duration_ms": pr["duration_ms"], "llm_calls": pr["llm_calls"],
        "tokens_in": pr["tokens_in"], "tokens_out": pr["tokens_out"], "summary": cp["summary"]})
    for it in [cp["primary_intent"]] + cp["secondary_intents"]:
        rows["intents"].append({"call_id": cid, "intent": it["value"], "primary": it is cp["primary_intent"],
                                "confidence": it["confidence"]})
    for p in call["products"]:
        rows["products"].append({"call_id": cid, "product_id": p["product_id"], "product_name": p["product_name"],
                                 "category": p["category"], "role": p["role"], "interest": p["customer_interest"],
                                 "outcome": p["outcome"], "confidence": p["confidence"],
                                 "normalization_tier": p["normalization"]["tier"],
                                 "mentions": "; ".join(p["mentions"])})
    for o in call["sales"]["objections"]:
        rows["objections"].append({"call_id": cid, "category": o["category"], "detail": o["detail"],
                                   "handled": o["handled"], "confidence": o["confidence"],
                                   "lines": _ev_lines(o["evidence"])})
    for r in call["compliance"]["rules"]:
        rows["compliance_rules"].append({"call_id": cid, "rule_id": r["rule_id"], "status": r["status"],
                                         "severity": r["severity"], "confidence": r["confidence"],
                                         "lines": _ev_lines(r["evidence"])})
    for r in call["compliance"]["risk_flags"]:
        rows["risk_flags"].append({"call_id": cid, "flag": r["flag"], "severity": r["severity"],
                                   "detail": r["detail"], "confidence": r["confidence"]})
    for t in call["quoted_terms"]:
        rows["quoted_terms"].append({"call_id": cid, "product_id": t["product_id"], "description": t["description"],
                                     "number": t["number"], "numeric_verified": t["numeric_verified"]})
    return rows


def export_bi(settings: Settings, day: str | None = None) -> dict[str, str]:
    """Flatten per-call JSON into fact tables: <output>/bi/<day|all>/<table>.parquet (or .csv)."""
    import pandas as pd

    root = settings.path(settings.paths.output_dir)
    pattern = f"calls/{day}/*.json" if day else "calls/*/*.json"
    tables: dict[str, list[dict[str, Any]]] = {}
    for f in sorted(root.glob(pattern)):
        for name, rows in flatten(loads(Path(f).read_bytes())).items():
            tables.setdefault(name, []).extend(rows)
    out_dir = root / "bi" / (day or "all")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for name, rows in tables.items():
        df = pd.DataFrame(rows)
        try:
            path = out_dir / f"{name}.parquet"
            df.to_parquet(path, index=False)
        except Exception:
            path = out_dir / f"{name}.csv"
            df.to_csv(path, index=False)
        written[name] = str(path)
    return written
