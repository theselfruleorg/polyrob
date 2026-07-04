import hashlib
from dataclasses import dataclass
from typing import Optional

from tools.dom.history_tree_processor.view import DOMHistoryElement, HashedDomElement
from tools.dom.views import DOMElementNode


class HistoryTreeProcessor:
	""" "
	Operations on the DOM elements

	@dev be careful - text nodes can change even if elements stay the same
	"""

	@staticmethod
	def convert_dom_element_to_history_element(dom_element: DOMElementNode) -> DOMHistoryElement:
		try:
			parent_branch_path = HistoryTreeProcessor._get_parent_branch_path(dom_element)
			# Safely try to get the CSS selector with error handling
			try:
				css_selector = dom_element.get_advanced_css_selector()
			except Exception as e:
				# If CSS selector generation fails, create a simple fallback
				css_selector = f"{dom_element.tag_name}_{hash(dom_element.xpath)}"
			
			return DOMHistoryElement(
				dom_element.tag_name,
				dom_element.xpath,
				dom_element.highlight_index,
				parent_branch_path,
				dom_element.attributes,
				dom_element.shadow_root,
				css_selector=css_selector,
				page_coordinates=dom_element.page_coordinates,
				viewport_coordinates=dom_element.viewport_coordinates,
				viewport_info=dom_element.viewport_info,
			)
		except Exception as e:
			# Create a minimal history element as fallback if anything fails
			default_branch_path = ["html", "body"] if dom_element.tag_name.lower() != "html" else ["html"]
			return DOMHistoryElement(
				tag_name=dom_element.tag_name or "div",
				xpath=dom_element.xpath or "",
				highlight_index=dom_element.highlight_index,
				entire_parent_branch_path=default_branch_path,
				attributes=dom_element.attributes or {},
				shadow_root=False,
				css_selector=f"fallback-{hash(str(dom_element))}",
				page_coordinates=None,
				viewport_coordinates=None,
				viewport_info=None
			)

	@staticmethod
	def find_history_element_in_tree(dom_history_element: DOMHistoryElement, tree: DOMElementNode) -> Optional[DOMElementNode]:
		hashed_dom_history_element = HistoryTreeProcessor._hash_dom_history_element(dom_history_element)

		def process_node(node: DOMElementNode):
			if node.highlight_index is not None:
				hashed_node = HistoryTreeProcessor._hash_dom_element(node)
				if hashed_node == hashed_dom_history_element:
					return node
			for child in node.children:
				if isinstance(child, DOMElementNode):
					result = process_node(child)
					if result is not None:
						return result
			return None

		return process_node(tree)

	@staticmethod
	def compare_history_element_and_dom_element(dom_history_element: DOMHistoryElement, dom_element: DOMElementNode) -> bool:
		hashed_dom_history_element = HistoryTreeProcessor._hash_dom_history_element(dom_history_element)
		hashed_dom_element = HistoryTreeProcessor._hash_dom_element(dom_element)

		return hashed_dom_history_element == hashed_dom_element

	@staticmethod
	def _hash_dom_history_element(dom_history_element: DOMHistoryElement) -> HashedDomElement:
		branch_path_hash = HistoryTreeProcessor._parent_branch_path_hash(dom_history_element.entire_parent_branch_path)
		attributes_hash = HistoryTreeProcessor._attributes_hash(dom_history_element.attributes)
		xpath_hash = HistoryTreeProcessor._xpath_hash(dom_history_element.xpath)

		return HashedDomElement(branch_path_hash, attributes_hash, xpath_hash)

	@staticmethod
	def _hash_dom_element(dom_element: DOMElementNode) -> HashedDomElement:
		"""Hash a DOM element with comprehensive error handling"""
		try:
			# Get parent branch path with error handling
			try:
				parent_branch_path = HistoryTreeProcessor._get_parent_branch_path(dom_element)
			except Exception as e:
				import logging
				logging.warning(f"Error getting parent branch path: {str(e)}")
				# Use a fallback path based on tag name
				parent_branch_path = ["html", dom_element.tag_name] if hasattr(dom_element, "tag_name") else ["html", "unknown"]
				
			# Get branch path hash with error handling
			try: 
				branch_path_hash = HistoryTreeProcessor._parent_branch_path_hash(parent_branch_path)
			except Exception as e:
				import logging
				logging.warning(f"Error hashing branch path: {str(e)}")
				# Use a fallback hash from the path or a constant
				import hashlib
				path_str = "/".join(parent_branch_path)
				branch_path_hash = hashlib.sha256(path_str.encode()).hexdigest()
				
			# Get attributes hash with error handling
			try:
				attributes = dom_element.attributes if hasattr(dom_element, "attributes") else {}
				attributes_hash = HistoryTreeProcessor._attributes_hash(attributes)
			except Exception as e:
				import logging
				logging.warning(f"Error hashing attributes: {str(e)}")
				# Use a fallback hash from stringified attributes or a constant
				import hashlib
				attr_str = str(getattr(dom_element, "attributes", {}))
				attributes_hash = hashlib.sha256(attr_str.encode()).hexdigest()
				
			# Get xpath hash with error handling
			try:
				xpath = dom_element.xpath if hasattr(dom_element, "xpath") else ""
				xpath_hash = HistoryTreeProcessor._xpath_hash(xpath)
			except Exception as e:
				import logging
				logging.warning(f"Error hashing xpath: {str(e)}")
				# Use a fallback hash from the xpath or a constant
				import hashlib
				xpath_str = str(getattr(dom_element, "xpath", ""))
				xpath_hash = hashlib.sha256(xpath_str.encode()).hexdigest()
			
			# Create a HashedDomElement with the computed hashes
			return HashedDomElement(branch_path_hash, attributes_hash, xpath_hash)
		except Exception as e:
			# Log the error and create a minimal valid hash as a fallback
			import logging
			logging.error(f"Critical error in _hash_dom_element: {str(e)}")
			
			# Create minimal valid hash values based on element ID to ensure uniqueness
			import hashlib
			element_id = str(id(dom_element))
			fallback_hash = hashlib.sha256(element_id.encode()).hexdigest()
			
			# Split the hash into three parts for the three hash fields
			third = len(fallback_hash) // 3
			return HashedDomElement(
				fallback_hash[:third], 
				fallback_hash[third:2*third], 
				fallback_hash[2*third:]
			)

	@staticmethod
	def _get_parent_branch_path(dom_element: DOMElementNode) -> list[str]:
		try:
			parents: list[DOMElementNode] = []
			current_element: DOMElementNode = dom_element
			
			# Safety check - limit iterations to prevent infinite loops
			iteration_limit = 100
			iteration_count = 0
			
			while current_element.parent is not None and iteration_count < iteration_limit:
				parents.append(current_element)
				current_element = current_element.parent
				iteration_count += 1
				
			if iteration_count >= iteration_limit:
				# If we hit the limit, it could be a circular reference
				# Return just the direct path we have so far
				return [parent.tag_name for parent in parents[-10:]] if parents else []

			parents.reverse()
			return [parent.tag_name for parent in parents]
		except Exception as e:
			# If anything goes wrong, return a minimal valid path
			return ["html", "body"] if dom_element.tag_name.lower() != "html" else ["html"]

	@staticmethod
	def _parent_branch_path_hash(parent_branch_path: list[str]) -> str:
		try:
			# If list is empty, use a default path
			if not parent_branch_path:
				return hashlib.sha256("default/path".encode()).hexdigest()
			
			# Join with extra safety for invalid data
			safe_path = []
			for item in parent_branch_path:
				if item is not None and isinstance(item, str):
					safe_path.append(item)
				else:
					safe_path.append("unknown")
				
			parent_branch_path_string = '/'.join(safe_path)
			return hashlib.sha256(parent_branch_path_string.encode()).hexdigest()
		except Exception as e:
			# Fallback hash if anything fails
			return hashlib.sha256("error".encode()).hexdigest()

	@staticmethod
	def _attributes_hash(attributes: dict[str, str]) -> str:
		try:
			if not attributes:
				return hashlib.sha256("empty_attributes".encode()).hexdigest()
			
			# Handle attributes safely
			safe_attrs = []
			for key, value in attributes.items():
				if key is not None and value is not None:
					# Convert any non-string values to strings safely
					if not isinstance(key, str):
						key = str(key)
					if not isinstance(value, str):
						value = str(value)
					safe_attrs.append(f'{key}={value}')
				
			attributes_string = ''.join(safe_attrs)
			return hashlib.sha256(attributes_string.encode()).hexdigest()
		except Exception as e:
			# Fallback hash if anything fails
			return hashlib.sha256("error_attributes".encode()).hexdigest()

	@staticmethod
	def _xpath_hash(xpath: str) -> str:
		try:
			if not xpath:
				return hashlib.sha256("empty_xpath".encode()).hexdigest()
			
			if not isinstance(xpath, str):
				xpath = str(xpath)
			
			return hashlib.sha256(xpath.encode()).hexdigest()
		except Exception as e:
			# Fallback hash if anything fails
			return hashlib.sha256("error_xpath".encode()).hexdigest()

	@staticmethod
	def _text_hash(dom_element: DOMElementNode) -> str:
		""" """
		text_string = dom_element.get_all_text_till_next_clickable_element()
		return hashlib.sha256(text_string.encode()).hexdigest()
