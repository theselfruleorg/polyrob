"""Session-metadata / construction-helper mixin (roadmap P9; code-motion from service.py).

Initial-action conversion, session-metadata updates, and version/source detection.
Moved verbatim off the ``Agent`` god-file; ``Agent`` composes
``SessionMetadataMixin`` (call sites unchanged via MRO).
"""
from __future__ import annotations

from typing import Any, Dict, List

from agents.task.agent.views import ActionModel, AgentError


class SessionMetadataMixin:
    """Initial-action conversion + session metadata + version/source for Agent."""

    def _convert_initial_actions(self, actions: List[Dict[str, Dict[str, Any]]]) -> List[ActionModel]:
        """Convert dictionary-based actions to ActionModel instances"""
        converted_actions = []

        for action_dict in actions:
            # Each action_dict should have a single key-value pair
            action_name = next(iter(action_dict))
            params = action_dict[action_name]

            # Get the parameter model for this action from Controller
            # Use Controller's high-level API instead of directly accessing registry
            action_info = self.controller.get_action_details(action_name)
            if not action_info:
                raise AgentError(f"Action {action_name} not found in controller")
            param_model = action_info.param_model

            # Create validated parameters using the appropriate param model
            validated_params = param_model(**params)

            # Create ActionModel instance with the validated parameters
            action_instance = self.ActionModel(**{action_name: validated_params})
            converted_actions.append(action_instance)

        return converted_actions

    def _update_session_metadata(self, metadata_update: Dict[str, Any]) -> None:
        """Update session metadata through SessionManager with proper error handling.

        Args:
            metadata_update: Dictionary with metadata to update
        """
        if not hasattr(self, 'session_id') or not self.session_id:
            return

        try:
            self.session_manager.update_session_metadata(self.session_id, metadata_update)
        except Exception as e:
            self.logger.debug(f"Could not update session metadata: {e}")

    def _set_version_and_source(self) -> None:
        """Set version and source attributes with graceful error handling."""
        # Use safe_operation from utils
        from agents.task.utils import safe_operation

        def get_version_info():
            # Use modern packaging instead of deprecated pkg_resources
            try:
                from importlib.metadata import version as get_version, PackageNotFoundError
            except ImportError:
                # Fallback for Python < 3.8
                from importlib_metadata import version as get_version, PackageNotFoundError

            try:
                version = get_version('browser-use')
                source = 'pip'
            except PackageNotFoundError:
                try:
                    # Try to get rob package version if browser-use is not available
                    version = get_version('rob')
                    source = 'rob'
                except PackageNotFoundError:
                    # Try git version (only if we're in a git repository)
                    try:
                        import subprocess
                        import os
                        # Check if we're in a git repository first
                        if os.path.exists('.git') or subprocess.run(['git', 'rev-parse', '--git-dir'],
                            capture_output=True, stderr=subprocess.DEVNULL).returncode == 0:
                            version = subprocess.check_output(['git', 'describe', '--tags'],
                                stderr=subprocess.DEVNULL).decode('utf-8').strip()
                            source = 'git'
                        else:
                            version = 'development'
                            source = 'local'
                    except Exception:
                        version = 'development'
                        source = 'local'
            return version, source

        version_info = safe_operation(
            get_version_info,
            self.logger,
            "Failed to determine version information",
            default_value=('unknown', 'unknown')
        )

        self.version, self.source = version_info
        self.logger.debug(f'Version: {self.version}, Source: {self.source}')
