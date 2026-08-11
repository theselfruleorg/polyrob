#!/usr/bin/env python3
"""Main entry point for the bot — thin shim over api.server_boot.

The uvicorn launch path lives in the installed ``api`` package
(``api/server_boot.py``) so ``polyrob serve`` works from any install; this
repo-root file remains only for the ``python main.py`` systemd entry.
"""

import os
import sys

# Use single project root path approach
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
    print(f"Added {project_root} to Python path")

from api.server_boot import APP_TARGET, run_server  # noqa: E402,F401


def main():
    """Main entry point - launch uvicorn server."""
    run_server()


if __name__ == "__main__":
    main()
