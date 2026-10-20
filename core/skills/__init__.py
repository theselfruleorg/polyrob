"""The one home of the skill catalog (081 WS-4.1).

- :mod:`core.skills.frontmatter` — SKILL.md frontmatter parse/emit and validation.
- :mod:`core.skills.scopes` — every skill root, discovery, and the ONE precedence
  function :func:`core.skills.scopes.resolve`.
- :mod:`core.skills.catalog` — the system rule index and the gate predicates.

Writing lives in ``modules/skills`` (it needs the threat scanner); session
matching and prompt delivery stay in ``agents/task/agent/skill_manager.py``.
"""
