"""066 P1 — trusted lazy installs: hashed closures, a seedless installer, a
read-only overlay. The refusals are the security model, so each one is a test:

* a hash mismatch refuses (REAL pip, offline, against a local wheel);
* an sdist refuses on a wheel-only (prod) box (real pip, and the ``wheel: false``
  closure that never reaches pip);
* a bad request name refuses — client, installer and spool;
* the agent cannot write the overlay — the installer refuses the wrong identity
  or a secret in its env, and the client refuses an overlay it could write;
* zai-coding's client initialises once the closure is installed (installer mocked).
"""
import builtins
import hashlib
import io
import os
import stat
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest

import core.lazy_deps as ld
import core.lazy_installer as li
from core.lazy_closures import Closure, closure_path, parse_closure
from core.lazy_deps import FeatureUnavailable

FAKE = "test.fakepkg"


def _wheel(dirpath: Path, name="fakepkg", version="1.0") -> Path:
    whl = dirpath / f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": "VALUE = 42\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(f"{p},,\n" for p in files) + f"{info}/RECORD,,\n"
    with zipfile.ZipFile(whl, "w") as zf:
        for p, body in files.items():
            zf.writestr(p, body)
        zf.writestr(f"{info}/RECORD", record)
    return whl


def _sdist(dirpath: Path, name="fakepkg", version="1.0") -> Path:
    path = dirpath / f"{name}-{version}.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        body = f"from setuptools import setup\nsetup(name={name!r}, version={version!r})\n".encode()
        ti = tarfile.TarInfo(f"{name}-{version}/setup.py")
        ti.size = len(body)
        tf.addfile(ti, io.BytesIO(body))
    return path


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _closure(digest_hash: str, *, wheel=True) -> Closure:
    text = (f"# feature: {FAKE}\n# lock-digest: 0123456789abcdef\n# wheel: {'true' if wheel else 'false'}\n"
            f"fakepkg==1.0 \\\n    --hash=sha256:{digest_hash}\n")
    return parse_closure(FAKE, text)


@pytest.fixture
def fake_feature(monkeypatch):
    monkeypatch.setitem(ld.LAZY_DEPS, FAKE, ("fakepkg",))
    monkeypatch.setitem(ld._FEATURE_EXTRA, FAKE, "fake")
    holder = {}
    import core.lazy_closures as lc
    monkeypatch.setattr(lc, "read_closure", lambda f: holder.get(f))
    return holder


def _offline(links: Path):
    return ["--no-index", "--find-links", str(links)]


# --- hashes and wheels (real pip, offline) ----------------------------------- #

def test_hash_mismatch_is_refused_and_nothing_lands(tmp_path, fake_feature):
    links = tmp_path / "links"
    links.mkdir()
    _wheel(links)
    fake_feature[FAKE] = _closure("0" * 64)                 # not the wheel's hash
    root = tmp_path / "pylibs"
    with pytest.raises(li.InstallRefused) as exc:
        li.install(FAKE, root=root, wheel_only=True, index_args=_offline(links), record_used=False)
    assert exc.value.kind == "hash_mismatch"
    dest = li.feature_dir(root, "0123456789abcdef", FAKE)
    assert not dest.exists()
    assert not [p for p in dest.parent.iterdir() if p.name.startswith(".tmp-")]


def test_the_right_hash_installs_sealed_and_complete(tmp_path, fake_feature):
    links = tmp_path / "links"
    links.mkdir()
    whl = _wheel(links)
    fake_feature[FAKE] = _closure(_sha(whl))
    root = tmp_path / "pylibs"
    dest = li.install(FAKE, root=root, wheel_only=True, index_args=_offline(links))
    assert li.is_complete(dest)
    assert (dest / "lib" / "fakepkg" / "__init__.py").is_file()
    for p in [dest, dest / "lib", dest / "lib" / "fakepkg" / "__init__.py"]:
        assert not stat.S_IMODE(os.stat(p).st_mode) & (stat.S_IWGRP | stat.S_IWOTH), p
    assert li.read_used(root) == [FAKE]
    # idempotent: a second call never reaches pip
    assert li.install(FAKE, root=root, wheel_only=True, run=lambda *a, **k: pytest.fail("pip")) == dest


def test_an_sdist_is_refused_on_a_wheel_only_box(tmp_path, fake_feature):
    links = tmp_path / "links"
    links.mkdir()
    sd = _sdist(links)
    fake_feature[FAKE] = _closure(_sha(sd))
    with pytest.raises(li.InstallRefused) as exc:
        li.install(FAKE, root=tmp_path / "pylibs", wheel_only=True, index_args=_offline(links))
    assert exc.value.kind == "sdist_refused"


