"""Third-party code carried inside the X pack.

``tweepy/`` is tweepy 4.17.0 (MIT, ``tweepy/LICENSE.txt``), the sync package
only (``tweepy.asynchronous`` is left out: nothing here uses it and it needs
aiohttp). The one change from upstream: its absolute ``tweepy.*`` imports point
at this copy.

Why it is here: tweepy 4.17.0 (the latest release) pins ``oauthlib<4``, and
oauthlib 4.0.0+ is the only release with the fixes for PYSEC-2026-4114 and
CVE-2026-49272. With the copy, the ``twitter`` extra depends on
requests-oauthlib + oauthlib>=4.0.1 directly, so pip installs the fixed oauthlib
with no conflict. tweepy's 4.0 breaking changes are all OAuth-server-side.

Remove this copy and depend on tweepy again once a tweepy release allows
oauthlib 4 (upstream PR tweepy/tweepy#2245).
"""
