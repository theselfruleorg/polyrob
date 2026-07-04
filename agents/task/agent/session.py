"""
Simplified Session Manager

This is a cleaned up version of SessionManager with:
- Feed scanning removed (overly complex)
- Metadata tracking simplified
- Direct session management
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from datetime import datetime
import json
import logging
import threading
import shutil
from enum import Enum

from agents.task.utils import get_safe_file_lock
from agents.task.path import pm, get_safe_singleton
from agents.task.constants import DEFAULT_USER_ID

logger = logging.getLogger(__name__)


class SessionStatus(Enum):
    """Session status enumeration for continuous chat.

    State machine:
    - CREATED → RUNNING, CANCELLED
    - RUNNING → COMPLETED, FAILED, CANCELLED, SUSPENDED
    - COMPLETED → RESUMED, CANCELLED
    - RESUMED → RUNNING, CANCELLED
    - SUSPENDED → RESUMED, CANCELLED
    - FAILED → RESUMED, CANCELLED
    - CANCELLED → (terminal state)

    Note: PAUSED was removed in favor of CANCELLED for user interruption.
    The continuous chat design uses COMPLETED → RESUMED flow for follow-up messages.
    """
    CREATED = "created"        # Initial state after creation
    RUNNING = "running"        # Currently executing
    COMPLETED = "completed"    # Finished successfully (waiting for follow-up)
    RESUMED = "resumed"        # Continuous chat resume (transitional)
    SUSPENDED = "suspended"    # Evicted from memory, persisted to disk
    FAILED = "failed"          # Execution failed
    CANCELLED = "cancelled"    # User cancelled (terminal)

# Keep old name for backwards compatibility
SessionState = SessionStatus


def get_user_status(internal_status: str) -> str:
	"""Convert internal status to user-facing status.

	Args:
		internal_status: Internal session status (running, completed, etc.)

	Returns:
		User-facing status:
		- "active" - Agent is working (can cancel)
		- "idle" - Agent finished (can send follow-up)
		- "stopped" - Agent was cancelled or failed

	Note: "paused" was removed. Use CANCELLED for user interruption,
	and COMPLETED → RESUMED flow for continuous chat.
	"""
	status_map = {
		"running": "active",
		"created": "active",
		"resumed": "active",
		"completed": "idle",
		"cancelled": "stopped",
		"failed": "stopped",
		"suspended": "idle",
		"error": "stopped",
	}
	return status_map.get(internal_status.lower(), "idle")


class SessionManager:
    """
    Simplified central manager for all agent sessions.
    
    This is the source of truth for:
    1. Session ID management and cleaning
    2. Path normalization and standardization
    3. Directory/file management for sessions
    4. Basic session state tracking
    """
    
    def __init__(self, base_dir: Optional[str] = None):
        """Initialize singleton session manager with specified base directory."""
        global logger
        from agents.task.logging_config import get_task_logger
        logger = get_task_logger('session')
        self.logger = logger
        
        # Initialize base directory
        self.base_dir = Path(base_dir) if base_dir else pm().data_root
        self.base_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize locking for thread safety
        self._lock = threading.RLock()
        
        # Simple session tracking
        self._sessions = {}  # {session_id: session_info}
        self._user_sessions = {}  # {user_id: [session_ids]}

        logger.info(f"SessionManager initialized with base_dir: {self.base_dir}")

        # Load existing sessions from disk
        self._load_sessions_from_disk()

    def _load_sessions_from_disk(self):
        """Load all sessions from disk on startup."""
        logger.info("Loading sessions from disk...")
        loaded_count = 0

        try:
            # Search both layouts: NEW flat (data_root/<user>/<session>/metadata.json,
            # written by path.py:get_session_root) AND legacy (<user>/sessions/<session>/...).
            _seen = set()
            for _pattern in ('*/*/metadata.json', '*/sessions/*/metadata.json'):
                for metadata_path in self.base_dir.glob(_pattern):
                    if metadata_path in _seen:
                        continue
                    _seen.add(metadata_path)
                    try:
                        with open(metadata_path) as f:
                            session_info = json.load(f)

                        session_id = session_info.get('id')
                        if not session_id:
                            continue

                        # Load into memory
                        self._sessions[session_id] = session_info

                        # Mark running sessions as suspended (they were interrupted)
                        if session_info.get('status') == 'running':
                            session_info['status'] = 'suspended'
                            session_info['suspended_at'] = datetime.now().isoformat()
                            self.logger.info(f"Marked session {session_id} as suspended (was running)")

                        # Track by user
                        user_id = session_info.get('user_id', DEFAULT_USER_ID)
                        if user_id not in self._user_sessions:
                            self._user_sessions[user_id] = []
                        if session_id not in self._user_sessions[user_id]:
                            self._user_sessions[user_id].append(session_id)

                        loaded_count += 1

                    except Exception as e:
                        logger.warning(f"Failed to load session from {metadata_path}: {e}")

            logger.info(f"Loaded {loaded_count} sessions from disk")

        except Exception as e:
            logger.error(f"Error loading sessions from disk: {e}")

    def create_session(self, session_id: str = None, user_id: Optional[str] = None) -> str:
        """
        Create or get a session.
        
        Args:
            session_id: Optional session ID. If not provided, generates a new one.
            user_id: Optional user ID for multi-user support
            
        Returns:
            The cleaned session ID
        """
        with self._lock:
            # Generate or clean session ID
            if not session_id:
                session_id = str(uuid.uuid4())
            
            # Always clean the session ID
            session_id = pm().clean_session_id(session_id)
            
            # Use default user if not specified
            user_id = user_id or DEFAULT_USER_ID
            
            # Check if session already exists
            if session_id in self._sessions:
                self.logger.debug(f"Session {session_id} already exists")
                return session_id
            
            # Create session info
            session_info = {
                'id': session_id,
                'user_id': user_id,
                'created_at': datetime.now().isoformat(),
                'status': 'created',  # Use 'created' for consistency
                'agents': []
            }
            
            # Store session
            self._sessions[session_id] = session_info
            
            # Track user sessions
            if user_id not in self._user_sessions:
                self._user_sessions[user_id] = []
            self._user_sessions[user_id].append(session_id)
            
            # Create session directory
            session_dir = self.get_session_dir(session_id, user_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            
            # Create standard subdirectories
            for subdir in ['logs', 'workspace', 'artifacts', 'screenshots', 'feed', 'telemetry']:
                (session_dir / subdir).mkdir(exist_ok=True)
            
            # Save minimal metadata
            self._save_metadata(session_id, user_id)
            
            self.logger.debug(f"Created session {session_id} for user {user_id}")
            return session_id
    
    def get_session_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """
        Get the directory path for a session.
        
        Args:
            session_id: The session ID
            user_id: Optional user ID
            
        Returns:
            Path to the session directory
        """
        session_id = pm().clean_session_id(session_id)
        
        # Get user ID from tracking if not provided
        if not user_id and session_id in self._sessions:
            user_id = self._sessions[session_id].get('user_id', DEFAULT_USER_ID)
        
        user_id = user_id or DEFAULT_USER_ID
        
        # Use path manager for consistent paths
        return pm().get_session_root(session_id, user_id)
    
    def get_workspace_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """Get workspace directory for a session - delegates to PathManager."""
        # Use PathManager as single source of truth for workspace paths
        return pm().get_workspace_dir(session_id, user_id)
    
    def get_subdirectory(self, session_id: str, subdir_name: str, user_id: Optional[str] = None) -> Path:
        """Get a subdirectory within the session directory.
        
        Args:
            session_id: Session ID
            subdir_name: Name of the subdirectory
            user_id: Optional user ID
            
        Returns:
            Path to the subdirectory
        """
        # Delegate to PathManager for consistent path handling
        return pm().get_subdir(session_id, subdir_name, user_id)
    
    def register_agent(self, session_id: str, agent_name: str, agent_id: str = None, 
                       role: str = None, agent_type: str = None, model_name: str = None, 
                       user_id: str = None, **kwargs) -> None:
        """
        Register an agent with a session.
        
        Args:
            session_id: The session ID
            agent_name: Name of the agent
            agent_id: Optional agent ID
            role: Optional agent role
            agent_type: Optional agent type/class name
            model_name: Optional model name
            user_id: Optional user ID
            **kwargs: Additional metadata to ignore
        """
        with self._lock:
            session_id = pm().clean_session_id(session_id)

            if session_id not in self._sessions:
                # Auto-create session if it doesn't exist (for backwards compatibility)
                self.logger.warning(f"Session {session_id} not found, creating it now")
                self.create_session(session_id, user_id)

            agent_info = {
                'name': agent_name,
                'id': agent_id or agent_name,
                'role': role,
                'type': agent_type,
                'model': model_name,
                'registered_at': datetime.now().isoformat()
            }

            # Check for duplicate agent registration and update instead of duplicating
            existing_agents = self._sessions[session_id].get('agents', [])
            for existing_agent in existing_agents:
                if existing_agent.get('id') == agent_info['id']:
                    self.logger.debug(f"Agent {agent_name} already registered, updating info")
                    # Update existing agent info instead of adding duplicate
                    existing_agent.update(agent_info)
                    self._save_metadata(session_id)
                    self.update_agents_summary(session_id)  # Update agents.json for webview
                    return

            # Add new agent
            self._sessions[session_id]['agents'].append(agent_info)
            self.logger.debug(f"Registered agent {agent_name} with session {session_id}")
            self._save_metadata(session_id)
            self.update_agents_summary(session_id)  # Update agents.json for webview
    
    def update_session_status(self, session_id: str, status: Union[str, SessionStatus]) -> None:
        """Update session status with enum validation.

        Args:
            session_id: Session identifier
            status: New status (string or SessionStatus enum)
        """
        with self._lock:
            session_id = pm().clean_session_id(session_id)

            if session_id not in self._sessions:
                self.logger.warning(f"Cannot update status for non-existent session {session_id}")
                return

            # Get previous status for event emission
            previous_status = self._sessions[session_id].get('status')

            # Convert string to enum if needed
            if isinstance(status, str):
                try:
                    status = SessionStatus(status.lower())
                except ValueError:
                    self.logger.warning(f"Invalid status value '{status}', using as-is")
                    # Use string as-is for backward compatibility
                    new_status_str = status.lower()
                    self._sessions[session_id]['status'] = new_status_str
                    self._sessions[session_id]['updated_at'] = datetime.now().isoformat()
                    self._save_metadata(session_id)
                    self._save_status_summary(session_id)  # Also save status.json for WebView
                    # Emit status event to feed for WebSocket updates
                    self._emit_status_event(session_id, new_status_str, previous_status)
                    return

            # Get current status for validation
            current = self._sessions[session_id].get('status')
            if isinstance(current, str):
                try:
                    current = SessionStatus(current.lower())
                except ValueError:
                    # Current status is invalid, allow transition
                    self.logger.warning(f"Current status '{current}' invalid, allowing transition")
                    current = None

            # Validate state transition
            if current and not self._is_valid_transition(current, status):
                self.logger.warning(
                    f"Invalid state transition for {session_id}: "
                    f"{current.value if isinstance(current, SessionStatus) else current} -> {status.value}"
                )
                # Allow it anyway but log warning

            # Update with enum value
            self._sessions[session_id]['status'] = status.value
            self._sessions[session_id]['updated_at'] = datetime.now().isoformat()
            self._save_metadata(session_id)
            self._save_status_summary(session_id)  # Also save status.json for WebView
            self.logger.debug(f"Updated session {session_id} status to {status.value}")
            
            # Emit status event to feed for WebSocket updates
            self._emit_status_event(session_id, status.value, previous_status)

    def _emit_status_event(self, session_id: str, new_status: str, previous_status: Optional[str] = None) -> None:
        """Emit a status event to the feed for WebSocket real-time updates.
        
        This allows the frontend to receive status changes immediately via WebSocket
        instead of relying on polling.
        
        Args:
            session_id: Session identifier
            new_status: New status value
            previous_status: Previous status value (optional)
        """
        import time
        
        try:
            # Get the user_id from session info
            session_info = self._sessions.get(session_id, {})
            user_id = session_info.get('user_id')
            
            # Get feed directory
            feed_dir = pm().get_subdir(session_id, "feed", user_id=user_id)
            if not feed_dir:
                self.logger.warning(f"Cannot emit status event - no feed dir for {session_id}")
                return
            
            # Create status event
            timestamp = time.time()
            status_event = {
                "type": "status",
                "timestamp": timestamp,
                "data": {
                    "status": new_status,
                    "previous_status": previous_status,
                    "session_id": session_id
                }
            }
            
            # Write to feed with timestamp-based filename
            filename = f"status_{int(timestamp * 1000)}.json"
            status_path = feed_dir / filename
            
            # Simple atomic write
            temp_path = status_path.with_suffix('.tmp')
            try:
                with open(temp_path, 'w') as f:
                    json.dump(status_event, f, indent=2)
                temp_path.replace(status_path)
                self.logger.debug(f"📊 Emitted status event: {new_status} (was: {previous_status})")
            except Exception as e:
                self.logger.error(f"Failed to write status event: {e}")
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass
                        
        except Exception as e:
            self.logger.error(f"Error emitting status event: {e}", exc_info=True)

    def _is_valid_transition(self, from_status: SessionStatus, to_status: SessionStatus) -> bool:
        """Validate if status transition is allowed.

        Args:
            from_status: Current status
            to_status: Target status

        Returns:
            True if transition is valid
        """
        # Same-state transitions are always valid (no-op)
        if from_status == to_status:
            return True

        # Define valid state transitions (PAUSED removed - use CANCELLED for interruption)
        VALID_TRANSITIONS = {
            SessionStatus.CREATED: [SessionStatus.RUNNING, SessionStatus.CANCELLED],
            SessionStatus.RUNNING: [SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED, SessionStatus.SUSPENDED],
            SessionStatus.COMPLETED: [SessionStatus.RESUMED, SessionStatus.CANCELLED],
            SessionStatus.RESUMED: [SessionStatus.RUNNING, SessionStatus.CANCELLED],
            SessionStatus.SUSPENDED: [SessionStatus.RESUMED, SessionStatus.CANCELLED],
            SessionStatus.FAILED: [SessionStatus.RESUMED, SessionStatus.CANCELLED],
            SessionStatus.CANCELLED: []  # Terminal state - no transitions out
        }

        allowed = VALID_TRANSITIONS.get(from_status, [])
        return to_status in allowed

    def try_transition_status(self, session_id: str, from_status: str, to_status: str) -> bool:
        """Atomically try to transition status. Returns False if current status doesn't match.

        This provides concurrency protection without locks - the status field itself
        acts as the synchronization mechanism.

        Args:
            session_id: Session ID to transition
            from_status: Expected current status
            to_status: New status to set

        Returns:
            True if transition succeeded, False if current status doesn't match expected
        """
        with self._lock:
            session_id = pm().clean_session_id(session_id)

            if session_id not in self._sessions:
                self.logger.warning(f"Cannot transition status for unknown session {session_id}")
                return False

            current = self._sessions[session_id].get('status')

            # Check if current status matches expected
            if current != from_status:
                self.logger.debug(f"Status transition failed for {session_id}: expected {from_status}, got {current}")
                return False

            # Perform transition
            self._sessions[session_id]['status'] = to_status
            self._sessions[session_id]['updated_at'] = datetime.now().isoformat()
            self._save_metadata(session_id)

            self.logger.info(f"Session {session_id}: {from_status} → {to_status}")
            return True

    def get_session_info(self, session_id: str) -> Dict:
        """Get session information."""
        with self._lock:
            session_id = pm().clean_session_id(session_id)
            return self._sessions.get(session_id, {})
    
    def get_active_sessions(self, user_id: Optional[str] = None) -> List[str]:
        """Get list of active session IDs (created or running)."""
        with self._lock:
            active_statuses = ['created', 'running', 'resumed']
            if user_id:
                user_sessions = self._user_sessions.get(user_id, [])
                return [sid for sid in user_sessions
                       if self._sessions.get(sid, {}).get('status') in active_statuses]
            else:
                return [sid for sid, info in self._sessions.items()
                       if info.get('status') in active_statuses]
    
    def cleanup_session(self, session_id: str, delete_files: bool = False) -> bool:
        """
        Cleanup a session.
        
        Args:
            session_id: The session ID to cleanup
            delete_files: Whether to delete session files
            
        Returns:
            True if cleanup was successful
        """
        with self._lock:
            session_id = pm().clean_session_id(session_id)
            
            if session_id not in self._sessions:
                return False
            
            session_info = self._sessions[session_id]
            user_id = session_info.get('user_id')
            
            # Remove from tracking
            del self._sessions[session_id]
            
            if user_id in self._user_sessions:
                self._user_sessions[user_id] = [
                    sid for sid in self._user_sessions[user_id] 
                    if sid != session_id
                ]
                if not self._user_sessions[user_id]:
                    del self._user_sessions[user_id]
            
            # Optionally delete files
            if delete_files:
                session_dir = self.get_session_dir(session_id, user_id)
                if session_dir.exists():
                    try:
                        shutil.rmtree(session_dir)
                        self.logger.info(f"Deleted session files for {session_id}")
                    except Exception as e:
                        self.logger.error(f"Failed to delete session files: {e}")
            
            self.logger.info(f"Cleaned up session {session_id}")
            return True
    
    def _save_metadata(self, session_id: str, user_id: Optional[str] = None) -> None:
        """Save session metadata to file."""
        try:
            session_info = self._sessions.get(session_id)
            if not session_info:
                return
            
            session_dir = self.get_session_dir(session_id, user_id or session_info.get('user_id'))
            metadata_file = session_dir / 'metadata.json'
            
            # Convert SessionState enums to strings before saving
            serializable_info = {}
            for key, value in session_info.items():
                if isinstance(value, SessionState):
                    serializable_info[key] = value.value  # Use the string value
                else:
                    serializable_info[key] = value

            # Use file lock to avoid concurrent writes by multiple processes/threads
            try:
                lock = get_safe_file_lock(str(metadata_file) + ".lock", timeout=10.0)
                with lock:
                    with open(metadata_file, 'w') as f:
                        json.dump(serializable_info, f, indent=2, default=str)
            except Exception:
                with open(metadata_file, 'w') as f:
                    json.dump(serializable_info, f, indent=2, default=str)
        except Exception as e:
            self.logger.warning(f"Failed to save metadata for {session_id}: {e}")
    
    def load_session(self, session_id: str, user_id: Optional[str] = None) -> bool:
        """
        Load session from disk if it exists.
        
        Args:
            session_id: The session ID to load
            user_id: Optional user ID
            
        Returns:
            True if session was loaded successfully
        """
        with self._lock:
            session_id = pm().clean_session_id(session_id)
            
            # Check if already loaded
            if session_id in self._sessions:
                return True
            
            # Try to load from disk
            session_dir = self.get_session_dir(session_id, user_id)
            metadata_file = session_dir / 'metadata.json'
            
            if not metadata_file.exists():
                return False
            
            try:
                with open(metadata_file, 'r') as f:
                    session_info = json.load(f)
                
                # Store in memory
                self._sessions[session_id] = session_info
                
                # Update user tracking
                user_id = session_info.get('user_id', DEFAULT_USER_ID)
                if user_id not in self._user_sessions:
                    self._user_sessions[user_id] = []
                if session_id not in self._user_sessions[user_id]:
                    self._user_sessions[user_id].append(session_id)
                
                self.logger.info(f"Loaded session {session_id} from disk")
                return True
                
            except Exception as e:
                self.logger.error(f"Failed to load session {session_id}: {e}")
                return False
    
    def session_exists(self, session_id: str) -> bool:
        """
        Check if a session exists.
        
        Args:
            session_id: Session ID to check
            
        Returns:
            True if the session exists, False otherwise
        """
        session_id = pm().clean_session_id(session_id)
        
        # Check in-memory sessions
        if session_id in self._sessions:
            return True
            
        # Check on disk
        session_root = pm().get_session_root(session_id)
        metadata_path = session_root / 'metadata.json'
        
        return metadata_path.exists()
    
    def update_session_metadata(self, session_id: str, metadata_update: Dict[str, Any]) -> None:
        """
        Update the metadata for a session.
        
        Args:
            session_id: The session ID
            metadata_update: The metadata to update
        """
        with self._lock:
            session_id = pm().clean_session_id(session_id)
            
            # Ensure session exists in memory
            if session_id not in self._sessions:
                # Try to load from disk
                if not self.load_session(session_id):
                    self.logger.warning(f"Session {session_id} not found for metadata update")
                    return
            
            # Update in-memory data
            session_info = self._sessions[session_id]
            
            # Deep merge metadata
            for key, value in metadata_update.items():
                if key == 'agents' and isinstance(value, list):
                    # Merge agent lists without duplicates
                    existing = session_info.get('agents', [])
                    merged = {(a.get('id') or a.get('name')): a for a in existing}
                    for agent in value:
                        agent_id = agent.get('id') or agent.get('name')
                        if agent_id:
                            merged[agent_id] = agent
                    session_info['agents'] = list(merged.values())
                else:
                    session_info[key] = value
            
            # Save to disk
            self._save_metadata(session_id)
    
    def get_task_phase(self, session_id: str) -> int:
        """Get current task phase for continuous conversation.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Current phase number (0 if not set)
        """
        with self._lock:
            session_id = pm().clean_session_id(session_id)
            
            if session_id not in self._sessions:
                return 0
            
            return self._sessions[session_id].get('task_phase', 0)
    
    def increment_task_phase(self, session_id: str) -> int:
        """Increment task phase counter for continuous conversation.
        
        Each user continuation increments the phase:
        - Phase 1: Initial task
        - Phase 2: First continuation
        - Phase 3: Second continuation, etc.
        
        Args:
            session_id: Session identifier
            
        Returns:
            New phase number
        """
        with self._lock:
            session_id = pm().clean_session_id(session_id)
            
            if session_id not in self._sessions:
                self.logger.warning(f"Cannot increment phase for unknown session {session_id}")
                return 0
            
            session_info = self._sessions[session_id]
            
            # Get current phase (default to 0)
            current_phase = session_info.get('task_phase', 0)
            new_phase = current_phase + 1
            
            # Update session metadata
            session_info['task_phase'] = new_phase
            session_info['phase_updated_at'] = datetime.now().isoformat()
            
            # Track phase history for analytics
            if 'phase_history' not in session_info:
                session_info['phase_history'] = []
            
            session_info['phase_history'].append({
                'phase': new_phase,
                'started_at': datetime.now().isoformat(),
                'status': 'active'
            })
            
            # Save to disk
            self._save_metadata(session_id)
            
            self.logger.info(f"✅ Session {session_id}: Phase {current_phase} → {new_phase}")
            return new_phase
    
    def add_to_feed(self, session_id: str, event_type: str, data: Dict[str, Any]) -> None:
        """
        Add an event to the session feed.
        
        Args:
            session_id: The session ID
            event_type: Type of event (e.g., 'step', 'action', 'result')
            data: Event data to store
        """
        import time
        import json
        
        session_id = pm().clean_session_id(session_id)
        
        # Get feed directory
        feed_dir = self.get_subdirectory(session_id, "feed")
        feed_dir.mkdir(parents=True, exist_ok=True)
        
        # Create feed entry
        timestamp = time.time()
        feed_entry = {
            'timestamp': timestamp,
            'type': event_type,  # Use 'type' to match webview expectations
            'data': data
        }
        
        # Write to feed file
        feed_file = feed_dir / f"{event_type}_{int(timestamp * 1000)}.json"
        with open(feed_file, 'w') as f:
            json.dump(feed_entry, f, indent=2)
        
        self.logger.debug(f"Added {event_type} event to feed for session {session_id}")

    def _save_summary_file(self, session_id: str, filename: str, data: Dict[str, Any]) -> None:
        """Save a summary JSON file using existing atomic write pattern.

        This method follows the same pattern as _save_metadata() for consistency.

        Args:
            session_id: The session ID
            filename: Name of the file (e.g., 'task.json', 'agents.json')
            data: Data to save
        """
        try:
            session_info = self._sessions.get(session_id)
            if not session_info:
                return

            session_dir = self.get_session_dir(session_id, session_info.get('user_id'))
            summary_file = session_dir / filename

            # Use file lock to avoid concurrent writes (SAME PATTERN as _save_metadata)
            try:
                lock = get_safe_file_lock(str(summary_file) + ".lock", timeout=10.0)
                with lock:
                    with open(summary_file, 'w') as f:
                        json.dump(data, f, indent=2, default=str)
            except Exception:
                # Fallback without lock
                with open(summary_file, 'w') as f:
                    json.dump(data, f, indent=2, default=str)

            self.logger.debug(f"Saved {filename} for session {session_id}")
        except Exception as e:
            self.logger.warning(f"Failed to save {filename} for {session_id}: {e}")

    def update_agents_summary(self, session_id: str) -> None:
        """Update agents.json summary file for webview.

        Extracts agent info from session metadata and writes to agents.json.
        This is called automatically when agents are registered.
        """
        from datetime import datetime

        session_id = pm().clean_session_id(session_id)

        if session_id not in self._sessions:
            return

        session_info = self._sessions[session_id]
        agents_list = session_info.get('agents', [])

        # Build last_updated timestamp
        summary_data = {
            "agents": agents_list,
            "last_updated": datetime.now().isoformat()
        }

        self._save_summary_file(session_id, 'agents.json', summary_data)

    def _save_status_summary(self, session_id: str) -> None:
        """Save status.json summary file for WebView.

        This is called automatically when session status is updated.
        WebView expects this file to exist for session status queries.
        """
        session_id = pm().clean_session_id(session_id)

        if session_id not in self._sessions:
            return

        session_info = self._sessions[session_id]

        # Build status summary data that WebView expects
        status_data = {
            "status": session_info.get('status', 'unknown'),
            "created_at": session_info.get('created_at'),
            "updated_at": session_info.get('updated_at', datetime.now().isoformat()),
            "session_id": session_id
        }

        self._save_summary_file(session_id, 'status.json', status_data)

    def get_all_sessions(self) -> List[Dict[str, Any]]:
        """
        Get all sessions from memory and disk.
        
        Returns:
            List of session info dictionaries
        """
        with self._lock:
            all_sessions = []
            
            # First get all from memory
            for session_id, session_info in self._sessions.items():
                all_sessions.append(session_info)
            
            # Then check disk for any not in memory using PathManager's structure
            from agents.task.path import pm
            try:
                # Search both layouts: NEW flat (data_root/<user>/<session>/metadata.json)
                # AND legacy (data_root/<user>/sessions/<session>/metadata.json).
                _seen = set()
                for _pattern in ('*/*/metadata.json', '*/sessions/*/metadata.json'):
                  for metadata_path in pm().data_root.glob(_pattern):
                    if metadata_path in _seen:
                        continue
                    _seen.add(metadata_path)
                    # Try to load from disk
                    try:
                        with open(metadata_path) as f:
                            session_info = json.load(f)
                        # L2 FIX: derive the id from the JSON `id` field (matching
                        # _load_sessions_from_disk), falling back to the directory name.
                        # Using the dir name alone desynced the dedup check from the
                        # in-memory map (keyed on the cleaned id) for sessions whose dir
                        # name != stored id, producing duplicate/extra list entries.
                        session_id = session_info.get('id') or metadata_path.parent.name
                        if session_id not in self._sessions:
                            all_sessions.append(session_info)
                    except Exception as e:
                        self.logger.warning(f"Failed to load session at {metadata_path} from disk: {e}")
            except Exception as e:
                # Fallback to old path structure if new approach fails
                sessions_dir = self.base_dir / "task" / "sessions"
                if sessions_dir.exists():
                    for session_dir in sessions_dir.iterdir():
                        if session_dir.is_dir():
                            session_id = session_dir.name
                            if session_id not in self._sessions:
                                metadata_path = session_dir / "metadata.json"
                                if metadata_path.exists():
                                    try:
                                        with open(metadata_path) as f:
                                            session_info = json.load(f)
                                        all_sessions.append(session_info)
                                    except Exception as e:
                                        self.logger.warning(f"Failed to load session {session_id} from disk: {e}")
            
            return all_sessions

    def cleanup_old_workspaces(self, max_age_days: int = 7) -> int:
        """Clean up workspaces for old completed sessions.

        Args:
            max_age_days: Maximum age in days before cleanup

        Returns:
            Number of workspaces cleaned
        """
        import shutil
        from datetime import datetime, timedelta

        cleaned_count = 0
        cutoff_time = datetime.now() - timedelta(days=max_age_days)

        for session_id, session_info in list(self._sessions.items()):
            # Only clean completed/failed/cancelled sessions
            status = session_info.get('status', '')
            if status not in ['completed', 'failed', 'cancelled']:
                continue

            # Check age
            updated_str = session_info.get('updated_at')
            if not updated_str:
                continue

            try:
                updated_time = datetime.fromisoformat(updated_str)
                if updated_time > cutoff_time:
                    continue  # Too recent

                # Get workspace directory
                user_id = session_info.get('user_id')
                workspace_dir = pm().get_workspace_dir(session_id, user_id)

                if workspace_dir.exists():
                    # Check size before deleting
                    size_mb = sum(f.stat().st_size for f in workspace_dir.rglob('*') if f.is_file()) / 1024 / 1024

                    # Delete workspace
                    shutil.rmtree(workspace_dir)
                    self.logger.info(f"Cleaned up workspace for {session_id} ({size_mb:.1f} MB, age: {(datetime.now() - updated_time).days} days)")
                    cleaned_count += 1

            except Exception as e:
                self.logger.warning(f"Failed to clean workspace for {session_id}: {e}")

        return cleaned_count


# Singleton access - use centralized get_safe_singleton
def get_session_manager() -> SessionManager:
    """Get the singleton SessionManager instance using centralized singleton pattern."""
    from agents.task.path import get_safe_singleton
    return get_safe_singleton(SessionManager)()