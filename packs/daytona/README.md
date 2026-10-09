# The daytona pack

A first-party POLYROB pack: Daytona sandboxes as an execution backend for
`run_code` (`CODE_EXEC_BACKEND=daytona`) and the `shell`/`process` tools
(`SHELL_BACKEND=daytona`). It ships inside the `polyrob` distribution; its SDK is
the `daytona` extra:

```bash
pip install 'polyrob[daytona]'
export DAYTONA_API_KEY=...          # optional: DAYTONA_API_URL, DAYTONA_TARGET
SHELL_BACKEND=daytona polyrob
```

One sandbox per session. `DAYTONA_SANDBOX_PERSIST=true` stops (not deletes) the
sandbox at session end and starts it again for the same session. The API key
never enters the sandbox. Files live in the sandbox (`~/workspace`), not in the
local session workspace.
