"""Stage 2 of the Email Injection Scanner: lightweight ML classifier.

TF-IDF (word 1-2 grams + char 3-5 grams) -> calibrated logistic regression.
Trained by ``training/train_l1_classifier.py`` on the unified L1 dataset and
persisted as a joblib artifact. Typical latency: 1-5 ms per email on CPU.

The optional transformer backend (DeBERTa / Prompt Guard 2) lives in
``evaluation/baselines.py``; this module stays dependency-light so the guard can
run inside the triage worker without torch.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, Pipeline

logger = logging.getLogger(__name__)

MODEL_VERSION = "l1_injection_clf_v1"


def build_pipeline(calibrate: bool = True) -> Pipeline:
    features = FeatureUnion(
        [
            (
                "word",
                TfidfVectorizer(
                    ngram_range=(1, 2),
                    min_df=2,
                    max_features=200_000,
                    sublinear_tf=True,
                    lowercase=True,
                ),
            ),
            (
                "char",
                TfidfVectorizer(
                    analyzer="char_wb",
                    ngram_range=(3, 5),
                    min_df=2,
                    max_features=300_000,
                    sublinear_tf=True,
                    lowercase=True,
                ),
            ),
        ]
    )
    base = LogisticRegression(C=4.0, max_iter=2000, class_weight="balanced")
    clf: Any = CalibratedClassifierCV(base, method="sigmoid", cv=3) if calibrate else base
    return Pipeline([("features", features), ("clf", clf)])


@dataclass
class InjectionClassifier:
    """Thin wrapper around the sklearn pipeline with a stable ``predict_proba``."""

    pipeline: Pipeline | None = None
    version: str = MODEL_VERSION
    metrics: dict[str, Any] | None = None

    @property
    def available(self) -> bool:
        return self.pipeline is not None

    @classmethod
    def load(cls, path: Path) -> InjectionClassifier:
        if not path.exists():
            logger.warning("L1 ML model not found at %s; stage 2 disabled", path)
            return cls(pipeline=None)
        payload = joblib.load(path)
        if isinstance(payload, dict):
            return cls(
                pipeline=payload["pipeline"],
                version=str(payload.get("version", MODEL_VERSION)),
                metrics=payload.get("metrics"),
            )
        return cls(pipeline=payload)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"pipeline": self.pipeline, "version": self.version, "metrics": self.metrics}, path
        )

    def fit(self, texts: Sequence[str], labels: Sequence[int], calibrate: bool = True) -> None:
        pipe = build_pipeline(calibrate=calibrate and len(texts) >= 30)
        pipe.fit(list(texts), np.asarray(labels, dtype=int))
        self.pipeline = pipe

    def predict_proba(self, text: str) -> float:
        """Probability that ``text`` contains a prompt injection (0 if unavailable)."""
        if self.pipeline is None:
            return 0.0
        proba = self.pipeline.predict_proba([text])[0]
        classes = list(self.pipeline.classes_)
        idx = classes.index(1) if 1 in classes else -1
        return float(proba[idx])

    def predict_proba_batch(self, texts: Sequence[str]) -> list[float]:
        if self.pipeline is None or not texts:
            return [0.0] * len(texts)
        proba = self.pipeline.predict_proba(list(texts))
        classes = list(self.pipeline.classes_)
        idx = classes.index(1) if 1 in classes else -1
        return [float(p[idx]) for p in proba]
