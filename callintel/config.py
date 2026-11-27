"""Configuration loading: YAML + ${ENV:default} interpolation + .env support.

Usage:
    settings = load_settings()                 # config/config.yaml (or $CALLINTEL_CONFIG)
    settings.role("map")                       # resolved model config for a role
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        out = _ENV_PATTERN.sub(repl, value)
        return out
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow")


class AppCfg(_Section):
    pipeline_version: str = "3.0.0"
    schema_version: str = "3.0"
    log_level: str = "INFO"


class PathsCfg(_Section):
    input_dir: str = "data/incoming"
    output_dir: str = "data/output"
    audit_dir: str = "data/audit"
    knowledge_base_dir: str = "knowledge_base"
    products_file: str = "config/products.yaml"
    taxonomy_file: str = "config/taxonomy.yaml"
    prompts_file: str = "config/prompts.yaml"


class RedisCfg(_Section):
    url: str = "redis://localhost:6379/0"
    allow_fakeredis_fallback: bool = True
    key_prefix: str = "ci"
    llm_cache_ttl_s: int = 604800
    embedding_cache_ttl_s: int = 2592000
    stream_maxlen: int = 200000


class WorkersCfg(_Section):
    processes: int = 2
    files_per_process: int = 4
    max_attempts: int = 3
    visibility_timeout_s: int = 1800
    block_ms: int = 3000
    stable_file_age_s: int = 5


class EgressCfg(_Section):
    enabled: bool = True
    apply_to: str = "external"
    action: str = "block"


class VaultCfg(_Section):
    enabled: bool = False
    key_env: str = "PII_VAULT_KEY"
    ttl_s: int = 86400


class PrivacyCfg(_Section):
    allow_cloud_llm: bool = True
    detectors: list[str] = Field(default_factory=lambda: ["regex", "context"])
    presidio_model: str = "en_core_web_lg"
    gliner_model: str = "urchade/gliner_multi_pii-v1"
    gliner_threshold: float = 0.5
    allow_terms: list[str] = Field(default_factory=list)
    spoken_digits: bool = True
    egress_guard: EgressCfg = Field(default_factory=EgressCfg)
    vault: VaultCfg = Field(default_factory=VaultCfg)


class SpeakersCfg(_Section):
    agent: list[str] = Field(default_factory=list)
    customer: list[str] = Field(default_factory=list)
    system: list[str] = Field(default_factory=list)


class PreprocessCfg(_Section):
    speakers: SpeakersCfg = Field(default_factory=SpeakersCfg)
    drop_system_turns: bool = True
    keep_system_if_matches: list[str] = Field(default_factory=list)
    remove_fillers: bool = True
    collapse_repeats: bool = True
    drop_backchannels: bool = True
    merge_same_speaker: bool = True
    max_merged_chars: int = 1200
    max_repeats: int = 2            # keep a repeated utterance template at most N times (0 = off)
    repeat_min_words: int = 5       # shorter lines ("yes", "okay, go ahead") are never compressed


class ChunkingCfg(_Section):
    target_tokens: int = 2400
    max_tokens: int = 3000
    overlap_turns: int = 2


class TriageCfg(_Section):
    enabled: bool = True
    min_score: float = 2.0
    always_keep_first_last: bool = True


class AgentsCfg(_Section):
    max_facts_per_chunk: int = 25
    max_tool_rounds: int = 1
    critic_enabled: bool = True
    max_retries: int = 2
    min_confidence: float = 0.75
    grounding_min_score: float = 0.80
    max_evidence_per_agent: int = 120


class NormalizerCfg(_Section):
    t2_accept_score: float = 0.86
    t2_margin: float = 0.05
    t3_llm_enabled: bool = True
    t3_top_k: int = 5


class RagCfg(_Section):
    kb_top_k: int = 4
    dense_enabled: str | bool = "auto"
    normalizer: NormalizerCfg = Field(default_factory=NormalizerCfg)


class OutputCfg(_Section):
    write_per_call_json: bool = True
    write_jsonl: bool = True
    scan_output_for_pii: bool = True
    include_facts: bool = True


class AuditCfg(_Section):
    capture_llm_payloads: bool = False


class ProviderCfg(_Section):
    kind: str
    base_url: str | None = None
    api_key: str | None = None
    trust_zone: str = "internal"
    max_concurrency: int = 4
    rpm: int | None = None
    tpm: int | None = None
    timeout_s: float = 300


class RoleCfg(_Section):
    provider: str
    model: str
    max_tokens: int | None = None
    temperature: float | None = 0.0
    num_ctx: int | None = None
    think: bool | None = None
    reasoning_effort: str | None = None
    seed: int | None = 7


class LLMCfg(_Section):
    active_profile: str = "local"
    providers: dict[str, ProviderCfg] = Field(default_factory=dict)
    profiles: dict[str, dict[str, RoleCfg]] = Field(default_factory=dict)
    pricing: dict[str, dict[str, float]] = Field(default_factory=dict)


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")

    app: AppCfg = Field(default_factory=AppCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    redis: RedisCfg = Field(default_factory=RedisCfg)
    workers: WorkersCfg = Field(default_factory=WorkersCfg)
    privacy: PrivacyCfg = Field(default_factory=PrivacyCfg)
    preprocess: PreprocessCfg = Field(default_factory=PreprocessCfg)
    chunking: ChunkingCfg = Field(default_factory=ChunkingCfg)
    triage: TriageCfg = Field(default_factory=TriageCfg)
    agents: AgentsCfg = Field(default_factory=AgentsCfg)
    rag: RagCfg = Field(default_factory=RagCfg)
    output: OutputCfg = Field(default_factory=OutputCfg)
    audit: AuditCfg = Field(default_factory=AuditCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)

    config_path: str | None = None

    # ------------------------------------------------------------------ helpers
    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def profile(self) -> dict[str, RoleCfg]:
        name = self.llm.active_profile or "local"
        if name not in self.llm.profiles:
            raise KeyError(f"LLM profile '{name}' not found; available: {list(self.llm.profiles)}")
        return self.llm.profiles[name]

    def role(self, role: str) -> RoleCfg:
        prof = self.profile()
        if role not in prof:
            # reduce is the fallback for any reasoning role that is not configured
            if role in ("critic", "normalizer") and "reduce" in prof:
                return prof["reduce"]
            raise KeyError(f"Role '{role}' not defined in profile '{self.llm.active_profile}'")
        return prof[role]

    def provider(self, name: str) -> ProviderCfg:
        if name not in self.llm.providers:
            raise KeyError(f"Provider '{name}' not configured")
        return self.llm.providers[name]


def load_yaml(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_settings(config_path: str | os.PathLike | None = None, profile: str | None = None) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    path = Path(config_path or os.environ.get("CALLINTEL_CONFIG") or PROJECT_ROOT / "config" / "config.yaml")
    if not path.is_absolute() and not path.exists():
        path = PROJECT_ROOT / path
    raw = _interpolate(load_yaml(path) or {})
    settings = Settings.model_validate(raw)
    settings.config_path = str(path)
    if profile:
        settings.llm.active_profile = profile
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
