"""Package initialization for packages/adapters.

Exposes MailProviderAdapter protocol, error taxonomy, registry, contract suite,
and FakeProviderAdapter implementation.
"""

from packages.adapters.exceptions import (
    AuthExpired,
    NotFound,
    Permanent,
    ProviderError,
    RateLimited,
    Transient,
)
from packages.adapters.fake import FakeProviderAdapter
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import (
    clear_registry,
    get_adapter,
    get_adapter_for_mailbox,
    is_provider_registered,
    list_registered_providers,
    register_adapter,
)
from packages.adapters.testing import MailProviderAdapterContractSuite

__all__ = [
    "AuthExpired",
    "FakeProviderAdapter",
    "MailProviderAdapter",
    "MailProviderAdapterContractSuite",
    "NotFound",
    "Permanent",
    "ProviderError",
    "RateLimited",
    "Transient",
    "clear_registry",
    "get_adapter",
    "get_adapter_for_mailbox",
    "is_provider_registered",
    "list_registered_providers",
    "register_adapter",
]
