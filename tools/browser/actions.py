"""Browser action parameter models.

This module contains all Pydantic models for browser tool actions.
"""

from typing import List, Literal, Optional
from pydantic import AliasChoices, BaseModel, model_validator, Field, field_validator, ConfigDict


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
	# None = the default, so a plain click stays `{index}` in the step history.
	button: Optional[Literal['left', 'middle', 'right']] = Field(
		default=None, description='The mouse button (default left); "right" opens a context menu')
	double: Optional[bool] = Field(default=None, description='true = double-click')

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
	clear: Optional[bool] = Field(
		default=None, description='Default true: replace what the element holds. false adds the text at the end')
	submit: Optional[bool] = Field(default=None, description='true = press Enter after the text')

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
	"""Model for scrolling the page, or one scrollable area of it."""
	model_config = ConfigDict(populate_by_name=True)

	direction: str = "down"  # Direction to scroll: up, down, left, right
	amount: Optional[int] = None  # The number of pixels to scroll. If None, uses default scroll amount
	index: Optional[int] = Field(
		default=None, alias='elementIndex',
		description='Scroll the scrollable area that holds this element (a list, a dialog) and not the page')


class SendKeysAction(BaseModel):
	"""Model for sending keyboard keys."""
	keys: str = Field(description='A key ("Enter"), a shortcut ("Control+A") or a sequence ("Tab Tab Enter")')


class _IndexedAction(BaseModel):
	"""An action on one element of the page state."""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	index: int = Field(alias='elementIndex')

	@field_validator('index', mode='before')
	@classmethod
	def coerce_index(cls, v):
		if isinstance(v, str) and v.strip().isdigit():
			return int(v)
		return v


class ElementIndexAction(_IndexedAction):
	"""Model for an action that only names its element."""
	xpath: Optional[str] = Field(default=None, alias='xPath')


class SelectOptionAction(_IndexedAction):
	"""Model for selecting one option of a dropdown."""
	text: str = Field(alias='inputText', description='The option as the dropdown shows it (or its value)')
	xpath: Optional[str] = Field(default=None, alias='xPath')


class SetCheckedAction(_IndexedAction):
	"""Model for setting a checkbox, a radio button or a switch."""
	checked: bool = Field(default=True, description='true to tick, false to clear')


class TypeTextAction(BaseModel):
	"""Model for typing text into the element that has the focus."""
	model_config = ConfigDict(extra='forbid')

	text: str = Field(description='The text; a line break becomes a new line, not a submit')
	submit: Optional[bool] = Field(default=None, description='true = press Enter after the text')


class UploadFileAction(BaseModel):
	"""Model for attaching workspace files to a file field."""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	paths: List[str] = Field(description='Workspace paths of the files to attach')
	index: Optional[int] = Field(
		default=None, alias='elementIndex',
		description='The file field, or its upload button or drop area. Not needed when the page has one file field')

	@field_validator('paths', mode='before')
	@classmethod
	def one_path_is_a_list(cls, v):
		return [v] if isinstance(v, str) else v


class DragAndDropAction(BaseModel):
	"""Model for dragging one element onto another."""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	from_index: int = Field(alias='fromIndex', description='The element to drag')
	to_index: int = Field(alias='toIndex', description='The element to drop it on')


class ClickAtAction(BaseModel):
	"""Model for clicking a point of the viewport."""
	model_config = ConfigDict(extra='forbid')

	x: int = Field(description='Pixels from the left edge of the viewport')
	y: int = Field(description='Pixels from the top edge of the viewport')
	button: Optional[Literal['left', 'middle', 'right']] = None
	double: Optional[bool] = None


class WaitAction(BaseModel):
	"""Model for waiting for time or for a text."""
	model_config = ConfigDict(extra='forbid')

	seconds: Optional[float] = Field(default=None, description='How long to wait (the limit when a text is given); at most 30')
	text: Optional[str] = Field(default=None, description='Wait until this text is on the page')


class CloseTabAction(BaseModel):
	"""Model for closing a tab."""
	model_config = ConfigDict(extra='forbid', populate_by_name=True)

	page_id: Optional[int] = Field(default=None, alias='pageId', description='The tab to close; default: the current tab')


class EvaluateScriptAction(BaseModel):
	"""Model for running JavaScript in the page."""
	model_config = ConfigDict(extra='forbid')

	script: str = Field(description='An expression, or an arrow function: () => document.title')


class DialogAnswerAction(BaseModel):
	"""Model for the answer to the next alert, confirm or prompt."""
	model_config = ConfigDict(extra='forbid')

	accept: bool = Field(default=True, description='true = OK, false = Cancel')
	prompt_text: Optional[str] = Field(default=None, description='The text to enter in a prompt dialog')


class ScrollToTextAction(BaseModel):
	"""Model for scrolling to a text on the page. ``keys`` (the old shared
	SendKeysAction field) still validates, so a replayed call keeps working."""
	text: str = Field(validation_alias=AliasChoices('text', 'keys'),
					  description='The visible text to scroll to (a part of it is enough)')


class ScreenshotAction(BaseModel):
	"""Model for saving a screenshot of the current page into the workspace."""
	model_config = ConfigDict(extra='forbid')

	name: Optional[str] = Field(
		default=None,
		description='File name without a folder (".png" is added); default: a timestamp')
	full_page: bool = Field(default=False, description='Capture the whole page, not only the viewport')


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
