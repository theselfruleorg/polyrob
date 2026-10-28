"""Flag-count ratchet (058 WS-2, 2026-09-21).

736 documented knobs on 2026-09-21, up from 408 on 2026-07-21 — +71% in two
months with nothing holding the line. This tree ratchets file size, layering,
path use, rate limiters, bool-env parsing, status silence, shipped assets, nav
count and inline scripts; the one surface an OPERATOR actually has to read had
no ratchet at all.

This does NOT forbid a new flag. It forbids a NET new flag: add one and retire
one, or raise/lower ``CEILING`` in the same commit and say why in the message.
The five named default bundles (``POLYROB_LOCAL`` / ``AUTONOMY_MODE`` /
``AUTONOMY_POSTURE`` / ``AGENT_COMPUTE_POSTURE`` / ``AGENT_BUILDER_MODE``) are
the intended operator surface — a sixth individual knob that only a bundle
should move is the thing this catches.

SHRINK-ONLY, mirroring ``tests/test_file_size_ratchet.py``, with the same two
guards: no growth, and a ceiling that may not drift above the real count (so a
retirement that forgets to lower the row stops the ratchet tightening).

⚠️ Reads ``core/flags_catalog.py`` — the checked-in literal that ships in the
WHEEL — and never ``docs/CONFIGURATION.md``, which ships only in the sdist
(``MANIFEST.in``). A test that read the doc would fail in an installed tree.
"""
from core.flags_catalog import CATALOG

# The live count. Lower it when a flag is retired; raise it only with the reason
# in the commit message. The history lives in `git log -p` of this line and in
# CHANGELOG.md — not here, so parallel flag commits do not conflict on a comment.
CEILING = 650
# A flag row is a deliberate act — unlike file line counts there is no ordinary
# churn to absorb, so the ceiling tracks the real count exactly.
SLACK = 0

# 067 F1: the PUBLIC tier (the 5th catalog field, from PUBLIC_FLAGS in
# scripts/gen_flags_catalog.py) is what `doctor --flags` shows by default — the
# operator surface. Same exact-tracking rule, slack 0: a new public flag is a
# deliberate act, so raise this in the same commit and say why.
PUBLIC_CEILING = 78  # 083: +AUTONOMOUS_MAX_STEPS; D7: -10 posture members, +POLYROB_OVERRIDES; -3 model-pin duplicates


def test_flag_count_does_not_grow():
    assert len(CATALOG) <= CEILING, (
        f"{len(CATALOG)} flags vs ceiling {CEILING}. Retire a flag, fold it into "
        "one of the five named bundles, or raise CEILING here with the reason in "
        "the commit message — 736 -> 408 took two months in the other direction."
    )


def test_ceiling_tracks_actual():
    drift = CEILING - len(CATALOG)
    assert drift <= SLACK, (
        f"CEILING {CEILING} sits {drift} above the real count {len(CATALOG)} — a "
        "retirement forgot to lower this row, so the ratchet stopped tightening."
    )


def _public_count() -> int:
    return sum(1 for row in CATALOG if row[4] == "public")


def test_public_tier_does_not_grow():
    assert _public_count() <= PUBLIC_CEILING, (
        f"{_public_count()} public flags vs PUBLIC_CEILING {PUBLIC_CEILING}. The "
        "public tier is the operator surface; demote a flag (drop it from "
        "PUBLIC_FLAGS in scripts/gen_flags_catalog.py) or raise the ceiling "
        "with the reason in the commit message."
    )


def test_public_ceiling_tracks_actual():
    assert PUBLIC_CEILING - _public_count() <= 0, (
        f"PUBLIC_CEILING {PUBLIC_CEILING} sits above the real public count "
        f"{_public_count()} — lower it."
    )


def test_every_row_has_a_known_tier():
    bad = [row[0] for row in CATALOG if len(row) < 5
           or row[4] not in ("public", "advanced", "internal")]
    assert not bad, f"catalog rows without a valid tier: {bad[:10]}"
