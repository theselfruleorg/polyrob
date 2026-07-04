from __future__ import annotations

import time


class MultiAgentMixin:
    """Multi-agent relationship registration and execution-timing tracking for SessionOrchestrator."""

    def register_multi_agent_relationship(self, agent_ids: list, agent_types: dict, execution_sequence: list = None,
                                        start_times: dict = None, end_times: dict = None, durations: dict = None) -> None:
        """Register a multi-agent relationship to the session with timing information.
        This is used to track relationships between agents in the session.

        Args:
            agent_ids: List of agent IDs in the relationship
            agent_types: Dictionary mapping agent_id to agent_type
            execution_sequence: Optional ordered list of agent IDs in execution sequence
            start_times: Optional dictionary mapping agent_id to start time (unix timestamp)
            end_times: Optional dictionary mapping agent_id to end time (unix timestamp)
            durations: Optional dictionary mapping agent_id to execution duration in seconds
        """
        try:
            self.logger.info(f"Registering multi-agent relationship with {len(agent_ids)} agents and timing data")

            # Session ID already clean from __init__
            clean_id = self.session_id

            # Use current time if timings not provided
            current_time = time.time()
            start_times = start_times or {}
            end_times = end_times or {}
            durations = durations or {}

            # Generate default timing for any missing agents
            for agent_id in agent_ids:
                if agent_id not in start_times:
                    start_times[agent_id] = current_time
                if agent_id not in end_times:
                    end_times[agent_id] = current_time
                if agent_id not in durations:
                    # Default duration is 0 if no actual duration info
                    durations[agent_id] = 0

            # Pass proper session ID to operations
            # ... rest of method ...

            # Prepare agent details with timing information
            agent_details = []
            for agent_id in agent_ids:
                # Extract agent name from ID
                agent_name = "Unknown"
                if "_" in agent_id:
                    agent_name = agent_id.split("_")[0]

                # Get agent type
                agent_type = agent_types.get(agent_id, "Unknown")

                # Create agent details
                agent_detail = {
                    'id': agent_id,
                    'agent_id': agent_id,
                    'name': agent_name,
                    'agent_name': agent_name,
                    'type': agent_type,
                    'agent_type': agent_type,
                }

                # Add model name if available in agent_models
                if hasattr(self, 'agent_models') and agent_id in self.agent_models:
                    agent_detail['model'] = self.agent_models[agent_id]

                # Add timing if available
                if agent_id in start_times:
                    agent_detail['start_time'] = start_times[agent_id]
                if agent_id in end_times:
                    agent_detail['end_time'] = end_times[agent_id]
                if agent_id in durations:
                    agent_detail['duration'] = durations[agent_id]

                agent_details.append(agent_detail)

            # Register with SessionManager
            if self.session_manager:
                try:
                    # First update agents in session
                    for agent_detail in agent_details:
                        # Add agent to session
                        if hasattr(self.session_manager, 'add_agent_to_session'):
                            self.session_manager.add_agent_to_session(
                                self.session_id,
                                agent_detail['agent_id'],
                                agent_detail  # Use the full details with timing
                            )

                    # Now add the relationship info to session metadata
                    # Use get_session_metadata if it exists, otherwise fallback to update_session_metadata
                    if hasattr(self.session_manager, 'get_session_metadata'):
                        metadata = self.session_manager.get_session_metadata(self.session_id) or {}

                        # Add relationship data with timing
                        metadata['multi_agent_relationship'] = {
                            'agent_ids': agent_ids,
                            'agent_types': agent_types,
                            'agent_models': getattr(self, 'agent_models', {}),
                            'execution_sequence': execution_sequence or agent_ids,
                            'start_times': start_times,
                            'end_times': end_times,
                            'durations': durations,
                            'agent_details': agent_details
                        }

                        # Update metadata
                        if hasattr(self.session_manager, 'set_session_metadata'):
                            self.session_manager.set_session_metadata(self.session_id, metadata)
                        else:
                            # Fallback to update method
                            self.session_manager.update_session_metadata(self.session_id, {
                                'multi_agent_relationship': metadata['multi_agent_relationship']
                            })
                    else:
                        # Just use update_session_metadata directly
                        self.session_manager.update_session_metadata(self.session_id, {
                            'multi_agent_relationship': {
                                'agent_ids': agent_ids,
                                'agent_types': agent_types,
                                'agent_models': getattr(self, 'agent_models', {}),
                                'execution_sequence': execution_sequence or agent_ids,
                                'start_times': start_times,
                                'end_times': end_times,
                                'durations': durations,
                                'agent_details': agent_details
                            }
                        })

                except Exception as e:
                    self.logger.warning(f"Failed to register multi-agent relationship with SessionManager: {e}")

            # Also capture with telemetry service using TelemetryManager
            try:
                self.telemetry_manager.capture_multi_agent_relationship(
                    agent_ids=agent_ids,
                    agent_types=agent_types,
                    execution_sequence=execution_sequence or agent_ids,
                    agent_models=getattr(self, 'agent_models', {}),
                    agent_details=agent_details
                )
            except Exception as e:
                self.logger.warning(f"Failed to register multi-agent relationship with telemetry: {e}")

        except Exception as e:
            self.logger.error(f"Error registering multi-agent relationship: {e}")

    def track_agent_execution(self, agent_id: str, agent_type: str, start_time: float = None, end_time: float = None) -> None:
        """Track execution timing for a specific agent.
        This is used to collect timing data for multi-agent relationships.

        Args:
            agent_id: The ID of the agent to track
            agent_type: The type of the agent
            start_time: Optional start time (unix timestamp), defaults to now if None
            end_time: Optional end time (unix timestamp), defaults to now if None
        """
        # Input validation
        if not agent_id or not agent_type:
            self.logger.warning("Cannot track agent execution: missing agent_id or agent_type")
            return

        try:
            # Initialize _agent_timing if not already present
            if not hasattr(self, '_agent_timing') or self._agent_timing is None:
                self._agent_timing = {
                    'start_times': {},
                    'end_times': {},
                    'durations': {},
                    'agent_types': {},
                    'execution_sequence': []
                }

            # Use current time if not provided
            current_time = time.time()

            # Record start time if provided or not already set
            start_times = self._agent_timing.get('start_times', {})
            if start_time is not None or agent_id not in start_times:
                start_times[agent_id] = start_time or current_time
                self._agent_timing['start_times'] = start_times

                # Add to execution sequence only when starting
                execution_sequence = self._agent_timing.get('execution_sequence', [])
                if agent_id not in execution_sequence:
                    execution_sequence.append(agent_id)
                    self._agent_timing['execution_sequence'] = execution_sequence

            # Record end time if provided
            if end_time is not None:
                end_times = self._agent_timing.get('end_times', {})
                end_times[agent_id] = end_time
                self._agent_timing['end_times'] = end_times

                # Calculate duration if both start and end are available
                if agent_id in start_times:
                    start = start_times[agent_id]
                    durations = self._agent_timing.get('durations', {})
                    durations[agent_id] = end_time - start
                    self._agent_timing['durations'] = durations

            # Always record agent type
            agent_types = self._agent_timing.get('agent_types', {})
            agent_types[agent_id] = agent_type
            self._agent_timing['agent_types'] = agent_types

            self.logger.debug(f"Tracked execution for agent {agent_id} ({agent_type})")

        except Exception as e:
            self.logger.error(f"Error tracking agent execution timing: {e}")

    def update_multi_agent_relationship(self) -> None:
        """Update the multi-agent relationship telemetry."""
        # Get telemetry service directly - don't rely on a class attribute
        try:
            from agents.task.telemetry import get_telemetry
            telemetry = get_telemetry()

            # Gather all agent IDs and their types
            agent_ids = []
            agent_types = {}
            agent_models = {}  # Explicitly track model information

            for agent_id, agent_controller in self.agents.items():
                agent_ids.append(agent_id)
                agent_types[agent_id] = self.agent_types.get(agent_id, "Unknown")
                agent_models[agent_id] = self.agent_models.get(agent_id, "Unknown")

            # Update telemetry with multi-agent relationship
            telemetry.track_multi_agent_relationship(
                session_id=self.session_id,
                agent_ids=agent_ids,
                agent_types=agent_types,
                agent_models=agent_models,
                execution_sequence=self.agent_execution_sequence
            )
        except Exception as e:
            self.logger.debug(f"Could not update multi-agent telemetry: {e}")
