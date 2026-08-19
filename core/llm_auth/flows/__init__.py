"""OAuth connect flows for LLM subscription seats (proposal 024, L2).

Provider-AGNOSTIC by design. The flows are driven entirely by ``OAuthSpec``
data (endpoints, client_id, scopes, grant, refresh skew) — this package ships
**no** built-in provider rows.

That is a deliberate call, not an omission. The subscription plans people most
want to connect (Claude Pro/Max, ChatGPT Plus) issue no OAuth client_id to
third-party applications; the only ids that work are the ones embedded in those
vendors' own CLIs, and presenting POLYROB as their client is exactly the terms-
of-service exposure §7.4 warns about. So the machinery is here and the
credential is yours: declare a provider with an ``oauth:`` block naming a
client_id you are entitled to use, and every flow, refresh and surface works.

Layering: this package is core-tier and imports no ``modules.*``/``tools.*``
code. In particular it does NOT import ``tools/oauth/manager.py`` — that
manager serializes refresh for the MCP token store and lives in the agent tier,
which core must not import. Its *discipline* is reused rather than its code:
one lock per provider plus an identity guard on the observed token (see
``registry.refresh_if_needed``), with cross-process safety coming from the
``flock`` the auth store already holds.
"""
from core.llm_auth.flows.manual_paste_pkce import run_manual_paste_pkce_flow
from core.llm_auth.flows.base import (
    FlowError,
    FlowResult,
    oauth_enabled,
)
from core.llm_auth.flows.registry import (
    UnsupportedGrantError,
    connect,
    disconnect,
    get_flow,
    refresh_if_needed,
)

__all__ = [
    "FlowError",
    "FlowResult",
    "UnsupportedGrantError",
    "connect",
    "disconnect",
    "get_flow",
    "oauth_enabled",
    "refresh_if_needed",
    "run_manual_paste_pkce_flow",
]
