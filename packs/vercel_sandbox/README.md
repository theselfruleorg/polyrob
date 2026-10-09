# The vercel_sandbox pack

A first-party POLYROB pack: Vercel Sandbox microVMs as an execution backend for
`run_code` (`CODE_EXEC_BACKEND=vercel_sandbox`) and the `shell`/`process` tools
(`SHELL_BACKEND=vercel_sandbox`). It ships inside the `polyrob` distribution; its
SDK is the `vercel-sandbox` extra:

```bash
pip install 'polyrob[vercel-sandbox]'
export VERCEL_TOKEN=... VERCEL_TEAM_ID=... VERCEL_PROJECT_ID=...
SHELL_BACKEND=vercel_sandbox polyrob
```

One sandbox per session, ephemeral by the provider's design (no resume). Vercel
gives no per-sandbox network deny, so the sandbox always has network. The token
never enters the sandbox.
