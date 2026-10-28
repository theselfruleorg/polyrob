"""A seeded ``goals.db`` for the Work › Goals tests: every live status, a
dependency, a parent with a sub-goal, an open decision, two finished goals, and
one goal of ANOTHER tenant that must never show.

Shared by ``test_goal_board_routes.py`` and the rendered-board check
(``tests/integration/test_goal_board_render.py``)."""
from agents.task.goals.board import GoalBoard

OWNER = "u1"
OTHER = "u2"


def seed(db_path: str, owner: str = OWNER) -> dict:
    """Write the board for *owner* and return ``{name: goal_id}``."""
    b = GoalBoard(db_path)
    ids = {}

    def mk(name, title, *, status=None, user=owner, **kw):
        g = b.create(user_id=user, title=title, force=True, **kw)
        if status and status != g.status:
            b.update_status(g.id, status)
        ids[name] = g.id
        return g

    mk("prices", "Refresh the token prices", body="Read the three price feeds.",
       payload={"owner_granted": True, "acceptance": "prices.json is under one hour old",
                "cost_usd": 0.42})
    mk("digest", "Write the weekly digest", status="running",
       payload={"stream": "weekly-digest", "cost_usd": 1.37})
    mk("recap", "Post the weekly recap", depends_on=[ids["digest"]],
       payload={"origin_session_id": "s-chat"})
    mk("sorting", "Look at the new inbox mail", status="triage",
       payload={"created_by_session_id": "s-self"})
    mk("site", "Fix the landing page", status="blocked",
       payload={"owner_granted": True, "block_kind": "needs_input"})
    mk("trade", "Rebalance the holdings", status="paused", payload={"owner_granted": True})
    mk("sub", "Draft the digest outline", status="running", parent_id=ids["digest"],
       payload={"created_by_session_id": "s-self"})
    mk("shipped", "Ship the changelog", status="done", payload={"owner_granted": True})
    mk("dropped", "Old clean-up", status="cancelled")
    mk("foreign", "Another tenant's goal", user=OTHER)
    ask = b.create_ask(user_id=owner, what="Which page should the site link to?",
                       why="The landing page needs a target.",
                       blocks_goal_ids=[ids["site"]])
    ids["ask"] = ask.id
    b.stamp_session(ids["digest"], "s-digest")
    return ids
