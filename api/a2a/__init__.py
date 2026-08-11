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

Authentication:
- x402 (primary): Pay-per-request crypto payments
- Bearer JWT: For registered users

Routers are imported directly from their modules (api.a2a.agent_card,
api.a2a.endpoints, api.a2a.streaming) — see api/app.py.
"""
