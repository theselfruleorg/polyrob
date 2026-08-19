"""Semver DB migrations for bot.db.

This __init__.py makes migrations a regular package so setuptools ships it in
the wheel — the 0.10.0 wheel omitted it and pip installs could never migrate
(proposal 027 WP2). Runner: python -m migrations.migrate [status|upgrade].
"""
