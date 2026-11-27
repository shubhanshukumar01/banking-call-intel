"""Command line interface.

    python -m callintel doctor                         # check Redis, models, PII detectors
    python -m callintel run --input data/incoming      # ingest + process (local, one command)
    python -m callintel process path/to/call.txt       # single file, no queue
    python -m callintel ingest [--watch 10]            # enqueue new files (production)
    python -m callintel worker --processes 4 --concurrency 4
    python -m callintel status                         # queue depth, DLQ, manifest, today's metrics
    python -m callintel export-bi [--day 2026-11-27]   # Parquet fact tables for Power BI
    python -m callintel schema --out schemas/call_analysis.schema.json
Global: --config PATH   --profile local|local_installed|openai|vllm|mock
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import Settings, load_settings
from .utils import dumps, setup_logging


def _settings(args: argparse.Namespace) -> Settings:
    s = load_settings(args.config, args.profile)
    setup_logging(args.log_level or s.app.log_level)
    return s


async def _doctor(s: Settings) -> int:
    from .llm.gateway import LLMGateway
    from .llm.prompts import PromptRegistry
    from .normalize.catalog import ProductCatalog
    from .preprocess.pii import build_recognizers
    from .rag.knowledge_base import KnowledgeBase
    from .redis_layer.client import connect_redis

    ok = True
    print(f"config        : {s.config_path}")
    print(f"llm profile   : {s.llm.active_profile}")
    r = await connect_redis(s)
    info = "fakeredis (in-process, dev only) - see docs/REDIS_SETUP.md" if r.is_fake else s.redis.url
    if not r.is_fake:
        ver = str((await r.r.info("server")).get("redis_version", "0"))
        major_minor = tuple(int(x) for x in (ver.split(".") + ["0", "0"])[:2] if x.isdigit())
        if major_minor < (6, 2):
            ok = False
            info += f" (redis {ver}) -> FAIL: need >= 6.2 for Streams XAUTOCLAIM"
        else:
            info += f" (redis {ver}) -> OK"
    print(f"redis         : {info}")
    gw = LLMGateway(s, r)
    for role in ("map", "reduce", "critic", "normalizer", "embedding"):
        try:
            rc = s.role(role)
            good, why = await gw.ping_role(role)
            zone = s.provider(rc.provider).trust_zone
            print(f"llm.{role:<10}: {rc.provider}:{rc.model} [{zone}] -> {'OK' if good else 'FAIL: ' + why}")
            ok &= good or role == "embedding"
        except KeyError as exc:
            print(f"llm.{role:<10}: not configured ({exc})")
    cheap, ner = build_recognizers(s.privacy.detectors, s.privacy.presidio_model, s.privacy.gliner_model,
                                   s.privacy.gliner_threshold)
    print(f"pii detectors : {[x.name for x in cheap + ner]}  (configured: {s.privacy.detectors})")
    cat = ProductCatalog(s.path(s.paths.products_file))
    kb = KnowledgeBase(s, cat)
    print(f"catalogue     : {len(cat.products)} products, {len(cat.alias_map)} aliases")
    print(f"knowledge base: {len(kb.rules)} compliance rules, {len(kb.index.docs)} passages")
    print(f"prompts       : {PromptRegistry(s.path(s.paths.prompts_file)).versions()}")
    print(f"egress guard  : {s.privacy.egress_guard.action} on {s.privacy.egress_guard.apply_to}; "
          f"cloud allowed={s.privacy.allow_cloud_llm}")
    await gw.close()
    await r.close()
    print("\nRESULT:", "OK" if ok else "issues found (see FAIL lines)")
    return 0 if ok else 1


async def _status(s: Settings) -> None:
    from .redis_layer.client import connect_redis
    from .redis_layer.store import Manifest, Metrics
    from .redis_layer.streams import StreamQueue

    r = await connect_redis(s)
    k = r.keys
    q = StreamQueue(r.r, k.stream_ingest)
    out = {"redis": "fakeredis" if r.is_fake else s.redis.url,
           "ingest_queue": await q.depth(),
           "dlq": await r.r.xlen(k.stream_dlq), "review_queue": await r.r.xlen(k.stream_review),
           "alias_review_queue": await r.r.xlen(k.stream_norm_review),
           "learned_aliases": await r.r.hlen(k.alias),
           "manifest": await Manifest(r.r, k).counts(), "today": await Metrics(r.r, k).get()}
    print(json.dumps(out, indent=2))
    await r.close()


async def _aliases(s: Settings) -> None:
    from .redis_layer.client import connect_redis

    r = await connect_redis(s)
    data = await r.r.hgetall(r.keys.alias)
    print(json.dumps({a: json.loads(v) for a, v in sorted(data.items())}, indent=2))
    await r.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="callintel", description="Banking call transcript analytics (agentic RAG)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--profile", default=None, help="LLM profile override (local, openai, vllm, mock, ...)")
    ap.add_argument("--log-level", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor")
    p = sub.add_parser("run")
    p.add_argument("--input", default=None)
    p.add_argument("--processes", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("process")
    p.add_argument("file")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("ingest")
    p.add_argument("--input", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--watch", type=float, default=0, help="poll interval seconds (0 = scan once)")
    p = sub.add_parser("worker")
    p.add_argument("--processes", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--exit-when-idle", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("export-bi")
    p.add_argument("--day", default=None)
    sub.add_parser("aliases")
    p = sub.add_parser("schema")
    p.add_argument("--out", default=None)
    sub.add_parser("vault-key")

    args = ap.parse_args(argv)
    s = _settings(args)

    if args.cmd == "doctor":
        return asyncio.run(_doctor(s))
    if args.cmd == "run":
        from .pipeline.worker import enqueue_folder, run_local, run_worker_pool
        from .redis_layer.client import connect_redis

        procs = args.processes or s.workers.processes
        conc = args.concurrency or s.workers.files_per_process

        async def _probe() -> bool:
            r = await connect_redis(s)
            fake = r.is_fake
            if not fake:
                print(json.dumps({"ingest": await enqueue_folder(s, r, args.input, force=args.force)}))
            await r.close()
            return fake

        fake = asyncio.run(_probe())
        if fake or procs <= 1:
            print(json.dumps(asyncio.run(run_local(s, args.input, conc, args.force)), indent=2))
        else:
            run_worker_pool(s, procs, conc, exit_when_idle=True)
        return 0
    if args.cmd == "process":
        from .pipeline.engine import Engine

        async def _one() -> dict:
            eng = await Engine.create(s)
            try:
                return await eng.process_file(args.file, force=args.force)
            finally:
                await eng.close()
        print(json.dumps(asyncio.run(_one()), indent=2))
        return 0
    if args.cmd == "ingest":
        from .pipeline.worker import enqueue_folder, watch_folder
        from .redis_layer.client import connect_redis

        if args.watch:
            asyncio.run(watch_folder(s, args.input, args.watch))
            return 0

        async def _ing() -> dict:
            r = await connect_redis(s)
            if r.is_fake:
                print("WARNING: fakeredis is in-process; use `run` for local mode or start Redis.", file=sys.stderr)
            try:
                return await enqueue_folder(s, r, args.input, force=args.force, recursive=args.recursive)
            finally:
                await r.close()
        print(json.dumps(asyncio.run(_ing()), indent=2))
        return 0
    if args.cmd == "worker":
        from .pipeline.worker import run_worker_pool

        run_worker_pool(s, args.processes or s.workers.processes, args.concurrency or s.workers.files_per_process,
                        args.exit_when_idle)
        return 0
    if args.cmd == "status":
        asyncio.run(_status(s))
        return 0
    if args.cmd == "export-bi":
        from .output.sinks import export_bi

        print(json.dumps(export_bi(s, args.day), indent=2))
        return 0
    if args.cmd == "aliases":
        asyncio.run(_aliases(s))
        return 0
    if args.cmd == "schema":
        from .schemas import CallAnalysis

        text = json.dumps(CallAnalysis.model_json_schema(), indent=2)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            print(text)
        return 0
    if args.cmd == "vault-key":
        from .redis_layer.vault import PIIVault

        print(PIIVault.generate_key())
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
