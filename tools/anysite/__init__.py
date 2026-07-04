from core.env import bool_env as _bool_env


def anysite_cli_enabled() -> bool:
    """The CLI-backed anysite tool is available when not explicitly disabled.

    Default ON (the CLI is the supported AnySite path); the tool itself fails
    soft at runtime if the binary/key are missing, so default-on is safe.
    """
    import os
    raw = os.getenv("ANYSITE_TOOL_ENABLED")
    if raw is not None:
        return _bool_env("ANYSITE_TOOL_ENABLED", True)
    return True


__all__ = ["anysite_cli_enabled"]
