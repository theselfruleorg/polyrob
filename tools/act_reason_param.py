"""The ``why`` param a write verb carries (proposal 122 W1).

A verb's param model inherits :class:`ActReason`; the Controller binds the value for the
action in flight (``core.act_reason``), and the money guard, the money notice and the act
record read it from there. The rule and its text live in ``core.act_reason``.
"""
from typing import Optional

from pydantic import BaseModel, Field

from core.act_reason import WHY_DESCRIPTION


class ActReason(BaseModel):
    why: Optional[str] = Field(None, description=WHY_DESCRIPTION)


__all__ = ["ActReason"]
