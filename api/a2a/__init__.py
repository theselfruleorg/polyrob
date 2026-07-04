"""A2A (Agent-to-Agent) Protocol Implementation for POLYROB Platform.

This module implements Google's A2A Protocol (https://github.com/google/A2A)
enabling POLYROB to both:
1. Expose its capabilities to other A2A-compliant agents (Server mode)
2. Consume services from other A2A agents (Client mode)

Key Components:
- AgentCard: Self-describing manifest of agent capabilities
- Task Management: JSON-RPC endpoints for task lifecycle
- Streaming: SSE-based real-time status updates
- Push Notifications: Webhook callbacks for async updates
- Client: Consume external A2A agent services

Authentication:
- x402 (primary): Pay-per-request crypto payments
- Bearer JWT: For registered users
"""

__all__ = [
    'agent_card_router',
    'a2a_router',
    'streaming_router'
]


def __getattr__(name):
    if name == "agent_card_router":
        from api.a2a.agent_card import router
        return router
    if name == "a2a_router":
        from api.a2a.endpoints import router
        return router
    if name == "streaming_router":
        from api.a2a.streaming import router
        return router
    raise AttributeError(name)
