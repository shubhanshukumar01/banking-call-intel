
# CallIntel v3: banking call transcript analytics (agentic RAG)

CallIntel turns large banking sales and service call transcripts (1,000+ files a day, 6,000+ lines each) into consistent, evidence-grounded, PII-safe JSON insights. It masks PII locally, removes noise, extracts facts in parallel, runs specialist agents with RAG, normalises product names, verifies claims to prevent hallucinations, uses Redis for its backbone, and lets you switch between local and OpenAI models in config. A separate evaluation harness gates quality.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the review of the v2 design, the 21 changes made in v3 (5 of them found by testing on real local models), and diagrams.

## Quick start (Windows, PowerShell)

```powershell
cd banking-call-intel
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
```

Pull the recommended local map model (about 2.5 GB). Skip this and use `--profile local_installed` to run on the `qwen3:8b` you already have.

```powershell
ollama pull qwen3:4b-instruct-2507-q4_K_M
```

Check Redis, models, PII detectors and the knowledge base:

```powershell
.\.venv\Scripts\python -m callintel doctor
```

Generate sample transcripts (or drop your own `.txt` files into `data/incoming`):

```powershell
.\.venv\Scripts\python -m tools.synth --n 3 --lines 6000 --out data/incoming
```

Process everything in `data/incoming` (ingest, then workers):

```powershell
.\.venv\Scripts\python -m callintel --profile local_installed run
```

Outputs:
- `data/output/calls/<date>/<call_id>.json` (one file per call)
- `data/output/jsonl/` (one record per line)
- `export-bi` writes Parquet fact tables for Power BI

### Redis

CallIntel needs Redis 6.2 or newer. On this laptop it is **Memurai Developer 4.1.2** (Redis API 7.2.5), an auto-start Windows service on `127.0.0.1:6379` with persistence (AOF), a 2 GB memory cap and `volatile-lru` eviction. Memurai Developer is for development and testing only.

Step-by-step Windows instructions (Memurai, WSL2 or Docker), the fix for the Memurai installer's error 1603, and the recommended settings are in **[docs/REDIS_SETUP.md](docs/REDIS_SETUP.md)**. For a production-like setup, use WSL2 + Ubuntu 24.04 or `docker compose up -d redis`.

Use `127.0.0.1`, not `localhost`, in URLs on Windows (`localhost` adds about 2 s per connection). `redis.allow_fakeredis_fallback` is `false`, so a missing server fails loudly. Set it to `true` only to experiment without Redis (one process, nothing persists).

## Commands

| Command | What it does |
|---|---|
| `python -m callintel doctor` | Health check: Redis, every LLM role, PII detectors, catalogue, knowledge base, prompt versions |
| `python -m callintel run [--input DIR] [--processes N] [--concurrency M]` | Ingest a folder and process it to completion |
| `python -m callintel process FILE [--force]` | Process one file without the queue (debugging) |
| `python -m callintel ingest [--watch 10]` | Enqueue new files (SHA-256 dedupe); `--watch` keeps polling |
| `python -m callintel worker --processes 4 --concurrency 4` | Long-running workers (production) |
| `python -m callintel status` | Queue depth, pending, DLQ, review queue, manifest counts, today's metrics |
| `python -m callintel export-bi [--day YYYY-MM-DD]` | Parquet tables: calls, products, intents, objections, compliance_rules, risk_flags, quoted_terms, facts |
| `python -m callintel aliases` | Product aliases learned by tiers 2/3 (promote good ones to `products.yaml`) |
| `python -m callintel schema --out schemas/call_analysis.schema.json` | Export the output JSON Schema |
| `python -m callintel vault-key` | Generate a key for the optional encrypted PII vault |

Global options: `--profile local|local_installed|openai|vllm|mock`, `--config PATH`, `--log-level DEBUG`.

Production topology: run one `ingest --watch` process, plus `worker` processes on one or more machines, all pointed at the same Redis.

## Choosing the LLM

Everything is in `config/config.yaml` under `llm.profiles`. Each role (`map`, `reduce`, `critic`, `normalizer`, `embedding`) can use a different provider and model.

| Profile | map (high volume) | reduce / critic (few calls) | Use for |
|---|---|---|---|
| `local` (recommended for this laptop) | `qwen3:4b-instruct-2507-q4_K_M` (about 2.5 GB, fits fully in 6 GB VRAM with an 8k context, non-thinking, strong at JSON) | the same model (one model avoids swaps; see the note below) | Development and testing |
| `local_installed` | `qwen3:8b` | `qwen3:8b` | Runs with what is already pulled (slower here: about 37% of the model spills to CPU) |
| `vllm` | `Qwen/Qwen3-30B-A3B-Instruct-2507` (MoE, 3B active: fast and strong) | same | On-prem production at 1,000+ files/day |
| `openai` | `gpt-6-luna`, `reasoning_effort: none` | `gpt-6.1-terra`, `reasoning_effort: low` | Cloud production (masked text only) |
| `mock` | deterministic stub | stub | CI and plumbing tests (not real analytics) |

