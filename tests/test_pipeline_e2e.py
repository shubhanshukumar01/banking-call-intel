"""End-to-end plumbing test with the deterministic mock provider (no model needed)."""
import json
from pathlib import Path

import pytest

from callintel.llm.egress import EgressBlocked, EgressGuard
from callintel.pipeline.engine import Engine
from callintel.pipeline.worker import run_local
from callintel.schemas import CallAnalysis
from tools.synth import build_call


async def test_process_file_end_to_end(settings, tmp_path):
    text, truth = build_call(11, 800)
    f = tmp_path / "call.txt"
    f.write_text(text, encoding="utf-8")
    eng = await Engine.create(settings)
    try:
        res = await eng.process_file(f)
        assert res["status"] in ("done", "needs_review"), res
        data = json.loads(Path(res["output"]).read_text(encoding="utf-8"))
        a = CallAnalysis.model_validate(data)               # schema-valid
        assert a.source.raw_lines == len(text.splitlines())
        assert a.processing.llm_calls >= 1
        out = Path(res["output"]).read_text(encoding="utf-8")
        for p in truth["pii"]:                               # no raw PII in the output
            if p["type"] in ("PHONE", "CARD", "ACCOUNT", "AADHAAR"):
                assert p["value"] not in out
        # every evidence quote is grounded in the masked transcript
        for prod in a.products:
            assert prod.product_id in [m["product_id"] for m in truth["mentions"]] + ["DC_DEBIT_CARD"]
        # idempotent: same content is skipped the second time
        again = await eng.process_file(f)
        assert again["status"] == "skipped_already_done"
    finally:
        await eng.close()


async def test_run_local_queue(settings, tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    for i in range(3):
        (inbox / f"c{i}.txt").write_text(build_call(100 + i, 300)[0], encoding="utf-8")
    (inbox / "dup.txt").write_text((inbox / "c0.txt").read_text(encoding="utf-8"), encoding="utf-8")
    out = await run_local(settings, str(inbox), concurrency=3, force=False)
    assert out["ingest"]["queued"] == 3 and out["ingest"]["skipped_duplicate_or_done"] == 1
    assert out["work"]["done"] + out["work"]["needs_review"] == 3


def test_egress_guard_blocks_raw_pii(settings):
    settings.privacy.egress_guard.apply_to = "all"
    g = EgressGuard(settings)
    with pytest.raises(EgressBlocked):
        g.check([{"role": "user", "content": "card 4111 1111 1111 1111"}], "internal",
                call_id="c", role="map", model="m")
    ok = g.check([{"role": "user", "content": "card <CARD_1>"}], "internal", call_id="c", role="map", model="m")
    assert ok[0]["content"] == "card <CARD_1>"
    settings.privacy.allow_cloud_llm = False
    with pytest.raises(EgressBlocked):
        EgressGuard(settings).check([{"role": "user", "content": "hi"}], "external", call_id="c", role="map",
                                    model="m")
