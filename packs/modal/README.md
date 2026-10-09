# The modal pack

A first-party POLYROB pack: Modal Sandboxes (gVisor) as an execution backend for
`run_code` (`CODE_EXEC_BACKEND=modal`) and the `shell`/`process` tools
(`SHELL_BACKEND=modal`). It ships inside the `polyrob` distribution; its SDK is
the `modal` extra:

```bash
pip install 'polyrob[modal]'
export MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=...   # or the SDK's ~/.modal.toml
SHELL_BACKEND=modal polyrob
```

One sandbox per session. The credentials authenticate the client in the agent
process and are never forwarded into the sandbox. `MODAL_SANDBOX_SNAPSHOT=true`
snapshots the sandbox filesystem at session end and resumes the session from it.
Files live in the sandbox, not in the local session workspace.
