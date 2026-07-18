"""Read-services that reconstruct session views from the telemetry feed.

Companion to ``agent_graph.py`` (F-2, 2026-07-17). These live in the ``agents``
tier so the console, CLI and HTTP API can all import them DOWNWARD. Each function
is a pure reader: it returns plain data (never a framework Response) and never
raises for a missing/corrupt individual feed file — the caller wraps the result.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from agents.task.path import pm

logger = logging.getLogger("agents.task.telemetry.feed_reads")


def build_session_services(clean_id: str, *, path_manager=None) -> Any:
    """Return a session's services view: the ``services.json`` cache verbatim if
    present, else a list of ``{name,type,actions,action_count}`` reconstructed
    from ``available_actions`` / ``service_actions`` / ``step`` feed entries.

    ``clean_id`` must already be cleaned. Unexpected errors propagate; per-file
    read errors are logged and skipped.
    """
    pm_ = path_manager or pm()

    # Look for a services.json file in the session directory
    feed_dir = pm_.get_feed_dir(clean_id)
    services_file = feed_dir.parent / "services.json"

    # If services.json exists, read it directly
    if services_file.exists():
        try:
            with services_file.open("r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in services file for session {clean_id}")
        except Exception as e:
            logger.warning(f"Error reading services file: {e}")

    # If no services.json, extract service info from feed entries (READ-ONLY)
    services: Dict[str, Dict[str, Any]] = {}

    if feed_dir.exists():
        # First, look for available_actions entries which have service grouping
        for file in sorted(feed_dir.glob("available_actions_*.json"), reverse=True):
            try:
                with file.open("r") as f:
                    entry = json.load(f)
                    if entry.get("type") == "available_actions" and "data" in entry:
                        data = entry["data"]
                        if "by_service" in data and isinstance(data["by_service"], dict):
                            service_info: List[Dict[str, Any]] = []
                            for service_name, actions in data["by_service"].items():
                                service_info.append({
                                    "name": service_name,
                                    "type": "controller",
                                    "actions": actions,
                                    "action_count": len(actions)
                                })

                            if service_info:
                                return service_info
            except Exception as e:
                logger.debug(f"Error processing services file {file}: {e}")
                continue

        # If we didn't find service data, look at service_actions entries
        for file in sorted(feed_dir.glob("service_actions_*.json")):
            try:
                with file.open("r") as f:
                    entry = json.load(f)
                    if entry.get("type") == "service_actions" and "data" in entry:
                        data = entry["data"]
                        service_name = data.get("service_name", "Unknown")
                        if service_name not in services:
                            services[service_name] = {
                                "name": service_name,
                                "type": data.get("service_type", "Unknown"),
                                "actions": data.get("available_actions", []),
                                "action_count": data.get("action_count", 0)
                            }
            except Exception as e:
                logger.debug(f"Error processing {file}: {e}")
                continue

        # If still no service data, extract from step actions
        if not services:
            for file in sorted(feed_dir.glob("step_*.json")):
                try:
                    with file.open("r") as f:
                        entry = json.load(f)
                        if entry.get("type") == "step" and "data" in entry:
                            data = entry["data"]
                            if "actions" in data and isinstance(data["actions"], list):
                                for action in data["actions"]:
                                    if "service" in action:
                                        service_name = action["service"]
                                        if service_name not in services:
                                            services[service_name] = {
                                                "name": service_name,
                                                "count": 0,
                                                "actions": set()
                                            }
                                        services[service_name]["count"] += 1
                                        if "name" in action:
                                            services[service_name]["actions"].add(action["name"])
                except Exception as e:
                    logger.debug(f"Error processing {file}: {e}")
                    continue

            # Convert sets to lists for JSON serialization
            for service in services.values():
                if "actions" in service and isinstance(service["actions"], set):
                    service["actions"] = list(service["actions"])

    return list(services.values())


def build_session_task(clean_id: str, *, path_manager=None) -> Optional[Dict[str, Any]]:
    """Return the session's INITIAL user task as ``{"task", "timestamp"}``, or None
    if no task is recorded. Prefers ``task.json`` → ``metadata.json`` → the earliest
    ``session_start`` → the earliest ``task_update`` feed event (the initial task,
    never a later derived goal). Timestamps fall back to file mtime.
    """
    pm_ = path_manager or pm()

    # First look for a dedicated task.json file in the session directory
    session_dir = pm_.get_feed_dir(clean_id).parent
    task_file = session_dir / "task.json"

    if task_file.exists():
        try:
            with task_file.open("r") as f:
                task_data = json.load(f)
            # Check if the task data is valid and contains the task
            if isinstance(task_data, dict) and "task" in task_data and task_data["task"]:
                logger.info(f"Found task in task.json: '{task_data['task']}'")
                timestamp = task_data.get("timestamp") or task_file.stat().st_mtime
                return {"task": task_data["task"], "timestamp": timestamp}
        except Exception as e:
            logger.warning(f"Error reading task.json for {clean_id}: {e}")

    # Next check for metadata.json as it often contains the original task
    metadata_file = session_dir / "metadata.json"
    if metadata_file.exists():
        try:
            with metadata_file.open("r") as f:
                metadata = json.load(f)
            if "task" in metadata and metadata["task"]:
                task = metadata["task"]
                logger.info(f"Found initial task in metadata.json: '{task}'")
                timestamp = metadata.get("created_at") or metadata.get("timestamp") or metadata_file.stat().st_mtime
                return {"task": task, "timestamp": timestamp}
        except Exception as e:
            logger.debug(f"Error reading metadata.json: {e}")

    # Look in the feed directory for session_start events (initial task)
    feed_dir = pm_.get_feed_dir(clean_id)
    if feed_dir.exists():
        for file in sorted(feed_dir.glob("session_start_*.json"), reverse=True):
            try:
                with file.open("r") as f:
                    entry = json.load(f)
                if entry.get("type") == "session_start" and "data" in entry:
                    data = entry["data"]
                    if "task" in data and data["task"]:
                        task = data["task"]
                        timestamp = entry.get("timestamp") or file.stat().st_mtime
                        logger.info(f"Found initial task in session_start event: '{task}'")
                        return {"task": task, "timestamp": timestamp}
            except Exception as e:
                logger.debug(f"Error processing session start file {file}: {e}")
                continue

        # Then the first (earliest) task_update event, which is likely the initial task
        task_update_files = sorted(feed_dir.glob("task_update_*.json"))
        if task_update_files:
            try:
                first_file = task_update_files[0]
                with first_file.open("r") as f:
                    entry = json.load(f)
                if entry.get("type") == "task_update" and "data" in entry:
                    data = entry["data"]
                    if "task" in data and data["task"]:
                        task = data["task"]
                        timestamp = entry.get("timestamp") or first_file.stat().st_mtime
                        logger.info(f"Found initial task in first task_update event: '{task}'")
                        return {"task": task, "timestamp": timestamp}
            except Exception as e:
                logger.debug(f"Error processing task update file: {e}")

    logger.warning(f"No task information found for session {clean_id}")
    return None


def build_session_skills(clean_id: str, *, path_manager=None) -> Any:
    """Return a session's skills view: the ``skills.json`` cache verbatim if present,
    else the ``skills`` list from the newest ``session_start`` event, else ``[]``.
    """
    pm_ = path_manager or pm()

    # Look for a skills.json file in the session directory
    feed_dir = pm_.get_feed_dir(clean_id)
    session_dir = feed_dir.parent
    skills_file = session_dir / "skills.json"

    if skills_file.exists():
        try:
            with skills_file.open("r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in skills file for session {clean_id}")
        except Exception as e:
            logger.warning(f"Error reading skills file: {e}")

    # If no skills.json, try to extract from session_start event
    if feed_dir.exists():
        for file in sorted(feed_dir.glob("session_start_*.json"), reverse=True):
            try:
                with file.open("r") as f:
                    entry = json.load(f)
                if entry.get("type") == "session_start" and "data" in entry:
                    data = entry["data"]
                    if "skills" in data and isinstance(data["skills"], list):
                        return data["skills"]
            except Exception as e:
                logger.debug(f"Error processing session start file {file}: {e}")
                continue

    return []
