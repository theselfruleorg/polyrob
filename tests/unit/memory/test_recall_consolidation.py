"""WS-K4 — repeated recall becomes ONE curated note.

The measured case, prod 2026-09-22: 19,001 raw recall rows, 0 curated notes,
no exact duplicates and plenty of near ones — the same lesson written down
again every few sessions in slightly different words.
"""
import os

import pytest

from modules.memory.recall_consolidation import (
    MIN_CLUSTER, SOURCE_PREFIX, cluster_rows, document_frequency, note_title,
    signature, tokens,
)
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

# The two real prod rows this work exists for.
POST_A = ("x_browser_x_post caps at 280 chars; the API rail 402s and twitter "
          "refuses the longer draft")
POST_B = ("x_browser_x_post rejects text over 280 chars; drafting a shorter "
          "variant before the call")
POST_C = ("x_browser_x_post refused a 340 char draft: the 280 limit is hard, "
          "shorten before calling")


def test_tokens_drop_glue_and_hex_blobs():
    got = tokens("The swap 0xea8b081e1d70df3688be87a664a0974a85a4625f succeeded")
    assert "swap" in got and "succeeded" in got
    assert not any(t.startswith("0x") or len(t) > 30 for t in got)
    assert "the" not in got


def test_a_row_with_nothing_distinctive_gets_no_signature():
    rows = [(i, "the run made a call and got a result") for i in range(10)]
    df = document_frequency(rows)
    assert signature(rows[0][1], df, len(rows)) is None


def test_the_same_lesson_written_three_ways_is_one_cluster():
    filler = [(i, f"unrelated note about topic{i} and matter{i} and thing{i}")
              for i in range(60)]
    rows = filler + [(900, POST_A), (901, POST_B), (902, POST_C)]

    clusters = cluster_rows(rows)

    assert len(clusters) == 1
    got = clusters[0]
    assert got.size == 3
    assert {r[0] for r in got.rows} == {900, 901, 902}
    # The representative is the SHORTEST statement of the shared fact.
    assert got.representative == min(POST_A, POST_B, POST_C, key=len)
    assert "x_browser_x_post" in got.signature


def test_two_repeats_are_not_enough():
    filler = [(i, f"unrelated note about topic{i} and matter{i} and thing{i}")
              for i in range(60)]
    clusters = cluster_rows(filler + [(900, POST_A), (901, POST_B)])
    assert clusters == []
    assert MIN_CLUSTER == 3


def test_a_common_token_can_never_anchor_a_cluster():
    """Phrasing is not a subject: a token in >5% of rows is excluded."""
    rows = [(i, f"deploy finished cleanly on attempt{i} with outcome{i} extra{i}")
            for i in range(40)]
    clusters = cluster_rows(rows)
    assert clusters == []


@pytest.fixture
def provider(tmp_path):
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


def _seed(provider, contents, user_id="rob"):
    from core.sqlite_util import execute_retry
    for i, text in enumerate(contents):
        execute_retry(provider.db_path,
                      "INSERT INTO memories (user_id, session_id, content) "
                      "VALUES (?,?,?)", (user_id, f"s{i}", text))


def _notes(provider, user_id="rob"):
    from core.sqlite_util import execute_retry
    return [dict(r) for r in execute_retry(
        provider.db_path,
        "SELECT id, content, title, source, created_by, status FROM curated_memory "
        "WHERE user_id = ?", (user_id,), fetch="all") or []]


def test_consolidation_writes_one_note_and_deletes_nothing(provider):
    filler = [f"unrelated note about topic{i} and matter{i} and thing{i}"
              for i in range(60)]
    _seed(provider, filler + [POST_A, POST_B, POST_C])

    out = provider.consolidate_recall(user_id="rob")

    assert out["clusters"] == 1 and out["written"] == 1
    notes = _notes(provider)
    assert len(notes) == 1
    assert notes[0]["created_by"] == "curator"
    assert notes[0]["status"] == "pending"
    assert notes[0]["source"].startswith(SOURCE_PREFIX)
    assert notes[0]["title"].startswith("learned 3×:")
    assert notes[0]["content"] == min(POST_A, POST_B, POST_C, key=len)
    # Non-destructive: every raw row is still there.
    assert out["rows"] == 63


