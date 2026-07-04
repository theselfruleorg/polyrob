from __future__ import annotations

"""Session statistics calculation utilities.

This module provides a single public helper – ``compute_session_stats`` –
that reads all JSON feed files for a given session and aggregates a rich set
of metrics that are then returned as a typed :pyclass:`dict`.  It replaces
custom ad-hoc code that previously lived directly in *server.py* and therefore
keeps the HTTP layer slim while making the statistics logic re-usable from
other parts of the code-base (e.g. CLI tools or scheduled jobs).

Key features
------------
1. **Robust parsing** – gracefully skips corrupted/partial feed files and logs
   the problem instead of aborting the whole aggregation.
2. **Provider-agnostic cost estimation** – relies on the cost estimate that is
   already written by :pyclass:`agents.task.telemetry.service.ProductTelemetry`
   (`cost_estimate` field) but also falls back to an on-the-fly estimate when
   only prompt/completion tokens are available.
3. **Extensible result** – returns a *flat* dictionary that can be directly
   JSON-serialised and sent to the browser without further transformation.

The expected directory structure is the standard
``…/<user>/sessions/<session_id>/feed`` layout produced by
:pyclass:`agents.task.path.PathManager` / :pyclass:`SessionManager`.
"""

from collections import Counter, defaultdict
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cost calculation utilities - using centralized modules/credits/cost_utils.py
# ---------------------------------------------------------------------------

# NOTE: We use lazy imports inside functions to avoid circular imports
# The stats_service runs in webview context which doesn't need core/bot/etc.
# Importing modules.credits.cost_utils at module level triggers a circular import chain:
#   modules -> modules.base_module -> core.base_component -> core -> core.bot ...


def _get_cost_utils():
    """Lazy import to avoid circular imports."""
    from modules.credits.cost_utils import calculate_cost_from_tokens, get_cost_breakdown
    return calculate_cost_from_tokens, get_cost_breakdown


def _calculate_user_cost_from_api_cost(api_cost_usd: float) -> dict:
    """
    Calculate what user pays from API cost.

    Uses centralized cost_utils for consistent pricing across the app.
    This is a FALLBACK for legacy telemetry events without user_cost_usd.

    Args:
        api_cost_usd: What we pay the API provider

    Returns:
        Dict with complete breakdown
    """
    _, get_cost_breakdown = _get_cost_utils()
    return get_cost_breakdown(api_cost_usd)


