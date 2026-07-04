---
name: secret-handling
description: Never embed secrets in skills, memory, or written files; reference by env-var name.
license: MIT
metadata:
  polyrob-priority: '4'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"keywords":["secret","api key","credentials","token","password"]}'
  polyrob-version: '1'
---
# Secret Handling

Never embed secrets in skills, memory, or files the agent writes.

## Rules
- No API keys, tokens, passwords, private keys, or connection strings in a SKILL.md, a
  memory entry, or a committed file. They persist and load into future sessions.
- Reference secrets by env-var name (e.g. `${OPENAI_API_KEY}`), never by value.
- If a tool result contains a secret, do not echo it into a skill, summary, or commit.
- If you must record that a credential exists, record only its NAME and where it is configured.