def test_a_second_run_updates_rather_than_duplicating(provider):
    filler = [f"unrelated note about topic{i} and matter{i} and thing{i}"
              for i in range(60)]
    _seed(provider, filler + [POST_A, POST_B, POST_C])
    provider.consolidate_recall(user_id="rob")

    again = provider.consolidate_recall(user_id="rob")

    assert again["written"] == 0 and again["updated"] == 0
    assert len(_notes(provider)) == 1


def test_a_grown_cluster_updates_the_same_note(provider):
    filler = [f"unrelated note about topic{i} and matter{i} and thing{i}"
              for i in range(60)]
    _seed(provider, filler + [POST_A, POST_B, POST_C])
    provider.consolidate_recall(user_id="rob")
    shorter = "x_browser_x_post 280 chars max"
    _seed(provider, [shorter], user_id="rob")

    out = provider.consolidate_recall(user_id="rob")

    notes = _notes(provider)
    assert out["updated"] == 1 and len(notes) == 1
    assert notes[0]["content"] == shorter


def test_consolidation_is_tenant_scoped(provider):
    filler = [f"unrelated note about topic{i} and matter{i} and thing{i}"
              for i in range(60)]
    _seed(provider, filler + [POST_A, POST_B, POST_C], user_id="rob")
    _seed(provider, filler + [POST_A, POST_B, POST_C], user_id="someone-else")

    provider.consolidate_recall(user_id="rob")

    assert len(_notes(provider, "rob")) == 1
    assert _notes(provider, "someone-else") == []


def test_the_note_cap_bounds_a_first_run(provider):
    rows = []
    for subject in range(10):
        for variant in range(MIN_CLUSTER):
            rows.append(f"verb{subject} raised fault{subject} "
                        f"during phase{subject} attempt {variant}")
    _seed(provider, rows)

    out = provider.consolidate_recall(user_id="rob", max_notes=4)

    assert out["written"] == 4
    assert len(_notes(provider)) == 4


def test_note_title_names_the_count():
    from modules.memory.recall_consolidation import Cluster
    c = Cluster(signature="alpha beta gamma", rows=[(1, "x"), (2, "yy")])
    assert note_title(c) == "learned 2×: alpha beta gamma"


def test_the_signature_is_stable_as_a_cluster_grows():
    """⚠️ The bug this pins: a variable-length signature splits its own cluster.

    Ranking by rarity alone picked the tokens that appear exactly ONCE — the
    phrasing that differs between two statements of the same fact — and a
    fourth row carrying one extra qualifying token landed in a bucket of its
    own, silently dissolving the cluster it belonged to.
    """
    filler = [(i, f"unrelated note about topic{i} and matter{i} and thing{i}")
              for i in range(60)]
    three = filler + [(900, POST_A), (901, POST_B), (902, POST_C)]
    grown = three + [(903, "x_browser_x_post 280 chars max")]

    sig3 = cluster_rows(three)[0].signature
    clusters = cluster_rows(grown)

    assert len(clusters) == 1
    assert clusters[0].signature == sig3
    assert clusters[0].size == 4


def test_the_note_sweep_does_not_archive_what_the_recall_sweep_writes(provider):
    """⚠️ Two curator passes must not undo each other every tick.

    `consolidate_notes` archives ACTIVE agent-authored notes with
    access_count 0 past the staleness cutoff. A consolidation note is DERIVED
    and nothing bumps its access count, so without an exemption every one of
    them would be archived exactly once the cutoff passed — while the pass that
    writes them kept rewriting the same archived rows.
    """
    import time

    filler = [f"unrelated note about topic{i} and matter{i} and thing{i}"
              for i in range(60)]
    _seed(provider, filler + [POST_A, POST_B, POST_C])
    provider.consolidate_recall(user_id="rob")
    note_id = _notes(provider)[0]["id"]

    # Age it well past any cutoff, then run the note sweep.
    from core.sqlite_util import execute_retry
    execute_retry(provider.db_path,
                  "UPDATE curated_memory SET updated_ts = ? WHERE id = ?",
                  (int(time.time()) - 400 * 86400, note_id))
    out = provider.consolidate_notes(stale_before_ts=int(time.time()))

    assert out["archived_stale"] == []
    assert _notes(provider)[0]["status"] == "pending"


