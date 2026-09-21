"""Model training and evaluation pipeline for Stage 2 ML triage (R6.1, R22.1, NFR3).

Trains a lightweight, high-performance TF-IDF + Logistic Regression classification
pipeline on the benchmark seed dataset, evaluates on held-out test data, computes
comprehensive metrics (accuracy, precision, recall, macro-F1, confusion matrix),
and exports versioned artifacts.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    precision_recall_fscore_support,
)
from sklearn.pipeline import Pipeline

from evaluation.datasets.loader import load_classification_dataset
from evaluation.datasets.schemas import ClassificationCategory, ClassificationDatasetItem

logger = logging.getLogger(__name__)

DEFAULT_MODEL_PATH = Path("artifacts/models/triage_ml_v1.joblib")
DEFAULT_METRICS_PATH = Path("artifacts/models/triage_ml_v1_metrics.json")
MODEL_VERSION = "1.0.0"
MODEL_NAME = "tfidf-logistic-v1"


def prepare_text(subject: str, body: str) -> str:
    """Combine and normalize subject and body text for feature extraction."""
    sub = (subject or "").strip()
    b = (body or "").strip()
    if sub and b:
        return f"Subject: {sub}\n\n{b}"
    return sub or b or ""


def get_git_sha() -> str:
    """Retrieve current git commit SHA for reproducible experiment tracking (R22.12)."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


def build_pipeline() -> Pipeline:
    """Build the scikit-learn TF-IDF + Logistic Regression pipeline."""
    return Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    ngram_range=(1, 2),
                    max_features=10000,
                    sublinear_tf=True,
                    strip_accents="unicode",
                    min_df=1,
                ),
            ),
            (
                "clf",
                LogisticRegression(
                    C=50.0,
                    max_iter=1000,
                    class_weight="balanced",
                    random_state=42,
                    solver="lbfgs",
                ),
            ),
        ]
    )


def evaluate_model(
    pipeline: Pipeline,
    test_items: list[ClassificationDatasetItem],
) -> dict[str, Any]:
    """Compute accuracy, precision, recall, macro-F1, and confusion matrix (R22.1).

    Args:
        pipeline: Trained scikit-learn pipeline.
        test_items: Held-out test dataset instances.

    Returns:
        Structured evaluation metrics dictionary.
    """
    test_texts = [prepare_text(item.subject, item.body) for item in test_items]
    test_labels = [
        str(getattr(item.gold_category, "value", item.gold_category))
        for item in test_items
    ]

    # Classes recognized by the model
    clf: LogisticRegression = pipeline.named_steps["clf"]
    classes = [str(c) for c in clf.classes_]

    # Predict
    test_preds = [str(p) for p in pipeline.predict(test_texts)]

    # Compute overall and macro metrics
    acc = float(accuracy_score(test_labels, test_preds))
    prec_macro, rec_macro, f1_macro, _ = precision_recall_fscore_support(
        test_labels,
        test_preds,
        average="macro",
        zero_division=0.0,
    )

    # Per-class metrics
    per_class_p, per_class_r, per_class_f1, per_class_sup = precision_recall_fscore_support(
        test_labels,
        test_preds,
        labels=classes,
        average=None,
        zero_division=0.0,
    )

    per_class_metrics: dict[str, dict[str, float | int]] = {}
    for idx, cls_name in enumerate(classes):
        per_class_metrics[cls_name] = {
            "precision": round(float(per_class_p[idx]), 4),
            "recall": round(float(per_class_r[idx]), 4),
            "f1_score": round(float(per_class_f1[idx]), 4),
            "support": int(per_class_sup[idx]),
        }

    # Confusion matrix
    cm = confusion_matrix(test_labels, test_preds, labels=classes)

    return {
        "accuracy": round(acc, 4),
        "macro_precision": round(float(prec_macro), 4),
        "macro_recall": round(float(rec_macro), 4),
        "macro_f1": round(float(f1_macro), 4),
        "classes": classes,
        "per_class": per_class_metrics,
        "confusion_matrix": {
            "labels": classes,
            "matrix": cm.tolist(),
        },
        "test_sample_count": len(test_items),
    }


