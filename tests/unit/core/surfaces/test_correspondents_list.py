"""WS-D: enumerate correspondents (for the `polyrob owner` quick-access CLI)."""
import os
import tempfile

import pytest

from core.surfaces.correspondents import CorrespondentRegistry


@pytest.fixture()
def reg():
    yield CorrespondentRegistry(os.path.join(tempfile.mkdtemp(), "corr.db"))


def test_list_all_and_tenant_scoped(reg):
    reg.seed(surface="email", address="a@x.com", session_id="s1", user_id="u_owner",
             thread_id="t1", provenance="owner", require_approval=True)
    reg.seed(surface="email", address="b@x.com", session_id="s2", user_id="u_other",
             thread_id="t2", provenance="owner", require_approval=False)
    assert len(reg.list()) == 2
    mine = reg.list(user_id="u_owner")
    assert len(mine) == 1
    assert mine[0]["address"] == "a@x.com"
    assert mine[0]["state"] == "pending"


def test_list_empty(reg):
    assert reg.list() == []
