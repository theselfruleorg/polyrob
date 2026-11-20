"""The token-launch preference rows (``launch.*``, proposal 122 W3): one more file, the same
machinery. Built by a call so this module imports nothing from ``core/prefs.py``.

An owner pin and two ceilings, not env flags ("What earns a setting", line 4): the money
guard holds every launch to them (``core/wallet/launch_policy.py``, guard step 7a), and a
bad value refuses every launch rather than lifting the pin.
"""


def build_launch_specs(_spec, SENSITIVITY_GUARDED) -> tuple:
    return (
        _spec("launch.pair", "str", SENSITIVITY_GUARDED, "override", "live",
              default_display="not set (any pair)",
              description="The one pair every token launch pairs to, as <network>:<token "
                          "address> (the native coin is the zero address); any other pair "
                          "refuses. Empty = any pair"),
        _spec("launch.per_day", "int", SENSITIVITY_GUARDED, "override", "live",
              min_value=0, max_value=100, default_display=1,
              description="Token launches in 24 hours (every deploy_token counts); 0 = none"),
        _spec("launch.max_pool_usd", "float", SENSITIVITY_GUARDED, "override", "live",
              min_value=0.0, default_display="not set (the per-transaction limit binds)",
              description="The most one launch puts into its new pool, in USD at the "
                          "guard's price"),
    )
