"""T9: recall-vs-filesystem honesty — artifact-existence stamping.

`stamp_artifact_references` (pure, containment-safe, never raises) appends an
existence marker to workspace-relative path-shaped tokens so the agent (and the
planner) sees what's ACTUALLY on disk rather than trusting stale memory. Wired
into `build_goal_run_task` (goal title/body/acceptance, opt-in via
`workspace_root`) and the planner's BLOCKED-list rendering, plus a new
escalate-once instruction sentence in the planner prompt.
"""
from agents.task.goals.board import GoalBoard
from agents.task.goals.context import build_goal_run_task, stamp_artifact_references
from agents.task.goals.planner import build_planner_prompt


# ---------------------------------------------------------------------------
# (a) missing artifact
# ---------------------------------------------------------------------------

def test_stamp_missing_artifact_in_goal_task(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="Follow up on design",
                 body="Update drafts/DESIGN.md with the new section.")
    t = build_goal_run_task(g, None, workspace_root=tmp_path)
    assert "drafts/DESIGN.md [MISSING on disk]" in t


# ---------------------------------------------------------------------------
# (b) present artifact
# ---------------------------------------------------------------------------

def test_stamp_present_artifact_in_goal_task(tmp_path):
    (tmp_path / "drafts").mkdir()
    p = tmp_path / "drafts" / "DESIGN.md"
    p.write_text("# Design\nsome content here")
    size = p.stat().st_size
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="Follow up on design",
                 body="Update drafts/DESIGN.md with the new section.")
    t = build_goal_run_task(g, None, workspace_root=tmp_path)
    assert f"drafts/DESIGN.md [present, {size} bytes]" in t


def test_stamp_present_artifact_via_acceptance(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    size = p.stat().st_size
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="Update notes", body="See notes.txt.",
                 payload={"acceptance": "notes.txt reflects the change"})
    t = build_goal_run_task(g, None, workspace_root=tmp_path)
    assert f"notes.txt [present, {size} bytes]" in t


# ---------------------------------------------------------------------------
# (c) paths outside the workspace root are ignored (no traversal escape)
# ---------------------------------------------------------------------------

def test_stamp_ignores_traversal_outside_root(tmp_path):
    # tmp_path/sub/../../escape.txt normalizes to a path ABOVE tmp_path itself —
    # must be left completely unstamped, never raise, never follow the escape.
    text = "see sub/../../escape.txt for the real file"
    stamped = stamp_artifact_references(text, tmp_path)
    assert stamped == text
    assert "[MISSING on disk]" not in stamped
    assert "[present" not in stamped


def test_stamp_never_raises_on_bad_root():
    # Any error resolving the root (not a real path, wrong type, etc) must fall
    # back to returning the text unchanged rather than raising.
    text = "see drafts/DESIGN.md"
    assert stamp_artifact_references(text, None) == text
    assert stamp_artifact_references(text, object()) == text
    assert stamp_artifact_references("", "/tmp") == ""


# ---------------------------------------------------------------------------
# (T9 review) absolute paths and ".."-traversal references are left ENTIRELY
# unstamped — a leading "/" or bare ".." must not let the tail get carved out
# and stamped as if it were the whole (in-root) reference.
# ---------------------------------------------------------------------------

def test_stamp_leaves_absolute_path_reference_untouched(tmp_path):
    text = "check /etc/passwd.txt for details"
    assert stamp_artifact_references(text, tmp_path) == text


def test_stamp_leaves_traversal_reference_untouched_even_if_target_exists(tmp_path):
    # escape.txt genuinely exists in the workspace root, but the text reads as
    # a "../../" traversal reference to it — must NOT be stamped "[present...]"
    # since that would be a stamping lie (implies the traversal path resolves
    # in-root when the token, read as written, does not).
    (tmp_path / "escape.txt").write_text("secret")
    text = "the real file is at ../../escape.txt now"
    assert stamp_artifact_references(text, tmp_path) == text