def _calculate_cost_from_registry(
    model_name: str | None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None
) -> float:
    """
    Calculate cost using centralized cost_utils.

    Wrapper for backward compatibility - delegates to cost_utils.

    Args:
        model_name: Name of the model
        prompt_tokens: Number of input tokens (preferred)
        completion_tokens: Number of output tokens (preferred)
        total_tokens: Total token count (fallback)

    Returns:
        Estimated cost in USD
    """
    calculate_cost_from_tokens, _ = _get_cost_utils()
    return calculate_cost_from_tokens(
        model_name=model_name or "unknown",
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        total_tokens=total_tokens
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_session_stats(feed_dir: Path) -> Dict[str, Any]:
    """Aggregate statistics for a *single* session.

    Parameters
    ----------
    feed_dir:
        Path to the session's ``feed`` directory.

    Returns
    -------
    dict
        A JSON-serialisable mapping with all calculated statistics.
    """

    stats: Dict[str, Any] = {
        "llm_calls": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "actions": 0,
        "top_services": [],  # to be filled later with list[{"name", "count"}]
        "top_actions": [],  # same structure as above
        "feed_entries": 0,
        "models_used": []  # list[{"name", "count", "tokens", "cost"}]
    }

    # CRITICAL FIX: Track ALL processed request signatures, not just IDs
    processed_requests = set()

    if not feed_dir.exists() or not feed_dir.is_dir():
        logger.debug("Feed directory %s does not exist – returning empty stats", feed_dir)
        return stats

    # Aggregation helpers
    service_counter: Counter[str] = Counter()
    action_counter: Counter[str] = Counter()
    model_usage: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "tokens": 0, "cost": 0.0})

    # PRIORITY 1: Process LLM usage directory FIRST (authoritative source)
    # Check both old (session_root/llm_usage) and new (session_root/data/llm_usage) paths
    session_root = feed_dir.parent
    llm_usage_dir = session_root / "data" / "llm_usage"
    if not (llm_usage_dir.exists() and llm_usage_dir.is_dir()):
        # Fallback to old location
        llm_usage_dir = session_root / "llm_usage"
    
    if llm_usage_dir.exists() and llm_usage_dir.is_dir():
        logger.debug("Found dedicated LLM usage directory, processing as authoritative source")
        llm_files = sorted(llm_usage_dir.glob("llm_usage_*.json"))
        for file_path in llm_files:
            try:
                with file_path.open("r") as fp:
                    llm_entry = json.load(fp)
                
                # Create comprehensive signature for deduplication
                signature = _create_request_signature(llm_entry)
                if signature in processed_requests:
                    logger.debug(f"Skipping duplicate LLM request: {signature}")
                    continue
                processed_requests.add(signature)
                
                # Process this entry using helper function
                _process_llm_entry(llm_entry, stats, model_usage)
                
            except Exception as exc:
                logger.debug("Could not parse LLM usage file %s – %s", file_path, exc)
                continue

    # Iterate over all JSON files in the feed directory
    json_files = sorted(feed_dir.glob("*.json"))
    stats["feed_entries"] = len(json_files)

    for file_path in json_files:
        try:
            with file_path.open("r") as fp:
                entry = json.load(fp)
        except Exception as exc:  # noqa: BLE001 – broad is ok inside loop
            logger.debug("Could not parse feed entry %s – %s", file_path, exc)
            continue

        entry_type = entry.get("type")
        if not entry_type:
            continue

        # ------------------------------------------------------------------
        # LLM request handling - SKIP from feed, use llm_usage/ as sole source
        # ------------------------------------------------------------------
        if entry_type == "llm_request":
            # LLM data is now written ONLY to llm_usage/ directory
            # Skip any llm_request entries in feed/ to prevent duplicate counting
            # This simplifies deduplication and ensures single source of truth
            logger.debug("Skipping llm_request from feed - using llm_usage/ as source")
            continue

        # ------------------------------------------------------------------
        # Actions → gathered from *step* entries (and agent_step that gets converted to "step" type)
        # ------------------------------------------------------------------
        elif entry_type == "step":
            data = entry.get("data", {})
            # FIXED: Ensure data is not None before accessing its fields
            if not data:
                continue
                
            actions: List[Dict[str, Any]] = data.get("actions", [])
            
            # Count total actions
            action_count = len(actions)
            stats["actions"] += action_count
            
            # Extract token count from step metrics if available
            context = data.get("context", {})
            if context:
                metrics = context.get("metrics", {})
                if metrics and "token_count" in metrics:
                    try:
                        step_tokens = int(metrics["token_count"] or 0)
                        if step_tokens > 0:
                            # ONLY add to total tokens - step events don't have accurate model info
                            # The actual LLM requests in llm_usage/ and feed have the correct model data
                            stats["total_tokens"] += step_tokens
                            
                            # REMOVED: Do NOT track model usage or cost from step events
                            # This causes inaccurate model attribution and inflated costs
                            # Step events are results of LLM calls, not the calls themselves
                            
                            logger.debug(f"Found {step_tokens} tokens in step event (not counting as LLM call)")
                    except (ValueError, TypeError) as e:
                        logger.debug(f"Failed to parse step token count: {e}")
                        
            # Process individual actions for service and action count
            for act in actions:
                # First try name, then fallback to action_type, then skip if neither exists
                act_name = act.get("name") or act.get("action_type")
                if not act_name:
                    continue  # Skip actions without proper names
                    
                action_counter[act_name] += 1
                
                # ARCHITECTURE FIX: Use tool from action metadata, not heuristics
                # Extract tool from action name prefix (browser_action → browser)
                if '_' in act_name:
                    tool = act_name.split('_', 1)[0]
                else:
                    # Core actions like 'done'
                    tool = act.get("service") or act.get("tool") or "default"
                        
                service_counter[tool] += 1
                
        # ------------------------------------------------------------------
        # Tool execution events - PROPER telemetry source
        # ------------------------------------------------------------------
        elif entry_type == "tool_execution":
            # Use the NEW tool_execution events we're now capturing
            data = entry.get("data", {})
            if data:
                tool_name = data.get("tool_name", "default")
                action_name = data.get("action_name", "unknown")
                
                # Count the tool and action
                service_counter[tool_name] += 1
                action_counter[action_name] += 1
                
                # Increment total actions count
                stats["actions"] += 1
                
        # ------------------------------------------------------------------
        # Status updates – currently not part of the numerical statistics but
        # could be added in the future.
        # ------------------------------------------------------------------
        elif entry_type == "status":
            # Nothing to aggregate yet – placeholder for future extension.
            pass
            
        # ------------------------------------------------------------------
        # Multi-agent relationship - store the relationship data
        # ------------------------------------------------------------------
        elif entry_type in ["multi_agent_relationship", "multi_agent_relationship_detailed", "session_relationship"]:
            # Currently not directly used for numerical stats, but could add
            # agent relationship metrics in the future
            pass
            
        # ------------------------------------------------------------------
        # REMOVED: available_actions event handling
        # ------------------------------------------------------------------
        # These events show what actions are AVAILABLE, not what was EXECUTED
        # Counting them inflates service usage numbers incorrectly
        # Tool execution events provide the actual usage data

    # Fill *top_* fields (limit to 5 each)
    stats["top_services"] = [
        {"name": name, "count": cnt} for name, cnt in service_counter.most_common(5)
    ]
    stats["top_actions"] = [
        {"name": name, "count": cnt} for name, cnt in action_counter.most_common(5)
    ]
    
    # Add detailed action breakdown (all actions, not just top 5)
    stats["detailed_actions"] = dict(action_counter)

    # Flatten model_usage mapping into list for easy JSON serialization
    stats["models_used"] = [
        {"name": m, **vals} for m, vals in model_usage.items()
    ]

    # ------------------------------------------------------------------
    # Fallback – if the session registered agents but none of their models
    # appear in the LLM usage data (e.g. because the executor never made an
    # LLM call or token tracking failed) we still want to list the model so
    # that the UI can display a complete agent line-up.  We therefore merge
    # any models from *agents.json* that are missing from the list above and
    # populate the numeric fields with zeros.
    # ------------------------------------------------------------------
    try:
        agents_file = feed_dir.parent / "agents.json"
        if agents_file.exists():
            with agents_file.open("r") as f:
                agents_data = json.load(f) or []

            for agent in agents_data:
                model_name = agent.get("model") or agent.get("model_name")
                if not model_name:
                    continue

                # If we already have usage stats for this model, skip
                if model_name in model_usage:
                    continue

                # Otherwise add a zero-usage placeholder so the UI can show it
                stats["models_used"].append({
                    "name": model_name,
                    "count": 0,
                    "tokens": 0,
                    "cost": 0.0
                })
    except Exception as e:
        logger.debug(f"Failed to merge agent models into stats: {e}")

    # Round monetary values to 6 decimal places for precision
    stats["cost_usd"] = round(stats["cost_usd"], 6)
    if "api_cost_usd" in stats:
        stats["api_cost_usd"] = round(stats["api_cost_usd"], 6)

    # If we somehow got a negative cost (shouldn't happen), reset to zero
    if stats["cost_usd"] < 0:
        stats["cost_usd"] = 0.0

    # Add cost breakdown for transparency (what user pays vs API cost)
    # Import locally to avoid circular dependencies
    from modules.credits.pricing import pricing

    stats["cost_breakdown"] = {
        "user_cost_usd": stats["cost_usd"],  # What user pays (with markup)
        "api_cost_usd": stats.get("api_cost_usd", 0),  # What we pay API
        "markup_usd": stats["cost_usd"] - stats.get("api_cost_usd", 0),  # Our markup
        "credits_estimated": int(stats["cost_usd"] / pricing.CREDIT_VALUE_USD)  # Rough credit estimate
    }

    # Log summarized statistics with both user cost and API cost
    logger.info(f"Session stats: {stats['llm_calls']} LLM calls, {stats['total_tokens']} tokens, "
                f"User: ${stats['cost_usd']}, API: ${stats.get('api_cost_usd', 0):.6f}, "
                f"{stats['actions']} actions, processed {len(processed_requests)} unique requests")

    return stats


