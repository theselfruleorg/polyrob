"""Map a recorded x402 payment onto the shared transaction notification rail."""
import logging


def notify_payment(tool, execution_context, result, amount_usd):
    try:
        from core.instance import resolve_owner_principal
        from core.wallet import tx_notify
        uid = getattr(execution_context, "user_id", None) or resolve_owner_principal()
        tx_notify.notify_soon(
            getattr(tool, "_container", None), uid,
            tx_notify.TxNotice(
                verb="x402 payment", route="x402", usd=amount_usd,
                tx_ref=result.tx_hash, chain=None,
                state="confirmed" if result.tx_hash else "unconfirmed",
                ledger_recorded=True),
            settled=bool(result.tx_hash),
            session_id=getattr(execution_context, "session_id", None))
    except Exception:
        logging.getLogger(__name__).warning("x402 payment notice could not be queued", exc_info=True)
