"""B28 — _replace_sensitive_data must not coerce non-string field types.

It model_dump()'d EVERY field back into params.__dict__, so a nested BaseModel
field became a plain dict (and datetimes→str etc.) even when no secret was present.
Only fields where a secret was actually substituted should be written back.
"""
from pydantic import BaseModel

from tools.controller.registry.service import Registry


class _Nested(BaseModel):
    kind: str = "cfg"
    n: int = 3


class _Params(BaseModel):
    name: str = "x"
    count: int = 5
    nested: _Nested = _Nested()
    secretful: str = "token=<secret>API_KEY</secret>"


def test_non_secret_fields_keep_their_types():
    reg = Registry()
    p = _Params()
    out = reg._replace_sensitive_data(p, {"API_KEY": "sk-live-123"})
    # The nested model must stay a model, not be coerced to a dict.
    assert isinstance(out.nested, _Nested)
    assert out.count == 5 and isinstance(out.count, int)
    # The secret-bearing field IS substituted.
    assert out.secretful == "token=sk-live-123"


def test_no_secrets_leaves_everything_untouched():
    reg = Registry()
    p = _Params(secretful="no secrets here")
    out = reg._replace_sensitive_data(p, {"API_KEY": "sk-live-123"})
    assert isinstance(out.nested, _Nested)
    assert out.secretful == "no secrets here"