def _create_request_signature(data: Dict[str, Any]) -> str:
    """Create a unique signature for deduplication.
    
    Args:
        data: LLM request data dictionary
        
    Returns:
        Unique signature string for deduplication
    """
    request_id = data.get("request_id")
    if request_id:
        return f"id:{request_id}"
    
    # Fallback to composite signature for requests without request_id
    model_name = data.get('model_name', 'unknown')
    purpose = data.get('purpose', 'unknown')
    duration = data.get('duration_seconds', 0)
    agent_id = data.get('agent_id', 'unknown')
    component = data.get('component', 'unknown')
    timestamp = data.get('timestamp', 0)
    
    return f"composite:{model_name}|{purpose}|{duration}|{agent_id}|{component}|{timestamp}"


def _process_llm_entry(data: Dict[str, Any], stats: Dict[str, Any], model_usage: Dict) -> None:
    """Process a single LLM entry consistently.

    Args:
        data: LLM request data dictionary
        stats: Statistics dictionary to update
        model_usage: Model usage tracking dictionary
    """
    stats["llm_calls"] += 1

    # Robust token extraction with multiple fallbacks
    token_count = _extract_token_count(data)
    stats["total_tokens"] += token_count

    # CRITICAL FIX: Calculate BOTH API cost and user cost
    # Check if telemetry already has user cost (from new unified tracker)
    if "user_cost_usd" in data.get("parameters", {}):
        # New unified tracker provides full breakdown
        user_cost = data["parameters"]["user_cost_usd"]
        api_cost = data["parameters"].get("api_cost_usd", 0)
        credits = data["parameters"].get("credits_charged", 0)
    else:
        # Legacy mode: calculate from API cost
        api_cost = _extract_or_calculate_cost(data, token_count)
        cost_breakdown = _calculate_user_cost_from_api_cost(api_cost)
        user_cost = cost_breakdown["user_cost_usd"]
        credits = cost_breakdown["credits_charged"]

    # Use USER cost (what they're charged), not API cost
    stats["cost_usd"] += user_cost

    # Track API cost separately for transparency
    if "api_cost_usd" not in stats:
        stats["api_cost_usd"] = 0
    stats["api_cost_usd"] += api_cost

    # Model tracking with user cost
    model_name = _extract_model_name(data)
    if model_name:
        mu = model_usage[model_name]
        mu["count"] += 1
        mu["tokens"] += token_count
        mu["cost"] += user_cost  # User cost, not API cost

        # Track API cost separately for transparency
        if "api_cost" not in mu:
            mu["api_cost"] = 0
        mu["api_cost"] += api_cost


