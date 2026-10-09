"""Reserve before inference, settle once using the existing credit transaction ledger.

An interrupted or unmetered call keeps its hold. Never refund an unknown provider
outcome merely because a timer expired. Reconciliation must establish its usage.
"""
import asyncio
import math
import uuid

PREFIX = "llm-reserve:"


def reservation_credits(model: str, provider: str) -> int:
    """Conservative full-context bound; no tokenizer or image-size assumptions.

    This deliberately reserves more than an ordinary short call costs. The unused
    portion returns at settlement. Explicitly priced models and the maximum cache
    write rate are required, including reasoning output and multimodal context.
    """
    from modules.credits.pricing import PricingConfig
    from modules.llm.model_registry import get_model_config_exact
    from modules.llm.provider_spec import is_flat_rate
    from modules.credits.balance_manager import validate_credit_amount
    if is_flat_rate(provider):
        amount = max(1, PricingConfig.MIN_CREDIT_CHARGE)
    else:
        config = get_model_config_exact(model)
        if config is None:
            raise ValueError("Credit reservation requires an explicitly priced model")
        p = config.pricing
        rates = [p.input_price, p.output_price, p.cached_input_price,
                 p.cache_write_price, p.cache_write_price_1h]
        if any(not math.isfinite(float(r)) or float(r) < 0 for r in rates if r is not None):
            raise ValueError("Invalid model pricing for credit reservation")
        context, output = config.context_window, config.max_completion_tokens
        if any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in (context, output)):
            raise ValueError("Credit reservation requires bounded model context and output")
        input_rate = max(float(r) for r in (p.input_price, p.cached_input_price,
                          p.cache_write_price, p.cache_write_price_1h) if r is not None)
        cost = (context * input_rate + output * float(p.output_price)) / 1_000_000
        amount, _ = PricingConfig.calculate_credits_from_api_cost(cost)
        amount = max(1, amount)
    validate_credit_amount(amount)
    return amount


def credit_reserver(balance, user_id: str, session_id=None):
    """Bind the authenticated payer and DB loop, including worker-thread aux calls."""
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("Credit reservation requires an authenticated payer")
    loop = asyncio.get_running_loop()

    async def reserve_here(model, provider):
        from core.exceptions import InsufficientCreditsError
        amount = reservation_credits(model, provider)
        request_id = PREFIX + uuid.uuid4().hex
        if not await balance.has_sufficient_balance(user_id, amount) or not await balance.deduct_credits(
                user_id, amount, request_id, session_id, transaction_type="reservation"):
            available = (await balance.get_balance(user_id))["balance"]
            raise InsufficientCreditsError(user_id, amount, available)
        return request_id

    async def reserve(model, provider):
        if asyncio.get_running_loop() is loop:
            return await reserve_here(model, provider)
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
            reserve_here(model, provider), loop))
    return reserve


async def settle_reservation(balance, user_id: str, request_id: str, amount: int) -> bool:
    """Atomic, durable, payer-bound settlement. False leaves an unresolved hold.

    The original hold remains an audit row; a release row returns only the unused
    balance without treating it as new income. Duplicate settlement is a no-op.
    """
    from modules.credits.balance_manager import validate_credit_amount
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("Invalid settlement amount")
    if amount:
        validate_credit_amount(amount)
    if not request_id.startswith(PREFIX):
        raise ValueError("Invalid credit reservation reference")
    db = balance.db
    await db.connection.begin_transaction()
    try:
        row = await db.fetch_one(
            "SELECT id, amount, transaction_type FROM credit_transactions "
            "WHERE user_id = ? AND reason = ? AND transaction_type IN ('reservation', 'settled_reservation')",
            (user_id, request_id))
        if row is None:
            await db.connection.rollback()
            return False
        if row['transaction_type'] == 'settled_reservation':
            settled = await db.fetch_one(
                "SELECT amount FROM credit_transactions WHERE user_id = ? AND reason = ? "
                "AND transaction_type = 'usage'", (user_id, request_id + ':settled'))
            await db.connection.commit()
            return settled is not None and -settled['amount'] == amount
        held = -row['amount']
        refund = held - amount
        # If prices changed or a provider reports a larger charge, collect the
        # difference only if it is covered. Otherwise preserve the hold and debt.
        cursor = await db.execute(
            "UPDATE user_credits SET balance = balance + ?, lifetime_spent = lifetime_spent - ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND balance + ? >= 0",
            (refund, refund, user_id, refund))
        if cursor.rowcount != 1:
            await db.connection.rollback()
            return False
        after = (await balance.get_balance(user_id))['balance']
        await db.execute("UPDATE credit_transactions SET transaction_type = 'settled_reservation' "
                         "WHERE id = ? AND transaction_type = 'reservation'", (row['id'],))
        # Release the original hold and charge actual usage in this transaction.
        # Amounts sum to the real balance delta and usage reports stay accurate.
        before = after - refund
        for delta, kind, suffix, start, end in (
            (held, 'reservation_release', ':release', before, before + held),
            (-amount, 'usage', ':settled', before + held, after),
        ):
            await db.execute(
                "INSERT INTO credit_transactions (user_id, amount, transaction_type, reason, "
                "session_id, balance_before, balance_after) "
                "SELECT ?, ?, ?, ?, session_id, ?, ? FROM credit_transactions WHERE id = ?",
                (user_id, delta, kind, request_id + suffix, start, end, row['id']))
        await db.connection.commit()
        return True
    except (Exception, asyncio.CancelledError):
        await db.connection.rollback()
        raise
