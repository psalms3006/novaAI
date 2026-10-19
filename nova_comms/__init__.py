"""NOVA Communications Module - Voice calling and messaging providers."""

from .provider import (
    CallStatus,
    CallResult,
    CallDirection,
    ICommunicationProvider,
)
from .models import (
    PhoneNumber,
    PhoneNumberType,
    PhoneNumberCapability,
    CallRecording,
    CallTranscript,
    WebhookEvent,
    DTMFEvent,
)
from .registry import ProviderRegistry
from .notifications import (
    NotificationService,
    DeliveryStateStore,
    NotificationPreferences,
    NotificationEvent,
    DeliveryRecord,
    ChannelType,
    DeliveryStatus,
    EventType,
    ChannelConfig,
    QuietHours,
    RetryPolicy,
    EscalationRule,
    get_notification_service,
    init_notification_service,
)
from .integration import (
    CommsIntegration,
    get_comms_integration,
    init_comms_integration,
)
from .providers import (
    TwilioProvider,
    BlandProvider,
    MockProvider,
    MockCallOutcome,
    MockCallConfig,
)

# Import providers to trigger auto-registration
from . import providers  # noqa: F401

__all__ = [
    "CallStatus",
    "CallResult",
    "CallDirection",
    "ICommunicationProvider",
    "PhoneNumber",
    "PhoneNumberType",
    "PhoneNumberCapability",
    "CallRecording",
    "CallTranscript",
    "WebhookEvent",
    "DTMFEvent",
    "ProviderRegistry",
    "NotificationService",
    "DeliveryStateStore",
    "NotificationPreferences",
    "NotificationEvent",
    "DeliveryRecord",
    "ChannelType",
    "DeliveryStatus",
    "EventType",
    "ChannelConfig",
    "QuietHours",
    "RetryPolicy",
    "EscalationRule",
    "get_notification_service",
    "init_notification_service",
    "CommsIntegration",
    "get_comms_integration",
    "init_comms_integration",
    "TwilioProvider",
    "BlandProvider",
    "MockProvider",
    "MockCallOutcome",
    "MockCallConfig",
]