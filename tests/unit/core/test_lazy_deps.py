"""058 WS-7 — ``core/lazy_deps.py``: an optional extra installs itself on first use.

The tests ARE the security model. Anything that reaches pip must have passed
the allowlist, the per-spec safety re-check, the consent gate and the lock
constraint; a server (LAZY_DEPS_MODE off) never reaches pip at all and is
told the extra to declare instead. 066 P1: the pip-into-this-venv tests below run
``LAZY_DEPS_MODE=legacy``; the trusted path is tests/unit/core/test_066_p1_trusted_lazy.py.
"""
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import core.lazy_deps as ld
from core.lazy_deps import (
    LAZY_DEPS,
    FeatureUnavailable,
    ensure,
    is_available,
    lazy_installs_enabled,
    remedy,
)

_REPO = Path(__file__).resolve().parents[3]


def _ok(cmd, **_kw):
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def local_on(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("LAZY_DEPS_ENABLED", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)


@pytest.fixture
def missing(monkeypatch):
    """Every feature reads as absent, so ensure() must reach the installer."""
    monkeypatch.setattr(ld, "_dist_present", lambda name: False)


# --- the gate ----------------------------------------------------------------- #

def test_mode_defaults_to_trusted_and_the_old_boolean_still_maps(monkeypatch):
    """066 P1: ``LAZY_DEPS_MODE`` (default ``trusted``) replaces the 058 boolean.
    ``LAZY_DEPS_ENABLED`` still maps (true -> trusted, false -> off); the new
    flag wins; an unknown value fails closed to ``off``."""
    for name in ("LAZY_DEPS_ENABLED", "LAZY_DEPS_MODE", "POLYROB_LOCAL", "ROB_LOCAL"):
        monkeypatch.delenv(name, raising=False)
    assert ld.lazy_mode() == "trusted" and lazy_installs_enabled() is True
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "off")
    assert ld.lazy_mode() == "off" and lazy_installs_enabled() is False
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "true")
    assert ld.lazy_mode() == "trusted"
    monkeypatch.setenv("LAZY_DEPS_MODE", "legacy")   # the new flag wins
    assert ld.lazy_mode() == "legacy"
    monkeypatch.setenv("LAZY_DEPS_MODE", "yolo")
    assert ld.lazy_mode() == "off"


@pytest.fixture
def legacy(monkeypatch):
    """The 058 pip-into-this-venv path (``LAZY_DEPS_MODE=legacy``)."""
    monkeypatch.setenv("LAZY_DEPS_MODE", "legacy")


def test_disabled_refuses_with_the_extra_named(monkeypatch, missing):
    """The refusal a SERVER sees. It must name the extra, because on a server the
    remedy is editing PROD_EXTRAS, not running pip."""
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "false")
    calls = []
    monkeypatch.setattr(ld, "_run_installer", lambda cmd, **kw: calls.append(cmd) or _ok(cmd))
    with pytest.raises(FeatureUnavailable) as exc:
        ensure("provider.gemini", prompt=False)
    assert "polyrob[gemini]" in str(exc.value) and "PROD_EXTRAS" in str(exc.value)
    assert calls == []  # never reached pip


# --- the allowlist -------------------------------------------------------------- #

def test_feature_not_in_allowlist_refuses(local_on):
    with pytest.raises(FeatureUnavailable, match="not in the allowlist"):
        ensure("provider.evil", prompt=False)


def test_unsafe_spec_refuses_even_if_allowlisted(monkeypatch, local_on, missing):
    """Belt and braces: the allowlist is the gate, but a bad row in it must not
    reach pip. A URL/path/VCS spec is refused by _spec_is_safe."""
    calls = []
    monkeypatch.setattr(ld, "_run_installer", lambda cmd, **kw: calls.append(cmd) or _ok(cmd))
    monkeypatch.setitem(LAZY_DEPS, "x.y", ("https://evil/pkg.whl",))
    monkeypatch.setitem(ld._FEATURE_EXTRA, "x.y", "x")
    with pytest.raises(FeatureUnavailable, match="unsafe spec"):
        ensure("x.y", prompt=False)
    assert calls == []


