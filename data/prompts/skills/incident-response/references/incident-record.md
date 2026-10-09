# Incident record

The working file for one incident. Create it in the session workspace (e.g.
`incident-<yyyymmdd-hhmm>.md`) at the first observation and append to it as
you go. It is the source for every owner update and for the closing summary.

## Severity

Severity is a value set at a time, from an observation. Each change is a new
`decision` entry; never overwrite the old level.

| Level | What makes it this level | What follows |
|---|---|---|
| S1 | A core flow is down or wrong for most users; data is being lost or corrupted; money is moving without approval; a credential with spend or admin rights is exposed | Tell the owner now; stabilise before anything else; updates on every state change |
| S2 | A core flow is degraded, or down for a bounded group; a credential with limited rights is exposed | Tell the owner promptly; stabilise; updates on state change |
| S3 | A non-core flow is degraded, or a core flow has a working workaround | Fix in normal working order; one summary at the end unless the owner asks for more |

An unknown reach is declared at the higher level, then lowered by a new entry
that names the measurement.

## Timeline entries

Three kinds only. Times in UTC.

| Kind | Records | Fields |
|---|---|---|
| `seen` | something observed: a metric, an error, a balance, a user report | time, who saw it, source, the value or a short quote |
| `changed` | something done: a rollback, a flag, a pause, a restart, a revoke | time, who did it, what, where, `temporary` or `permanent`, what removes it |
| `decided` | a choice: a severity change, a mitigation picked, a handover | time, who decided, the choice, the reason as believed then |

A correction is a new entry of the same kind that names the entry it corrects
and says what was wrong. Do not edit, delete or reorder entries: the point of
the timeline is what was believed when.

Example lines:

```
14:02 seen     agent   telegram replies failing: 12/12 sends error 401 since 13:55
14:03 decided  agent   S2 — owner channel down, no money at risk (balance unchanged)
14:04 changed  agent   none yet; told owner via email (send_message to telegram failed)
14:20 changed  owner   rotated TELEGRAM_BOT_TOKEN, restarted — permanent
14:23 seen     agent   test send ok; 5/5 replies delivered 14:21-14:23
```

## Mitigation list

| What changed | Where | Temporary? | What removes it | Owner |
|---|---|---|---|---|

## Recovery check

- Signal:
- Healthy value (number, unit, window):
- Observed value and time:
- Observed by:

## Leaked-secret checklist

- [ ] Credential name and fingerprint (never the value)
- [ ] What it could reach (read / write / spend / admin)
- [ ] Where it leaked; public or private; first exposure time
- [ ] Replacement issued and in use (owner set it, service restarted)
- [ ] Old credential revoked at the issuer
- [ ] Issuer access log read for the exposure window; unexplained use noted
- [ ] Value removed from files, logs, memory, notes and drafts you control
- [ ] History rewrite decided by the owner (only after rotation), or declined
- [ ] Old credential proven rejected (a refused call, or the issuer's console)
- [ ] Prevention added (ignore rule, pre-commit secret scan) or listed as follow-up

## Closing summary (for the owner)

Keep it short:

1. What broke, for whom, and for how long.
2. Severity and the observation that set it.
3. What stopped it (the mitigation) and what fixed it, if anything did yet.
4. The recovery signal and its observed value.
5. Open follow-ups: each temporary mitigation still in place, each unproven
   step, each prevention item — with who owns it.
6. What is still unknown. Say so plainly rather than guessing a cause.
