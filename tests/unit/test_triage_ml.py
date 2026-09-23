"""Unit tests for Stage 2 lightweight ML triage classifier (R6.1, R22.1, NFR3).

Verifies:
1. Artifact loading, metadata inspection, and serialization integrity.
2. Output contract compliance adhering to design.md §5.3.
3. Classification accuracy across diverse intent categories.
4. Urgency detection, reply requirements, workflow hints, and retrieval flags.
5. Inference latency compliance under the 20-50ms NFR3 budget.
6. Held-out benchmark metrics calculation (accuracy, precision, recall, macro-F1, confusion matrix).
7. Edge cases: empty text, huge text, unicode, malformed inputs.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from evaluation.datasets.loader import load_classification_dataset
from packages.domain.entities import (
    Classification,
    EmailAddress,
    NormalizedMessage,
)
from packages.domain.rules import EmailContext
from services.triage_worker.classifier import MLClassifier
from services.triage_worker.training import (
    DEFAULT_METRICS_PATH,
    DEFAULT_MODEL_PATH,
    build_pipeline,
    evaluate_model,
    prepare_text,
    train_triage_model,
)

CANONICAL_CATEGORIES = {
    "support",
    "sales",
    "billing",
    "administration",
    "scheduling",
    "general_inquiry",
    "automated_notification",
    "acknowledgement",
    "no_response",
}


@pytest.fixture(scope="module")
def classifier() -> MLClassifier:
    """Load default trained ML classifier artifact."""
    if not DEFAULT_MODEL_PATH.exists():
        train_triage_model()
    return MLClassifier.load_from_artifact(DEFAULT_MODEL_PATH)


class TestMLClassifierArtifact:
    """Verification of model artifact loading, serialization, and metadata."""

    def test_load_default_artifact(self, classifier: MLClassifier) -> None:
        assert classifier.model_name == "tfidf-logistic-v1"
        assert classifier.model_version == "1.0.0"
        assert set(classifier.classes) == CANONICAL_CATEGORIES
        assert len(classifier.classes) == 9

    def test_artifact_not_found(self, tmp_path: Path) -> None:
        missing_file = tmp_path / "non_existent.joblib"
        with pytest.raises(FileNotFoundError, match="not found"):
            MLClassifier.load_from_artifact(missing_file)

    def test_invalid_artifact_structure(self, tmp_path: Path) -> None:
        invalid_file = tmp_path / "corrupt.joblib"
        invalid_file.write_text("not a joblib file", encoding="utf-8")
        with pytest.raises(ValueError, match="Failed to load ML model artifact"):
            MLClassifier.load_from_artifact(invalid_file)

    def test_artifact_missing_pipeline(self, tmp_path: Path) -> None:
        import joblib

        empty_file = tmp_path / "empty_dict.joblib"
        joblib.dump({"some_key": "some_value"}, empty_file)
        with pytest.raises(ValueError, match="expected Pipeline or dict"):
            MLClassifier.load_from_artifact(empty_file)


class TestMLClassifierInferenceContract:
    """Verification of structured Classification contract adherence (R6.1, design.md §5.3)."""

    def test_classify_email_context(self, classifier: MLClassifier) -> None:
        ctx = EmailContext(
            subject="Application crash HTTP 500 on PDF export",
            body_text=(
                "When exporting quarterly PDF reports, the web application crashes with HTTP 500. "
                "Stack trace indicates line 142 in exporter.py. Please investigate immediately."
            ),
        )
        res = classifier.classify(ctx)

        assert isinstance(res, Classification)
        assert res.decided_by == "ml"
        assert res.category == "support"
        assert 0.0 <= res.confidence <= 1.0
        assert res.latency_ms > 0
        assert res.model is not None and res.model.startswith("tfidf-logistic-v1:")
        assert "probabilities" in res.raw
        assert len(res.raw["probabilities"]) == 9
        # Probabilities sum to approximately 1.0
        assert pytest.approx(sum(res.raw["probabilities"].values()), abs=0.01) == 1.0

    def test_classify_normalized_message(self, classifier: MLClassifier) -> None:
        msg = NormalizedMessage(
            message_id=uuid4(),
            thread_id=uuid4(),
            mailbox_id=uuid4(),
            organization_id=uuid4(),
            provider="gmail",
            provider_message_id="msg-12345",
            received_at=datetime.now(UTC),
            rfc822_message_id="<msg-12345@domain.com>",
            sender=EmailAddress(email="billing@client.com", name="Accounts Payable"),
            subject="Invoice INV-2026-0045 inquiry",
            body_text="Please provide the remittance details for invoice INV-2026-0045.",
            body_text_clean="Please provide the remittance details for invoice INV-2026-0045.",
        )
        res = classifier.classify(msg)

        assert isinstance(res, Classification)
        assert res.category == "billing"
        assert res.reply_required is True
        assert res.workflow_hint == "ai"
        assert res.retrieval_required is True

    def test_classify_dict_payload(self, classifier: MLClassifier) -> None:
        payload = {
            "subject": "Quarterly Business Review: Scheduling call for next week",
            "body_text": (
                "Hi team, could we find 30 minutes next Tuesday or Wednesday "
                "for our quarterly business review meeting?"
            ),
        }
        res = classifier.classify(payload)

        assert isinstance(res, Classification)
        assert res.category == "scheduling"
        assert res.reply_required is True

    def test_classify_unsupported_type_raises(self, classifier: MLClassifier) -> None:
        with pytest.raises(TypeError, match="Unsupported context type"):
            classifier.classify(12345)  # type: ignore[arg-type]


class TestTriageSignalsAndRouting:
    """Verification of downstream workflow hints, priority, and reply gates."""

    def test_urgent_priority_detection(self, classifier: MLClassifier) -> None:
        ctx = EmailContext(
            subject="CRITICAL: Outage on production cluster ASAP",
            body_text="The cluster has crashed and customers cannot access service immediately.",
        )
        res = classifier.classify(ctx)
        assert res.priority == "urgent"

    def test_normal_priority_default(self, classifier: MLClassifier) -> None:
        ctx = EmailContext(
            subject="Question regarding standard subscription plans",
            body_text="Could you send over the updated price tiers when you have a moment?",
        )
        res = classifier.classify(ctx)
        assert res.priority == "normal"

    def test_automated_notification_gate(self, classifier: MLClassifier) -> None:
        ctx = EmailContext(
            subject="[Alert] CPU utilization exceeded 85% on node-prod-04",
            body_text=(
                "Automated monitoring notification: Host CPU load 92% sustained for 10 minutes."
            ),
        )
        res = classifier.classify(ctx)
        assert res.category == "automated_notification"
        assert res.reply_required is False
        assert res.workflow_hint == "none"
        assert res.retrieval_required is False
        assert res.priority == "low"

    def test_acknowledgement_template_gate(self, classifier: MLClassifier) -> None:
        ctx = EmailContext(
            subject="Thank you for your assistance",
            body_text="Thanks, we received the update and everything is working well now.",
        )
        res = classifier.classify(ctx)
        assert res.category == "acknowledgement"
        assert res.reply_required is False
        assert res.workflow_hint == "none"
        assert res.retrieval_required is False


class TestInferenceLatency:
    """Verification of NFR3 budget: 20-50 ms inference latency."""

    def test_sub_50ms_latency_benchmark(self, classifier: MLClassifier) -> None:
        ctx = EmailContext(
            subject="Feature request: Export audit log to CSV format",
            body_text="Our compliance team requires monthly audit log exports in CSV format.",
        )

        latencies: list[float] = []
        for _ in range(50):
            t0 = time.perf_counter()
            res = classifier.classify(ctx)
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1000)
            assert res.latency_ms < 50

        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)

        # Average latency must be comfortably under 20ms on modern CPU
        assert avg_latency < 20.0, f"Average latency too high: {avg_latency:.2f} ms"
        assert max_latency < 50.0, f"Max latency exceeded NFR3 budget: {max_latency:.2f} ms"


class TestEvaluationMetrics:
    """Verification of R22.1 evaluation metrics calculation and held-out benchmark."""

    def test_metrics_schema_and_calculation(self) -> None:
        test_items = load_classification_dataset("test")
        pipeline = build_pipeline()
        train_items = load_classification_dataset("train")

        train_texts = [prepare_text(i.subject, i.body) for i in train_items]
        train_labels = [
            str(getattr(i.gold_category, "value", i.gold_category)) for i in train_items
        ]
        pipeline.fit(train_texts, train_labels)

        metrics = evaluate_model(pipeline, test_items)

        assert "accuracy" in metrics
        assert "macro_precision" in metrics
        assert "macro_recall" in metrics
        assert "macro_f1" in metrics
        assert "classes" in metrics
        assert "per_class" in metrics
        assert "confusion_matrix" in metrics

        assert metrics["accuracy"] >= 0.90
        assert metrics["macro_f1"] >= 0.90
        assert metrics["confusion_matrix"]["labels"] == metrics["classes"]

        # Validate confusion matrix shape (9 x 9)
        cm = metrics["confusion_matrix"]["matrix"]
        assert len(cm) == 9
        for row in cm:
            assert len(row) == 9

    def test_saved_metrics_file_exists(self) -> None:
        assert DEFAULT_METRICS_PATH.exists()


class TestEdgeCases:
    """Robustness testing on extreme, empty, and unusual inputs."""

    def test_empty_subject_and_body(self, classifier: MLClassifier) -> None:
        ctx = EmailContext(subject="", body_text="")
        res = classifier.classify(ctx)
        assert isinstance(res, Classification)
        assert res.category in CANONICAL_CATEGORIES
        assert res.confidence >= 0.0

    def test_huge_email_body(self, classifier: MLClassifier) -> None:
        large_body = "word " * 50000  # 50,000 words (~250 KB)
        ctx = EmailContext(subject="Massive email body test", body_text=large_body)
        res = classifier.classify(ctx)
        assert isinstance(res, Classification)
        assert res.latency_ms < 600  # Within acceptable budget for massive 250KB payload under CI load

    def test_unicode_and_emojis(self, classifier: MLClassifier) -> None:
        ctx = EmailContext(
            subject="🚨 紧急: Système de facturation en panne ⚠️",
            body_text=(
                "Bonjour, nous avons une erreur 500 sur la facture #INV-999. "
                "Merci de corriger! 谢谢"
            ),
        )
        res = classifier.classify(ctx)
        assert isinstance(res, Classification)
        assert res.category in CANONICAL_CATEGORIES
