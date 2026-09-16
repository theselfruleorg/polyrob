"""043 W7 — a WRITABLE console must know whose console it is.

With no owner bound, `resolve_owner_user_id` falls back to the local tenant
(`"local"`, since 2026-09-15 — it was the instance id `"polyrob"` before), so
`_effective_user_id` scopes every read and write to that tenant. Nothing errors:
on a headless server whose agent is bound to some other owner, the console
renders honest-LOOKING empty lists — no goals, no invoices, no pending items —
for an agent that has plenty of all three under the real owner's id. A monitoring
(read-only) console can live with that; a console you can DRIVE cannot, because
the writes land in the wrong tenant too.

So: writable + unbound refuses at BOOT. `local_owner_id()` itself warns once
rather than raising — after the boot refusal an unbound writable console cannot
exist in a running process, so a per-call raise would only fire where the startup
hook never ran (a test harness, an embedder) and would turn a configuration
problem into a 500 on every page with none of the boot refusal's remedy text.
"""
import importlib

import pytest

_OWNER_KEYS = ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
               "SURFACE_SUPER_ADMIN_USER_IDS", "POLYROB_LOCAL_OWNER")


@pytest.fixture()
def wg(monkeypatch):
    for key in _OWNER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
    import webview.webgate as module
    importlib.reload(module)
    return module


def test_a_writable_console_with_no_owner_says_so(wg, caplog):
    """The REFUSAL is at boot (below). Per call, the honest thing is to say the
    true sentence once — a process that never ran the startup hook (a test
    harness, an embedder) must not be silently mis-scoped, and must not 500 on
    every page either.

    ⚠️ The message must NAME the tenant it resolved. It read "scoped to the
    instance id" for a day after that stopped being true, and this test could not
    catch it because it asserted only the flag name — an operator reading a
    warning that names the wrong bucket looks in the wrong place."""
    wg._warned_unbound_owner = False
    with caplog.at_level("WARNING"):
        assert wg.local_owner_id() == "local"
    assert "POLYROB_OWNER_USER_ID is not set" in caplog.text
    assert "'local'" in caplog.text, "the warning must name the tenant it resolved"
    assert "instance id" not in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING"):
        wg.local_owner_id()
    assert caplog.text == "", "the warning is once per process, not per request"


def test_owner_is_bound_is_false_when_unbound(wg):
    assert wg.owner_is_bound() is False


def test_a_read_only_console_is_not_warned(wg, monkeypatch, caplog):
    """Prod's monitoring console is unbound today and that is fine — a read
    scoped to the unbound default is a cosmetic gap, not a wrong write."""
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    wg._warned_unbound_owner = False
    with caplog.at_level("WARNING"):
        assert wg.local_owner_id() == "local"
    assert "POLYROB_OWNER_USER_ID" not in caplog.text


def test_an_explicit_owner_satisfies_it(wg, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u-owner")
    assert wg.local_owner_id() == "u-owner"


def test_the_super_admin_ladder_satisfies_it(wg, monkeypatch):
    monkeypatch.setenv("SURFACE_SUPER_ADMIN_USER_IDS", "u-admin,u-other")
    assert wg.local_owner_id() == "u-admin"


def test_the_console_local_owner_override_satisfies_it(wg, monkeypatch):
    """`POLYROB_LOCAL_OWNER` is this module's OWN owner override — it ranks
    between an explicit owner and the local tenant in `resolve_owner_user_id`.
    An operator who named the owner there HAS bound one; refusing would be a
    false alarm about a setting the console itself defines."""
    monkeypatch.setenv("POLYROB_LOCAL_OWNER", "u-local")
    assert wg.local_owner_id() == "u-local"


# --- the boot assertion ---------------------------------------------------- #

def test_assert_writable_console_refuses_when_unbound(wg):
    from webview import posture_guard
    with pytest.raises(RuntimeError) as exc:
        posture_guard.assert_writable_console(env={}, posture="own_ops")
    assert "POLYROB_OWNER_USER_ID" in str(exc.value)
    assert "WEBVIEW_READ_ONLY=true" in str(exc.value)   # the remedy is stated
    # The refusal text an operator READS must name the tenant that would be
    # used, not the one that used to be. A refusal may be incomplete; it may
    # not be confident and wrong.
    assert "'local'" in str(exc.value)
    assert "instance id" not in str(exc.value)


def test_assert_writable_console_is_quiet_when_bound(wg):
    """Both preconditions met (W7's owner, W8's shared registry) → no refusal."""
    from webview import posture_guard
    posture_guard.assert_writable_console(
        env={"POLYROB_OWNER_USER_ID": "u-owner",
             "SESSION_REGISTRY_BACKEND": "sqlite"},
        posture="own_ops")


def test_every_unmet_precondition_is_reported_at_once(wg):
    """An operator flipping the flag wants the whole list, not a game of
    whack-a-mole across three failed boots."""
    from webview import posture_guard
    with pytest.raises(RuntimeError) as exc:
        posture_guard.assert_writable_console(env={}, posture="own_ops")
    text = str(exc.value)
    assert "POLYROB_OWNER_USER_ID" in text
    assert "SESSION_REGISTRY_BACKEND=sqlite" in text


def test_assert_writable_console_is_quiet_when_read_only(wg, monkeypatch):
    from webview import posture_guard
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    posture_guard.assert_writable_console(env={}, posture="own_ops")


def test_a_local_workstation_console_is_not_refused(wg):
    """`polyrob dashboard` on a laptop: the console and the agent share one tree
    and resolve the owner through the same function, so unbound is not a
    mis-scope. Refusing here would break first run and buy nothing — and a
    `local` posture on a SERVER is already refused by assert_console_posture."""
    from webview import posture_guard
    posture_guard.assert_writable_console(env={}, posture="local")


def test_the_startup_hook_calls_it():
    """The refusal has to abort the BOOT, not wait for the first request that
    happens to resolve an owner."""
    import inspect
    import webview.server as srv
    src = inspect.getsource(srv.startup_event)
    assert "assert_writable_console" in src


def test_a_signalled_local_box_does_not_get_the_workstation_pass(wg):
    """`WEBVIEW_ALLOW_LOCAL_POSTURE=1` waves a SERVER through the S8 posture
    refusal. It must not also buy the workstation exemption from the writable
    preconditions — that box has an agent service and a separate env file, which
    is the whole reason those preconditions exist."""
    from webview import posture_guard
    env = {"WEBVIEW_PUBLIC_URL": "https://console.example.com"}
    with pytest.raises(RuntimeError) as exc:
        posture_guard.assert_writable_console(env=env, posture="local")
    assert "POLYROB_OWNER_USER_ID" in str(exc.value)

    # A plain workstation (no signal) keeps the pass.
    posture_guard.assert_writable_console(env={}, posture="local")
