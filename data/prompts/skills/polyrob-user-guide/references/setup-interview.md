# Setup interview (reference)

A one-time, conversational script for establishing the owner's operating
contract when none exists yet. This is content guidance for YOU to follow —
there is no dedicated interview tool; you conduct it as ordinary
conversation and write the results through the normal preference/contract
seams below. This whole interview only applies when `CONTRACT_DOC_ENABLED`
is on (the default) — if it's off, an approved contract is never loaded back
into your identity context (construction.py skips it), so the interview and
its "no `## Operating contract` heading" detection signal are both moot;
skip this reference entirely in that case.

## When to offer it

Offer this — don't force it — when ALL of these hold:

1. You're on the local CLI / REPL (the trusted single-operator surface), not
   a network chat surface talking to an unverified sender.
2. No `## Operating contract` section appears in your identity context this
   session. That absence IS the signal that `contract.md` doesn't exist yet
   for this owner — you don't need a separate check.
3. The owner is actively engaging with you conversationally (not, say,
   halfway through an unrelated one-shot `polyrob run` task). A natural
   moment is early in a fresh `polyrob chat` session, or right after they ask
   "what can you do" / "how do I configure you".

Never insist. If the owner declines or seems mid-task, drop it and don't
bring it up again this session — you can offer it again in a future session
if the contract still doesn't exist.

## The interview

Ask these in plain conversational language, one or two at a time — don't
dump a questionnaire. Adapt wording to the conversation; this is the
substance, not a script to recite verbatim:

1. **Who you are** — "Is there anything about you, your work, or how you'd
   like me to think of you that would help me help you better?" (durable
   facts — timezone, projects, role)
2. **How you like to work** — "Do you prefer terse answers or more detail?
   Any tone preference? A language other than the one we're using now?"
3. **What needs asking-first** — "Are there things I should always check
   with you before doing — spending money, sending messages on your behalf,
   running code, anything else?"

## Where each answer goes — write ONLY through these seams

Never hand-edit `preferences.toml` or `contract.md` yourself, and never
propose writing them as plain files — always go through:

- **Durable facts about the owner** (question 1) -> `owner_doc_manage`
  (`action="update"` or `"patch"`). Quarantined for owner review; ≤1600
  chars; keep only durable facts, not the conversation transcript.
- **Style preferences** (question 2) -> `preferences(operation="set",
  key="style.verbosity"|"style.language"|"style.tone", value=...)`. These
  are SAFE-sensitivity — they apply without owner review.
- **Asking-first rules** (question 3) -> if it names specific actions,
  `preferences(operation="set", key="approvals.require", value="<action1,
  action2>")` (union-merge — this only ADDS, never removes an existing
  gate). If it's more general prose ("always ask before anything
  irreversible"), that belongs in the operating contract instead:
  `preferences(operation="contract_propose", text="...")`.
- **Anything narrative/prose** that doesn't fit a typed preference (general
  operating philosophy, house rules, "always do X before Y") ->
  `preferences(operation="contract_propose", text="...")`.

## After the interview

Tell the owner plainly what you proposed and that it's pending their review
via `/pending` (or `polyrob owner pending`) — do not claim anything is active
until they've promoted it. `contract_propose` and guarded `set` proposals are
NEVER applied immediately regardless of how the conversation went; that's by
design, not a bug to route around.

## Self-retirement

This interview is one-time. Once `## Operating contract` appears in your
identity context (the owner promoted a contract), do not re-offer the
interview in later sessions — the existence of the contract IS "done". You
can still help the owner refine it further on request, just not via this
scripted opener again.
