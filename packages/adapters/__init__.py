"""Package initialization for packages/adapters.

Exposes MailProviderAdapter protocol, error taxonomy, registry, contract suite,
and concrete adapter implementations (Fake, Gmail).
"""

from packages.adapters.exceptions import (
    DEFAULT_RETRY_AFTER_S,
    AuthExpired,
    NotFound,
    Permanent,
    PermanentProviderError,
    ProviderError,
    RateLimited,
    RetryableProviderError,
    Transient,
    parse_retry_after,
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
from packages.adapters.webhooks import webhook_router

__all__ = [
    "DEFAULT_RETRY_AFTER_S",
    "AuthExpired",
    "FakeProviderAdapter",
    "GmailProviderAdapter",
    "GmailPushNotification",
    "GraphChangeNotification",
    "GraphProviderAdapter",
    "MailProviderAdapter",
    "MicrosoftGraphProviderAdapter",
    "NotFound",
    "Permanent",
    "PermanentProviderError",
    "ProviderError",
    "RateLimited",
    "RetryableProviderError",
    "Transient",
    "clear_registry",
    "get_adapter",
    "get_adapter_for_mailbox",
    "is_provider_registered",
    "list_registered_providers",
    "parse_graph_notification",
    "parse_pubsub_notification",
    "parse_retry_after",
    "register_adapter",
    "register_default_adapters",
    "webhook_router",
]
