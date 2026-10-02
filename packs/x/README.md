# The x pack

A first-party POLYROB pack for X (Twitter):

- the `twitter` tool (X API reads, gated posts, engagement and DMs);
- the `x_browser` tool (posting, DMs and account registration through a real
  browser on a saved login; off unless `X_BROWSER_ENABLED`);
- the X DM chat surface (`polyrob x`, or `polyrob gateway` with `X_SURFACE_ENABLED`);
- `polyrob x-account` (capture a login, the OAuth 2.0 PKCE grant);
- the `twitter` cron delivery channel (a cron report as a public post);
- the `x-engagement` and `social-discovery` skills.

It ships inside the `polyrob` distribution: there is nothing separate to
install. Its SDKs (tweepy, chatxdk) are the `twitter` extra:

```bash
pip install 'polyrob[twitter]'
polyrob pack list                  # x ... loaded
```

Without the extra the pack still loads; the `twitter` tool is withheld and
`polyrob pack list` names the remedy (`needs pip install 'polyrob[twitter]'`).
From a source tree, `install.sh --packs x` installs the extra hash-checked.

Configuration flags (`TWITTER_*`, `X_*`) are documented in the core
`docs/CONFIGURATION.md`.
