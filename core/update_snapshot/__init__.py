"""The update snapshot: what a ``polyrob update`` or a boot migration backs up, and the restore.

``context`` resolves the inputs (every database, the env files, the identity,
skills and wallet directories) from the runtime paths; ``snapshot`` writes and
prunes snapshots with the SQLite Online-Backup API; ``restore`` puts one back
through pinned directories. A core package (proposal 125 S21h) so the boot
migration in ``migrations/boot.py`` and the CLI updater share it downward.
"""
