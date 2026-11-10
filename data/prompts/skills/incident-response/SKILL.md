---
name: incident-response
description: 'Run a live incident: impact first, stabilise before root cause, roll back before you debug, one change at a time, an append-only timeline, recovery only on an observed signal. Includes the leaked-secret containment order (revoke or rotate, scope the exposure, purge, prove the old credential dead, record).'
license: MIT
metadata:
  polyrob-when: 'Something is broken live, or a secret leaked'
  polyrob-priority: '2'
  polyrob-owner-only: 'true'
  polyrob-requires: '["secret-handling"]'
  polyrob-version: '2'
---
# Incident Response

An incident is something broken or unsafe RIGHT NOW: a service down or wrong,
money moving that should not, a credential in the wrong hands. The job is to
stop the damage first and understand it second. Recommended procedure — adapt
it to what you can reach this session.

## When to use
- A production service, bot, app or endpoint is down, erroring or giving wrong
  results for real users.
- Funds moved, or are about to move, in a way nobody approved.
- A key, token, password, seed phrase or private key was exposed (pasted,
  committed, logged, posted).

Not for: an ordinary bug with no live impact (use `coding-workflow`), or a
review after the fact with nothing still broken (write the record from
`references/incident-record.md` and stop).

## The rules

1. **Impact first.** Before any fix, establish: what is broken, for whom,
   since when, whether money or a secret is at risk, and whether it is getting
   worse. Set a severity (S1/S2/S3, defined in the reference) from what you
   observed, not from how alarming the alert sounds. If you cannot measure the
   reach yet, use the higher level until you can.
2. **Tell the owner early.** Your first `send_message` goes out as soon as
   you know the impact — not after you know the cause. See "Telling the owner".
3. **Stabilise before root cause.** Stop the bleeding with the smallest safe
   action: roll back, turn a feature off, pause a loop, stop a job. Root-cause
   work starts only when the damage has stopped.
4. **Roll back before you debug.** If the trouble started after a change (a
   deploy, a config edit, a new dependency, a new goal or cron job), returning
   to the last known good state is the default first move. ⚠️ Never edit files
   on a live server and never copy a single file onto it. Roll back or fix
   forward only through the normal deploy path (for this codebase, the deploy
   scripts and their backup). If you cannot deploy, give the owner the exact
   rollback target and the command.
5. **One change at a time.** Say what you will change before you change it,
   record it after, and watch the signal before the next change. Two changes
   at once make it impossible to tell which one helped or hurt.
6. **A mitigation is not a fix.** For each one, record what changed, where,
   whether it is temporary, and what will remove it. A temporary measure
   nobody wrote down becomes permanent.
7. **Recovery is observed, never assumed.** Name the signal (an error rate, a
   health check, a balance, a successful test call), the value that counts as
   healthy, the value you saw and when. "I applied the fix" is not recovery. If
   the signal itself is unavailable, the incident stays open and you say which
   signal is missing.
8. **Keep an append-only timeline** in the session workspace (format in the
   reference): what was seen, what was changed, what was decided, each with a
   time and the reason as believed then. Correct an entry with a new entry;
   never rewrite one.

## When money is at risk

- Ask the owner to `/pause` the affected scope (e.g. trading) or `/halt`
  everything. Call `autonomy_control` yourself only when the owner explicitly
  asks you to stop; only the owner can `/resume`.
- Do not move funds on your own initiative to "save" them. Moving funds is an
  owner decision: if the owner wants it, put the move in front of them with
  `propose_action` (a `/send` quote card they confirm). If that action is
  unavailable, give the `/send …` line WITHOUT `go`, and say that no card was
  made.
- A refusal "unaccounted wallet submission … reconcile before …" (another send,
  or further spending) freezes every on-chain and x402 send by design. Do not
  retry. Read the tx (`defi_data.wallet_activity` or the explorer), give the
  owner the reference, and say that the release is an operator step on the
  server (the terminal commands `polyrob wallet submissions`, then
  `polyrob wallet release-submission <ref> --charge-usd N`, labeled as terminal
  commands).
- Cut standing authority in this order: the dapp session
  (`dapp_browser.dapp_disconnect`; the owner's `/dapp revoke <session-id>`),
  then the token allowance (`defi_trade.revoke_approval`) or the NFT operator
  approval (`defi_trade.nft_revoke_approval`), then the open approvals on an
  owned NFT account (`agent_nft_revoke_all`). Dry-run each revoke first. If the turn cannot broadcast it,
  give the owner the verb to run.
- A transfer that says `BROADCAST BUT NOT CONFIRMED` and a bridge that is
  `in_flight` are never sent again: check the reference first (`defi-bridge`).
- `wallet_sign_status` lists the stored site credentials (ids and
  fingerprints only); a leaked one is cut at the site that issued it.
- Do not touch an unknown token, airdrop or contract that appeared in the
  wallet. Interacting with it can be the attack.

## Leaked secret — containment order

Follow the `secret-handling` skill throughout: never repeat the value, refer to
the credential by its env-var name, show at most a fingerprint. Then work in
this order:

1. **Revoke or rotate first.** Every minute the old value works is exposure.
   Have the owner issue a replacement at the provider, set it under the same
   env-var name, and restart; then revoke the old one at the issuer. The env
   files are the owner's to edit — never yours. If revoking first would break a
   critical service, the owner may rotate in first; the old value still dies
   within this incident, not later.
2. **Scope the exposure.** What the credential could reach (read, write,
   spend, admin), where it leaked (chat, a commit, a public repo, a log, a
   post), whether that place is public, and the exposure window from the first
   leak to the revoke. Read the issuer's access log for that window if you can,
   or ask the owner to. Note any use you cannot attribute.
3. **Purge.** Remove the value from the files, logs, memory entries, notes
   and drafts you control. Rewriting git history is the owner's decision, comes
   only AFTER rotation, and does not reach clones, forks, caches or anyone who
   already read a public push — that is why step 1 comes first.
4. **Prove the old credential is dead.** A successful revoke command only
   says the request was accepted. Proof is a call with the old credential
   being rejected, or the issuer's console showing it revoked. If you cannot
   check without handling the old value, ask the owner to confirm.
5. **Record.** In the timeline: the credential's name and fingerprint, where
   it leaked, the window, what the issuer log showed, each step and its proof,
   and what will stop a repeat (an ignore rule, a pre-commit secret scan).

⚠️ A wallet seed phrase or private key cannot be revoked or rotated. The only
containment is moving the funds to a new wallet the owner creates — an owner
decision, and an urgent one. Tell the owner at once, send nothing more to the
exposed address, and never ask the owner to show you the seed.

## Telling the owner

- `send_message` is your only reply channel. A notice counts as sent only when
  the call returns success; if it fails, record that and try another reachable
  route (e.g. `email`).
- First message: what is broken, who is affected, the severity, what you are
  doing now, what you need from the owner, and when you will update next.
- Then update on each change of state, not on a timer. State only causes you
  observed and recoveries you verified. Say "suspected" when it is a guess.
- For a decision with a few clear options (roll back or hold), use
  `owner_ask(question=…, options=[…])`. An autonomous run cannot wait, so keep the
  safe state until the owner answers.
- Never include a secret value, a full key or a seed phrase in any message,
  even to the owner.

## Closing

Close only when: recovery is observed on the named signal, every temporary
mitigation is removed or listed as a follow-up with an owner, and, for a
leaked secret, the old credential is proven dead. Then write the closing
record with
`read_skill_resource(skill_id="incident-response", resource_path="references/incident-record.md")`
as the template, and send the owner a short summary.
