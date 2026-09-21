# Proof per rail

<!-- GENERATED from core/rails/verification.py — do not edit by hand.
     Regenerate: python -c "import pathlib, core.rails.verification as v; pathlib.Path('docs/guide/rails-verification.md').write_text(v.render_guide())" -->

"Did that land?" has a different answer on every rail, and getting it
wrong costs a turn in both directions. An agent that re-reads a Telegram
channel to confirm its own post will never find it — Telegram does not
deliver a bot's own channel messages back — and will wrongly report a
delivered post as unconfirmed. An agent that treats a transaction hash as
a result will wrongly report a no-op as done.

This page is generated from the one table in the code
(`core/rails/verification.py`), which is also what every tool result's
`proof:` line is rendered from. There is no second copy to disagree with.

**What this table states is what CAN be proved, never that anything WAS.**
A rail whose proof is the receipt still needs the receipt.

## X — your own post (`x_post`)

- **Proof:** the status URL (or post id) the write call returned
- **How to check:** open the returned status URL; it is a public page and it is readable by the same browser session that posted it
- **Watch out:** a saved draft, a plan, or 'ready to post' is NOT a post — only the returned URL/id proves publication

## X — a reply under someone else's post (`x_reply`)

- **Proof:** the status URL (or post id) the reply call returned
- **How to check:** open the returned status URL; the reply is a status of its own
- **Watch out:** the API tier returns 403 for a non-mentioner, so a reply that did not come back with a URL did not happen — the browser rail is the one that can post it

## Telegram — a channel you post to (`telegram_channel`)

- **Proof:** the send receipt (API 200 + message_id) IS the verification
- **How to check:** read the message_id off the send result — there is nothing else to read
- **Watch out:** the bot cannot read its own channel posts: Telegram never delivers them back as updates, so an empty room ledger says NOTHING about whether your post rendered. Do not call a delivered post unconfirmed because you cannot find it

## Telegram — a group/room (`telegram_group`)

- **Proof:** the send receipt (API 200 + message_id), confirmable by a room-ledger read-back
- **How to check:** `room_read(room=<chat_id>)` — the harness appends every allowlisted-room line to the local ledger
- **Watch out:** the ledger holds what OTHERS wrote and what the harness captured; a missing line is a capture gap or the retention window, never on its own proof that the send failed

## Email (`email`)

- **Proof:** the SMTP 250 (or provider accept) plus the Message-ID stamped on the sent mail
- **How to check:** the send result carries the Message-ID; a reply arrives with it in In-Reply-To
- **Watch out:** delivery to the server is not delivery to a human — a 250 proves the mail was accepted for delivery, never that it was read or that it escaped a spam filter

## On-chain transaction (`onchain`)

- **Proof:** a confirmed receipt AND the state read back afterwards
- **How to check:** the existing `tx_guard` rule: assert the simulated deltas, broadcast, then read the resulting state (balance, owner, tokenId) back
- **Watch out:** a transaction hash is not a result: a receipt can succeed while the call did nothing. Only the read-back proves the effect

## When you cannot prove it

Say so precisely. `OUTCOME: BLOCKED — <exactly what you need>` is a real
result; "unconfirmed" over a rail whose receipt IS the proof is not.
