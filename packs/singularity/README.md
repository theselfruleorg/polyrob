# The singularity pack

A first-party POLYROB pack: Apptainer / SingularityCE instances as an execution
backend for `run_code` (`CODE_EXEC_BACKEND=singularity`) and the `shell`/`process`
tools (`SHELL_BACKEND=singularity`). It drives the CLI; there is no SDK and no
extra.

```bash
SINGULARITY_IMAGE=docker://python:3.12-slim SHELL_BACKEND=singularity polyrob
```

One `--containall --no-home --cleanenv` instance per session, with the session
workspace bound at `/workspace`. The container runs as the invoking user on the
host kernel: a weaker boundary than the docker backend, meant for a single-user
workstation or an HPC host.
