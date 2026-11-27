"""Optional reversible pseudonymisation vault (encrypted, short TTL).

Off by default: analytics never needs real PII. When enabled, surrogate -> original
values are Fernet-encrypted with a key held outside Redis (env var) and expire
after `ttl_s`. Only `reveal()` with the key can recover a value.
"""
from __future__ import annotations

import os

import redis.asyncio as aioredis
from cryptography.fernet import Fernet

from ..config import VaultCfg
from .client import Keys


class PIIVault:
    def __init__(self, r: aioredis.Redis, keys: Keys, cfg: VaultCfg) -> None:
        self.r, self.k, self.cfg = r, keys, cfg
        self._fernet: Fernet | None = None
        if cfg.enabled:
            key = os.environ.get(cfg.key_env)
            if not key:
                raise RuntimeError(f"privacy.vault.enabled=true but env {cfg.key_env} is not set "
                                   "(generate one: python -m callintel.cli vault-key)")
            self._fernet = Fernet(key.encode())

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    async def store(self, call_id: str, mapping: dict[str, str]) -> None:
        if not self._fernet or not mapping:
            return
        enc = {sur: self._fernet.encrypt(val.encode()).decode() for sur, val in mapping.items()}
        key = self.k.vault(call_id)
        await self.r.hset(key, mapping=enc)
        await self.r.expire(key, self.cfg.ttl_s)

    async def reveal(self, call_id: str, surrogate: str) -> str | None:
        if not self._fernet:
            return None
        token = await self.r.hget(self.k.vault(call_id), surrogate)
        return None if token is None else self._fernet.decrypt(token.encode()).decode()

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()
