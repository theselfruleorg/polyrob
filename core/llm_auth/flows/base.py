"""Shared flow types, the gate, and the injectable HTTP seam (024 L2)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from core.llm_auth.errors import LLMAuthError

#: How long a whole interactive connect may take before we give up. A device
#: code typically expires in 5-15 min; this is the hard backstop so a flow can
#: never wedge a terminal (or a script) forever.
DEFAULT_FLOW_TIMEOUT_SEC = 600.0

#: Network timeout for a single token/authorization request.
HTTP_TIMEOUT_SEC = 30.0


class FlowError(LLMAuthError):
    """A connect flow could not complete. Message is user-facing and must never
    contain a token (see ``_safe_error_body``)."""


def oauth_enabled(env=None) -> bool:
    """Gate for the OAuth connect flows — ``LLM_OAUTH_ENABLED``, default OFF.

    Off EVERYWHERE including local (024 §7.4), unlike most local-profile flags:
    connecting a subscription seat is a deliberate owner act with a terms-of-
    service dimension, so it is never switched on as a side effect of running in
    local mode.
    """
    from core.env import bool_env, parse_bool
    if env is not None:
        return parse_bool(env.get("LLM_OAUTH_ENABLED"), False)
    return bool_env("LLM_OAUTH_ENABLED", False)


@dataclass(frozen=True)
class FlowResult:
    """What a completed flow yields, ready to persist into the auth store."""
    access_token: str = field(repr=False)
    refresh_token: Optional[str] = field(repr=False, default=None)
    expires_at: Optional[float] = None
    scope: str = ""
    token_type: str = "Bearer"

    def __repr__(self) -> str:  # never let a token reach a log or a traceback
        return (
            f"FlowResult(token_type={self.token_type!r}, scope={self.scope!r}, "
            f"expires_at={self.expires_at!r}, access_token=<redacted>, "
            f"refresh_token={'<redacted>' if self.refresh_token else None})"
        )

    def to_entry(self) -> Dict[str, Any]:
        """The auth-store provider entry for this result."""
        entry: Dict[str, Any] = {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "obtained_at": time.time(),
        }
        if self.refresh_token:
            entry["refresh_token"] = self.refresh_token
        if self.expires_at is not None:
            entry["expires_at"] = self.expires_at
        if self.scope:
            entry["scope"] = self.scope
        return entry


#: An HTTP poster: (url, form_fields) -> parsed JSON dict. Injected so every
#: flow is testable without a network, and so a deployment can route through its
#: own client if it must.
HttpPost = Callable[[str, Dict[str, str]], Dict[str, Any]]


def _safe_error_body(body: str, limit: int = 400) -> str:
    """Scrub + truncate a provider error body before it reaches a user.

    An OAuth error response can echo back the very token or code that failed,
    so this never goes out raw — it runs the shared shape battery first.
    """
    try:
        from core.secret_patterns import apply_ssot_shapes
        body = apply_ssot_shapes(body)
    except Exception:
        pass
    body = " ".join(body.split())
    return body[:limit]


def post_form(url: str, fields: Dict[str, str], *, timeout: float = HTTP_TIMEOUT_SEC,
              headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """POST an ``application/x-www-form-urlencoded`` body, return parsed JSON.

    The default :data:`HttpPost`. A non-2xx with a JSON body is returned as
    parsed JSON rather than raised — the OAuth specs carry meaningful state
    (``authorization_pending``, ``slow_down``) in 4xx bodies, and the device
    flow must be able to act on it rather than treat it as a hard failure.
    """
    data = urllib.parse.urlencode(fields).encode("ascii")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            return json.loads(raw)
        except ValueError:
            raise FlowError(
                f"{url} returned HTTP {exc.code}: {_safe_error_body(raw)}"
            ) from None
    except urllib.error.URLError as exc:
        raise FlowError(f"could not reach {url}: {exc.reason}") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise FlowError(f"{url} returned non-JSON: {_safe_error_body(raw)}") from None


def post_json(url: str, fields: Dict[str, str], *, timeout: float = HTTP_TIMEOUT_SEC,
              headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """POST a JSON body, return parsed JSON. Same error contract as post_form.

    Needed because not every provider follows RFC 8628's form encoding for the
    device-authorization leg (OpenAI takes JSON).
    """
    body = json.dumps(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            return json.loads(raw)
        except ValueError:
            raise FlowError(
                f"{url} returned HTTP {exc.code}: {_safe_error_body(raw)}"
            ) from None
    except urllib.error.URLError as exc:
        raise FlowError(f"could not reach {url}: {exc.reason}") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise FlowError(f"{url} returned non-JSON: {_safe_error_body(raw)}") from None


def expires_at_from(payload: Dict[str, Any], now: Optional[float] = None) -> Optional[float]:
    """Absolute expiry from an ``expires_in`` (seconds) token response.

    Absolute, not relative: the value is persisted, and a relative one silently
    becomes wrong the moment it is written to disk.
    """
    raw = payload.get("expires_in")
    if raw is None:
        return None
    try:
        return (time.time() if now is None else now) + float(raw)
    except (TypeError, ValueError):
        return None


def result_from_token_payload(payload: Dict[str, Any],
                              now: Optional[float] = None) -> FlowResult:
    """Build a FlowResult from a standard OAuth token response."""
    token = payload.get("access_token")
    if not token:
        error = payload.get("error") or "no access_token in token response"
        desc = payload.get("error_description") or ""
        raise FlowError(f"{error}{': ' + desc if desc else ''}")
    return FlowResult(
        access_token=str(token),
        refresh_token=str(payload["refresh_token"]) if payload.get("refresh_token") else None,
        expires_at=expires_at_from(payload, now),
        scope=str(payload.get("scope") or ""),
        token_type=str(payload.get("token_type") or "Bearer"),
    )


#: Sent on the Copilot exchange. GitHub gates the endpoint on looking like an
#: editor integration; a bare client is rejected.
EXCHANGE_USER_AGENT = "GitHubCopilotChat/0.26.7"
EXCHANGE_EDITOR_VERSION = "vscode/1.104.1"


def exchange_token(exchange_url: str, raw_token: str, *,
                   timeout: float = HTTP_TIMEOUT_SEC) -> "FlowResult":
    """Trade an OAuth token for the credential the inference API actually wants.

    GitHub Copilot's OAuth token is NOT usable against ``api.githubcopilot.com``
    — it must be exchanged at ``copilot_internal/v2/token`` for a short-lived
    (~30 min) token. Without this leg a connected Copilot seat authenticates
    fine and then 401s on every request.

    The exchange also advertises an account-specific ``endpoints.api`` host for
    Enterprise/proxied accounts; it is returned in ``scope`` so the caller can
    persist it. Individual accounts omit it.
    """
    req = urllib.request.Request(
        exchange_url, method="GET",
        headers={
            "Authorization": f"token {raw_token}",
            "User-Agent": EXCHANGE_USER_AGENT,
            "Accept": "application/json",
            "Editor-Version": EXCHANGE_EDITOR_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        if exc.code in (401, 403, 404):
            # Not retryable and not a network problem: the account has no
            # Copilot entitlement, or the token was revoked/org-blocked. Say so
            # rather than surfacing a bare status.
            raise FlowError(
                f"this account is not entitled to Copilot (HTTP {exc.code}). "
                "A classic ghp_ token also cannot be exchanged — reconnect with "
                "`polyrob auth add github-copilot`."
            ) from None
        raise FlowError(
            f"Copilot token exchange failed (HTTP {exc.code}): {_safe_error_body(body)}"
        ) from None
    except urllib.error.URLError as exc:
        raise FlowError(f"could not reach {exchange_url}: {exc.reason}") from None

    token = data.get("token")
    if not token:
        raise FlowError("token exchange returned no token")
    expires_at = data.get("expires_at")
    try:
        expires_at = float(expires_at) if expires_at else None
    except (TypeError, ValueError):
        expires_at = None
    endpoints = data.get("endpoints")
    api_host = ""
    if isinstance(endpoints, dict):
        api_host = str(endpoints.get("api") or "").strip().rstrip("/")
    return FlowResult(access_token=str(token), expires_at=expires_at,
                      scope=api_host, token_type="Bearer")


def require_oauth_spec_obj(oauth: Any, provider: str):
    """Validate an OAuthSpec handed in by the caller, or raise a FlowError
    naming exactly what to declare.

    Takes the OAuthSpec itself, not a ProviderSpec to dig it out of: core must
    not import the provider registry (it lives in ``modules.llm``), so the
    surface resolves the provider and passes the block down.

    This is where the "no built-in provider rows" decision meets the user, so
    the message says what to add rather than just refusing.
    """
    if oauth is None:
        raise FlowError(
            f"provider '{provider}' declares no oauth block. POLYROB ships no "
            "built-in OAuth providers — the plans people most want to connect "
            "issue no client_id to third-party apps, and borrowing a vendor "
            "CLI's id would make POLYROB impersonate their product. Declare "
            f"'{provider}' in ~/.polyrob/providers.yaml with an oauth: block "
            "(auth_url, token_url, client_id, scopes, grant) using a client_id "
            "you are entitled to use."
        )
    if not getattr(oauth, "client_id", ""):
        raise FlowError(f"provider '{provider}' oauth block has no client_id")
    if not getattr(oauth, "token_url", ""):
        raise FlowError(f"provider '{provider}' oauth block has no token_url")
    return oauth
