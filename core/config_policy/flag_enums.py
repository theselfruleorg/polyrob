"""Valid-value tables for enum-shaped env flags (026 P1.4).

Failure mode G: ``shape_of_default`` knows only bool/numeric/free, so
``polyrob config set AUTONOMY_MODE autonmous`` wrote cleanly and the resolver
degraded the typo to ``supervised`` — no error at either end. This table is
the SSOT the shape validators consult; the mode/posture tuples come straight
from the policy resolvers so the two can never disagree.

Values are compared case-insensitively at the validation sites (the resolvers
themselves lower()/upper() their input). Extensible: add a row when a new
enum-shaped flag lands — a flag absent here simply keeps its bool/numeric/free
shape check.
"""
from core.config_policy.policy import _AUTONOMY_MODES, _AUTONOMY_POSTURES

FLAG_ENUMS: "dict[str, tuple[str, ...]]" = {
    "AUTONOMY_MODE": tuple(_AUTONOMY_MODES),
    "AUTONOMY_POSTURE": tuple(_AUTONOMY_POSTURES),
    # 0-3 compute ladder — only the literal values; garbage degrades CLOSED to
    # 0 at the resolver, so writing it must be caught here instead.
    "AGENT_COMPUTE_POSTURE": ("0", "1", "2", "3"),
    "PAYMENT_APPROVAL_MODE": ("approve", "auto"),
    # core/surfaces/outbound_policy.py (same set as the outbound.policy pref).
    "OUTBOUND_POLICY": ("open", "domains", "allowlist", "off"),
    # modules/memory/backend_factory.py ('' also means off — unset instead).
    "MEMORY_BACKEND": ("sqlite", "local_vector", "none", "off"),
    # tools/code_exec — registry names.
    "CODE_EXEC_BACKEND": ("local_subprocess", "docker"),
    # tools/controller/registry/schema_generators.py (upper()'d on read).
    "TOOL_SCHEMA_ERROR_POLICY": ("DROP_TOOL", "RAISE", "WARN"),
}


def flag_enum_values(key: str):
    """The valid values for an enum-shaped flag, or None when *key* is not one."""
    return FLAG_ENUMS.get(key)


def enum_error(key: str, value) -> "str | None":
    """Validation-error string for KEY=VALUE, or None when the value is valid
    (or the key is not enum-shaped). Case-insensitive."""
    values = FLAG_ENUMS.get(key)
    if values is None:
        return None
    raw = str(value).strip()
    if not raw:
        # A blank value is the universal unset/disable idiom (MEMORY_BACKEND=
        # means off) — never an enum violation.
        return None
    if raw.lower() in {v.lower() for v in values}:
        return None
    return f"{key} expects one of: {', '.join(values)}; got {value!r}"
