"""Pre-validation cleanup of tool-call args (one rule, no guessing).

A model sometimes restates the tool it is calling as an ``action`` arg —
``twitter_whoami(action='whoami')`` — against a param model that forbids extras.
That arg carries no information, so it is dropped. Only when the param model does
NOT declare ``action`` and the value names the tool itself (its full name or its
last ``_``-token, case-insensitive); anything else still fails validation.
"""
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel


def drop_redundant_action_arg(tool_name: str, param_model: Optional[Type[BaseModel]],
                              args: Dict[str, Any]) -> Dict[str, Any]:
	if not isinstance(args, dict) or "action" not in args or param_model is None:
		return args
	if "action" in getattr(param_model, "model_fields", {}):
		return args
	value = str(args["action"] or "").strip().lower()
	name = (tool_name or "").lower()
	if value and (value == name or value == name.rsplit("_", 1)[-1]):
		return {k: v for k, v in args.items() if k != "action"}
	return args
