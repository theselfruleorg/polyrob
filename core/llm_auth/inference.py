"""Per-call tenancy for cached owner subscription credentials."""
from core.inference_context import inference_user_id


def require_credential_access(client):
    """A globally cached bearer is still the owner's, on every inference call."""
    source = getattr(client, '_credential_source', None)
    auth = getattr(getattr(client, '_spec', None), 'auth_type', None)
    auth = getattr(auth, 'value', auth)
    private = source in ('oauth', 'borrowed') or auth in ('oauth_device', 'oauth_pkce', 'borrowed')
    if not private:
        return
    from core.llm_auth.resolve import _tenant_allowed
    user_id = inference_user_id()
    if not isinstance(user_id, str) or not user_id or not _tenant_allowed(user_id):
        raise PermissionError('Owner subscription credentials require an authenticated owner session')
