# Implementation Plan — Phase 2 Task 2.3: Lightweight ML Classifier (Triage Stage 2)

Implement Stage 2 of the cascading triage engine: a lightweight, fast (budgeted 20–50 ms), highly accurate machine learning classifier trained on the Phase 0 classification seed dataset (`evaluation/datasets/classification/train.jsonl` and `test.jsonl`), serialized as a versioned artifact, and exposing structured `Classification` outputs adhering to `R6.1, R22.1, NFR3` and `design.md §5.3`.

---

### Goal

1. Add `scikit-learn` to project dependencies in `pyproject.toml` for TF-IDF feature extraction, calibrated linear classification, and benchmark metric computation.
2. Build `services/triage_worker/classifier.py` implementing `MLClassifier` to load versioned model artifacts and perform sub-50ms inference on `EmailContext` or `NormalizedMessage`, emitting full probability distributions and structured `Classification` entities.
3. Build `services/triage_worker/training.py` to train the TF-IDF + Logistic Regression pipeline on the 252-example training set, evaluate on the 64-example held-out test set, report macro-F1, precision, recall, and confusion matrix, and export the versioned artifact to `artifacts/models/triage_ml_v1.joblib` and `artifacts/models/triage_ml_v1_metrics.json`.
4. Update configuration in `packages/core/settings.py`, `.env.example`, and `docs/configuration.md` with `TRIAGE__ML_MODEL_PATH`.
5. Add unit and regression test suites in `tests/unit/test_triage_ml.py` verifying model performance, sub-50ms latency, serialization, edge cases, and compliance with requirements `R6.1, R22.1, NFR3`.

---

### Assumptions

1. The classification dataset built in Task 0.13 (`evaluation/datasets/classification/train.jsonl` and `test.jsonl`) contains 316 labeled emails covering all 9 categories defined in `R6.4`.
2. `scikit-learn` provides both the necessary inference primitives (`TfidfVectorizer` + `LogisticRegression` with `predict_proba`) and metric computation (`accuracy_score`, `precision_recall_fscore_support`, `confusion_matrix`) required by `R22.1`.
3. Inference latency of TF-IDF + Logistic Regression on standard CPU takes ~1–10 ms, comfortably inside the NFR3 20–50 ms budget.
4. `packages/domain/` remains pure stdlib + `packages/core`; `scikit-learn` is imported only inside `services/triage_worker/` and `evaluation/`.

---

### Plan

1. **Add scikit-learn dependency & update environment**
   - Files: `pyproject.toml`
   - Change:
     - Add `scikit-learn>=1.4.0` to `dependencies`.
     - Run `uv sync` to lock and install in virtualenv.
   - Verify:
     - Run `uv run python -c "import sklearn; print(sklearn.__version__)"`.

2. **Add ML model configuration settings**
   - Files: `packages/core/settings.py`, `.env.example`, `docs/configuration.md`
   - Change:
     - Add `ml_model_path: str = Field(default="artifacts/models/triage_ml_v1.joblib", description="Path to trained ML classifier model artifact")` to `TriageSettings`.
     - Document `TRIAGE__ML_MODEL_PATH` in `.env.example` and `docs/configuration.md`.
   - Verify:
     - Run `pytest tests/unit/test_settings.py` (or verify settings instantiation).

3. **Implement training & evaluation pipeline**
   - Files: `services/triage_worker/training.py`
   - Change:
     - Load train/test datasets via `evaluation.datasets.loader.load_classification_dataset`.
     - Construct scikit-learn pipeline: `TfidfVectorizer(ngram_range=(1, 2), max_features=10000, sublinear_tf=True)` + `LogisticRegression(class_weight="balanced", C=1.0, max_iter=1000, random_state=42)`.
     - Train on train set; evaluate on held-out test set for accuracy, macro-F1, macro-precision, macro-recall, per-class metrics, and confusion matrix (`R22.1`).
     - Serialize trained pipeline and metadata bundle to `artifacts/models/triage_ml_v1.joblib` and metrics JSON to `artifacts/models/triage_ml_v1_metrics.json`.
     - Expose CLI / main execution so it can be re-trained and re-evaluated at any time.
   - Verify:
     - Run `python -m services.triage_worker.training`.
     - Inspect generated artifacts in `artifacts/models/` and verify held-out macro-F1.

4. **Implement MLClassifier inference engine**
   - Files: `services/triage_worker/classifier.py`
   - Change:
     - Define `MLClassifier` with `load_from_artifact(path)` and `classify(context) -> Classification`.
     - Extract text features from subject + body (clean); predict class probabilities.
     - Populate `Classification` entity: `category`, `confidence` (calibrated winning probability), `reply_required`, `workflow_hint`, `priority`, `retrieval_required`, `decided_by="ml"`, `latency_ms`, `model`, and raw class distribution in `raw`.
     - Implement fail-safe error isolation if artifact is missing or corrupted.
   - Verify:
     - Run test inference script on sample fixture messages.

5. **Author comprehensive automated test suite**
   - Files: `tests/unit/test_triage_ml.py`
   - Change:
     - Test training pipeline, artifact saving, and loading.
     - Test classification correctness across diverse email samples (support, billing, scheduling, automated).
     - Test confidence threshold calibration and raw class distribution output.
     - Benchmark inference latency asserting `< 50ms` (NFR3).
     - Test held-out evaluation metrics computation (`R22.1`: accuracy, precision, recall, macro-F1, confusion matrix).
     - Test edge cases: empty subject/body, missing artifact fallback, unusual unicode characters.
   - Verify:
     - Run `pytest tests/unit/test_triage_ml.py -v`.
     - Run full test suite: `pytest tests/unit tests/integration`.

6. **Update task checklist**
   - Files: `specs/tasks.md`
   - Change:
     - Mark task 2.3 `[x]` upon all passing tests and verified Definition of Done.
   - Verify:
     - Review `git diff specs/tasks.md`.

---

### Risks & mitigations

- **Risk:** Small training set (252 samples) might produce imbalanced performance or overfit certain rare classes.
  - *Mitigation:* Use `sublinear_tf=True`, n-gram range (1, 2), and `class_weight="balanced"` in Logistic Regression. Test set F1 will be logged explicitly; Stage 3 LLM fallback handles confidence below threshold `0.80`.
- **Risk:** Inference latency overhead under high concurrency.
  - *Mitigation:* Pure TF-IDF + LogisticRegression runs in <5 ms per sample on CPU without external API/network calls or GPU requirements.
- **Risk:** Dependency bloat or breaking architecture boundaries.
  - *Mitigation:* `scikit-learn` is isolated in `services/triage_worker/` and `evaluation/`. `packages/domain/` remains 100% pure standard library.

---

### Rollback plan

If issues occur:
1. Revert `pyproject.toml` changes and remove `scikit-learn` via `git checkout pyproject.toml && uv sync`.
2. Remove created files `services/triage_worker/classifier.py`, `services/triage_worker/training.py`, `artifacts/models/`, and `tests/unit/test_triage_ml.py`.
3. Existing Stage 1 rule engine remains fully operational and untainted.