def _extract_token_count(data: Dict[str, Any]) -> int:
    """Extract token count with comprehensive fallback logic.
    
    Args:
        data: LLM request data dictionary
        
    Returns:
        Token count (guaranteed to be >= 0)
    """
    # Try direct token_count field first
    token_count = 0
    if "token_count" in data and data["token_count"] is not None:
        try:
            token_count = int(data["token_count"])
            if token_count > 0:
                return token_count
        except (ValueError, TypeError):
            logger.debug(f"Failed to parse token_count: {data.get('token_count')}")
    
    # Try prompt + completion tokens
    prompt_tokens = 0
    completion_tokens = 0
    
    if "prompt_tokens" in data and data["prompt_tokens"] is not None:
        try:
            prompt_tokens = int(data["prompt_tokens"])
        except (ValueError, TypeError):
            logger.debug(f"Failed to parse prompt_tokens: {data.get('prompt_tokens')}")
    
    if "completion_tokens" in data and data["completion_tokens"] is not None:
        try:
            completion_tokens = int(data["completion_tokens"])
        except (ValueError, TypeError):
            logger.debug(f"Failed to parse completion_tokens: {data.get('completion_tokens')}")
    
    if prompt_tokens > 0 or completion_tokens > 0:
        return prompt_tokens + completion_tokens
    
    # Try total_tokens field as alternative
    if "total_tokens" in data and data["total_tokens"] is not None:
        try:
            token_count = int(data["total_tokens"])
            if token_count > 0:
                return token_count
        except (ValueError, TypeError):
            logger.debug(f"Failed to parse total_tokens: {data.get('total_tokens')}")
    
    # Final fallback: estimate based on model and purpose
    model_name = data.get("model_name", "")
    purpose = data.get("purpose", "")
    duration = data.get("duration_seconds", 0)
    
    if duration > 0:  # Only estimate if we have meaningful duration
        estimated = _estimate_tokens_from_metadata(model_name, purpose, duration)
        if estimated > 0:
            logger.debug(f"Estimated {estimated} tokens for {model_name} {purpose} request")
            return estimated
    
    # Return 0 if all extraction methods failed
    logger.warning(f"No token data found for LLM request: {list(data.keys())}")
    return 0


