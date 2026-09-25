"""Prompt templates used by the LLM-backed guard stages (also reused for SFT data)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """Return the text of ``mailguard/prompts/<name>.txt`` (cached)."""
    path = PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")


__all__ = ["PROMPTS_DIR", "load_prompt"]
