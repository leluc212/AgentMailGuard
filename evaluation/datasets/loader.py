"""Dataset loader utilities for benchmark and evaluation datasets (R22.1, R22.2).

Provides type-safe, validated loaders for:
- Classification train and test splits (ClassificationDatasetItem)
- Retrieval query sets (RetrievalDatasetItem)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from evaluation.datasets.schemas import (
    ClassificationDatasetItem,
    RetrievalDatasetItem,
)

DEFAULT_DATASETS_DIR = Path(__file__).resolve().parent


def load_classification_dataset(
    split: Literal["train", "test", "all"] = "all",
    datasets_dir: Path | None = None,
) -> list[ClassificationDatasetItem]:
    """Load and validate classification seed dataset items from JSONL.

    Args:
        split: One of 'train', 'test', or 'all'.
        datasets_dir: Root datasets directory (defaults to evaluation/datasets).

    Returns:
        List of validated ClassificationDatasetItem instances.
    """
    target_dir = (datasets_dir or DEFAULT_DATASETS_DIR) / "classification"
    files_to_load: list[Path] = []

    if split in ("train", "all"):
        train_file = target_dir / "train.jsonl"
        if not train_file.exists():
            raise FileNotFoundError(f"Classification train file not found: {train_file}")
        files_to_load.append(train_file)

    if split in ("test", "all"):
        test_file = target_dir / "test.jsonl"
        if not test_file.exists():
            raise FileNotFoundError(f"Classification test file not found: {test_file}")
        files_to_load.append(test_file)

    items: list[ClassificationDatasetItem] = []
    for file_path in files_to_load:
        with open(file_path, encoding="utf-8") as f:
            for line_idx, line in enumerate(f, start=1):
                clean_line = line.strip()
                if not clean_line:
                    continue
                try:
                    raw_data = json.loads(clean_line)
                    item = ClassificationDatasetItem.model_validate(raw_data)
                    items.append(item)
                except Exception as err:
                    raise ValueError(
                        f"Failed parsing {file_path.name} line {line_idx}: {err}"
                    ) from err

    return items


def load_retrieval_dataset(
    datasets_dir: Path | None = None,
) -> list[RetrievalDatasetItem]:
    """Load and validate retrieval query dataset items from JSONL.

    Args:
        datasets_dir: Root datasets directory (defaults to evaluation/datasets).

    Returns:
        List of validated RetrievalDatasetItem instances.
    """
    target_file = (datasets_dir or DEFAULT_DATASETS_DIR) / "retrieval" / "queries.jsonl"
    if not target_file.exists():
        raise FileNotFoundError(f"Retrieval queries file not found: {target_file}")

    queries: list[RetrievalDatasetItem] = []
    with open(target_file, encoding="utf-8") as f:
        for line_idx, line in enumerate(f, start=1):
            clean_line = line.strip()
            if not clean_line:
                continue
            try:
                raw_data = json.loads(clean_line)
                item = RetrievalDatasetItem.model_validate(raw_data)
                queries.append(item)
            except Exception as err:
                raise ValueError(f"Failed parsing queries.jsonl line {line_idx}: {err}") from err

    return queries
