"""`polyrob update` — reliable self-update for the OSS release.

See docs/plans/2026-07-01-polyrob-update-command-UPGRADE-PROPOSAL.md. This package is
built in slices: detection + version resolution + `--check`/`--dry-run` first (read
only, zero mutation), then the snapshot/rollback safety net, then the mutate paths.
"""
