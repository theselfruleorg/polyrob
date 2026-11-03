"""``polyrob gateway`` launch for Telegram (long polling). See ``surfaces/_launch.py``."""
import os

from surfaces._launch import LaunchContext, Launched


async def launch(ctx: LaunchContext):
    tok = (ctx.options.get("telegram_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not tok:
        ctx.warn("TELEGRAM_SURFACE_ENABLED=true but no token "
                 "(set --telegram-token or TELEGRAM_BOT_TOKEN). Skipping Telegram.")
        return None
    from surfaces.telegram.harness import build_telegram_harness
    h = build_telegram_harness(ctx.container, ctx.task_agent, token=tok,
                               webhook_base=None, data_dir=ctx.data_dir)
    await h.start()

    def _on_signal():
        # 098 TS-17: sticky — a SIGTERM drain before run_polling's first step
        # used to be lost (run_polling set _running back to True).
        h._running, h._stop_requested = False, True

    return Launched(run=h.run_polling, stop=h.stop, on_signal=_on_signal)