def test_a_wheel_false_closure_is_refused_before_pip(tmp_path, fake_feature):
    fake_feature[FAKE] = _closure("0" * 64, wheel=False)
    with pytest.raises(li.InstallRefused) as exc:
        li.install(FAKE, root=tmp_path, wheel_only=True, run=lambda *a, **k: pytest.fail("pip"))
    assert exc.value.kind == "sdist_refused" and "PROD_EXTRAS" in str(exc.value)


def test_the_pip_command_is_isolated_hashed_no_deps_and_wheel_only(tmp_path, fake_feature):
    fake_feature[FAKE] = _closure("a" * 64)
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw.get("env", {})))
        import subprocess
        if "pip" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES")
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    with pytest.raises(li.InstallRefused):
        li.install(FAKE, root=tmp_path, wheel_only=True, run=run)
    cmd, env = next((c, e) for c, e in seen if "pip" in c)
    for flag in ("--isolated", "--require-hashes", "--no-deps", "--only-binary=:all:"):
        assert flag in cmd, flag
    assert cmd[cmd.index("--index-url") + 1] == "https://pypi.org/simple"
    assert not any("KEY" in k or "SEED" in k or k.startswith("PIP_") for k in env)


# --- request names ------------------------------------------------------------ #

@pytest.mark.parametrize("name", ["../../etc/passwd", "evil;rm -rf /", "provider.evil", "", "PROVIDER.ANTHROPIC"])
def test_a_bad_name_is_refused_everywhere(tmp_path, name):
    with pytest.raises(li.InstallRefused) as exc:
        li.install(name, root=tmp_path, wheel_only=True, run=lambda *a, **k: pytest.fail("pip"))
    assert exc.value.kind == "unknown_feature"
    assert closure_path(name) is None
    with pytest.raises(FeatureUnavailable) as fu:
        ld.ensure(name, prompt=False)
    assert fu.value.kind == "unknown_feature"


def test_the_spool_deletes_and_refuses_bad_requests_without_installing(tmp_path, monkeypatch):
    spool = tmp_path / "requests"
    spool.mkdir()
    for bad in ("..x", "evil;rm", "provider.evil"):
        (spool / bad).write_text("ignored content")
    (spool / "adir.x").mkdir()
    monkeypatch.setattr(li, "install", lambda *a, **k: pytest.fail("installed a bad request"))
    out = li.drain(tmp_path, wheel_only=True)
    assert set(out.values()) == {"refused:bad_request"}
    assert list(spool.iterdir()) == []


def test_the_spool_never_reads_request_content(tmp_path, monkeypatch):
    spool = tmp_path / "requests"
    spool.mkdir()
    (spool / "docs.pdf").write_text("--index-url http://evil/simple")
    got = []
    monkeypatch.setattr(li, "install", lambda name, **kw: got.append((name, kw)) or tmp_path)
    assert li.drain(tmp_path, wheel_only=True) == {"docs.pdf": "installed"}
    assert got == [("docs.pdf", {"root": tmp_path, "wheel_only": True})]
    assert not (spool / "docs.pdf").exists()


# --- the agent cannot write the overlay ------------------------------------------ #

def test_the_system_installer_refuses_any_identity_but_polyrob_deps(tmp_path, capsys):
    rc = li.main(["--root", str(tmp_path), "--system", "run", "docs.pdf"])
    assert rc == 3
    assert "agent write attempt refused" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_the_system_installer_refuses_a_secret_in_its_env(tmp_path, monkeypatch):
    monkeypatch.setattr(li, "_whoami", lambda: li.DEPS_USER)
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "abandon " * 11 + "about")
    r = li.system_refusal()
    assert r is not None and r.kind == "secret_in_env"
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED")
    assert li.system_refusal() is None


def _provision(root: Path):
    (root / "requests").mkdir(parents=True)


def test_the_client_refuses_an_overlay_it_could_write(monkeypatch):
    root = ld.system_root()
    _provision(root)
    monkeypatch.setattr(ld, "_dist_present", lambda n: False)
    with pytest.raises(FeatureUnavailable) as exc:
        ld.ensure("docs.pdf", prompt=False, wait=0.2)
    assert exc.value.kind == "overlay_writable"
    assert "agent write attempt refused" in str(exc.value)
    assert list((root / "requests").iterdir()) == []      # nothing requested either


@pytest.mark.skipif(os.geteuid() == 0 if hasattr(os, "geteuid") else True, reason="root ignores modes")
def test_a_read_only_overlay_gets_a_request_and_a_bounded_wait(monkeypatch):
    root = ld.system_root()
    _provision(root)
    os.chmod(root, 0o555)
    try:
        monkeypatch.setattr(ld, "_dist_present", lambda n: False)
        t0 = time.monotonic()
        with pytest.raises(FeatureUnavailable) as exc:
            ld.ensure("docs.pdf", prompt=False, wait=0.3)
        assert exc.value.kind == "timeout" and time.monotonic() - t0 < 5
        assert [p.name for p in (root / "requests").iterdir()] == ["docs.pdf"]
        assert (root / "requests" / "docs.pdf").read_bytes() == b""   # the name, nothing else
    finally:
        os.chmod(root, 0o755)


