"""Unit tests for seed benchmark datasets (R22.1, R22.2, R5.9, R6.4)."""

from evaluation.datasets import (
    ClassificationCategory,
    QueryType,
    WorkflowHint,
    load_classification_dataset,
    load_retrieval_dataset,
)
from packages.db.fixtures.knowledge import KNOWLEDGE_DOCS, compute_chunk_id


def test_classification_dataset_counts_and_splits() -> None:
    """Validate total dataset size >= 300 and disjoint frozen train/test split (R22.1)."""
    train_items = load_classification_dataset(split="train")
    test_items = load_classification_dataset(split="test")
    all_items = load_classification_dataset(split="all")

    assert len(all_items) >= 300
    assert len(train_items) >= 240
    assert len(test_items) >= 60
    assert len(train_items) + len(test_items) == len(all_items)

    train_ids = {item.id for item in train_items}
    test_ids = {item.id for item in test_items}

    # Verify ID uniqueness and complete separation
    assert len(train_ids) == len(train_items)
    assert len(test_ids) == len(test_items)
    assert train_ids.isdisjoint(test_ids)


def test_classification_all_categories_covered_in_splits() -> None:
    """Validate all 9 categories from R6.4 exist in both train and test splits."""
    train_items = load_classification_dataset(split="train")
    test_items = load_classification_dataset(split="test")

    train_categories = {item.gold_category for item in train_items}
    test_categories = {item.gold_category for item in test_items}
    all_expected = set(ClassificationCategory)

    assert train_categories == all_expected
    assert test_categories == all_expected

    # Verify every category has adequate training representation
    for cat in ClassificationCategory:
        cat_train_count = sum(1 for item in train_items if item.gold_category == cat)
        cat_test_count = sum(1 for item in test_items if item.gold_category == cat)
        assert cat_train_count >= 20, f"Insufficient train items for {cat}: {cat_train_count}"
        assert cat_test_count >= 5, f"Insufficient test items for {cat}: {cat_test_count}"


def test_classification_rule_triggers_and_workflow_hints() -> None:
    """Validate header-based and pattern-based rule triggers in classification set (Task 2.2)."""
    all_items = load_classification_dataset(split="all")

    # 1. List-Unsubscribe header triggers
    unsub_items = [item for item in all_items if "List-Unsubscribe" in item.headers]
    assert len(unsub_items) >= 15
    for item in unsub_items:
        assert item.gold_reply_required is False
        assert item.gold_workflow_hint == WorkflowHint.NONE

    # 2. Auto-Submitted header triggers
    auto_items = [
        item for item in all_items if item.headers.get("Auto-Submitted") == "auto-replied"
    ]
    assert len(auto_items) >= 15
    for item in auto_items:
        assert item.gold_reply_required is False
        assert item.gold_workflow_hint == WorkflowHint.NONE

    # 3. Sender triggers (no-reply, alerts)
    no_reply_senders = [
        item for item in all_items if "no-reply@" in item.sender or "alerts@" in item.sender
    ]
    assert len(no_reply_senders) >= 10


def test_retrieval_dataset_counts_and_split() -> None:
    """Validate retrieval dataset size >= 100 with ~50/50 semantic vs lexical split (R22.2)."""
    queries = load_retrieval_dataset()
    assert len(queries) >= 100

    query_ids = {q.query_id for q in queries}
    assert len(query_ids) == len(queries)

    nl_queries = [q for q in queries if q.query_type == QueryType.NATURAL_LANGUAGE]
    id_queries = [q for q in queries if q.query_type == QueryType.IDENTIFIER_BEARING]

    assert len(nl_queries) >= 45
    assert len(id_queries) >= 45
    assert len(nl_queries) + len(id_queries) == len(queries)


def test_retrieval_gold_chunk_ids_resolve_to_knowledge_corpus() -> None:
    """Verify 100% of gold chunk IDs resolve to real knowledge corpus chunks (R22.2)."""
    queries = load_retrieval_dataset()

    # Collect all valid chunk UUIDs from KNOWLEDGE_DOCS
    valid_chunk_ids = {
        compute_chunk_id(doc.org_id, doc.title, chunk.chunk_index)
        for doc in KNOWLEDGE_DOCS
        for chunk in doc.chunks
    }
    assert len(valid_chunk_ids) >= 10

    for query in queries:
        assert len(query.gold_chunk_ids) >= 1, f"Query {query.query_id} has no gold chunks"
        for chunk_id in query.gold_chunk_ids:
            assert chunk_id in valid_chunk_ids, (
                f"Query {query.query_id} references non-existent chunk ID: {chunk_id}"
            )

        # For identifier-bearing queries, ensure the cited entity exists in the query string
        if query.query_type == QueryType.IDENTIFIER_BEARING:
            assert query.target_entity is not None
            assert query.target_entity in query.query
