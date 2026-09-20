"""Mail provider adapter registry.

Requirements:
- R1.3: Resolve adapters through a registry keyed by mailbox.provider, with
  no provider name appearing in any module outside adapters/.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from packages.adapters.exceptions import NotFound
from packages.adapters.protocol import MailProviderAdapter
from packages.domain.entities import Mailbox

AdapterFactory = Callable[..., MailProviderAdapter]

_REGISTRY: dict[str, AdapterFactory] = {}


def resolve_provider_credentials(
    credentials_ref: str | None,
    provider: str,
) -> dict[str, Any]:
    """Resolve provider credentials reference into adapter initialization kwargs.

    Per GEMINI.md §6, credentials are stored as references (never plaintext secrets).
    Supports:
    - 'env:<VAR_NAME>': Reads access token from environment variable.
    - 'file:<PATH>': Reads access token from JSON (keys 'access_token' or 'token') or text file.
    - Direct access token string (e.g. 'ya29...').
    - Environment fallbacks: GMAIL_ACCESS_TOKEN, GRAPH_ACCESS_TOKEN.
    """
    token: str | None = None
    prov_key = provider.strip().lower()

    if credentials_ref:
        ref = credentials_ref.strip()
        if ref.startswith("env:"):
            var_name = ref.removeprefix("env:").strip()
            token = os.environ.get(var_name)
        elif ref.startswith("file:"):
            file_path = Path(ref.removeprefix("file:").strip())
            if file_path.exists() and file_path.is_file():
                content = file_path.read_text(encoding="utf-8").strip()
                try:
                    data = json.loads(content)
                    token = data.get("access_token") or data.get("token") or content
                except Exception:
                    token = content
        elif ref.startswith("ya29."):
            token = ref

    # Environment variable fallbacks if not resolved from reference
    if not token:
        if prov_key == "gmail":
            token = os.environ.get("GMAIL_ACCESS_TOKEN")
        elif prov_key == "graph":
            token = os.environ.get("GRAPH_ACCESS_TOKEN")

    kwargs: dict[str, Any] = {}
    if token:
        kwargs["access_token"] = token
    return kwargs


def register_adapter(provider: str, factory: AdapterFactory) -> None:
    """Register an adapter factory for a given provider key (R1.3)."""
    key = provider.strip().lower()
    _REGISTRY[key] = factory


def register_default_adapters() -> None:
    """Register built-in default provider adapters."""
    from packages.adapters.fake import FakeProviderAdapter
    from packages.adapters.gmail import GmailProviderAdapter
    from packages.adapters.graph import GraphProviderAdapter

    register_adapter("fake", FakeProviderAdapter)
    register_adapter("gmail", GmailProviderAdapter)
    register_adapter("graph", GraphProviderAdapter)


def get_adapter(provider: str, **kwargs: Any) -> MailProviderAdapter:
    """Resolve and instantiate an adapter for a provider key (R1.3).

    Raises:
        NotFound: If no adapter has been registered for the provider key.
    """
    key = provider.strip().lower()
    if key not in _REGISTRY and key in ("fake", "gmail", "graph"):
        register_default_adapters()

    factory = _REGISTRY.get(key)
    if factory is None:
        raise NotFound(
            f"No mail provider adapter registered for provider '{provider}'.",
            provider=provider,
        )
    return factory(**kwargs)


def get_adapter_for_mailbox(mailbox: Mailbox, **kwargs: Any) -> MailProviderAdapter:
    """Convenience helper to resolve an adapter using mailbox.provider (R1.3).

    Resolves credentials reference from mailbox.credentials_ref (or env fallback)
    and passes credentials to the adapter instance.
    """
    cred_kwargs = resolve_provider_credentials(mailbox.credentials_ref, mailbox.provider)
    merged_kwargs = {**cred_kwargs, **kwargs}
    return get_adapter(mailbox.provider, mailbox_id=str(mailbox.id), **merged_kwargs)


def is_provider_registered(provider: str) -> bool:
    """Check whether an adapter is registered for the specified provider."""
    key = provider.strip().lower()
    if key not in _REGISTRY and key in ("fake", "gmail", "graph"):
        register_default_adapters()
    return key in _REGISTRY


def list_registered_providers() -> list[str]:
    """Return sorted list of all registered provider names."""
    return sorted(_REGISTRY.keys())


def clear_registry() -> None:
    """Clear all registered adapters (primarily for test teardown)."""
    _REGISTRY.clear()