def test_a_recorded_installer_refusal_is_named_to_the_agent(monkeypatch):
    root = ld.system_root()
    _provision(root)
    monkeypatch.setattr(ld, "_overlay_writable", lambda: None)
    monkeypatch.setattr(ld, "_dist_present", lambda n: False)

    def installer():
        req = root / "requests" / "docs.pdf"
        for _ in range(200):
            if req.exists():
                break
            time.sleep(0.01)
        li._record_failure(root, "docs.pdf", li.InstallRefused("hash_mismatch", "sha256 differs"))

    th = threading.Thread(target=installer)
    th.start()
    with pytest.raises(FeatureUnavailable) as exc:
        ld.ensure("docs.pdf", prompt=False, wait=5)
    th.join()
    assert exc.value.kind == "hash_mismatch" and "Remedy:" in str(exc.value)


def test_a_server_under_custody_without_the_installer_refuses(monkeypatch):
    monkeypatch.setattr(ld, "_dist_present", lambda n: False)
    monkeypatch.setattr(ld, "_custody", lambda: True)
    monkeypatch.setattr(ld, "_local_mode", lambda: False)
    with pytest.raises(FeatureUnavailable) as exc:
        ld.ensure("docs.pdf", prompt=False)
    assert exc.value.kind == "no_installer" and "install-deps-installer.sh" in str(exc.value)


def test_local_custody_installs_wheel_only_and_elsewhere_allows_a_source_build(monkeypatch):
    monkeypatch.setattr(ld, "_dist_present", lambda n: False)
    monkeypatch.setattr(ld, "_local_mode", lambda: True)
    calls = []

    def fake(feature, *, wheel_only):
        calls.append(wheel_only)
        raise FeatureUnavailable("stop", "stop")

    monkeypatch.setattr(ld, "_trusted_install", fake)
    monkeypatch.setattr(ld, "_custody", lambda: True)
    import core.security.process_hardening as ph
    monkeypatch.setattr(ph, "custody_reads_closed", lambda: True)
    with pytest.raises(FeatureUnavailable):
        ld.ensure("docs.pdf", prompt=False)
    monkeypatch.setattr(ld, "_custody", lambda: False)
    with pytest.raises(FeatureUnavailable):
        ld.ensure("docs.pdf", prompt=False)
    assert calls == [True, False]


# --- zai-coding boots once the closure is installed --------------------------- #

def test_zai_coding_client_initialises_once_the_closure_is_installed(monkeypatch):
    """066 D1: prod keeps zai-coding. The `anthropic` SDK is NOT in the venv; the
    agent requests `provider.anthropic`, the installer (mocked here) finishes the
    overlay, and the client builds."""
    root = ld.system_root()
    _provision(root)
    digest = ld._lock_digest()
    state = {"installed": False}
    blocked = ("anthropic",)
    for mod in list(sys.modules):
        if mod.startswith(blocked) or mod in ("modules.llm.anthropic_client", "modules.llm.compat_anthropic"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith(blocked) and not state["installed"]:
            raise ImportError(f"No module named '{name}' (not installed yet)")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(ld, "_dist_present", lambda n: n != "anthropic" or state["installed"])
    monkeypatch.setattr(ld, "_overlay_writable", lambda: None)
    # The installer double runs as the test UID, not polyrob-deps. Ownership
    # validation has independent positive/negative security regressions.
    monkeypatch.setattr(ld, "_protected_overlay", lambda feature: True)
    monkeypatch.setenv("ZAI_API_KEY", "zai-test-key")
    requested = []

    def installer():                      # stands in for polyrob-deps@spool.service
        req = root / "requests" / "provider.anthropic"
        for _ in range(500):
            if req.exists():
                break
            time.sleep(0.01)
        requested.append(req.exists())
        req.unlink()
        dest = li.feature_dir(root, digest, "provider.anthropic")
        (dest / "lib").mkdir(parents=True)
        (dest / li.COMPLETE).write_text("{}")
        state["installed"] = True

    th = threading.Thread(target=installer)
    th.start()
    from core.config import BotConfig
    from modules.llm.llm_client_registry import create_llm_client
    client = create_llm_client("zai-coding", BotConfig())
    th.join()
    assert requested == [True]
    assert type(client).__name__ == "AnthropicCompatClient"
    assert str(li.feature_dir(root, digest, "provider.anthropic") / "lib") in sys.path
    assert ld.overlay_status()["installed"] == ["provider.anthropic"]
    sys.path.remove(str(li.feature_dir(root, digest, "provider.anthropic") / "lib"))
