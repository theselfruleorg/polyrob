"""The honest "invoicing is off" note every owner seat renders.

Three seats (the REPL, Telegram, the console) each probed
``x402_invoicing_enabled`` and wrote their own sentence — three wordings of
one fact, and three different answers to a probe error. ONE fact, ONE
sentence, ONE rule: a resolver error counts as OFF (fail-to-warn, the same
rule ``cli/_flag_warn.py::warn_if_flag_off`` applies), because a note that
goes quiet exactly when the probe breaks is the confident-silent failure the
note exists to prevent.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

OFF_TEXT = ("X402_INVOICE_ENABLED is off — rows are durable, but no settlement "
            "watcher runs; pending invoices will not settle or wake sessions.")
REMEDY = ("Enable: `polyrob config set X402_INVOICE_ENABLED true --global` "
          "(takes effect: restart).")


def invoicing_off_note(*, remedy: bool = False) -> str:
    """``""`` while invoicing is enabled, else the note (+ the CLI remedy when
    *remedy*). Never raises."""
    try:
        from modules.x402.invoicing import x402_invoicing_enabled
        if x402_invoicing_enabled():
            return ""
    except Exception:
        logger.debug("invoicing enablement probe failed — reporting OFF", exc_info=True)
    return f"{OFF_TEXT} {REMEDY}" if remedy else OFF_TEXT
