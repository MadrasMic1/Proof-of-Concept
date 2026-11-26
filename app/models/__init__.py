# app/models/__init__.py
"""Importing this package registers all 33 tables on Base.metadata."""

from app.db.base import Base
from app.models.agentic import (
    AgentChatMessage,
    AgentChatSession,
    AgentIncidentDraft,
    AgentKbArticle,
    AgentKbChunk,
    AgentKbGuideSession,
    AgentLlmCall,
    AgentStoreDependency,
    AgentToolCall,
)
from app.models.ai import AiPromptVersion, AiWorkflowRun
from app.models.audit import ApiRequestLog, AuditLog, EventProcessingRun
from app.models.communication import (
    Notification,
    NotificationTarget,
    StoreAction,
    StoreActionResponse,
    StoreAlert,
)
from app.models.inventory import (
    Device,
    NetworkElement,
    NetworkElementCurrentStatus,
    NetworkEvent,
    RawNetworkEvent,
)
from app.models.itsm import Appointment, Incident, IncidentJournal, ItsmInbox, ItsmSyncState
from app.models.master import AssignmentGroup, DeviceType, NetworkElementType, Store

__all__ = [
    "AgentChatMessage",
    "AgentChatSession",
    "AgentIncidentDraft",
    "AgentKbArticle",
    "AgentKbChunk",
    "AgentKbGuideSession",
    "AgentLlmCall",
    "AgentStoreDependency",
    "AgentToolCall",
    "AiPromptVersion",
    "AiWorkflowRun",
    "ApiRequestLog",
    "Appointment",
    "AssignmentGroup",
    "AuditLog",
    "Base",
    "Device",
    "DeviceType",
    "EventProcessingRun",
    "Incident",
    "IncidentJournal",
    "ItsmInbox",
    "ItsmSyncState",
    "NetworkElement",
    "NetworkElementCurrentStatus",
    "NetworkElementType",
    "NetworkEvent",
    "Notification",
    "NotificationTarget",
    "RawNetworkEvent",
    "Store",
    "StoreAction",
    "StoreActionResponse",
    "StoreAlert",
]