### Measured on this laptop (RTX 3060 6 GB, 16 GB RAM)

All numbers below are from synthetic test calls (`tools/synth.py`). Real calls will differ.

| Stage | Result |
|---|---|
| Preprocessing (parse, denoise, repetition compression, regex+context PII masking, chunking) | About 0.5 s per 6,000-line file. Tokens fell **82%** (65.8k to 12.1k); 6,001 lines became 1,882. |
| Triage | 3 of 6 chunks sent to the LLM on a 6,000-line call |
| **Full 6,000-line call, `local` profile** (`qwen3:4b-instruct-2507`, 100% GPU) | **133 s**, 17 LLM calls (22k tokens in / 7.8k out): map 36 s, specialists 25 s, verification 67 s. Correct call type, intent and products. |
| 500-line calls, 2 at a time | p50 94 s, p95 137 s each (the two calls share the GPU) |
| Same 500-line call on `qwen3:8b` (37% on CPU, about 9 tokens/s) | 5-6 min, with weak fact labelling. The 4B model fully on the GPU is about 6x faster *and* better here. |

**Golden-set evaluation, `local` profile, 8 synthetic calls: 11 of 14 gates pass.**

| Metric | Result | Gate |
|---|---|---|
| PII leaks (LLM prompts and outputs) / masking recall | 0 / 100% | pass |
| Schema validity / enum validity | 100% / 100% | pass |
| Product normalisation accuracy / product F1 / invented products | 100% / 1.0 / 0% | pass |
| Evidence grounding | 100% (163 evidence items) | pass |
| Sales outcome / objection F1 / compliance status | 75% / 0.67 / 76% | pass |
| Call type / primary intent | 62.5% / 50% | **fail** (gates 70% / 60%) |

Conclusion: the laptop works for development, testing and small batches. At about 133 s per long call it handles roughly 27 calls an hour, not 1,000 a day. The judgement fields (intent, call type) need a stronger model. For production, use the `vllm` profile (a 30B-class MoE on a 24-48 GB GPU) or `openai`. Re-run `evaluation.run_eval` on **hand-labelled real calls** before trusting any accuracy number. To make verification faster on the laptop, set `agents.max_retries: 1`, which roughly halves it.

Two Ollama rules learned on this laptop:

- **One context size per model.** Roles that share a model must use the same `num_ctx`; otherwise Ollama reloads the model on every map/reduce switch. The gateway enforces this automatically.
- **One model per GPU when VRAM is tight.** Two different models cannot both stay loaded in 6 GB, so mixing them makes Ollama swap them constantly. Keep `providers.ollama.max_concurrency` equal to `OLLAMA_NUM_PARALLEL` (default 1).

Other local options, if you have more VRAM: `gemma4:e4b-it-qat` (6.1 GB), `gemma4:12b-it-qat` (7.2 GB), `gemma4:26b-a4b` MoE (16 GB+), `gpt-oss:20b`. Your v2 diagram listed `gpt-4o-mini` / `gpt-4o`; OpenAI marks these as deprecated, so the `openai` profile uses current models instead. Verify model ids and prices on OpenAI's model and pricing pages before production use.

To switch to OpenAI, put `OPENAI_API_KEY=...` in `.env` (or set `llm.providers.openai.api_key` in config) and run with `--profile openai`, or set `CALLINTEL_PROFILE=openai`. The egress guard re-scans every outbound prompt for unmasked PII and blocks it. Set `privacy.allow_cloud_llm: false` to forbid cloud use entirely.

## Optional stronger PII detection

```powershell
.\.venv\Scripts\python -m pip install -r requirements-pii.txt
```

```powershell
.\.venv\Scripts\python -m spacy download en_core_web_lg
```

Presidio and GLiNER then activate automatically (they are already listed in `privacy.detectors`). They run only on "gated" lines (name or address cues, capitalised tokens) to keep masking fast.

## Customising for the client

- `config/products.yaml`: the real product catalogue and aliases. Attributes (fees, rates) let the compliance agent catch misquoted terms.
- `config/taxonomy.yaml`: intents, objections, complaints, risk flags and outcomes. These become JSON-schema enums, so every call uses the same values.
- `knowledge_base/compliance_rules.yaml`: the bank's rulebook (which calls and products each rule applies to, and phrases used to retrieve candidate lines).
- `knowledge_base/*.md`: playbooks and policies (split on `## ` headings).
- `config/prompts.yaml`: versioned prompts. Editing one invalidates its cache entries automatically.
- `preprocess.speakers`: speaker labels used in your transcripts. Unknown labels that look like names are assigned roles automatically.