def _extract_or_calculate_cost(data: Dict[str, Any], token_count: int) -> float:
    """Extract or calculate cost estimate using model registry.

    Args:
        data: LLM request data dictionary
        token_count: Token count for calculation fallback

    Returns:
        Cost estimate in USD
    """
    # Try stored cost_estimate first (may be from old pricing)
    cost_est = data.get("cost_estimate")
    if cost_est is not None:
        try:
            cost_value = float(cost_est)
            if cost_value >= 0:  # Valid cost
                return cost_value
        except (ValueError, TypeError):
            logger.debug(f"Invalid cost_estimate: {cost_est}")

    # Fallback to calculation using model registry
    model_name = data.get("model_name")
    prompt_tokens = data.get("prompt_tokens")
    completion_tokens = data.get("completion_tokens")

    return _calculate_cost_from_registry(
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=token_count
    )


def _extract_model_name(data: Dict[str, Any]) -> str:
    """Extract model name with fallbacks.
    
    Args:
        data: LLM request data dictionary
        
    Returns:
        Model name or inferred name
    """
    # Try various model name fields
    model_name = (data.get("model_name") or 
                 data.get("model") or 
                 data.get("model_id") or 
                 data.get("llm_model"))
    
    if model_name and model_name != "None":
        return model_name
    
    # Fallback to provider-based generic names
    provider = data.get("provider", "").lower()
    if provider == "anthropic":
        return "claude"
    elif provider == "openai":
        return "gpt"
    elif provider == "google":
        return "gemini"
    elif provider in ["llama", "deepseek"]:
        return provider
    
    return "unknown"


def _estimate_tokens_from_metadata(model_name: str, purpose: str, duration: float) -> int:
    """Estimate tokens based on metadata when token data is missing.
    
    Args:
        model_name: Name of the model
        purpose: Purpose of the request
        duration: Duration in seconds
        
    Returns:
        Estimated token count
    """
    # Base estimate
    base_tokens = 1000
    
    # Adjust for purpose
    purpose_lower = purpose.lower()
    if purpose_lower in ["planning", "plan"]:
        base_tokens = 3000
    elif purpose_lower in ["evaluation", "assess", "review"]:
        base_tokens = 2000
    elif purpose_lower in ["research", "analysis"]:
        base_tokens = 4000
    elif purpose_lower in ["next_action", "generate_response"]:
        base_tokens = 1500
    
    # Adjust for duration
    if duration > 20:
        base_tokens = int(base_tokens * 1.8)
    elif duration > 10:
        base_tokens = int(base_tokens * 1.4)
    elif duration > 5:
        base_tokens = int(base_tokens * 1.2)
    elif duration < 2:
        base_tokens = int(base_tokens * 0.7)
    
    # Adjust for model
    model_lower = model_name.lower()
    if "gpt-4" in model_lower:
        base_tokens = int(base_tokens * 1.1)
    elif "claude" in model_lower:
        base_tokens = int(base_tokens * 1.2)
    elif "gemini" in model_lower:
        base_tokens = int(base_tokens * 0.9)
    
    return max(100, base_tokens)  # Minimum 100 tokens 