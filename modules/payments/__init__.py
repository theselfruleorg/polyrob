"""Payment system modules for crypto deposits and treasury management.

⚠️ Lazy on purpose. ``modules.database.auth_tables`` imports ``sweep_state`` from
this package while it creates the tables, so an eager import here made
``eth_account`` a requirement of every database start. A base install has no
crypto extra: ``migrations.migrate upgrade`` raised, so ``polyrob update --apply``
rolled back on every such install.
"""

__all__ = [
    'DepositWalletGenerator',
    'DepositMonitor',
    'TreasurySweeper'
]

_HOMES = {
    'DepositWalletGenerator': '.wallet_generator',
    'DepositMonitor': '.deposit_monitor',
    'TreasurySweeper': '.treasury_sweeper',
}


def __getattr__(name):
    home = _HOMES.get(name)
    if home is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(home, __name__), name)
