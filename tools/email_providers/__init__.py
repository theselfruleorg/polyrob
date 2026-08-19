"""Email transport providers (Phase 1, 2026-08-18 agent-mail plan).

The `email` tool speaks two transports behind ONE public surface
(`send_email_ex` / `read_emails` / the `email_send` action):

- ``smtp`` — the legacy stdlib smtplib/imaplib path (GMAIL_* creds), unchanged.
- ``agentmail`` — a managed HTTP inbox (api.agentmail.to): the agent provisions
  its OWN address idempotently on first run, so mail works with one env var
  (``AGENTMAIL_API_KEY``) and zero mailbox setup.

Provider selection: ``core.config_policy.policy.email_provider()``.
"""
from tools.email_providers.agentmail import AgentMailClient

__all__ = ["AgentMailClient"]