def train_triage_model(
    train_items: list[ClassificationDatasetItem] | None = None,
    test_items: list[ClassificationDatasetItem] | None = None,
    output_model_path: Path | str | None = None,
    output_metrics_path: Path | str | None = None,
) -> tuple[Pipeline, dict[str, Any]]:
    """Train the Stage 2 ML classifier, evaluate on test data, and persist artifacts.

    Args:
        train_items: Optional training items (defaults to loading 'train' split).
        test_items: Optional test items (defaults to loading 'test' split).
        output_model_path: Target path for the joblib model bundle.
        output_metrics_path: Target path for the metrics JSON.

    Returns:
        Tuple of (trained Pipeline, metrics dictionary).
    """
    start_time = time.perf_counter()

    if train_items is None:
        train_items = load_classification_dataset(split="train")
    if test_items is None:
        test_items = load_classification_dataset(split="test")

    logger.info("Training on %d items, testing on %d items", len(train_items), len(test_items))

    train_texts = [prepare_text(item.subject, item.body) for item in train_items]
    train_labels = [
        str(getattr(item.gold_category, "value", item.gold_category))
        for item in train_items
    ]

    # Validate all canonical categories represented
    all_categories = {c.value for c in ClassificationCategory}
    present_categories = set(train_labels)
    missing = all_categories - present_categories
    if missing:
        logger.warning("Training set is missing canonical categories: %s", missing)

    # Train
    pipeline = build_pipeline()
    pipeline.fit(train_texts, train_labels)

    # Evaluate
    metrics = evaluate_model(pipeline, test_items)
    elapsed_training = time.perf_counter() - start_time

    model_path = Path(output_model_path or DEFAULT_MODEL_PATH)
    metrics_path = Path(output_metrics_path or DEFAULT_METRICS_PATH)

    # Ensure parent directories exist
    model_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)

    # Construct metadata bundle (R22.12)
    bundle_metadata = {
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "git_sha": get_git_sha(),
        "train_samples": len(train_items),
        "test_samples": len(test_items),
        "training_duration_seconds": round(elapsed_training, 3),
        "classes": metrics["classes"],
        "metrics": {
            "accuracy": metrics["accuracy"],
            "macro_precision": metrics["macro_precision"],
            "macro_recall": metrics["macro_recall"],
            "macro_f1": metrics["macro_f1"],
        },
    }

    # Save versioned joblib model artifact
    artifact_payload = {
        "pipeline": pipeline,
        "metadata": bundle_metadata,
    }
    joblib.dump(artifact_payload, model_path)
    logger.info("Saved trained ML classifier artifact to %s", model_path)

    # Save metrics JSON artifact (R22.1, R22.12)
    full_metrics_export = {
        "metadata": bundle_metadata,
        "evaluation": metrics,
    }
    metrics_path.write_text(json.dumps(full_metrics_export, indent=2), encoding="utf-8")
    logger.info("Saved evaluation metrics to %s", metrics_path)

    return pipeline, full_metrics_export


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(f"=== Training Stage 2 ML Classifier ({MODEL_NAME} v{MODEL_VERSION}) ===")
    _, results = train_triage_model()
    eval_res = results["evaluation"]
    print("\n--- Held-Out Benchmark Results (R22.1) ---")
    print(f"Accuracy:        {eval_res['accuracy'] * 100:.2f}%")
    print(f"Macro Precision: {eval_res['macro_precision'] * 100:.2f}%")
    print(f"Macro Recall:    {eval_res['macro_recall'] * 100:.2f}%")
    print(f"Macro F1:        {eval_res['macro_f1'] * 100:.2f}%")
    print("\n--- Per-Class Metrics ---")
    for cls_name, pcm in eval_res["per_class"].items():
        f1_val = pcm["f1_score"]
        p_val = pcm["precision"]
        r_val = pcm["recall"]
        sup = pcm["support"]
        print(f"  {cls_name:<25} F1={f1_val:.2f}  P={p_val:.2f}  R={r_val:.2f}  (n={sup})")
    print(f"\nArtifact saved: {DEFAULT_MODEL_PATH}")
    print(f"Metrics saved:  {DEFAULT_METRICS_PATH}")
