"""P3 — CLI commands resolve the tenant via the IdentityProvider seam, not a hardcoded
"local" (findings F3). A swapped identity must change the tenant the command uses, so
identity resolution has a single source of truth instead of drifting string literals.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from core.identity import ConstantIdentity


def test_session_cancel_resolves_user_via_identity_seam(tmp_path):
    """113 T-04: the tenant is the identity seam's, through the admin seam's owner
    tenant (the deployed owner on a box); the session is read from the admin home."""
    from cli.commands import session as sess

    seen = {}

    def _owner_tenant(uid):
        seen["uid"] = uid
        return uid

    with patch("core.identity.resolve_identity", lambda: "tenantZ"), \
         patch("cli._admin_home.admin_owner_tenant", _owner_tenant), \
         patch.object(sess, "_data_root", lambda write=False: tmp_path), \
         patch.object(sess, "ensure_env_loaded", lambda: None), \
         patch("cli.commands._session_control.control_live_session", new_callable=AsyncMock) as control:
        asyncio.run(sess._session_cancel("sess-1"))

    assert seen["uid"] == "tenantZ"
    control.assert_awaited_once_with(None, None, "tenantZ", "sess-1", "cancel")
