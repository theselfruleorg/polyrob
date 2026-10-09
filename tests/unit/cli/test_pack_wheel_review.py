"""SUP-3: a third-party pack wheel may add only its own package to the shared venv."""
import pytest

from cli.commands.pack import wheel_refusal

OK = ["acme_pack/__init__.py", "acme_pack/pack.toml",
      "acme_pack-1.0.dist-info/METADATA", "acme_pack-1.0.dist-info/RECORD"]


def test_a_plain_pack_wheel_passes():
    assert wheel_refusal(OK, "acme_pack", "acme-pack") is None


@pytest.mark.parametrize("extra", [
    "evil.pth",                                  # runs at every interpreter start
    "__editable___acme_finder.py",
    "core/__init__.py",                          # shadows polyrob's own package
    "eth_account/__init__.py",                   # replaces a dependency
    "acme_pack-1.0.data/scripts/polyrob",        # .data scheme
    "other_dist-2.0.dist-info/METADATA",         # foreign metadata
    "acme_pack/hook.pth",
])
def test_anything_outside_the_pack_package_is_refused(extra):
    assert wheel_refusal(OK + [extra], "acme_pack", "acme-pack")


def test_install_path_builds_and_reviews_before_pip(monkeypatch):
    import inspect
    from cli.commands import pack

    src = inspect.getsource(pack._install_third_party)
    assert "_install_reviewed_wheel(" in src
    assert '_pip(["--no-deps", str(root)])' not in src
