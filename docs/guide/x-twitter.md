# X / Twitter rails

POLYROB has two complementary X rails because API access and the visible X inbox
are not equivalent.

## API rails

X exposes two different private-message protocols:

- **X Chat** (where X delivers DMs): `GET /2/chat/conversations`,
  `GET /2/chat/conversations/{id}/events` and, to send,
  `POST /2/chat/conversations/{id}/messages` (a first contact registers a
  conversation key with `POST /2/chat/conversations/{id}/keys`). It requires an
  OAuth 2.0 PKCE user token with `dm.read`, `dm.write`, `users.read`, and
  `tweet.read`. Events contain ciphertext; POLYROB uses the official Chat XDK
  and the account's Chat keys to verify, decrypt and encrypt them.
- **Legacy Direct Messages**: the plaintext send still delivers (it is the cold
  open path). The read, `GET /2/dm_events`, is **obsolete**: X stopped delivering
  new DMs to it in September 2026, so it shows only old history; results are
  marked `obsolete`.

`twitter_dm` tries X Chat first. When X Chat refuses the send (a 4xx — X Chat
rejects a cold first contact — or a recipient without X Chat keys), it sends on
the plaintext endpoint (`POST /2/dm_conversations/with/:id/messages`), which still
delivers; the result names the rail. A 5xx or timeout is never re-sent on the
other rail, since the message may have landed. Every decrypted message carries `verified`; a sender whose X
Chat key record has no identity binding signature is returned with
`verified: false` and a `trust` note instead of being dropped. A thread whose key
this account cannot read is never re-keyed; the send goes plaintext instead.

`twitter_get_dms` accepts `rail=auto|chat|legacy`. `auto` selects X Chat when
the OAuth 2.0 login is usable (legacy only when that login is dead). An account-wide Chat read lists
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

polyrob x-account oauth-login --account-id <agent-account-id>  # first verified login
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

### Renewing the login from your phone: `/x login`

X revokes the pair when the app's secret is regenerated or the owner revokes
access, and the refresh then fails for good. You do not need a shell to fix
it: send **`/x login`** to your agent (Telegram, the terminal chat, or the
console chat). It answers with a one-time X link. Open it, approve as the
agent's X account, and X sends your browser back to the console, which stores
the new pair and says "X login renewed — you can close this tab". You also get
the "X login renewed" message in chat. **`/x status`** (or bare `/x`) shows the
state of the API login and the browser session, without any secret.

One-time setup on a deployed instance:

```sh
# 1. developer.x.com → your app → User authentication settings → Callback URI:
#    add EXACTLY this URL (keep the 127.0.0.1 one if you also use oauth-login)
https://<your console host>/api/packs/x/oauth/callback

# 2. the same URL in the instance env — both the agent and the console read it
X_OAUTH2_REDIRECT_URI=https://<your console host>/api/packs/x/oauth/callback
```

Then restart both services. Without the flag, `/x login` answers with this
remedy and does nothing else. The link works once and expires after ten
minutes; the callback is the console's only unauthenticated route of a pack,
and its proof is that one-time link, not a console login (the tab X opens may
have none). The console reaches it even when it runs read-only.

An empty result, or a result containing only the account's own sent messages,
means only that the configured API access tier returned no inbound events. It
must not be reported as an empty X inbox. DM lookup requires user-context OAuth,
and endpoint availability remains subject to the X app's access tier.

The `polyrob x` background surface poller is **obsolete**: it polls the legacy
event feed, which no longer receives new DMs. Use `twitter_get_dms` /
`twitter_dm` (X Chat) instead.

## Who may write on X

Every X write (a post, a thread, a reply, a DM) goes through the same owner rule:

- **A genuine owner turn may post.** With `TWITTER_REQUIRE_APPROVAL` ON (the
  default) the agent asks you once more before the write; OFF, your request is the
  approval. A turn that has read third-party content since you last spoke (a page,
  a mail, another post) always needs a real approval, whatever the flag says.
- **An owner-authored standing cron job posts without a per-run approval**, as long
  as its run has not read third-party content. A job the agent wrote, or one made on
  a turn that had read such content, is agent-authored: each of its posts needs your
  tap. `/adopt` makes such a job yours after you see what it will do.
- **An autonomous run waits in the run for your tap.** It queues the ask (a Telegram
  card, or `/pending`) and waits inside the X action's own timeout. If you do not
  answer in time, the result reads "waiting for the owner's approval" and nothing
  was posted; the run does not retry. A later approval sends exactly the approved
  text once from the agent process, and never re-runs the job.
- **Any other turn** (a self-wake, a delegation result) needs a real owner approval,
  and a room turn cannot reach the X tools at all.

Writes are also bounded by count: `TWITTER_WRITE_MAX_PER_HOUR` (default 15) is one
durable hourly budget shared by the API and the browser rail, and DMs take a separate
`TWITTER_DM_MAX_PER_HOUR` (default 5) on top of it. An unreadable budget refuses the
write. Autonomous posts also keep a minimum gap (`TWITTER_POST_COOLDOWN_SEC`, one
hour by default).

## Browser rail

The optional `x_browser` tool drives the X web app with a captured login. It is
**not** needed for DMs: the X Chat API above reads and sends them. Capture the login once on a
machine with a visible browser:

```sh
polyrob x-account capture-session
```

When the agent runs on a headless server, capture on the desktop with
`--out x-session.json`, copy the file over and run
`polyrob x-account import-session x-session.json --handle <handle>` on the
server (or enter the two login cookies at hidden prompts with `--cookies`); the encrypted
store itself is per-box and cannot be copied. See the self-hosting guide.

Enable the tool with `X_BROWSER_ENABLED=true`. Its dedicated verbs are:

- `x_read_dms`: list the visible inbox or read an existing thread by handle,
  visible name, or conversation ID.
- `x_dm`: send in an existing visible conversation. This is approval-gated and
  blocked for delegated/forged turns.
- `x_post`: publish a post, also approval-gated.
- `x_reply`: reply under an existing post (status URL or id) — the lane the API tier refuses for non-mentioners; approval-gated like `x_post`.
- `x_login_check`: verify the captured session.

The encrypted session is tenant-scoped. If X expires it, run the capture command
again. Browser-returned messages are framed as untrusted external data before
they reach model history.

The first OAuth login requires the expected numeric X account ID. The callback
checks `/2/users/me` before replacing credentials; another account or an unreadable
identity leaves the previous pair intact. Later `/x login` links bind the stored
account ID when minted. Existing unverified installations need one CLI login with
`--account-id` before phone re-login is available.
