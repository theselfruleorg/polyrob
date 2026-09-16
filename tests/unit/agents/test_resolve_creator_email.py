"""An email session is never `creator="owner"` (043 T4).

Owner-by-email is OFF in v1: a `From:` header is forgeable, so every email
sender resolves correspondent-or-denied (AGENTS.md, chat-surface access model).
`email` nonetheless sat in `_OWNER_CHAT_SURFACES`, so a session created from an
email turn was labelled `owner` — which the console renders as "you". That is a
claim about WHO started a session that nothing had authenticated.

The honest label is `correspondent` when the source names that tier, else `api`
("a program"). `api` says only "something outside the owner's chat seats", which
is exactly what is known.
"""
from agents.task.agent.session import (
    SESSION_CREATOR_KINDS, _OWNER_CHAT_SURFACES, resolve_creator)


class _Source:
    def __init__(self, surface_id, tier=None):
        self.surface_id = surface_id
        if tier is not None:
            self.tier = tier


def test_email_is_not_an_owner_chat_surface():
    assert "email" not in _OWNER_CHAT_SURFACES


def test_email_session_source_resolves_to_api_not_owner():
    from core.surfaces.envelopes import SessionSource
    src = SessionSource(surface_id="email", chat_id="someone@example.com")
    assert resolve_creator(None, src) == "api"


def test_email_source_naming_the_correspondent_tier_says_so():
    """When the source DOES carry the tier, the label is the specific truth."""
    assert resolve_creator(None, _Source("email", tier="correspondent")) == "correspondent"


def test_the_label_is_still_a_known_kind():
    from core.surfaces.envelopes import SessionSource
    src = SessionSource(surface_id="email", chat_id="someone@example.com")
    assert resolve_creator(None, src) in SESSION_CREATOR_KINDS


def test_an_explicit_creator_still_wins_for_email():
    """The correspondent RESUME path passes `creator=` explicitly
    (agents/task/conversation_resume.py) and must be unaffected."""
    from core.surfaces.envelopes import SessionSource
    src = SessionSource(surface_id="email", chat_id="someone@example.com")
    assert resolve_creator("correspondent", src) == "correspondent"


def test_the_other_chat_surfaces_are_untouched():
    from core.surfaces.envelopes import SessionSource
    for surface_id in ("telegram", "whatsapp", "discord", "slack", "signal",
                       "x", "webview"):
        src = SessionSource(surface_id=surface_id, chat_id="c")
        assert resolve_creator(None, src) == "owner", surface_id
