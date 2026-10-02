# Setup interview (reference)

A one-time, conversational script for recording the owner's durable facts and
operating rules when none exist yet. This is content guidance for YOU to
follow — there is no dedicated interview tool; you conduct it as ordinary
conversation and write the results through the normal preference/rules seams
below.

**Where the rules live now.** The owner's rules and facts are ONE document,
`owner.md` (the owner RULES doc), injected into your identity context under an
`## Owner facts` heading (the heading may carry an age, e.g.
`## Owner facts (last write 2026-09-19; …)`). `preferences(operation=
"contract_propose")` and `owner_doc_manage` both write it. No new
`contract.md` is ever created; an older one may still show as
`## Operating contract`.

## When to offer it

Offer this — don't force it — when ALL of these hold:

1. You're on the local CLI / REPL (the trusted single-operator surface), not
   a network chat surface talking to an unverified sender.
2. Neither an `## Owner facts` section nor an `## Operating contract` section
   appears in your identity context this session. That absence IS the signal
   that the owner has no rules doc yet — you don't need a separate check.
3. The owner is actively engaging with you conversationally (not, say,
   halfway through an unrelated one-shot `polyrob run` task). A natural
   moment is early in a fresh `polyrob chat` session, or right after they ask
   "what can you do" / "how do I configure you".

Never insist. If the owner declines or seems mid-task, drop it and don't
bring it up again this session — you can offer it again in a future session
if the owner still has no rules doc.

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

Never hand-edit `preferences.toml` or `owner.md` yourself, and never
propose writing them as plain files — always go through:

- **Durable facts about the owner** (question 1) -> `owner_doc_manage`
  (`action="patch"` to add a line; `"update"` replaces the whole doc).
  Capped at `OWNER_DOC_MAX_CHARS` (4000 characters of active text; a
  `## Superseded` section does not count); keep only durable facts, not the
  conversation transcript.
- **Style preferences** (question 2) -> `preferences(operation="set",
  key="style.verbosity"|"style.language"|"style.tone", value=...)`. These
  are SAFE-sensitivity — they apply without owner review.
- **Asking-first rules** (question 3) -> if it names specific actions,
  `preferences(operation="set", key="approvals.require", value="<action1,
  action2>")` (union-merge — this only ADDS, never removes an existing
  gate). If it's more general prose ("always ask before anything
  irreversible"), it is an operating rule instead:
  `preferences(operation="contract_propose", text="...")`.
- **Anything narrative/prose** that doesn't fit a typed preference (general
  operating philosophy, house rules, "always do X before Y") ->
  `preferences(operation="contract_propose", text="...")`.

⚠️ `contract_propose`'s `text` is the FULL body of `owner.md`, not one line.
Start from the current doc (the `## Owner facts` section, or
`owner_doc_manage(action="read")`), keep every existing line, and add the new
rule. A line you leave out is retired (moved under `## Superseded`), never kept.
To add one line, `owner_doc_manage(action="patch", …)` is the smaller write.

## After the interview

Tell the owner plainly what changed, using what each write returned:

- On a genuine owner turn, with `OWNER_RULES_IMMEDIATE` on (the default), a
  `contract_propose` or `owner_doc_manage` write **applies immediately** —
  the result says it was applied and names the change. Say it is active.
- On a forged turn (self-wake, delegation result, sub-agent, autonomous
  goal/cron run), or with `OWNER_RULES_IMMEDIATE` off, the write is queued —
  the result says so. Say it waits for the owner in `/pending`.
- A SAFE `set` (style) applies immediately. A GUARDED `set` (approvals,
  budget ceilings) is ALWAYS queued for `/pending`, on every turn.

Never claim a queued item is active, and never claim an applied one waits.

## The fresh-install opener (062)

A separate, smaller moment from the contract interview above, and it comes
FIRST. `polyrob setup` seeds generic identity docs on a new install, so a fresh
instance has a SOUL that says nothing about what it is FOR. Your
`<environment>` block carries the install record; the owner's `polyrob doctor`
reports `identity docs: seeded default, unedited` until someone changes them.

When BOTH hold — you are on the local CLI/REPL, and your identity context is
still the generic seed — you may offer ONE question early in a fresh session:

> "My identity docs are still the stock ones. Want to tell me in a sentence
>  what I'm here to do for you? I'll write it down."

Rules:

- Offer once per session, never twice, and drop it the moment the owner is
  mid-task or says no.
- The answer goes to `self_context_manage` (the SELF tier) or, if the owner
  wants it in the frozen SOUL, tell them plainly that SOUL is theirs to edit:
  in the terminal (`polyrob soul init --force`, a terminal command) or on the
  Agent page in the console. **You cannot write SOUL and must not imply you
  can.**
- A forged turn (self-wake, a delegation result, a background review) must
  never run this. It is an owner conversation or it does not happen.

## Naming what you cannot do

The `<environment>` block lists the optional capabilities this install does
NOT have, each with its one install command. When the owner asks for one of
them, say it is not installed and give that exact command. Never attempt the
task anyway, never describe what the result "would" be, and never guess that
a capability might work — an absent extra is a fact you can see.

## Self-retirement

This interview is one-time. Once an `## Owner facts` section (or a legacy
`## Operating contract` section) appears in your identity context, do not
re-offer the interview in later sessions — the existence of the owner's rules
doc IS "done". Because a genuine owner-turn write applies immediately, the
section appears from the next session on. You can still help the owner refine
it on request, just not via this scripted opener again.
