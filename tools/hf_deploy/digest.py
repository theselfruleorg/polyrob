"""Workspace digest + the ship==tested green-test gate (proposal §3.3/§3.6,
acceptance-contract leg 1: "the thing you deploy is the thing you just tested").

``compute_workspace_digest`` is a pure, deterministic sha256 over the sorted
relative file paths + bytes of a workspace tree. WHICH files that is comes from
``core/ship_tree.py`` — the ONE exclusion policy, shared with
``core/app_service/snapshot.py``, so the digest identifies exactly the bytes a
deploy ships (before, the two filtered different trees and the recorded digest
did not describe the shipped tree). ``tested_tree_digest`` is the gate itself:
it refuses (returns
``(None, reason)``) unless the session's action ledger shows a SUCCESSFUL
``run_tests`` with no edit action after it — reusing
``agents.task.runtime.edit_verify.edited_since_last_test`` (same ledger walker
the coding-finalization "did you edit without re-testing" check uses) rather
than adding new session-timestamp state.
"""
import hashlib
import os

from agents.task.runtime.edit_verify import TEST_ACTIONS as _TEST_ACTIONS
from core.ship_tree import SKIP_DIRS as _SKIP_DIRS  # noqa: F401  (kept importable)
from core.ship_tree import walk_shippable


def compute_workspace_digest(root: str) -> str:
    """Deterministic sha256 over the workspace tree.

    Sorted relative paths (so rename/add/remove always changes the digest) +
    file bytes, over exactly the files a deploy would ship
    (``core.ship_tree.walk_shippable``): noise directories are pruned at any
    depth, symlinks / special files / credential-shaped files are excluded
    because the snapshot refuses them, and an otherwise-empty directory
    contributes nothing — only FILES are hashed.
    """
    root = os.path.abspath(root)
    paths = {rel: full for rel, full in walk_shippable(root)}

    h = hashlib.sha256()
    for rel in sorted(paths):
        h.update(rel.encode("utf-8", errors="replace"))
        h.update(b"\x00")
        try:
            with open(paths[rel], "rb") as f:
                h.update(f.read())
        except OSError:
            pass  # a file that vanished mid-walk contributes no bytes, not a crash
        h.update(b"\x00")
    return h.hexdigest()


def tested_tree_digest(orch, root: str):
    """Return ``(digest, None)`` when the tree is safe to ship, else
    ``(None, reason)``.

    Refuses when:
    - there is no orchestrator to check ledger history against (``orch is
      None``) — cannot verify, refuse-closed;
    - the session ledger shows no SUCCESSFUL ``run_tests`` at all;
    - a code-edit action (``str_replace``/``apply_patch``/``create_file``/
      ``move_file``/``delete_file``) succeeded after the last green
      ``run_tests`` (``edited_since_last_test``).

    Only on all three checks passing is the CURRENT tree digest computed and
    returned as the "tested" digest — this call reuses the ledger walk rather
    than persisting a separate ``last_green_test_digest`` in session state.

    Single-agent assumption: ``_walk_ledger`` yields steps grouped BY AGENT (all
    of agent A's steps, then agent B's), NOT in global chronological order, so
    ``edited_since_last_test``'s ``last_edit > last_test`` index compare is
    strictly correct only for a single-agent session. This is INERT for
    ``hf_deploy``: the ``coding`` tool (which owns ``run_tests`` and the edit
    verbs) is in ``DELEGATE_BLOCKED_TOOLS``, so a sub-agent can neither edit nor
    run tests — a sub-agent green-test can never mask a main-agent edit here.
    """
    if orch is None:
        return None, "cannot verify: no orchestrator/session ledger to check test history against"

    from agents.task.runtime.evidence import _walk_ledger
    from agents.task.runtime.edit_verify import edited_since_last_test

    has_green_test = False
    try:
        for _label, name, _action, result in _walk_ledger(orch):
            if name in _TEST_ACTIONS and not getattr(result, "error", None):
                has_green_test = True
    except Exception:
        has_green_test = False  # fail-closed for deploy: an unreadable ledger is not proof of green

    if not has_green_test:
        return None, "no green run_tests found in the session ledger — run tests before deploying"

    if edited_since_last_test(orch):
        return None, "the workspace was edited after the last green run_tests — re-run tests before deploying"

    return compute_workspace_digest(root), None
