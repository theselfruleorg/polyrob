"""Tool-result offload mixin (P7 finalization).

Extracted verbatim from MemoryWriterMixin, which mixed H-MEM writing with the
unrelated large-tool-result file-offload concern. These three methods only call each
other (self-contained) and operate on the same Agent `self`, so composing this mixin
into Agent is behavior-identical. Logic moved byte-for-byte, unchanged."""

from __future__ import annotations

from typing import List

from agents.task.agent.views import ActionResult


class ToolResultOffloadMixin:
	def _extract_intelligent_preview(self, content: str, max_length: int = 10000) -> str:
		"""
		Extract intelligent preview from content with context-aware summarization.
		For HTML: Extract structured data (title, headings, key info)
		For other: Clean and compact whitespace
		Based on 2025 research: Query-focused summarization for tool outputs.
		"""
		import re

		# Detect if HTML
		is_html = any(marker in content[:500].lower() for marker in ['<html', '<!doctype', '<head', '<body'])

		if is_html:
			# HTML-specific extraction
			try:
				parts = []

				# Extract title
				title_match = re.search(r'<title[^>]*>(.*?)</title>', content, re.IGNORECASE | re.DOTALL)
				if title_match:
					title = re.sub(r'\s+', ' ', title_match.group(1)).strip()
					parts.append(f"Title: {title}")

				# Extract meta description
				desc_match = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)', content, re.IGNORECASE)
				if desc_match:
					parts.append(f"Description: {desc_match.group(1)}")

				# Extract h1 headings (max 3)
				h1_matches = re.findall(r'<h1[^>]*>(.*?)</h1>', content, re.IGNORECASE | re.DOTALL)
				if h1_matches:
					h1_clean = [re.sub(r'<[^>]+>|&\w+;|\s+', ' ', h.strip()) for h in h1_matches[:3]]
					h1_clean = [h for h in h1_clean if h and len(h) > 5]
					if h1_clean:
						parts.append(f"Main Headings: {'; '.join(h1_clean)}")

				# Extract h2 headings (max 5)
				h2_matches = re.findall(r'<h2[^>]*>(.*?)</h2>', content, re.IGNORECASE | re.DOTALL)
				if h2_matches:
					h2_clean = [re.sub(r'<[^>]+>|&\w+;|\s+', ' ', h.strip()) for h in h2_matches[:5]]
					h2_clean = [h for h in h2_clean if h and len(h) > 5]
					if h2_clean:
						parts.append(f"Sections: {'; '.join(h2_clean)}")

				# Extract first paragraph of actual content (skip nav/header)
				# Look for <p> tags after <main>, <article>, or <div class="content">
				content_areas = re.findall(r'<(?:main|article|div[^>]*(?:class|id)=["\'][^"\']*content[^"\']*["\'])[^>]*>(.*?)</(?:main|article|div)>',
										   content, re.IGNORECASE | re.DOTALL)
				if content_areas:
					for area in content_areas[:1]:
						p_matches = re.findall(r'<p[^>]*>(.*?)</p>', area, re.IGNORECASE | re.DOTALL)
						if p_matches:
							first_p = re.sub(r'<[^>]+>|&\w+;|\s+', ' ', p_matches[0]).strip()
							if len(first_p) > 50:
								parts.append(f"Content: {first_p[:300]}...")
								break

				# Combine parts
				if parts:
					preview = ' | '.join(parts)
					if len(preview) > max_length:
						preview = preview[:max_length] + "..."
					return preview

			except Exception as e:
				# Fallback to simple preview if parsing fails
				pass

		# Non-HTML or fallback: Clean whitespace and compact
		preview = re.sub(r'\s+', ' ', content[:max_length]).strip()
		if len(content) > max_length:
			preview += "..."
		return preview

	def _result_is_untrusted(self, result) -> bool:
		"""True if this result's content came from an untrusted tool (P1-2).

		Resolves the owning tool via the same registry seam UP-06 uses; also treats a
		URL in metadata as a fetched-from-web signal (the common offload case). Used to
		decide whether the OFFLOADED FILE content must be framed as DATA — otherwise a
		later filesystem read re-enters untrusted content unwrapped (the laundering the
		pointer's own UP-06 wrap can't cover, since `filesystem` is a trusted tool).
		"""
		try:
			from agents.task.agent.core.untrusted_wrap import is_untrusted_tool
			name = getattr(result, 'action_name', None) or getattr(result, 'action_type', None)
			tool = None
			controller = getattr(self, 'controller', None)
			if name and controller is not None:
				try:
					details = controller.get_action_details(name)
					tool = getattr(details, 'tool', None) if details is not None else None
				except Exception:
					tool = None
			if is_untrusted_tool(name, tool):
				return True
			md = getattr(result, 'metadata', None)
			return bool(isinstance(md, dict) and 'url' in md)
		except Exception:
			return False  # fail-open: never block the offload on the provenance check

	def _handle_large_action_results(self, results: List[ActionResult]) -> None:
		"""Handle large content in ActionResults to prevent memory issues."""
		from agents.task.robust_parse_config import RobustParseConfig
		import time
		import re
		
		for result in results:
			if result.extracted_content and len(result.extracted_content) > RobustParseConfig.MAX_EXTRACTED_CONTENT_SIZE:
				try:
					# Store original content length before replacement
					original_content_length = len(result.extracted_content)
					
					# Try to store large content in a file and replace with reference
					from agents.task.path import pm
					
					# FIXED: Generate intelligent filename based on action context and content type
					filename = None
					content_preview = result.extracted_content[:500]  # First 500 chars for analysis
					
					# Enhanced filename generation based on content analysis
					if hasattr(result, 'action_type'):
						action_type = result.action_type
					elif hasattr(result, 'action_name'):
						action_type = result.action_name
					else:
						action_type = 'content'
					
					# Analyze content to determine appropriate extension
					file_extension = '.txt'  # Default
					if any(marker in content_preview.lower() for marker in ['<html', '<!doctype', '<head', '<body']):
						file_extension = '.html'
					elif any(marker in content_preview for marker in ['{', '}', '[', ']', '":']):
						# Likely JSON
						file_extension = '.json'
					elif content_preview.strip().startswith('<?xml'):
						file_extension = '.xml'
					elif '|' in content_preview and content_preview.count('\n') > 3:
						# Looks like tabular data
						file_extension = '.csv'
					
					# FIXED: Create more descriptive filename with timestamp and content hash
					import hashlib
					content_hash = hashlib.md5(result.extracted_content.encode()).hexdigest()[:8]
					timestamp = int(time.time())
					
					if hasattr(result, 'metadata') and result.metadata and 'url' in result.metadata:
						url = result.metadata['url']
						# Convert URL to safe filename component
						safe_url = re.sub(r'[^\w\-_.]', '_', url.replace('https://', '').replace('http://', ''))
						safe_url = safe_url[:50]  # Limit length
						filename = f"{action_type}_{safe_url}_{timestamp}_{content_hash}{file_extension}"
					else:
						filename = f"{action_type}_{timestamp}_{content_hash}{file_extension}"
					
					# FIXED: Store in workspace root so filesystem can access it
					# Filesystem enforces workspace-only access, so files must be in workspace/
					content_file = pm().create_file_path(
						self.session_id,
						"workspace",
						filename,
						user_id=self.user_id
					)
					
					# P1-2: frame the FILE content as untrusted DATA when it came from an
					# untrusted tool, so a later `read_file` (a trusted tool, hence not
					# UP-06-wrapped on read) surfaces it as DATA, not instructions. The
					# pointer/preview is still UP-06-wrapped downstream; this closes the
					# file-offload laundering path. Trusted large results are unchanged.
					content_to_write = result.extracted_content
					if self._result_is_untrusted(result):
						try:
							from agents.task.agent.core.untrusted_wrap import wrap_untrusted
							content_to_write = wrap_untrusted(str(action_type), result.extracted_content)
						except Exception:
							content_to_write = result.extracted_content  # fail-open

					# FIXED: Write content with proper encoding and error handling
					try:
						with open(content_file, 'w', encoding='utf-8', errors='replace') as f:
							f.write(content_to_write)
					except UnicodeEncodeError:
						# Fallback: write as bytes if UTF-8 fails
						with open(content_file, 'wb') as f:
							f.write(content_to_write.encode('utf-8', errors='replace'))
					
					# FIXED: Create enhanced file reference with explicit agent instructions
					# Build metadata section
					metadata_parts = []
					if hasattr(result, 'metadata') and result.metadata:
						if 'url' in result.metadata:
							metadata_parts.append(f"Source: {result.metadata['url']}")
						if 'title' in result.metadata:
							metadata_parts.append(f"Title: {result.metadata['title'][:100]}")
						if 'content_type' in result.metadata:
							metadata_parts.append(f"Type: {result.metadata['content_type']}")

					metadata_str = f" | {' | '.join(metadata_parts)}" if metadata_parts else ""

					# Build file reference with EXPLICIT agent instructions for accessing stored content
					file_reference = f"""[LARGE CONTENT STORED]
File: {content_file.name}
Size: {original_content_length:,} characters{metadata_str}

HOW TO ACCESS: Use the `read_file` action with file_path="{content_file.name}" to read the full content.
Example: {{"read_file": {{"file_path": "{content_file.name}"}}}}
"""

					# Add intelligent preview with context-aware summarization
					preview_length = RobustParseConfig.LARGE_CONTENT_PREVIEW_LENGTH
					if preview_length > 0:
						# Use intelligent extraction for HTML/structured content
						preview = self._extract_intelligent_preview(result.extracted_content, max_length=preview_length)
						file_reference += f"\nPREVIEW (first {len(preview):,} chars):\n{preview}"

					file_reference += "\n[END LARGE CONTENT REFERENCE]"
					
					# FIXED: Store file reference metadata for better tracking
					# Initialize file_references if None or not a list
					if not isinstance(getattr(result, 'file_references', None), list):
						result.file_references = []

					file_ref_metadata = {
						'type': 'large_content',
						'path': str(content_file),
						'original_size': original_content_length,
						'preview_size': len(file_reference),
						'content_type': file_extension[1:],  # Remove the dot
						'created_at': time.time()
					}

					if hasattr(result, 'metadata') and result.metadata:
						file_ref_metadata['source_metadata'] = result.metadata

					result.file_references.append(file_ref_metadata)
					
					# Replace with enhanced file reference
					result.extracted_content = file_reference
					
					# Log with better context
					self.logger.info(f"Stored large {action_type} content ({original_content_length:,} chars) in {content_file.name}")
					
				except Exception as e:
					self.logger.warning(f"Failed to store large content in file: {e}", exc_info=True)
					# Fallback to simple truncation using new config
					result.extracted_content = RobustParseConfig.truncate_extracted_content(result.extracted_content)

					# FIXED: Still create file reference metadata for fallback case
					# Initialize file_references if None or not a list
					if not isinstance(getattr(result, 'file_references', None), list):
						result.file_references = []

					result.file_references.append({
						'type': 'truncated_content',
						'original_size': len(result.extracted_content) + len(' [TRUNCATED]'),
						'truncated_size': len(result.extracted_content),
						'reason': f'File storage failed: {str(e)}'
					})
