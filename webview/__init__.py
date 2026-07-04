"""POLYROB webgate (FastAPI + Socket.IO) package.

Making ``webview`` a regular package lets setuptools discover it and ship its
``static/`` + ``templates/`` assets as package-data in the wheel (see
``pyproject.toml`` ``[tool.setuptools.package-data]``). The runtime prefers the
packaged ``web_dist/`` bundle and falls back to these dev-tree assets via
``core.assets.webgate_asset_dir()``.
"""
