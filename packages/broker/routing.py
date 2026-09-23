"""Category-aware message routing and queue lane resolution (R7.1, R7.2, R7.4, R7.6).

Provides:
- Resolution of business priority levels to queue lanes ('normal' vs 'priority').
- Deterministic routing key generation ('email.<category>.<priority>').
- Evaluation of unconsumed queues against configured consumer patterns.
- Declarative category taxonomy loading from YAML configuration.
- Envelope preparation for downstream AI worker consumption.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from packages.broker.envelope import JobEnvelope
from packages.domain.taxonomy import (
    CategoryDefinition,
    TaxonomyRegistry,
    get_default_registry,
    normalize_category,
)

logger = logging.getLogger(__name__)

# Priority levels mapped to the "priority" lane (R7.2)
HIGH_PRIORITY_LEVELS: frozenset[str] = frozenset({"urgent", "high", "priority", "critical"})

# Canonical lanes supported across category queues
CANONICAL_LANES: tuple[str, str] = ("normal", "priority")


def resolve_priority_lane(priority: str | None) -> str:
    """Resolve an incoming priority attribute into an active routing lane (R7.2).

    Parameters
    ----------
    priority : str | None
        Urgency level from classification or rules (e.g. 'urgent', 'high', 'normal', 'low').

    Returns
    -------
    str
        Either 'priority' or 'normal'.
    """
    if not priority:
        return "normal"
    cleaned = str(priority).strip().lower()
    if cleaned in HIGH_PRIORITY_LEVELS:
        return "priority"
    return "normal"


def format_routing_key(category: str, priority: str | None = None) -> str:
    """Format the canonical AMQP topic routing key for an email (R7.1).

    Routing key structure: `email.<category>.<lane>`
    Example: `email.billing.priority` or `email.support.normal`

    Parameters
    ----------
    category : str
        Category name (e.g. 'billing', 'technical_support').
    priority : str | None
        Priority level, resolved to 'normal' or 'priority' lane.

    Returns
    -------
    str
        AMQP routing key string formatted as `email.<category>.<lane>`.
    """
    normalized_cat = normalize_category(category)
    lane = resolve_priority_lane(priority)
    return f"email.{normalized_cat}.{lane}"


def is_queue_consumed(queue_name: str, configured_consumers: list[str]) -> bool:
    """Check whether a declared queue is matched by configured consumer patterns (R7.6).

    Supports exact queue names and wildcard patterns (e.g. 'email.*.priority' or 'email.support.*').

    Parameters
    ----------
    queue_name : str
        The declared queue name to check.
    configured_consumers : list[str]
        List of configured queue names or wildcard glob patterns.

    Returns
    -------
    bool
        True if the queue has a matching consumer, False otherwise.
    """
    for pattern in configured_consumers:
        pattern = pattern.strip()
        if not pattern:
            continue
        if pattern == queue_name or fnmatch.fnmatch(queue_name, pattern):
            return True
    return False


def load_categories_from_yaml(
    path: str | Path,
    registry: TaxonomyRegistry | None = None,
) -> list[CategoryDefinition]:
    """Load and register category definitions from a declarative YAML file (R7.4).

    Parameters
    ----------
    path : str | Path
        Path to the categories YAML configuration file.
    registry : TaxonomyRegistry | None
        Target registry to register into (defaults to global registry).

    Returns
    -------
    list[CategoryDefinition]
        List of registered CategoryDefinition instances.
    """
    cfg_path = Path(path)
    if not cfg_path.is_file():
        logger.warning(
            "Category config file not found at '%s'; skipping dynamic registration", path
        )
        return []

    try:
        content = cfg_path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
    except Exception as err:
        logger.error("Failed to parse category YAML from '%s': %s", path, err)
        raise

    if not isinstance(data, dict) or "categories" not in data:
        logger.warning("Category YAML at '%s' missing top-level 'categories' list", path)
        return []

    target_registry = registry or get_default_registry()
    registered: list[CategoryDefinition] = []

    for entry in data.get("categories", []):
        if not isinstance(entry, dict) or "category" not in entry:
            continue
        defn = target_registry.register_from_dict(entry)
        registered.append(defn)

    logger.info("Loaded %d category definitions from '%s'", len(registered), path)
    return registered


def prepare_route_envelope(
    envelope: JobEnvelope,
    classification: Any,
) -> tuple[str, JobEnvelope]:
    """Prepare a JobEnvelope for downstream AI generation and compute its routing key (R7.1, R7.3).

    Parameters
    ----------
    envelope : JobEnvelope
        Current job envelope from triage stage.
    classification : Any
        Classification entity or dictionary containing category, priority, etc.

    Returns
    -------
    tuple[str, JobEnvelope]
        Tuple of (routing_key, updated_envelope).
    """
    # Extract classification metadata
    if is_dataclass(classification) and not isinstance(classification, type):
        cls_dict = asdict(classification)
    elif hasattr(classification, "to_dict"):
        cls_dict = classification.to_dict()
    elif hasattr(classification, "model_dump"):
        cls_dict = classification.model_dump()
    elif isinstance(classification, dict):
        cls_dict = classification.copy()
    else:
        raise TypeError(f"Unsupported classification type: {type(classification)}")

    raw_cat = cls_dict.get("category", "general_inquiry")
    cat = normalize_category(raw_cat)
    cls_dict["category"] = cat
    priority = cls_dict.get("priority", "normal")
    routing_key = format_routing_key(cat, priority)

    # Clone envelope with generate_reply job type and full classification snapshot (R7.3)
    routed_envelope = envelope.model_copy(
        update={
            "job_type": "generate_reply",
            "attempt": 0,
        }
    )
    routed_envelope.set_classification(cls_dict)

    return routing_key, routed_envelope