@pytest.mark.parametrize("spec", [
    "https://evil/pkg.whl", "git+https://x/y.git", "./local/path", "/abs/path",
    "pkg @ https://x/y.whl", "-e .", "--index-url http://x", "pkg; os_name=='nt'",
    "", "pkg name", "pkg==1.0 --hash=sha256:abc",
])
def test_spec_is_safe_rejects_everything_that_is_not_a_pypi_name(spec):
    assert ld._spec_is_safe(spec) is False


@pytest.mark.parametrize("spec", ["anthropic>=0.20.0", "sqlite-vec>=0.1.6", "numpy", "Pillow==12.3.0", "apsw>=3.46,<4"])
def test_spec_is_safe_accepts_a_pinned_pypi_name(spec):
    assert ld._spec_is_safe(spec) is True


def test_every_shipped_row_is_safe():
    for feature, specs in LAZY_DEPS.items():
        for spec in specs:
            assert ld._spec_is_safe(spec), f"{feature}: {spec!r}"


# --- the install ---------------------------------------------------------------- #

def test_install_is_constrained_by_the_lock(monkeypatch, local_on, legacy, missing, tmp_path):
    """⚠️ THE guard that stops a lazy install upgrading a pinned core package out
    from under a venv the deploy already verified."""
    lock = tmp_path / "requirements.lock"
    lock.write_text("google-generativeai==0.8.6\n")
    monkeypatch.setattr(ld, "_lock_path", lambda: lock)
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = list(cmd)
        monkeypatch.setattr(ld, "_dist_present", lambda name: True)  # the install "landed"
        return _ok(cmd)

    monkeypatch.setattr(ld, "_run_installer", fake)
    ensure("provider.gemini", prompt=False)
    cmd = seen["cmd"]
    assert cmd[:4] == [sys.executable, "-m", "pip", "install"], "sys.executable -m pip, never bare pip"
    assert "--constraint" in cmd
    assert cmd[cmd.index("--constraint") + 1].endswith("requirements.lock")
    assert "google-generativeai>=0.8.0" in cmd


def test_missing_lock_still_constrains_to_installed_versions(monkeypatch, local_on, legacy, missing):
    """An installed WHEEL has no requirements.lock (MANIFEST.in ships it in the
    sdist only). Unconstrained is not acceptable; fall back to pinning what is
    installed, the reference scheme."""
    monkeypatch.setattr(ld, "_lock_path", lambda: None)
    monkeypatch.setattr(ld, "_installed_pins", lambda: ["pydantic==2.0.0", "click==8.1.0"])
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = list(cmd)
        idx = cmd.index("--constraint")
        seen["constraints"] = Path(cmd[idx + 1]).read_text()  # read BEFORE the temp file goes
        monkeypatch.setattr(ld, "_dist_present", lambda name: True)
        return _ok(cmd)

    monkeypatch.setattr(ld, "_run_installer", fake)
    ensure("provider.anthropic", prompt=False)
    assert "--constraint" in seen["cmd"]
    assert "pydantic==2.0.0" in seen["constraints"] and "click==8.1.0" in seen["constraints"]


def test_already_satisfied_is_a_no_op(monkeypatch, local_on):
    """ensure() on a present package must not shell out at all."""
    monkeypatch.setattr(ld, "_dist_present", lambda name: True)
    monkeypatch.setattr(ld, "_run_installer", lambda cmd, **kw: pytest.fail("shelled out"))
    ensure("provider.gemini", prompt=False)
    assert is_available("provider.gemini") is True


