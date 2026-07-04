"""Step-telemetry mixin (roadmap P9; code-motion from step.py).

The best-effort per-step + iteration-complete telemetry emission, split out of the
StepMixin so step.py stays focused on the step phases. Agent composes
StepTelemetryMixin; the only caller is _finalize_step (via MRO).
"""
from __future__ import annotations


def _detect_service_for_action(action_name: str) -> str:
	"""Lazy delegate to the module-level helper in service.py.

	Defined here so the moved telemetry body (which references
	``_detect_service_for_action`` as a global) resolves at runtime without a
	circular import at module load time. Mirrors the shim in ``step.py``.
	"""
	from agents.task.agent.service import _detect_service_for_action as _impl
	return _impl(action_name)


class StepTelemetryMixin:
	"""Per-step telemetry emission for Agent."""

	async def _emit_step_telemetry(self, model_output, state, result, step_info=None):
		"""Best-effort step + iteration-complete telemetry; returns extracted actions for memory persistence."""
		actions = []
		try:  # Capture telemetry using TelemetryManager
			import time as time_module
			step_end_time = time_module.time()

			# Extract actions from model output
			if model_output and model_output.action is not None:
				for a in model_output.action:
					if hasattr(a, 'model_dump'):
						actions.append(a.model_dump(exclude_unset=True))
					elif isinstance(a, dict):
						actions.append(a)
					else:
						actions.append({'unknown': str(a)})

			# Extract errors from results
			errors = [r.error for r in result if r.error] if result else []

			# Create enhanced input data
			inputs = {
				"task": self.task,
				"state": {
					"url": state.url if state else None,
					"title": state.title if state else None,
				},
				"last_result": [r.error or r.extracted_content for r in self._last_result] if self._last_result else []
			}

			# Create enhanced output data
			outputs = {}
			if model_output:
				outputs = {
					"page_summary": model_output.current_state.page_summary if hasattr(model_output.current_state, "page_summary") else "",
					"evaluation_previous_goal": model_output.current_state.evaluation_previous_goal if hasattr(model_output.current_state, "evaluation_previous_goal") else "",
					"memory": model_output.current_state.memory if hasattr(model_output.current_state, "memory") else "",
					"next_goal": model_output.current_state.next_goal if hasattr(model_output.current_state, "next_goal") else "",
					"reasoning": model_output.current_state.reasoning if hasattr(model_output.current_state, "reasoning") else ""
				}

			# Capture metrics
			metrics = {
				"consecutive_failures": self.state.consecutive_failures,
				"token_count": self.message_manager.get_token_count() if hasattr(self.message_manager, "get_token_count") else None
			}

			# Extract file operations from actions and results
			files_created = []
			files_modified = []
			files_read = []
			files_deleted = []

			for action in actions:
				action_name = None
				action_params = {}

				# Handle different action formats
				if isinstance(action, dict):
					# Format: {action_name: {params}} or {name: x, params: y}
					if 'name' in action:
						action_name = action.get('name')
						action_params = action.get('params', action)
					else:
						# First key is action name
						keys = [k for k in action.keys() if k not in ('status', 'error')]
						if keys:
							action_name = keys[0]
							action_params = action.get(action_name, {}) if isinstance(action.get(action_name), dict) else {}

				if action_name:
					action_name_lower = action_name.lower()
					file_path = action_params.get('file_path') or action_params.get('path') or action_params.get('filename')

					if file_path:
						if action_name_lower in ('write_file', 'create_file', 'save_file'):
							files_created.append(file_path)
						elif action_name_lower in ('append_file', 'edit_file', 'modify_file', 'update_file'):
							files_modified.append(file_path)
						elif action_name_lower in ('read_file', 'get_file_content', 'load_file'):
							files_read.append(file_path)
						elif action_name_lower in ('delete_file', 'remove_file'):
							files_deleted.append(file_path)

			# Determine iteration type based on actions
			iteration_type = "mixed"
			if not actions:
				iteration_type = "thinking"
			else:
				services = set()
				has_done = False
				for action in actions:
					if isinstance(action, dict):
						# Check for done action
						if 'done' in action or action.get('name') == 'done':
							has_done = True
						# Detect service
						action_name = action.get('name') or list(action.keys())[0] if action else None
						if action_name:
							service = _detect_service_for_action(action_name)
							services.add(service)

				if has_done:
					iteration_type = "done"
				elif services == {'browser'}:
					iteration_type = "browser"
				elif services == {'filesystem'}:
					iteration_type = "filesystem"
				elif services == {'mcp'}:
					iteration_type = "mcp"

			# Determine iteration status
			iteration_status = "completed"
			error_message = None
			if errors:
				if len(errors) == len(actions) and actions:
					iteration_status = "failed"
				else:
					iteration_status = "partial"
				error_message = errors[0] if errors else None

			# Check for done action
			is_done = any(
				isinstance(a, dict) and ('done' in a or a.get('name') == 'done')
				for a in actions
			) if actions else False

			if is_done:
				iteration_status = "done"

			# Use TelemetryManager - single method call with new iteration fields
			self.telemetry_manager.capture_step(
				step=self.state.n_steps,
				actions=actions,
				errors=errors,
				brain_state=model_output.current_state if model_output else None,
				consecutive_failures=self.state.consecutive_failures,
				agent_name=getattr(self, 'agent_name', 'Unknown'),
				agent_type=self.__class__.__name__,
				current_task=self.task,
				inputs=inputs,
				outputs=outputs,
				metrics=metrics,
				# New iteration fields
				iteration=self.state.n_steps,
				iteration_type=iteration_type,
				iteration_status=iteration_status,
				files_created=files_created,
				files_modified=files_modified,
				files_read=files_read,
				files_deleted=files_deleted,
				error_message=error_message,
				is_done=is_done
			)

			# Also emit iteration_complete event for clean UI boundary
			reasoning_summary = ""
			if model_output and hasattr(model_output.current_state, 'reasoning'):
				reasoning_summary = (model_output.current_state.reasoning or "")[:200]

			# Build action results summary
			action_results = []
			if result:
				for i, r in enumerate(result):
					action_results.append({
						'index': i,
						'success': not r.error,
						'error': r.error,
						'has_content': bool(r.extracted_content)
					})

			# Calculate duration using state's last_step_start_time
			step_duration = 0.0
			if hasattr(self.state, 'last_step_start_time') and self.state.last_step_start_time:
				step_duration = step_end_time - self.state.last_step_start_time

			self.telemetry_manager.capture_iteration_complete(
				iteration=self.state.n_steps,
				step=self.state.n_steps,
				iteration_type=iteration_type,
				iteration_status=iteration_status,
				reasoning_summary=reasoning_summary,
				actions_executed=[],  # Don't duplicate actions - step event already has them
				action_results=action_results,
				files_created=files_created,
				files_modified=files_modified,
				files_read=files_read,
				files_deleted=files_deleted,
				success=(iteration_status in ('completed', 'done')),
				error=error_message,
				is_done=is_done,
				duration_seconds=step_duration
			)

		except Exception as telemetry_error:
			self.logger.debug(f"Failed to capture step telemetry: {telemetry_error}", exc_info=True)

		return actions

