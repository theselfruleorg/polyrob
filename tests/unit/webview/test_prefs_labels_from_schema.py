"""043 §9 phase 4 — the legacy `/preferences` page (preferences.html) is deleted;
its typed-prefs panel moved to the new Agent destination's Settings tab, which
renders from ``core.prefs.PREF_SCHEMA`` over the same
``GET/PATCH /api/webgate/preferences`` endpoints (see test_preferences_page.py).

The page-render + inline-template-JS assertions (043 A11) went with the deleted
template. What survives is the schema invariant the new UI still relies on: a
blank ``PrefSpec.description`` would silently fall back to the raw key on every
row, so the schema itself must never regress to an empty description.
"""


def test_pref_schema_descriptions_are_all_non_empty():
    # A blank description would silently fall back to the raw key on every
    # row — make sure the schema itself never regresses to that.
    from core.prefs import PREF_SCHEMA
    empty = [k for k, spec in PREF_SCHEMA.items() if not spec.description.strip()]
    assert not empty, f"PrefSpec.description empty for: {empty}"
