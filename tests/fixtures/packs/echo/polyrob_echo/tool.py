"""The ``echo`` tool: one read-only action, ``echo_say``."""
from pydantic import BaseModel

from tools.base_tool import BaseTool


class EchoParams(BaseModel):
    text: str


class EchoTool(BaseTool):
    @BaseTool.action("Echo the text back", param_model=EchoParams)
    async def echo_say(self, params: EchoParams, execution_context=None):
        return self._ar(content=params.text)


def register_echo_tool(force: bool = False) -> bool:
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    return register_optional_tool(
        "echo", EchoTool,
        ToolDescriptor(name="echo", description="Echo text back (fixture pack)",
                       category=ToolCategory.INTEGRATION, is_optional=True, init_priority=90),
        lambda: True, force=force)
