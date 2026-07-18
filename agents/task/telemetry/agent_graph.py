"""Reconstruct a session's multi-agent roster from its telemetry feed.

Pure read-service extracted from ``webview/server.py::api_agents`` (F-2, 2026-07-17).
It lives in the ``agents`` tier (not ``webview``) so the console, the CLI and the
HTTP API can all import it DOWNWARD — a webview-tier helper cannot be reused by
cli/api (same tier is forbidden by the layering ratchet).

``build_session_agents`` reads the ``agents.json`` cache if present and valid,
otherwise reconstructs the roster from feed files (``multi_agent_relationship``,
``agent_registration``, ``llm_request``, ``step``) plus a ``metadata.json``
fallback, then dedups and orders the result. It returns a plain list of
``{"id", "name", "type", "model"}`` dicts and never raises for a missing/corrupt
individual file — the caller wraps the result in whatever response shape it needs.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

from agents.task.path import pm

logger = logging.getLogger("agents.task.telemetry.agent_graph")


def build_session_agents(clean_id: str, *, path_manager=None) -> List[Dict[str, str]]:
    """Return the ``{id,name,type,model}`` roster for ``clean_id``.

    ``clean_id`` must already be cleaned (``pm().clean_session_id``). ``path_manager``
    defaults to the ``pm()`` singleton; inject one for tests. Unexpected errors
    propagate to the caller; per-file read errors are logged and skipped.
    """
    pm_ = path_manager or pm()

    # Look for an agents.json file in the session directory
    feed_dir = pm_.get_feed_dir(clean_id)
    session_dir = feed_dir.parent
    agents_file = session_dir / "agents.json"

    # If agents.json exists, read it directly
    if agents_file.exists():
        try:
            with agents_file.open("r") as f:
                cached_data = f.read()
                if cached_data and len(cached_data) >= 10:
                    cached_agents = json.loads(cached_data)

                    if cached_agents and isinstance(cached_agents, list) and len(cached_agents) > 0:
                        # Normalize the cached data
                        normalized_agents = []
                        for agent in cached_agents:
                            normalized_agent = {
                                'id': agent.get('id', agent.get('agent_id', 'Unknown')),
                                'name': agent.get('name', agent.get('agent_name', 'Unknown')),
                                'type': agent.get('type', agent.get('agent_type', 'Unknown')),
                                'model': agent.get('model', agent.get('model_name', ''))
                            }
                            normalized_agents.append(normalized_agent)

                        logger.debug(f"Using cached agents data with {len(normalized_agents)} agents")
                        return normalized_agents
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in agents file for session {clean_id}")
        except Exception as e:
            logger.warning(f"Error reading agents file: {e}")

    # If no agents.json or it's invalid, extract from feed files (READ-ONLY)
    agents: List[Dict[str, str]] = []
    agent_ids = set()
    agent_models: Dict[str, str] = {}
    execution_sequence: List[str] = []

    logger.debug(f"Extracting agents data from feed for session {clean_id}")

    if feed_dir.exists():
        # Check for multi_agent_relationship entries - they have the most complete agent info
        relationship_files = sorted(feed_dir.glob("*multi_agent_relationship*.json"), reverse=True)

        # Limit to latest 5 relationship files for efficiency
        for file in relationship_files[:5]:
            try:
                with file.open("r") as f:
                    entry = json.load(f)
                    if entry.get("type") in ["multi_agent_relationship", "multi_agent_relationship_detailed"] and "data" in entry:
                        data = entry["data"]
                        # Look for agent_models first - this is the most reliable source
                        if "agent_models" in data and isinstance(data["agent_models"], dict):
                            for agent_id, model in data["agent_models"].items():
                                if model:
                                    agent_models[agent_id] = model
                                    logger.debug(f"Found model info from relationship: {agent_id} -> {model}")

                        # Store execution sequence if available
                        if "execution_sequence" in data and isinstance(data["execution_sequence"], list) and data["execution_sequence"]:
                            execution_sequence = data["execution_sequence"]
                            logger.debug(f"Found execution sequence with {len(execution_sequence)} agents")
                        elif "agent_ids" in data and isinstance(data["agent_ids"], list) and data["agent_ids"]:
                            execution_sequence = data["agent_ids"]
                            logger.debug(f"Using agent_ids as execution sequence with {len(execution_sequence)} agents")

                        # Process agent details
                        if "agent_details" in data and isinstance(data["agent_details"], list):
                            for agent_detail in data["agent_details"]:
                                agent_id = agent_detail.get("id") or agent_detail.get("agent_id")
                                if not agent_id:
                                    continue

                                # Extract model info directly from agent_detail if available
                                model_from_detail = agent_detail.get("model")
                                if model_from_detail:
                                    agent_models[agent_id] = model_from_detail
                                    logger.debug(f"Found agent model in detail: {agent_id} -> {model_from_detail}")

                                # Use the best available model info for this agent
                                best_model = model_from_detail or agent_models.get(agent_id, '')

                                if agent_id not in agent_ids:
                                    agent = {
                                        'id': agent_id,
                                        'name': agent_detail.get("name") or agent_detail.get("agent_name") or agent_id,
                                        'type': agent_detail.get("type") or agent_detail.get("agent_type") or "Unknown",
                                        'model': best_model
                                    }
                                    agents.append(agent)
                                    agent_ids.add(agent_id)
                                    logger.debug(f"Added agent from relationship detail: {agent_id} with model {best_model}")
                                # Update model for existing agents
                                else:
                                    for agent in agents:
                                        if agent.get("id") == agent_id:
                                            if not agent.get("model") and best_model:
                                                agent["model"] = best_model
                                                logger.debug(f"Updated agent model: {agent_id} -> {best_model}")
                                            break
            except Exception as e:
                logger.debug(f"Error processing relationship file {file}: {e}")
                continue

        # Look for agent_registration entries to find more agents
        registration_files = sorted(feed_dir.glob("agent_registration_*.json"), reverse=True)

        # Limit to 20 most recent registration files
        for file in registration_files[:20]:
            try:
                with file.open("r") as f:
                    entry = json.load(f)
                    if entry.get("type") == "agent_registration" and "data" in entry:
                        agent_data = entry["data"]
                        agent_id = agent_data.get("id") or agent_data.get("agent_id")

                        if not agent_id:
                            # Try to extract agent_id from filename if not in data
                            filename = file.name
                            if "_" in filename:
                                parts = filename.split("_")
                                if len(parts) >= 3:  # agent_registration_AGENT_ID_TIMESTAMP.json
                                    possible_id = "_".join(parts[2:-1])
                                    if possible_id:
                                        agent_id = possible_id
                                        logger.debug(f"Extracted agent_id from filename: {agent_id}")

                        if not agent_id:
                            continue

                        # Extract model information from registration
                        model = None
                        if "model_name" in agent_data and agent_data["model_name"]:
                            model = agent_data["model_name"]
                        elif "model" in agent_data and agent_data["model"]:
                            model = agent_data["model"]
                        elif "llm_model" in agent_data and agent_data["llm_model"]:
                            model = agent_data["llm_model"]

                        # Handle "Unknown" or "None" values
                        if model and (model == "Unknown" or model == "None"):
                            model = None

                        # Store the model in our models dictionary for later use
                        if model:
                            agent_models[agent_id] = model
                            logger.debug(f"Stored model for agent {agent_id}: {model}")

                        # Use the best available model
                        best_model = model or agent_models.get(agent_id, '')

                        if agent_id not in agent_ids:
                            agent = {
                                'id': agent_id,
                                'name': agent_data.get("name") or agent_data.get("agent_name") or agent_id,
                                'type': agent_data.get("type") or agent_data.get("agent_type") or "Unknown",
                                'model': best_model
                            }
                            agents.append(agent)
                            agent_ids.add(agent_id)
                            logger.debug(f"Added agent from registration: {agent_id} with model {best_model}")
                        # Update existing agents with better model info if needed
                        else:
                            for agent in agents:
                                if agent.get("id") == agent_id:
                                    if not agent.get("model") or (best_model and len(best_model) > len(agent.get("model", ""))):
                                        agent["model"] = best_model
                                        logger.debug(f"Updated existing agent model: {agent_id} -> {best_model}")
                                    if agent.get("name") in [agent_id, "Unknown", "Agent"] and agent_data.get("name"):
                                        agent["name"] = agent_data.get("name") or agent_data.get("agent_name")
                                    if agent.get("type") == "Unknown" and agent_data.get("type"):
                                        agent["type"] = agent_data.get("type") or agent_data.get("agent_type")
                                    break
            except Exception as e:
                logger.debug(f"Error processing agent registration file {file}: {e}")
                continue

        # Look for LLM request data to capture model information
        llm_request_files = sorted(feed_dir.glob("llm_request_*.json"), reverse=True)

        # Limit to 30 most recent LLM requests
        for file in llm_request_files[:30]:
            try:
                with file.open("r") as f:
                    entry = json.load(f)
                    if entry.get("type") == "llm_request" and "data" in entry:
                        data = entry["data"]
                        agent_id = data.get("agent_id")

                        if not agent_id:
                            continue

                        # Extract model information
                        model_name = None
                        if "model_name" in data and data["model_name"]:
                            model_name = data["model_name"]
                        elif "model" in data and data["model"]:
                            model_name = data["model"]

                        # Update agent models if we found a model name
                        if agent_id and model_name:
                            agent_models[agent_id] = model_name
                            logger.debug(f"Updated agent model: {agent_id} -> {model_name}")

                            # Update existing agents' model info if needed
                            for agent in agents:
                                if agent.get("id") == agent_id and (not agent.get("model") or agent.get("model") == ''):
                                    agent["model"] = model_name
                                    logger.debug(f"Applied model to existing agent: {agent_id} -> {model_name}")
            except Exception as e:
                logger.debug(f"Error processing LLM request file {file}: {e}")
                continue

        # If we still don't have agent data, search in step files
        if not agents:
            step_files = sorted(feed_dir.glob("step_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
            for file in step_files:
                try:
                    with file.open("r") as f:
                        entry = json.load(f)
                        if entry.get("type") == "step" and "data" in entry:
                            data = entry["data"]
                            agent_id_val = (
                                data.get("agent_id")
                                or data.get("id")
                                or data.get("agent_name")
                            )
                            agent_name_val = data.get("agent_name") or data.get("name")
                            agent_type_val = data.get("agent_type") or data.get("type") or "Unknown"

                            if agent_id_val and agent_id_val not in agent_ids:
                                agents.append({
                                    'id': agent_id_val,
                                    'name': agent_name_val or agent_id_val,
                                    'type': agent_type_val,
                                    'model': agent_models.get(agent_id_val, '')
                                })
                                agent_ids.add(agent_id_val)
                except Exception as e:
                    logger.debug(f"Error processing step file {file}: {e}")
                    continue

    # Check metadata.json as a fallback for all agents
    try:
        metadata_file = session_dir / "metadata.json"
        if metadata_file.exists():
            with metadata_file.open("r") as f:
                metadata_payload = json.load(f)
                meta_agents = metadata_payload.get("agents", []) if isinstance(metadata_payload, dict) else []
                for meta_agent in meta_agents:
                    meta_id = meta_agent.get("id") or meta_agent.get("agent_id")
                    if not meta_id:
                        continue
                    if meta_id not in agent_ids:
                        agents.append({
                            'id': meta_id,
                            'name': meta_agent.get('name') or meta_agent.get('agent_name') or meta_id,
                            'type': meta_agent.get('type') or meta_agent.get('agent_type') or 'Unknown',
                            'model': meta_agent.get('model') or meta_agent.get('model_name') or ''
                        })
                        agent_ids.add(meta_id)
                    else:
                        # Update existing agent entry with any missing details
                        for ag in agents:
                            if ag.get('id') == meta_id:
                                if (not ag.get('name') or ag['name'] == 'Unknown') and (meta_agent.get('name') or meta_agent.get('agent_name')):
                                    ag['name'] = meta_agent.get('name') or meta_agent.get('agent_name')
                                if (not ag.get('type') or ag['type'] == 'Unknown') and (meta_agent.get('type') or meta_agent.get('agent_type')):
                                    ag['type'] = meta_agent.get('type') or meta_agent.get('agent_type')
                                if not ag.get('model') and (meta_agent.get('model') or meta_agent.get('model_name')):
                                    ag['model'] = meta_agent.get('model') or meta_agent.get('model_name')
                                break
    except Exception as e:
        logger.debug(f"Error merging agents from metadata.json: {e}")

    # Ensure we're getting all unique agents with complete information
    unique_agents: Dict[str, Dict[str, str]] = {}
    for agent in agents:
        agent_id = agent.get('id')
        if agent_id:
            # If this agent ID already exists, merge information (favor non-empty values)
            if agent_id in unique_agents:
                existing = unique_agents[agent_id]
                # For each field, use the new value only if the existing one is empty
                for field in ['name', 'type', 'model']:
                    if not existing.get(field) and agent.get(field):
                        existing[field] = agent.get(field)
                    # If the new value is longer or more specific, prefer it
                    elif existing.get(field) and agent.get(field) and len(agent.get(field)) > len(existing.get(field)):
                        # Only replace if it's a more informative value (longer and not just "Unknown")
                        field_value = agent.get(field)
                        if field_value and field_value.lower() != "unknown":
                            existing[field] = agent.get(field)
            else:
                unique_agents[agent_id] = agent

    # Convert back to list
    agents = list(unique_agents.values())

    # Sort agents by execution order if possible, otherwise by ID
    try:
        if execution_sequence:
            # First sort by natural execution order using known sequence
            ordered_agents = []
            remaining_agents = []

            # First add agents in the execution sequence order
            for exec_id in execution_sequence:
                for agent in agents:
                    if agent['id'] == exec_id:
                        ordered_agents.append(agent)
                        break

            # Then add any remaining agents not in the sequence
            for agent in agents:
                if agent['id'] not in execution_sequence:
                    remaining_agents.append(agent)

            # Sort remaining agents by ID
            remaining_agents.sort(key=lambda a: a['id'])

            # Combine ordered and remaining agents
            agents = ordered_agents + remaining_agents
        else:
            # Fall back to sorting by agent ID
            agents.sort(key=lambda a: a['id'])
    except Exception as e:
        logger.debug(f"Error sorting agents: {e}")
        # If sorting fails, ensure we still return agents in some order
        agents.sort(key=lambda a: a.get('id', ''))

    return agents
