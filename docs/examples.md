# Examples

Worked examples, in roughly the order a new install grows into them: one-off tasks
first, then scheduled work, then the things that need a decision from you.

Every command here is real. Flags are named but not explained — that is
[`docs/CONFIGURATION.md`](CONFIGURATION.md)'s job, and
[guide/configuration.md](guide/configuration.md) explains how they resolve.

---

## 1. One task, right now

```bash
polyrob run "summarize https://example.com/article"
polyrob run "take a screenshot of https://github.com and save it"
polyrob run "read ./src and write architecture.md: modules, dependencies, data flow"
polyrob run -t browser,filesystem "find the top 3 launches on Product Hunt today and save them as markdown"
```

`-t` picks an explicit tool list, `--toolset` picks a named one, `--max-steps` bounds
the run. `polyrob run --resume <session_id>` continues a session instead of starting
one.

The REPL is the same agent with a conversation around it:

```bash
polyrob chat
> Analyse the code in ./src and find the three riskiest input paths.
> Now write pytest cases for the first one.
> /memory        # which recall provider is live
> /inbox         # anything waiting on a decision from me
```

**Be specific about the artifact.** "Research AI companies" gives you prose;
"search for AI automation companies founded after 2020 with over $10M raised,
extract name, funding, investors and product focus, and write a comparison table to
`competitors.md`" gives you a file you can check.

---

## 2. Work on a schedule

Cron is POLYROB's durable scheduler: jobs survive restarts and are tenant-scoped.
It needs `CRON_ENABLED=true` (on by itself at `AUTONOMY_POSTURE=full`).

```bash
polyrob cron schedule "Check https://example.com/product/12345 and tell me if it is under $500" "every day 09:00"
polyrob cron schedule "Review error logs for new patterns and summarise" "every sunday 03:00"
polyrob cron schedule "Re-run the smoke suite" "30m"
polyrob cron list
polyrob cron show <id>
polyrob cron cancel <id>
```

