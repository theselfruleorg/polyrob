"""Public developer tools (W11): the generators whose outputs ship.

Not part of the wheel. Each one is a pure function of public files, so a
public contributor can regenerate what the drift tests check. Run them from the
repo root, e.g. ``python devtools/gen_lazy_closures.py --check``; the old
``python scripts/<name>.py`` commands are shims onto these files.
"""
