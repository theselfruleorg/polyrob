"""The modal pack (073 W7): Modal sandboxes (gVisor) as an execution and shell backend.

The code half of the pack. ``pack()`` contributes nothing to the tool table;
``exec_backends()`` is the execution-backend seam
(``tools/code_exec/pack_backends.py``) — pulled only while this pack is LOADED,
so the kill list, ``POLYROB_PACKS`` / ``POLYROB_PACKS_DISABLED`` and the custody
rule apply. Importing this module is cheap: the backend module and its SDK load
on first use. Persistence is this pack's concern (see ``backend.py``).
"""
from core.packs.spec import PackSpec


def pack() -> PackSpec:
    return PackSpec(id="modal")


def exec_backends():
    from tools.code_exec.pack_backends import ExecBackendContribution
    return (ExecBackendContribution(
        name="modal",
        backend="polyrob_modal.backend:ModalBackend",
        shell_executor="polyrob_modal.backend:shell_executor",
        summary="Modal sandboxes (gVisor) as an execution and shell backend"),)