Schedule formats and the per-run cap: [guide/streams.md](guide/streams.md#on-a-clock--cron).

The same thing in chat, in plain words — the agent calls its own cron tool and tells
you the job id and the next run:

```
> Every weekday at 8am, check security@company.com for new alerts and email me a
> summary if anything is critical.
```

That is the whole cron story; the rest of this page assumes it.

---

## 3. Work that is not on a clock

A **goal** is durable background work with no schedule. It needs a dispatcher:
`AUTONOMY_ENABLED` (plus `POLYROB_LOCAL`, which the CLI sets for you).

```bash
polyrob autonomy on
polyrob goals create "Pricing comparison" \
  -b "Scrape the five competitor pricing pages, normalise, write the table." \
  --acceptance "a file at reports/pricing.md with five rows" -p 7
polyrob goals list --status ready
polyrob goals tree
```

Goals can wait on each other. The dependency edges are set through the agent's own
goal tool, so ask for them:

```
> Create three goals: scrape competitor pricing pages, normalise the data into a
> table, and write the comparison report — each depending on the one before it.
```

An **objective** is a standing mission the planner keeps feeding:

```bash
polyrob goals objective add "Grow the newsletter" \
  --success-criteria "list size and open rate trending up, judged monthly" \
  --goal-budget 20
```

Full treatment, including fair dispatch and the knobs that matter as the board
grows: [guide/streams.md](guide/streams.md).

---

## 4. The decision loop

Autonomous work stops when it needs you. One command shows everything that does:

```bash
polyrob owner inbox          # blocking items first, across every store
polyrob owner asks           # what the agent needs to unblock work
polyrob owner fulfill <id>   # supply it; the blocked goals go back to ready
polyrob owner pending        # the agent's own proposals, waiting for promotion
polyrob owner promote <kind> <id>   # kind: self_context | skill | tool_approval | all
```

`/inbox` in the REPL and on Telegram render the same composition. A store that
refuses to open is **named**, and the count becomes a floor — this never prints
"nothing needs you" over a list it could not read.

Money and high-impact actions wait here too. Under `PAYMENT_APPROVAL_MODE=approve`
an outward payment is a durable ask you can answer from your phone.

---

## 5. Let people reach it

```bash
polyrob gateway        # every enabled surface in one process
polyrob telegram       # or one at a time
polyrob email
polyrob serve          # the REST/A2A/OpenAI-compatible API
polyrob dashboard      # the web console
```

An enabled surface with missing credentials is warned about and skipped, so read the
startup output. Bind yourself as owner before exposing anything:

```bash
polyrob config set POLYROB_OWNER_TELEGRAM_ID 123456789
polyrob config set ALLOWED_TELEGRAM_USER_IDS 123456789
polyrob owner show
```

**Email gives the agent its own address.** Set `AGENTMAIL_API_KEY` and it provisions
a managed inbox on first run — no IMAP or SMTP setup. The legacy path still works:

```bash
EMAIL_SURFACE_ENABLED=true
GMAIL_EMAIL=bot@example.com
GMAIL_APP_PASSWORD=your-16-char-app-password
polyrob email
```

Email is **correspondent-only**: a reply routes back as data into the session that
reached out first, never as a command a stranger can send.

```bash
polyrob run "Email finance@company.com asking for this month's invoice totals"
```

### A group room

A room is a different regime from a DM: the agent answers anyone in a room you have
allowed, from a read-only toolset, under a policy you set per room.

```bash
polyrob config set GROUP_CHAT_ENABLED true
```

Then, from inside the room (owner only, confirmed by DM):

```
/groups allow here
/groups set here chat.mode active
/groups set here chat.reply_cap_per_hour 10
/groups service here every 30m        # a recurring catch-up pass over the room
```

Nothing is answered in a room you have not allowed. The Telegram privacy-mode step
and the full policy list are in [guide/groups.md](guide/groups.md).

---

## 6. Several bots on one machine

A profile is a whole isolated home — env, characters, skills, memory, goals,
sessions:

```bash
polyrob profile create scout --description "research bot"
polyrob -P scout                      # run the REPL as scout
polyrob -P scout telegram             # scout's own daemon, its own bot token
polyrob profile use scout             # make it sticky on this machine
polyrob profile list
polyrob profile export scout -o scout.tar.gz   # credentials never travel
```

`polyrob profile install https://github.com/you/scout-profile#v1.2` installs a
shared identity; your `.env`, wallet and `data/` are never touched by an update.
See [guide/profiles.md](guide/profiles.md).

---

## 7. Ship something that stays up

An app the agent builds can outlive its session, behind a public URL, without the
agent ever holding the privilege to do it.

```bash
polyrob config set AGENT_BUILDER_MODE ship     # PUBLISH_ENABLED + APP_SERVICE_* on
polyrob config set APP_SERVICE_BASE_DOMAIN apps.example.com
```

The agent deploys into a **pending** row; that row *is* the ask. You decide:

```bash
polyrob apps list             # status, URL, last health
polyrob apps show <slug>
polyrob apps approve <slug>   # the owner decision the agent cannot make
polyrob apps logs <slug>
polyrob apps kill <slug>
```

`/apps` on Telegram and in the REPL render the same thing. Approval binds to the
address **and** the approved configuration, so a code bump redeploys unattended
while a changed command, port or environment-key set comes back to you naming what
changed. Without a base domain and a certificate, `ship` clamps to `build` and apps
run on loopback only — `apps list` says so rather than showing a URL that does not
work.

---

## 8. Bound what a run can cost

```bash
RUN_BUDGET_USD=0.50 polyrob run "Research the top 20 vector databases and compare them"
```

The run stops **honestly** the moment its real provider cost reaches the cap —
reported as stopped, never as a fabricated "done". The remaining budget is shown to
the agent so it can pace itself, and sub-agents share the parent's.

---

## 9. Run code somewhere that is not your laptop

Code execution is off by default and is never in a default toolset.

```bash
CODE_EXEC_ENABLED=true
CODE_EXEC_BACKEND=docker         # the hardened container
```

Or a remote host over your system `ssh`:

```bash
CODE_EXEC_BACKEND=ssh
CODE_EXEC_SSH_HOST=build-box.internal
CODE_EXEC_SSH_USER=agent
CODE_EXEC_SSH_KEY=~/.ssh/agent_id_ed25519
CODE_EXEC_SSH_SANDBOXED=true     # your attestation that the host is disposable
```

⚠️ The `ssh` backend is **not a sandbox**: agent code runs with the SSH user's full
privileges. A server refuses it without that attestation, and refuses
`local_subprocess` outright. Read
[guide/security-model.md](guide/security-model.md) before enabling any of this on a
shared machine.

---

## 10. Let it learn from its own work

`AUTONOMY_ENABLED` (with `POLYROB_LOCAL`) turns on `SKILLS_WRITABLE` and
`BACKGROUND_REVIEW_ENABLED` as a group. A background reviewer then forks off every
`BG_REVIEW_INTERVAL` productive turns (default 10) and can author or patch a skill
from what worked — staged for your review, never silently active:

```bash
polyrob autonomy on
polyrob skill list          # builtin / user / external, with provenance
polyrob skill approve <id>  # activate a quarantined one
```

A skill from someone else goes through the managed path, which threat-scans every
file and quarantines it:

```bash
polyrob skill install owner/repo
```

See [guide/skills.md](guide/skills.md).

---

## 11. Money, end to end

> ⚠️ Unaudited, off by default, and it moves real value on mainnet. Validate on a
> testnet first. Every flag, gate and cap:
> [guide/payments.md](guide/payments.md).

**1. Create the wallet** (once; prints the mnemonic a single time):

```bash
polyrob wallet init
polyrob wallet          # every address, balances, caps
```

**2. Arm sight, then spend:**

```bash
DEFI_DATA_ENABLED=true
DEFI_TRADE_ENABLED=true
DEFI_EVM_RPC_BASE=https://base-mainnet.your-provider.example/v2/KEY   # money refuses unpinned
DEFI_AUTONOMOUS_MAX_USD=5
WALLET_DAILY_CAP_USD=25
PAYMENT_APPROVAL_MODE=approve
```

**3. Screen first, address-first, dry-run-first:**

```bash
polyrob run "
Resolve the ticker TOSHI with defi_data.token_resolve and show me every candidate
contract address with its liquidity — do not pick one for me.
Then run defi_data.token_info on the address I name and report the safety screen
verbatim. If the screen is clean, quote a \$2 swap from USDC into it with
swap_quote, then run defi_trade.swap with max_spend_usd=2 as a DRY RUN and show me
the guard's verdict, the simulated deltas, and the lane.
"
```

Re-running with `dry_run=false` broadcasts only after the same guard passes; above
`DEFI_AUTONOMOUS_MAX_USD` it waits on the owner queue instead.

**4. Check the book against the chain** — the anti-phantom-position check:

```bash
polyrob wallet book      # every money chain, one verdict, then what disagrees
```

**5. Invoice, and let the chain settle it:**

```bash
X402_INVOICE_ENABLED=true
X402_SETTLE_ONCHAIN_DETECT=true       # a plain USDC transfer settles it, no facilitator
INVOICE_CARD_ENABLED=true             # a branded QR card is attached to the delivery
```

```bash
polyrob run "
Create a \$5 invoice for 'site audit — example.com' billed to
'Alice <alice@example.com>' with x402_request, and send her the invoice card by email.
"
```

When Alice sends USDC to the treasury address, the settlement watcher matches the
exact amount, marks the invoice completed, wakes the originating session, and the
payment appears as income in `polyrob finance` — never summed with your compute
cost.

**Solana:** arm `SOLANA_TRADE_ENABLED=true`, pin `DEFI_SOLANA_RPC`, fund the Solana
address shown by `polyrob wallet` with SOL for fees, and ask for a swap by **mint
address**.

---

## 12. Call it from your own software

Start the API with `polyrob serve` and mint a key at `POST /api/auth/api-keys`. The
whole route surface, the auth methods and the MCP-server setup for Claude Desktop or
Cursor are in [guide/api.md](guide/api.md).

**A chat bot** wants a synchronous reply, so use the OpenAI-compatible surface
(`OPENAI_COMPAT_API_ENABLED=true`) rather than session creation, which is
fire-and-forget:

```python
import requests

def ask(text: str, user_id: str) -> str:
    r = requests.post(
        "http://localhost:9000/v1/chat/completions",
        json={"model": "gpt-5",
              "messages": [{"role": "user", "content": text}],
              "user": user_id},
        headers={"X-API-KEY": API_KEY},
    )
    return r.json()["choices"][0]["message"]["content"]
```

**A webhook** wants to start work and walk away, so use a session:

```python
@app.post("/webhook/github")
async def github_webhook(request):
    payload = await request.json()
    if payload["action"] != "opened":
        return {"status": "ignored"}
    r = requests.post(
        "http://localhost:9000/api/task/sessions",
        json={"task": f"Review this PR and comment on it: {payload['pull_request']['url']}"},
        headers={"X-API-KEY": API_KEY},
    )
    # Returns a session_id immediately, not the review. Poll
    # GET /api/task/sessions/{id}, or watch it in the console.
    return {"session_id": r.json()["session_id"]}
```

Posting the comment itself needs the optional `github` tool
(`GITHUB_TOOL_ENABLED=true` + `GITHUB_TOKEN`); PR comments are high-impact and
approval-gated.

**In CI**, run it as a command:

```yaml
- name: Review the diff
  env:
    GITHUB_TOOL_ENABLED: "true"
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  run: |
    polyrob run --tools github,filesystem "
    Review PR #${{ github.event.pull_request.number }} of ${{ github.repository }}
    for security issues, breaking changes and API compatibility, and post your
    findings as a comment.
    "
```

---

## When something does not work

| Symptom | Check |
|---|---|
| A goal sits there forever | `polyrob autonomy status` — the master switch, the posture, and whether a pause is active |
| "no dispatcher will pick goals up" | `polyrob autonomy on` |
| The agent refuses a tool | `polyrob tools status` names the gate and the remedy |
| A flag seems to do nothing | `polyrob doctor --flags --search NAME`, then `polyrob config check` |
| It stopped answering in a room | `/groups set here chat.mode active`, and check the room is still allowed |
| Nothing is happening at all | a pause is in force: `polyrob autonomy resume` |

---

## More

- [guide/getting-started.md](guide/getting-started.md) · [guide/cli.md](guide/cli.md) · [guide/api.md](guide/api.md)
- [guide/configuration.md](guide/configuration.md) · [`docs/CONFIGURATION.md`](CONFIGURATION.md)
- [guide/security-model.md](guide/security-model.md) · [guide/payments.md](guide/payments.md)
- [GitHub Discussions](https://github.com/theselfruleorg/polyrob/discussions) ·
  [Issues](https://github.com/theselfruleorg/polyrob/issues)

Have a use case worth adding? Open a pull request against this file — see
[CONTRIBUTING.md](../CONTRIBUTING.md).
