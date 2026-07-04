"""C4: set_auth_state is the ONE place every auth middleware writes
request.state — this locks its contract so no middleware can drift from it.
"""
import types

from api.auth_state import set_auth_state


def test_writes_all_five_canonical_fields():
    state = types.SimpleNamespace()
    set_auth_state(
        state, user_id="u1", tier="holder", role="admin",
        payment_method="x402", authenticated=True,
    )
    assert state.user_id == "u1"
    assert state.tier == "holder"
    assert state.role == "admin"
    assert state.payment_method == "x402"
    assert state.authenticated is True


def test_defaults_match_an_unauthenticated_shape():
    state = types.SimpleNamespace()
    set_auth_state(state, user_id=None)
    assert state.user_id is None
    assert state.tier == "free"
    assert state.role == "user"
    assert state.payment_method is None
    assert state.authenticated is True  # caller decides whether to call this at all
