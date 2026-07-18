"""``hf_deploy`` — publish the session workspace as a Hugging Face Space
(proposal 2026-07-10, §3). Off by default; never in default ``tool_ids``.

``HF_DEPLOY_ENABLED`` is deliberately NOT a member of the ``POLYROB_LOCAL``
safe-flags group (``agents/task/constants.py::_SAFE_LOCAL_FLAGS``) — publishing
to a PUBLIC Hugging Face Space is not a safe local default the way e.g.
``CODING_TOOLS_ENABLED`` is; an operator opts in explicitly.
"""
import os

from core.env import bool_env as _bool_env
from core.env import int_env as _int_env


def hf_deploy_enabled() -> bool:
    """Register the ``hf_deploy`` tool. Default OFF; NOT flipped by POLYROB_LOCAL."""
    return _bool_env("HF_DEPLOY_ENABLED", False)


def hf_deploy_daily_max() -> int:
    """Max deploy ATTEMPTS per tenant per rolling 24h (default 10)."""
    return _int_env("HF_DEPLOY_DAILY_MAX", 10)


def hf_deploy_min_interval_sec() -> int:
    """Minimum seconds between deploy attempts of the SAME app (default 120)."""
    return _int_env("HF_DEPLOY_MIN_INTERVAL_SEC", 120)


def register_hf_deploy_tool(force: bool = False) -> bool:
    """Register the 'hf_deploy' descriptor + class IFF ``HF_DEPLOY_ENABLED``
    (or forced). Mirrors ``tools.cronjob_tools.register_cronjob_tool``.

    Returns True when registered. No-op (returns False) when the flag is off,
    so flag-off => ``get_tool_class('hf_deploy')`` is None. Never in the
    default ``tool_ids`` — agents (and goal/cron runs, at posture>=2) opt in.
    """
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    from tools.hf_deploy.tool import HFDeployTool

    return register_optional_tool(
        "hf_deploy",
        HFDeployTool,
        ToolDescriptor(
            name="hf_deploy",
            description="Publish the session workspace as a Hugging Face Space "
                       "(deploy/undeploy/list_deployments). First publish of a new "
                       "app needs an approving provider (interactive default, denied "
                       "headless); an approved app redeploys unattended within caps.",
            category=ToolCategory.INTEGRATION,
            required_config=[],
            init_priority=83,
            is_optional=True,
        ),
        hf_deploy_enabled,
        force=force,
    )


__all__ = [
    "hf_deploy_enabled", "hf_deploy_daily_max", "hf_deploy_min_interval_sec",
    "register_hf_deploy_tool",
]
