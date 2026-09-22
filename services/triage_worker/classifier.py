"""Lightweight ML classifier engine for Stage 2 email triage (R6.1, NFR3, design.md §5.3).

Executes fast (20-50ms budget), calibrated inference using a serialized TF-IDF +
linear classification pipeline, returning structured Classification domain entities.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

import joblib
from sklearn.pipeline import Pipeline

from packages.domain.entities import Classification, NormalizedMessage
from packages.domain.rules import EmailContext
from packages.domain.taxonomy import (
    NO_REPLY_CATEGORIES,
    RETRIEVAL_CATEGORIES,
    normalize_category,
)
from services.triage_worker.training import DEFAULT_MODEL_PATH, prepare_text

logger = logging.getLogger(__name__)

# Common regex pattern for high urgency indicators
URGENT_PATTERN = re.compile(
    r"\b(urgent|urgently|asap|emergency|critical|immediately|deadline|high priority)\b",
    re.IGNORECASE,
)


class MLClassifier:
    """Stage 2 ML triage classifier executing sub-50ms inference.

    Loads a versioned scikit-learn model artifact and predicts category, confidence,
    priority, reply requirement, workflow hint, and retrieval requirement.
    """

    def __init__(
        self,
        pipeline: Pipeline,
        metadata: dict[str, Any] | None = None,
        model_path: Path | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._metadata = metadata or {}
        self._model_path = model_path
        self._model_name: str = str(self._metadata.get("model_name") or "tfidf-logistic-v1")
        self._model_version: str = str(self._metadata.get("model_version") or "1.0.0")

        # Cache class names from the underlying estimator
        clf = self._pipeline.named_steps.get("clf")
        if clf is not None and hasattr(clf, "classes_"):
            self._classes = [str(c) for c in clf.classes_]
        else:
            self._classes = []

    @classmethod
    def load_from_artifact(cls, path: str | Path | None = None) -> MLClassifier:
        """Load an MLClassifier from a serialized joblib model bundle.

        Args:
            path: Path to the joblib artifact file (defaults to DEFAULT_MODEL_PATH).

        Raises:
            FileNotFoundError: If the artifact does not exist.
            ValueError: If the artifact is corrupted or missing the pipeline.
        """
        model_path = Path(path or DEFAULT_MODEL_PATH)
        if not model_path.exists():
            raise FileNotFoundError(f"ML classifier artifact not found at {model_path}")

        try:
            bundle = joblib.load(model_path)
        except Exception as exc:
            raise ValueError(f"Failed to load ML model artifact from {model_path}: {exc}") from exc

        if isinstance(bundle, Pipeline):
            return cls(pipeline=bundle, model_path=model_path)

        if isinstance(bundle, dict) and "pipeline" in bundle:
            return cls(
                pipeline=bundle["pipeline"],
                metadata=bundle.get("metadata", {}),
                model_path=model_path,
            )

        raise ValueError(
            f"Invalid ML model artifact at {model_path}: expected Pipeline or dict with 'pipeline'"
        )

    @property
    def model_name(self) -> str:
        """Identifier of the active model architecture."""
        return self._model_name

    @property
    def model_version(self) -> str:
        """Version string of the active model artifact."""
        return self._model_version

    @property
    def classes(self) -> list[str]:
        """List of target classification categories."""
        return list(self._classes)

    def classify(
        self,
        context: EmailContext | NormalizedMessage | dict[str, Any],
    ) -> Classification:
        """Classify an inbound email, measuring latency and returning a Classification entity.

        Args:
            context: EmailContext, NormalizedMessage, or raw payload dict.

        Returns:
            Classification entity adhering to design.md §5.3.
        """
        start_time = time.perf_counter()

        # Coerce input to EmailContext
        if isinstance(context, NormalizedMessage):
            ctx = EmailContext.from_message(context)
        elif isinstance(context, dict):
            ctx = EmailContext.from_dict(context)
        elif isinstance(context, EmailContext):
            ctx = context
        else:
            raise TypeError(f"Unsupported context type for classification: {type(context)}")

        # Prepare normalized text representation
        body = ctx.body_text_clean or ctx.body_text
        text = prepare_text(ctx.subject, body)

        # Execute model prediction
        probs = self._pipeline.predict_proba([text])[0]
        best_idx = int(probs.argmax())
        if self._classes:
            winning_category = normalize_category(self._classes[best_idx])
        else:
            winning_category = normalize_category(str(self._pipeline.predict([text])[0]))
        confidence = float(probs[best_idx])

        # Build full class distribution
        probabilities: dict[str, float] = {}
        for cls_name, prob in zip(self._classes, probs, strict=False):
            probabilities[cls_name] = round(float(prob), 4)

        # Sort probabilities descending
        sorted_probs = dict(sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True))

        # Determine downstream triage signals
        reply_required = winning_category not in NO_REPLY_CATEGORIES

        # Workflow hint (R6.12)
        if not reply_required:
            workflow_hint = "none"
        elif winning_category == "acknowledgement":
            workflow_hint = "template"
        else:
            workflow_hint = "ai"

        # Priority determination
        full_text = f"{ctx.subject} {body}"
        if URGENT_PATTERN.search(full_text):
            priority = "urgent"
        elif winning_category in ("automated_notification", "no_response"):
            priority = "low"
        else:
            priority = "normal"

        # Knowledge retrieval requirement (R6.6)
        retrieval_required = (
            reply_required
            and (workflow_hint == "ai")
            and (winning_category in RETRIEVAL_CATEGORIES)
        )

        elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))

        return Classification(
            category=winning_category,
            intent=None,
            priority=priority,
            reply_required=reply_required,
            workflow_hint=workflow_hint,
            retrieval_required=retrieval_required,
            confidence=round(confidence, 4),
            decided_by="ml",
            latency_ms=elapsed_ms,
            model=f"{self._model_name}:{self._model_version}",
            raw={"probabilities": sorted_probs},
        )
