"""Console setting metadata from the config control plane, never a second policy."""
from core import config_service
from core.flags import REGISTRY
from core.config_policy.flag_enums import flag_enum_values
from webview import webgate
from webview.copy import t


def flag_metadata(key: str) -> dict:
    """Effective value and permission, including posture and read-only refusal."""
    if key not in REGISTRY:
        return {"console_writable": False, "write_reason": t("agent.control_pattern")}
    info = config_service.describe(key)
    allowed = not config_service.is_console_unwritable(key)
    reason = ""
    if not allowed:
        reason = t("agent.control_operator")
    elif webgate.posture() not in ("local", "own_ops"):
        allowed, reason = False, t("agent.control_owner")
    elif webgate.read_only():
        allowed, reason = False, t("agent.control_readonly")
    return {"key": key, "type": info.kind, "value": info.effective,
            "source": info.source, "applies": info.applies,
            "enum_values": sorted(flag_enum_values(key) or ()),
            "console_writable": allowed, "write_reason": reason}
