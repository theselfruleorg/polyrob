"""core.box_parity — a box names each provisioning step its config needs and lacks."""
from core.box_parity import BoxProbe, parity_lines


def _probe(units=(), paths=(), mounts=(), tmp=("ext4", 40000), **env):
    return BoxProbe(unit_exists=lambda n: n in units, path_exists=lambda p: p in paths,
                    is_mountpoint=lambda p: p in mounts, tmp_fs=lambda: tmp, env=env)


def test_not_a_server_says_nothing():
    assert parity_lines(_probe()) == []


def test_a_box_without_the_trusted_installer_says_so():
    lines = parity_lines(_probe(units=("polyrob.service",)))
    assert any("trusted lazy installer MISSING" in line for line in lines)


def test_a_provisioned_box_is_ok():
    lines = parity_lines(_probe(
        units=("polyrob.service", "polyrob-deps.path", "polyrob-signer.service"),
        paths=("/var/lib/polyrob-deps/requests", "/run/polyrob-sandbox/docker.sock"),
        mounts=("/var/lib/polyrob-sandbox-view/sessions", "/var/lib/polyrob-sandbox-view/sandbox_installs"),
        WALLET_SIGNER="remote", CODE_EXEC_ENABLED="true",
        DOCKER_HOST="unix:///run/polyrob-sandbox/docker.sock"))
    assert lines and all(" ok" in line for line in lines), lines


def test_missing_sandbox_view_signer_and_apps_are_named():
    lines = "\n".join(parity_lines(_probe(
        units=("polyrob.service",), WALLET_SIGNER="remote", CODE_EXEC_ENABLED="true",
        DOCKER_HOST="unix:///run/polyrob-sandbox/docker.sock", AGENT_BUILDER_MODE="ship")))
    assert "signer MISSING" in lines
    assert "code-exec sandbox MISSING the daemon socket, the workspace view, the install view" in lines
    assert "apps supervisor MISSING" in lines


def test_a_small_tmpfs_and_legacy_mode_warn():
    lines = "\n".join(parity_lines(_probe(units=("polyrob.service",), tmp=("tmpfs", 955),
                                          LAZY_DEPS_MODE="legacy")))
    assert "955 MB tmpfs" in lines
    assert "legacy" in lines


def test_only_a_whole_path_parent_holds_a_path():
    from core.box_parity import _holds
    p = "/var/x"
    assert _holds("/", p) and _holds("/var", p) and _holds("/var/", p) and _holds("/var/x", p)
    assert not _holds("/v", p) and not _holds("/va", p) and not _holds("/var/xy", p)


def test_remedies_name_the_guide_not_a_private_file():
    lines = "\n".join(parity_lines(_probe(
        units=("polyrob.service",), WALLET_SIGNER="remote", CODE_EXEC_ENABLED="true",
        DOCKER_HOST="unix:///run/polyrob-sandbox/docker.sock")))
    assert ".sh" not in lines  # no operator installer: a public install has none
    assert "docs/guide/self-hosting.md" in lines
