"""MCP → direct-action registration (Item 7H — extracted from ``controller/service.py``).

LLMs consistently fail to use nested ``mcp_execute_tool(arguments={...})``. This
registrar flattens each discovered MCP tool into a direct action named
``{server}_{tool}`` with a dynamic Pydantic param model built from the tool's JSON
input schema — eliminating the nesting problem.

It holds a back-reference to the owning ``Controller`` so ``registry`` and ``logger``
stay live (the Controller delegates ``_register_mcp_tools_as_direct_actions`` /
``_create_param_model_from_schema`` here). The WS-B5 typed-array behaviour (arrays
carry their item type; objects stay lenient ``dict``) is preserved verbatim.
"""
from __future__ import annotations

from typing import Any, Dict, Type

from pydantic import BaseModel


class MCPActionRegistrar:
    """Registers MCP server tools as flat Controller actions + builds param models."""

    def __init__(self, controller: Any) -> None:
        self._controller = controller

    @property
    def registry(self):
        return self._controller.registry

    @property
    def logger(self):
        return self._controller.logger

    async def register(self, mcp_tool: Any) -> None:
        """Register individual MCP tools as direct actions with flat parameter schemas.

        Args:
            mcp_tool: The MCPTool instance with server_manager
        """
        self.logger.info("🔧 _register_mcp_tools_as_direct_actions called")

        if not hasattr(mcp_tool, 'server_manager') or not mcp_tool.server_manager:
            self.logger.warning("MCP tool has no server_manager, cannot register direct actions")
            return

        try:
            # Get all MCP tools from all servers
            all_tools = mcp_tool.server_manager.get_all_tools()
            self.logger.info(f"🔧 Found {len(all_tools)} MCP servers with tools")
            registered_count = 0

            for server_name, tools in all_tools.items():
                for tool_meta in tools:
                    try:
                        # Create namespaced action name: server_toolname
                        action_name = f"{server_name}_{tool_meta.name}"

                        # Skip if already registered (avoid duplicates with mcp_* wrapper actions)
                        if action_name in self.registry.actions:
                            self.logger.debug(f"MCP action '{action_name}' already registered, skipping")
                            continue

                        # Create a dynamic Pydantic model from the tool's input schema
                        param_model = self.create_param_model(
                            action_name,
                            tool_meta.input_schema or {}
                        )

                        # Create an async executor that routes to execute_mcp_tool
                        # Use closure to capture server_name and tool_name
                        def make_executor(srv_name, t_name, mcp_ref):
                            # execution_context is a NAMED param (not **kwargs) so the
                            # registry routes the per-call context here instead of folding
                            # it into the MCP params dict — and so it reaches the per-tenant
                            # rate limiter (was falling back to a shared "global" bucket,
                            # cross-keying tenants).
                            async def executor(params_obj=None, execution_context=None, **kwargs):
                                # Convert Pydantic model to dict if needed
                                if params_obj is not None:
                                    if hasattr(params_obj, 'model_dump'):
                                        params = params_obj.model_dump()
                                    elif isinstance(params_obj, dict):
                                        params = params_obj
                                    else:
                                        params = dict(params_obj) if params_obj else {}
                                else:
                                    params = kwargs
                                # Execute via MCP tool's execute_mcp_tool method
                                action = f"{srv_name}_{t_name}"
                                return await mcp_ref.execute_mcp_tool(
                                    action, params, execution_context=execution_context
                                )
                            return executor

                        executor_func = make_executor(server_name, tool_meta.name, mcp_tool)

                        # Store metadata for routing
                        executor_func._mcp_server = server_name
                        executor_func._mcp_tool = tool_meta.name

                        # Register with the registry
                        self.registry.wrap_function(
                            name=action_name,
                            function=executor_func,
                            description=tool_meta.description or f"Execute {tool_meta.name} on {server_name}",
                            tool='mcp',
                            param_model=param_model
                        )

                        registered_count += 1
                        self.logger.debug(f"Registered MCP action: {action_name}")

                    except Exception as e:
                        self.logger.warning(f"Failed to register MCP tool {server_name}_{tool_meta.name}: {e}")

            self.logger.info(f"✅ Registered {registered_count} MCP tools as direct actions")

        except Exception as e:
            self.logger.error(f"Failed to register MCP tools as direct actions: {e}", exc_info=True)

    def create_param_model(self, action_name: str, schema: Dict[str, Any]) -> Type[BaseModel]:
        """Create a dynamic Pydantic model from a JSON schema.

        Args:
            action_name: Name for the model
            schema: JSON schema dict with properties, required, etc.

        Returns:
            Dynamically created Pydantic BaseModel subclass
        """
        from pydantic import create_model, Field
        from typing import Optional, List, Any as TypingAny

        properties = schema.get('properties', {})
        required = set(schema.get('required', []))

        # Build field definitions
        fields = {}
        for prop_name, prop_schema in properties.items():
            prop_type = prop_schema.get('type', 'string')
            description = prop_schema.get('description', '')
            default = prop_schema.get('default', ...)

            # Map JSON schema types to Python types
            type_mapping = {
                'string': str,
                'integer': int,
                'number': float,
                'boolean': bool,
                'array': list,
                'object': dict,
            }

            python_type = type_mapping.get(prop_type, TypingAny)

            # WS-B5: preserve array item type (List[item]) instead of bare list.
            # Objects stay as dict (lenient) so previously-valid payloads with
            # unforeseen keys are never rejected.
            if prop_type == 'array':
                items_schema = prop_schema.get('items') or {}
                item_type = type_mapping.get(items_schema.get('type'), TypingAny)
                python_type = List[item_type]

            # Handle nullable types (anyOf with null)
            if 'anyOf' in prop_schema:
                for option in prop_schema['anyOf']:
                    if option.get('type') == 'null':
                        python_type = Optional[python_type]
                        break

            # Set default based on whether field is required
            if prop_name in required:
                field_default = ...  # Required
            elif default != ...:
                field_default = default
            else:
                field_default = None
                python_type = Optional[python_type] if python_type != Optional else python_type

            # Create field with description
            fields[prop_name] = (python_type, Field(default=field_default, description=description))

        # Create dynamic model
        model_name = f"MCP_{action_name.replace('-', '_').title().replace('_', '')}Params"

        try:
            return create_model(model_name, **fields)
        except Exception as e:
            self.logger.warning(f"Failed to create param model for {action_name}: {e}, using empty model")
            return create_model(model_name)
