"""Shared types for the controller and tools.

This module contains types that are produced by tools and consumed by agents.
Placing them here follows the correct dependency direction: agents -> tools.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict


class ActionResult(BaseModel):
    """Result of executing an action.
    
    This is the standard return type for all tool actions.
    Tools produce ActionResults, agents consume them.
    """

    is_done: Optional[bool] = False
    extracted_content: Optional[str] = None
    error: Optional[str] = None
    include_in_memory: bool = False  # whether to include in past messages as context
    file_references: Optional[List[Dict[str, Any]]] = None  # References to files storing large content
    metadata: Optional[Dict[str, Any]] = None  # Generic metadata for extensibility
    # Set by multi_act to the originating action's tool_call_id (identity pairing).
    tool_call_id: Optional[str] = None

    model_config = ConfigDict(slots=True)

