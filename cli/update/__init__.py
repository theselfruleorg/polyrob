"""`polyrob update` — reliable self-update for the OSS release.

This package is
built in slices: detection + version resolution + `--check`/`--dry-run` first (read
only, zero mutation), then the snapshot/rollback safety net, then the mutate paths.
"""
