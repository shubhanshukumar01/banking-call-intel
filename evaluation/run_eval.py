"""Offline evaluation harness - run on a golden set, gate releases in CI.

    # run the pipeline on the golden set with a given model profile, then score it
    python -m evaluation.run_eval --golden evaluation/golden --profile local

    # score outputs that already exist (no LLM calls)
    python -m evaluation.run_eval --golden evaluation/golden --predictions evaluation/reports/<run>/outputs

    # extras: --judge (LLM-as-judge summary faithfulness), --export-ragas, --save-baseline

Golden set layout:  <golden>/transcripts/*.txt  +  <golden>/truth/*.json  (same stem)
Truth fields used:  call_type, primary_intent, sales_outcome, identity_verification, products[],
                    mentions[{surface, product_id}], objections[], compliance{rule_id: status}, pii[{type, value}]

Metric families: privacy (leakage hard gate, masking recall, product-name preservation),
schema/enum validity, product normalisation (mention accuracy, call-level F1), grounding &
hallucination, extraction accuracy/F1, optional LLM-judge faithfulness, latency/cost/cache.
Outputs: report.json + report.md; exit code 1 if any gate fails or a metric regresses.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from callintel.config import Settings, load_settings
from callintel.normalize.catalog import ProductCatalog
from callintel.normalize.normalizer import ProductNormalizer
from callintel.pipeline.engine import Engine
from callintel.preprocess.pii import build_recognizers
from callintel.preprocess.transcript import prepare_transcript
from callintel.redis_layer.client import connect_redis
from callintel.schemas import CallAnalysis, Taxonomy
from callintel.utils import setup_logging, sha256_file

from .metrics import (accuracy, contains_pii, mean_or_zero, micro_prf, percentile, quote_grounded)

ROOT = Path(__file__).resolve().parent


# ----------------------------------------------------------------------------- pipeline run
async def run_pipeline(s: Settings, files: list[Path], out_dir: Path, concurrency: int) -> dict[str, dict]:
    s.paths.output_dir = str(out_dir / "outputs")
    s.paths.audit_dir = str(out_dir / "audit")
    s.audit.capture_llm_payloads = True          # needed for the leakage test (masked prompts only)
    eng = await Engine.create(s)
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}

    async def one(f: Path) -> None:
        async with sem:
            t0 = time.perf_counter()
            try:
                res = await eng.process_file(f, force=True)
            except Exception as exc:
                res = {"status": "error", "error": repr(exc)[:300]}
            res["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            results[f.name] = res
            print(f"  {f.name}: {res.get('status')} {res.get('duration_ms', '')}ms", flush=True)
    try:
        await asyncio.gather(*(one(f) for f in files))
    finally:
        await eng.close()
    return results


def load_predictions(pred_dir: Path, files: list[Path]) -> dict[str, dict]:
    by_sha = {}
    for p in pred_dir.rglob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            by_sha[d["source"]["sha256"]] = d
        except Exception:
            continue
    return {f.name: by_sha.get(sha256_file(f)) for f in files}


# ----------------------------------------------------------------------------- LLM judge
class _Judgement(BaseModel):
    faithfulness: int           # 1..5
    unsupported_statements: list[str]


async def judge_summaries(s: Settings, preds: dict[str, dict]) -> dict[str, Any]:
    from callintel.llm.gateway import LLMGateway

    r = await connect_redis(s)
    gw = LLMGateway(s, r)
    sys_p = ("You grade call summaries. Score faithfulness 1-5 (5 = every statement is supported by the EVIDENCE "
             "lines, 1 = mostly unsupported). List unsupported statements verbatim. Judge only from EVIDENCE.")
    scores = []
    for name, d in preds.items():
        if not d or not d["call_profile"]["summary"]:
            continue
        ev = [e["quote"] for sec in _walk_evidence(d) for e in sec][:40]
        msg = [{"role": "system", "content": sys_p},
               {"role": "user", "content": f"SUMMARY: {d['call_profile']['summary']}\nEVIDENCE:\n" + "\n".join(ev)}]
        try:
            res = await gw.structured("critic", msg, _Judgement, use_cache=True)
            scores.append(max(1, min(5, res.data.faithfulness)))
        except Exception as exc:
            print(f"  judge failed for {name}: {exc}")
    await gw.close()
    await r.close()
    return {"summary_faithfulness_mean_1to5": round(sum(scores) / len(scores), 3) if scores else None,
            "judged": len(scores)}


def _walk_evidence(node: Any) -> list[list[dict]]:
    out: list[list[dict]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("evidence", "summary_evidence") and isinstance(v, list):
                out.append(v)
            else:
                out.extend(_walk_evidence(v))
    elif isinstance(node, list):
        for x in node:
            out.extend(_walk_evidence(x))
    return out


# ----------------------------------------------------------------------------- scoring
def _enum_ok(d: dict, tax: Taxonomy, product_ids: set[str], rule_ids: set[str]) -> bool:
    cp = d["call_profile"]
    checks = [cp["call_type"] in tax.call_types, cp["primary_intent"]["value"] in tax.intents,
              cp["resolution_status"]["value"] in tax.resolution_statuses,
              cp["sentiment"]["start"] in tax.sentiments, cp["sentiment"]["end"] in tax.sentiments,
              d["sales"]["sales_outcome"]["value"] in tax.sales_outcomes,
              d["customer"]["churn_risk"]["value"] in tax.churn_risk,
              d["compliance"]["identity_verification"]["value"] in tax.identity_verification]
    checks += [p["product_id"] in product_ids and p["role"] in tax.product_roles for p in d["products"]]
    checks += [o["category"] in tax.objections for o in d["sales"]["objections"]]
    checks += [r["rule_id"] in rule_ids and r["status"] in tax.rule_statuses for r in d["compliance"]["rules"]]
    checks += [f["flag"] in tax.risk_flags for f in d["compliance"]["risk_flags"]]
    return all(checks)


async def score(s: Settings, golden: Path, files: list[Path], preds: dict[str, dict], audit_dir: Path | None,
                runs: dict[str, dict] | None) -> dict[str, Any]:
    tax = Taxonomy.load(s.path(s.paths.taxonomy_file))
    cat = ProductCatalog(s.path(s.paths.products_file))
    from callintel.rag.knowledge_base import KnowledgeBase
    rule_ids = set(KnowledgeBase(s, cat).rule_ids)
    cheap, ner = build_recognizers(s.privacy.detectors, s.privacy.presidio_model, s.privacy.gliner_model,
                                   s.privacy.gliner_threshold)
    truths = {f.name: json.loads((golden / "truth" / f"{f.stem}.json").read_text(encoding="utf-8")) for f in files}
    m: dict[str, Any] = {"n_calls": len(files), "n_outputs": sum(1 for v in preds.values() if v)}

    # --- privacy ------------------------------------------------------------------
    pii_total = pii_masked = prod_total = prod_kept = 0
    masked_lines: dict[str, dict[int, str]] = {}
    leaks: list[dict] = []
    payload_text: dict[str, str] = {}
    if audit_dir and audit_dir.exists():
        for pf in audit_dir.glob("llm_payloads_*.jsonl"):
            for line in pf.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                payload_text[rec.get("call_id") or ""] = payload_text.get(rec.get("call_id") or "", "") + "\n" + \
                    "\n".join(m_["content"] for m_ in rec["messages"])
    for f in files:
        t = truths[f.name]
        raw = f.read_text(encoding="utf-8")
        sha = sha256_file(f)
        p = prepare_transcript(raw, call_id=sha[:16], file_name=f.name, sha256=sha, settings=s, cheap=cheap, ner=ner,
                               protected_spans=cat.protected_spans)
        masked_lines[f.name] = {no: ln.text for no, ln in p.lines.items()}
        masked = "\n".join(masked_lines[f.name].values())
        for item in t.get("pii", []):
            pii_total += 1
            if not contains_pii(masked, item["value"], item["type"]):
                pii_masked += 1
            for where, text in (("llm_payload", payload_text.get(sha[:16], "")),
                                ("output", json.dumps({k: v for k, v in (preds.get(f.name) or {}).items()
                                                       if k not in ("source", "processing")}))):
                if text and contains_pii(text, item["value"], item["type"]):
                    leaks.append({"file": f.name, "type": item["type"], "where": where})
        for mt in t.get("mentions", []):
            prod_total += 1
            prod_kept += int(mt["surface"].lower() in masked.lower())
    m["pii_leakage_count"] = len(leaks)
    m["pii_leaks"] = leaks[:20]
    m["pii_masking_recall"] = round(pii_masked / pii_total, 4) if pii_total else 1.0
    m["product_names_preserved"] = round(prod_kept / prod_total, 4) if prod_total else 1.0
    m["llm_payloads_scanned"] = bool(payload_text)

    # --- schema ---------------------------------------------------------------------
    valid = enum_valid = 0
    for d in preds.values():
        if not d:
            continue
        try:
            CallAnalysis.model_validate(d)
            valid += 1
            enum_valid += int(_enum_ok(d, tax, set(cat.ids), rule_ids))
        except ValidationError:
            pass
    m["schema_validity_rate"] = round(valid / len(files), 4) if files else 1.0
    m["enum_validity_rate"] = round(enum_valid / len(files), 4) if files else 1.0

    # --- normalisation ----------------------------------------------------------------
    r = await connect_redis(s)
    s.rag.normalizer.t3_llm_enabled = False  # deterministic tiers only for a stable metric
    norm = ProductNormalizer(s, cat, r, None, None, None)
    gold_mentions = [mt for t in truths.values() for mt in t.get("mentions", [])]
    norm_ok = 0
    for mt in gold_mentions:
        res = await norm.normalize(mt["surface"])
        norm_ok += int(res.product_id == mt["product_id"])
    await r.close()
    m["normalization_mention_accuracy"] = round(norm_ok / len(gold_mentions), 4) if gold_mentions else 1.0
    pairs, halluc, n_pred_products = [], 0, 0
    for name, d in preds.items():
        gold = set(truths[name].get("products", []))
        pred = {p["product_id"] for p in (d or {}).get("products", []) if not p["product_id"].startswith("GEN_")}
        pairs.append((pred, gold))
        n_pred_products += len(pred)
        halluc += len(pred - gold)  # products reported that the call never discussed
    pf = micro_prf(pairs)
    m["product_set_f1"], m["product_set_precision"], m["product_set_recall"] = pf["f1"], pf["precision"], pf["recall"]
    m["hallucinated_product_rate"] = round(halluc / n_pred_products, 4) if n_pred_products else 0.0

    # --- grounding --------------------------------------------------------------------
    ev_total = ev_ok = 0
    for name, d in preds.items():
        if not d:
            continue
        lines = masked_lines[name]
        for sec in _walk_evidence(d):
            for e in sec:
                ev_total += 1
                ev_ok += int(quote_grounded(e["quote"], lines.get(e["line"], "")))
    m["evidence_items"] = ev_total
    m["evidence_grounding_rate"] = round(ev_ok / ev_total, 4) if ev_total else 1.0
    m["hallucination_rate"] = round(1 - m["evidence_grounding_rate"], 4)
    q = [d["quality"] for d in preds.values() if d]
    m["claims_dropped_ungrounded_mean"] = mean_or_zero([x["claims_dropped_ungrounded"] for x in q])
    m["critic_rejected_mean"] = mean_or_zero([x["critic_rejected"] for x in q])
    m["needs_review_rate"] = round(sum(1 for x in q if x["status"] == "needs_review") / len(q), 4) if q else 0.0

    # --- extraction ---------------------------------------------------------------------
    def pv(d: dict | None, path: list[str]) -> Any:
        cur: Any = d
        for k in path:
            if cur is None:
                return None
            cur = cur.get(k)
        return cur
    m["call_type_accuracy"] = accuracy([(pv(preds[n], ["call_profile", "call_type"]), t.get("call_type"))
                                        for n, t in truths.items()])
    m["primary_intent_accuracy"] = accuracy([(pv(preds[n], ["call_profile", "primary_intent", "value"]),
                                              t.get("primary_intent")) for n, t in truths.items()])
    m["sales_outcome_accuracy"] = accuracy([(pv(preds[n], ["sales", "sales_outcome", "value"]),
                                             t.get("sales_outcome")) for n, t in truths.items()])
    m["identity_verification_accuracy"] = accuracy([(pv(preds[n], ["compliance", "identity_verification", "value"]),
                                                     t.get("identity_verification")) for n, t in truths.items()])
    comp = []
    for n, t in truths.items():
        rules = {x["rule_id"]: x["status"] for x in (pv(preds[n], ["compliance", "rules"]) or [])}
        comp += [(rules.get(rid, "unclear"), st) for rid, st in t.get("compliance", {}).items()]
    m["compliance_status_accuracy"] = accuracy(comp)
    obj = micro_prf([({o["category"] for o in (pv(preds[n], ["sales", "objections"]) or [])},
                      set(t.get("objections", []))) for n, t in truths.items()])
    m["objection_f1"] = obj["f1"]

    # --- ops ------------------------------------------------------------------------------
    procs = [d["processing"] for d in preds.values() if d]
    durations = [p["duration_ms"] for p in procs]
    m["latency_ms_p50"], m["latency_ms_p95"] = percentile(durations, 0.5), percentile(durations, 0.95)
    m["llm_calls_mean"] = mean_or_zero([p["llm_calls"] for p in procs])
    m["tokens_in_mean"] = mean_or_zero([p["tokens_in"] for p in procs])
    m["tokens_out_mean"] = mean_or_zero([p["tokens_out"] for p in procs])
    calls = sum(p["llm_calls"] for p in procs)
    hits = sum(p["llm_cache_hits"] for p in procs)
    m["cache_hit_rate"] = round(hits / (calls + hits), 4) if (calls + hits) else 0.0
    cost = 0.0
    for p in procs:
        for role, u in p.get("llm_by_role", {}).items():
            model = p["models"].get(role if role in p["models"] else "reduce", "").split(":", 1)[-1]
            price = s.llm.pricing.get(model)
            if price:
                cost += u.get("tokens_in", 0) / 1e6 * price["input"] + u.get("tokens_out", 0) / 1e6 * price["output"]
    m["est_cost_usd_per_call"] = round(cost / len(procs), 5) if procs else 0.0
    m["chunks_skipped_by_triage_mean"] = mean_or_zero([d["domain_insights"]["triage"]["chunks_skipped"]
                                                       for d in preds.values() if d])
    if runs:
        m["pipeline_errors"] = [{"file": k, "error": v.get("error")} for k, v in runs.items() if v.get("status") == "error"]
    return m


# ----------------------------------------------------------------------------- gates & report
def apply_gates(m: dict[str, Any], baseline: dict[str, Any] | None) -> tuple[list[dict], bool]:
    cfg = yaml.safe_load((ROOT / "thresholds.yaml").read_text(encoding="utf-8"))
    rows, ok = [], True
    for name, rule in cfg["gates"].items():
        v = m.get(name)
        if v is None:
            rows.append({"metric": name, "value": None, "gate": rule, "pass": None})
            continue
        passed = v >= rule["min"] if "min" in rule else v <= rule["max"]
        ok &= passed
        rows.append({"metric": name, "value": v, "gate": rule, "pass": passed})
    if baseline:
        tol = cfg.get("regression_tolerance", 0.03)
        for name in cfg.get("regression_metrics", []):
            b, v = baseline.get(name), m.get(name)
            if isinstance(b, (int, float)) and isinstance(v, (int, float)) and v < b - tol:
                ok = False
                rows.append({"metric": f"regression:{name}", "value": v, "gate": {"baseline": b, "tol": tol},
                             "pass": False})
    return rows, ok


def write_report(out_dir: Path, m: dict[str, Any], gates: list[dict], ok: bool, meta: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps({"meta": meta, "passed": ok, "gates": gates, "metrics": m},
                                                    indent=2), encoding="utf-8")
    lines = [f"# Evaluation report - {meta['timestamp']}", "",
             f"Profile: `{meta['profile']}` | models: {meta.get('models', {})} | calls: {m['n_calls']} | "
             f"**{'PASSED' if ok else 'FAILED'}**", "", "| Metric | Value | Gate | Pass |", "|---|---|---|---|"]
    for g in gates:
        mark = "-" if g["pass"] is None else ("yes" if g["pass"] else "**NO**")
        lines.append(f"| {g['metric']} | {g['value']} | {g['gate']} | {mark} |")
    lines += ["", "## All metrics", "", "```json", json.dumps(m, indent=2), "```"]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def export_ragas(out_dir: Path, preds: dict[str, dict]) -> Path:
    """RAGAS/DeepEval-ready dataset: question / answer / contexts per call summary."""
    path = out_dir / "ragas_dataset.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for d in preds.values():
            if d and d["call_profile"]["summary"]:
                ctx = [e["quote"] for sec in _walk_evidence(d) for e in sec][:30]
                fh.write(json.dumps({"question": "Summarise this banking call.", "answer": d["call_profile"]["summary"],
                                     "contexts": ctx}) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(ROOT / "golden"))
    ap.add_argument("--profile", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--predictions", default=None, help="score existing outputs instead of running the pipeline")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--judge", action="store_true", help="LLM-as-judge summary faithfulness (uses the critic role)")
    ap.add_argument("--export-ragas", action="store_true")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    s = load_settings(args.config, args.profile)
    setup_logging("WARNING")
    golden = Path(args.golden)
    files = sorted((golden / "transcripts").glob("*.txt"))
    if not files:
        print(f"no transcripts in {golden / 'transcripts'} (generate: python -m tools.synth --n 8 --lines 500 "
              f"--out {golden / 'transcripts'} --truth {golden / 'truth'})")
        return 2
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else ROOT / "reports" / f"{ts}_{s.llm.active_profile}"
    s.redis.key_prefix = f"{s.redis.key_prefix}:eval"   # keep eval state apart from production keys
    runs = None
    if args.predictions:
        preds = load_predictions(Path(args.predictions), files)
        audit = Path(args.predictions).parent / "audit"
    else:
        print(f"running pipeline on {len(files)} golden calls with profile '{s.llm.active_profile}' ...")
        runs = asyncio.run(run_pipeline(s, files, out_dir, args.concurrency))
        preds = load_predictions(out_dir / "outputs", files)
        audit = out_dir / "audit"
    m = asyncio.run(score(s, golden, files, preds, audit, runs))
    if args.judge:
        m.update(asyncio.run(judge_summaries(s, preds)))
    if args.export_ragas:
        m["ragas_dataset"] = str(export_ragas(out_dir, preds))
    base_path = ROOT / "reports" / "baseline.json"
    baseline = json.loads(base_path.read_text(encoding="utf-8"))["metrics"] if base_path.exists() else None
    gates, ok = apply_gates(m, baseline)
    meta = {"timestamp": ts, "profile": s.llm.active_profile,
            "models": {r: f"{s.role(r).provider}:{s.role(r).model}" for r in ("map", "reduce", "critic")}}
    write_report(out_dir, m, gates, ok, meta)
    if args.save_baseline:
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_text(json.dumps({"meta": meta, "metrics": m}, indent=2), encoding="utf-8")
    print(f"\n{'PASSED' if ok else 'FAILED'} - report: {out_dir / 'report.md'}")
    for g in gates:
        flag = "  " if g["pass"] in (True, None) else "XX"
        print(f" {flag} {g['metric']:<34} {g['value']}   gate={g['gate']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
