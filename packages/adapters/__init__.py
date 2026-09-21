"""Package initialization for packages/adapters.

Exposes MailProviderAdapter protocol, error taxonomy, registry, contract suite,
and concrete adapter implementations (Fake, Gmail).
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
from packages.adapters.gmail import (
    GmailProviderAdapter,
    GmailPushNotification,
    parse_pubsub_notification,
)
from packages.adapters.graph import (
    GraphChangeNotification,
    GraphProviderAdapter,
    MicrosoftGraphProviderAdapter,
    parse_graph_notification,
)
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import (
    clear_registry,
    get_adapter,
    get_adapter_for_mailbox,
    is_provider_registered,
    list_registered_providers,
    register_adapter,
    register_default_adapters,
)

try:
    from packages.adapters.testing import MailProviderAdapterContractSuite
except ImportError:
    MailProviderAdapterContractSuite = None  # type: ignore[assignment, misc]
from packages.adapters.webhooks import webhook_router

__all__ = [
    "AuthExpired",
    "FakeProviderAdapter",
    "GmailProviderAdapter",
    "GmailPushNotification",
    "GraphChangeNotification",
    "GraphProviderAdapter",
    "MailProviderAdapter",
    "MailProviderAdapterContractSuite",
    "MicrosoftGraphProviderAdapter",
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
    "parse_graph_notification",
    "parse_pubsub_notification",
    "register_adapter",
    "register_default_adapters",
    "webhook_router",
]
