"""P0.5 (proposal 018): a pref key can never ship DEAD again.

The 2026-07-17 config review found 4 keys that were settable, persisted and
displayed while their enforcement sites read the env flag directly — the
write-only trap. This ratchet makes that structurally impossible: every key
marked ``enforced`` in PREF_SCHEMA must have at least one literal
``resolve("<key>"...)`` / ``resolve_with_source("<key>"...)`` consumer in
production code outside ``core/prefs.py``. Display surfaces iterate the schema
with a key VARIABLE (``display_effective(key)``), so they never satisfy the
pattern — a match is a genuine enforcement-site read.

Adding a new key? Either wire a consumer (the ``effective_*`` house pattern)
or mark it ``enforcement=ENFORCEMENT_ADVISORY`` — silence is not an option.
"""
import re
from pathlib import Path

from core.prefs import ENFORCEMENT_ENFORCED, PREF_SCHEMA

_REPO = Path(__file__).resolve().parents[3]
_SKIP_PARTS = {"tests", ".git", "node_modules", ".venv", "venv", "__pycache__",
               "deployment", "docs", "scripts"}


def _production_sources() -> list[tuple[Path, str]]:
    out = []
    for path in _REPO.rglob("*.py"):
        rel = path.relative_to(_REPO)
        if _SKIP_PARTS.intersection(rel.parts):
            continue
        if rel == Path("core/prefs.py"):
            continue
        try:
            out.append((rel, path.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            continue
    return out


def test_every_enforced_pref_key_has_a_literal_consumer():
    sources = _production_sources()
    assert sources, "repo scan came up empty — ratchet is broken"
    missing = {}
    for key, spec in PREF_SCHEMA.items():
        if spec.enforcement != ENFORCEMENT_ENFORCED:
            continue
        # A literal read via resolve()/resolve_with_source(), or via the shared
        # env-AND-pref merge every autonomy loop's effective_* reader goes
        # through (core.prefs.effective_autonomy_switch, itself a resolve()).
        pat = re.compile(
            r"(?:resolve(?:_with_source)?|effective_autonomy_switch)\(\s*[\"']"
            + re.escape(key) + r"[\"']")
        hits = [str(rel) for rel, text in sources if pat.search(text)]
        if not hits:
            missing[key] = "no resolve('<key>') consumer found"
    assert not missing, (
        "enforced pref keys with NO enforcement-site consumer (wire one via the "
        f"effective_* pattern, or mark the spec advisory): {missing}")


# ---------------------------------------------------------------------------
# 044 T17: the same promise for the per-CHAT overlay (`chat.*`).
#
# Those rows are NOT in PREF_SCHEMA (they live in a per-chat file, not in
# preferences.toml), so the ratchet above cannot see them — and a `chat.*` key
# with no reader would be exactly the write-only trap this module exists to
# forbid, only in a new namespace. Their enforcement site is an attribute read
# on a `ChatPolicy`, not `resolve("<key>")`, so the proof is: some production
# module that knows about chat_policy reads that field OFF A POLICY OBJECT.
#
# The match is deliberately anchored to a policy-shaped receiver
# (`policy.<field>` / `pol.<field>` / `getattr(policy, "<field>")`) rather than
# a bare `.<field>` or a quoted `"<field>"`: field names like `mode`, `name` and
# `language` are common words, and a loose match would let an unwired key pass
# on an unrelated coincidence — which is exactly the silence this forbids.
# `chat_policy.py` itself stays eligible, because `mode`, `mute_until` and
# `quiet_hours` are consumed inside `mode_allows_trigger` and nowhere else.
# ---------------------------------------------------------------------------

_CHAT_POLICY_MODULE = "core/surfaces/chat_policy.py"

#: Local names a ChatPolicy is bound to across the tree.
_POLICY_RECEIVERS = ("policy", "pol", "_pol", "room", "_room_policy")


def _chat_policy_readers() -> list[tuple[Path, str]]:
    """Production sources that know about the chat overlay — the only places a
    `chat.*` field can legitimately be consumed."""
    return [(rel, text) for rel, text in _production_sources()
            if "chat_policy" in text or str(rel) == _CHAT_POLICY_MODULE]


def _field_read_pattern(field: str) -> "re.Pattern":
    recv = "|".join(re.escape(r) for r in _POLICY_RECEIVERS)
    f = re.escape(field)
    return re.compile(
        rf"(?:{recv})\.{f}\b"                               # policy.<field>
        rf"|getattr\(\s*(?:{recv})\s*,\s*[\"']{f}[\"']"      # getattr(policy, "<field>")
    )


def test_every_chat_overlay_key_is_actually_read():
    from core.prefs import CHAT_PREF_SCHEMA

    readers = _chat_policy_readers()
    assert readers, "no module imports chat_policy — the overlay is dead"
    missing = {}
    for key in CHAT_PREF_SCHEMA:
        field = key.split(".", 1)[1]
        pat = _field_read_pattern(field)
        hits = [str(rel) for rel, text in readers if pat.search(text)]
        if not hits:
            missing[key] = "no <policy>.<field> read found"
    assert not missing, (
        "chat.* keys nothing reads (wire a consumer, or do not ship the row until "
        f"the task that consumes it does): {missing}")


def test_the_chat_ratchet_would_catch_an_unwired_key():
    """The ratchet's own proof: a field no policy object is read for must FAIL
    the pattern, or the check above is decoration."""
    readers = _chat_policy_readers()
    # 046 wired `member_verbs`: the dispatcher reads it to decide whether a
    # plain member's slash line is a COMMAND. It is no longer proof of an
    # unwired key, so it left this list — leaving it here would make the
    # ratchet assert a lie.
    for never_wired in ("reply_mode", "tool_deny", "thread_scope"):
        pat = _field_read_pattern(never_wired)
        assert not [str(rel) for rel, text in readers if pat.search(text)], never_wired


def test_the_chat_overlay_is_not_a_tenant_preference():
    """A `chat.*` key must never be listed or stored as a tenant preference:
    `/config`, `polyrob config` and the webview panel all iterate PREF_SCHEMA,
    and `write_preference` would put the value in `preferences.toml`, where the
    chat policy never looks."""
    from core.prefs import CHAT_PREF_SCHEMA, PREF_SCHEMA, write_preference

    assert not (set(CHAT_PREF_SCHEMA) & set(PREF_SCHEMA))
    ok, err = write_preference("/tmp/does-not-matter", "rob", "chat.mode", "active")
    assert not ok and "per-room" in err
