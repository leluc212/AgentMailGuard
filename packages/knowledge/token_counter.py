"""Token counter abstraction for knowledge chunking and budget calculations.

Requirements:
- R9.4: Target 350-700 tokens per chunk with configurable overlap.
- R9.11: Track embedding tokens for cost accounting.
- specs/design.md §5.6: BPE token counting matching modern embedding models.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ENCODING = "cl100k_base"


class TokenCounter:
    """Accurate BPE token counter and text truncator backed by tiktoken."""

    def __init__(self, encoding_name: str = DEFAULT_ENCODING) -> None:
        self.encoding_name = encoding_name
        self._encoding: Any = None
        try:
            import tiktoken

            self._encoding = tiktoken.get_encoding(encoding_name)
        except Exception as err:
            logger.warning(
                "Failed to initialize tiktoken encoding '%s': %s. Falling back to heuristic.",
                encoding_name,
                err,
            )
            self._encoding = None

    @property
    def uses_bpe(self) -> bool:
        """True when counts come from the BPE encoding, False when they are the word heuristic.

        The heuristic is the fallback for an encoding that could not be loaded (no cache and no
        network). Its numbers differ from the BPE's, so a caller that must count as another
        process does checks this instead of assuming.
        """
        return self._encoding is not None

    def count_tokens(self, text: str) -> int:
        """Count the number of tokens in the provided text string."""
        if not text:
            return 0

        if self._encoding is not None:
            try:
                return len(self._encoding.encode(text, disallowed_special=()))
            except Exception:
                pass

        # Resilient fallback: approx 1.33 tokens per whitespace word or 4 chars/token
        words = text.split()
        return max(1, int(len(words) * 1.33)) if words else 0

    def truncate_tokens(self, text: str, max_tokens: int) -> str:
        """Truncate text to at most max_tokens without breaking token boundaries."""
        if not text or max_tokens <= 0:
            return ""

        if self._encoding is not None:
            try:
                tokens = self._encoding.encode(text, disallowed_special=())
                if len(tokens) <= max_tokens:
                    return text
                return str(self._encoding.decode(tokens[:max_tokens]))
            except Exception:
                pass

        # Fallback truncation by words
        words = text.split()
        target_words = int(max_tokens / 1.33)
        return " ".join(words[:target_words])

    def encode(self, text: str) -> list[int]:
        """Encode text to token IDs."""
        if not text:
            return []
        if self._encoding is not None:
            return list(self._encoding.encode(text, disallowed_special=()))
        return list(range(self.count_tokens(text)))

    def decode(self, tokens: list[int]) -> str:
        """Decode token IDs back to string."""
        if not tokens:
            return ""
        if self._encoding is not None:
            return str(self._encoding.decode(tokens))
        return ""
