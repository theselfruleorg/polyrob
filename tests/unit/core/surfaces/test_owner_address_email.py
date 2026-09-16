"""A7 / A40: `owner_address(container, "email", user_id)` must resolve the
owner's email even when *container* is not a mapping (a DependencyContainer,
or any object with no `.get`).

Before this fix, the email branch called
``resolve_owner_email(container)`` — but ``resolve_owner_email``'s signature
is ``resolve_owner_email(env: Optional[Mapping[str, str]] = None)``, which
reads ``container.get(...)``. A real ``DependencyContainer`` has no ``.get``
(only ``get_service``), so every call raised ``AttributeError``, silently
swallowed by the blanket ``except Exception: return None`` — the owner's
email target was dead on every call site that passed a container.
"""
import pytest


class _ContainerStandIn:
    """A DependencyContainer stand-in with no `.get` — exactly the shape that
    used to blow up ``resolve_owner_email(container)``."""


def test_email_target_resolves_with_a_container(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "own@example.com")
    from core.surfaces.owner_address import owner_address
    assert owner_address(_ContainerStandIn(), "email", "u1") == "own@example.com"


def test_email_target_none_when_unset(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("BOT_OWNER_EMAIL", raising=False)
    from core.surfaces.owner_address import owner_address
    assert owner_address(_ContainerStandIn(), "email", "u1") is None


def test_email_target_resolves_with_container_none(monkeypatch):
    """The pre-existing ``container=None`` call shape (e.g. the CLI) stays
    green — this fix must not regress the already-working path."""
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "cli@example.com")
    from core.surfaces.owner_address import owner_address
    assert owner_address(None, "email", "u1") == "cli@example.com"
