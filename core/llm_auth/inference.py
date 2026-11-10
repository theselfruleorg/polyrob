"""The credential of one inference call: the owner's, and fresh."""
import asyncio
import logging
import time

from core.exceptions import LLMConnectionError
from core.inference_context import inference_user_id
from core.llm_auth.health import token_expired

logger = logging.getLogger(__name__)


def _require_owner() -> None:
    from core.llm_auth.resolve import _tenant_allowed
    user_id = inference_user_id()
    if not isinstance(user_id, str) or not user_id or not _tenant_allowed(user_id):
        raise PermissionError('Owner subscription credentials require an authenticated owner session')


def require_credential_access(client):
    """A globally cached bearer is still the owner's, on every inference call. This runs on
    the event loop, so it only reads: ``refresh_credential`` ran before the call."""
    source = getattr(client, '_credential_source', None)
    auth = getattr(getattr(client, '_spec', None), 'auth_type', None)
    auth = getattr(auth, 'value', auth)
    private = source in ('oauth', 'borrowed') or auth in ('oauth_device', 'oauth_pkce', 'borrowed')
    if not private:
        return
    _require_owner()
    read_credential(client)


async def refresh_credential(client) -> None:
    """Before a call on a store-served OAuth credential: refresh a token near its expiry, off
    the event loop, because the token endpoint can take 30 s. Any other client returns here
    with no thread and no store read."""
    if getattr(client, '_credential_source', None) != 'oauth':
        return
    spec = getattr(client, '_spec', None)
    provider = getattr(spec, 'name', None)
    oauth = getattr(spec, 'oauth', None)
    if not provider or oauth is None:
        return
    _require_owner()
    from core.llm_auth.store import AuthStoreUnreadable, get_auth_store
    try:
        await asyncio.to_thread(_refresh, provider, oauth, get_auth_store(), time.time())
    except AuthStoreUnreadable as exc:
        logger.warning("%s: no refresh: %s", provider, exc)


def _relogin(provider, detail):
    from core.llm_auth.errors import LLMAuthError
    return LLMAuthError(
        f"{provider}: {detail}. Connect the account again: polyrob auth add {provider}",
        code="relogin_required", provider=provider, relogin_required=True)


def _refresh(provider, oauth, store, now):
    """Refresh at the provider's own skew, under the flows' per-provider lock and the store's
    flock. A failure before the expiry keeps the token that still works."""
    from core.llm_auth.flows.base import TRANSIENT_CODE, FlowError
    from core.llm_auth.flows.refresh import refresh_token
    try:
        refresh_token(provider, oauth, store=store, now=now)
    except FlowError as exc:
        entry = store.get_provider(provider) or {}
        if entry.get("access_token") and not token_expired(entry.get("expires_at"), now):
            logger.warning("%s: token refresh failed, the current token is still valid: %s", provider, exc)
            return
        if exc.code == TRANSIENT_CODE:
            raise LLMConnectionError(f"{provider}: the token endpoint failed: {exc}") from exc
        raise _relogin(provider, f"the token refresh failed ({exc})") from exc


def _install_token(client, token) -> None:
    """Give the client and its SDK object the current token. Both read it per request."""
    if token == getattr(client, 'api_key', None):
        return
    client.api_key = token
    sdk = getattr(client, '_client', None)
    for attr in ('auth_token', 'api_key'):
        if sdk is not None and getattr(sdk, attr, None):
            setattr(sdk, attr, token)


def read_credential(client) -> None:
    """Give a store-served client the stored token: this also takes a token that another
    process refreshed. An expired or removed one refuses as relogin required. A client with
    no provider spec or no oauth block has nothing to read."""
    source = getattr(client, '_credential_source', None)
    if source not in ('oauth', 'borrowed'):
        return
    spec = getattr(client, '_spec', None)
    provider = getattr(spec, 'name', None)
    if not provider:
        return
    if source == 'oauth' and getattr(spec, 'oauth', None) is None:
        return
    from core.llm_auth.store import AuthStoreUnreadable, get_auth_store
    store = get_auth_store()
    try:
        entry = store.get_provider(provider) if source == 'oauth' else store.get_borrowed(provider)
    except AuthStoreUnreadable as exc:
        logger.warning("%s: keeping the cached token: %s", provider, exc)
        return
    if not entry or not entry.get("access_token"):
        raise _relogin(provider, "the stored credential is gone")
    if token_expired(entry.get("expires_at"), time.time()):
        raise _relogin(provider, "the token expired")
    _install_token(client, str(entry["access_token"]))
