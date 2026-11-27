"""Configurable LLM gateway: one interface for Ollama, OpenAI, vLLM/OpenAI-compatible and a mock.

Per request it applies, in order:
  1. egress guard (PII re-scan; cloud kill-switch)
  2. exact-match Redis cache (content-addressed: provider+model+params+schema+messages)
  3. per-provider concurrency semaphore + distributed RPM/TPM rate limiter
  4. JSON-schema constrained decoding (Ollama `format`, OpenAI strict json_schema)
  5. transient-error retries with exponential backoff + jitter
  6. pydantic validation, one self-repair re-ask on validation failure
  7. usage accounting (per call via contextvars, per day in Redis)

Why Ollama's native /api/chat instead of its OpenAI-compatible endpoint: the compat
endpoint cannot set num_ctx, so long chunks are silently truncated at the default
context size. The native API also accepts a JSON schema and think=false.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Generic, Iterator, TypeVar

import httpx
import numpy as np
from pydantic import BaseModel, ValidationError

from ..config import ProviderCfg, RoleCfg, Settings
from ..redis_layer.client import RedisHandle
from ..redis_layer.ratelimit import RateLimiter
from ..redis_layer.store import JsonCache, Metrics
from ..utils import dumps, estimate_tokens, sha256_text
from .egress import EgressBlocked, EgressGuard
from .schema_utils import minimal_instance, schema_hash, strict_schema

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class TransientLLMError(LLMError):
    pass


# ----------------------------------------------------------------------------- usage accounting
@dataclass
class Usage:
    calls: int = 0
    cache_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    repairs: int = 0
    by_role: dict[str, dict[str, float]] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)

    def add(self, role: str, model: str, tin: int, tout: int, ms: float, cached: bool) -> None:
        self.models[role] = model
        r = self.by_role.setdefault(role, {"calls": 0, "cache_hits": 0, "tokens_in": 0, "tokens_out": 0, "ms": 0.0})
        if cached:
            self.cache_hits += 1
            r["cache_hits"] += 1
            return
        self.calls += 1
        self.tokens_in += tin
        self.tokens_out += tout
        self.latency_ms += ms
        r["calls"] += 1
        r["tokens_in"] += tin
        r["tokens_out"] += tout
        r["ms"] = round(r["ms"] + ms, 1)


_USAGE: ContextVar[Usage | None] = ContextVar("callintel_usage", default=None)
_CALL_ID: ContextVar[str | None] = ContextVar("callintel_call_id", default=None)


@contextmanager
def usage_scope(call_id: str | None = None) -> Iterator[Usage]:
    u = Usage()
    t1, t2 = _USAGE.set(u), _CALL_ID.set(call_id)
    try:
        yield u
    finally:
        _USAGE.reset(t1)
        _CALL_ID.reset(t2)


@dataclass
class LLMResult(Generic[T]):
    data: T
    model: str
    cached: bool
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0


# ----------------------------------------------------------------------------- providers
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _extract_json(text: str) -> str:
    text = _THINK_RE.sub("", text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    if not text.startswith("{"):
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            text = text[s:e + 1]
    return text


def salvage_truncated_json(text: str) -> str | None:
    """Recover the complete items of a JSON answer cut off by the token limit:
    '{"facts":[{..},{..},{"type":"x","lab' -> '{"facts":[{..},{..}]}'."""
    s = _extract_json(text)
    stack: list[str] = []
    in_str = esc = False
    cut: tuple[int, list[str]] | None = None
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
            if ch == "}" and stack and stack[-1] == "[":
                cut = (i + 1, list(stack))
    if cut is None or not stack:  # nothing complete, or not truncated at all
        return None
    pos, st = cut
    return s[:pos] + "".join("}" if c == "{" else "]" for c in reversed(st))


class OllamaProvider:
    def __init__(self, cfg: ProviderCfg) -> None:
        self.cfg = cfg
        self.client = httpx.AsyncClient(base_url=cfg.base_url or "http://localhost:11434",
                                        timeout=httpx.Timeout(cfg.timeout_s, connect=10))
        self._no_think: set[str] = set()

    async def chat(self, rc: RoleCfg, messages: list[dict], schema: dict, schema_name: str,
                   max_tokens: int | None) -> tuple[str, int, int]:
        options = {"temperature": 0.0 if rc.temperature is None else rc.temperature, "num_ctx": rc.num_ctx,
                   "num_predict": max_tokens, "seed": rc.seed}
        payload: dict[str, Any] = {"model": rc.model, "messages": messages, "stream": False, "format": schema,
                                   "options": {k: v for k, v in options.items() if v is not None},
                                   "keep_alive": "30m"}
        if rc.think is not None and rc.model not in self._no_think:
            payload["think"] = rc.think
        try:
            r = await self.client.post("/api/chat", json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientLLMError(f"ollama transport: {exc!r}") from exc
        if r.status_code == 400 and "think" in r.text.lower() and "think" in payload:
            self._no_think.add(rc.model)  # model has no thinking switch
            payload.pop("think")
            r = await self.client.post("/api/chat", json=payload)
        if r.status_code in (429, 500, 502, 503, 504):
            raise TransientLLMError(f"ollama HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            raise LLMError(f"ollama HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        content = data.get("message", {}).get("content", "")
        return content, int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        r = await self.client.post("/api/embed", json={"model": model, "input": texts, "keep_alive": "30m"})
        r.raise_for_status()
        return r.json()["embeddings"]

    async def ping(self, model: str | None = None) -> tuple[bool, str]:
        try:
            r = await self.client.get("/api/tags")
            r.raise_for_status()
            names = {m["name"] for m in r.json().get("models", [])}
            if model and model not in names and f"{model}:latest" not in names:
                return False, f"model '{model}' not pulled (run: ollama pull {model})"
            return True, "ok"
        except Exception as exc:
            return False, f"unreachable: {exc.__class__.__name__}"

    async def close(self) -> None:
        await self.client.aclose()


class OpenAIProvider:
    """OpenAI and any OpenAI-compatible server (vLLM, LiteLLM proxy, LM Studio)."""

    def __init__(self, cfg: ProviderCfg) -> None:
        from openai import AsyncOpenAI

        self.cfg = cfg
        self.is_openai = cfg.kind == "openai"
        if self.is_openai and not cfg.api_key:
            log.warning("OpenAI provider configured without api_key (set OPENAI_API_KEY in .env or config)")
        self.client = AsyncOpenAI(api_key=cfg.api_key or "EMPTY", base_url=cfg.base_url or None,
                                  timeout=cfg.timeout_s, max_retries=0)

    async def chat(self, rc: RoleCfg, messages: list[dict], schema: dict, schema_name: str,
                   max_tokens: int | None) -> tuple[str, int, int]:
        import openai

        kw: dict[str, Any] = {"model": rc.model, "messages": messages,
                              "response_format": {"type": "json_schema",
                                                  "json_schema": {"name": schema_name, "schema": schema,
                                                                  "strict": True}}}
        if max_tokens:
            kw["max_completion_tokens" if self.is_openai else "max_tokens"] = max_tokens
        if rc.temperature is not None:
            kw["temperature"] = rc.temperature
        if rc.reasoning_effort:
            kw["reasoning_effort"] = rc.reasoning_effort
        if rc.seed is not None and not self.is_openai:
            kw["seed"] = rc.seed
        try:
            resp = await self.client.chat.completions.create(**kw)
        except (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError,
                openai.InternalServerError) as exc:
            raise TransientLLMError(f"{exc.__class__.__name__}: {exc}") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"{exc.__class__.__name__}: {exc}") from exc
        msg = resp.choices[0].message
        if getattr(msg, "refusal", None):
            raise LLMError(f"model refusal: {msg.refusal[:200]}")
        u = resp.usage
        return msg.content or "", int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0)

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        resp = await self.client.embeddings.create(model=model, input=texts)
        return [d.embedding for d in resp.data]

    async def ping(self, model: str | None = None) -> tuple[bool, str]:
        try:
            if model:
                await self.client.models.retrieve(model)
            else:
                await self.client.models.list()
            return True, "ok"
        except Exception as exc:
            return False, f"{exc.__class__.__name__}: {str(exc)[:120]}"

    async def close(self) -> None:
        await self.client.close()


class MockProvider:
    """Deterministic offline stand-in for CI and plumbing tests (NOT for analytics).

    Map step: keyword rules over the chunk lines produce a few grounded facts so the
    downstream agents, verification and output stages are exercised end to end.
    Other steps: the minimal schema-valid answer ("unclear", empty lists).
    """

    _RULES = [
        (re.compile(r"\b(too (?:high|much|expensive)|expensive|costly)\b", re.I), "objection", "fees_too_high"),
        (re.compile(r"\b(interested|sounds good|go ahead|sign me up|apply)\b", re.I), "buying_signal", "other"),
        (re.compile(r"\b(recorded|monitored)\b", re.I), "compliance", "other"),
        (re.compile(r"\b(complain|complaint|charged twice|unhappy|frustrated)\b", re.I), "complaint", "unexpected_charges"),
        (re.compile(r"\d+(?:\.\d+)?\s?%|\brs\.? ?\d[\d,]*", re.I), "quoted_term", "other"),
        (re.compile(r"\b(date of birth|verify|verification)\b", re.I), "identity_check", "other"),
    ]
    _PRODUCT = re.compile(r"\b((?:\w+\s){0,2}(?:card|loan|deposit|account|insurance|sip))\b", re.I)
    _LINE = re.compile(r"^(\d+)\|([AC?S]): (.*)$")

    def __init__(self, cfg: ProviderCfg) -> None:
        self.cfg = cfg

    async def chat(self, rc: RoleCfg, messages: list[dict], schema: dict, schema_name: str,
                   max_tokens: int | None) -> tuple[str, int, int]:
        if schema_name == "MapOutput":
            facts = []
            for raw in messages[-1]["content"].splitlines():
                m = self._LINE.match(raw.strip())
                if not m:
                    continue
                line, text = int(m.group(1)), m.group(3)
                words = text.split()
                quote = " ".join(words[:20])
                pm = self._PRODUCT.search(text)
                if pm:
                    facts.append({"type": "product_mention", "label": "other", "value": f"mentions {pm.group(1)}",
                                  "product": pm.group(1), "number": None, "line": line, "quote": quote})
                for rx, ftype, label in self._RULES:
                    mm = rx.search(text)
                    if mm:
                        facts.append({"type": ftype, "label": label, "value": text[:80], "product": None,
                                      "number": mm.group(0) if ftype == "quoted_term" else None,
                                      "line": line, "quote": quote})
            return dumps({"facts": facts[:25]}), 0, 0
        return dumps(minimal_instance(schema)), 0, 0

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            rng = np.random.default_rng(int(sha256_text(t)[:8], 16))
            out.append(rng.standard_normal(64).tolist())
        return out

    async def ping(self, model: str | None = None) -> tuple[bool, str]:
        return True, "mock"

    async def close(self) -> None:
        return None


_PROVIDERS = {"ollama": OllamaProvider, "openai": OpenAIProvider, "openai_compatible": OpenAIProvider,
              "mock": MockProvider}


# ----------------------------------------------------------------------------- gateway
class LLMGateway:
    def __init__(self, settings: Settings, redis: RedisHandle) -> None:
        self.s = settings
        self.redis = redis
        self.cache = JsonCache(redis.r)
        self.metrics = Metrics(redis.r, redis.keys)
        self.limiter = RateLimiter(redis.r, redis.keys.rl)
        self.egress = EgressGuard(settings)
        self._providers: dict[str, Any] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        # Ollama reloads a model whenever num_ctx changes. Roles sharing one model must use one
        # context size, or every map->reduce switch costs a full reload (and can crash a tight GPU).
        self._ctx: dict[tuple[str, str], int] = {}
        for rc in settings.profile().values():
            if rc.num_ctx:
                k = (rc.provider, rc.model)
                self._ctx[k] = max(self._ctx.get(k, 0), rc.num_ctx)

    def provider(self, name: str) -> Any:
        if name not in self._providers:
            cfg = self.s.provider(name)
            self._providers[name] = _PROVIDERS[cfg.kind](cfg)
        return self._providers[name]

    def _sem(self, name: str) -> asyncio.Semaphore:
        if name not in self._sems:
            self._sems[name] = asyncio.Semaphore(max(1, self.s.provider(name).max_concurrency))
        return self._sems[name]

    def role_model(self, role: str) -> str:
        rc = self.s.role(role)
        return f"{rc.provider}:{rc.model}"

    async def structured(self, role: str, messages: list[dict[str, str]], schema_model: type[T], *,
                         max_tokens: int | None = None, use_cache: bool = True) -> LLMResult[T]:
        rc = self.s.role(role)
        if rc.num_ctx and self._ctx.get((rc.provider, rc.model), rc.num_ctx) != rc.num_ctx:
            rc = rc.model_copy(update={"num_ctx": self._ctx[(rc.provider, rc.model)]})
        pc = self.s.provider(rc.provider)
        call_id = _CALL_ID.get()
        messages = self.egress.check(messages, pc.trust_zone, call_id=call_id, role=role, model=rc.model)
        # grammar-based decoders (Ollama) can enforce array limits at generation time
        schema = strict_schema(schema_model, keep_limits=pc.kind == "ollama")
        name = schema_model.__name__
        max_tokens = max_tokens or rc.max_tokens
        key = self.redis.keys.llm(sha256_text(dumps([
            rc.provider, rc.model, rc.temperature, rc.reasoning_effort, rc.num_ctx, rc.think, max_tokens,
            schema_hash(schema_model), messages])))
        usage = _USAGE.get()

        if use_cache:
            hit = await self.cache.get(key)
            if hit is not None:
                try:
                    obj = schema_model.model_validate(hit)
                    if usage:
                        usage.add(role, rc.model, 0, 0, 0, cached=True)
                    await self.metrics.incr(llm_cache_hits=1)
                    return LLMResult(obj, rc.model, True)
                except ValidationError:
                    pass  # schema changed - recompute

        prov = self.provider(rc.provider)
        est_tokens = sum(estimate_tokens(m["content"]) for m in messages) + (max_tokens or 512)
        t0 = time.perf_counter()
        tin = tout = 0
        obj: T | None = None
        msgs = list(messages)
        async with self._sem(rc.provider):
            await self.limiter.acquire(f"{rc.provider}:rpm", pc.rpm, 1)
            await self.limiter.acquire(f"{rc.provider}:tpm", pc.tpm, est_tokens)
            for repair in range(2):
                content, a, b = await self._call_with_retries(prov, rc, msgs, schema, name, max_tokens)
                tin, tout = tin + a, tout + b
                try:
                    obj = schema_model.model_validate_json(_extract_json(content))
                    break
                except ValidationError as exc:
                    truncated = "EOF while parsing" in str(exc) or "json_invalid" in str(exc)
                    salvaged = salvage_truncated_json(content) if truncated else None
                    if salvaged:
                        try:  # keep every complete item instead of paying for a full re-generation
                            obj = schema_model.model_validate_json(salvaged)
                            if usage:
                                usage.repairs += 1
                            log.info("%s: salvaged truncated output (%d chars)", name, len(salvaged))
                            break
                        except ValidationError:
                            pass
                    if repair == 1:
                        raise LLMError(f"{name}: invalid output after repair: {str(exc)[:300]}") from exc
                    if usage:
                        usage.repairs += 1
                    if truncated:
                        fix = ("Your previous answer was cut off by the length limit. Answer again with FEWER "
                               "items and shorter text. Valid JSON only.")
                        msgs = list(messages) + [{"role": "user", "content": fix}]
                    else:
                        msgs = msgs + [{"role": "assistant", "content": content[:4000]},
                                       {"role": "user", "content": "Your JSON failed validation: "
                                        f"{str(exc)[:600]}\nReturn the corrected JSON only."}]
        ms = (time.perf_counter() - t0) * 1000
        assert obj is not None
        if use_cache:
            await self.cache.set(key, obj.model_dump(mode="json"), self.s.redis.llm_cache_ttl_s)
        if usage:
            usage.add(role, rc.model, tin, tout, ms, cached=False)
        await self.metrics.incr(llm_calls=1, tokens_in=tin, tokens_out=tout)
        return LLMResult(obj, rc.model, False, tin, tout, round(ms, 1))

    async def _call_with_retries(self, prov: Any, rc: RoleCfg, msgs: list[dict], schema: dict, name: str,
                                 max_tokens: int | None, attempts: int = 4) -> tuple[str, int, int]:
        delay = 1.0
        for i in range(attempts):
            try:
                return await prov.chat(rc, msgs, schema, name, max_tokens)
            except TransientLLMError as exc:
                if i == attempts - 1:
                    raise
                log.warning("transient LLM error (%s), retry %d/%d in %.1fs", exc, i + 1, attempts - 1, delay)
                await asyncio.sleep(delay + random.random() * 0.5)
                delay = min(delay * 2, 30)
        raise LLMError("unreachable")

    async def embed(self, texts: list[str], role: str = "embedding") -> np.ndarray:
        rc = self.s.role(role)
        pc = self.s.provider(rc.provider)
        if pc.trust_zone == "external":
            self.egress.check([{"role": "user", "content": t} for t in texts], pc.trust_zone,
                              call_id=_CALL_ID.get(), role=role, model=rc.model)
        keys = [self.redis.keys.emb(rc.model, sha256_text(t)) for t in texts]
        cached = await self.redis.r.mget(keys) if keys else []
        vecs: list[list[float] | None] = [None if c is None else list(map(float, c.split(","))) for c in cached]
        missing = [i for i, v in enumerate(vecs) if v is None]
        prov = self.provider(rc.provider)
        for b in range(0, len(missing), 64):
            idx = missing[b:b + 64]
            async with self._sem(rc.provider):
                out = await prov.embed(rc.model, [texts[i] for i in idx])
            pipe = self.redis.r.pipeline(transaction=False)
            for i, v in zip(idx, out):
                vecs[i] = v
                pipe.set(keys[i], ",".join(f"{x:.6g}" for x in v), ex=self.s.redis.embedding_cache_ttl_s)
            await pipe.execute()
        arr = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.maximum(norms, 1e-9)

    async def ping_role(self, role: str) -> tuple[bool, str]:
        try:
            rc = self.s.role(role)
        except KeyError as exc:
            return False, str(exc)
        return await self.provider(rc.provider).ping(rc.model)

    async def close(self) -> None:
        for p in self._providers.values():
            await p.close()


__all__ = ["LLMGateway", "LLMResult", "LLMError", "EgressBlocked", "Usage", "usage_scope"]
