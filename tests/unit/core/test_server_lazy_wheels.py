"""A trusted non-custody server cannot execute a source build backend."""
import pytest

@pytest.mark.parametrize("local,wheels", [(False, True), (True, False)])
def test_trusted_unprovisioned_profile_install_policy(monkeypatch, local, wheels):
    import core.lazy_deps as ld
    monkeypatch.setattr(ld, "spool_provisioned", lambda: False)
    monkeypatch.setattr(ld, "_custody", lambda: False)
    monkeypatch.setattr(ld, "_local_mode", lambda: local)
    monkeypatch.setattr(ld, "is_available", lambda _: True)
    seen = []
    monkeypatch.setattr(ld, "_trusted_install", lambda feature, **kw: seen.append(kw))
    ld._ensure_trusted("provider.anthropic", ("anthropic",), prompt=False, wait=None)
    assert seen == [{"wheel_only": wheels}]
