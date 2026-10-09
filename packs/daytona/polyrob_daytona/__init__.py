"""The daytona pack (073 W7): Daytona sandboxes as an execution and shell backend, with stop and resume.

The code half of the pack. ``pack()`` contributes nothing to the tool table;
``exec_backends()`` is the execution-backend seam
(``tools/code_exec/pack_backends.py``) — pulled only while this pack is LOADED,
so the kill list, ``POLYROB_PACKS`` / ``POLYROB_PACKS_DISABLED`` and the custody
rule apply. Importing this module is cheap: the backend module and its SDK load
on first use. Persistence is this pack's concern (see ``backend.py``).
"""
from core.packs.spec import PackSpec


def pack() -> PackSpec:
    return PackSpec(id="daytona")


def exec_backends():
    from tools.code_exec.pack_backends import ExecBackendContribution
    return (ExecBackendContribution(
        name="daytona",
        backend="polyrob_daytona.backend:DaytonaBackend",
        shell_executor="polyrob_daytona.backend:shell_executor",
        summary="Daytona sandboxes as an execution and shell backend, with stop and resume"),)
