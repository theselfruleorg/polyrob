# Feishu / Lark and DingTalk

Two chat surfaces for teams that work in Feishu (Lark outside mainland China)
or DingTalk. Both run under `polyrob gateway`, both start as soon as their
credentials are in your environment (configured = on), and both follow the
same access model as every other surface: the owner, correspondents, group
members, and everyone else denied. See [groups.md](groups.md) and
[owner-controls.md](owner-controls.md).

Your owner identity on these surfaces comes from a pairing row: send the bot a
message, then approve the pairing request from a seat you already own
(`polyrob owner approve`). A configured owner id alone does not make a sender
the owner.

## Feishu / Lark

### Create the app

1. In the Lark developer console (open.larksuite.com — or open.feishu.cn for a
   mainland Feishu tenant), create a **custom app** and enable its **bot**
   capability.
2. Grant the message scopes: receive messages in DMs and in groups where the
   bot is @-mentioned, send messages, read message resources (for images and
   files), and upload images and files.
3. Subscribe to the event `im.message.receive_v1`. For buttons, also enable the
   card callback `card.action.trigger`.
4. Copy the **App ID** (`cli_…`) and the **App Secret**.

### Configure

```bash
polyrob surfaces add feishu        # asks for FEISHU_APP_ID and FEISHU_APP_SECRET
polyrob surfaces probe feishu      # proves them with a tenant_access_token read
```

| Setting | Default | Meaning |
|---|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | unset | The app credentials. Both set = the surface starts. |
| `FEISHU_DOMAIN` | `lark` | `lark` (open.larksuite.com, international) or `feishu` (open.feishu.cn, mainland). |
| `FEISHU_TRANSPORT` | `ws` | `ws` = the long connection (no public URL; needs `pip install "polyrob[feishu]"`). `webhook` = the event request URL (no extra). |
| `FEISHU_ENCRYPT_KEY` / `FEISHU_VERIFICATION_TOKEN` | unset | Webhook mode only: the Event Subscription secrets. The Encrypt Key is required; a token alone needs the opt-in below. |
| `FEISHU_WEBHOOK_ALLOW_UNSIGNED` | off | Webhook mode only: accept events with a Verification Token and no Encrypt Key. Such events carry no signature. |
| `OWNER_FEISHU_ID` | unset | Where owner notices go: an `ou_` open_id, an `oc_` chat id, or an email. |

### Long connection or webhook

- **`ws` (default).** The app opens an outbound connection to the platform, so
  you need no public URL. In the developer console, select "receive events
  through a long connection".
- **`webhook`.** The platform POSTs events to
  `https://<your host>/webhooks/feishu`. Put the gateway's webhook port behind
  your TLS reverse proxy for the `/webhooks/` path. Set the same URL as the
  app's event request URL; the gateway answers the `url_verification`
  challenge itself. With an Encrypt Key set, every body is decrypted once, every
  event must carry a valid `X-Lark-Signature`, and a plaintext body is refused.
  With neither secret set, the gateway refuses every request. With only a
  Verification Token, no event is signed, so anyone with the token or one
  captured body could type as any user. The gateway skips Feishu and
  `polyrob surfaces probe feishu` names `FEISHU_ENCRYPT_KEY`. Set the Encrypt
  Key, or set `FEISHU_WEBHOOK_ALLOW_UNSIGNED=true` to accept the risk.

### What works

- DMs, and group messages that @-mention the bot. A group message without the
  mention is not read.
- Images, files, audio and video in. The owner's files are stored in the
  session workspace; a correspondent's files are only named.
- Images and files out. A failed upload is named in the chat; the text is
  still delivered.
- Approve / reject buttons as an interactive card. A press counts as the
  presser typing the command, so a non-owner's press is refused exactly like a
  typed command.
- Text is split at 10 000 characters. Markdown does not render.

Not yet: cron delivery to Feishu, streaming card replies, voice replies.

## DingTalk

### Create the app

1. In the DingTalk developer console, create an **internal app** and add a
   **robot** to it.
2. Set the robot's message receive mode to **Stream Mode**.
3. Copy the **Client ID** (AppKey — it is also the robot code) and the
   **Client Secret** (AppSecret).

DingTalk developer accounts generally need a mainland-China entity.

### Configure

```bash
polyrob surfaces add dingtalk      # asks for DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET
polyrob surfaces probe dingtalk    # proves them with an accessToken read
```

| Setting | Default | Meaning |
|---|---|---|
| `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET` | unset | The app credentials. Both set = the surface starts. |
| `OWNER_DINGTALK_ID` | unset | Where owner notices go: the owner's staff id, or a `cid…` group conversation id. |

### What works

- Stream Mode: an outbound WebSocket, so no public URL and no extra package.
- DMs, and group messages that @-mention the robot.
- Replies go through the conversation's session webhook while it is valid and
  through the robot API after it expires.
- Text is split at 5 000 characters.

Not yet: cron delivery to DingTalk, AI Card streaming, buttons, media.
