"""D1: ONE definition of "this surface's sender address is forgeable".

The rule had two copies guarding two different halves of the same invariant:

* `dispatcher._FORGEABLE_NETWORK_SURFACES` refused the legacy obey-path when
  the correspondent tier model is OFF;
* the tier model, when ON, ran `_is_owner_or_paired` with NO surface filter on
  its PAIRING branch — and a pairing row is keyed on the same forgeable
  address. So the fix for "an email sender can never be the owner" held only in
  the configuration where the tier model was disabled.

Owner-by-email is off in v1 precisely because a `From:` header is spoofable.
A pairing row proves nothing about a sender whose address anyone can write.
"""
from core.surfaces.access import FORGEABLE_NETWORK_SURFACES


def test_the_dispatcher_imports_the_one_definition():
    from core.surfaces import dispatcher
    assert dispatcher._FORGEABLE_NETWORK_SURFACES is FORGEABLE_NETWORK_SURFACES


def test_email_is_forgeable():
    assert "email" in FORGEABLE_NETWORK_SURFACES


def test_a_local_surface_is_never_in_the_forgeable_set():
    """The two sets are inverses: local surfaces get the owner bypass, these
    are refused it. An overlap would be a contradiction, not a policy."""
    from core.surfaces.access import _LOCAL_OWNER_SURFACES
    assert not (set(_LOCAL_OWNER_SURFACES) & set(FORGEABLE_NETWORK_SURFACES))


def test_the_pairing_branch_is_gated_on_the_surface():
    """The behaviour, not just the constant: `allow_pairing=False` must make a
    genuinely paired uid a non-owner."""
    import os
    import tempfile

    from core.surfaces.access import _is_owner_or_paired

    workdir = tempfile.mkdtemp()

    class _Cfg:
        data_dir = workdir

    class _Container:
        config = _Cfg()

        def get_service(self, name):
            return None

    from core.pairing import PairingStore
    store = PairingStore(os.path.join(workdir, "pairing.db"))
    code = store.request("u_paired")
    assert store.approve(code) == "u_paired"

    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    c = _Container()
    assert _is_owner_or_paired(c, "u_paired", env, allow_local=False,
                               allow_pairing=True) is True
    assert _is_owner_or_paired(c, "u_paired", env, allow_local=False,
                               allow_pairing=False) is False


def test_the_owner_principal_is_unaffected_by_the_pairing_gate():
    """The gate narrows the PAIRING evidence only. A bound owner principal is
    still the owner, on any surface."""
    from core.surfaces.access import _is_owner_or_paired

    class _Container:
        config = None

        def get_service(self, name):
            return None

    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    assert _is_owner_or_paired(_Container(), "u_owner", env, allow_local=False,
                               allow_pairing=False) is True