# ---------------------------------------------------------------------------
# (T9 review) URL tails and version/pin-shaped tokens are path-shaped but are
# never workspace artifacts — must not be stamped "[MISSING on disk]" noise.
# ---------------------------------------------------------------------------

def test_stamp_leaves_url_tail_untouched(tmp_path):
    text = "see https://example.io/spec/a.md for the spec"
    assert stamp_artifact_references(text, tmp_path) == text


def test_stamp_leaves_version_pin_token_untouched(tmp_path):
    text = "bump the pin to node-18.2.json in config"
    assert stamp_artifact_references(text, tmp_path) == text


def test_stamp_bare_filename_stamped_present_when_it_exists(tmp_path):
    # The asymmetric rule's positive case: a bare filename-shaped token (no
    # directory component) IS stamped "[present, N bytes]" when it genuinely
    # exists in the workspace root — only the "MISSING" claim is suppressed
    # for bare tokens, not the "present" one.
    p = tmp_path / "notes.md"
    p.write_text("hello world")
    size = p.stat().st_size
    text = "see notes.md for details"
    stamped = stamp_artifact_references(text, tmp_path)
    assert stamped == f"see notes.md [present, {size} bytes] for details"


# ---------------------------------------------------------------------------
# (d) planner escalate-once instruction
# ---------------------------------------------------------------------------

def test_planner_prompt_contains_escalate_once_instruction(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    p = build_planner_prompt(b, "rob", None)
    assert "≥3 blocked/failed goals" in p
    assert "create ONE ask that names the artifact" in p
    assert "STOP queuing goals that depend on it" in p


def test_planner_stamps_blocked_goal_missing_artifact(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    b.create_objective(user_id="rob", title="Grow the substack")
    blocked = b.create(user_id="rob", title="Broken goal wholly unrelated")
    b.claim(blocked.id, "w", ttl_seconds=60)
    b.record_failure(blocked.id, error="missing file: drafts/DESIGN.md")
    b.claim(blocked.id, "w", ttl_seconds=60)
    b.record_failure(blocked.id, error="missing file: drafts/DESIGN.md")  # trips breaker
    p = build_planner_prompt(b, "rob", tmp_path)
    assert "drafts/DESIGN.md [MISSING on disk]" in p


def test_planner_no_stamping_when_root_none(tmp_path):
    # Without a deliverables_root the planner must not attempt to stamp at all
    # (no crash, plain error text as before).
    b = GoalBoard(str(tmp_path / "g.db"))
    b.create_objective(user_id="rob", title="Grow the substack")
    blocked = b.create(user_id="rob", title="Broken goal wholly unrelated")
    b.claim(blocked.id, "w", ttl_seconds=60)
    b.record_failure(blocked.id, error="missing file: drafts/DESIGN.md")
    b.claim(blocked.id, "w", ttl_seconds=60)
    b.record_failure(blocked.id, error="missing file: drafts/DESIGN.md")
    p = build_planner_prompt(b, "rob", None)
    assert "drafts/DESIGN.md" in p
    assert "[MISSING on disk]" not in p


# ---------------------------------------------------------------------------
# workspace_root=None byte-identity regression (supervised-mode style guard)
# ---------------------------------------------------------------------------

def test_build_goal_run_task_byte_identical_without_workspace_root(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    o = b.create_objective(user_id="rob", title="Grow the substack",
                           body="1 real post/day.")
    g = b.create(user_id="rob", title="Draft memory-arch thread", parent_id=o.id,
                 body="Update drafts/DESIGN.md with the new section.",
                 payload={"acceptance": "notes.txt reflects the change"})
    legacy = build_goal_run_task(g, o)
    explicit_none = build_goal_run_task(g, o, workspace_root=None)
    assert legacy == explicit_none
    assert "[MISSING on disk]" not in legacy
    assert "[present" not in legacy
