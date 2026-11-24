"""The agent's own self-notes preference rows (``self.*``, proposal 128 D3): one more file, the
same machinery as ``core/prefs_launch.py``. Built by a call so this module imports nothing from
``core/prefs.py``.

An explicit owner act, so a preference and not an env flag ("What earns a setting"): the owner
lets the agent's OWN scheduled runs (a cron or goal run of the main agent) write ``self.md`` at
once, with no draft. ``tools/controller/doc_authoring.py`` is the one reader; each such write must
quote the soul line it serves and is told to the owner.
"""


def build_self_specs(_spec, SENSITIVITY_GUARDED) -> tuple:
    return (
        _spec("self.autonomous_writes", "bool", SENSITIVITY_GUARDED, "override", "live",
              default_display=False,
              description="The agent's own scheduled runs write its self notes at once (no "
                          "draft); each write quotes the soul line it serves and the owner is "
                          "told. Off = every such write is a draft for the owner"),
    )
