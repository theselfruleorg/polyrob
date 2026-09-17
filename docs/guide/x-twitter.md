# X / Twitter rails

POLYROB has two complementary X rails because API access and the visible X inbox
are not equivalent.

## API rails

X exposes two different private-message protocols:

- **X Chat** (preferred): `GET /2/chat/conversations` and
  `GET /2/chat/conversations/{id}/events`. It requires an OAuth 2.0 PKCE user
  token with `dm.read`, `users.read`, and `tweet.read`. Events contain
  ciphertext; POLYROB uses the official Chat XDK and the account's Chat keys to
  verify and decrypt them.
- **Legacy Direct Messages**: `GET /2/dm_events` and its participant/thread
  variants. OAuth 1.0a user context remains supported, but this rail does not
  contain encrypted X Chat messages.

`twitter_get_dms` accepts `rail=auto|chat|legacy`. `auto` selects X Chat when
`TWITTER_OAUTH2_ACCESS_TOKEN` is configured. An account-wide Chat read lists
the inbox; pass `participant` or `conversation_id` to fetch that thread's
events. The response always reports decryption status, so ciphertext or a
missing key can never be mistaken for an empty thread.

For plaintext X Chat reads configure one of:

```sh
# Existing account using X Chat secure key backup
TWITTER_CHAT_PASSPHRASE=...

# Server/bot identity using a Chat XDK export
TWITTER_CHAT_PRIVATE_KEYS_B64=...
TWITTER_CHAT_KEY_VERSION=...
```

Both require an OAuth 2.0 user token. The ordinary `TWITTER_BEARER_TOKEN` is
app-only and cannot read private messages.

### Getting and keeping the OAuth 2.0 user token

An X user access token expires **two hours** after it is minted. POLYROB keeps
it alive for you: the pair (access + refresh) lives encrypted in the X token
store and is refreshed automatically before expiry and on any 401. You need the
app's Client ID once:

```sh
# developer.x.com → your app → User authentication settings: enable OAuth 2.0,
# type "Web App, Automated App or Bot", redirect URI http://127.0.0.1:8765/callback
TWITTER_OAUTH2_CLIENT_ID=...          # required to refresh
TWITTER_OAUTH2_CLIENT_SECRET=...      # only for a confidential app

polyrob x-account oauth-login         # PKCE in a browser, logged in AS the agent's account
polyrob x-account oauth-status        # valid for N min · refresh token yes · scope [...]
polyrob x-account oauth-refresh       # prove the refresh works right now
```

Already have a pair from a manual PKCE run? `polyrob x-account oauth-import`
takes both values on hidden prompts and stores them. As an alternative for a
headless deploy, set `TWITTER_OAUTH2_ACCESS_TOKEN` **and**
`TWITTER_OAUTH2_REFRESH_TOKEN` in the env once: the pair is seeded into the
store on first use and managed from there. A static access token alone still
works as an override — for exactly two hours.

Scopes to request: `dm.read dm.write tweet.read users.read offline.access`
(`offline.access` is what makes X issue a refresh token at all).

An empty result, or a result containing only the account's own sent messages,
means only that the configured API access tier returned no inbound events. It
must not be reported as an empty X inbox. DM lookup requires user-context OAuth,
and endpoint availability remains subject to the X app's access tier.

The current `polyrob x` background surface poller remains on the legacy event
feed. Interactive and agent reads should use `twitter_get_dms` with the Chat
rail for X Chat conversations.

## Browser rail

The optional `x_browser` tool reads the inbox X renders to the logged-in account,
so it is the fallback when API reads are incomplete. Capture the login once on a
machine with a visible browser:

```sh
polyrob x-account capture-session
```

Enable the tool with `X_BROWSER_ENABLED=true`. Its dedicated verbs are:

- `x_read_dms`: list the visible inbox or read an existing thread by handle,
  visible name, or conversation ID.
- `x_dm`: send in an existing visible conversation. This is approval-gated and
  blocked for delegated/forged turns.
- `x_post`: publish a post, also approval-gated.
- `x_login_check`: verify the captured session.

The encrypted session is tenant-scoped. If X expires it, run the capture command
again. Browser-returned messages are framed as untrusted external data before
they reach model history.