def test_scoped_recall_cannot_be_consolidated_into_shared_notes(provider):
    from core.sqlite_util import execute_retry
    _seed(provider, [POST_A, POST_B, POST_C])
    execute_retry(provider.db_path,
                  "INSERT INTO mem_provenance (mem_rowid,user_id,ts,scope) "
                  "SELECT rowid,user_id,0,'goal:quarantine' FROM memories")
    result = provider.consolidate_recall(user_id="rob")
    assert result["rows"] == 0
    assert _notes(provider) == []


def test_changed_consolidation_loses_prior_active_status(provider):
    from core.sqlite_util import execute_retry
    _seed(provider, [POST_A, POST_B, POST_C])
    provider.consolidate_recall(user_id="rob")
    execute_retry(provider.db_path, "UPDATE curated_memory SET status='active'")
    _seed(provider, ["x_browser_x_post 280 chars max"])
    result = provider.consolidate_recall(user_id="rob")
    assert result["updated"] == 1
    assert _notes(provider)[0]["status"] == "pending"


def test_an_archived_consolidation_note_is_updated_not_duplicated(provider):
    """An owner who archived one has decided; the next tick must not resurrect
    it, and must not write a second row for the same cluster either."""
    filler = [f"unrelated note about topic{i} and matter{i} and thing{i}"
              for i in range(60)]
    _seed(provider, filler + [POST_A, POST_B, POST_C])
    provider.consolidate_recall(user_id="rob")
    note_id = _notes(provider)[0]["id"]

    from core.sqlite_util import execute_retry
    execute_retry(provider.db_path,
                  "UPDATE curated_memory SET status = 'archived' WHERE id = ?",
                  (note_id,))
    shorter = "x_browser_x_post 280 chars max"
    _seed(provider, [shorter])

    out = provider.consolidate_recall(user_id="rob")

    notes = _notes(provider)
    assert out["written"] == 0 and out["updated"] == 1
    assert len(notes) == 1
    assert notes[0]["status"] == "archived"      # never resurrected
    assert notes[0]["content"] == shorter        # but kept current


def test_derived_notes_have_a_ceiling_of_their_own(provider):
    """⚠️ Every other writer goes through note_create, which refuses past
    MEMORY_TOOL_MAX_ENTRIES. This pass inserts directly, so it carries its own
    ceiling — otherwise a 19,001-row corpus distils into hundreds of rows in a
    store the rest of the system assumes is small."""
    from modules.memory.recall_consolidation import MAX_CONSOLIDATION_NOTES

    rows = []
    for subject in range(MAX_CONSOLIDATION_NOTES + 6):
        for variant in range(MIN_CLUSTER):
            rows.append(f"verb{subject} raised fault{subject} "
                        f"during phase{subject} attempt {variant}")
    _seed(provider, rows)

    written = 0
    for _ in range(20):                       # several ticks, 20 notes each
        out = provider.consolidate_recall(user_id="rob")
        written += out["written"]
        if out.get("at_ceiling"):
            break

    notes = _notes(provider)
    assert len(notes) == MAX_CONSOLIDATION_NOTES
    assert written == MAX_CONSOLIDATION_NOTES


def test_an_existing_note_is_still_updated_at_the_ceiling(provider):
    from modules.memory.recall_consolidation import MAX_CONSOLIDATION_NOTES
    from core.sqlite_util import execute_retry

    rows = []
    for subject in range(MAX_CONSOLIDATION_NOTES):
        for variant in range(MIN_CLUSTER):
            rows.append(f"verb{subject} raised fault{subject} "
                        f"during phase{subject} attempt {variant}")
    _seed(provider, rows)
    for _ in range(4):
        provider.consolidate_recall(user_id="rob")
    assert len(_notes(provider)) == MAX_CONSOLIDATION_NOTES

    # a shorter statement of an EXISTING cluster must still land
    _seed(provider, ["verb0 fault0 phase0"])
    out = provider.consolidate_recall(user_id="rob")

    assert out["updated"] >= 1
    assert len(_notes(provider)) == MAX_CONSOLIDATION_NOTES
