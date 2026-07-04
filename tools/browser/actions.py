"""Browser action parameter models.

This module contains all Pydantic models for browser tool actions.
"""

from typing import Optional
from pydantic import BaseModel, model_validator, Field, field_validator, ConfigDict


class SearchGoogleAction(BaseModel):
	"""Model for searching Google with a query."""
	query: str


class GoToUrlAction(BaseModel):
	"""Model for navigating to a URL."""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	url: str = Field(alias='URL')

	@field_validator('url')
	@classmethod
	def normalize_url(cls, v):
		if isinstance(v, str):
			v = v.strip()
			# Add protocol if missing
			if not v.startswith(('http://', 'https://')):
				v = 'https://' + v
		return v


class ClickElementAction(BaseModel):
	"""Model for clicking an element on the page."""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	index: int = Field(alias='elementIndex')
	xpath: Optional[str] = Field(default=None, alias='xPath')

	@field_validator('index')
	@classmethod
	def coerce_index(cls, v):
		if isinstance(v, str) and v.isdigit():
			return int(v)
		return v


class InputTextAction(BaseModel):
	"""Model for inputting text into an element."""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	index: int = Field(alias='elementIndex')
	text: str = Field(alias='inputText')
	xpath: Optional[str] = Field(default=None, alias='xPath')

	@field_validator('index')
	@classmethod
	def coerce_index(cls, v):
		if isinstance(v, str) and v.isdigit():
			return int(v)
		return v

	@field_validator('text')
	@classmethod
	def normalize_text(cls, v):
		return str(v) if v is not None else ""


class SwitchTabAction(BaseModel):
	"""Model for switching to a different browser tab."""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	page_id: int = Field(alias='pageId')

	@field_validator('page_id')
	@classmethod
	def coerce_page_id(cls, v):
		if isinstance(v, str) and v.isdigit():
			return int(v)
		return v


class OpenTabAction(BaseModel):
	"""Model for opening a new browser tab."""
	url: str


class ScrollAction(BaseModel):
	"""Model for scrolling the page."""
	direction: str = "down"  # Direction to scroll: up, down, left, right
	amount: Optional[int] = None  # The number of pixels to scroll. If None, uses default scroll amount


class SendKeysAction(BaseModel):
	"""Model for sending keyboard keys."""
	keys: str


class NoParamsAction(BaseModel):
	"""Model that accepts and discards all parameters.

	Accepts absolutely anything in the incoming data
	and discards it, so the final parsed model is empty.
	"""
	model_config = ConfigDict(extra='forbid')

	@model_validator(mode='before')
	def ignore_all_inputs(cls, values):
		# No matter what the user sends, discard it and return empty.
		return {}


class ExtractPageContentAction(NoParamsAction):
	"""Model for extracting content from the current page.

	This action takes no parameters - it extracts content from the current page.
	The model extends NoParamsAction to properly handle parameter-less actions.
	"""
	pass
