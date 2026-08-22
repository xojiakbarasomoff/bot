"""Channel adapters, and the registration that makes them findable.

Importing this package is what puts an adapter in the registry, so anything
resolving a channel by type (app.services.delivery) imports from here rather
than from a concrete platform module. When the Telegram bot merges in, its
adapter is registered on the line below the Instagram one and nothing else
in the codebase changes.
"""

from app.channels.base import (
    ChannelAdapter,
    ChannelType,
    DeliveryBlocked,
    UnknownChannelTypeError,
    get_adapter,
    register_adapter,
    registered_channel_types,
)
from app.channels.instagram.adapter import get_instagram_adapter

register_adapter(get_instagram_adapter())

__all__ = [
    "ChannelAdapter",
    "ChannelType",
    "DeliveryBlocked",
    "UnknownChannelTypeError",
    "get_adapter",
    "register_adapter",
    "registered_channel_types",
]
