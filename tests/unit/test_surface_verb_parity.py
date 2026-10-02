"""064 S2-01 (E-S2-01) — owner-verb parity columns, one per catalog surface.

``tests/unit/test_surface_parity.py`` pins capabilities for cli / repl /
webview / telegram. This file adds a COLUMN PER CHAT SURFACE in the catalog
(``core/surfaces/catalog.py::SURFACES``) and the ratchet the stream burns down:
every owner verb in ``core/verbs.py`` either REACHES the surface or is a
documented ``None`` here, with a reason.

What "reaches" means, mechanically (the shared inbound contract, no per-surface
verb code — "reach, never policy"):

* the surface can carry an OWNER inbound: an owner seat whose sender the
  platform authenticates (``owner_seat`` and not ``forgeable``);
* the verb routes as a COMMAND on every surface: it is in
  ``core.surfaces.dispatcher._COMMANDS`` (the shared executor then owner-gates
  it by principal, the same on every surface);
* the verb is not seat-local (``Verb.seats`` names another seat).

A new catalog row without a column fails; a documented ``None`` that starts
reaching fails (delete the debt); a verb that stops reaching on a surface
without a ``None`` row fails. Seed = today's truth (2026-09-23).
"""
import pytest

from core.surfaces import catalog
from core.surfaces.dispatcher import command_names
from core.verbs import VERB_TABLE, verb_for

#: surface id → {verb name | "*": why it does not reach}. EVERY catalog row has
#: a column here — an empty dict is "every owner verb reaches".
SURFACE_VERB_NONE = {
    "telegram": {
        "/gates": "seat-local: a REPL pane (Verb.seats = repl)",
        "/meter": "seat-local: a REPL pane (Verb.seats = repl)",
        "/workers": "seat-local: Stop/Steer act on the REPL's live session (Verb.seats = repl)",
        "/memory": "seat-local: recall and scope inspection use the REPL memory handler (Verb.seats = repl)",
    },
    "email": {
        "*": "forgeable sender (email-class): an inbound is never OWNER, so no "
             "owner verb can reach — correspondent-only by design",
    },
}
#: Chat surfaces where the only gaps are the seat-local REPL verbs.
for _sid in ("slack", "discord", "signal", "whatsapp", "x", "feishu", "dingtalk"):
    SURFACE_VERB_NONE[_sid] = dict(SURFACE_VERB_NONE["telegram"])

OWNER_VERBS = tuple(v.name for v in VERB_TABLE)


def _reaches(spec, verb_name: str) -> bool:
    verb = verb_for(verb_name)
    if verb is None or verb.seats:
        return False                           # seat-local rows are not routed
    if not spec.owner_seat or spec.forgeable:
        return False
    return verb_name in command_names()        # 067 P5a: incl. contributed verbs


def _documented_none(sid: str, verb_name: str) -> bool:
    col = SURFACE_VERB_NONE.get(sid, {})
    return "*" in col or verb_name in col


def test_every_catalog_surface_has_a_column():
    ids = set(catalog.surface_ids())
    assert ids - set(SURFACE_VERB_NONE) == set(), (
        "a catalog surface with no parity column — add one (empty dict = every "
        "owner verb reaches; else a documented None per verb)")
    assert set(SURFACE_VERB_NONE) - ids == set(), "a column for a surface not in the catalog"


@pytest.mark.parametrize("sid", sorted(catalog.surface_ids()))
def test_every_owner_verb_reaches_or_is_a_documented_none(sid):
    spec = catalog.get(sid)
    gaps = sorted(v for v in OWNER_VERBS
                  if not _reaches(spec, v) and not _documented_none(sid, v))
    assert not gaps, f"{sid}: owner verbs that do not reach and have no documented None: {gaps}"


@pytest.mark.parametrize("sid", sorted(catalog.surface_ids()))
def test_a_documented_none_never_outlives_its_debt(sid):
    spec = catalog.get(sid)
    col = SURFACE_VERB_NONE.get(sid, {})
    if "*" in col:
        assert not any(_reaches(spec, v) for v in OWNER_VERBS), (
            f"{sid}: documented '*' but some owner verb now reaches — narrow the None")
        return
    stale = sorted(v for v in col if _reaches(spec, v))
    assert not stale, f"{sid}: these verbs reach now — delete their None rows: {stale}"
    unknown = sorted(v for v in col if verb_for(v) is None)
    assert not unknown, f"{sid}: None rows for verbs that are not in core/verbs.py: {unknown}"


def test_every_reason_is_one_non_empty_line():
    for sid, col in SURFACE_VERB_NONE.items():
        for verb, why in col.items():
            assert why.strip() and "\n" not in why, (sid, verb)


def test_the_telegram_column_agrees_with_the_capability_matrix():
    """The capability matrix's telegram column and this one read the same
    dispatcher table: a verb it names routes here too."""
    from tests.unit.test_surface_parity import CAPABILITY_MATRIX
    tg = catalog.get("telegram")
    for _cap, (_cli, _slash, _web, tg_verb) in CAPABILITY_MATRIX.items():
        if tg_verb is not None and verb_for(tg_verb) is not None:
            assert _reaches(tg, tg_verb), tg_verb
