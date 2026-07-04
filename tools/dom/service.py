import gc
import logging
from importlib import resources
from typing import Optional
import os

from playwright.async_api import Page

from tools.dom.history_tree_processor.view import Coordinates
from tools.dom.views import (
	CoordinateSet,
	DOMBaseNode,
	DOMElementNode,
	DOMState,
	DOMTextNode,
	SelectorMap,
	ViewportInfo,
)

logger = logging.getLogger(__name__)


class DomService:
	def __init__(self, page: Page):
		self.page = page
		self.xpath_cache = {}
		
		try:
			# Try to load the JavaScript code from the file system directly instead of using resources module
			# Get the directory of this file
			current_dir = os.path.dirname(os.path.abspath(__file__))
			js_file_path = os.path.join(current_dir, 'buildDomTree.js')
			
			# Load the JS file content
			if os.path.exists(js_file_path):
				with open(js_file_path, 'r') as f:
					self.js_code = f.read()
				# Use debug level instead of info for JS loading messages
				logger.debug(f"Successfully loaded JS code from {js_file_path}")
			else:
				raise FileNotFoundError(f"JS file not found at {js_file_path}")
		except Exception as e:
			logger.error(f"Error loading JavaScript code: {str(e)}")
			# Provide a minimal fallback in case the resource is not found
			self.js_code = """
			// Minimal fallback JavaScript function if resource is not found
			function buildDomTree(args) {
				console.error("Using minimal fallback DOM tree builder");
				const rootNode = document.documentElement;
				return {
					rootId: "root",
					map: {
						"root": {
							tagName: rootNode.tagName,
							xpath: "",
							attributes: {},
							children: [],
							isVisible: true,
							isInteractive: false,
							isTopElement: false
						}
					}
				};
			}
			
			buildDomTree(arguments[0]);
			"""
			logger.warning("Using fallback JavaScript code for DOM tree generation")

	# region - Clickable elements

	async def get_clickable_elements(
		self,
		highlight_elements: bool = True,
		focus_element: int = -1,
		viewport_expansion: int = 0,
	) -> DOMState:
		element_tree, selector_map = await self._build_dom_tree(highlight_elements, focus_element, viewport_expansion)

		dom_state = DOMState(element_tree=element_tree, selector_map=selector_map)

		return dom_state

	async def _build_dom_tree(
		self,
		highlight_elements: bool,
		focus_element: int,
		viewport_expansion: int,
	) -> tuple[DOMElementNode, SelectorMap]:
		try:
			# First check if the page can run JavaScript properly
			js_test = await self.page.evaluate('1+1')
			if js_test != 2:
				logger.warning(f"Basic JavaScript test failed: 1+1 = {js_test}")
				# Continue with fallback mechanisms instead of raising an error
		except Exception as e:
			logger.error(f"JavaScript evaluation test failed: {e}")
			# Continue with fallback mechanisms

		# NOTE: We execute JS code in the browser to extract important DOM information.
		#       The returned hash map contains information about the DOM tree and the
		#       relationship between the DOM elements.
		args = {
			'doHighlightElements': highlight_elements,
			'focusHighlightIndex': focus_element,
			'viewportExpansion': viewport_expansion,
		}

		try:
			# Try basic JS evaluation first
			logger.debug("Attempting to evaluate JS code")
			
			# Create a simple test function to verify JS execution
			test_result = await self.page.evaluate("""
				() => {
					try {
						return {success: true, message: "JS evaluation works"};
					} catch (e) {
						return {success: false, error: e.toString()};
					}
				}
			""")
			
			# Only log test results at debug level
			if not test_result.get('success', False):
				logger.debug(f"JS test result: {test_result}")
			
			# Now try the actual DOM tree building
			logger.debug("Evaluating DOM tree building JS")
			eval_page = await self.page.evaluate(self.js_code, args)
			
			if not isinstance(eval_page, dict):
				logger.error(f"JS evaluation returned non-dict: {eval_page}")
				
				# Try with a simpler direct approach - create a minimal representation
				logger.debug("Attempting simpler JS evaluation approach")
				simple_js = """
				(args) => {
					try {
						console.log("Simple DOM extraction starting");
						const rootNode = document.body || document.documentElement;
						if (!rootNode) {
							console.error("No root node found");
							return {error: "No root node found"};
						}
						
						// Create a simple map with just the root
						const map = {
							"root": {
								tagName: rootNode.tagName.toLowerCase(),
								xpath: "",
								attributes: {},
								children: [],
								isVisible: true,
								isInteractive: false,
								isTopElement: true
							}
						};
						
						return {rootId: "root", map: map};
					} catch (e) {
						console.error("Error in simple DOM extraction:", e);
						return {error: e.toString()};
					}
				}
				"""
				
				eval_page = await self.page.evaluate(simple_js, args)
				logger.debug("Simple JS evaluation completed")
				
				if not isinstance(eval_page, dict) or 'error' in eval_page:
					logger.error(f"Both JS evaluation approaches failed: {eval_page}")
					# Don't raise error, use fallback
					root_node = DOMElementNode(
						tag_name="html",
						is_visible=True,
						parent=None,
						xpath="",
						attributes={},
						children=[],
						is_top_element=True
					)
					return root_node, {}
			
			# Check for expected structure    
			if 'map' not in eval_page:
				logger.error(f"JS evaluation result missing 'map': {eval_page}")
				# Use fallback instead of raising error
				root_node = DOMElementNode(
					tag_name="html",
					is_visible=True,
					parent=None,
					xpath="",
					attributes={},
					children=[],
					is_top_element=True
				)
				return root_node, {}
				
			js_node_map = eval_page['map']
			
			if not isinstance(js_node_map, dict):
				logger.error(f"JS evaluation 'map' not a dict: {type(js_node_map)}")
				# Use fallback instead of raising error
				root_node = DOMElementNode(
					tag_name="html",
					is_visible=True,
					parent=None,
					xpath="",
					attributes={},
					children=[],
					is_top_element=True
				)
				return root_node, {}
				
			if 'rootId' not in eval_page:
				logger.error(f"JS evaluation result missing 'rootId': {eval_page}")
				# Use fallback instead of raising error
				root_node = DOMElementNode(
					tag_name="html",
					is_visible=True,
					parent=None,
					xpath="",
					attributes={},
					children=[],
					is_top_element=True
				)
				return root_node, {}
				
			js_root_id = eval_page['rootId']
			
			if js_root_id not in js_node_map:
				logger.error(f"Root ID {js_root_id} not found in node map with keys: {list(js_node_map.keys())}")
				# Use fallback instead of raising error
				root_node = DOMElementNode(
					tag_name="html",
					is_visible=True,
					parent=None,
					xpath="",
					attributes={},
					children=[],
					is_top_element=True
				)
				return root_node, {}
				
		except Exception as e:
			logger.error(f"Error evaluating JS: {str(e)}")
			# Create minimal DOM state in case of failure
			root_node = DOMElementNode(
				tag_name="html",
				is_visible=True,
				parent=None,
				xpath="",
				attributes={},
				children=[],
				is_top_element=True
			)
			return root_node, {}

		# Handle the node map processing with defensive code
		try:
			selector_map = {}
			node_map = {}

			# Only log node count at debug level, not keys
			logger.debug(f"Processing {len(js_node_map)} DOM nodes")

			for id, node_data in js_node_map.items():
				try:
					node, children_ids = self._parse_node(node_data)
					if node is None:
						continue

					node_map[id] = node

					if isinstance(node, DOMElementNode) and node.highlight_index is not None:
						selector_map[node.highlight_index] = node
				except Exception as node_e:
					logger.error(f"Error processing node {id}: {node_e}")
					continue

			# Simplified log for parent-child relationships
			logger.debug(f"Building parent-child relationships")

			# Build parent-child relationships
			for id, node in node_map.items():
				if not isinstance(node, DOMElementNode):
					continue
					
				try:
					node_data = js_node_map.get(id, {})
					children_ids = node_data.get('children', [])
					
					for child_id in children_ids:
						if child_id not in node_map:
							continue

						child_node = node_map[child_id]

						child_node.parent = node
						node.children.append(child_node)
				except Exception as rel_e:
					logger.error(f"Error building relationships for node {id}: {rel_e}")
					continue

			html_to_dict = node_map.get(js_root_id)

			# Release references to help garbage collection
			del node_map
			del js_node_map
			del js_root_id

			# If something went wrong, return a minimal DOM tree
			if html_to_dict is None or not isinstance(html_to_dict, DOMElementNode):
				logger.error("Failed to build DOM tree, using minimal fallback")
				root_node = DOMElementNode(
					tag_name="html",
					is_visible=True,
					parent=None,
					xpath="",
					attributes={},
					children=[],
					is_top_element=True
				)
				return root_node, selector_map
				
			return html_to_dict, selector_map
			
		except Exception as e:
			# Catch-all exception handler to ensure we never raise a NotImplementedError
			logger.error(f"Unexpected error in _build_dom_tree: {e}")
			root_node = DOMElementNode(
				tag_name="html",
				is_visible=True,
				parent=None,
				xpath="",
				attributes={},
				children=[],
				is_top_element=True
			)
			return root_node, {}

	def _parse_node(self, node_data: dict) -> tuple[Optional[DOMBaseNode], list]:
		try:
			if node_data.get('type') == 'TEXT_NODE':
				node = DOMTextNode(
					text=node_data.get('text', ''),
					is_visible=node_data.get('isVisible', True),
					parent=None,
				)
				return node, []

			# Fix the None.lower() issue with a safer approach to get the tag name
			tag_name = node_data.get('tagName')
			if tag_name is None:
				tag_name = 'div'  # Default fallback
			else:
				# Only call lower() if it's a string
				try:
					tag_name = tag_name.lower()
				except (AttributeError, TypeError):
					tag_name = str(tag_name).lower()  # Convert to string first
					
			node = DOMElementNode(
				tag_name=tag_name,
				xpath=node_data.get('xpath', ''),
				attributes=node_data.get('attributes', {}),
				is_visible=node_data.get('isVisible', True),
				is_interactive=node_data.get('isInteractive', False),
				is_top_element=node_data.get('isTopElement', False),
				shadow_root=node_data.get('shadowRoot', False),
				highlight_index=node_data.get('highlightIndex'),
				parent=None,
				children=[],
			)

			if 'viewport_coordinates' in node_data:
				try:
					viewport_coords = node_data['viewport_coordinates']
					if viewport_coords and isinstance(viewport_coords, dict):
						# Check that all required fields exist
						required_fields = ['topLeft', 'topRight', 'bottomLeft', 'bottomRight', 'center', 'width', 'height']
						if all(field in viewport_coords for field in required_fields):
							# Create safe coordinates with fallbacks
							node.viewport_coordinates = CoordinateSet(
								top_left=Coordinates(
									x=viewport_coords['topLeft'].get('x', 0) if isinstance(viewport_coords['topLeft'], dict) else 0,
									y=viewport_coords['topLeft'].get('y', 0) if isinstance(viewport_coords['topLeft'], dict) else 0
								),
								top_right=Coordinates(
									x=viewport_coords['topRight'].get('x', 0) if isinstance(viewport_coords['topRight'], dict) else 0,
									y=viewport_coords['topRight'].get('y', 0) if isinstance(viewport_coords['topRight'], dict) else 0
								),
								bottom_left=Coordinates(
									x=viewport_coords['bottomLeft'].get('x', 0) if isinstance(viewport_coords['bottomLeft'], dict) else 0,
									y=viewport_coords['bottomLeft'].get('y', 0) if isinstance(viewport_coords['bottomLeft'], dict) else 0
								),
								bottom_right=Coordinates(
									x=viewport_coords['bottomRight'].get('x', 0) if isinstance(viewport_coords['bottomRight'], dict) else 0,
									y=viewport_coords['bottomRight'].get('y', 0) if isinstance(viewport_coords['bottomRight'], dict) else 0
								),
								center=Coordinates(
									x=viewport_coords['center'].get('x', 0) if isinstance(viewport_coords['center'], dict) else 0,
									y=viewport_coords['center'].get('y', 0) if isinstance(viewport_coords['center'], dict) else 0
								),
								width=viewport_coords.get('width', 0),
								height=viewport_coords.get('height', 0),
							)
				except Exception as e:
					logger.error(f"Error processing viewport coordinates: {e}")
					# Continue without setting viewport coordinates

			if 'page_coordinates' in node_data:
				try:
					page_coords = node_data['page_coordinates']
					if page_coords and isinstance(page_coords, dict):
						# Check that all required fields exist
						required_fields = ['topLeft', 'topRight', 'bottomLeft', 'bottomRight', 'center', 'width', 'height']
						if all(field in page_coords for field in required_fields):
							# Create safe coordinates with fallbacks
							node.page_coordinates = CoordinateSet(
								top_left=Coordinates(
									x=page_coords['topLeft'].get('x', 0) if isinstance(page_coords['topLeft'], dict) else 0,
									y=page_coords['topLeft'].get('y', 0) if isinstance(page_coords['topLeft'], dict) else 0
								),
								top_right=Coordinates(
									x=page_coords['topRight'].get('x', 0) if isinstance(page_coords['topRight'], dict) else 0,
									y=page_coords['topRight'].get('y', 0) if isinstance(page_coords['topRight'], dict) else 0
								),
								bottom_left=Coordinates(
									x=page_coords['bottomLeft'].get('x', 0) if isinstance(page_coords['bottomLeft'], dict) else 0,
									y=page_coords['bottomLeft'].get('y', 0) if isinstance(page_coords['bottomLeft'], dict) else 0
								),
								bottom_right=Coordinates(
									x=page_coords['bottomRight'].get('x', 0) if isinstance(page_coords['bottomRight'], dict) else 0,
									y=page_coords['bottomRight'].get('y', 0) if isinstance(page_coords['bottomRight'], dict) else 0
								),
								center=Coordinates(
									x=page_coords['center'].get('x', 0) if isinstance(page_coords['center'], dict) else 0,
									y=page_coords['center'].get('y', 0) if isinstance(page_coords['center'], dict) else 0
								),
								width=page_coords.get('width', 0),
								height=page_coords.get('height', 0),
							)
				except Exception as e:
					logger.error(f"Error processing page coordinates: {e}")
					# Continue without setting page coordinates

			if 'viewport_info' in node_data:
				try:
					viewport_info = node_data['viewport_info']
					if viewport_info and isinstance(viewport_info, dict):
						# Check that all required fields exist
						required_fields = ['scrollX', 'scrollY', 'width', 'height']
						if all(field in viewport_info for field in required_fields):
							node.viewport_info = ViewportInfo(
								scroll_x=viewport_info.get('scrollX', 0),
								scroll_y=viewport_info.get('scrollY', 0),
								width=viewport_info.get('width', 0),
								height=viewport_info.get('height', 0),
							)
				except Exception as e:
					logger.error(f"Error processing viewport info: {e}")
					# Continue without setting viewport info

			return node, node_data.get('children', [])
			
		except Exception as e:
			# Prevent errors from bubbling up in _parse_node
			logger.error(f"Error parsing node: {str(e)}")
			return None, []
