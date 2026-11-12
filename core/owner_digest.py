"""The owner digest's on/off switch — ONE reader (116 W5.9).

The pref (``digest.enabled``, per tenant) wins over the ``OWNER_DIGEST_ENABLED``
env default. The cron runner and the status health item both read it here: the
health item read the env flag alone, so an owner who turned the digest off in
his prefs still got "no digest job is scheduled" on every status.
"""
from typing import Optional


def digest_enabled_for(user_id: Optional[str], home_dir: Optional[str]) -> bool:
    """Owner's digest on/off switch: pref (override, spec ``digest.enabled``)
    over the ``OWNER_DIGEST_ENABLED`` env default. No pref file present =>
    byte-identical to ``AutonomyConfig.owner_digest_enabled()`` (owner-UX P1 T4)."""
    from core import prefs
    from core.config_policy import AutonomyConfig
    env_value = AutonomyConfig.owner_digest_enabled()
    return prefs.resolve("digest.enabled", user_id, home_dir,
                         env_value=env_value, default=env_value)
