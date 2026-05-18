"""Hermes Agent native continuous conversation controller."""

from core.hermes import HermesManager
from core.external_conversation import ExternalConversationController


class HermesConversationController(ExternalConversationController):
    """Manages multi-turn conversation for the Hermes Agent native API."""

    CONFIG_PREFIX = "hermes"
    BACKEND_NAME = "Hermes"
    LOG_MODULE = "Hermes Conv"
    WAKEUP_SOURCE = "hermes"
    MANAGER = HermesManager