def test_a_failed_install_refuses_with_the_remedy_and_pip_output(monkeypatch, local_on, legacy, missing):
    monkeypatch.setattr(ld, "_lock_path", lambda: None)
    monkeypatch.setattr(ld, "_installed_pins", lambda: [])
    monkeypatch.setattr(ld, "_run_installer",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No matching distribution"))
    with pytest.raises(FeatureUnavailable) as exc:
        ensure("docs.pdf", prompt=False)
    msg = str(exc.value)
    assert "polyrob[docs]" in msg and "No matching distribution" in msg


def test_an_install_that_reports_success_but_leaves_nothing_refuses(monkeypatch, local_on, legacy, missing):
    """pip exit 0 is not the proof; the distribution being importable is."""
    monkeypatch.setattr(ld, "_lock_path", lambda: None)
    monkeypatch.setattr(ld, "_installed_pins", lambda: [])
    monkeypatch.setattr(ld, "_run_installer", _ok)
    with pytest.raises(FeatureUnavailable, match="still absent"):
        ensure("media.qr", prompt=False)


def test_consent_no_on_a_tty_refuses_without_installing(monkeypatch, local_on, legacy, missing):
    monkeypatch.setattr(ld, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(ld, "_ask", lambda question: False)
    monkeypatch.setattr(ld, "_run_installer", lambda cmd, **kw: pytest.fail("installed without consent"))
    with pytest.raises(FeatureUnavailable, match="declined"):
        ensure("provider.gemini", prompt=True)


def test_is_available_reads_every_spec_of_a_multi_package_feature(monkeypatch):
    present = {"apsw", "sqlite-vec"}  # numpy missing
    monkeypatch.setattr(ld, "_dist_present", lambda name: name in present)
    assert is_available("memory.vector") is False
    present.add("numpy")
    assert is_available("memory.vector") is True


def test_is_available_never_installs(monkeypatch):
    monkeypatch.setattr(ld, "_dist_present", lambda name: False)
    monkeypatch.setattr(ld, "_run_installer", lambda cmd, **kw: pytest.fail("is_available shelled out"))
    assert is_available("provider.gemini") is False
    assert is_available("not.a.feature") is False


# --- the remedy --------------------------------------------------------------- #

def test_remedy_names_the_extra_and_on_a_server_prod_extras(monkeypatch):
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "false")
    r = remedy("provider.gemini")
    assert "pip install 'polyrob[gemini]'" in r and "PROD_EXTRAS" in r
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "true")
    assert "PROD_EXTRAS" not in remedy("provider.gemini")


# --- drift with pyproject ----------------------------------------------------- #

def test_allowlist_matches_pyproject_extras():
    """⚠️ Drift a human would otherwise have to remember. Every LAZY_DEPS spec must
    appear in the extra its namespace names (or, until 058 T1.x moves it, still
    in base), and every dep of a lazy-managed extra must have a row — except the
    embedder, which the vector backend degrades without."""
    data = tomllib.loads((_REPO / "pyproject.toml").read_text())
    base = {ld._dist_name(s) for s in data["project"]["dependencies"]}
    extras = {k: {ld._dist_name(s) for s in v}
              for k, v in data["project"]["optional-dependencies"].items()}
    for feature, specs in LAZY_DEPS.items():
        extra = ld._FEATURE_EXTRA[feature]
        for spec in specs:
            name = ld._dist_name(spec)
            home = extras.get(extra, set())
            assert name in home, f"{feature}: {name} is not in extra [{extra}]"
            assert name not in base, f"{feature}: {name} is still a BASE dependency"
    covered = {ld._dist_name(s) for specs in LAZY_DEPS.values() for s in specs}
    for extra in set(ld._FEATURE_EXTRA.values()) & set(extras):
        for name in extras[extra] - ld._EXTRA_DEPS_WITHOUT_LAZY_ROW:
            assert name in covered, f"[{extra}] dep {name} has no LAZY_DEPS row"


def test_layering_core_never_imports_agents():
    src = (_REPO / "core/lazy_deps.py").read_text()
    assert "agents" not in src.replace("agents.*", "")  # doc mention only
