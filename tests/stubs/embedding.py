"""Stub embedding generator for hermetic, credential-free testing (R24.5).

Produces deterministic, unit-normalized 1536-dimensional vector representations
from input text without external network requests or embedding API keys.
"""

import hashlib
import math


class StubEmbedder:
    """Deterministic embedding generator test double.

    Requires zero live credentials. Produces deterministic L2-normalized
    vectors of the configured dimension (default: 1536).
    """

    def __init__(self, dimension: int = 1536) -> None:
        self.dimension = dimension
        self.call_count = 0

    def embed_text(self, text: str) -> list[float]:
        """Convert input text to a deterministic, L2-normalized float vector."""
        self.call_count += 1

        # Seed pseudo-random stream using SHA256 of the input text
        hasher = hashlib.sha256(text.encode("utf-8"))
        seed_bytes = hasher.digest()

        # Generate dimension floats deterministically
        raw_values: list[float] = []
        for i in range(self.dimension):
            # Sample byte cycling across digest with index offset
            b = seed_bytes[(i + (i // len(seed_bytes))) % len(seed_bytes)]
            val = (b / 127.5) - 1.0  # Normalize to [-1.0, 1.0]
            # Add positional harmonic perturbation
            val += 0.1 * math.sin(i * 0.1)
            raw_values.append(val)

        # L2-normalize vector to unit length
        norm = math.sqrt(sum(x * x for x in raw_values))
        if norm == 0:
            return [0.0] * self.dimension

        return [x / norm for x in raw_values]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch generate embeddings for multiple text strings."""
        return [self.embed_text(t) for t in texts]

    def cosine_similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        """Compute dot product of two unit-normalized vectors."""
        if len(vec_a) != len(vec_b):
            raise ValueError(f"Vector dimension mismatch: {len(vec_a)} != {len(vec_b)}")
        return sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
