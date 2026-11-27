import uuid

import pytest

from callintel.config import load_settings


@pytest.fixture
def settings(tmp_path):
    """Mock-LLM settings, isolated fakeredis keyspace and output folder per test."""
    s = load_settings(profile="mock")
    s.redis.url = "redis://127.0.0.1:1/0"          # unreachable -> fakeredis fallback
    s.redis.allow_fakeredis_fallback = True
    s.redis.key_prefix = f"t{uuid.uuid4().hex[:8]}"
    s.paths.output_dir = str(tmp_path / "out")
    s.paths.audit_dir = str(tmp_path / "audit")
    s.privacy.detectors = ["regex", "context"]
    s.rag.dense_enabled = False
    s.workers.stable_file_age_s = 0
    return s
