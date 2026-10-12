from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel


@dataclass
class HashedDomElement:
	"""
	Hash of the dom element to be used as a unique identifier
	"""

	branch_path_hash: str
	attributes_hash: str
	xpath_hash: str
	# text_hash: str

	def __eq__(self, other):
		"""Compare hashes safely, handling potential attribute errors or type differences"""
		try:
			# Check if other is the same class
			if not isinstance(other, HashedDomElement):
				return False
				
			# Compare all hash fields with safety checks
			return (
				getattr(self, 'branch_path_hash', '') == getattr(other, 'branch_path_hash', '') and
				getattr(self, 'attributes_hash', '') == getattr(other, 'attributes_hash', '') and
				getattr(self, 'xpath_hash', '') == getattr(other, 'xpath_hash', '')
			)
		except Exception:
			# If any comparison fails, consider them unequal
			return False
	
	def __hash__(self):
		"""Make hashable to support use in sets and as dictionary keys"""
		try:
			return hash((
				getattr(self, 'branch_path_hash', ''),
				getattr(self, 'attributes_hash', ''),
				getattr(self, 'xpath_hash', '')
			))
		except Exception:
			# Return a constant hash if anything fails
			return hash(id(self))


class Coordinates(BaseModel):
	x: int
	y: int


class CoordinateSet(BaseModel):
	top_left: Coordinates
	top_right: Coordinates
	bottom_left: Coordinates
	bottom_right: Coordinates
	center: Coordinates
	width: int
	height: int


class ViewportInfo(BaseModel):
	scroll_x: int
	scroll_y: int
	width: int
	height: int


@dataclass
class DOMHistoryElement:
	tag_name: str
	xpath: str
	highlight_index: Optional[int]
	entire_parent_branch_path: list[str]
	attributes: dict[str, str]
	shadow_root: bool = False
	css_selector: Optional[str] = None
	page_coordinates: Optional[CoordinateSet] = None
	viewport_coordinates: Optional[CoordinateSet] = None
	viewport_info: Optional[ViewportInfo] = None

	def to_dict(self) -> dict:
		page_coordinates = self.page_coordinates.model_dump() if self.page_coordinates else None
		viewport_coordinates = self.viewport_coordinates.model_dump() if self.viewport_coordinates else None
		viewport_info = self.viewport_info.model_dump() if self.viewport_info else None

		return {
			'tag_name': self.tag_name,
			'xpath': self.xpath,
			'highlight_index': self.highlight_index,
			'entire_parent_branch_path': self.entire_parent_branch_path,
			'attributes': self.attributes,
			'shadow_root': self.shadow_root,
			'css_selector': self.css_selector,
			'page_coordinates': page_coordinates,
			'viewport_coordinates': viewport_coordinates,
			'viewport_info': viewport_info,
		}
