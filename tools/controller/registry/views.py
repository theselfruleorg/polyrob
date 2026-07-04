from typing import Callable, Dict, Type, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class RegisteredAction(BaseModel):
    """Model for a registered action.

    SINGULAR RESPONSIBILITY: Store action metadata and callable.

    Attributes:
        name: Action name
        description: Human-readable description
        function: Callable to execute
        param_model: Pydantic model for parameters
        tool: The tool this action belongs to (REQUIRED)
    """

    name: str
    description: str
    function: Callable
    param_model: Type[BaseModel]
    tool: Optional[str] = None  # Tool this action belongs to

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def prompt_description(self) -> str:
        """Get a description of the action for the prompt"""
        skip_keys = ['title']
        s = f'{self.description}: \n'
        s += '{' + str(self.name) + ': '
        s += str(
            {
                k: {sub_k: sub_v for sub_k, sub_v in v.items() if sub_k not in skip_keys}
                for k, v in self.param_model.model_json_schema()['properties'].items()
            }
        )
        s += '}'
        return s


class ActionModel(BaseModel):
    """Base model for dynamically created action models"""

    # this will have all the registered actions, e.g.
    # click_element = param_model = ClickElementParams
    # done = param_model = None
    #
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Originating tool_call_id, threaded from the LLM tool call so results can be
    # paired back to the correct call by IDENTITY (not list position) after the
    # executor reorders/truncates actions. PrivateAttr so it stays OUT of
    # model_dump() — dispatch reads the action name from model_dump keys.
    _tool_call_id: Optional[str] = PrivateAttr(default=None)

    def get_index(self) -> int | None:
        """Get the index of the action"""
        # {'clicked_element': {'index':5}}
        params = self.model_dump(exclude_unset=True).values()
        if not params:
            return None
        for param in params:
            if param is not None and 'index' in param:
                return param['index']
        return None

    def set_index(self, index: int):
        """Overwrite the index of the action"""
        # Get the action name and params
        action_data = self.model_dump(exclude_unset=True)
        action_name = next(iter(action_data.keys()))
        action_params = getattr(self, action_name)

        # Update the index directly on the model
        if hasattr(action_params, 'index'):
            action_params.index = index


class ActionRegistry(BaseModel):
    """Model representing the action registry"""

    # Use default_factory to avoid shared mutable default across instances
    actions: Dict[str, RegisteredAction] = Field(default_factory=dict)

    def get_prompt_description(self) -> str:
        """Get a description of all actions for the prompt, organized by tool"""
        # Group actions by tool
        tools = {}
        
        # First add actions to their tool groups
        for action in self.actions.values():
            tool_name = action.tool or "default"
            if tool_name not in tools:
                tools[tool_name] = []
            tools[tool_name].append(action)
        
        # Build the description with tool grouping
        descriptions = []
        
        # Start with default tool if it exists
        if "default" in tools:
            descriptions.append("General Actions:")
            for action in tools["default"]:
                descriptions.append(action.prompt_description())
            descriptions.append("")  # Empty line for separation
        
        # Then add all other tools
        for tool_name, actions in sorted(tools.items()):
            if tool_name == "default":
                continue  # Already handled
            
            descriptions.append(f"{tool_name.capitalize()} Tool Actions:")
            for action in actions:
                descriptions.append(action.prompt_description())
            descriptions.append("")  # Empty line for separation
        
        return '\n'.join(descriptions)
