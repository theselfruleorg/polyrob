from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Dict, List, Optional

from tools.dom.history_tree_processor.view import CoordinateSet, HashedDomElement, ViewportInfo

# Avoid circular import issues
if TYPE_CHECKING:
	from .views import DOMElementNode


@dataclass(frozen=False)
class DOMBaseNode:
	is_visible: bool
	# Use None as default and set parent later to avoid circular reference issues
	parent: Optional['DOMElementNode']


@dataclass(frozen=False)
class DOMTextNode(DOMBaseNode):
	text: str
	type: str = 'TEXT_NODE'

	def has_parent_with_highlight_index(self) -> bool:
		try:
			current = self.parent
			# Set a maximum depth to avoid infinite loops
			max_depth = 100
			depth = 0
			
			while current is not None and depth < max_depth:
				if hasattr(current, 'highlight_index') and current.highlight_index is not None:
					return True
				current = current.parent if hasattr(current, 'parent') else None
				depth += 1
				
			return False
		except Exception as e:
			# If anything fails, assume no parent with highlight index
			return False


@dataclass(frozen=False)
class DOMElementNode(DOMBaseNode):
	"""
	xpath: the xpath of the element from the last root node (shadow root or iframe OR document if no shadow root or iframe).
	To properly reference the element we need to recursively switch the root node until we find the element (work you way up the tree with `.parent`)
	"""

	tag_name: str
	xpath: str
	attributes: Dict[str, str]
	children: List[DOMBaseNode]
	is_interactive: bool = False
	is_top_element: bool = False
	shadow_root: bool = False
	highlight_index: Optional[int] = None
	viewport_coordinates: Optional[CoordinateSet] = None
	page_coordinates: Optional[CoordinateSet] = None
	viewport_info: Optional[ViewportInfo] = None

	@classmethod
	def empty(cls):
		"""Create an empty DOMElementNode instance."""
		return cls(
			tag_name='html',
			xpath='/',
			attributes={},
			children=[],
			is_visible=False,
			parent=None,
			is_interactive=False,
			is_top_element=False,
			shadow_root=False
		)

	def __repr__(self) -> str:
		tag_str = f'<{self.tag_name}'

		# Add attributes
		for key, value in self.attributes.items():
			tag_str += f' {key}="{value}"'
		tag_str += '>'

		# Add extra info
		extras = []
		if self.is_interactive:
			extras.append('interactive')
		if self.is_top_element:
			extras.append('top')
		if self.shadow_root:
			extras.append('shadow-root')
		if self.highlight_index is not None:
			extras.append(f'highlight:{self.highlight_index}')

		if extras:
			tag_str += f' [{", ".join(extras)}]'

		return tag_str

	@cached_property
	def hash(self) -> HashedDomElement:
		try:
			# Only import this at function level to avoid circular import issues
			from tools.dom.history_tree_processor.service import HistoryTreeProcessor
			
			# Ensure we have all the components needed for hashing
			# First check that we have access to the HistoryTreeProcessor class
			if not hasattr(HistoryTreeProcessor, '_hash_dom_element'):
				raise AttributeError("HistoryTreeProcessor._hash_dom_element not available")
				
			# Call the hash function with defensive error handling
			return HistoryTreeProcessor._hash_dom_element(self)
			
		except (ImportError, AttributeError, NotImplementedError) as e:
			# If we encounter any import or attribute error, use a local fallback implementation
			import hashlib
			import logging
			
			logger = logging.getLogger('dom.views')
			logger.warning(f"Using fallback hash implementation due to: {str(e)}")
			
			# Generate fallback hash components
			try:
				# Hash the tag name
				tag_name = self.tag_name or "unknown"
				tag_hash = hashlib.sha256(tag_name.encode()).hexdigest()[:16]
				
				# Hash the xpath or use a placeholder
				xpath = self.xpath or ""
				xpath_hash = hashlib.sha256(xpath.encode()).hexdigest()[:16]
				
				# Hash attributes or use a placeholder
				attr_string = ""
				if hasattr(self, 'attributes') and self.attributes:
					for k, v in self.attributes.items():
						if k and v:  # Only use non-empty keys/values
							attr_string += f"{k}={v};"
				attr_hash = hashlib.sha256(attr_string.encode()).hexdigest()[:16]
				
				# Import HashedDomElement dynamically to avoid circular imports
				try:
					from tools.dom.history_tree_processor.view import HashedDomElement
					return HashedDomElement(
						branch_path_hash=tag_hash,
						attributes_hash=attr_hash,
						xpath_hash=xpath_hash
					)
				except ImportError:
					# If we can't even import HashedDomElement, create a minimal class
					class FallbackHash:
						def __init__(self, branch_path_hash, attributes_hash, xpath_hash):
							self.branch_path_hash = branch_path_hash
							self.attributes_hash = attributes_hash
							self.xpath_hash = xpath_hash
							
					return FallbackHash(
						branch_path_hash=tag_hash, 
						attributes_hash=attr_hash, 
						xpath_hash=xpath_hash
					)
					
			except Exception as nested_error:
				# Last resort fallback for any unexpected errors
				logger.error(f"Fallback hash implementation failed: {str(nested_error)}")
				
				# Create a minimal valid object with random but consistent hashes
				import random
				random.seed(id(self))  # Use object id for consistent randomness
				
				class EmergencyHash:
					def __init__(self):
						# Generate unique but reproducible hashes
						self.branch_path_hash = hex(random.getrandbits(64))[2:]
						self.attributes_hash = hex(random.getrandbits(64))[2:]
						self.xpath_hash = hex(random.getrandbits(64))[2:]
						
				return EmergencyHash()
		except Exception as e:
			# Absolute last resort
			import logging
			logging.error(f"Critical error in hash property: {str(e)}")
			
			# Return minimal object that won't break comparisons
			class CriticalFallbackHash:
				def __init__(self):
					self.branch_path_hash = "error"
					self.attributes_hash = "error"
					self.xpath_hash = "error"
					
				def __eq__(self, other):
					return False
				
			return CriticalFallbackHash()

	def get_all_text_till_next_clickable_element(self, max_depth: int = -1) -> str:
		try:
			text_parts = []

			def collect_text(node: DOMBaseNode, current_depth: int) -> None:
				try:
					if max_depth != -1 and current_depth > max_depth:
						return

					# Skip this branch if we hit a highlighted element (except for the current node)
					if isinstance(node, DOMElementNode) and node != self and node.highlight_index is not None:
						return

					if isinstance(node, DOMTextNode):
						if hasattr(node, 'text') and node.text:
							text_parts.append(node.text)
					elif isinstance(node, DOMElementNode):
						for child in node.children:
							if child is not None:  # Make sure child exists
								collect_text(child, current_depth + 1)
				except Exception as e:
					# If any node processing fails, just continue
					pass

			collect_text(self, 0)
			return '\n'.join(text_parts).strip()
		except Exception as e:
			# If anything fails, return minimal text representation
			return f"[{self.tag_name}]" if hasattr(self, 'tag_name') else ""

	def clickable_elements_to_string(self, include_attributes: list[str] = []) -> str:
		"""Convert the processed DOM content to HTML."""
		try:
			formatted_text = []

			def process_node(node: DOMBaseNode, depth: int) -> None:
				try:
					if isinstance(node, DOMElementNode):
						# Add element with highlight_index
						if node.highlight_index is not None:
							attributes_str = ''
							if include_attributes:
								try:
									attributes_str = ' ' + ' '.join(
										f'{key}="{value}"' 
										for key, value in node.attributes.items() 
										if key in include_attributes and key and value
									)
								except Exception:
									# If attributes processing fails, use empty string
									attributes_str = ''
									
							# Safely get text content
							try:
								element_text = node.get_all_text_till_next_clickable_element()
							except Exception:
								element_text = f"[Element {node.tag_name}]"
								
							formatted_text.append(
								f'[{node.highlight_index}]<{node.tag_name}{attributes_str}>{element_text}</{node.tag_name}>'
							)

						# Process children regardless
						if hasattr(node, 'children'):
							for child in node.children:
								if child is not None:  # Safety check
									process_node(child, depth + 1)

					elif isinstance(node, DOMTextNode):
						# Add text only if it doesn't have a highlighted parent
						try:
							if not node.has_parent_with_highlight_index():
								text = node.text if hasattr(node, 'text') and node.text else ""
								if text.strip():  # Only add non-empty text
									formatted_text.append(f'[]{text}')
						except Exception:
							# If parent check fails, just add the text
							if hasattr(node, 'text') and node.text and node.text.strip():
								formatted_text.append(f'[]{node.text}')
				except Exception as e:
					# If processing a node fails, add an error placeholder
					formatted_text.append(f'[Error processing node]')

			process_node(self, 0)
			return '\n'.join(formatted_text)
		except Exception as e:
			# If the entire method fails, return a minimal representation
			return f"[DOM tree with {len(self.children) if hasattr(self, 'children') else 0} elements]"

	def get_file_upload_element(self, check_siblings: bool = True) -> Optional['DOMElementNode']:
		# Check if current element is a file input
		if self.tag_name == 'input' and self.attributes.get('type') == 'file':
			return self

		# Check children
		for child in self.children:
			if isinstance(child, DOMElementNode):
				result = child.get_file_upload_element(check_siblings=False)
				if result:
					return result

		# Check siblings only for the initial call
		if check_siblings and self.parent:
			for sibling in self.parent.children:
				if sibling is not self and isinstance(sibling, DOMElementNode):
					result = sibling.get_file_upload_element(check_siblings=False)
					if result:
						return result

		return None

	def get_advanced_css_selector(self) -> str:
		try:
			# Try to import from the correct module path first
			from tools.browser.context import BrowserContext
			return BrowserContext._enhanced_css_selector_for_element(self)
		except (ImportError, NotImplementedError, AttributeError) as e:
			# Provide a fallback selector generation method
			selector_parts = []
			
			# Use tag name as base
			selector_parts.append(self.tag_name)
			
			# Add id if available
			if 'id' in self.attributes and self.attributes['id']:
				selector_parts.append(f"#{self.attributes['id']}")
			
			# Add some classes if available
			if 'class' in self.attributes and self.attributes['class']:
				classes = self.attributes['class'].split()
				for cls in classes[:2]:  # Just use first two classes to avoid too complex selectors
					if cls:
						selector_parts.append(f".{cls}")
			
			# Combine parts
			fallback_selector = ''.join(selector_parts)
			
			# Add context from xpath if available
			if not fallback_selector and self.xpath:
				fallback_selector = f"css-fallback-from-xpath-{hash(self.xpath)}"
			
			return fallback_selector or f"{self.tag_name}-{hash(str(self.attributes))}"


SelectorMap = dict[int, DOMElementNode]


@dataclass
class DOMState:
	element_tree: DOMElementNode
	selector_map: SelectorMap
