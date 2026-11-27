"""Small shared helpers (hashing, timing, token estimates, atomic writes, logging)."""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import orjson

_TOKENIZER = None
_TOKENIZER_FAILED = False


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | os.PathLike, block: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def dumps(obj: Any, indent: bool = False) -> str:
    opt = orjson.OPT_NON_STR_KEYS | (orjson.OPT_INDENT_2 if indent else 0)
    return orjson.dumps(obj, option=opt, default=_default).decode("utf-8")


def loads(data: str | bytes) -> Any:
    return orjson.loads(data)


def _default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not serialisable: {type(obj)}")


def estimate_tokens(text: str) -> int:
    """Fast token estimate: chars/4 (good enough for chunk sizing on English speech).

    Set CALLINTEL_USE_TIKTOKEN=1 for exact o200k counts (tiktoken downloads its
    vocabulary on first use, which is not possible in air-gapped deployments).
    """
    global _TOKENIZER, _TOKENIZER_FAILED
    if not text:
        return 0
    if os.environ.get("CALLINTEL_USE_TIKTOKEN") != "1":
        return max(1, (len(text) + 3) // 4)
    if _TOKENIZER is None and not _TOKENIZER_FAILED:
        try:
            import tiktoken

            _TOKENIZER = tiktoken.get_encoding("o200k_base")
        except Exception:  # offline / not installed
            _TOKENIZER_FAILED = True
    if _TOKENIZER is not None:
        return len(_TOKENIZER.encode(text, disallowed_special=()))
    return max(1, len(text) // 4)


def atomic_write_text(path: str | os.PathLike, text: str) -> None:
    """Write via temp file + os.replace so readers never see a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.marks: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t = time.perf_counter()
        try:
            yield
        finally:
            self.marks[name] = round((time.perf_counter() - t) * 1000, 1)

    def total_ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 1)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if getattr(root, "_callintel", False):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(processName)s] %(name)s: %(message)s",
                                           "%H:%M:%S"))
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._callintel = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
