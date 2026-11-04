---
name: secret-handling
description: Never embed or echo secrets; reference by env-var name, show only a fingerprint, and tell the owner to rotate a pasted secret.
license: MIT
metadata:
  polyrob-when: 'A key, password or token shows up in the work'
  polyrob-priority: '4'
  polyrob-version: '3'
---
# Secret Handling

Never embed secrets in skills, memory, or files the agent writes, and never
show one back.

## Rules
- No API keys, tokens, passwords, private keys, seed phrases, or connection
  strings in a SKILL.md, a memory entry, a message, or a committed file. They
  persist and load into future sessions.
- Reference secrets by env-var name (e.g. `${OPENAI_API_KEY}`), never by value.
- **Never echo a secret.** Not in a reply, a summary, a log line, a commit, or
  a tool argument that is shown to anyone. This includes a secret that a tool
  result or a web page shows you.
- **To tell two credentials apart, show only a fingerprint** — the first and
  last 4 characters (`sk-a…9f2c`), the same shape the status views print. A
  short value is shown as `********`.
- **If the owner pastes a secret into chat, tell the owner to rotate it.** The
  chat transcript, the surface's servers and any log now hold it. Do not repeat
  it, do not store it, and say where to set the new value (the env-var name) —
  the env file is the owner's to edit, never yours.
- **A key the owner gives for one API call goes only into that call's header**
  (`http_request(headers_json=…)`, see `api-integration`): never into a file, a
  goal, a job, memory or a message. A tool argument has no stored-secret reference,
  so the key must be given again for a later call.
- If you must record that a credential exists, record only its NAME, where it
  is configured, and at most its fingerprint.
