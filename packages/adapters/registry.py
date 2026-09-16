"""Mail provider adapter registry.

Requirements:
- R1.3: Resolve adapters through a registry keyed by mailbox.provider, with
  no provider name appearing in any module outside adapters/.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from packages.adapters.exceptions import NotFound
from packages.adapters.protocol import MailProviderAdapter
from packages.domain.entities import Mailbox

AdapterFactory = Callable[..., MailProviderAdapter]

_REGISTRY: dict[str, AdapterFactory] = {}


def register_adapter(provider: str, factory: AdapterFactory) -> None:
    """Register an adapter factory for a given provider key (R1.3)."""
    key = provider.strip().lower()
    _REGISTRY[key] = factory


def get_adapter(provider: str, **kwargs: Any) -> MailProviderAdapter:
    """Resolve and instantiate an adapter for a provider key (R1.3).

    Raises:
        NotFound: If no adapter has been registered for the provider key.
    """
    key = provider.strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise NotFound(
            f"No mail provider adapter registered for provider '{provider}'.",
            provider=provider,
        )
    return factory(**kwargs)


def get_adapter_for_mailbox(mailbox: Mailbox, **kwargs: Any) -> MailProviderAdapter:
    """Convenience helper to resolve an adapter using mailbox.provider (R1.3)."""
    return get_adapter(mailbox.provider, mailbox_id=str(mailbox.id), **kwargs)


def is_provider_registered(provider: str) -> bool:
    """Check whether an adapter is registered for the specified provider."""
    return provider.strip().lower() in _REGISTRY


def list_registered_providers() -> list[str]:
    """Return sorted list of all registered provider names."""
    return sorted(_REGISTRY.keys())


def clear_registry() -> None:
    """Clear all registered adapters (primarily for test teardown)."""
    _REGISTRY.clear()
