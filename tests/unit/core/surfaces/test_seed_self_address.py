"""031 T15: the agent's OWN address can never become a correspondent."""


def test_is_self_address_matches_the_agent_email_case_insensitively(monkeypatch):
    monkeypatch.setenv("POLYROB_AGENT_EMAIL", "rob@theselfrule.org")
    monkeypatch.delenv("GMAIL_EMAIL", raising=False)
    from core.surfaces import seed
    assert seed.is_self_address("Rob@TheSelfRule.org") is True
    assert seed.is_self_address(" rob@theselfrule.org ") is True
    assert seed.is_self_address("someone@else.org") is False
    assert seed.is_self_address("") is False
    monkeypatch.setenv("GMAIL_EMAIL", "legacy@gmail.com")
    assert seed.is_self_address("LEGACY@gmail.com") is True


def test_self_address_seed_is_refused(monkeypatch):
    monkeypatch.setenv("POLYROB_AGENT_EMAIL", "rob@theselfrule.org")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    from core.surfaces import seed
    calls = []
    reg = type("R", (), {"exists": lambda *a, **k: False, "count_seeds_since": lambda *a, **k: 0,
                         "seed": lambda *a, **k: calls.append(k) or "active"})()
    container = type("C", (), {"get_service": lambda self, name: reg})()
    out = seed.maybe_seed_correspondent(container, surface="email", address="rob@theselfrule.org",
                                        session_id="s", user_id="rob")
    assert out == "refused_self" and calls == []


def test_registry_deactivate_expires_only_the_active_row(tmp_path):
    from core.surfaces.correspondents import CorrespondentRegistry, STATE_EXPIRED
    reg = CorrespondentRegistry(str(tmp_path / "c.db"))
    reg.seed(surface="email", address="rob@theselfrule.org", session_id="s1", user_id="rob",
             require_approval=False)
    rows = reg.list(user_id="rob")
    assert rows and rows[0]["state"] == "active"
    assert reg.deactivate(surface="email", address="rob@theselfrule.org", user_id="rob") is True
    assert reg.list(user_id="rob")[0]["state"] == STATE_EXPIRED
    assert reg.deactivate(surface="email", address="rob@theselfrule.org", user_id="rob") is False


def test_boot_sweep_removes_self_bindings(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("POLYROB_AGENT_EMAIL", "rob@theselfrule.org")
    from core.surfaces.correspondents import CorrespondentRegistry
    from core import autonomy_runtime as ar
    monkeypatch.setattr(ar, "_self_binding_sweep_scheduled", False)  # once-per-process flag
    reg = CorrespondentRegistry(str(tmp_path / "c.db"))
    reg.seed(surface="email", address="rob@theselfrule.org", session_id="s1", user_id="rob",
             require_approval=False)
    reg.seed(surface="email", address="friend@x.org", session_id="s1", user_id="rob",
             require_approval=False)
    container = type("C", (), {"get_service": lambda self, n: reg if n == "correspondent_registry" else None})()
    agent = type("A", (), {"container": container})()

    async def _run():
        ar._schedule_self_binding_sweep(agent)
        await asyncio.gather(*list(ar._BACKGROUND_TASKS), return_exceptions=True)
    asyncio.run(_run())
    states = {r["address"]: r["state"] for r in reg.list(user_id="rob")}
    assert states["rob@theselfrule.org"] == "expired" and states["friend@x.org"] == "active"


class TestPlusAddressing:
    """A sub-addressed copy of the agent's OWN address is still the agent.

    `rob+goal42@x.org` and `rob@x.org` are one mailbox on every provider that
    implements RFC 5233 sub-addressing (agentmail, gmail, fastmail, outlook), so a
    sent copy that comes back tagged must hit the same "never seed self" refusal —
    otherwise it is bound as a correspondent and re-enters its own sending session,
    which is the prod loop 1ed9a1e7 closed (five re-runs of a finished treasury
    goal off the agent's own sent mail).
    """

    def test_tagged_copy_of_own_address_is_self(self, monkeypatch):
        from core.surfaces.seed import is_self_address
        monkeypatch.setenv("POLYROB_AGENT_EMAIL", "rob@theselfrule.org")
        assert is_self_address("rob+goal42@theselfrule.org") is True
        assert is_self_address("ROB+Goal42@TheSelfRule.org") is True

    def test_configured_address_may_itself_be_tagged(self, monkeypatch):
        from core.surfaces.seed import is_self_address
        monkeypatch.setenv("POLYROB_AGENT_EMAIL", "rob+bot@theselfrule.org")
        assert is_self_address("rob@theselfrule.org") is True

    def test_a_different_local_part_is_not_self(self, monkeypatch):
        from core.surfaces.seed import is_self_address
        monkeypatch.setenv("POLYROB_AGENT_EMAIL", "rob@theselfrule.org")
        assert is_self_address("robert@theselfrule.org") is False
        assert is_self_address("rob@evil.org") is False
        # A '+' in the DOMAIN is not sub-addressing and must not fold.
        assert is_self_address("rob@theselfrule.org+evil.com") is False

    def test_a_malformed_address_never_matches(self, monkeypatch):
        from core.surfaces.seed import is_self_address
        monkeypatch.setenv("POLYROB_AGENT_EMAIL", "rob@theselfrule.org")
        assert is_self_address("not-an-email") is False
        assert is_self_address("@theselfrule.org") is False
        assert is_self_address("") is False
