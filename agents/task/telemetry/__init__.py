"""
Telemetry package for task.

This package provides functionality for capturing telemetry events
and metrics from the task system.
"""

import logging

# Setup logging
from agents.task.logging_config import get_task_logger
logger = get_task_logger('telemetry')
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(levelname)-8s [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Import primary classes - safe imports that don't create circular dependencies
from agents.task.telemetry.views import (
    BaseTelemetryEvent,
    AgentRunTelemetryEvent,
    AgentStepTelemetryEvent,
    AgentEndTelemetryEvent,
    LLMRequestTelemetryEvent,
    SessionRelationshipTelemetryEvent,
    InterfaceTelemetryEvent,
    SessionStartTelemetryEvent,
    SessionCompletionTelemetryEvent,
    ProviderFailureEvent,
    ProviderFallbackSuccessEvent
)

# Lazy import ProductTelemetry to avoid circular references
# This way consumers can import the module without triggering additional imports
ProductTelemetry = None

def get_telemetry():
    """Get the ProductTelemetry instance safely using singleton pattern"""
    from agents.task.path import get_safe_singleton
    from agents.task.telemetry.service import ProductTelemetry
    return get_safe_singleton(ProductTelemetry)()

__all__ = [
    "get_telemetry",  # New function for safe access
    "BaseTelemetryEvent",
    "AgentRunTelemetryEvent",
    "AgentStepTelemetryEvent",
    "AgentEndTelemetryEvent",
    "LLMRequestTelemetryEvent",
    "SessionRelationshipTelemetryEvent",
    "InterfaceTelemetryEvent",
    "SessionStartTelemetryEvent",
    "SessionCompletionTelemetryEvent",
    "ProviderFailureEvent",
    "ProviderFallbackSuccessEvent"
] 