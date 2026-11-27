# CallIntel code walkthrough: every function, in the order it runs

This guide explains the Python code of CallIntel step by step, **in the same order the program executes**.
For each step you get:

- **What happens**: the functions that run, what goes in and what comes out.
- **Function by function**: what each function does, with its file and line.
- **Why this way**: the reason for the design and the alternatives that were rejected.

Read it top to bottom once, with the code open beside it. Every function in `callintel/`, `evaluation/`,
`tools/` and `tests/` is covered; the [function index](#appendix-a-function-index) at the end tells you
which section explains each one.

---

## Contents

0. [The big picture in one page](#0-the-big-picture-in-one-page)
1. [The running example](#1-the-running-example)
2. [Step 1: the command starts (CLI)](#step-1-the-command-starts-cli)
3. [Step 2: configuration is loaded](#step-2-configuration-is-loaded)
4. [Step 3: connect to Redis](#step-3-connect-to-redis)
5. [Step 4: the Engine is built once per process](#step-4-the-engine-is-built-once-per-process)
6. [Step 5: files are found and queued (ingest)](#step-5-files-are-found-and-queued-ingest)
7. [Step 6: workers take jobs from the queue](#step-6-workers-take-jobs-from-the-queue)
8. [Step 7: process_file opens a call (lock, manifest, timers)](#step-7-process_file-opens-a-call)
9. [Step 8: parse lines and work out who is speaking](#step-8-parse-lines-and-work-out-who-is-speaking)
10. [Step 9: clean the noise](#step-9-clean-the-noise)
11. [Step 10: mask PII](#step-10-mask-pii)
12. [Step 11: build turns and chunks](#step-11-build-turns-and-chunks)
13. [Step 12: the Supervisor starts (call context, transcript index)](#step-12-the-supervisor-starts)
14. [Step 13: triage, which chunks need the LLM](#step-13-triage)
15. [Step 14: the map agent extracts facts](#step-14-the-map-agent-extracts-facts)
16. [Step 15: inside the LLM gateway (every LLM call goes through here)](#step-15-inside-the-llm-gateway)
17. [Step 16: ground, check and de-duplicate the facts](#step-16-ground-check-and-de-duplicate-the-facts)
18. [Step 17: normalise product names](#step-17-normalise-product-names)
19. [Step 18: the call-type hint and the rule list](#step-18-the-call-type-hint-and-the-rule-list)
20. [Step 19: the specialist agents (with RAG and tools)](#step-19-the-specialist-agents)
21. [Step 20: the synthesis agent](#step-20-the-synthesis-agent)
22. [Step 21: the verification loop (hallucination gate)](#step-21-the-verification-loop)
23. [Step 22: assemble the final JSON](#step-22-assemble-the-final-json)
24. [Step 23: scan the output for PII, write files, close the job](#step-23-scan-write-close)
25. [Step 24: after processing (status, BI export, aliases, schema)](#step-24-after-processing)
26. [Step 25: the evaluation harness](#step-25-the-evaluation-harness)
27. [Step 26: the synthetic data generator and the tests](#step-26-synthetic-data-and-tests)
28. [Step 27: small shared helpers (utils.py)](#step-27-shared-helpers)
29. [Appendix A: function index](#appendix-a-function-index)
30. [Appendix B: design principles that repeat everywhere](#appendix-b-design-principles)

---

## 0. The big picture in one page

```text
python -m callintel run
   │
   ├─ cli.main()                     parse the command
   ├─ load_settings()                config.yaml + .env  -> Settings object
   ├─ connect_redis()                one Redis connection (queue, cache, locks, manifest)
   ├─ Engine.create()                load EVERYTHING once: prompts, taxonomy, catalogue,
   │                                 knowledge base, LLM gateway, PII detectors, normaliser, agents
   ├─ enqueue_folder()               hash each .txt, skip duplicates, push a job to the Redis Stream
   └─ worker_loop()                  read jobs, run up to N files at the same time
         │
         └─ Engine.process_file(path)            ONE CALL, start to finish
               ├─ RedisLock + Manifest           nobody else works on this file; skip if already done
               ├─ prepare_transcript()           CPU work, no LLM:
               │     parse_lines -> clean noise -> mask PII -> turns -> chunks
               ├─ Supervisor.analyze()           the agent graph:
               │     triage -> map (parallel LLM calls) -> ground facts -> normalise products
               │     -> 4 specialists in parallel (sales, customer, product, compliance)
               │     -> synthesis -> verification loop (critic + targeted retries)
               ├─ Assembler.build()              keep only verified claims -> CallAnalysis JSON
               ├─ _scan_output()                 last PII check on the result
               ├─ OutputSink.write()             per-call JSON + daily JSONL
               └─ manifest "done" / "needs_review", metrics, release lock
```

Three rules explain almost every design choice in the code:

1. **Do cheap, deterministic work first; call the LLM only where judgement is needed.** Parsing, cleaning,
   masking, chunking, triage, grounding and number checks are plain Python. The LLM is used for extraction
   (map), reasoning (specialists, synthesis) and checking (critic).
2. **Never trust the LLM's words; trust only what can be traced to a transcript line.** Every fact carries a
   line number and a verbatim quote, every claim cites fact IDs, and code (not the model) checks them.
3. **Everything must survive restarts and scale out.** Redis holds the queue, the per-file state, the locks,
   the caches and the rate limits, so any number of worker processes on any number of machines can share
   the work safely.

---

## 1. The running example

To make each step concrete, this guide follows one small (made-up) transcript through the pipeline.
Real files are up to 6,000 lines; the steps are the same.

```text
1  [00:00:01] IVR: Welcome to Horizon Bank. Calls may be recorded for quality.
2  [00:00:09] Agent: Thank you for calling Horizon Bank, this call is recorded. How may I help you?
3  [00:00:14] Rahul: um hi, my name is Rahul Sharma, I got an SMS about the plat card
4  [00:00:20] Agent: Sure. For verification, may I know your date of birth?
5  [00:00:24] Rahul: twelfth March nineteen ninety
6  [00:00:30] Agent: mm-hmm
7  [00:00:31] Agent: Thank you Mr Sharma. The Platinum Rewards Card has an annual fee of rupees 2,500
8  [00:00:40] Rahul: that's too expensive [inaudible] for me
9  [00:00:46] Agent: I can waive the first year fee. Interest is 3.5% per month.
10 [00:00:52] Rahul: okay, go ahead and apply. My card number is four one one one one one one one one one one one one one one one
```

By the end of the pipeline, this becomes a JSON with (for example) `primary_intent = product_enquiry`,
an objection `fees_too_high` citing line 8, the product `CC_PLATINUM_REWARDS` (normalised from
"plat card"), compliance rule `REC_DISCLOSURE = met` citing line 2, and no real name or card number anywhere.

---

## Step 1: the command starts (CLI)

**Files:** `callintel/__main__.py`, `callintel/cli.py`

### What happens

You type `python -m callintel run --input data/incoming`. Python runs `callintel/__main__.py`, which calls
`cli.main()`. `main()` reads the sub-command (`run`, `process`, `ingest`, `worker`, `status`, ...), loads the
settings, and hands over to the right part of the program.

### Function by function

| Function | Where | What it does |
|---|---|---|
| `__main__` block | `__main__.py:5` | Calls `main()` and turns its return value into the process exit code (`sys.exit`). |
| `main(argv)` | `cli.py:106` | Builds the `argparse` parser with one sub-parser per command, parses the arguments, calls `_settings()`, then dispatches with `if args.cmd == ...`. Returns 0 on success, 2 for an unknown command. |
| `_settings(args)` | `cli.py:25` | Calls `load_settings(--config, --profile)` and `setup_logging(--log-level or config value)`. Every command starts here, so all commands share one configuration path. |
| `_probe()` (inside `main`, `run`) | `cli.py:151` | Connects to Redis once to learn whether it is real Redis or the in-process fake. With real Redis it enqueues the folder now so that separate worker processes can pick the jobs up. |
| `_one()` (inside `main`, `process`) | `cli.py:168` | Builds an `Engine`, runs `process_file()` on one file, closes the engine. Used for debugging one call without a queue. |
| `_ing()` (inside `main`, `ingest`) | `cli.py:184` | Connects to Redis and calls `enqueue_folder()`; warns if Redis is the in-process fake (jobs would be lost when the process exits). |
| `_doctor(s)` | `cli.py:31` | Health check: Redis reachable and version ≥ 6.2 (needed for `XAUTOCLAIM`), each LLM role reachable and its model pulled, which PII detectors loaded, catalogue and knowledge-base sizes, prompt versions, and the egress-guard settings. Returns 1 if anything fails. |
| `_status(s)` | `cli.py:79` | Prints queue depth, dead-letter queue (DLQ) length, review queue length, alias-review queue length, number of learned aliases, manifest counts per status, and today's metrics. |
| `_aliases(s)` | `cli.py:97` | Prints every product alias the normaliser has learned (the Redis hash `ci:alias`). |

How `run` chooses its mode (`cli.py:144-164`):

- If Redis is the in-process fake **or** `--processes 1`: `run_local()` does ingest and processing in this one
  process.
- Otherwise: `run_worker_pool()` starts N OS processes that read the queue.

### Why this way

- **One entry point, many sub-commands.** Operations staff learn one tool. `doctor` exists because most
  production failures are environmental (Redis down, model not pulled), and a single command that checks
  everything saves hours.
- **Imports inside each branch** (`from .pipeline.worker import ...` inside `if args.cmd == "run"`): `status` or
  `schema` start instantly without loading PII models or HTTP clients.
- **`argparse` instead of Click/Typer:** no extra dependency, and the CLI is simple.

---

## Step 2: configuration is loaded

**File:** `callintel/config.py`

### What happens

`load_settings()` reads `.env`, then `config/config.yaml`, replaces every `${VAR:default}` with the environment
value, and validates the result into a typed `Settings` object. From then on, code reads settings as
attributes (`settings.chunking.target_tokens`), never as dictionary keys.

### Function by function

| Function / class | Where | What it does |
|---|---|---|
| `load_settings(config_path, profile)` | `config.py:242` | 1) `load_dotenv(.env)` without overriding variables already set in the shell. 2) Picks the config path: argument → `$CALLINTEL_CONFIG` → `config/config.yaml`. 3) `load_yaml()`. 4) `_interpolate()`. 5) `Settings.model_validate()`. 6) Applies `--profile` if given. |
| `load_yaml(path)` | `config.py:237` | `yaml.safe_load` (safe: cannot run code embedded in YAML). |
| `_interpolate(value)` | `config.py:23` | Walks dicts and lists recursively. In strings, replaces `${NAME:default}` with `os.environ["NAME"]` or the default. This is how `REDIS_URL`, `OPENAI_API_KEY` and `CALLINTEL_PROFILE` reach the config. |
| `get_settings()` | `config.py:256` | Cached (`lru_cache`) single settings instance for code that has no settings passed in. |
| `Settings.path(rel)` | `config.py:212` | Turns a relative path from config into an absolute path under the project root, so the program works from any current directory. |
| `Settings.profile()` | `config.py:216` | Returns the active LLM profile (`local`, `openai`, `vllm`, `mock`, ...) or raises a clear error listing the available ones. |
| `Settings.role(role)` | `config.py:222` | Returns the model settings for a role (`map`, `reduce`, `critic`, `normalizer`, `embedding`). If `critic` or `normalizer` is not defined, falls back to `reduce`. |
| `Settings.provider(name)` | `config.py:231` | Returns a provider's settings (`kind`, `base_url`, `api_key`, `trust_zone`, `max_concurrency`, `rpm`, `tpm`, `timeout_s`). |
| `repl(m)` (inside `_interpolate`) | `config.py:25` | Replacement function for one `${NAME:default}` match. |
| Section classes | `config.py:36-207` | Base class `_Section` (allows extra keys), then `AppCfg`, `PathsCfg`, `RedisCfg`, `WorkersCfg`, `EgressCfg`, `VaultCfg`, `PrivacyCfg`, `SpeakersCfg`, `PreprocessCfg`, `ChunkingCfg`, `TriageCfg`, `AgentsCfg`, `NormalizerCfg`, `RagCfg`, `OutputCfg`, `AuditCfg`, `ProviderCfg`, `RoleCfg`, `LLMCfg`. Each is a pydantic model with defaults, so a missing key in YAML still yields a working value. |

### Why this way

- **Roles instead of model names in code.** Code asks for `role("map")`; config decides that `map` is
  `qwen3:4b` on Ollama, or `gpt-6-luna` on OpenAI. Switching the whole system from local to cloud is one word:
  `--profile openai` (or `CALLINTEL_PROFILE=openai`). This meets the "local by default, OpenAI configurable with
  the key in config" requirement without touching code.
- **Typed settings (pydantic).** A typo such as `target_tokens: "abc"` fails at start-up with a clear message,
  not in the middle of the 600th file.
- **`${VAR:default}` interpolation.** Secrets (API keys) and per-machine values (Redis URL) stay in `.env` or
  the environment, not in the YAML that goes into git.
- **`extra="allow"`** on sections: new config keys can be added for experiments without breaking old code.

---

## Step 3: connect to Redis

**File:** `callintel/redis_layer/client.py`

### What happens

`connect_redis(settings)` opens an async Redis connection and pings it. It returns a `RedisHandle`: the
connection (`.r`), the key names (`.keys`) and a flag saying whether this is real Redis (`.is_fake`).

### Function by function

| Function / class | Where | What it does |
|---|---|---|
| `connect_redis(settings)` | `client.py:77` | `redis.asyncio.from_url(url, decode_responses=True, socket_connect_timeout=2, health_check_interval=30)` then `ping()`. If that fails and `allow_fakeredis_fallback` is true, it creates one in-process `fakeredis` server (shared by every connection in this process) and logs a warning. If the fallback is off (your config), it raises `Redis unreachable at ...`. |
| `Keys(prefix)` | `client.py:31` | The **single place** where every Redis key name is defined: streams (`ci:stream:ingest`, `ci:stream:dlq`, `ci:stream:review`, `ci:stream:norm_review`), the alias hash, and the manifest index. |
| `Keys.manifest(sha)` / `lock(name)` / `llm(digest)` / `emb(model, digest)` / `rl(bucket)` / `vault(call_id)` / `stats(day)` | `client.py:42-61` | Build per-item key names, for example `ci:manifest:<sha256>` or `ci:stats:2026-11-27`. |
| `RedisHandle.close()` | `client.py:70` | Closes the connection and swallows errors (closing must never crash shutdown). |

### Why this way

- **Redis does six jobs with one moving part:** work queue (Streams), idempotency state (manifest hashes),
  distributed locks, LLM/embedding caches, shared rate limits, and daily metrics. The alternative (Celery or
  RabbitMQ plus a database plus a cache) means three systems to run and monitor.
- **`decode_responses=True`:** values come back as `str`, so no `.decode()` calls everywhere.
- **`127.0.0.1`, not `localhost`:** on Windows, `localhost` can resolve to IPv6 `::1` first, and Memurai
  listens on IPv4, which adds a delay on every connect.
- **Fallback off by default in your config:** a silent fake Redis loses all state when the process exits, which
  is fine in a unit test and dangerous in production.
- **A central `Keys` class** means you can run two environments on one Redis by changing `key_prefix`
  (the evaluation harness does this: `ci:eval:*`).

---

## Step 4: the Engine is built once per process

**File:** `callintel/pipeline/engine.py` (`Engine.create`, line 50), plus the constructors it calls.

### What happens

Before any file is processed, `Engine.create(settings)` builds every long-lived object **once**. A worker
process then reuses them for thousands of files. In order:

```text
Engine.create
 ├─ connect_redis()                              (Step 3)
 ├─ PromptRegistry(config/prompts.yaml)          all prompt templates, versioned
 ├─ Taxonomy.load(config/taxonomy.yaml)          allowed labels (intents, objections, ...)
 ├─ ProductCatalog(config/products.yaml)         products, aliases, regex matchers
 ├─ KnowledgeBase(settings, catalog)             compliance rules + playbooks + product facts, BM25 index
 ├─ LLMSchemas(taxonomy, product ids, rule ids)  enum-constrained output models for every agent
 ├─ LLMGateway(settings, redis)                  the only door to any LLM
 ├─ ProductNormalizer(...)                       3-tier product name resolver
 ├─ build_recognizers(...)                       PII detectors (regex, context, Presidio, GLiNER)
 ├─ PIIVault, Manifest, Metrics, review StreamQueue, OutputSink
 ├─ AgentDeps(...)                               one bundle of shared dependencies for all agents
 ├─ Supervisor(deps)                             creates the map, specialist, synthesis and critic agents
 └─ _maybe_enable_dense()                        turn on embeddings if the embedding model is reachable
```

### Function by function

**Engine**

| Function | Where | What it does |
|---|---|---|
| `Engine.__init__(settings, redis)` | `engine.py:45` | Stores settings and the Redis handle. The real work is in `create`, because constructors cannot be `async`. |
| `Engine.create(settings, redis=None)` | `engine.py:50` | Async factory that builds everything listed above and returns the ready engine. |
| `Engine._maybe_enable_dense()` | `engine.py:74` | If `rag.dense_enabled` is `auto` or `true`, pings the `embedding` role; if it answers, embeds the knowledge base and the product documents (`kb.enable_dense`, `normalizer.enable_dense`). With `auto`, failure just logs "using BM25 + fuzzy" and carries on; with `true`, failure stops start-up. |
| `Engine.close()` | `engine.py:90` | Closes the LLM HTTP clients and the Redis connection. |

**Prompts** (`callintel/llm/prompts.py`)

| Function | Where | What it does |
|---|---|---|
| `PromptRegistry.__init__(path)` | `prompts.py:47` | Loads `prompts.yaml`: one entry per prompt id (`map_extract`, `sales_agent`, `customer_agent`, `product_agent`, `compliance_agent`, `synthesis_agent`, `critic_agent`, `normalizer_agent`) with `version`, `system`, `user`. |
| `PromptRegistry.get(pid)` | `prompts.py:53` | Returns one `Prompt`. |
| `PromptRegistry.versions()` | `prompts.py:56` | `{prompt_id: tag}` for all prompts; written into every output JSON. |
| `Prompt.tag` | `prompts.py:33` | `id@version+hash8`: the version you declared **plus** a hash of the text, so even an edit without a version bump is visible in the output. |
| `Prompt.with_static(**vars)` | `prompts.py:37` | Renders the `{{variables}}` of the system prompt once (for example, the fact-type guide) and returns a new `Prompt`. |
| `Prompt.messages(**user_vars)` | `prompts.py:41` | Returns `[{"role":"system",...},{"role":"user",...}]` with the user template rendered. |
| `render(template, **vars)` | `prompts.py:16` | Replaces `{{ name }}` (its inner `repl` handles one match). A missing variable raises an error instead of silently sending `{{name}}` to the model. |

**Taxonomy and output schemas** (`callintel/schemas.py`)

| Function | Where | What it does |
|---|---|---|
| `Taxonomy.load(path)` | `schemas.py:39` | Validates `taxonomy.yaml` into lists of allowed values (call types, intents, objections, complaints, risk flags, outcomes, sentiments, ...) and the fact-type descriptions. |
| `Taxonomy.labels_for(fact_type)` | `schemas.py:43` | Which labels are valid for a fact type (for example, `objection` → objection categories; `outcome` → accepted/declined/resolved/escalated/other). |
| `Taxonomy.fact_type_guide()` | `schemas.py:53` | Builds the text block "- objection: customer resistance ... label: fees_too_high\|..." that goes into the map prompt. |
| `Taxonomy.all_labels()` | `schemas.py:61` | Union of all labels; used for the `label` enum of a `Fact`. |
| `_L(values)` | `schemas.py:68` | Turns a list into a `Literal[...]` type, which pydantic exports as a JSON-schema `enum`. |
| `LLMSchemas.__init__(tax, product_ids, rule_ids, max_facts)` | `schemas.py:98` | Builds, with `create_model`, the output model of every agent: `Fact`/`MapOutput`, `SalesOutput`, `CustomerOutput`, `ProductOutput`, `ComplianceOutput`, `SynthesisOutput`, and uses the fixed `CriticOutput`. Every categorical field is an enum built from your YAML. |
| `LLMSchemas.normalizer_output(candidates)` | `schemas.py:156` | Builds (and caches) a tiny schema whose only field is `product_id: Literal[candidate ids..., "NONE"]`. |
| Fixed models | `schemas.py:75-92` | `ToolRequest` (tool name enum + argument), `TextClaim` (text + evidence ids), `CriticVerdict`, `CriticOutput`. |
| Final output models | `schemas.py:161-381` | `Evidence`, `Claim`, `Labeled`, `SourceInfo`, `ProcessingInfo`, `PrivacyInfo`, `Sentiment`, `CallProfile`, `Normalization`, `ProductInsight`, `ObjectionInsight`, `CrossSellInsight`, `SalesInsights`, `CustomerInsights`, `RuleResult`, `RiskFlag`, `MisquotedTerm`, `ComplianceInsights`, `QuotedTerm`, `AgentPerformance`, `ReviewItem`, `FactOut`, `Quality`, `CallAnalysis`. These define the final JSON contract (Step 22). |

**Product catalogue** (`callintel/normalize/catalog.py`)

| Function | Where | What it does |
|---|---|---|
| `ProductCatalog.__init__(path)` | `catalog.py:40` | Loads `products.yaml` into `Product` objects; builds `alias_map` (normalised alias → product id, specific products beat generic ones on collisions); compiles `_matcher` (all aliases, **longest first**, so "platinum rewards card" wins over "card") and `_name_matcher` (only specific product names, used to protect them from PII masking). |
| `norm_mention(text)` | `catalog.py:16` | Normal form of a product mention: lower-case, `&`→`and`, `-`→space, strip punctuation, drop leading words such as "the/my/your/new". "The Plat-Card!" → "plat card". |
| `Product.all_names` | `catalog.py:35` | Name plus aliases. |
| `ProductCatalog.ids` / `get(pid)` | `catalog.py:65-70` | List of ids; look up one product. |
| `ProductCatalog.find_mentions(text)` | `catalog.py:72` | Every catalogue alias found in a text: `(surface, product_id, start, end)`. Used by triage and the recall booster. |
| `ProductCatalog.protected_spans(text)` | `catalog.py:83` | Character spans of specific product names. The PII masker will not mask a "name" inside these spans (otherwise NER may think "Platinum" or "Rewards" is a person). |

**Knowledge base** (`callintel/rag/knowledge_base.py`) and **retrieval** (`callintel/rag/retrieval.py`)

| Function | Where | What it does |
|---|---|---|
| `KnowledgeBase.__init__(settings, catalog)` | `knowledge_base.py:41` | Loads `compliance_rules.yaml` into `Rule` objects (8 rules, for example `REC_DISCLOSURE`, `ID_VERIFICATION`, `APR_FEE_DISCLOSURE`, `NO_OTP_REQUEST`); splits every `knowledge_base/*.md` file on `## ` headings into passages; adds one passage per product (name, category, description, fees/rates). Builds one `HybridIndex` over all of them, tagged by collection (`compliance`, `playbook`, `product`). |
| `Doc` | `retrieval.py:62` | One searchable passage: id, text, metadata (collection, rule id, product id, source). |
| `HybridIndex.__init__(docs)` | `retrieval.py:77` | Tokenises every document and builds a `BM25` index. |
| `BM25.__init__(docs, k1, b)` | `retrieval.py:37` | Builds postings lists (term → [(doc, term frequency)]) and IDF values. Classic Okapi BM25. |
| `tokenize(text)` / `_stem(t)` | `retrieval.py:32`, `:25` | Lower-case word tokens, stop words removed, light suffix stemming ("charges" → "charg", "waived" → "waiv"). |
| `HybridIndex.enable_dense(embed_fn)` / `KnowledgeBase.enable_dense` | `retrieval.py:83`, `knowledge_base.py:68` | Embeds every document once and keeps the matrix in memory. |
| `KnowledgeBase.rule_ids` | `knowledge_base.py:72` | Rule ids for the compliance enum (`["NONE"]` if there are no rules, so the enum is never empty). |

**Agents** (constructors only; their `run` methods are in Steps 14-21)

| Function | Where | What it does |
|---|---|---|
| `AgentDeps` | `agents/context.py:28` | Dataclass bundling settings, gateway, prompts, schemas, taxonomy, catalogue, knowledge base, normaliser. Every agent gets this one object. |
| `Supervisor.__init__(deps)` | `agents/supervisor.py:57` | Creates `MapAgent`, `SalesAgent`, `CustomerAgent`, `ProductAgent`, `ComplianceAgent`, `SynthesisAgent`, `CriticAgent`. |
| `MapAgent.__init__(deps)` | `agents/mapper.py:18` | Pre-renders the map system prompt with `max_facts` and the fact-type guide (see "Why" below). |
| `BaseAgent.__init__(deps)` | `agents/base.py:27` | Stores deps and loads the agent's prompt by `prompt_id`. |
| `CriticAgent.__init__(deps, batch=10)` | `agents/critic.py:22` | Loads the critic prompt; 10 claims per critic call. |

The gateway, normaliser, PII detectors, vault, manifest, metrics and output sink constructors are explained in
the step where they do their main work (Steps 15, 17, 10, 23, 7, 7 and 23).

### Why this way

- **Build once, reuse many times.** Loading spaCy/GLiNER models takes seconds and hundreds of MB; compiling
  the catalogue regex and BM25 index is fast but would add up over 1,000 files. Building per process, not per
  file, is the single biggest start-up saving.
- **Schemas generated from YAML.** Adding a new objection category is a YAML change; the enum in the JSON
  schema, the decoder constraint and the validation all update automatically. Nobody edits Python for a
  taxonomy change.
- **Enums are what make outputs comparable across thousands of calls.** If the model could write free text for
  `primary_intent`, you would get "card enquiry", "Card Enquiry", "asked about card"; with an enum it can
  only emit one of your values.
- **In-memory BM25 instead of a vector database.** The knowledge base is small (hundreds of passages), so an
  in-process index answers in well under a millisecond and needs no extra service. `HybridIndex.search()` has
  the same signature a Redis-vector or Qdrant backend would have, so swapping it later is a local change.
- **Pre-rendered static system prompt** (`with_static`): the system prompt is byte-identical for every chunk,
  which lets Ollama/vLLM/OpenAI reuse the cached prefix (KV cache / prompt caching) instead of re-reading it.
- **Embeddings are optional (`auto`).** BM25 + fuzzy matching already works offline; dense retrieval improves
  recall when an embedding model is available, but the system never depends on it.

---

## Step 5: files are found and queued (ingest)

**Files:** `callintel/pipeline/worker.py`, `callintel/redis_layer/store.py`, `callintel/redis_layer/streams.py`

### What happens

`enqueue_folder()` lists the `.txt` files in the input folder. For each file that is not still being written,
it computes the SHA-256 of the content and asks the manifest whether this content was already queued or
processed. Only new (or failed, or `--force`) files become a job on the Redis Stream `ci:stream:ingest`.

In the running example: `call_0001.txt` → SHA-256 `9f2c41ab7e3d0c55...` → manifest has no record → status set to
`queued` → job `{"path": ".../call_0001.txt", "sha": "9f2c...", "attempts": 0}` added to the stream.

### Function by function

| Function | Where | What it does |
|---|---|---|
| `enqueue_folder(settings, redis, input_dir, force, recursive)` | `worker.py:35` | Lists `*.txt` (or recursively). Skips files modified in the last `stable_file_age_s` seconds (still being copied). Hashes each file in a thread (`asyncio.to_thread(sha256_file)`), calls `Manifest.register`, enqueues if allowed. Returns counts: `found`, `queued`, `skipped_duplicate_or_done`, `skipped_unstable`. |
| `queues(redis, settings)` | `worker.py:29` | Returns the two `StreamQueue` objects used everywhere: the ingest queue and the DLQ. |
| `sha256_file(path)` | `utils.py:29` | Hashes in 1 MB blocks, so memory stays flat for any file size. |
| `Manifest.__init__(r, keys)` | `store.py:66` | Stores the Redis connection and key names. |
| `Manifest.register(sha, file, force)` | `store.py:69` | If the status is `queued`, `processing`, `done` or `needs_review` (and not forced) → returns False (skip). Otherwise writes `status=queued`, the file path and time, adds the hash to the sorted-set index `ci:manifest:index`, and returns True. `failed` and `dead_lettered` files are allowed back in. |
| `StreamQueue.__init__(r, stream, group, maxlen)` | `streams.py:25` | Remembers the stream name, the consumer group (`workers`) and the maximum length. |
| `StreamQueue.ensure_group()` | `streams.py:31` | `XGROUP CREATE ... MKSTREAM`; ignores "BUSYGROUP" (group already exists), so it is safe to call every time. |
| `StreamQueue.enqueue(payload)` | `streams.py:38` | `XADD stream MAXLEN ~ N * data=<json>`. The approximate `MAXLEN` keeps the stream from growing forever. |
| `watch_folder(settings, input_dir, interval_s)` | `worker.py:59` | Production mode (`ingest --watch 10`): runs `enqueue_folder` every N seconds forever. |

### Why this way

- **Content hash, not file name, is the identity of a call.** The same recording re-delivered under another
  name is not processed twice (cost saving); a corrected file with the same name **is** processed (its hash
  changed). The first 16 hex characters of the hash become the `call_id`.
- **Idempotent ingest.** You can run `ingest` as often as you like (cron, watcher, manual re-runs); the
  manifest makes it safe.
- **Stable-age check.** A file being copied over the network looks complete to `glob` but is truncated. Waiting
  5 seconds after the last modification avoids analysing half a call.
- **Redis Streams instead of a list (`LPUSH`/`BRPOP`):** a list loses the job if the worker crashes after popping
  it. Streams keep the message "pending" until it is acknowledged, so a crashed worker's job can be reclaimed.
- **Hashing in a thread** (`to_thread`) keeps the event loop free; with many large files this matters.

---

## Step 6: workers take jobs from the queue

**Files:** `callintel/pipeline/worker.py`, `callintel/redis_layer/streams.py`

### What happens

There are two levels of parallelism:

- **Level 1, processes:** `run_worker_pool()` starts N OS processes (default 2). Each has its own Python
  interpreter, so CPU-heavy PII masking runs truly in parallel (no GIL contention).
- **Level 2, concurrent files:** inside each process, `worker_loop()` runs up to M files at once (default 4) on
  one asyncio event loop. While one file waits for the LLM, another is being masked or parsed.

Inside a file there is a third level: the map calls and the four specialists run concurrently, limited by the
gateway's per-provider semaphore and the shared Redis rate limiter (Step 15).

### Function by function

| Function | Where | What it does |
|---|---|---|
| `run_local(settings, input_dir, concurrency, force)` | `worker.py:168` | One-process mode: build an `Engine`, `enqueue_folder`, then `worker_loop(exit_when_idle=True)` with that same engine; returns ingest counts, work stats and wall time. Used on a laptop and with the fake Redis. |
| `run_worker_pool(settings, processes, concurrency, exit_when_idle)` | `worker.py:150` | If 1 process: runs `worker_loop` directly. Otherwise starts `processes` child processes with the `spawn` method, each running `_process_entry`, and waits for them; Ctrl+C terminates them. |
| `_process_entry(config_path, profile, idx, concurrency, exit_when_idle)` | `worker.py:142` | Runs in each child: reloads settings (objects cannot be shared across processes), sets up logging, runs `worker_loop` with a unique consumer name `host-w<idx>-<pid>`. |
| `worker_loop(settings, concurrency, exit_when_idle, engine, name)` | `worker.py:71` | The scheduler. Loop: if all M slots are busy, wait for one task to finish. Every 30 s, first `reclaim` stuck messages. Then `read` up to `free` new messages (block 3 s when idle, 0.2 s when busy). Start one asyncio task per message (`handle`). If idle twice and `exit_when_idle`, check `depth()` and stop when the stream is empty. On exit, waits for running tasks and closes its own engine. Returns stats: done, needs_review, requeued, dead_lettered, skipped. |
| `handle(msg)` (inside `worker_loop`) | `worker.py:84` | Calls `engine.process_file(path, sha, force)`. **Success:** `ack` and count the status. **`NonRetryableError`** (for example the egress guard blocked PII): send straight to the DLQ, ack, mark the manifest `dead_lettered`. **Any other error:** `retry_or_dead_letter` (re-queue with `attempts+1`, or DLQ after `max_attempts=3`). |
| `Message` | `streams.py:19` | Dataclass: stream message id + decoded payload. |
| `StreamQueue.read(consumer, count, block_ms)` | `streams.py:50` | `XREADGROUP GROUP workers <consumer> COUNT n BLOCK ms STREAMS ingest >`: new messages never delivered to anyone. |
| `StreamQueue._parse(entries)` | `streams.py:42` | Converts raw stream entries into `Message` objects; skips entries deleted while pending. |
| `StreamQueue.reclaim(consumer, min_idle_ms, count)` | `streams.py:57` | `XAUTOCLAIM` messages pending longer than the visibility timeout (30 min): their worker crashed or hung. This is what makes delivery "at least once". |
| `StreamQueue.ack(msg_id)` | `streams.py:67` | `XACK` + `XDEL` in one pipeline: the job is finished and removed, so the stream length equals the backlog. |
| `StreamQueue.retry_or_dead_letter(msg, error, max_attempts, dlq)` | `streams.py:73` | Increments `attempts`; below the limit, re-enqueues the job (with `last_error`); at the limit, writes it to the DLQ. Acks the original in both cases. Returns `requeued` or `dead_lettered`. |
| `StreamQueue.depth()` | `streams.py:86` | `in_stream` (XLEN), `pending` (XPENDING), `waiting = in_stream - pending`. |

### Why this way

- **Processes for CPU, asyncio for I/O.** PII masking is CPU-bound; LLM calls are I/O-bound (waiting on HTTP).
  Processes give real CPU parallelism; asyncio gives cheap concurrency while waiting. Threads alone would be
  limited by the GIL for the CPU part; processes alone would waste memory while waiting.
- **`spawn`, not `fork`:** works the same on Windows and Linux, and avoids copying half-initialised model state.
- **At-least-once delivery + idempotent processing = effectively once.** Streams may deliver a job twice (after a
  reclaim); the per-file lock and manifest (Step 7) make the second delivery a cheap no-op.
- **Retry counter and DLQ.** A poison file (corrupt encoding, a model that always times out on it) cannot block
  the queue forever; after 3 attempts it goes to `ci:stream:dlq` for a human, with the last error attached.
- **Non-retryable errors skip retries.** If raw PII would leave the machine, retrying gives the same result;
  it goes to the DLQ at once.
- **Capacity math (why this meets 1,000+ files a day):** 1,000 files/day ≈ 42 files/hour ≈ one file every
  86 seconds. With 2 processes × 4 files in flight, each file may take up to about 11 minutes and the system
  still keeps up, provided the LLM backend can serve that many requests. With the local 4B model on one GPU,
  the GPU is the bottleneck, which is why Ollama has `max_concurrency: 1` and why triage (Step 13) exists.

---

## Step 7: process_file opens a call

**Files:** `callintel/pipeline/engine.py` (`process_file`, line 124), `callintel/redis_layer/store.py`,
`callintel/llm/gateway.py` (`usage_scope`), `callintel/utils.py` (`Stopwatch`)

### What happens

`Engine.process_file(path, sha, force)` is the function that turns one transcript file into one JSON. Its first
lines make sure that only one worker in the whole fleet works on this call, and that finished calls are not
paid for twice:

```text
sha     = sha or sha256_file(path)            (the queue usually passes it in)
call_id = sha[:16]
lock    = RedisLock("ci:lock:<sha>", ttl = 30 min)
if not lock.acquire():           return "locked_elsewhere"
manifest = Manifest.get(sha)
if status in (done, needs_review) and not force:  return "skipped_already_done"
Manifest.set(sha, "processing", file, worker pid)
raw = read the file (UTF-8, bad bytes replaced)
start a Stopwatch and a usage_scope(call_id)
prepared = prepare_transcript(...)   in a worker thread      → Steps 8-11
outputs, ctx, report = Supervisor.analyze(prepared)          → Steps 12-21
...
```

### Function by function

| Function | Where | What it does |
|---|---|---|
| `Engine.process_file(path, sha, force)` | `engine.py:124` | The per-call orchestrator (the later part is explained in Steps 22-23). |
| `RedisLock.__init__(r, key, ttl_ms)` | `store.py:33` | Generates a random token (`secrets.token_hex(8)`) that identifies this lock holder. |
| `RedisLock.acquire()` | `store.py:38` | `SET key token NX PX ttl`: succeeds only if nobody holds the key; the key expires by itself after the TTL, so a crashed worker cannot hold it forever. |
| `RedisLock.release()` | `store.py:42` | Runs a tiny Lua script: delete the key **only if it still contains my token**. Fallback without Lua: compare, then delete. |
| `RedisLock.__aenter__` / `__aexit__` | `store.py:53-58` | Lets the lock be used as `async with RedisLock(...)`. |
| `Manifest.get(sha)` | `store.py:85` | `HGETALL ci:manifest:<sha>`: status, file, timestamps, output path, error. |
| `Manifest.set(sha, status, **fields)` | `store.py:79` | Writes the new status, a `<status>_at` timestamp and any extra fields (non-scalar values as JSON). |
| `Stopwatch()` / `.stage(name)` / `.total_ms()` | `utils.py:104-118` | `with sw.stage("map"):` records milliseconds per stage into `sw.marks`; these end up in `processing.stage_ms` of the output. |
| `usage_scope(call_id)` | `gateway.py:86` | A context manager that puts a fresh `Usage` object and the `call_id` into Python **context variables**. Every LLM call made anywhere inside (even in parallel tasks) adds its tokens, latency and cache hits to this call's `Usage`, without passing an object through every function. |
| `Usage.add(role, model, tin, tout, ms, cached)` | `gateway.py:64` | Adds one LLM call (or one cache hit) to the totals and the per-role breakdown. |

### Why this way

- **Lock + manifest = exactly one useful run per file.** The queue can deliver a job twice (Step 6); the lock
  stops two workers from running it at the same time, and the manifest stops a second run after the first
  finished.
- **Token-checked release.** Without the token check, this can happen: worker A's lock expires, worker B takes
  it, then A finishes and deletes **B's** lock. The Lua script makes compare-and-delete atomic.
- **TTL = visibility timeout (30 min).** The lock expires at the same time the stream would reclaim the job, so a
  crashed worker's call can be retried by someone else.
- **`errors="replace"` when reading.** Real transcripts sometimes contain invalid bytes; one bad byte should not
  fail a 6,000-line call.
- **Context variables for usage.** Concurrency-safe: two calls running at the same time in one process each see
  their own `Usage`, because asyncio copies the context into each task.
- **Preprocessing in a thread** (`asyncio.to_thread`): it is pure CPU work; running it in a thread lets the event
  loop keep sending other files' LLM requests in the meantime.

---

## Step 8: parse lines and work out who is speaking

**File:** `callintel/preprocess/transcript.py` (`prepare_transcript`, line 228, stage `parse`)

### What happens

`prepare_transcript()` is the whole preprocessing stage. It runs four sub-stages, each timed:
`parse` → `denoise` → `pii_mask` → `chunk`. This step covers `parse`.

`parse_lines()` reads every non-empty line with one regex that understands the common transcript formats:

```text
[00:00:14] Rahul: text        00:14 - Agent: text       Speaker 1 (00:14): text       Agent> text
```

It extracts the timestamp, the speaker label and the text, **keeping the original line number**. Then
`_label_role_map()` decides which label is the agent and which is the customer.

Running example:

| Line | Label | Role decided | Why |
|---|---|---|---|
| 1 | `IVR` | `SYSTEM` | `ivr` is in `preprocess.speakers.system` |
| 2, 4, 6, 7, 9 | `Agent` | `AGENT` | `agent` is in `preprocess.speakers.agent` |
| 3, 5, 8, 10 | `Rahul` | `CUSTOMER` | unknown label, frequent (≥3% of lines), and an agent already exists, so it is the customer |

### Function by function

| Function / class | Where | What it does |
|---|---|---|
| `prepare_transcript(raw_text, call_id, file_name, sha256, settings, cheap, ner, protected_spans)` | `transcript.py:228` | Runs parse, denoise, PII masking and chunking; returns a `PreparedTranscript` (Step 11). |
| `parse_lines(raw_text, settings)` | `transcript.py:141` | Pass 1: regex-parse each line, count labels, collect each label's text. Pass 2: assign roles. A line with no label inherits the current speaker (continuation lines). A "label" that is not a speaker (for example `Note: ...`) is put back into the text. |
| `_label_role_map(labels, lines_by_label, settings)` | `transcript.py:113` | Known labels come from config (`agent`, `rep`, `speaker 1`, `customer`, `caller`, `ivr`, ...). Unknown but frequent labels (≥3% of lines, ≤3 words) are probably people's names. Each is scored with `_AGENT_CUES` ("thank you for calling", "how may I help", "for verification", ...) minus `_CUSTOMER_CUES` ("I want to", "my card", "I got charged", ...). If no agent is known yet, the best-scoring name becomes the agent; the others become customers. |
| `agent_score(lab)` (inside `_label_role_map`) | `transcript.py:127` | Counts agent cue matches minus customer cue matches over that label's first 400 lines. |
| `_LINE` regex | `transcript.py:23` | Optional timestamp (`[00:01]`, `00:01:02.5`), optional speaker label (1-31 characters, optionally followed by a time), separator (`:`, `>`, full-width `：`), then text. |
| `Line` dataclass | `transcript.py:35` | `no` (original line number), `speaker`, `text`, `ts`. |
| Role constants | `transcript.py:20-21` | `AGENT`, `CUSTOMER`, `SYSTEM`, `UNKNOWN` and the one-letter codes `A`, `C`, `S`, `?` used in prompts. |

### Why this way

- **Original line numbers are kept on everything.** Every later fact, claim and evidence item points back to a
  real line of the original file. This is the foundation of traceability and of hallucination checks.
- **The speaker is decided by code, not by the LLM.** Later, a "customer objection" on an agent line can be
  rejected mechanically (Step 16). Asking the model "who said this?" would add cost and errors.
- **Name labels handled.** Many transcription tools label speakers by name ("Rahul:", "Priya:"). The cue-phrase
  scoring works without any model and gets the common case right; known labels from config always win.
- **One tolerant regex instead of per-vendor parsers.** Different ASR vendors produce slightly different
  formats; one forgiving pattern handles all common ones.

---

## Step 9: clean the noise

**Files:** `callintel/preprocess/transcript.py` (stage `denoise`), `callintel/preprocess/cleaning.py`

### What happens

For each parsed line, in this order:

1. **System lines** (IVR, hold messages) are dropped, **unless** they match `keep_system_if_matches`
   (`record`, `monitor`, `consent`, `terms`, `privacy`). Line 1 of the example says "Calls may be recorded", so
   it is **kept**: it is compliance evidence.
2. **Backchannel-only lines** ("mm-hmm", "uh-huh") are dropped. Line 6 is dropped.
3. `NoiseCleaner.clean()` cleans the text: ASR tags, fillers, spoken digits, stutters, extra spaces.
   Line 3 loses "um"; line 8 loses "[inaudible]"; line 10's spoken card number becomes `4111111111111111`.
4. **Empty after cleaning** → dropped.
5. **Exact duplicate of the previous line by the same speaker** (an ASR double segment) → dropped.
6. **Repetition compression:** lines of 5+ words are reduced to a "template" (numbers replaced by `#`,
   punctuation removed). Each template is kept at most `max_repeats` (2) times per speaker. A hold loop
   ("still checking, please stay on the line" × 50) or a statement read-out ("on the 9th there is a debit of
   rupees 1,250" × 80) shrinks to two lines.

Every drop is counted (`dropped_system`, `dropped_backchannel`, `dropped_duplicate`, `dropped_repetitive`, ...);
the counts appear in the output under `domain_insights.noise`.

### Function by function

| Function / regex | Where | What it does |
|---|---|---|
| `NoiseCleaner.__init__(remove_fillers, collapse_repeats, spoken_digits)` | `cleaning.py:88` | Feature switches from config. |
| `NoiseCleaner.clean(text)` | `cleaning.py:94` | Applies, in order: remove `_TAGS`; remove `_FILLERS`; `normalize_spoken_digits`; `_collapse_repeats`; squeeze spaces; return `""` if only punctuation is left. |
| `_TAGS` | `cleaning.py:23` | Bracketed ASR annotations: `[inaudible]`, `(crosstalk)`, `<laughter>`, `{hold music}`, `[pause]`, `[blank_audio]`, ... |
| `_FILLERS` | `cleaning.py:28` | `um`, `uh`, `erm`, `er`, `ah`, `hmm`, `mm-hmm`, `uh-huh` as whole words (so "umbrella" and "summer" are safe). |
| `normalize_spoken_digits(text, min_run=4)` | `cleaning.py:35` | Scans word tokens. A run of digit words ("four", "oh", "double seven", "triple two") and digits, separated only by spaces/commas/dashes, becomes one digit string if it has at least 4 digits and at least one spoken word. "oh"/"o" count as 0 only inside a run (so "oh, okay" is untouched). `double seven` → `77`. |
| `_collapse_repeats(text)` (and its inner `repl`) | `cleaning.py:77` | "I I I want" → "I want". **Never** collapses digits or digit words, because "one one one" may be part of a number. |
| `_BACKCHANNEL_ONLY` | `transcript.py:223` | Line consisting only of mm/hmm/uh-huh/mhm. |
| `_TEMPLATE_NUM`, `_TEMPLATE_PUNCT` | `transcript.py:224-225` | Build the repetition template (numbers → `#`, punctuation removed). |

### Why this way

- **Deterministic cleaning before the LLM.** Noise costs tokens (money and GPU time) and distracts small models.
  Regex cleaning costs microseconds per line, is identical on every run, and is testable.
- **Meaning-bearing short answers are protected.** "yes", "no", "okay", "I agree" carry consent and outcome;
  only pure backchannels are dropped, and repetition compression ignores lines under 5 words.
- **Spoken digits are converted before PII detection.** Callers read card and phone numbers aloud. Without this
  step, "four one one one ..." would pass straight through every regex and reach the LLM unmasked. This is a
  privacy fix as much as a cleaning step.
- **Repetition compression is the main speed lever for 6,000-line files.** Long calls are long mostly because of
  hold loops and read-outs. Keeping 2 copies preserves the fact that it happened while removing thousands of
  tokens. Templates replace numbers with `#` so "debit of 1,250 on the 9th" and "debit of 300 on the 12th" count
  as the same pattern.
- **System lines are dropped unless they matter.** The IVR's recording notice is evidence for the
  `REC_DISCLOSURE` rule, so it is kept; the hold music announcements are not.

---

## Step 10: mask PII

**Files:** `callintel/preprocess/pii.py`, `callintel/preprocess/transcript.py` (stage `pii_mask`)

### What happens

A new `PIIMasker` is created **for each call** and `mask_lines()` is called with all kept lines and their
speakers. Masking is two-pass:

- **Pass 1, detect:** run the detectors on every line, including the dialogue-aware detector (a question by one
  speaker tells us what the other speaker's next answer contains), and learn the call's names.
- **Pass 2, mask:** add name matches found anywhere in the call (a "gazetteer" of this call's names), drop
  detections inside protected spans (product names, allow-listed terms), resolve overlaps, and replace each
  value with a **consistent surrogate**: the same value always becomes the same token within the call.

Running example after masking:

```text
1  S: Welcome to Horizon Bank. Calls may be recorded for quality.          ("Horizon Bank" is allow-listed)
2  A: Thank you for calling Horizon Bank, this call is recorded. How may I help you?
3  C: hi, my name is <CUST_NAME_1>, I got an SMS about the plat card       (name cue "my name is")
4  A: Sure. For verification, may I know your date of birth?
5  C: <DOB_1>                                                              (dialogue-aware: answer to a DOB question)
7  A: Thank you Mr <CUST_NAME_1>. The Platinum Rewards Card has an annual fee of rupees 2,500
8  C: that's too expensive for me
9  A: I can waive the first year fee. Interest is 3.5% per month.
10 C: okay, go ahead and apply. My card number is <CARD_1>                 (16 digits, passes the Luhn check)
```

"Sharma" on line 7 became `<CUST_NAME_1>`, the same token as "Rahul Sharma" on line 3, because pass 1 learned that
"sharma" is part of that name. "Platinum Rewards Card" was not mistaken for a person because it is a protected
product span. The fee `2,500` and the rate `3.5%` are not masked: they are business facts, not identifiers.

### The detectors (layered, cheapest first)

| Detector | Class | What it catches | How it avoids false alarms |
|---|---|---|---|
| 1. `regex` | `RegexRecognizer` (`pii.py:106`) | E-mail, UPI id, card (13-19 digits), Aadhaar (12 digits), PAN, IFSC, IBAN, SSN, Indian/international/US phone numbers, and any remaining run of ≥9 digits (`ID_NUMBER`). | Cards must pass the **Luhn** checksum, Aadhaar the **Verhoeff** checksum. Existing surrogates are ignored. |
| 2. `context` | `ContextRecognizer` (`pii.py:166`) | Values announced by a cue phrase: "account number is ...", "ending with 1234", "OTP is ...", "CVV ...", "my date of birth is ...", "my address is ...", "pin code ...", PAN/passport, customer/reference/ticket numbers, spoken e-mails ("rahul dot s at gmail dot com"), names after "my name is / this is / I'm" (speaker's own name) or "Mr / Ms / speaking with" (the other party's name). | Name tokens stop at common words (`COMMON_WORDS`: "calling", "regarding", "card", ...). In mixed-case text, names must be capitalised. |
| 2b. dialogue | `ContextRecognizer.expected_types` + `analyze_answer` | The answer after a question: "date of birth?" → next customer line's date; "card number?" → next digits; "your name?" → next words; "your address?" → next sentence. | Only the next 2 lines of the **other** speaker are checked; the expectation resets when the asker speaks again. |
| 3. `presidio` (optional) | `PresidioRecognizer` (`pii.py:285`) | Microsoft Presidio + spaCy NER: persons, locations, and built-in recognisers. | Runs only on lines passing `_NER_GATE` (words like "name", "address", "mr", "street") or with a capitalised word mid-sentence. |
| 4. `gliner` (optional) | `GlinerRecognizer` (`pii.py:309`) | GLiNER multi-PII model with labels person, address, date of birth, e-mail, phone, account, card, passport. | Same gate; score threshold 0.5. |

### Function by function

| Function | Where | What it does |
|---|---|---|
| `luhn_ok(number)` | `pii.py:42` | Luhn checksum for 13-19 digit card numbers. A random 16-digit number passes only 1 time in 10, which removes most false positives (order ids, amounts). |
| `verhoeff_ok(number)` | `pii.py:65` | Verhoeff checksum (Aadhaar's check digit) for 12-digit numbers. |
| `Span` | `pii.py:77` | One detection: start, end, entity type, text, score, source detector, role (for names). |
| `Recognizer` | `pii.py:87` | The interface every detector implements: `name` + `analyze(text, speaker)`. |
| `_Pat` | `pii.py:94` | One pattern: entity type, regex, capture group, optional validator, score. |
| `_digits(s)` | `pii.py:102` | Keeps only digits. |
| `RegexRecognizer.__init__` / `.analyze(text, speaker)` | `pii.py:111`, `:128` | Runs every pattern; applies validators; skips text that is already a surrogate. |
| `ContextRecognizer.__init__` | `pii.py:171` | Compiles cue patterns, question cues, answer-lead stripper (`yes`, `it's`, `my name is`, ...), date and digit-run patterns, and the two name patterns. |
| `ContextRecognizer._clean_name(raw, mixed_case)` | `pii.py:210` | Keeps name tokens until it hits a common word or (in mixed case) a lower-case word. "Rahul Sharma calling about" → "Rahul Sharma". |
| `ContextRecognizer.analyze(text, speaker)` | `pii.py:221` | Cue-phrase patterns + names. A name after "my name is" gets the **speaker's** role; after "Mr"/"speaking with" it gets the **other** role. References must contain a digit. |
| `ContextRecognizer.expected_types(text)` | `pii.py:242` | Which PII types a line is asking for (DOB, PERSON, ACCOUNT, CARD, PHONE, ADDRESS, PINCODE, OTP). |
| `ContextRecognizer.analyze_answer(text, expected, speaker)` | `pii.py:246` | Strips the answer lead, then: dates for DOB; digit runs classified by length (≥12 → CARD if asked, ≥10 → PHONE, ≥6 → ACCOUNT, 5-6 → PINCODE, 4-8 → OTP, else PARTIAL_NUMBER); a short name for PERSON; the whole answer (3+ words) for ADDRESS. |
| `PresidioRecognizer.__init__(model)` / `.analyze` | `pii.py:291`, `:300` | Builds the Presidio analyser with the spaCy model; maps Presidio entity names to ours; ignores `DATE_TIME` (too many false positives on "yesterday", "next week"). |
| `GlinerRecognizer.__init__(model, threshold)` / `.analyze` | `pii.py:315`, `:321` | Loads the GLiNER model; maps its labels to ours. |
| `build_recognizers(detectors, presidio_model, gliner_model, threshold)` | `pii.py:333` | Builds the configured detectors, split into **cheap** (regex, context: every line) and **NER** (Presidio, GLiNER: gated lines). A detector that is not installed or fails to load is skipped with a warning; masking still works with the rest. |
| `PIIMasker.__init__(cheap, ner, protected_spans, allow_terms)` | `pii.py:377` | Per-call state: surrogate registry, counters per prefix, name aliases; compiles the allow-list regex. |
| `PIIMasker.mask_lines(lines)` | `pii.py:452` | The two-pass algorithm described above. Returns a `MaskResult`. |
| `PIIMasker._detect(text, speaker)` | `pii.py:404` | Cheap detectors always; NER detectors only if `_NER_GATE` or `_CAP_INSIDE` match. An NER crash on one line is logged, not raised. |
| `PIIMasker._learn_names(spans)` | `pii.py:424` | For each PERSON span, records the full name and each token of 3+ letters (not a common word) as an alias of that full name, with its role. |
| `PIIMasker._gazetteer()` | `pii.py:437` | One regex of all names learned in this call, longest first. Pass 2 uses it to mask names said before or after the introduction. |
| `PIIMasker._protected(text)` | `pii.py:416` | Spans that must not be masked as names/locations/addresses: product names (`catalog.protected_spans`) and allow-listed terms (your bank's name). |
| `PIIMasker._resolve(spans)` | `pii.py:444` | Overlapping detections: keep the highest score, then the longest, then the earliest. |
| `PIIMasker._surrogate(etype, value, role)` | `pii.py:389` | Returns the existing token for this value or creates the next one: `<CARD_1>`, `<PHONE_2>`, `<CUST_NAME_1>`, `<AGENT_NAME_1>`. |
| `_canon(etype, value)` | `pii.py:364` | Canonical value used for consistency: digits only for numbers (last 10 digits for phones, so "+91 98765 43210" and "9876543210" match), lower-case alphanumerics otherwise. |
| `MaskResult` | `pii.py:357` | Masked lines, counts per type, `mapping` (surrogate → original, used only by the optional vault), detector names. |
| `find_pii(text, recognizer)` | `pii.py:498` | High-precision scan (regex only, ignoring partial numbers), used later by the egress guard (Step 15) and the output scanner (Step 23). |
| `SURROGATE_RE`, `PREFIX`, `SOFT_TYPES`, `COMMON_WORDS`, `_NER_GATE`, `_CAP_INSIDE` | `pii.py:28-38`, `:144`, `:328-330` | Surrogate pattern; entity → token prefix; types that respect product protection; stop list for names; NER gates. |

### Why this way

- **Masking happens before any LLM sees the text**, inside this process. Even the local model gets masked text,
  so a later switch to a cloud model does not change the privacy story.
- **Layers, cheapest first.** Regex with checksums is precise and microseconds-fast for structured IDs; cue
  phrases catch what regexes cannot (names, DOB, addresses spoken in context); NER models are slow, so they only
  run on the few lines that look like they contain a name or address.
- **Checksums cut false positives.** Without Luhn, every 16-digit reference number would be masked as a card;
  with it, only real-looking card numbers are.
- **Two passes fix the order problem.** In real calls the agent often says the customer's name ("Mr Sharma")
  before or long after the customer introduces themselves. Pass 1 learns the names from the whole call; pass 2
  masks every occurrence.
- **Consistent surrogates keep the meaning.** The model still knows that the card mentioned on line 10 is the same
  as the one on line 400 (`<CARD_1>`), and that `<CUST_NAME_1>` is the customer, not the agent.
- **Product names are protected.** NER models often tag "Platinum", "Rewards" or "Horizon" as names or places;
  masking them would destroy exactly the product information the analytics need. The evaluation harness checks
  this with the `product_names_preserved` gate (≥ 98%).
- **Amounts and rates are not masked.** "rupees 2,500" and "3.5%" are needed for misquote detection; only digit
  runs of 9+ digits are treated as generic identifiers.

---

## Step 11: build turns and chunks

**File:** `callintel/preprocess/transcript.py` (stage `chunk`)

### What happens

1. `build_turns()` merges consecutive lines of the same speaker into one **turn** (up to 1,200 characters),
   keeping each original line as a `Segment` with its line number.
2. `chunk_turns()` groups turns into **chunks** of about 2,400 estimated tokens (hard maximum 3,000). It never
   splits a turn unless that single turn is larger than the maximum, and it repeats the last 2 turns of a chunk
   at the start of the next one (overlap), so a question and its answer are not separated.
3. `prepare_transcript()` returns a `PreparedTranscript`: kept lines (by original number), turns, chunks, PII
   counts, the surrogate mapping, noise statistics, stage timings and detector names.

Each turn renders for the LLM as `line|role: text`, for example:

```text
7|A: Thank you Mr <CUST_NAME_1>. The Platinum Rewards Card has an annual fee of rupees 2,500
8|C: that's too expensive for me
```

A 6,000-line call typically becomes a few hundred turns and 10-25 chunks; the running example fits in one chunk.

### Function by function

| Function / class | Where | What it does |
|---|---|---|
| `build_turns(lines, merge, max_chars)` | `transcript.py:170` | Appends a line to the previous turn if same speaker and the merged text stays under `max_chars`; otherwise starts a new turn. |
| `chunk_turns(turns, target_tokens, max_tokens, overlap_turns)` | `transcript.py:197` | Turn-aware packing with overlap, as described above. The condition `len(cur) > overlap_n` guarantees every chunk has at least one new turn (no infinite loop of overlap-only chunks). |
| `_split_long_turn(turn, max_tokens)` | `transcript.py:181` | Splits a giant turn (an agent reading terms for 5 minutes) at segment (line) boundaries. |
| `Segment` | `transcript.py:43` | One original line inside a turn: line number + text. |
| `Turn` + `line_start`, `line_end`, `text`, `render()` | `transcript.py:49-67` | A speaker turn; `render()` produces `first_line|A: text`. |
| `Chunk` + `line_start`, `line_end`, `render()` | `transcript.py:71-89` | Turns + token count + overlap count + triage score, signals and `selected` flag (filled in Step 13). |
| `PreparedTranscript` + `masked_token_count` | `transcript.py:93-109` | The result of preprocessing. `pii_mapping` holds original values and is **never** serialised or logged. |
| `estimate_tokens(text)` | `utils.py:64` | Fast estimate: characters / 4. Set `CALLINTEL_USE_TIKTOKEN=1` for exact counts (needs the tiktoken vocabulary download, which is not possible in air-gapped sites). |

### Why this way

- **Turn-aware chunking.** Splitting in the middle of a turn cuts sentences and separates questions from answers;
  the LLM then misreads intent and outcome. Turns are the natural unit of a conversation.
- **Overlap of 2 turns** gives each chunk the immediate context ("Agent: Shall I go ahead?" → "Customer: Yes")
  at a small cost; duplicate facts from the overlap are removed later (Step 16).
- **2,400-token chunks for an 8k-context local model.** The prompt also needs the system instructions (about
  1,500 tokens) and room for the answer (1,200 tokens). 2,400 + 1,500 + 1,200 fits in 8,192 with margin. For
  cloud models with large contexts, raise `target_tokens` to 6,000+ and you get fewer, larger calls.
- **Map-reduce over chunks instead of the whole call in one prompt.** A 6,000-line call can be 60k+ tokens:
  too big for local models, slow and expensive for cloud ones, and models lose details in the middle of very
  long contexts. Chunks keep each call small, run in parallel and keep every fact tied to its line.
- **chars/4 instead of a real tokenizer.** Chunk sizing only needs an estimate; a tokenizer would add a
  dependency and a download, for no practical gain.

---

## Step 12: the Supervisor starts

**Files:** `callintel/agents/supervisor.py` (`Supervisor.analyze`, line 68), `callintel/agents/context.py`,
`callintel/rag/transcript_index.py`

### What happens

Back in `process_file`, the masked transcript goes to `Supervisor.analyze(prepared, stopwatch)`. This is the
**agent graph** for one call:

```text
analyze()
 ├─ TranscriptIndex(prepared)        BM25 search over this call's turns + line lookups
 ├─ CallContext(prepared, index, EvidenceRegistry(index))
 ├─ triage              score_chunks                         (Step 13)  no LLM
 ├─ map                 MapAgent.extract × selected chunks   (Step 14)  LLM, parallel
 ├─ ground              _ingest_facts                        (Step 16)  no LLM
 ├─ normalize           _normalize                           (Step 17)  mostly no LLM
 ├─ call_type_hint                                           (Step 18)  no LLM
 ├─ specialists         sales, customer, product, compliance (Step 19)  LLM, parallel
 ├─ synthesis                                                (Step 20)  LLM
 └─ verify              ClaimCollector + CriticAgent + retries (Step 21) LLM, bounded loop
returns (outputs per agent, the CallContext, the GateReport)
```

### Function by function

| Function / class | Where | What it does |
|---|---|---|
| `Supervisor.analyze(prepared, sw)` | `supervisor.py:68` | Runs the graph above, timing each stage. If **every** map call fails, raises `MapStageFailed` (the file is retried); if only some fail, it continues and records `map_failures` (the output will be flagged for review). |
| `MapStageFailed` | `supervisor.py:52` | Error type for "no chunk could be extracted". |
| `TranscriptIndex.__init__(prepared)` | `transcript_index.py:16` | Sorted list of kept line numbers + a BM25 index over turn texts. |
| `TranscriptIndex.line(no)` | `transcript_index.py:23` | The kept line with that original number. |
| `TranscriptIndex.nearest_line(no, max_dist)` | `transcript_index.py:26` | That line, or the closest kept line within `max_dist` (a cited line may have been dropped as noise). |
| `TranscriptIndex.window(no, radius)` | `transcript_index.py:38` | The kept lines around a line number (used by grounding). |
| `TranscriptIndex.range(start, end, limit)` | `transcript_index.py:43` | Kept lines between two numbers (the `get_lines` tool). |
| `TranscriptIndex.search_lines(query, k)` | `transcript_index.py:51` | BM25 over turns, then the best-matching line inside each turn. This is **transcript RAG**. |
| `TranscriptIndex.fmt(line, max_chars)` | `transcript_index.py:64` | `L7 A: "text"`: the citable format shown to agents. |
| `CallContext` | `context.py:121` | Per-call shared state: prepared transcript, index, evidence registry, call-type hint, detected product ids and categories, statistics. |
| `CallContext.facts` | `context.py:131` | All registered facts. |
| `CallContext.facts_of(types, limit)` | `context.py:134` | Facts of the given types; if more than `limit`, takes them **round-robin across types** (so 100 quoted terms cannot crowd out the 2 objections), then re-sorts by line. |
| `EvidenceRegistry.__init__(tindex)` / `.add(fact)` | `context.py:65-70` | Holds the facts by id (`F1`, `F2`, ...). Its `resolve` method is the core of hallucination checking (Step 21). |

### Why this way

- **Plain asyncio instead of a graph framework (LangGraph, CrewAI).** The graph is a fixed fan-out/fan-in with one
  bounded loop. Explicit code is faster, easier to debug with a normal stack trace, and adds no dependency. Each
  node is its own agent class, so moving to LangGraph later is a wiring change, not a rewrite.
- **"Agentic" where it adds value, deterministic everywhere else.** The supervisor routes work, specialists can
  request tools, the critic verifies and the loop retries; but the order of stages is fixed, so latency and cost
  per call are predictable (important at 1,000 calls/day).
- **One context object per call** avoids passing 10 arguments to every agent, and keeps per-call state out of
  the long-lived agents (which are shared by concurrent calls).

---

## Step 13: triage

**File:** `callintel/agents/triage.py`

### What happens

`score_chunks()` scores every chunk with keyword lexicons and catalogue product mentions, **without an LLM**.
Chunks scoring below `triage.min_score` (2.0) skip the map step. The first and last chunk are always kept
(opening intent and closing outcome live there).

Signal weights: product mention 3.0, complaint 2.0, intent 2.0, outcome 2.0, sales 1.5, compliance 1.5,
numbers 0.5. Each signal counts at most 3 matches. In a 6,000-line call, typically 30-60% of chunks are pure
hold time, verification, small talk or statement read-outs, and are skipped.

### Function by function

| Function / constant | Where | What it does |
|---|---|---|
| `score_chunks(chunks, catalog, cfg)` | `triage.py:36` | For each chunk: count matches of each lexicon + `catalog.find_mentions`; `score = Σ weight × min(count, 3)`; set `chunk.signals`, `chunk.score`, `chunk.selected`. Returns the totals over the call (used for the call-type hint). |
| `LEXICONS` | `triage.py:17` | Regexes for `sales` (offer, pre-approved, eligible, upgrade, cashback, ...), `complaint` (charged, refund, dispute, escalate, fraud, ...), `compliance` (recorded, consent, interest rate, APR, OTP, ...), `intent` ("I want to", "calling about", ...), `outcome` ("go ahead and", "not interested", "call me back", ...), `numbers` (percentages, rupees, lakhs). |
| `WEIGHTS` | `triage.py:32` | As above. Numbers alone max out at 3 × 0.5 = 1.5 < 2.0, so a statement read-out cannot select a chunk by itself. |

(`call_type_hint` and `HINT_TO_CALL_TYPES` from the same file are explained in Step 18.)

### Why this way

- **Biggest cost saving after noise removal.** Every skipped chunk is one fewer LLM call (seconds of GPU time
  locally, money in the cloud).
- **Deterministic and explainable.** Each chunk's signals and score are stored, so "why was this part not
  analysed?" has an answer.
- **Safety nets against missed content:** first/last chunks are always analysed; the catalogue recall booster
  (Step 16) scans **all** lines, including skipped chunks, for product names; and specialists can still retrieve
  lines from skipped chunks with transcript search (Step 19).
- **Weights favour business signals.** The rule "bare 'go ahead' is filler" (only "go ahead **and/with**" counts)
  came from testing: polite phrases were selecting empty chunks.

---

## Step 14: the map agent extracts facts

**File:** `callintel/agents/mapper.py`, prompt `map_extract` in `config/prompts.yaml`

### What happens

For each selected chunk, `MapAgent.extract()` sends one LLM request through the gateway (Step 15), **all chunks
in parallel** (`asyncio.gather` in `Supervisor.analyze`). The model returns a list of **atomic facts**, each with:

| Field (in generation order) | Meaning |
|---|---|
| `type` | one of 14 fact types: product_mention, intent, need, objection, complaint, buying_signal, offer_made, outcome, next_step, quoted_term, compliance, risk, sentiment, identity_check |
| `label` | a taxonomy label for that type (for example `fees_too_high`), or `other` |
| `line` | the line number where it was said |
| `quote` | 3-15 words copied verbatim from that line |
| `value` | a short normalised description |
| `product` | the product as said ("plat card"), if any |
| `number` | the figure for a quoted term ("2,500", "3.5%"), if any |

Running example (the model's answer for the single chunk):

```json
{"facts": [
  {"type": "compliance", "label": "other", "line": 2, "quote": "this call is recorded", "value": "recording notice given", "product": null, "number": null},
  {"type": "intent", "label": "new_product_enquiry", "line": 3, "quote": "I got an SMS about the plat card", "value": "asks about card offer from SMS", "product": "plat card", "number": null},
  {"type": "identity_check", "label": "other", "line": 4, "quote": "may I know your date of birth", "value": "DOB verification", "product": null, "number": null},
  {"type": "quoted_term", "label": "other", "line": 7, "quote": "annual fee of rupees 2,500", "value": "annual fee", "product": "Platinum Rewards Card", "number": "2,500"},
  {"type": "objection", "label": "fees_too_high", "line": 8, "quote": "that's too expensive for me", "value": "fee too high", "product": null, "number": null},
  {"type": "offer_made", "label": "other", "line": 9, "quote": "I can waive the first year fee", "value": "first-year fee waiver", "product": null, "number": null},
  {"type": "quoted_term", "label": "other", "line": 9, "quote": "Interest is 3.5% per month", "value": "monthly interest rate", "product": null, "number": "3.5%"},
  {"type": "outcome", "label": "accepted", "line": 10, "quote": "okay, go ahead and apply", "value": "customer applies", "product": null, "number": null}
]}
```

### Function by function

| Function | Where | What it does |
|---|---|---|
| `MapAgent.__init__(deps)` | `mapper.py:18` | Pre-renders the static system prompt (rules, worked example, fact-type guide, `max_facts`). |
| `MapAgent.extract(chunk, total)` | `mapper.py:25` | Builds the user message (chunk number, line range, rendered turns), calls `gateway.structured("map", messages, MapOutput)`, returns at most `max_facts_per_chunk` (15) facts. |

### Why this way

- **One multi-aspect extraction per chunk, not one pass per specialist.** Four specialists each reading the whole
  transcript would cost 4× the tokens. Instead the transcript is read **once**, into a compact list of typed,
  quoted facts, and the specialists reason over that list (usually a few dozen to a couple of hundred short
  fact lines instead of 50,000+ tokens of transcript).
- **Field order is generation order.** Grammar-constrained decoders write JSON keys in schema order. Deciding the
  type and label first, then the line, then copying the quote, and only then writing a summary, makes the model
  commit to evidence before it paraphrases, which reduces made-up quotes.
- **Verbatim quote + line number** make every fact checkable by code (Step 16).
- **Cap of 15 facts per chunk.** On a local GPU, output tokens dominate latency; 15 well-chosen facts per
  2,400-token chunk keep each call to a few seconds. The cap is enforced inside the decoder for Ollama (see
  `maxItems` in Step 15) and by slicing for providers that ignore it.
- **The prompt has a worked example and a recency reminder** at the end (prompt `map_extract` v3.5). Small models
  follow a concrete example better than abstract rules, and repeat-at-the-end counters "lost in the middle".

---

## Step 15: inside the LLM gateway

**Files:** `callintel/llm/gateway.py`, `callintel/llm/egress.py`, `callintel/llm/schema_utils.py`,
`callintel/redis_layer/ratelimit.py`, `callintel/redis_layer/store.py` (`JsonCache`, `Metrics`)

### What happens

**Every** LLM request in the system (map, specialists, synthesis, critic, normaliser, evaluation judge) goes
through `LLMGateway.structured(role, messages, schema_model)`. It applies, in order:

```text
1. resolve the role → provider + model (and unify num_ctx per model)
2. EGRESS GUARD     re-scan the prompt for PII; block (or redact) before it can leave
3. strict JSON schema from the pydantic model (provider-specific)
4. CACHE LOOKUP     key = hash(provider, model, params, schema, messages) → return stored answer if present
5. CONCURRENCY      per-provider semaphore (Ollama: 1, OpenAI: 48, vLLM: 64)
6. RATE LIMIT       shared Redis token buckets: requests/min and tokens/min
7. CALL PROVIDER    with retries + exponential backoff + jitter on transient errors
8. VALIDATE         parse JSON → pydantic; if cut off, SALVAGE complete items; else one REPAIR re-ask
9. CACHE STORE, usage accounting, daily metrics
```

### Function by function: the gateway

| Function | Where | What it does |
|---|---|---|
| `LLMGateway.__init__(settings, redis)` | `gateway.py:331` | Creates the cache, metrics, rate limiter and egress guard. Computes, per (provider, model), the **largest** `num_ctx` of any role that uses it. |
| `LLMGateway.structured(role, messages, schema_model, max_tokens, use_cache)` | `gateway.py:363` | The 9 steps above. Returns `LLMResult(data, model, cached, tokens_in, tokens_out, latency_ms)`. |
| `LLMGateway._call_with_retries(prov, rc, msgs, schema, name, max_tokens, attempts=4)` | `gateway.py:440` | Retries only `TransientLLMError` (timeouts, connection errors, HTTP 429/5xx) up to 4 times: waits 1 s, 2 s, 4 s ... (max 30 s) plus up to 0.5 s random jitter. Other errors fail at once. |
| `LLMGateway.embed(texts, role)` | `gateway.py:454` | Embeddings with a Redis cache per text (`ci:emb:<model>:<hash>`), batches of 64, egress check for external providers, returns L2-normalised vectors (so a dot product is the cosine similarity). |
| `LLMGateway.provider(name)` | `gateway.py:348` | Creates the provider client on first use (`OllamaProvider`, `OpenAIProvider`, `MockProvider`) and reuses it. |
| `LLMGateway._sem(name)` | `gateway.py:354` | One `asyncio.Semaphore(max_concurrency)` per provider. |
| `LLMGateway.role_model(role)` | `gateway.py:359` | `"provider:model"` string for the output's `processing.models`. |
| `LLMGateway.ping_role(role)` | `gateway.py:478` | Is this role's provider reachable and its model available? Used by `doctor` and dense-retrieval auto-detection. |
| `LLMGateway.close()` | `gateway.py:485` | Closes all provider clients. |
| `LLMResult` | `gateway.py:97` | Typed result of a structured call. |
| `LLMError`, `TransientLLMError` | `gateway.py:44-49` | Permanent vs retryable errors. |
| `_extract_json(text)` | `gateway.py:110` | Removes `<think>...</think>` blocks and ```` ```json ```` fences, and trims to the outermost `{...}`. Makes parsing robust to chatty models. |
| `salvage_truncated_json(text)` | `gateway.py:121` | If the answer was cut off by the token limit (`{"facts":[{..},{..},{"type":"x","lab`), walks the JSON with a bracket stack, cuts after the last **complete** list item and closes the open brackets: `{"facts":[{..},{..}]}`. |

### Function by function: providers

| Function | Where | What it does |
|---|---|---|
| `OllamaProvider.__init__(cfg)` | `gateway.py:154` | `httpx.AsyncClient` to `base_url` with the configured timeout. |
| `OllamaProvider.chat(rc, messages, schema, schema_name, max_tokens)` | `gateway.py:160` | Native `POST /api/chat` with `format = <JSON schema>`, `options = {temperature, num_ctx, num_predict, seed}`, `keep_alive: 30m`, and `think: false`. If the model rejects `think`, remembers that and retries without it. Maps HTTP 429/5xx and transport errors to `TransientLLMError`. Returns content + prompt and output token counts. |
| `OllamaProvider.embed(model, texts)` | `gateway.py:185` | `POST /api/embed`. |
| `OllamaProvider.ping(model)` | `gateway.py:190` | `GET /api/tags`; checks the model is pulled (and says `ollama pull <model>` if not). |
| `OllamaProvider.close()` | `gateway.py:201` | Closes the HTTP client. |
| `OpenAIProvider.__init__(cfg)` | `gateway.py:208` | `AsyncOpenAI` client (also used for vLLM, LiteLLM, LM Studio via `base_url`); SDK retries off (the gateway retries). |
| `OpenAIProvider.chat(...)` | `gateway.py:218` | `response_format = {"type": "json_schema", "json_schema": {..., "strict": true}}`; `max_completion_tokens` for OpenAI, `max_tokens` for compatible servers; `reasoning_effort` if set; `seed` for non-OpenAI servers. Maps timeouts/connection/rate-limit/5xx to transient; raises on a model refusal. |
| `OpenAIProvider.embed` / `ping` / `close` | `gateway.py:247-261` | Embeddings API; `models.retrieve`; close. |
| `MockProvider.chat(...)` | `gateway.py:287` | Offline deterministic stand-in for tests: for the map schema, keyword rules over the chunk lines produce a few grounded facts; for other schemas, `minimal_instance()` (the smallest valid answer: "unclear", empty lists). |
| `MockProvider.embed` / `ping` / `close` | `gateway.py:311-322` | Pseudo-random vectors seeded by the text hash; always "ok". |
| `minimal_instance(schema)` | `schema_utils.py:72` | Smallest valid value for a strict schema; prefers `unclear`/`other`/`not_applicable`/`neutral` for enums. |

### Function by function: schema, egress, rate limit, cache

| Function | Where | What it does |
|---|---|---|
| `strict_schema(model, keep_limits)` | `schema_utils.py:63` | Pydantic model → a fully dereferenced JSON schema where every object has all properties `required` and `additionalProperties: false` (OpenAI strict mode rules), unsupported keywords removed, `const` turned into a one-value `enum`. With `keep_limits=True` (Ollama) it keeps `minItems`/`maxItems`. |
| `_strict(node, defs, keep_limits)` | `schema_utils.py:26` | The recursive transformation; inlines `$ref` definitions. |
| `_cached(model, keep_limits)` | `schema_utils.py:53` | Caches the schema text and its hash per model; keeps the declared field order. |
| `schema_hash(model)` | `schema_utils.py:68` | 16-hex hash of the schema; part of the cache key, so changing a schema invalidates old cached answers. |
| `EgressGuard.__init__(settings)` | `egress.py:29` | Reads the guard config, the cloud kill-switch, the audit directory, and builds a `RegexRecognizer`. |
| `EgressGuard.check(messages, trust_zone, call_id, role, model)` | `egress.py:43` | 1) If the provider is **external** and `allow_cloud_llm` is false → `EgressBlocked`. 2) If the guard applies (external providers by default, or all), scans every message with `find_pii`. Any finding → writes an audit record with **types and counts only** (never the values), then blocks (default) or redacts to `[REDACTED_CARD]`. 3) Optionally logs the full masked payload (evaluation runs only). |
| `EgressGuard._audit(name, record)` | `egress.py:37` | Appends one JSON line to `data/audit/<name>_<day>_<pid>.jsonl` (thread lock, one file per process). |
| `EgressBlocked` | `egress.py:24` | Raised on a block; becomes a `NonRetryableError` in the engine (straight to the DLQ). |
| `RateLimiter.__init__(r, key_fn)` | `ratelimit.py:58` | Holds the Redis connection, key builder and a fallback local bucket map. |
| `RateLimiter.acquire(bucket, per_minute, cost)` | `ratelimit.py:65` | Token bucket with capacity = per-minute limit and refill = limit / 60 per second. If the bucket has enough tokens, take them and return; else sleep the computed wait (max 30 s) and try again. No limit configured → returns immediately. |
| `RateLimiter._take(bucket, cap, rate, cost)` | `ratelimit.py:79` | Runs the Lua script `_LUA` atomically in Redis (refill by elapsed time, take or compute wait, store, set expiry). If Lua is unavailable, uses a per-process `_LocalBucket`. |
| `_LocalBucket.take(cost)` | `ratelimit.py:44` | The same algorithm in Python, for one process. |
| `JsonCache.get(key)` / `.set(key, value, ttl_s)` | `store.py:22-27` | JSON values in Redis; the LLM cache lives 7 days. |
| `Metrics.incr(**counters)` | `store.py:105` | `HINCRBYFLOAT` on `ci:stats:<today>` for llm_calls, tokens, cache hits, files done/failed/needs_review, egress blocks; keeps 90 days. |
| `Metrics.get(day)` | `store.py:114` | Reads one day's counters (for `status`). |

### Why this way

- **One door for all LLM traffic.** Privacy, caching, rate limiting, retries and accounting are implemented once,
  so no agent can forget them, and switching provider is a config change.
- **Native Ollama API instead of its OpenAI-compatible endpoint.** The compatible endpoint cannot set `num_ctx`,
  so long chunks would be silently truncated at the default context (2k-4k tokens). The native API also accepts a
  JSON schema in `format` and `think: false`.
- **One `num_ctx` per model.** Ollama reloads the model whenever `num_ctx` changes. If map used 8192 and the critic
  4096, every switch between them would reload the model (seconds each) and could run out of GPU memory.
- **Constrained decoding (`format` / strict `json_schema`).** The model physically cannot produce an invalid enum
  value or broken JSON in most cases. This is the first and cheapest hallucination layer, and the reason outputs
  are consistent across calls.
- **`temperature 0` + fixed `seed`** make answers as repeatable as the backend allows; combined with the cache,
  re-running the same call returns the same JSON.
- **Content-addressed cache.** The key includes the model, all generation parameters, the schema hash and the exact
  messages. Any change (new prompt version, new model) is a different key, so you never get a stale answer. A
  re-run after a crash, or an evaluation re-run, costs nothing for calls already answered.
- **Salvage before repair.** When a long answer is cut off at the token limit, the first 12 facts are still good.
  Keeping them is free; a re-ask costs another full generation. Only if salvage fails is the model re-asked
  (once), with a specific instruction ("fewer items" for truncation, the validation error otherwise).
- **Egress guard as the last line of defence.** Masking (Step 10) should catch everything; the guard re-checks
  with high-precision detectors right before sending. A masking bug becomes a failed job, not a data leak, and the
  audit log never contains the PII itself.
- **Distributed rate limiting in Lua.** With 2-8 processes on several machines, each process limiting itself would
  overspend the shared OpenAI quota. A Lua script runs atomically inside Redis, so all workers share one bucket.
- **Exponential backoff with jitter** prevents all workers from retrying at the same instant after an outage.

---

## Step 16: ground, check and de-duplicate the facts

**Files:** `callintel/agents/supervisor.py` (`_ingest_facts`, line 103), `callintel/verify/grounding.py`,
`callintel/agents/context.py` (`FactRec`)

### What happens

The model's facts are **claims**, not truth. `_ingest_facts()` checks each one with plain code before anything
else may use it:

1. **Quote length:** a quote of 1-2 words ("okay", "right") cannot prove an intent, objection or complaint, so it
   is dropped (allowed only for outcome, buying_signal, next_step, identity_check).
2. **Grounding:** `ground_quote()` looks for the quote in the cited line ±2 lines, then anywhere in the chunk.
   If the best match scores below `grounding_min_score` (0.80), the fact is dropped as made up. If it is found on a
   nearby line, the line number is **corrected** to the line where it really is.
3. **Speaker rule:** the speaker of every line is known (Step 8). An `objection`, `intent`, `need`, `complaint`,
   `buying_signal` or `sentiment` must be said by the **customer**; an `offer_made` or `identity_check` by the
   **agent**. Violations are dropped and counted (`facts_dropped_wrong_speaker`).
4. **Label check:** a label not valid for the fact type becomes `other`.
5. **Number check:** for a `quoted_term`, every figure in `number` must appear in the line text
   (`number_verified`).
6. **Recall booster:** every kept line (including lines of chunks skipped by triage) is scanned for exact
   catalogue aliases; up to 2 `product_mention` facts per product are added with `source="catalog_scan"`.
7. **De-duplication** (the chunk overlap produces duplicates): two keys, (type, line, first 40 characters of
   the quote) and (type, label, product, first 60 characters of the value); the LLM version wins over the
   catalogue version on the same line.
8. **IDs:** surviving facts are numbered `F1, F2, ...` in line order and registered in the `EvidenceRegistry`.

Running example after this step:

```text
F1  L2  A compliance/other:        "this call is recorded"
F2  L3  C intent/new_product_enquiry: "I got an SMS about the plat card"   [product said as: plat card]
F3  L3  C product_mention/other:   "...about the plat card"                (catalog_scan)
F4  L4  A identity_check/other:    "may I know your date of birth"
F5  L7  A quoted_term/other:       "annual fee of rupees 2,500"             [number=2,500, verified]
F6  L7  A product_mention/other:   "...The Platinum Rewards Card has an..." (catalog_scan)
F7  L8  C objection/fees_too_high: "that's too expensive for me"
F8  L9  A offer_made/other:        "I can waive the first year fee"
F9  L9  A quoted_term/other:       "Interest is 3.5% per month"             [number=3.5%, verified]
F10 L10 C outcome/accepted:        "okay, go ahead and apply"
```

If the model had tagged line 9 (the agent) as an `objection`, rule 3 would have dropped it.

### Function by function

| Function | Where | What it does |
|---|---|---|
| `Supervisor._ingest_facts(ctx, selected, results)` | `supervisor.py:103` | The 8 steps above; records `facts_raw`, `facts_dropped_ungrounded`, `facts_kept`. |
| `SPEAKER_RULES` | `supervisor.py:45` | Fact type → allowed speakers. |
| `_n(s)` | `supervisor.py:39` | Lower-case, collapse whitespace, strip punctuation (for de-duplication keys). |
| `ground_quote(line_no, quote, tindex, fallback_lines)` | `grounding.py:35` | Best (line, score) in the ±2 window; if below 0.9, also tries every line of the chunk. |
| `quote_score(quote, text)` | `grounding.py:24` | 1.0 if the normalised quote is a substring of the line; else `rapidfuzz.partial_ratio` (best matching substring), or `ratio` if the quote is longer than the line. 0-1. |
| `_norm(s)` | `grounding.py:20` | Normalisation used by `quote_score`. |
| `number_supported(number, texts)` | `grounding.py:64` | True if every figure in `number` appears among the figures of the texts. Spoken numbers ("two point five percent") fall back to fuzzy text matching. |
| `numbers_in(text)` | `grounding.py:54` | Extracts figures, ignoring surrogate tokens, removing thousands separators and trailing zeros ("2,500" → "2500", "3.50" → "3.5"). |
| `FactRec` + `fmt()` | `context.py:40-61` | One grounded fact; `fmt()` renders it for the specialist prompts as shown above. |
| `Taxonomy.labels_for(type)` | `schemas.py:43` | Valid labels for the label check. |

### Why this way

- **This is the main hallucination filter, and it costs no LLM call.** A fact without a verbatim quote on a real
  line never reaches the specialists, so they cannot build conclusions on it.
- **Fuzzy, not exact, matching** (threshold 0.80): small models change punctuation or a word ("I'm" vs "I am");
  exact matching would throw away good facts. 0.80 tolerates that while rejecting invented sentences.
- **Line correction instead of rejection:** models are often one line off when turns are merged; the quote is
  what matters.
- **Speaker rules use what we know for certain.** The most common labelling error of small models is calling the
  agent's own disclosure ("the APR is 42% per annum") a customer objection. The speaker is known from parsing, so
  this check is exact.
- **Catalogue recall booster:** products are the most important entity for the business, and exact alias matching
  is 100% precise, so it is used to make sure no named product is missed, even in chunks the LLM never saw.

---

## Step 17: normalise product names

**Files:** `callintel/agents/supervisor.py` (`_normalize`, line 165), `callintel/normalize/normalizer.py`

### What happens

Customers and agents name products in many ways: "plat card", "the platinum one", "platnum crd", "pre approved
loan". `_normalize()` collects every fact's `product` text with its line as context, and
`ProductNormalizer.normalize_many()` maps each unique mention to a canonical product id from `products.yaml`.

The normaliser tries the cheapest method first and stops at the first confident answer:

```text
mention ──► norm_mention()  "The Plat-Card!" → "plat card"
   │
   ├─ empty / only "it", "that", "the offer" ...............► unresolved
   ├─ T0 EXACT     alias dictionary from products.yaml .........► product id   (microseconds)
   ├─ no product head noun ("late fee", "debit transaction") ...► unresolved   (guard)
   ├─ this process already resolved it (_local) ................► same answer
   ├─ another task is resolving it right now (_inflight) .......► wait and share its answer
   ├─ T1 LEARNED   Redis hash ci:alias (answers learned earlier by T2/T3, shared by all workers)
   ├─ T2 FUZZY     edit-distance score ≥ 0.86 and ≥ 0.05 ahead of the runner-up → accept, LEARN
   ├─ T3 LLM       top-5 candidates (fuzzy + hybrid retrieval), model must pick one id or NONE
   │               (enum-constrained) → accept, LEARN
   └─ otherwise    unresolved + sent to ci:stream:norm_review for a human to add the alias
```

Then the supervisor:

- sets `product_id` (and the `NormResult`) on each fact,
- builds `ctx.product_ids` in order of first mention,
- removes a **generic** id (for example `GEN_CREDIT_CARD`) when a specific product of the same category is present,
- sets `ctx.categories` (for example `{credit_card}`), which decides which compliance rules apply (Step 18).

Running example: "plat card" and "Platinum Rewards Card" are both exact aliases (T0) of `CC_PLATINUM_REWARDS`.
A typo such as "platnum crd" would be resolved by T2 fuzzy matching and then stored in `ci:alias`, so the next
call anywhere in the fleet resolves it at T1 in under a millisecond.

### Function by function

| Function | Where | What it does |
|---|---|---|
| `Supervisor._normalize(ctx)` | `supervisor.py:165` | Collects (mention, line text), calls `normalize_many`, writes ids back to facts, builds `product_ids` and `categories`, removes redundant generic ids. |
| `ProductNormalizer.__init__(settings, catalog, redis, gateway, prompts, schemas)` | `normalizer.py:51` | Builds the fuzzy choice list (every normalised alias with its owner), a `HybridIndex` over product documents, the per-process cache, the in-flight map, the alias-token set for the guard, and the review stream. |
| `ProductNormalizer.normalize_many(items)` | `normalizer.py:176` | De-duplicates mentions by normal form and resolves them concurrently. Returns `{normal form: NormResult}`. |
| `ProductNormalizer.normalize(mention, context)` | `normalizer.py:105` | The fast checks (generic words, T0, guard, local cache, in-flight sharing), then `_resolve`. |
| `ProductNormalizer._resolve(n, mention, context)` | `normalizer.py:136` | T1 (Redis), T2 (fuzzy accept rule), T3 (LLM among candidates), else review stream. A T3 failure is logged and never fails the call. |
| `ProductNormalizer._looks_like_product(n)` | `normalizer.py:75` | True if the mention contains a product head noun (card, loan, account, deposit, FD, insurance, SIP, fund, mortgage, EMI, ...) or a distinctive token of a known alias (platinum, cashback, ...). |
| `ProductNormalizer._fuzzy(n, limit)` | `normalizer.py:83` | `rapidfuzz` top matches by `ratio`, re-scored with `max(ratio, token_sort_ratio)` (word order insensitive), best score per product, generic products −0.03. |
| `ProductNormalizer.candidates(n, k)` | `normalizer.py:94` | Top-k product ids from fuzzy scores plus hybrid (BM25 + dense) retrieval over product descriptions, for T3. |
| `ProductNormalizer._learn(n, res)` | `normalizer.py:173` | `HSET ci:alias <mention> {product_id, confidence, tier, learned_at}`. |
| `ProductNormalizer.enable_dense(embed_fn)` | `normalizer.py:79` | Turns on embeddings for candidate retrieval. |
| `NormResult` + `as_json()` | `normalizer.py:40-47` | mention, product id (or None), confidence, tier (`exact`, `learned`, `fuzzy`, `llm`, `unresolved`). |
| `HybridIndex.search(query, k, where)` | `retrieval.py:89` | BM25 ranks (and dense cosine ranks if enabled), fused with Reciprocal Rank Fusion: `score = Σ 1/(60 + rank)`; optional filter; returns the top k as `Hit` objects (`Hit` = document + fused score, `retrieval.py:69`). |
| `BM25.search(query, k)` | `retrieval.py:48` | Okapi BM25 scoring over the postings lists. |
| `LLMSchemas.normalizer_output(candidates)` | `schemas.py:156` | The enum schema for T3: candidate ids + `NONE`. |

### Why this way

- **Cheapest tier first.** In steady state almost every mention is resolved by T0 or T1 in microseconds; the LLM
  runs only for genuinely new phrasings, once per new phrase **across the whole fleet**, because every T2/T3 answer
  is written back to Redis. The system gets faster and cheaper as it sees more calls ("self-learning").
- **The LLM can only choose from the catalogue.** At T3 the output schema is an enum of the top-5 candidate ids
  plus `NONE`; the model cannot invent a product id. `NONE` gives it a legal way to say "none of these".
- **Accept rule with a margin.** A fuzzy score of 0.86 is accepted only if the runner-up is at least 0.05 behind;
  if two products are almost equally similar ("platinum card" vs "platinum travel card"), the decision goes to T3
  with context instead of guessing.
- **Head-noun guard.** Without it, "late fee" or "debit transaction" can fuzzy-match a product ("Debit Card"); the
  guard rejects mentions that do not name a product type.
- **In-flight sharing.** If ten facts in one call mention "platnum crd", only one resolution runs; the others await
  the same future.
- **Unresolved mentions are queued for curation**, not silently dropped, so the catalogue improves over time.
- **RRF instead of weighted score blending.** BM25 and cosine scores have different scales; rank fusion needs no
  tuning and is robust.

---

## Step 18: the call-type hint and the rule list

**Files:** `callintel/agents/triage.py`, `callintel/agents/specialists.py` (`ComplianceAgent.applicable_rules`),
`callintel/rag/knowledge_base.py`

### What happens

1. `call_type_hint(fact_type_counts, triage_signals)` gives a first guess of the call type from the grounded facts:
   offers/buying signals → `sales`; complaints → `complaint`; both → `mixed`; only intents/needs → `service`;
   nothing → `enquiry`. In the example, F8 (`offer_made`) makes it `sales`.
2. At the start of the specialist stage, `ComplianceAgent.applicable_rules(ctx)` maps the hint to call types
   (`HINT_TO_CALL_TYPES`: `sales` → {sales}; `complaint` → {complaint, service}; ...) and asks the knowledge base
   for the rules that apply to those call types **and** the detected product categories. The list is stored in
   `ctx.stats["applicable_rules"]` so the assembler can report "not assessed" for any rule the agent skipped.

For the example (sales call, credit card): `REC_DISCLOSURE` (all calls), `ID_VERIFICATION`,
`APR_FEE_DISCLOSURE` (sales of credit products), `TNC_CONSENT`, and any other rule whose `applies_to_*` lists
match.

### Function by function

| Function | Where | What it does |
|---|---|---|
| `call_type_hint(fact_types, signals)` | `triage.py:50` | The rule above (facts count 1 each, keyword signals 0.2 each). |
| `HINT_TO_CALL_TYPES` | `triage.py:64` | Hint → set of call types used for rule selection. |
| `ComplianceAgent.applicable_rules(ctx)` | `specialists.py:125` | Rules for the hint's call types and the call's product categories. |
| `KnowledgeBase.rules_for(call_types, categories)` | `knowledge_base.py:78` | Filters rules with `Rule.applies`. |
| `Rule.applies(call_types, categories)` | `knowledge_base.py:34` | True if the rule lists `all` or overlaps on **both** call type and category. |
| `KnowledgeBase.rule(rule_id)` | `knowledge_base.py:75` | Look up one rule (title, description, search phrases, severity). |

### Why this way

- **Rules are data, not code.** Compliance teams change `compliance_rules.yaml` (scope, phrases, severity); no
  Python change and no prompt change is needed.
- **Only relevant rules are checked.** A service call about a statement should not be judged on APR disclosure.
  Fewer rules also means a shorter compliance prompt and fewer false "missed" flags.
- **The hint is computed from grounded facts**, not from a separate LLM classification call, so it is free and
  explainable. The final `call_type` is still decided by the synthesis agent (Step 20) with all findings in view.

---

## Step 19: the specialist agents

**Files:** `callintel/agents/specialists.py`, `callintel/agents/base.py`, `callintel/agents/supervisor.py`
(`_run_specialists`, line 187), `callintel/rag/knowledge_base.py`, `callintel/rag/transcript_index.py`

### What happens

Four specialists run **in parallel**, each on its own slice of the grounded facts, with retrieved knowledge (RAG):

| Agent | Facts it sees (`TYPES`) | Extra context (RAG) | Output (enum-constrained) |
|---|---|---|---|
| **Sales** | offer_made, buying_signal, objection, outcome, next_step, product_mention, quoted_term | 2 sales-playbook passages retrieved for the objection labels; up to 4 **outcome candidate lines** (customer lines near the end matching "go ahead and", "not interested", "call me back", ...); product list | pitch made + evidence, sales outcome (`converted`, `interested_follow_up`, `declined`, `no_pitch`, `unclear`) + evidence, objections (category, detail, handled), buying signals, cross-sell, next steps |
| **Customer** | intent, need, complaint, sentiment, objection, outcome, next_step, buying_signal | up to 4 **intent candidate lines** (customer lines matching "I want to", "calling about", "I got an SMS", ...) | primary intent + evidence, secondary intents, needs, pain points, sentiment start/end, churn risk, vulnerability indicators |
| **Product** | product_mention, offer_made, quoted_term, buying_signal, objection, outcome, complaint (product-linked) | reference attributes of the detected products (fees, rates) | per product: role, customer interest, outcome, key points, evidence |
| **Compliance** | compliance, risk, identity_check, quoted_term, offer_made, outcome | each applicable rule with **2 candidate lines** retrieved by the rule's search phrases; product reference fees/rates | per rule: met / missed / not_applicable / unclear + evidence; risk flags; identity verification; **misquoted terms** (stated vs reference) |

Evidence is passed as fact lines (`F7 L8 C objection/fees_too_high: "that's too expensive for me"`) and candidate
lines (`L10 C: "okay, go ahead and apply"`). Agents must cite these **ids** (`F7`, `L10`) as evidence, never write
their own quotes.

Each agent **short-circuits without an LLM call** when it has nothing to work on: no offers/objections/buying
signals → sales returns `no_pitch`; no products → product returns an empty list; and so on.

**Bounded tool loop:** an agent may answer with `tool_requests` instead of (or alongside) its findings:
`search_transcript` (BM25 over this call), `get_lines` ("120-130"), `search_kb` (playbook). The runtime runs at
most 2 requests, appends the results as new citable `L<n>` lines, and asks once more with "lookups are done".
`max_tool_rounds = 1`, so at most one extra LLM call per agent.

Running example: the compliance agent sees `APR_FEE_DISCLOSURE` with the product reference
`apr_monthly: 3.6%` and fact F9 "Interest is 3.5% per month", and reports a misquoted term: stated `3.5%`,
reference `3.6%`, evidence `F9`. The sales agent reports `sales_outcome = converted` citing F10, an objection
`fees_too_high` (handled: `yes`, by the waiver in F8) citing F7.

### Function by function

| Function | Where | What it does |
|---|---|---|
| `Supervisor._run_specialists(ctx)` | `supervisor.py:187` | Stores the applicable rules, runs the 4 agents with `asyncio.gather(return_exceptions=True)`. A failed agent is replaced by its empty answer and recorded in `agent_failures` (the call will be flagged for review). |
| `BaseAgent.call(ctx, schema, user_vars, feedback)` | `base.py:31` | Adds reviewer feedback to the evidence if given (Step 21), calls the gateway with the agent's role (`reduce`), runs the bounded tool loop, clears `tool_requests` in the result. |
| `BaseAgent.run_tools(ctx, requests)` | `base.py:53` | Executes `search_transcript` (3 lines), `get_lines` (max 12-line range, 8 lines), `search_kb` (2 playbook passages, labelled "reference, not evidence"). A bad argument is logged, never raised. Results are de-duplicated. |
| `fmt_facts(facts)` | `base.py:72` | Facts → text lines (or "(no facts)"). |
| `SalesAgent.run(ctx, feedback)` / `.empty()` | `specialists.py:70`, `:65` | Short-circuit, playbook search, outcome candidates, call. |
| `CustomerAgent.run` / `.empty()` | `specialists.py:95`, `:89` | Intent candidates; short-circuit only if no facts **and** no candidates. |
| `ProductAgent.run` | `specialists.py:109` | Product list + reference attributes + product-linked facts. |
| `ComplianceAgent.run` / `.empty()` | `specialists.py:133`, `:129` | Per-rule candidate lines (transcript RAG) + reference attributes + facts. |
| `candidate_lines(ctx, rx, speaker, k, from_end)` | `specialists.py:30` | First (or last) k lines of a speaker matching a cue regex, in line order. |
| `with_candidates(evidence, title, cands)` | `specialists.py:45` | Appends a titled candidate block to the evidence text. |
| `_products_line(ctx, catalog)` | `specialists.py:49` | `CC_PLATINUM_REWARDS = Platinum Rewards Credit Card (credit_card); said as: 'plat card', ...; facts: F2,F3,F6`. |
| `INTENT_CUES`, `OUTCOME_CUES` | `specialists.py:18-27` | Cue phrases for candidate retrieval. |
| `KnowledgeBase.search(query, collection, k)` | `knowledge_base.py:81` | Hybrid search restricted to one collection. |
| `KnowledgeBase.product_reference(product_ids)` | `knowledge_base.py:86` | Reference attributes of the given products as text. |
| `TranscriptIndex.search_lines` / `range` / `fmt` | `transcript_index.py:43-66` | Used by candidate retrieval and the tools. |

### Why this way

- **Specialists instead of one giant prompt.** Each agent has a short, focused prompt with its own output schema;
  small local models are far more accurate on narrow tasks. They run in parallel, so total latency is roughly that
  of the slowest one.
- **Evidence by reference (`F7`, `L10`) instead of free quotes.** An id either exists in the registry or it does
  not; a made-up citation is detected mechanically (Step 21). Free-text quotes would need fuzzy matching again
  and could be paraphrased.
- **RAG with three sources, each for a reason:** the **playbook** tells the sales agent what good objection
  handling looks like; the **product reference** lets the compliance agent detect misquoted rates and fees
  (impossible from the transcript alone); **transcript retrieval** gives the headline judgements (intent, outcome,
  rule met/missed) a second chance to see the decisive line even if the map step missed it.
- **Short-circuits** save one LLM call per empty agent; on a pure service call, the sales agent costs nothing.
- **Bounded tool use** gives agents a real agentic capability (look something up when evidence is thin) without
  unbounded loops: latency and cost stay predictable.
- **`facts_of` caps and balances input** (120 facts, round-robin by type) so a fact-heavy call cannot overflow the
  context window.

---

## Step 20: the synthesis agent

**Files:** `callintel/agents/specialists.py` (`SynthesisAgent`, `findings_digest`), `callintel/agents/supervisor.py`

### What happens

`findings_digest()` compresses the four specialist outputs into a few lines, for example:

```text
SALES: pitch_made=True; outcome=converted (F10); objections: fees_too_high (F7); next_steps: none
CUSTOMER: primary_intent=new_product_enquiry (F2); needs: none; pain: none; sentiment neutral->positive; churn=low
PRODUCTS: CC_PLATINUM_REWARDS role=pitched interest=high outcome=accepted
COMPLIANCE: rules: REC_DISCLOSURE=met; APR_FEE_DISCLOSURE=met; risks: none; identity=performed
```

`SynthesisAgent.run()` receives this digest plus up to 40 key facts and returns the call-level view: final
`call_type`, `resolution_status` + evidence, a short `summary` + evidence, agent scores 1-5 (empathy, clarity,
product knowledge, compliance adherence) and notes. If synthesis fails, `_synthesis_fallback()` returns a neutral
answer (call type from the hint, `unclear`, scores 3) so the call still produces a valid JSON.

### Function by function

| Function | Where | What it does |
|---|---|---|
| `findings_digest(outputs)` | `specialists.py:160` | Text digest of the specialist outputs. |
| `SynthesisAgent.run(ctx, findings, feedback)` | `specialists.py:154` | One reduce-role call with the digest and facts. |
| `Supervisor._run_synthesis(ctx, outputs, feedback)` | `supervisor.py:208` | Calls the synthesis agent; on error logs it, records `agent_failures`, returns the fallback. |
| `Supervisor._synthesis_fallback(ctx)` | `supervisor.py:201` | Neutral, schema-valid synthesis output. |

### Why this way

- **Synthesis after the specialists**, so the summary and call type are consistent with the detailed findings
  instead of a separate, possibly contradictory, reading of the call.
- **A digest instead of the raw outputs** keeps the prompt short; the facts are included so the summary can cite
  real evidence ids.
- **Graceful degradation:** a failure in a non-critical agent produces a flagged output, not a lost call.

---

## Step 21: the verification loop

**Files:** `callintel/agents/supervisor.py` (`_verify`, line 218), `callintel/verify/gate.py`,
`callintel/agents/critic.py`, `callintel/agents/context.py` (`EvidenceRegistry`), `callintel/verify/grounding.py`

### What happens

This is the **hallucination gate**. Every statement the agents made is turned into a `ClaimRec` and checked,
cheapest check first:

```text
for attempt in 0..max_retries (2):
    claims = ClaimCollector.collect(outputs)
        each claim: resolve its evidence ids → real lines?           (evidence-by-reference)
                    figures in misquote claims present in the cited lines? (numeric fidelity)
                    "missed" rule really missed? identity "not performed" really? (contradiction probes)
    critic: high-risk claims that passed, not judged before (max 30), 10 per LLM call
                    verdict supported / partially_supported / unsupported
    failing = high-risk claims whose status is not "ok"
    if none failing, or last attempt: stop
    re-run ONLY the agents that own failing claims, with the reasons as feedback
    re-run synthesis
```

Claim statuses:

| Status | Meaning | Set by |
|---|---|---|
| `ok` | evidence ids resolve to real lines (and figures match) | `ClaimCollector._add` |
| `ungrounded` | no evidence cited, all ids invalid, or a stated figure not in the cited lines | `ClaimCollector._add` |
| `contradicted` | the agent said a rule was missed / identity not verified, but the transcript shows otherwise | `_probe_missed`, identity probe |
| `unsupported` | the critic read the cited lines and they do not support the claim | critic verdict |

**High-risk** claims (checked by the critic and able to trigger retries): sales outcome, primary intent, high churn,
vulnerability, accepted product outcome, every compliance "met" rule, risk flags, identity "performed",
misquoted terms, the summary, and any contradicted claim.

Running example of a contradiction probe: if the compliance agent had said `REC_DISCLOSURE = missed`, the probe
would search the transcript for the rule's phrases ("recorded for quality", "call may be recorded", ...), find line 1
("Calls may be recorded for quality.") with a match score ≥ 0.9, and mark the claim `contradicted` with the hint
"L1 appears to satisfy REC_DISCLOSURE". On the retry, the compliance agent receives that hint as reviewer
feedback.

**Confidence is computed, never asked from the model:**

```text
confidence = grounding × support × critic_factor
  grounding     = mean grounding score of the cited facts (L-line citations count as 1.0)
  support       = 0.9 + 0.1 × min(number of evidence items, 3) / 3      (0.933 for 1 item, 1.0 for 3+)
  critic_factor = supported 1.0 | partially 0.8 | unsupported 0.3 | not checked 0.92
abstentions ("unclear", "no_pitch", ...) get a neutral 0.6
```

Example: the objection citing F7 (grounding 1.0), one evidence item, not sent to the critic:
`1.0 × 0.933 × 0.92 = 0.859`.

### Function by function

| Function | Where | What it does |
|---|---|---|
| `Supervisor._verify(ctx, outputs)` | `supervisor.py:218` | The loop above; returns a `GateReport` (claims, critic checked/rejected, retries). Critic verdicts are cached by claim key across attempts. |
| `ClaimCollector.__init__(ctx, kb)` | `gate.py:59` | Per-call collector. |
| `ClaimCollector.collect(outputs)` | `gate.py:83` | Walks every field of every agent output and creates one claim per statement, with the right `needs_evidence`, `high_risk`, severity and figure. |
| `ClaimCollector._add(agent, field, text, ids, needs, high, severity, number)` | `gate.py:66` | Resolves the ids, sets the grounding score, the `ref` (for example `sales.objections[0]`, used by the assembler), and the status (`ungrounded` checks). |
| `ClaimCollector._probe_missed(claim, rule, speaker)` | `gate.py:167` | Searches the transcript for the rule's phrases; a near-exact match (≥ 0.9), optionally by the agent, marks the claim `contradicted` and high-risk. |
| `ckey(agent, field, text, ids)` | `gate.py:52` | Stable claim key (agent, field, text, sorted ids) for caching critic verdicts. |
| `ClaimRec` + `.confidence` | `gate.py:27-49` | One claim and its computed confidence. |
| `GateReport` + `.by_ref()` | `gate.py:183-190` | Results of verification; `by_ref()` maps `agent.field[i]` → claim for the assembler. |
| `EvidenceRegistry.parse_ids(ids)` | `context.py:73` | Accepts messy forms ("F 12", "f-3", "L40, L41") and returns clean unique ids. |
| `EvidenceRegistry.resolve(ids)` | `context.py:82` | `F` ids → the fact's line, speaker, quote, grounding; `L` ids → the kept line (or nearest within 1); one evidence item per line; reports invalid ids. |
| `EvidenceRegistry.texts_for(evidence)` | `context.py:112` | Full line texts for evidence items (for the figure check). |
| `CriticAgent.verify(ctx, claims)` | `critic.py:49` | Splits claims into batches of 10 and runs them in parallel; merges verdicts; a failed batch just means "not checked". |
| `CriticAgent._run_batch(ctx, claims)` | `critic.py:35` | One critic call: `C1 claim: ... cited: L8 C: "..."`; maps `claim_id` back to claim keys. |
| `CriticAgent._fmt(ctx, i, claim)` | `critic.py:27` | Shows the claim and the **full text** of up to 4 cited lines. |
| `confidence(grounding, n_evidence, verdict)` | `grounding.py:80` | The formula above. |
| `CRITIC_FACTOR` | `grounding.py:77` | Verdict → multiplier. |

### Why this way

- **Layered, cheapest first.** Constrained decoding (free) → evidence-id resolution (microseconds) → span
  grounding (done in Step 16) → numeric fidelity → contradiction probes (BM25, milliseconds) → critic (LLM, only
  for high-risk claims) → targeted retry → abstain. Most problems are caught before any extra LLM call.
- **The critic sees the actual lines, not the agent's paraphrase.** A quote can be verbatim yet irrelevant
  ("Can you repeat that?" cited as the primary intent); only reading the line in context catches that.
- **Critic only on high-risk claims, batched 10 per call:** about 1-3 extra calls per transcript instead of one per
  claim.
- **Targeted retries:** only the agents that own failing claims are re-asked, with specific feedback ("L1 appears
  to satisfy REC_DISCLOSURE"). Re-running everything would cost more and could break claims that were fine.
- **Bounded loop (max 2 retries):** latency and cost cannot explode on a hard call.
- **Abstain over invent:** whatever still fails after the retries is not reported as fact; the assembler turns it
  into `unclear` with confidence 0 and a review item (Step 22).
- **Computed confidence:** LLM self-reported confidence is poorly calibrated (models say 0.9 for wrong answers).
  A formula from grounding, evidence count and an independent critic is reproducible and auditable.

---

## Step 22: assemble the final JSON

**Files:** `callintel/pipeline/engine.py` (lines 144-152), `callintel/redis_layer/vault.py`,
`callintel/agents/assemble.py`, `callintel/schemas.py`

### What happens

Back in `process_file`, after `Supervisor.analyze` returns:

1. **Optional vault:** if `privacy.vault.enabled`, the surrogate → original mapping is encrypted and stored in
   Redis with a 24-hour TTL. It is off by default.
2. **`prepared.pii_mapping.clear()`:** the original PII values are deleted from memory; nothing after this point
   can leak them.
3. Stage timings from preprocessing and from the supervisor are merged.
4. `Assembler(deps, ctx, report).build(...)` builds the `CallAnalysis` object, keeping **only claims whose status is
   `ok`**.

How the assembler decides each field:

| Kind of field | Rule |
|---|---|
| List items (objections, needs, pain points, buying signals, next steps, risk flags, misquotes, cross-sell) | Item kept only if its claim (`agent.field[i]`) is `ok`; it gets the resolved evidence and the computed confidence. |
| Single labels (primary intent, sales outcome, resolution status, churn, identity) | Claim `ok` → the value with evidence and confidence. Claim failed → `unclear` (or `other`) with confidence 0. No claim because the value is an abstention → the value with confidence 0.6. |
| Compliance rules | One result per rule; a failed claim becomes `unclear` with the reason; an applicable rule the agent never assessed is added as `unclear` "not assessed by agent". |
| Products | Only ids that were really detected in this call (`ctx.product_ids`); a product the agent invented is ignored; a detected product the agent did not describe is still reported (role `other`, `unclear`) so product counts are consistent. Each carries the best normalisation tier. |
| Summary | Kept only if its claim is `ok`; otherwise empty. |
| Quoted terms | Straight from `quoted_term` facts, with `numeric_verified`. |

**Status:** `needs_review` if there is any review item (a failed high-risk claim, a failed agent, failed map
chunks) **or** the mean confidence of kept claims is below `min_confidence` (0.75); otherwise `validated`.

Abbreviated output for the running example:

```json
{
  "schema_version": "3.0",
  "call_id": "9f2c41ab7e3d0c55",
  "call_profile": {
    "call_type": "sales",
    "primary_intent": {"value": "new_product_enquiry",
                       "evidence": [{"line": 3, "speaker": "CUSTOMER", "quote": "I got an SMS about the plat card"}],
                       "confidence": 0.933},
    "summary": "Customer enquired about the Platinum Rewards card from an SMS, objected to the fee, accepted after a first-year waiver."
  },
  "products": [{"product_id": "CC_PLATINUM_REWARDS", "mentions": ["Platinum Rewards Card", "plat card"],
                "role": "pitched", "outcome": "accepted", "normalization": {"tier": "exact", "confidence": 1.0}}],
  "sales": {"pitch_made": true, "sales_outcome": {"value": "converted", "confidence": 0.933},
            "objections": [{"category": "fees_too_high", "handled": "yes", "evidence": [{"line": 8, "...": "..."}]}]},
  "compliance": {"rules": [{"rule_id": "REC_DISCLOSURE", "status": "met", "evidence": [{"line": 2, "...": "..."}]}],
                 "misquoted_terms": [{"product_id": "CC_PLATINUM_REWARDS", "term": "monthly interest rate",
                                      "stated": "3.5%", "reference": "3.6%", "numeric_verified": true}]},
  "privacy": {"pii_masked": {"PERSON": 2, "DOB": 1, "CARD": 1}, "output_pii_findings": 0},
  "quality": {"status": "validated", "overall_confidence": 0.9, "critic_checked": 6, "retries": 0}
}
```

### Function by function

| Function | Where | What it does |
|---|---|---|
| `PIIVault.__init__(r, keys, cfg)` | `vault.py:19` | If enabled, reads the Fernet key from the environment variable (`PII_VAULT_KEY`) and fails clearly if it is missing. |
| `PIIVault.enabled` | `vault.py:30` | True when a key is loaded. |
| `PIIVault.store(call_id, mapping)` | `vault.py:33` | Encrypts each original value and stores `surrogate → ciphertext` in `ci:vault:<call_id>` with a TTL. |
| `PIIVault.reveal(call_id, surrogate)` | `vault.py:41` | Decrypts one value (only possible with the key, only before the TTL). |
| `PIIVault.generate_key()` | `vault.py:48` | New Fernet key (`python -m callintel vault-key`). |
| `Assembler.__init__(deps, ctx, report)` | `assemble.py:27` | Keeps the claims indexed by `ref`. |
| `Assembler.build(outputs, usage, stage_ms, duration_ms, prompt_versions, vault_stored)` | `assemble.py:183` | Builds review items, the call profile, overall confidence and status, all sections, `domain_insights` (fact-type counts, labels that fell into `other`, triage counts, tool calls, noise stats), the facts list and the quality block. |
| `Assembler._get(ref)` / `_ok(ref)` | `assemble.py:33-38` | Claim by reference; the claim only if its status is `ok`. |
| `Assembler._labeled(ref, value, fallback)` | `assemble.py:40` | The single-label rule from the table. |
| `Assembler._claims(prefix, items)` | `assemble.py:48` | The list-item rule for `TextClaim` lists. |
| `Assembler._sales(s)` | `assemble.py:57` | Sales section (objections, cross-sell with product names, pitch only if its claim is ok). |
| `Assembler._customer(c)` | `assemble.py:79` | Customer section; primary/secondary intents; sentiment trajectory computed from the enum order (start vs end: improving / declining / stable). |
| `Assembler._products(p)` | `assemble.py:99` | Product section with the "detected only" and "always report detected" rules. |
| `Assembler._product(pid, role, interest, outcome, points, ev, conf)` | `assemble.py:119` | One `ProductInsight`, including the mentions as said and the best normalisation tier. |
| `Assembler._compliance(k, review)` | `assemble.py:131` | Compliance section with the rules described above. |
| `Assembler._quoted_terms()` | `assemble.py:173` | Up to 30 quoted terms from the facts. |
| `_TIER_RANK` | `assemble.py:23` | Order used to pick the best normalisation tier. |

### Why this way

- **Fixed core + extensible `domain_insights`.** The core fields never change shape, so dashboards and downstream
  loads are stable; new experimental signals go into `domain_insights` without a schema version bump.
- **Abstain, never invent.** An unverified value becomes `unclear` with confidence 0, and the reason is in
  `review_items`. Analysts can trust that a non-`unclear` value has evidence.
- **Every claim carries evidence (line, speaker, masked quote) and a confidence**, so any number on a dashboard can
  be traced back to the exact words in the call.
- **Consistent product reporting:** a product that was detected is always reported, even if an agent forgot it, so
  product-level metrics do not depend on LLM mood.
- **Vault off by default:** analytics never needs real PII; reversibility is an explicit, audited, time-limited
  opt-in.

---

## Step 23: scan, write, close

**Files:** `callintel/pipeline/engine.py` (lines 153-177), `callintel/output/sinks.py`, `callintel/utils.py`

### What happens

1. **Output PII scan:** `_scan_output()` walks every string in the JSON (except source/processing metadata) with
   the regex detector. Anything found is replaced by `[REDACTED_<TYPE>]`, counted in
   `privacy.output_pii_findings`, and the call is set to `needs_review`.
2. **Write:** `OutputSink.write()` writes `data/output/calls/<day>/<call_id>.json` atomically, and appends one line to
   `data/output/jsonl/insights_<day>_<pid>.jsonl`.
3. **Status:** `done` if `validated`, otherwise `needs_review`; review calls are also pushed to `ci:stream:review`
   with up to 20 review items.
4. **Manifest** gets the status, output path, duration and call id; **daily metrics** are incremented.
5. `process_file` returns a small summary (status, output path, duration, LLM calls, cache hits, chunks
   analysed/total, facts, confidence) that the worker logs.
6. **Errors:** `EgressBlocked` → manifest `failed` + metric `egress_blocked` → `NonRetryableError` (DLQ at once).
   Any other exception → manifest `failed` → re-raised (the worker retries up to 3 times).
7. **`finally`:** the lock is always released.
8. Back in `worker_loop.handle()`, the message is acknowledged (Step 6).

### Function by function

| Function | Where | What it does |
|---|---|---|
| `Engine._scan_output(analysis)` | `engine.py:95` | Recursive scan and redaction as described; returns the (possibly new) analysis and the number of findings. |
| `walk(node, top)` (inside `_scan_output`) | `engine.py:99` | Recursion over dicts, lists and strings; skips `source`, `processing`, `privacy`, `call_id`, `schema_version` at the top level (hashes and metadata, not transcript content). |
| `OutputSink.__init__(settings)` / `.write(analysis)` | `sinks.py:15`, `:19` | Per-call JSON (pretty, atomic) and per-process daily JSONL (compact, append). Returns the JSON path. |
| `atomic_write_text(path, text)` | `utils.py:87` | Writes to a temp file in the same folder, then `os.replace` (atomic rename). On any error, the temp file is removed. |
| `dumps(obj, indent)` / `loads(data)` / `_default(obj)` | `utils.py:45-61` | Fast JSON with `orjson`; handles pydantic models, sets (sorted) and paths. |
| `today()` / `now_iso()` | `utils.py:37-42` | UTC date and timestamp. |
| `Metrics.incr(...)` | `store.py:105` | Daily counters (Step 15). |
| `NonRetryableError` | `engine.py:40` | Error class that tells the worker "do not retry". |

### Why this way

- **Defence in depth for privacy:** mask before the LLM (Step 10), block at the egress (Step 15), scan the output
  (here). The evaluation harness measures leakage in prompts **and** outputs with a hard gate of zero.
- **Atomic writes:** a dashboard or loader reading the folder never sees a half-written JSON, even if the worker
  is killed mid-write.
- **One JSONL file per process per day:** appends from different processes never interleave inside a line, and
  bulk loading (Spark, pandas, a warehouse `COPY`) is one file read.
- **Human-review queue:** low-confidence or partially failed calls are not hidden; they are routed to people with
  the exact reasons.
- **The lock is released in `finally`**, so even a crash inside the pipeline leaves the call retryable.

---

## Step 24: after processing

**Files:** `callintel/cli.py`, `callintel/output/sinks.py`, `callintel/schemas.py`

### What happens and function by function

| Command | Functions | What it does |
|---|---|---|
| `status` | `_status`, `StreamQueue.depth`, `Manifest.counts`, `Metrics.get` | Operational snapshot (queue, DLQ, review queues, learned aliases, manifest status counts, today's metrics). |
| `export-bi [--day]` | `export_bi`, `flatten`, `_ev_lines` | Reads the per-call JSONs and writes flat tables for Power BI or a warehouse: `calls`, `products`, `objections`, `compliance_rules`, `risk_flags`, `quoted_terms`, `intents`, `facts`, as Parquet (CSV if pyarrow is missing). |
| `aliases` | `_aliases` | Lists learned product aliases (for review and promotion into `products.yaml`). |
| `schema --out` | `CallAnalysis.model_json_schema()` | Writes the JSON Schema of the output contract for downstream teams. |
| `vault-key` | `PIIVault.generate_key` | Prints a new encryption key for the optional vault. |

| Function | Where | What it does |
|---|---|---|
| `Manifest.counts(limit)` | `store.py:88` | Reads the manifest index and pipelines `HGET status` for every hash; returns counts per status. |
| `export_bi(settings, day)` | `sinks.py:90` | Globs `calls/<day or *>/*.json`, flattens each, concatenates rows per table, writes Parquet/CSV to `output/bi/<day or all>/`. |
| `flatten(call)` | `sinks.py:37` | One call JSON → rows for each table (one row per call, per product, per objection, per rule, ...), always with `call_id` as the join key. |
| `_ev_lines(ev)` | `sinks.py:33` | Evidence list → "8,9,10" (line numbers as text) for BI tables. |

### Why this way

- **Star-schema-style flat tables** are what BI tools load fastest; nested JSON is awkward in Power BI.
- **Parquet** is columnar and compressed (fast, small); CSV is the fallback so the command never fails for a missing
  optional library.
- **Export is a separate step** from processing, so a BI refresh never slows down the pipeline and can be re-run
  for any day.

---

## Step 25: the evaluation harness

**Files:** `evaluation/run_eval.py`, `evaluation/metrics.py`, `evaluation/thresholds.yaml`

### What happens

The evaluation code is separate from the pipeline and runs on a **golden set**: transcripts in
`evaluation/golden/transcripts/` with hand-made (or generated) truth JSON in `evaluation/golden/truth/`.

```text
python -m evaluation.run_eval --profile local
 ├─ load settings, use key prefix "ci:eval" (never touches production state)
 ├─ run_pipeline()       Engine.process_file(force=True) on every golden file, with prompt capture ON
 ├─ load_predictions()   match outputs to golden files by SHA-256
 ├─ score()              6 metric families (below)
 ├─ judge_summaries()    optional --judge: LLM-as-judge faithfulness 1-5
 ├─ export_ragas()       optional --export-ragas: dataset for RAGAS / DeepEval
 ├─ apply_gates()        thresholds.yaml gates + regression check against the saved baseline
 ├─ write_report()       report.json + report.md
 └─ exit code 1 if any gate fails or a metric regressed (CI blocks the release)
```

Metric families computed by `score()`:

| Family | Metrics | How |
|---|---|---|
| Privacy | `pii_leakage_count` (hard gate **0**), `pii_masking_recall` (≥ 0.98), `product_names_preserved` (≥ 0.98) | Re-runs preprocessing; checks every truth PII value (and its variants: digits only, spaced, spoken e-mail, name parts) against the masked text, the **captured LLM prompts** and the outputs. |
| Schema | `schema_validity_rate`, `enum_validity_rate` (both 1.0) | Validates each output against `CallAnalysis` and checks every categorical value against the taxonomy, product ids and rule ids. |
| Normalisation | `normalization_mention_accuracy` (≥ 0.95), `product_set_f1` (≥ 0.80), `hallucinated_product_rate` (≤ 0.02) | Normalises every truth mention with deterministic tiers only (T3 off, for a stable metric); micro-F1 of product sets per call (generic ids ignored). |
| Grounding | `evidence_grounding_rate` (≥ 0.98), `hallucination_rate`, claims dropped, critic rejections, `needs_review_rate` | Every evidence quote in every output must match the masked line it cites. |
| Extraction | `call_type_accuracy`, `primary_intent_accuracy`, `sales_outcome_accuracy`, `identity_verification_accuracy`, `compliance_status_accuracy`, `objection_f1` | Compared with the truth labels. |
| Operations | latency p50/p95, LLM calls, tokens, cache hit rate, estimated cost per call, chunks skipped by triage, pipeline errors | From `processing` in each output and `llm.pricing` in config. |

### Function by function

| Function | Where | What it does |
|---|---|---|
| `main()` | `run_eval.py:348` | Arguments, settings, the flow above; `--predictions` scores existing outputs without running the pipeline; `--save-baseline` stores this run as the regression baseline. |
| `run_pipeline(s, files, out_dir, concurrency)` | `run_eval.py:50` | Redirects output/audit folders into the report folder, turns on `capture_llm_payloads` (masked prompts only), processes files with a semaphore, records status and wall time; errors are recorded, not raised. |
| `one(f)` (inside `run_pipeline`) | `run_eval.py:58` | Processes one golden file. |
| `load_predictions(pred_dir, files)` | `run_eval.py:75` | Indexes output JSONs by `source.sha256` and maps them to golden files. |
| `score(s, golden, files, preds, audit_dir, runs)` | `run_eval.py:147` | The metric families above. |
| `pv(d, path)` (inside `score`) | `run_eval.py:250` | Safe nested lookup (None if any level is missing). |
| `_enum_ok(d, tax, product_ids, rule_ids)` | `run_eval.py:132` | All categorical values valid? |
| `judge_summaries(s, preds)` | `run_eval.py:92` | Uses the `critic` role through the gateway to grade each summary 1-5 against its own evidence quotes. |
| `_Judgement` | `run_eval.py:87` | Judge output schema: faithfulness + unsupported statements. |
| `_walk_evidence(node)` | `run_eval.py:117` | Collects every `evidence` / `summary_evidence` list anywhere in an output. |
| `apply_gates(m, baseline)` | `run_eval.py:300` | Applies `min`/`max` gates; flags any regression metric that dropped more than `regression_tolerance` (0.03) below the baseline. |
| `write_report(out_dir, m, gates, ok, meta)` | `run_eval.py:322` | `report.json` (machine) + `report.md` (human table + all metrics). |
| `export_ragas(out_dir, preds)` | `run_eval.py:336` | JSONL with question / answer (summary) / contexts (evidence quotes). |
| `prf(pred, gold)` | `metrics.py:17` | Set precision, recall, F1 (with counts). |
| `micro_prf(pairs)` | `metrics.py:27` | Micro-averaged P/R/F1 across calls. |
| `accuracy(pairs)` | `metrics.py:40` | Share of exact matches, ignoring items without truth. |
| `percentile(values, q)` | `metrics.py:45` | Linear-interpolated percentile (p50, p95). |
| `pii_variants(value, ptype)` | `metrics.py:54` | Forms a leaked value could take (digits only, groups of 4, name parts, spoken e-mail). |
| `contains_pii(text, value, ptype)` | `metrics.py:68` | True if any variant appears (word-boundary match for names). |
| `quote_grounded(quote, line_text, threshold)` | `metrics.py:79` | Substring or fuzzy ≥ 0.8 (ignores a trailing "..."). |
| `mean_or_zero(xs)` | `metrics.py:88` | Mean, or 0 for an empty list. |

### Why this way

- **Separate, standard evaluation code** (a requirement): the harness imports the pipeline but the pipeline never
  imports the harness; metrics are plain definitions (accuracy, P/R/F1, percentiles), comparable across models,
  prompts and releases.
- **Gates in YAML, run in CI:** a prompt change or model swap that lowers masking recall or grounding cannot be
  released by accident; the exit code blocks it.
- **Leakage is tested where it matters,** in the actual prompts sent to the model and in the outputs, not just in the
  masked text.
- **Baseline regression check** catches slow quality drift that stays above the absolute gates.
- **Deterministic normalisation metric:** T3 (LLM) is turned off when measuring normalisation so the number reflects
  the catalogue and fuzzy logic, not model randomness.
- **RAGAS export** lets you use standard external tools without coupling the code to them.

---

## Step 26: synthetic data and tests

### The synthetic transcript generator (`tools/synth.py`)

Generates realistic long calls with ground truth, for smoke tests, load tests and the golden set:

```powershell
python -m tools.synth --n 5 --lines 6000 --out data/incoming
python -m tools.synth --n 8 --lines 500 --out evaluation/golden/transcripts --truth evaluation/golden/truth
```

| Function | Where | What it does |
|---|---|---|
| `luhn_card(rng)` | `synth.py:50` | A fake 16-digit card number with a valid Luhn check digit (so the masker must catch it). |
| `aadhaar(rng)` | `synth.py:62` | A fake 12-digit Aadhaar with a valid Verhoeff check digit. |
| `spoken(num, rng)` | `synth.py:70` | Reads a number aloud with "double" ("nine eight double seven ..."), like real callers. |
| `CallBuilder.__init__(rng, labels)` | `synth.py:83` | Speaker labels (Agent/Customer, Rep/Caller, Speaker 1/2), a clock. |
| `CallBuilder._ts()` | `synth.py:89` | Advances the clock 1-6 s and returns `[hh:mm:ss]`. |
| `CallBuilder.say(who, text)` | `synth.py:93` | Adds a line; 15% chance of a filler prefix, 5% chance of a stutter, 2% chance of a duplicated ASR segment. |
| `CallBuilder.noise_block(n)` | `synth.py:107` | Hold loops with `[hold music]`, statement read-outs, network trouble, small talk, system-is-slow, crosstalk. |
| `build_call(seed, target_lines)` | `synth.py:141` | Picks a scenario (sales card, sales loan, service dispute, fee complaint, mixed block + cross-sell, SIP investment), PII (names, phone, DOB, address, card, Aadhaar, account, e-mail) and product aliases; spreads business content through noise; records the truth (call type, intent, outcome, products, mentions, objections, compliance statuses, PII values). |
| `gap()`, `mention(pid)`, `apr_disclosure(pid)` (inside `build_call`) | `synth.py:160-188` | Inserts a noise gap; picks an alias for a product and records it in the truth; says the rate/fee disclosure for card or loan products. |
| `main()` | `synth.py:298` | CLI: writes `call_<seed>.txt` (+ truth JSON) for `--n` calls of `--lines` lines. |

**Why:** real banking transcripts cannot be shared or put in a repository. Synthetic calls with
**checksum-valid** fake PII, spoken numbers and heavy noise test the hard cases on purpose, and give exact ground
truth for the evaluation gates. Seeds make every set reproducible.

### The tests (`tests/`, 27 tests, run with `pytest`)

All tests use the `mock` LLM profile and an isolated in-process Redis, so they run offline in seconds.

| Test file | Functions | What they prove |
|---|---|---|
| `conftest.py` | `settings(tmp_path)` fixture | Mock profile, unreachable Redis URL → fakeredis fallback, unique key prefix per test, temp output folders, cheap detectors only, no embeddings. |
| `test_pii.py` | `masker()` helper, `test_checksums`, `test_spoken_digits`, `test_structured_ids_masked`, `test_names_consistent_and_two_pass`, `test_dialogue_aware_answers`, `test_product_names_protected_from_name_detection`, `test_amounts_and_rates_not_masked`, `test_egress_scan_ignores_surrogates` | Luhn/Verhoeff; spoken digits; card/PAN/phone/e-mail masked; same name → same token even before the introduction; answers to DOB/number questions masked; product names kept; amounts and rates kept; surrogates are not re-flagged by the egress scan. |
| `test_preprocess.py` | `test_noise_cleaner_keeps_meaning`, `test_parse_roles_and_named_labels`, `test_repetition_compression_keeps_short_answers`, `test_chunking_respects_turns_and_overlap`, `test_chunk_turns_single_long_turn` | Fillers/tags removed but "yes/no" kept; name labels mapped to roles; hold loops compressed but short answers untouched; chunks never split turns and overlap correctly; an oversized turn is split safely. |
| `test_gateway.py` | `_schemas()` helper, `test_salvage_truncated_json`, `test_strict_schema_openai_vs_grammar` | Truncated JSON salvage; OpenAI strict schema drops `maxItems` while the Ollama schema keeps it. |
| `test_normalize_and_grounding.py` | `_norm()` helper, `test_tiers`, `test_learned_alias_shared_via_redis`, `test_catalog_spans_prefer_longest`, `test_grounding_helpers` | Exact/fuzzy/unresolved tiers; an alias learned by one normaliser is found by another through Redis; longest catalogue match wins; quote and number grounding. |
| `test_redis_layer.py` | `test_stream_ack_retry_dlq`, `test_reclaim_from_crashed_consumer`, `test_manifest_idempotency`, `test_lock_exclusive`, `test_rate_limiter_blocks_over_budget` | Ack, retry and DLQ; reclaim of a crashed consumer's message; duplicate files not re-queued; lock is exclusive; rate limiter waits when over budget. |
| `test_pipeline_e2e.py` | `test_process_file_end_to_end`, `test_run_local_queue`, `test_egress_guard_blocks_raw_pii` | A full call produces a valid `CallAnalysis` with no raw PII and is skipped on re-run; the local queue mode processes a folder; raw PII in a prompt to an external provider is blocked. |

**Why:** each test pins one design promise (privacy, idempotency, grounding, consistency) so a refactor that breaks
it fails fast, without needing a GPU or network.

(`docs/diagrams/build_diagrams.py` only draws the architecture diagrams for documentation; it is not part of the
pipeline.)

---

## Step 27: shared helpers

**File:** `callintel/utils.py`

| Function | Where | What it does | Used in |
|---|---|---|---|
| `sha256_bytes(data)` | `utils.py:21` | SHA-256 hex of bytes. | utility |
| `sha256_text(text)` | `utils.py:25` | SHA-256 hex of a UTF-8 string. | cache keys, prompt tags, schema hashes |
| `sha256_file(path, block)` | `utils.py:29` | Streaming file hash. | Step 5, Step 7, evaluation |
| `now_iso()` | `utils.py:37` | UTC timestamp, seconds precision. | manifest, outputs, DLQ |
| `today()` | `utils.py:41` | UTC date `YYYY-MM-DD`. | metrics keys, output folders, audit files |
| `dumps(obj, indent)` | `utils.py:45` | orjson serialisation (non-string keys allowed). | everywhere |
| `loads(data)` | `utils.py:50` | orjson parsing. | everywhere |
| `_default(obj)` | `utils.py:54` | Serialises pydantic models, sets, paths. | `dumps` |
| `estimate_tokens(text)` | `utils.py:64` | chars/4 or exact tiktoken (opt-in). | chunking, rate limiting |
| `atomic_write_text(path, text)` | `utils.py:87` | Temp file + atomic rename. | Step 23 |
| `Stopwatch` (`__init__`, `stage`, `total_ms`) | `utils.py:104-118` | Stage timings. | Steps 7-21 |
| `setup_logging(level)` | `utils.py:121` | One stderr handler with time, level, process name; quiets noisy libraries (httpx, openai, ...); idempotent. | Step 1, workers |
| `get_logger(name)` | `utils.py:135` | `logging.getLogger`. | utility |

**Why orjson:** several times faster than the standard `json` module, and outputs are serialised for every call
(JSON + JSONL + cache entries).

---

## Appendix A: function index

Every function and class in the project, in file order, with the step that explains it.
Total: **487** functions and classes across 44 files.
Generated from the source code, so line numbers match the current files.

### `callintel/agents/assemble.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `Assembler` | class | 26 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler.__init__` | function | 27 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler._get` | function | 33 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler._ok` | function | 36 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler._labeled` | function | 40 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler._claims` | function | 48 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler._sales` | function | 57 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler._customer` | function | 79 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler._products` | function | 99 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler._product` | function | 119 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler._compliance` | function | 131 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler._quoted_terms` | function | 173 | [Step 22](#step-22-assemble-the-final-json) |
| `Assembler.build` | function | 183 | [Step 22](#step-22-assemble-the-final-json) |

### `callintel/agents/base.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `BaseAgent` | class | 22 | [Step 19](#step-19-the-specialist-agents) |
| `BaseAgent.__init__` | function | 27 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `BaseAgent.call` | function | 31 | [Step 19](#step-19-the-specialist-agents) |
| `BaseAgent.run_tools` | function | 53 | [Step 19](#step-19-the-specialist-agents) |
| `fmt_facts` | function | 72 | [Step 19](#step-19-the-specialist-agents) |

### `callintel/agents/context.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `AgentDeps` | class | 28 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `FactRec` | class | 40 | [Step 16](#step-16-ground-check-and-de-duplicate-the-facts) |
| `FactRec.fmt` | function | 57 | [Step 16](#step-16-ground-check-and-de-duplicate-the-facts) |
| `EvidenceRegistry` | class | 64 | [Step 12](#step-12-the-supervisor-starts) |
| `EvidenceRegistry.__init__` | function | 65 | [Step 12](#step-12-the-supervisor-starts) |
| `EvidenceRegistry.add` | function | 69 | [Step 12](#step-12-the-supervisor-starts) |
| `EvidenceRegistry.parse_ids` | function | 73 | [Step 21](#step-21-the-verification-loop) |
| `EvidenceRegistry.resolve` | function | 82 | [Step 21](#step-21-the-verification-loop) |
| `EvidenceRegistry.texts_for` | function | 112 | [Step 21](#step-21-the-verification-loop) |
| `CallContext` | class | 121 | [Step 12](#step-12-the-supervisor-starts) |
| `CallContext.facts` | function | 131 | [Step 12](#step-12-the-supervisor-starts) |
| `CallContext.facts_of` | function | 134 | [Step 12](#step-12-the-supervisor-starts) |

### `callintel/agents/critic.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `CriticAgent` | class | 19 | [Step 21](#step-21-the-verification-loop) |
| `CriticAgent.__init__` | function | 22 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `CriticAgent._fmt` | function | 27 | [Step 21](#step-21-the-verification-loop) |
| `CriticAgent._run_batch` | function | 35 | [Step 21](#step-21-the-verification-loop) |
| `CriticAgent.verify` | function | 49 | [Step 21](#step-21-the-verification-loop) |

### `callintel/agents/mapper.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `MapAgent` | class | 15 | [Step 14](#step-14-the-map-agent-extracts-facts) |
| `MapAgent.__init__` | function | 18 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `MapAgent.extract` | function | 25 | [Step 14](#step-14-the-map-agent-extracts-facts) |

### `callintel/agents/specialists.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `candidate_lines` | function | 30 | [Step 19](#step-19-the-specialist-agents) |
| `with_candidates` | function | 45 | [Step 19](#step-19-the-specialist-agents) |
| `_products_line` | function | 49 | [Step 19](#step-19-the-specialist-agents) |
| `SalesAgent` | class | 60 | [Step 19](#step-19-the-specialist-agents) |
| `SalesAgent.empty` | function | 65 | [Step 19](#step-19-the-specialist-agents) |
| `SalesAgent.run` | function | 70 | [Step 19](#step-19-the-specialist-agents) |
| `CustomerAgent` | class | 84 | [Step 19](#step-19-the-specialist-agents) |
| `CustomerAgent.empty` | function | 89 | [Step 19](#step-19-the-specialist-agents) |
| `CustomerAgent.run` | function | 95 | [Step 19](#step-19-the-specialist-agents) |
| `ProductAgent` | class | 104 | [Step 19](#step-19-the-specialist-agents) |
| `ProductAgent.run` | function | 109 | [Step 19](#step-19-the-specialist-agents) |
| `ComplianceAgent` | class | 120 | [Step 19](#step-19-the-specialist-agents) |
| `ComplianceAgent.applicable_rules` | function | 125 | [Step 18](#step-18-the-call-type-hint-and-the-rule-list) |
| `ComplianceAgent.empty` | function | 129 | [Step 19](#step-19-the-specialist-agents) |
| `ComplianceAgent.run` | function | 133 | [Step 19](#step-19-the-specialist-agents) |
| `SynthesisAgent` | class | 149 | [Step 20](#step-20-the-synthesis-agent) |
| `SynthesisAgent.run` | function | 154 | [Step 20](#step-20-the-synthesis-agent) |
| `findings_digest` | function | 160 | [Step 20](#step-20-the-synthesis-agent) |

### `callintel/agents/supervisor.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `_n` | function | 39 | [Step 16](#step-16-ground-check-and-de-duplicate-the-facts) |
| `MapStageFailed` | class | 52 | [Step 12](#step-12-the-supervisor-starts) |
| `Supervisor` | class | 56 | [Step 12](#step-12-the-supervisor-starts) |
| `Supervisor.__init__` | function | 57 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Supervisor.analyze` | function | 68 | [Step 12](#step-12-the-supervisor-starts) |
| `Supervisor._ingest_facts` | function | 103 | [Step 16](#step-16-ground-check-and-de-duplicate-the-facts) |
| `Supervisor._normalize` | function | 165 | [Step 17](#step-17-normalise-product-names) |
| `Supervisor._run_specialists` | function | 187 | [Step 19](#step-19-the-specialist-agents) |
| `Supervisor._synthesis_fallback` | function | 201 | [Step 20](#step-20-the-synthesis-agent) |
| `Supervisor._run_synthesis` | function | 208 | [Step 20](#step-20-the-synthesis-agent) |
| `Supervisor._verify` | function | 218 | [Step 21](#step-21-the-verification-loop) |

### `callintel/agents/triage.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `score_chunks` | function | 36 | [Step 13](#step-13-triage) |
| `call_type_hint` | function | 50 | [Step 18](#step-18-the-call-type-hint-and-the-rule-list) |

### `callintel/cli.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `_settings` | function | 25 | [Step 1](#step-1-the-command-starts-cli) |
| `_doctor` | function | 31 | [Step 1](#step-1-the-command-starts-cli) |
| `_status` | function | 79 | [Step 1](#step-1-the-command-starts-cli) |
| `_aliases` | function | 97 | [Step 1](#step-1-the-command-starts-cli) |
| `main` | function | 106 | [Step 1](#step-1-the-command-starts-cli) |
| `main._probe` | function | 151 | [Step 1](#step-1-the-command-starts-cli) |
| `main._one` | function | 168 | [Step 1](#step-1-the-command-starts-cli) |
| `main._ing` | function | 184 | [Step 1](#step-1-the-command-starts-cli) |

### `callintel/config.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `_interpolate` | function | 23 | [Step 2](#step-2-configuration-is-loaded) |
| `_interpolate.repl` | function | 25 | [Step 2](#step-2-configuration-is-loaded) |
| `_Section` | class | 36 | [Step 2](#step-2-configuration-is-loaded) |
| `AppCfg` | class | 40 | [Step 2](#step-2-configuration-is-loaded) |
| `PathsCfg` | class | 46 | [Step 2](#step-2-configuration-is-loaded) |
| `RedisCfg` | class | 56 | [Step 2](#step-2-configuration-is-loaded) |
| `WorkersCfg` | class | 65 | [Step 2](#step-2-configuration-is-loaded) |
| `EgressCfg` | class | 74 | [Step 2](#step-2-configuration-is-loaded) |
| `VaultCfg` | class | 80 | [Step 2](#step-2-configuration-is-loaded) |
| `PrivacyCfg` | class | 86 | [Step 2](#step-2-configuration-is-loaded) |
| `SpeakersCfg` | class | 98 | [Step 2](#step-2-configuration-is-loaded) |
| `PreprocessCfg` | class | 104 | [Step 2](#step-2-configuration-is-loaded) |
| `ChunkingCfg` | class | 117 | [Step 2](#step-2-configuration-is-loaded) |
| `TriageCfg` | class | 123 | [Step 2](#step-2-configuration-is-loaded) |
| `AgentsCfg` | class | 129 | [Step 2](#step-2-configuration-is-loaded) |
| `NormalizerCfg` | class | 139 | [Step 2](#step-2-configuration-is-loaded) |
| `RagCfg` | class | 146 | [Step 2](#step-2-configuration-is-loaded) |
| `OutputCfg` | class | 152 | [Step 2](#step-2-configuration-is-loaded) |
| `AuditCfg` | class | 159 | [Step 2](#step-2-configuration-is-loaded) |
| `ProviderCfg` | class | 163 | [Step 2](#step-2-configuration-is-loaded) |
| `RoleCfg` | class | 174 | [Step 2](#step-2-configuration-is-loaded) |
| `LLMCfg` | class | 185 | [Step 2](#step-2-configuration-is-loaded) |
| `Settings` | class | 192 | [Step 2](#step-2-configuration-is-loaded) |
| `Settings.path` | function | 212 | [Step 2](#step-2-configuration-is-loaded) |
| `Settings.profile` | function | 216 | [Step 2](#step-2-configuration-is-loaded) |
| `Settings.role` | function | 222 | [Step 2](#step-2-configuration-is-loaded) |
| `Settings.provider` | function | 231 | [Step 2](#step-2-configuration-is-loaded) |
| `load_yaml` | function | 237 | [Step 2](#step-2-configuration-is-loaded) |
| `load_settings` | function | 242 | [Step 2](#step-2-configuration-is-loaded) |
| `get_settings` | function | 256 | [Step 2](#step-2-configuration-is-loaded) |

### `callintel/llm/egress.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `EgressBlocked` | class | 24 | [Step 15](#step-15-inside-the-llm-gateway) |
| `EgressGuard` | class | 28 | [Step 15](#step-15-inside-the-llm-gateway) |
| `EgressGuard.__init__` | function | 29 | [Step 15](#step-15-inside-the-llm-gateway) |
| `EgressGuard._audit` | function | 37 | [Step 15](#step-15-inside-the-llm-gateway) |
| `EgressGuard.check` | function | 43 | [Step 15](#step-15-inside-the-llm-gateway) |

### `callintel/llm/gateway.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `LLMError` | class | 44 | [Step 15](#step-15-inside-the-llm-gateway) |
| `TransientLLMError` | class | 48 | [Step 15](#step-15-inside-the-llm-gateway) |
| `Usage` | class | 54 | [Step 7](#step-7-process_file-opens-a-call) |
| `Usage.add` | function | 64 | [Step 7](#step-7-process_file-opens-a-call) |
| `usage_scope` | function | 86 | [Step 7](#step-7-process_file-opens-a-call) |
| `LLMResult` | class | 97 | [Step 15](#step-15-inside-the-llm-gateway) |
| `_extract_json` | function | 110 | [Step 15](#step-15-inside-the-llm-gateway) |
| `salvage_truncated_json` | function | 121 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OllamaProvider` | class | 153 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OllamaProvider.__init__` | function | 154 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OllamaProvider.chat` | function | 160 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OllamaProvider.embed` | function | 185 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OllamaProvider.ping` | function | 190 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OllamaProvider.close` | function | 201 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OpenAIProvider` | class | 205 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OpenAIProvider.__init__` | function | 208 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OpenAIProvider.chat` | function | 218 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OpenAIProvider.embed` | function | 247 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OpenAIProvider.ping` | function | 251 | [Step 15](#step-15-inside-the-llm-gateway) |
| `OpenAIProvider.close` | function | 261 | [Step 15](#step-15-inside-the-llm-gateway) |
| `MockProvider` | class | 265 | [Step 15](#step-15-inside-the-llm-gateway) |
| `MockProvider.__init__` | function | 284 | [Step 15](#step-15-inside-the-llm-gateway) |
| `MockProvider.chat` | function | 287 | [Step 15](#step-15-inside-the-llm-gateway) |
| `MockProvider.embed` | function | 311 | [Step 15](#step-15-inside-the-llm-gateway) |
| `MockProvider.ping` | function | 318 | [Step 15](#step-15-inside-the-llm-gateway) |
| `MockProvider.close` | function | 321 | [Step 15](#step-15-inside-the-llm-gateway) |
| `LLMGateway` | class | 330 | [Step 15](#step-15-inside-the-llm-gateway) |
| `LLMGateway.__init__` | function | 331 | [Step 15](#step-15-inside-the-llm-gateway) |
| `LLMGateway.provider` | function | 348 | [Step 15](#step-15-inside-the-llm-gateway) |
| `LLMGateway._sem` | function | 354 | [Step 15](#step-15-inside-the-llm-gateway) |
| `LLMGateway.role_model` | function | 359 | [Step 15](#step-15-inside-the-llm-gateway) |
| `LLMGateway.structured` | function | 363 | [Step 15](#step-15-inside-the-llm-gateway) |
| `LLMGateway._call_with_retries` | function | 440 | [Step 15](#step-15-inside-the-llm-gateway) |
| `LLMGateway.embed` | function | 454 | [Step 15](#step-15-inside-the-llm-gateway) |
| `LLMGateway.ping_role` | function | 478 | [Step 15](#step-15-inside-the-llm-gateway) |
| `LLMGateway.close` | function | 485 | [Step 15](#step-15-inside-the-llm-gateway) |

### `callintel/llm/prompts.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `render` | function | 16 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `render.repl` | function | 17 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Prompt` | class | 26 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Prompt.tag` | function | 33 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Prompt.with_static` | function | 37 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Prompt.messages` | function | 41 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `PromptRegistry` | class | 46 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `PromptRegistry.__init__` | function | 47 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `PromptRegistry.get` | function | 53 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `PromptRegistry.versions` | function | 56 | [Step 4](#step-4-the-engine-is-built-once-per-process) |

### `callintel/llm/schema_utils.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `_strict` | function | 26 | [Step 15](#step-15-inside-the-llm-gateway) |
| `_cached` | function | 53 | [Step 15](#step-15-inside-the-llm-gateway) |
| `strict_schema` | function | 63 | [Step 15](#step-15-inside-the-llm-gateway) |
| `schema_hash` | function | 68 | [Step 15](#step-15-inside-the-llm-gateway) |
| `minimal_instance` | function | 72 | [Step 15](#step-15-inside-the-llm-gateway) |

### `callintel/normalize/catalog.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `norm_mention` | function | 16 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Product` | class | 25 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Product.all_names` | function | 35 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `ProductCatalog` | class | 39 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `ProductCatalog.__init__` | function | 40 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `ProductCatalog.ids` | function | 66 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `ProductCatalog.get` | function | 69 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `ProductCatalog.find_mentions` | function | 72 | [Step 13](#step-13-triage) |
| `ProductCatalog.protected_spans` | function | 83 | [Step 10](#step-10-mask-pii) |

### `callintel/normalize/normalizer.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `NormResult` | class | 40 | [Step 17](#step-17-normalise-product-names) |
| `NormResult.as_json` | function | 46 | [Step 17](#step-17-normalise-product-names) |
| `ProductNormalizer` | class | 50 | [Step 17](#step-17-normalise-product-names) |
| `ProductNormalizer.__init__` | function | 51 | [Step 17](#step-17-normalise-product-names) |
| `ProductNormalizer._looks_like_product` | function | 75 | [Step 17](#step-17-normalise-product-names) |
| `ProductNormalizer.enable_dense` | function | 79 | [Step 17](#step-17-normalise-product-names) |
| `ProductNormalizer._fuzzy` | function | 83 | [Step 17](#step-17-normalise-product-names) |
| `ProductNormalizer.candidates` | function | 94 | [Step 17](#step-17-normalise-product-names) |
| `ProductNormalizer.normalize` | function | 105 | [Step 17](#step-17-normalise-product-names) |
| `ProductNormalizer._resolve` | function | 136 | [Step 17](#step-17-normalise-product-names) |
| `ProductNormalizer._learn` | function | 173 | [Step 17](#step-17-normalise-product-names) |
| `ProductNormalizer.normalize_many` | function | 176 | [Step 17](#step-17-normalise-product-names) |

### `callintel/output/sinks.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `OutputSink` | class | 14 | [Step 23](#step-23-scan-write-close) |
| `OutputSink.__init__` | function | 15 | [Step 23](#step-23-scan-write-close) |
| `OutputSink.write` | function | 19 | [Step 23](#step-23-scan-write-close) |
| `_ev_lines` | function | 33 | [Step 24](#step-24-after-processing) |
| `flatten` | function | 37 | [Step 24](#step-24-after-processing) |
| `export_bi` | function | 90 | [Step 24](#step-24-after-processing) |

### `callintel/pipeline/engine.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `NonRetryableError` | class | 40 | [Step 23](#step-23-scan-write-close) |
| `Engine` | class | 44 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Engine.__init__` | function | 45 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Engine.create` | function | 50 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Engine._maybe_enable_dense` | function | 74 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Engine.close` | function | 90 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Engine._scan_output` | function | 95 | [Step 23](#step-23-scan-write-close) |
| `Engine._scan_output.walk` | function | 99 | [Step 23](#step-23-scan-write-close) |
| `Engine.process_file` | function | 124 | [Step 7](#step-7-process_file-opens-a-call) |

### `callintel/pipeline/worker.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `queues` | function | 29 | [Step 5](#step-5-files-are-found-and-queued-ingest) |
| `enqueue_folder` | function | 35 | [Step 5](#step-5-files-are-found-and-queued-ingest) |
| `watch_folder` | function | 59 | [Step 5](#step-5-files-are-found-and-queued-ingest) |
| `worker_loop` | function | 71 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `worker_loop.handle` | function | 84 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `_process_entry` | function | 142 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `run_worker_pool` | function | 150 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `run_local` | function | 168 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |

### `callintel/preprocess/cleaning.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `normalize_spoken_digits` | function | 35 | [Step 9](#step-9-clean-the-noise) |
| `_collapse_repeats` | function | 77 | [Step 9](#step-9-clean-the-noise) |
| `_collapse_repeats.repl` | function | 78 | [Step 9](#step-9-clean-the-noise) |
| `NoiseCleaner` | class | 87 | [Step 9](#step-9-clean-the-noise) |
| `NoiseCleaner.__init__` | function | 88 | [Step 9](#step-9-clean-the-noise) |
| `NoiseCleaner.clean` | function | 94 | [Step 9](#step-9-clean-the-noise) |

### `callintel/preprocess/pii.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `luhn_ok` | function | 42 | [Step 10](#step-10-mask-pii) |
| `verhoeff_ok` | function | 65 | [Step 10](#step-10-mask-pii) |
| `Span` | class | 77 | [Step 10](#step-10-mask-pii) |
| `Recognizer` | class | 87 | [Step 10](#step-10-mask-pii) |
| `Recognizer.analyze` | function | 90 | [Step 10](#step-10-mask-pii) |
| `_Pat` | class | 94 | [Step 10](#step-10-mask-pii) |
| `_digits` | function | 102 | [Step 10](#step-10-mask-pii) |
| `RegexRecognizer` | class | 106 | [Step 10](#step-10-mask-pii) |
| `RegexRecognizer.__init__` | function | 111 | [Step 10](#step-10-mask-pii) |
| `RegexRecognizer.analyze` | function | 128 | [Step 10](#step-10-mask-pii) |
| `ContextRecognizer` | class | 166 | [Step 10](#step-10-mask-pii) |
| `ContextRecognizer.__init__` | function | 171 | [Step 10](#step-10-mask-pii) |
| `ContextRecognizer._clean_name` | function | 210 | [Step 10](#step-10-mask-pii) |
| `ContextRecognizer.analyze` | function | 221 | [Step 10](#step-10-mask-pii) |
| `ContextRecognizer.expected_types` | function | 242 | [Step 10](#step-10-mask-pii) |
| `ContextRecognizer.analyze_answer` | function | 246 | [Step 10](#step-10-mask-pii) |
| `PresidioRecognizer` | class | 285 | [Step 10](#step-10-mask-pii) |
| `PresidioRecognizer.__init__` | function | 291 | [Step 10](#step-10-mask-pii) |
| `PresidioRecognizer.analyze` | function | 300 | [Step 10](#step-10-mask-pii) |
| `GlinerRecognizer` | class | 309 | [Step 10](#step-10-mask-pii) |
| `GlinerRecognizer.__init__` | function | 315 | [Step 10](#step-10-mask-pii) |
| `GlinerRecognizer.analyze` | function | 321 | [Step 10](#step-10-mask-pii) |
| `build_recognizers` | function | 333 | [Step 10](#step-10-mask-pii) |
| `MaskResult` | class | 357 | [Step 10](#step-10-mask-pii) |
| `_canon` | function | 364 | [Step 10](#step-10-mask-pii) |
| `PIIMasker` | class | 374 | [Step 10](#step-10-mask-pii) |
| `PIIMasker.__init__` | function | 377 | [Step 10](#step-10-mask-pii) |
| `PIIMasker._surrogate` | function | 389 | [Step 10](#step-10-mask-pii) |
| `PIIMasker._detect` | function | 404 | [Step 10](#step-10-mask-pii) |
| `PIIMasker._protected` | function | 416 | [Step 10](#step-10-mask-pii) |
| `PIIMasker._learn_names` | function | 424 | [Step 10](#step-10-mask-pii) |
| `PIIMasker._gazetteer` | function | 437 | [Step 10](#step-10-mask-pii) |
| `PIIMasker._resolve` | function | 444 | [Step 10](#step-10-mask-pii) |
| `PIIMasker.mask_lines` | function | 452 | [Step 10](#step-10-mask-pii) |
| `find_pii` | function | 498 | [Step 10](#step-10-mask-pii) |

### `callintel/preprocess/transcript.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `Line` | class | 35 | [Step 8](#step-8-parse-lines-and-work-out-who-is-speaking) |
| `Segment` | class | 43 | [Step 11](#step-11-build-turns-and-chunks) |
| `Turn` | class | 49 | [Step 11](#step-11-build-turns-and-chunks) |
| `Turn.line_start` | function | 55 | [Step 11](#step-11-build-turns-and-chunks) |
| `Turn.line_end` | function | 59 | [Step 11](#step-11-build-turns-and-chunks) |
| `Turn.text` | function | 63 | [Step 11](#step-11-build-turns-and-chunks) |
| `Turn.render` | function | 66 | [Step 11](#step-11-build-turns-and-chunks) |
| `Chunk` | class | 71 | [Step 11](#step-11-build-turns-and-chunks) |
| `Chunk.line_start` | function | 81 | [Step 11](#step-11-build-turns-and-chunks) |
| `Chunk.line_end` | function | 85 | [Step 11](#step-11-build-turns-and-chunks) |
| `Chunk.render` | function | 88 | [Step 11](#step-11-build-turns-and-chunks) |
| `PreparedTranscript` | class | 93 | [Step 11](#step-11-build-turns-and-chunks) |
| `PreparedTranscript.masked_token_count` | function | 108 | [Step 11](#step-11-build-turns-and-chunks) |
| `_label_role_map` | function | 113 | [Step 8](#step-8-parse-lines-and-work-out-who-is-speaking) |
| `_label_role_map.agent_score` | function | 127 | [Step 8](#step-8-parse-lines-and-work-out-who-is-speaking) |
| `parse_lines` | function | 141 | [Step 8](#step-8-parse-lines-and-work-out-who-is-speaking) |
| `build_turns` | function | 170 | [Step 11](#step-11-build-turns-and-chunks) |
| `_split_long_turn` | function | 181 | [Step 11](#step-11-build-turns-and-chunks) |
| `chunk_turns` | function | 197 | [Step 11](#step-11-build-turns-and-chunks) |
| `prepare_transcript` | function | 228 | [Step 8](#step-8-parse-lines-and-work-out-who-is-speaking) |

### `callintel/rag/knowledge_base.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `Rule` | class | 25 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Rule.applies` | function | 34 | [Step 18](#step-18-the-call-type-hint-and-the-rule-list) |
| `KnowledgeBase` | class | 40 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `KnowledgeBase.__init__` | function | 41 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `KnowledgeBase.enable_dense` | function | 68 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `KnowledgeBase.rule_ids` | function | 72 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `KnowledgeBase.rule` | function | 75 | [Step 18](#step-18-the-call-type-hint-and-the-rule-list) |
| `KnowledgeBase.rules_for` | function | 78 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `KnowledgeBase.search` | function | 81 | [Step 19](#step-19-the-specialist-agents) |
| `KnowledgeBase.product_reference` | function | 86 | [Step 19](#step-19-the-specialist-agents) |

### `callintel/rag/retrieval.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `_stem` | function | 25 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `tokenize` | function | 32 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `BM25` | class | 36 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `BM25.__init__` | function | 37 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `BM25.search` | function | 48 | [Step 17](#step-17-normalise-product-names) |
| `Doc` | class | 62 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Hit` | class | 69 | [Step 17](#step-17-normalise-product-names) |
| `HybridIndex` | class | 74 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `HybridIndex.__init__` | function | 77 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `HybridIndex.enable_dense` | function | 83 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `HybridIndex.search` | function | 89 | [Step 17](#step-17-normalise-product-names) |

### `callintel/rag/transcript_index.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `TranscriptIndex` | class | 15 | [Step 12](#step-12-the-supervisor-starts) |
| `TranscriptIndex.__init__` | function | 16 | [Step 12](#step-12-the-supervisor-starts) |
| `TranscriptIndex.line` | function | 23 | [Step 12](#step-12-the-supervisor-starts) |
| `TranscriptIndex.nearest_line` | function | 26 | [Step 12](#step-12-the-supervisor-starts) |
| `TranscriptIndex.window` | function | 38 | [Step 12](#step-12-the-supervisor-starts) |
| `TranscriptIndex.range` | function | 43 | [Step 12](#step-12-the-supervisor-starts) |
| `TranscriptIndex.search_lines` | function | 51 | [Step 12](#step-12-the-supervisor-starts) |
| `TranscriptIndex.fmt` | function | 64 | [Step 12](#step-12-the-supervisor-starts) |

### `callintel/redis_layer/client.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `Keys` | class | 31 | [Step 3](#step-3-connect-to-redis) |
| `Keys.__init__` | function | 32 | [Step 3](#step-3-connect-to-redis) |
| `Keys.manifest` | function | 42 | [Step 3](#step-3-connect-to-redis) |
| `Keys.lock` | function | 45 | [Step 3](#step-3-connect-to-redis) |
| `Keys.llm` | function | 48 | [Step 3](#step-3-connect-to-redis) |
| `Keys.emb` | function | 51 | [Step 3](#step-3-connect-to-redis) |
| `Keys.rl` | function | 54 | [Step 3](#step-3-connect-to-redis) |
| `Keys.vault` | function | 57 | [Step 3](#step-3-connect-to-redis) |
| `Keys.stats` | function | 60 | [Step 3](#step-3-connect-to-redis) |
| `RedisHandle` | class | 65 | [Step 3](#step-3-connect-to-redis) |
| `RedisHandle.close` | function | 70 | [Step 3](#step-3-connect-to-redis) |
| `connect_redis` | function | 77 | [Step 3](#step-3-connect-to-redis) |

### `callintel/redis_layer/ratelimit.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `_LocalBucket` | class | 40 | [Step 15](#step-15-inside-the-llm-gateway) |
| `_LocalBucket.__init__` | function | 41 | [Step 15](#step-15-inside-the-llm-gateway) |
| `_LocalBucket.take` | function | 44 | [Step 15](#step-15-inside-the-llm-gateway) |
| `RateLimiter` | class | 55 | [Step 15](#step-15-inside-the-llm-gateway) |
| `RateLimiter.__init__` | function | 58 | [Step 15](#step-15-inside-the-llm-gateway) |
| `RateLimiter.acquire` | function | 65 | [Step 15](#step-15-inside-the-llm-gateway) |
| `RateLimiter._take` | function | 79 | [Step 15](#step-15-inside-the-llm-gateway) |

### `callintel/redis_layer/store.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `JsonCache` | class | 18 | [Step 15](#step-15-inside-the-llm-gateway) |
| `JsonCache.__init__` | function | 19 | [Step 15](#step-15-inside-the-llm-gateway) |
| `JsonCache.get` | function | 22 | [Step 15](#step-15-inside-the-llm-gateway) |
| `JsonCache.set` | function | 26 | [Step 15](#step-15-inside-the-llm-gateway) |
| `RedisLock` | class | 30 | [Step 7](#step-7-process_file-opens-a-call) |
| `RedisLock.__init__` | function | 33 | [Step 7](#step-7-process_file-opens-a-call) |
| `RedisLock.acquire` | function | 38 | [Step 7](#step-7-process_file-opens-a-call) |
| `RedisLock.release` | function | 42 | [Step 7](#step-7-process_file-opens-a-call) |
| `RedisLock.__aenter__` | function | 53 | [Step 7](#step-7-process_file-opens-a-call) |
| `RedisLock.__aexit__` | function | 57 | [Step 7](#step-7-process_file-opens-a-call) |
| `Manifest` | class | 61 | [Step 7](#step-7-process_file-opens-a-call) |
| `Manifest.__init__` | function | 66 | [Step 5](#step-5-files-are-found-and-queued-ingest) |
| `Manifest.register` | function | 69 | [Step 5](#step-5-files-are-found-and-queued-ingest) |
| `Manifest.set` | function | 79 | [Step 7](#step-7-process_file-opens-a-call) |
| `Manifest.get` | function | 85 | [Step 7](#step-7-process_file-opens-a-call) |
| `Manifest.counts` | function | 88 | [Step 24](#step-24-after-processing) |
| `Metrics` | class | 101 | [Step 15](#step-15-inside-the-llm-gateway) |
| `Metrics.__init__` | function | 102 | [Step 15](#step-15-inside-the-llm-gateway) |
| `Metrics.incr` | function | 105 | [Step 15](#step-15-inside-the-llm-gateway) |
| `Metrics.get` | function | 114 | [Step 15](#step-15-inside-the-llm-gateway) |

### `callintel/redis_layer/streams.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `Message` | class | 19 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `StreamQueue` | class | 24 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `StreamQueue.__init__` | function | 25 | [Step 5](#step-5-files-are-found-and-queued-ingest) |
| `StreamQueue.ensure_group` | function | 31 | [Step 5](#step-5-files-are-found-and-queued-ingest) |
| `StreamQueue.enqueue` | function | 38 | [Step 5](#step-5-files-are-found-and-queued-ingest) |
| `StreamQueue._parse` | function | 42 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `StreamQueue.read` | function | 50 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `StreamQueue.reclaim` | function | 57 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `StreamQueue.ack` | function | 67 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `StreamQueue.retry_or_dead_letter` | function | 73 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |
| `StreamQueue.depth` | function | 86 | [Step 6](#step-6-workers-take-jobs-from-the-queue) |

### `callintel/redis_layer/vault.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `PIIVault` | class | 18 | [Step 22](#step-22-assemble-the-final-json) |
| `PIIVault.__init__` | function | 19 | [Step 22](#step-22-assemble-the-final-json) |
| `PIIVault.enabled` | function | 30 | [Step 22](#step-22-assemble-the-final-json) |
| `PIIVault.store` | function | 33 | [Step 22](#step-22-assemble-the-final-json) |
| `PIIVault.reveal` | function | 41 | [Step 22](#step-22-assemble-the-final-json) |
| `PIIVault.generate_key` | function | 48 | [Step 24](#step-24-after-processing) |

### `callintel/schemas.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `Taxonomy` | class | 19 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Taxonomy.load` | function | 39 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Taxonomy.labels_for` | function | 43 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Taxonomy.fact_type_guide` | function | 53 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `Taxonomy.all_labels` | function | 61 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `_L` | function | 68 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `ToolRequest` | class | 75 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `TextClaim` | class | 80 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `CriticVerdict` | class | 85 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `CriticOutput` | class | 91 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `LLMSchemas` | class | 95 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `LLMSchemas.__init__` | function | 98 | [Step 4](#step-4-the-engine-is-built-once-per-process) |
| `LLMSchemas.normalizer_output` | function | 156 | [Step 17](#step-17-normalise-product-names) |
| `Evidence` | class | 161 | [Step 22](#step-22-assemble-the-final-json) |
| `Claim` | class | 167 | [Step 22](#step-22-assemble-the-final-json) |
| `Labeled` | class | 173 | [Step 22](#step-22-assemble-the-final-json) |
| `SourceInfo` | class | 179 | [Step 22](#step-22-assemble-the-final-json) |
| `ProcessingInfo` | class | 191 | [Step 22](#step-22-assemble-the-final-json) |
| `PrivacyInfo` | class | 207 | [Step 22](#step-22-assemble-the-final-json) |
| `Sentiment` | class | 214 | [Step 22](#step-22-assemble-the-final-json) |
| `CallProfile` | class | 221 | [Step 22](#step-22-assemble-the-final-json) |
| `Normalization` | class | 231 | [Step 22](#step-22-assemble-the-final-json) |
| `ProductInsight` | class | 236 | [Step 22](#step-22-assemble-the-final-json) |
| `ObjectionInsight` | class | 250 | [Step 22](#step-22-assemble-the-final-json) |
| `CrossSellInsight` | class | 258 | [Step 22](#step-22-assemble-the-final-json) |
| `SalesInsights` | class | 266 | [Step 22](#step-22-assemble-the-final-json) |
| `CustomerInsights` | class | 275 | [Step 22](#step-22-assemble-the-final-json) |
| `RuleResult` | class | 282 | [Step 22](#step-22-assemble-the-final-json) |
| `RiskFlag` | class | 292 | [Step 22](#step-22-assemble-the-final-json) |
| `MisquotedTerm` | class | 300 | [Step 22](#step-22-assemble-the-final-json) |
| `ComplianceInsights` | class | 309 | [Step 22](#step-22-assemble-the-final-json) |
| `QuotedTerm` | class | 316 | [Step 22](#step-22-assemble-the-final-json) |
| `AgentPerformance` | class | 324 | [Step 22](#step-22-assemble-the-final-json) |
| `ReviewItem` | class | 332 | [Step 22](#step-22-assemble-the-final-json) |
| `FactOut` | class | 337 | [Step 22](#step-22-assemble-the-final-json) |
| `Quality` | class | 352 | [Step 22](#step-22-assemble-the-final-json) |
| `CallAnalysis` | class | 366 | [Step 22](#step-22-assemble-the-final-json) |

### `callintel/utils.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `sha256_bytes` | function | 21 | [Step 27](#step-27-shared-helpers) |
| `sha256_text` | function | 25 | [Step 27](#step-27-shared-helpers) |
| `sha256_file` | function | 29 | [Step 27](#step-27-shared-helpers) |
| `now_iso` | function | 37 | [Step 27](#step-27-shared-helpers) |
| `today` | function | 41 | [Step 27](#step-27-shared-helpers) |
| `dumps` | function | 45 | [Step 27](#step-27-shared-helpers) |
| `loads` | function | 50 | [Step 27](#step-27-shared-helpers) |
| `_default` | function | 54 | [Step 27](#step-27-shared-helpers) |
| `estimate_tokens` | function | 64 | [Step 11](#step-11-build-turns-and-chunks) |
| `atomic_write_text` | function | 87 | [Step 23](#step-23-scan-write-close) |
| `Stopwatch` | class | 104 | [Step 7](#step-7-process_file-opens-a-call) |
| `Stopwatch.__init__` | function | 105 | [Step 7](#step-7-process_file-opens-a-call) |
| `Stopwatch.stage` | function | 110 | [Step 7](#step-7-process_file-opens-a-call) |
| `Stopwatch.total_ms` | function | 117 | [Step 7](#step-7-process_file-opens-a-call) |
| `setup_logging` | function | 121 | [Step 27](#step-27-shared-helpers) |
| `get_logger` | function | 135 | [Step 27](#step-27-shared-helpers) |

### `callintel/verify/gate.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `ClaimRec` | class | 27 | [Step 21](#step-21-the-verification-loop) |
| `ClaimRec.confidence` | function | 46 | [Step 21](#step-21-the-verification-loop) |
| `ckey` | function | 52 | [Step 21](#step-21-the-verification-loop) |
| `ClaimCollector` | class | 56 | [Step 21](#step-21-the-verification-loop) |
| `ClaimCollector.__init__` | function | 59 | [Step 21](#step-21-the-verification-loop) |
| `ClaimCollector._add` | function | 66 | [Step 21](#step-21-the-verification-loop) |
| `ClaimCollector.collect` | function | 83 | [Step 21](#step-21-the-verification-loop) |
| `ClaimCollector._probe_missed` | function | 167 | [Step 21](#step-21-the-verification-loop) |
| `GateReport` | class | 183 | [Step 21](#step-21-the-verification-loop) |
| `GateReport.by_ref` | function | 189 | [Step 21](#step-21-the-verification-loop) |

### `callintel/verify/grounding.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `_norm` | function | 20 | [Step 16](#step-16-ground-check-and-de-duplicate-the-facts) |
| `quote_score` | function | 24 | [Step 16](#step-16-ground-check-and-de-duplicate-the-facts) |
| `ground_quote` | function | 35 | [Step 16](#step-16-ground-check-and-de-duplicate-the-facts) |
| `numbers_in` | function | 54 | [Step 16](#step-16-ground-check-and-de-duplicate-the-facts) |
| `number_supported` | function | 64 | [Step 16](#step-16-ground-check-and-de-duplicate-the-facts) |
| `confidence` | function | 80 | [Step 21](#step-21-the-verification-loop) |

### `evaluation/metrics.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `prf` | function | 17 | [Step 25](#step-25-the-evaluation-harness) |
| `micro_prf` | function | 27 | [Step 25](#step-25-the-evaluation-harness) |
| `accuracy` | function | 40 | [Step 25](#step-25-the-evaluation-harness) |
| `percentile` | function | 45 | [Step 25](#step-25-the-evaluation-harness) |
| `pii_variants` | function | 54 | [Step 25](#step-25-the-evaluation-harness) |
| `contains_pii` | function | 68 | [Step 25](#step-25-the-evaluation-harness) |
| `quote_grounded` | function | 79 | [Step 25](#step-25-the-evaluation-harness) |
| `mean_or_zero` | function | 88 | [Step 25](#step-25-the-evaluation-harness) |

### `evaluation/run_eval.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `run_pipeline` | function | 50 | [Step 25](#step-25-the-evaluation-harness) |
| `run_pipeline.one` | function | 58 | [Step 25](#step-25-the-evaluation-harness) |
| `load_predictions` | function | 75 | [Step 25](#step-25-the-evaluation-harness) |
| `_Judgement` | class | 87 | [Step 25](#step-25-the-evaluation-harness) |
| `judge_summaries` | function | 92 | [Step 25](#step-25-the-evaluation-harness) |
| `_walk_evidence` | function | 117 | [Step 25](#step-25-the-evaluation-harness) |
| `_enum_ok` | function | 132 | [Step 25](#step-25-the-evaluation-harness) |
| `score` | function | 147 | [Step 25](#step-25-the-evaluation-harness) |
| `score.pv` | function | 250 | [Step 25](#step-25-the-evaluation-harness) |
| `apply_gates` | function | 300 | [Step 25](#step-25-the-evaluation-harness) |
| `write_report` | function | 322 | [Step 25](#step-25-the-evaluation-harness) |
| `export_ragas` | function | 336 | [Step 25](#step-25-the-evaluation-harness) |
| `main` | function | 348 | [Step 25](#step-25-the-evaluation-harness) |

### `tests/conftest.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `settings` | function | 9 | [Step 26](#step-26-synthetic-data-and-tests) |

### `tests/test_gateway.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `_schemas` | function | 10 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_salvage_truncated_json` | function | 16 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_strict_schema_openai_vs_grammar` | function | 24 | [Step 26](#step-26-synthetic-data-and-tests) |

### `tests/test_normalize_and_grounding.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `_norm` | function | 7 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_tiers` | function | 14 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_learned_alias_shared_via_redis` | function | 29 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_catalog_spans_prefer_longest` | function | 39 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_grounding_helpers` | function | 46 | [Step 26](#step-26-synthetic-data-and-tests) |

### `tests/test_pii.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `masker` | function | 9 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_checksums` | function | 13 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_spoken_digits` | function | 22 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_structured_ids_masked` | function | 29 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_names_consistent_and_two_pass` | function | 42 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_dialogue_aware_answers` | function | 53 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_product_names_protected_from_name_detection` | function | 61 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_amounts_and_rates_not_masked` | function | 68 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_egress_scan_ignores_surrogates` | function | 73 | [Step 26](#step-26-synthetic-data-and-tests) |

### `tests/test_pipeline_e2e.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `test_process_file_end_to_end` | function | 14 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_run_local_queue` | function | 40 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_egress_guard_blocks_raw_pii` | function | 51 | [Step 26](#step-26-synthetic-data-and-tests) |

### `tests/test_preprocess.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `test_noise_cleaner_keeps_meaning` | function | 6 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_parse_roles_and_named_labels` | function | 14 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_repetition_compression_keeps_short_answers` | function | 26 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_chunking_respects_turns_and_overlap` | function | 40 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_chunk_turns_single_long_turn` | function | 54 | [Step 26](#step-26-synthetic-data-and-tests) |

### `tests/test_redis_layer.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `test_stream_ack_retry_dlq` | function | 10 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_reclaim_from_crashed_consumer` | function | 26 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_manifest_idempotency` | function | 37 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_lock_exclusive` | function | 46 | [Step 26](#step-26-synthetic-data-and-tests) |
| `test_rate_limiter_blocks_over_budget` | function | 55 | [Step 26](#step-26-synthetic-data-and-tests) |

### `tools/synth.py`

| Name | Kind | Line | Explained in |
|---|---|---|---|
| `luhn_card` | function | 50 | [Step 26](#step-26-synthetic-data-and-tests) |
| `aadhaar` | function | 62 | [Step 26](#step-26-synthetic-data-and-tests) |
| `spoken` | function | 70 | [Step 26](#step-26-synthetic-data-and-tests) |
| `CallBuilder` | class | 82 | [Step 26](#step-26-synthetic-data-and-tests) |
| `CallBuilder.__init__` | function | 83 | [Step 26](#step-26-synthetic-data-and-tests) |
| `CallBuilder._ts` | function | 89 | [Step 26](#step-26-synthetic-data-and-tests) |
| `CallBuilder.say` | function | 93 | [Step 26](#step-26-synthetic-data-and-tests) |
| `CallBuilder.noise_block` | function | 107 | [Step 26](#step-26-synthetic-data-and-tests) |
| `build_call` | function | 141 | [Step 26](#step-26-synthetic-data-and-tests) |
| `build_call.gap` | function | 160 | [Step 26](#step-26-synthetic-data-and-tests) |
| `build_call.mention` | function | 164 | [Step 26](#step-26-synthetic-data-and-tests) |
| `build_call.apr_disclosure` | function | 188 | [Step 26](#step-26-synthetic-data-and-tests) |
| `main` | function | 298 | [Step 26](#step-26-synthetic-data-and-tests) |


---

## Appendix B: design principles

These ideas appear again and again in the code. If an interviewer asks "why did you do X?", the answer is usually
one of them.

| # | Principle | Where you see it |
|---|---|---|
| 1 | **Deterministic first, LLM last.** Use code wherever the answer can be computed; use the LLM only for judgement. | Parsing, cleaning, masking, chunking, triage (Steps 8-13); grounding, speaker rules, number checks (Step 16); normaliser tiers T0-T2 (Step 17); contradiction probes (Step 21) |
| 2 | **Evidence or it did not happen.** Every fact has a line and a verbatim quote; every claim cites ids; code checks them; failures abstain. | Steps 14, 16, 19, 21, 22 |
| 3 | **Constrain the output space.** Enums from YAML, strict JSON schemas, grammar-constrained decoding, temperature 0, fixed seed. This is what makes 1,000s of outputs comparable. | Steps 4, 15 |
| 4 | **Privacy in depth.** Mask locally before any model → egress guard re-scan before sending → output scan after → leakage gate in evaluation. Original values are cleared from memory after use. | Steps 10, 15, 22, 23, 25 |
| 5 | **Idempotent and resumable.** Content hash identity, manifest, lock, at-least-once queue, retries, DLQ, atomic writes. A crash or re-run never produces duplicates or half files. | Steps 5, 6, 7, 23 |
| 6 | **Cheapest tier first, learn from the expensive tier.** Normaliser T0 → T1 → T2 → T3 with write-back; LLM response cache; embedding cache. | Steps 15, 17 |
| 7 | **Bounded agency.** Agents can route, look things up, verify and retry, but every loop has a fixed limit, so cost and latency are predictable. | Steps 19, 21 |
| 8 | **Configuration over code.** Models, providers, profiles, taxonomy, products, rules, prompts, thresholds and gates are YAML; code reads roles and ids. | Steps 2, 4, 18, 25 |
| 9 | **Parallel at three levels.** Processes (CPU), concurrent files per process (I/O), concurrent LLM calls per file, all governed by one semaphore per provider and one shared rate limiter. | Steps 6, 14, 15, 19 |
| 10 | **Measure everything.** Stage timings, token usage per role, cache hits, prompt versions, model names, drop counts and confidence are in every output; the evaluation harness turns them into gated metrics. | Steps 7, 15, 22, 25 |

### Honest trade-offs to mention in an interview

- **Fixed agent graph vs a fully autonomous planner.** The fixed graph is less flexible, but it is predictable in
  cost and latency and easy to audit, which matters more for 1,000+ regulated calls a day.
- **In-memory BM25/hybrid index vs a vector database.** Right for a knowledge base of hundreds of passages. For a
  cross-call search over millions of past calls, you would add Redis vector search or Qdrant behind the same
  `search()` interface.
- **Regex/cue-phrase masking vs NER only.** Regex and cues are fast and precise for structured and announced PII;
  NER (Presidio, GLiNER) adds recall for free-form names and addresses but is slower, so it is gated and optional.
  The evaluation gate (masking recall ≥ 0.98, leakage = 0) decides whether the current mix is good enough.
- **Chunked map-reduce vs one long-context call.** Map-reduce costs a few more requests and needs de-duplication,
  but it works on small local models, runs in parallel, and keeps every fact tied to a line. With a large cloud
  model you can raise the chunk size to reduce the number of calls.
- **Critic on high-risk claims only.** Checking every claim with an LLM would cost much more; low-risk claims are
  still grounded deterministically.
