"""WS-A hardening (Fusion CRITICAL): local-mode owner must NOT apply to network surfaces.

`is_owner(local=True)` grants OWNER to any non-empty uid (single-user local). That is
correct for the CLI/local surface, but on a NETWORK surface (email/telegram/whatsapp)
the principal is a forgeable remote address — local mode must NOT auto-own it, else any
email sender becomes an owner command-turn.
"""
import os
import tempfile

import pytest

from core.surfaces.access import AccessTier, resolve_access_tier
from core.surfaces.envelopes import Identity, SessionSource


class _Container:
    def __init__(self):
        self.config = type("C", (), {"data_dir": tempfile.mkdtemp()})()

    def get_service(self, name):
        return None


def _identity(uid, surface):
    return Identity(user_id=uid, source=SessionSource(surface_id=surface, chat_id="c", chat_type="dm"),
                    raw_user_id=uid)


def test_local_mode_does_not_own_email_sender():
    env = {"POLYROB_LOCAL": "true"}
    # no owner bound, no correspondent binding -> a network sender is DENIED, not OWNER
    assert resolve_access_tier(_Container(), _identity("john@acme.com", "email"), env=env) == AccessTier.DENIED


def test_local_mode_does_not_own_telegram_sender():
    env = {"POLYROB_LOCAL": "true"}
    assert resolve_access_tier(_Container(), _identity("99999", "telegram"), env=env) == AccessTier.DENIED


def test_local_mode_still_owns_cli_surface():
    env = {"POLYROB_LOCAL": "true"}
    assert resolve_access_tier(_Container(), _identity("u_local", "cli"), env=env) == AccessTier.OWNER
