"""The composition root (W7): the process wiring that builds a running POLYROB.

``bootstrap`` (env layering + the CLI container), ``initialization`` (the server
container's component bring-up), ``autonomy_runtime`` (the background loops) and
``bot`` (the server Bot). They import every lower tier, so they sit ABOVE
``core``/``modules``/``agents``/``tools``/``cron`` and BELOW the entry points
(``api``, ``cli``, ``surfaces``, ``webview``) that call them — see
``tests/test_layering_ratchet.py``. Keep this file free of imports.
"""