## Output JSON (abridged)

```json
{
  "schema_version": "3.0", "call_id": "9f2c...",
  "source": {"file": "call_00001.txt", "raw_lines": 6000, "kept_lines": 3510, "chunks_total": 17, "chunks_analyzed": 7},
  "processing": {"duration_ms": 84000, "llm_calls": 12, "llm_cache_hits": 0, "models": {"map": "ollama:qwen3:4b-..."}, "prompt_versions": {}},
  "privacy": {"pii_masked": {"PERSON": 6, "PHONE": 1, "CARD": 1}, "output_pii_findings": 0},
  "call_profile": {"call_type": "sales", "primary_intent": {"value": "loan_enquiry", "evidence": [{"line": 212, "speaker": "CUSTOMER", "quote": "I got an SMS about a pre approved loan"}], "confidence": 0.93},
                   "resolution_status": {}, "sentiment": {"start": "neutral", "end": "positive", "trajectory": "improving"}, "summary": "..."},
  "products": [{"product_id": "PL_PERSONAL_LOAN", "mentions": ["pre approved loan", "personal lone"], "role": "pitched", "customer_interest": "high", "outcome": "accepted", "normalization": {"tier": "exact", "confidence": 1.0}}],
  "sales": {"pitch_made": true, "sales_outcome": {"value": "converted"}, "objections": [{"category": "fees_too_high", "handled": "yes"}]},
  "customer": {"needs": [], "pain_points": [], "churn_risk": {"value": "low"}},
  "compliance": {"rules": [{"rule_id": "APR_FEE_DISCLOSURE", "status": "met", "evidence": []}], "risk_flags": [], "identity_verification": {"value": "performed"}},
  "quoted_terms": [{"number": "10.99%", "numeric_verified": true}],
  "agent_performance": {"empathy": 4, "clarity": 4, "product_knowledge": 4, "compliance_adherence": 5},
  "domain_insights": {"fact_type_counts": {}, "taxonomy_other_values": []},
  "quality": {"status": "validated", "overall_confidence": 0.91, "critic_checked": 6, "critic_rejected": 0, "retries": 0, "review_items": []}
}
```

## Evaluation (separate harness)

Generate the golden set (or replace it with hand-labelled real calls):

```powershell
.\.venv\Scripts\python -m tools.synth --n 8 --lines 500 --seed 1000 --out evaluation/golden/transcripts --truth evaluation/golden/truth
```

Run the pipeline on the golden set and score it; the run fails (exit 1) if a gate fails:

```powershell
.\.venv\Scripts\python -m evaluation.run_eval --profile local_installed
```

Score outputs that already exist, with no LLM calls:

```powershell
.\.venv\Scripts\python -m evaluation.run_eval --predictions evaluation/reports/<run>/outputs
```

Extras: `--judge` adds an LLM-as-judge faithfulness check, `--export-ragas` writes a RAGAS/DeepEval dataset, and `--save-baseline` sets the regression baseline. Gates live in `evaluation/thresholds.yaml`. Reports are written to `evaluation/reports/<timestamp>_<profile>/report.md` and `report.json`.

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q
```

The 27 tests cover PII (checksums, spoken digits, two-pass names, dialogue answers, product protection), noise handling (including repetition compression), chunking, Redis (streams, retry/DLQ, reclaim, lock, manifest, rate limiter), normalisation tiers, grounding, the egress guard, strict and grammar schemas, truncated-JSON salvage, and an end-to-end run on the mock provider.

## Project layout

```
callintel/
  config.py                 YAML + env loading, profiles/roles
  schemas.py                taxonomy -> enum-constrained LLM schemas; final CallAnalysis model
  preprocess/               cleaning.py (noise, spoken digits), pii.py (layered masking), transcript.py (parse, turns, chunks)
  redis_layer/              client (keyspace), streams (queue/DLQ), ratelimit (Lua), store (cache/lock/manifest/metrics), vault
  llm/                      gateway (ollama/openai/vllm/mock), egress guard, prompt registry, strict schema
  rag/                      BM25 + dense hybrid, knowledge base, transcript index
  normalize/                catalogue + 3-tier self-learning normaliser
  agents/                   triage, mapper, specialists, critic, supervisor, assemble, base (tool loop)
  verify/                   grounding (deterministic checks), gate (claim verification)
  pipeline/                 engine (process_file), worker (ingest, worker pool, run_local)
  output/                   sinks (JSON/JSONL/Parquet BI export)
  cli.py
config/                     config.yaml, taxonomy.yaml, products.yaml, prompts.yaml
knowledge_base/             compliance_rules.yaml, sales_playbook.md, service_policies.md
evaluation/                 run_eval.py, metrics.py, thresholds.yaml, golden/, reports/
tools/synth.py              synthetic transcript + ground-truth generator
tests/                      pytest suite
```
