"""FileSystem tool: file operations (read/write/append/list/delete/create_directory).

PDF extraction lives in tools/filesystem_pdf.py (PdfExtractionMixin).
Document/web processing lives in tools/filesystem_docproc.py (DocProcessingMixin).

IMPORTANT: Do NOT add `from __future__ import annotations` to this file.
The registry inspects first-param annotations on @action closures via issubclass()
and stringized annotations break that routing.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple
import asyncio
import json
from pathlib import Path
import re
import os
import tempfile
import shutil
import time
from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from core.config import BotConfig
from core.exceptions import ServiceError
from tools.controller.views import (
    ReadFileAction, WriteFileAction, AppendFileAction,
    ListDirectoryAction, DeleteFileAction, CreateDirectoryAction,
    DocProcessAction, DocAnalyzeAction, DocProcessUrlAction,
)
from tools.filesystem_pdf import PdfExtractionMixin
from tools.filesystem_docproc import DocProcessingMixin

# Local action model for extract_urls (not in controller.views)
class ExtractUrlsAction(BaseModel):
    """Parameters for extracting URLs from text"""
    text: str = Field(..., description="Text content to extract URLs from")

# Constants
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_TEXT_LENGTH = 100000  # 100k chars (reasonable for file content)
CHUNK_SIZE = 4096  # 4KB chunks
CACHE_TTL = 3600  # 1 hour

SUPPORTED_FORMATS = {
    'text': ['.txt', '.md', '.rst'],
    'doc': ['.doc', '.docx', '.odt'],
    'pdf': ['.pdf'],
    'code': ['.py', '.js', '.ts', '.java', '.cpp', '.c', '.go', '.rs']
}


class FileSystem(PdfExtractionMixin, DocProcessingMixin, BaseTool):
    """Service for processing various types of documents and content."""

    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {
            'rate_limit_manager': 'Rate limit management'  # For API rate limiting
        }

    @property
    def optional_services(self) -> Dict[str, str]:
        """Get optional services."""
        return {
            'llm_client': 'LLM client for text enhancement',  # Optional text enhancement
            'cache_manager': 'Cache for processed documents'  # Optional caching
        }

    @property
    def required_config(self) -> Dict[str, str]:
        """Get required configuration keys."""
        return {
            'model_name': 'Name of the LLM model to use',
            'temperature': 'Sampling temperature',
            'max_tokens': 'Maximum tokens for generation'
        }

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        """Initialize document processor."""
        super().__init__(name=name, config=config, container=container)

        # Initialize configuration
        self.max_file_size = getattr(config, 'doc_max_file_size', MAX_FILE_SIZE)
        self.max_text_length = getattr(config, 'doc_max_text_length', MAX_TEXT_LENGTH)
        self.chunk_size = getattr(config, 'doc_chunk_size', CHUNK_SIZE)
        self.cache_ttl = getattr(config, 'doc_cache_ttl', CACHE_TTL)
        self.supported_formats = getattr(config, 'doc_supported_formats', SUPPORTED_FORMATS)

    async def _initialize(self) -> None:
        """Initialize document processor."""
        # First call parent's _initialize to register decorated actions
        await super()._initialize()

        try:
            # Services will be automatically injected by BaseService
            # No need to manually get them

            # Test API connection if needed
            try:
                await self._test_api_connection()
            except Exception as e:
                self.logger.error(f"Failed to connect to document processing API: {e}")
                self._enabled = False
                return

        except Exception as e:
            self.logger.error(f"Failed to initialize document processor: {e}")
            raise ServiceError(f"Failed to initialize document processor: {e}")

    async def cleanup(self) -> None:
        """Clean up resources."""
        self._initialized = False

    async def ensure_initialized(self) -> None:
        """Ensure service is initialized."""
        if not self._initialized:
            await self.initialize()

    # ---------------------------------------------------------------------------
    # Verification helper (used by write_file / append_file)
    # ---------------------------------------------------------------------------

    async def _verify_file_write(self, file_path: str, expected_content: str, original_path: str) -> dict:
        """Verify file was written correctly (OPTIMIZATION: Task 6 - Nov 14, 2025)"""
        try:
            # Read back the file
            with open(file_path, 'r', encoding='utf-8') as f:
                actual_content = f.read()

            # For JSON files, verify structure and provide item counts
            if file_path.endswith('.json'):
                try:
                    expected_data = json.loads(expected_content)
                    actual_data = json.loads(actual_content)

                    # Compare counts for lists
                    if isinstance(expected_data, list) and isinstance(actual_data, list):
                        if len(expected_data) != len(actual_data):
                            return {
                                "verified": False,
                                "reason": f"Count mismatch: wrote {len(expected_data)}, file has {len(actual_data)}"
                            }

                        return {
                            "verified": True,
                            "item_count": len(actual_data),
                            "message": f"Verified: file contains {len(actual_data)} items"
                        }

                    # For dict, return keys
                    elif isinstance(actual_data, dict):
                        return {
                            "verified": True,
                            "type": "dict",
                            "keys": list(actual_data.keys())[:5],
                            "message": f"Verified: dict with {len(actual_data)} keys"
                        }

                except json.JSONDecodeError as e:
                    return {
                        "verified": False,
                        "reason": f"Invalid JSON in file: {e}"
                    }

            # For text files, verify size
            if len(actual_content) != len(expected_content):
                return {
                    "verified": False,
                    "reason": f"Size mismatch: expected {len(expected_content)} chars, got {len(actual_content)} chars"
                }

            return {
                "verified": True,
                "size_bytes": len(actual_content.encode('utf-8')),
                "message": "File write verified successfully"
            }

        except Exception as e:
            return {
                "verified": False,
                "reason": f"Verification failed: {e}"
            }

    # ---------------------------------------------------------------------------
    # @action: extract_urls
    # ---------------------------------------------------------------------------

    @BaseTool.action(
        'Extract URLs from text content with their context',
        param_model=ExtractUrlsAction
    )
    async def extract_urls(self, params: ExtractUrlsAction) -> List[Tuple[str, str]]:
        """Extract URLs from text content with their context."""
        from urllib.parse import urlparse

        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            text = params.text
            # URL pattern for http(s) and www
            url_pattern = r"""(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'".,<>?«»""'']))"""

            # Find all URLs with their positions
            urls: List[Tuple[str, str]] = []
            for match in re.finditer(url_pattern, text):
                url = match.group(0)

                # Normalize URL
                if url.lower().startswith('www'):
                    url = 'http://' + url

                try:
                    # Validate URL
                    parsed = urlparse(url)
                    if not parsed.netloc:
                        continue

                    # Get context (up to 100 chars before and after)
                    start = max(0, match.start() - 100)
                    end = min(len(text), match.end() + 100)
                    context = text[start:end].strip()

                    # Add to results if not already present
                    if url not in {u[0] for u in urls}:
                        urls.append((url, context))

                except Exception as e:
                    self.logger.warning(f"Failed to parse URL {url}: {str(e)}")
                    continue

            return urls

        except Exception as e:
            self.logger.error(f"Error extracting URLs: {str(e)}")
            return []

    # ---------------------------------------------------------------------------
    # @action: read_file
    # ---------------------------------------------------------------------------

    @BaseTool.action(
        'Read a file from the workspace. For large files (>25K tokens), use offset and limit parameters to read specific line ranges, or use grep to search for content.',
        param_model=ReadFileAction
    )
    async def read_file(self, params: ReadFileAction, execution_context=None) -> str:
        """Read a file from the workspace."""
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            file_path = params.file_path

            # Get session_id from execution_context if available
            if execution_context and hasattr(execution_context, 'session_id') and execution_context.session_id:
                self.session_id = execution_context.session_id
                # Tenant fix: adopt the execution_context user_id too, so a delegated
                # sub-agent's virtual session resolves under the parent tenant in
                # _normalize_path (which reads self.user_id) instead of _anonymous_.
                if getattr(execution_context, 'user_id', None):
                    self.user_id = execution_context.user_id
            if execution_context and hasattr(execution_context, 'workspace_dir') and execution_context.workspace_dir:
                self.workspace_dir = execution_context.workspace_dir

            # Normalize file path
            file_path = self._normalize_path(file_path)

            # Secret-content guard (read): see _reject_credential_path.
            self._reject_credential_path(file_path, params.file_path)

            if not os.path.exists(file_path):
                raise ServiceError(f"File not found: {params.file_path}")

            # Read file with offset/limit support
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()

            # Handle character-based chunking (for JSON/dense files)
            if params.char_offset is not None or params.char_limit is not None:
                full_content = ''.join(lines)
                total_chars = len(full_content)

                char_offset = params.char_offset if params.char_offset is not None else 0
                char_limit = params.char_limit if params.char_limit is not None else total_chars

                # Validate char_offset
                if char_offset < 0:
                    raise ServiceError(f"Invalid char_offset: {char_offset}. Must be >= 0")
                if char_offset >= total_chars:
                    raise ServiceError(f"char_offset {char_offset} exceeds file length ({total_chars} chars)")

                # Extract the requested character range
                end_char = min(char_offset + char_limit, total_chars)
                chunk = full_content[char_offset:end_char]

                # Add metadata about position
                header = f"[chars {char_offset}-{end_char} of {total_chars}]\n"
                return header + chunk

            # Apply line-based offset and limit if provided
            if params.offset is not None or params.limit is not None:
                offset = (params.offset - 1) if params.offset else 0  # Convert to 0-indexed
                limit = params.limit if params.limit else len(lines)

                # Validate offset
                if offset < 0:
                    raise ServiceError(f"Invalid offset: {params.offset}. Offset must be >= 1")
                if offset >= len(lines):
                    raise ServiceError(f"Offset {params.offset} exceeds file length ({len(lines)} lines)")

                # Extract the requested range
                end = min(offset + limit, len(lines))
                selected_lines = lines[offset:end]
                content = ''.join(selected_lines)

                # Add line number prefix for clarity
                numbered_content = []
                for i, line in enumerate(selected_lines, start=offset + 1):
                    numbered_content.append(f"{i:6}|{line}")

                return ''.join(numbered_content)

            # Read entire file - check token limit first
            content = ''.join(lines)

            # Estimate tokens (rough estimate: 1 token ≈ 4 characters)
            estimated_tokens = len(content) // 4
            MAX_TOKENS = 25000

            if estimated_tokens > MAX_TOKENS:
                total_lines = len(lines)
                total_chars = len(content)

                # For JSON files with few lines but many tokens, provide character-based guidance
                is_json = params.file_path.lower().endswith('.json')
                is_dense_file = total_lines < 50 and estimated_tokens > MAX_TOKENS

                if is_json or is_dense_file:
                    # JSON/dense files: line-based chunking won't help
                    # Suggest using jq or provide a summary
                    safe_chars = MAX_TOKENS * 4  # ~100k chars is safe
                    raise ServiceError(
                        f"File content ({estimated_tokens:,} tokens, {total_chars:,} chars) exceeds limit ({MAX_TOKENS:,} tokens).\n\n"
                        f"This is a {'JSON' if is_json else 'dense'} file with only {total_lines} lines - line-based chunking won't help.\n\n"
                        f"**Options:**\n"
                        f"1. Use `char_offset` and `char_limit` for character-based reading:\n"
                        f"   - First {safe_chars:,} chars: {{\"filePath\": \"{params.file_path}\", \"char_offset\": 0, \"char_limit\": {safe_chars}}}\n"
                        f"   - Next chunk: {{\"filePath\": \"{params.file_path}\", \"char_offset\": {safe_chars}, \"char_limit\": {safe_chars}}}\n"
                        f"2. For JSON: use shell command to extract specific keys with jq\n"
                        f"3. The data was saved by MCP - consider using a smaller `limit` parameter in the original MCP call"
                    )
                else:
                    # Normal multi-line file: line-based chunking works
                    raise ServiceError(
                        f"File content ({estimated_tokens:,} tokens) exceeds maximum allowed tokens ({MAX_TOKENS:,}). "
                        f"Please use offset and limit parameters to read specific portions of the file.\n\n"
                        f"File has {total_lines} lines. Example usage:\n"
                        f"- Read first 100 lines: {{\"filePath\": \"{params.file_path}\", \"offset\": 1, \"limit\": 100}}\n"
                        f"- Read lines 500-600: {{\"filePath\": \"{params.file_path}\", \"offset\": 500, \"limit\": 100}}\n"
                        f"- Read last 100 lines: {{\"filePath\": \"{params.file_path}\", \"offset\": {max(1, total_lines - 99)}, \"limit\": 100}}"
                    )

            # Process content with text cleaner
            processed_content = await self._clean_text(content)

            return processed_content

        except ServiceError:
            # Re-raise ServiceErrors as-is (they already have good messages)
            raise
        except Exception as e:
            self.logger.error(f"Error reading file: {str(e)}")
            raise ServiceError(f"Failed to read file: {str(e)}")

    # ---------------------------------------------------------------------------
    # @action: write_file
    # ---------------------------------------------------------------------------

    @BaseTool.action(
        'Write content to a file in the workspace',
        param_model=WriteFileAction
    )
    async def write_file(self, params: WriteFileAction, execution_context=None) -> str:
        """Write content to a file in the workspace."""
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            file_path = params.file_path
            content = params.content

            # Get session_id from execution_context if available
            if execution_context and hasattr(execution_context, 'session_id') and execution_context.session_id:
                self.session_id = execution_context.session_id
                # Tenant fix: adopt the execution_context user_id too, so a delegated
                # sub-agent's virtual session resolves under the parent tenant in
                # _normalize_path (which reads self.user_id) instead of _anonymous_.
                if getattr(execution_context, 'user_id', None):
                    self.user_id = execution_context.user_id
            if execution_context and hasattr(execution_context, 'workspace_dir') and execution_context.workspace_dir:
                self.workspace_dir = execution_context.workspace_dir

            # Add a small delay to handle potential race conditions with other operations
            await asyncio.sleep(0.2)

            # Normalize file path with additional logging
            try:
                normalized_path = self._normalize_path(file_path)
                self.logger.debug(f"Normalized path: {file_path} -> {normalized_path}")
            except Exception as norm_error:
                self.logger.error(f"Path normalization failed for {file_path}: {norm_error}")
                raise ServiceError(f"Path normalization failed: {str(norm_error)}")

            # Secret-content guard (write): symmetric with read_file/coding._confine.
            self._reject_credential_path(normalized_path, file_path)

            # Store paths for reporting
            original_path = file_path
            file_path = normalized_path

            # Create directory with enhanced error handling
            try:
                dir_path = os.path.dirname(os.path.abspath(file_path))
                os.makedirs(dir_path, exist_ok=True)

                # Verify directory was created and is writable
                if not os.path.exists(dir_path):
                    raise ServiceError(f"Failed to create directory {dir_path}")

                if not os.access(dir_path, os.W_OK):
                    raise ServiceError(f"Directory {dir_path} is not writable")

                self.logger.debug(f"Verified directory {dir_path} exists and is writable")
            except Exception as dir_error:
                self.logger.error(f"Directory creation/verification failed: {str(dir_error)}")
                raise ServiceError(f"Directory error: {str(dir_error)}")

            # F9 (live-test): write content VERBATIM. _clean_text collapses horizontal
            # whitespace and strips every line — appropriate for extracted DOCUMENT text,
            # but it DESTROYS indentation in code/YAML/Markdown the agent authors (e.g.
            # valid Python became IndentationError on disk). A file write must persist
            # exactly what was requested.
            processed_content = content

            # Write using a temporary file first for atomic operation
            max_retries = 3
            retry_count = 0
            success = False

            while retry_count < max_retries and not success:
                try:
                    # Create a unique temporary file name based on timestamp
                    timestamp = int(time.time() * 1000)
                    temp_path = f"{file_path}.{timestamp}.tmp"

                    # Write content to temporary file with explicit encoding
                    with open(temp_path, 'w', encoding='utf-8') as temp_file:
                        temp_file.write(processed_content)

                    # Ensure the temp file was written successfully
                    if not os.path.exists(temp_path):
                        raise ServiceError(f"Temp file {temp_path} was not created")

                    # Verify temp file content before moving
                    verify_size = os.path.getsize(temp_path)
                    expected_size = len(processed_content.encode('utf-8'))

                    if abs(verify_size - expected_size) > 10:  # Allow small difference due to encoding
                        raise ServiceError(f"Temp file size verification failed: expected ~{expected_size} bytes, got {verify_size}")

                    # Atomically move the temp file to the target location
                    # Use different methods for different platforms
                    if os.name == 'nt':  # Windows
                        # Windows needs special handling for replace
                        if os.path.exists(file_path):
                            os.replace(temp_path, file_path)
                        else:
                            os.rename(temp_path, file_path)
                    else:  # Unix/Linux/MacOS
                        shutil.move(temp_path, file_path)

                    # Verify file was written successfully and has expected content
                    if not os.path.exists(file_path):
                        raise ServiceError(f"File {file_path} does not exist after write operation")

                    actual_size = os.path.getsize(file_path)
                    if abs(actual_size - expected_size) > 10:  # Allow small difference due to encoding
                        raise ServiceError(f"File size verification failed: expected ~{expected_size} bytes, got {actual_size}")

                    # Mark as successful
                    success = True

                    # Log success
                    self.logger.info(f"Successfully wrote {len(processed_content)} chars to {file_path}")

                    # OPTIMIZATION: Add verification (Task 6 - Nov 14, 2025)
                    verification = await self._verify_file_write(file_path, processed_content, original_path)

                    # Return success message with verification
                    return json.dumps({
                        "success": True,
                        "filepath": original_path,
                        "verification": verification
                    }, indent=2)

                except Exception as write_error:
                    self.logger.warning(f"Write attempt {retry_count+1}/{max_retries} failed: {str(write_error)}")
                    retry_count += 1

                    # Clean up temp file if it exists
                    try:
                        if 'temp_path' in locals() and os.path.exists(temp_path):
                            os.unlink(temp_path)
                    except Exception as cleanup_error:
                        self.logger.debug(f"Failed to clean up temp file: {cleanup_error}")

                    # Wait before retrying with increasing backoff
                    if retry_count < max_retries:
                        await asyncio.sleep(0.5 * retry_count)

            # All retries failed - try direct write method as last resort
            if not success:
                self.logger.warning(f"Atomic file operations failed after {max_retries} attempts. Trying direct write...")

                try:
                    # Try direct write mode as a last resort
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(processed_content)

                    # Verify the file was written
                    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                        self.logger.info(f"Successfully wrote {len(processed_content)} chars to {file_path} using direct write")

                        # OPTIMIZATION: Add verification (Task 6 - Nov 14, 2025)
                        verification = await self._verify_file_write(file_path, processed_content, original_path)

                        # Return success message with verification
                        return json.dumps({
                            "success": True,
                            "filepath": original_path,
                            "verification": verification
                        }, indent=2)
                    else:
                        raise ServiceError("Direct write verification failed: file is empty or missing")
                except Exception as direct_error:
                    self.logger.error(f"Direct write failed: {str(direct_error)}")
                    raise ServiceError(f"Failed to write to file after all recovery attempts: {str(direct_error)}")

        except ServiceError:
            # Re-raise ServiceError without wrapping
            raise
        except Exception as e:
            self.logger.error(f"Error writing to file: {str(e)}")
            raise ServiceError(f"Failed to write to file: {str(e)}")

    # ---------------------------------------------------------------------------
    # @action: append_file
    # ---------------------------------------------------------------------------

    @BaseTool.action(
        'Append content to a file in the workspace',
        param_model=AppendFileAction
    )
    async def append_file(self, params: AppendFileAction, execution_context=None) -> str:
        """Append content to a file in the workspace."""
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            file_path = params.file_path
            content = params.content

            # Get session_id from execution_context if available
            if execution_context and hasattr(execution_context, 'session_id') and execution_context.session_id:
                self.session_id = execution_context.session_id
                # Tenant fix: adopt the execution_context user_id too, so a delegated
                # sub-agent's virtual session resolves under the parent tenant in
                # _normalize_path (which reads self.user_id) instead of _anonymous_.
                if getattr(execution_context, 'user_id', None):
                    self.user_id = execution_context.user_id
            if execution_context and hasattr(execution_context, 'workspace_dir') and execution_context.workspace_dir:
                self.workspace_dir = execution_context.workspace_dir

            # Add a small delay to handle potential race conditions with other operations
            await asyncio.sleep(0.2)

            # Normalize file path with additional logging
            try:
                normalized_path = self._normalize_path(file_path)
                self.logger.debug(f"Normalized path for append: {file_path} -> {normalized_path}")
            except Exception as norm_error:
                self.logger.error(f"Path normalization failed for append to {file_path}: {norm_error}")
                raise ServiceError(f"Path normalization failed: {str(norm_error)}")

            # Secret-content guard (append): symmetric with read_file/coding._confine.
            self._reject_credential_path(normalized_path, file_path)

            # Store paths for reporting
            original_path = file_path
            file_path = normalized_path

            # Create directory with enhanced error handling
            try:
                dir_path = os.path.dirname(os.path.abspath(file_path))
                os.makedirs(dir_path, exist_ok=True)

                # Verify directory was created and is writable
                if not os.path.exists(dir_path):
                    raise ServiceError(f"Failed to create directory {dir_path}")

                if not os.access(dir_path, os.W_OK):
                    raise ServiceError(f"Directory {dir_path} is not writable")

                self.logger.debug(f"Verified directory {dir_path} exists and is writable")
            except Exception as dir_error:
                self.logger.error(f"Directory creation/verification failed: {str(dir_error)}")
                raise ServiceError(f"Directory error: {str(dir_error)}")

            # F9 (live-test): append content VERBATIM (see write_file) — _clean_text
            # would strip indentation from authored code/markdown.
            processed_content = content

            # Read existing file content with multiple attempts and encodings
            existing_content = ""
            read_success = False
            read_retries = 3

            for attempt in range(read_retries):
                try:
                    if os.path.exists(file_path):
                        # Try different encodings in order of likelihood
                        encodings = ['utf-8', 'latin-1', 'cp1252']
                        for encoding in encodings:
                            try:
                                with open(file_path, 'r', encoding=encoding, errors='replace') as f:
                                    existing_content = f.read()
                                self.logger.debug(f"Read {len(existing_content)} chars from existing file {file_path} using {encoding} encoding (attempt {attempt+1})")
                                read_success = True
                                break
                            except UnicodeDecodeError:
                                self.logger.debug(f"Failed to read with {encoding} encoding, trying next")
                                continue

                        if read_success:
                            break
                    else:
                        self.logger.debug(f"File {file_path} does not exist, will create new file")
                        read_success = True
                        break
                except Exception as read_error:
                    self.logger.warning(f"Failed to read existing file (attempt {attempt+1}): {str(read_error)}")
                    await asyncio.sleep(0.3 * (attempt + 1))  # Increasing backoff

            if not read_success:
                self.logger.warning(f"Could not read existing file after {read_retries} attempts, assuming empty file")
                existing_content = ""

            # Combine existing content with new content, ensuring proper spacing
            if existing_content:
                if not existing_content.endswith('\n'):
                    combined_content = existing_content + '\n' + processed_content
                else:
                    combined_content = existing_content + processed_content
            else:
                combined_content = processed_content

            # Write using a temporary file first for atomic operation
            max_retries = 3
            retry_count = 0
            success = False

            while retry_count < max_retries and not success:
                try:
                    # Create a unique temporary file name based on timestamp
                    timestamp = int(time.time() * 1000)
                    temp_path = f"{file_path}.{timestamp}.tmp"

                    # Write content to temporary file with explicit encoding
                    with open(temp_path, 'w', encoding='utf-8') as temp_file:
                        temp_file.write(combined_content)

                    # Ensure the temp file was written successfully
                    if not os.path.exists(temp_path):
                        raise ServiceError(f"Temp file {temp_path} was not created")

                    # Verify temp file content before moving
                    verify_size = os.path.getsize(temp_path)
                    expected_size = len(combined_content.encode('utf-8'))

                    if abs(verify_size - expected_size) > 10:  # Allow small difference due to encoding
                        raise ServiceError(f"Temp file size verification failed: expected ~{expected_size} bytes, got {verify_size}")

                    # Atomically move the temp file to the target location
                    # Use different methods for different platforms
                    if os.name == 'nt':  # Windows
                        # Windows needs special handling for replace
                        if os.path.exists(file_path):
                            os.replace(temp_path, file_path)
                        else:
                            os.rename(temp_path, file_path)
                    else:  # Unix/Linux/MacOS
                        shutil.move(temp_path, file_path)

                    # Verify file was written successfully and has expected content
                    if not os.path.exists(file_path):
                        raise ServiceError(f"File {file_path} does not exist after append operation")

                    actual_size = os.path.getsize(file_path)
                    if abs(actual_size - expected_size) > 10:  # Allow small difference due to encoding
                        raise ServiceError(f"File size verification failed: expected ~{expected_size} bytes, got {actual_size}")

                    # Mark as successful
                    success = True

                    # Log success
                    self.logger.info(f"Successfully appended {len(processed_content)} chars to {file_path}")

                    # Return just the relative path for clarity to the agent
                    return f"Content appended to {original_path}"

                except Exception as write_error:
                    self.logger.warning(f"Append attempt {retry_count+1}/{max_retries} failed: {str(write_error)}")
                    retry_count += 1

                    # Clean up temp file if it exists
                    try:
                        if 'temp_path' in locals() and os.path.exists(temp_path):
                            os.unlink(temp_path)
                    except Exception as cleanup_error:
                        self.logger.debug(f"Failed to clean up temp file: {cleanup_error}")

                    # Wait before retrying with increasing backoff
                    if retry_count < max_retries:
                        await asyncio.sleep(0.5 * retry_count)

            # All retries failed - try direct append method as last resort
            if not success:
                self.logger.warning(f"Atomic file operations failed after {max_retries} attempts. Trying direct append...")

                try:
                    # Try direct append mode as a last resort
                    with open(file_path, 'a', encoding='utf-8') as f:
                        if not existing_content.endswith('\n') and existing_content:
                            f.write('\n')
                        f.write(processed_content)

                    # Verify the file size increased
                    new_size = os.path.getsize(file_path)
                    if new_size <= len(existing_content.encode('utf-8')):
                        raise ServiceError(f"Direct append verification failed: file size did not increase")

                    self.logger.info(f"Successfully appended {len(processed_content)} chars to {file_path} using direct append")
                    return f"Content appended to {original_path}"
                except Exception as direct_error:
                    self.logger.error(f"Direct append failed: {str(direct_error)}")
                    raise ServiceError(f"Failed to append to file after all recovery attempts")

        except ServiceError:
            # Re-raise ServiceError without wrapping
            raise
        except Exception as e:
            self.logger.error(f"Error appending to file: {str(e)}")
            raise ServiceError(f"Failed to append to file: {str(e)}")

    # ---------------------------------------------------------------------------
    # Path normalization helper
    # ---------------------------------------------------------------------------

    def _reject_credential_path(self, normalized_path: str, display_path: str) -> None:
        """Refuse credential-shaped files (.env*, *.pem, config/.env.*, …) for read
        AND write.

        Under POLYROB_LOCAL the workspace IS the project cwd, so path confinement
        can't stop the agent touching a config/.env.production that lives inside the
        project. This is the content guard that stops secret exfiltration (read) and
        secret tampering (write); it mirrors the coding tool's ``_confine`` guard so
        both file surfaces are symmetric. Raises ServiceError on a match.
        """
        from agents.task.agent.core.secret_guard import is_credential_file
        if is_credential_file(Path(normalized_path)):
            raise ServiceError(f"Refusing to access a credential/secret file: {display_path}")

    def _normalize_path(self, file_path: str) -> str:
        """Normalize a file path to be within the workspace directory."""
        try:
            from agents.task.path import pm

            # Get session ID with multiple fallback options
            session_id = None

            # Try multiple sources for session ID in priority order
            if hasattr(self, "session_id") and self.session_id:
                session_id = self.session_id
                self.logger.debug(f"Using session_id from service: {session_id}")
            elif hasattr(self, "container") and hasattr(self.container, "session_id") and self.container.session_id:
                session_id = self.container.session_id
                self.logger.debug(f"Using session_id from container: {session_id}")
            elif hasattr(self, "_current_session_id") and self._current_session_id:
                session_id = self._current_session_id
                self.logger.debug(f"Using cached session_id: {session_id}")

            if not session_id:
                self.logger.error("No session_id available for path normalization")
                raise ValueError("No session_id available for path normalization")

            # Always clean the session ID using the path manager
            clean_id = pm().clean_session_id(session_id)

            # Cache the clean session ID for future use
            self._current_session_id = clean_id

            # Get user ID with fallbacks
            user_id = None
            if hasattr(self, "user_id") and self.user_id:
                user_id = self.user_id
            elif hasattr(self, "container") and hasattr(self.container, "user_id") and self.container.user_id:
                user_id = self.container.user_id

            # Get the workspace directory directly from the path manager
            workspace_dir = pm().get_workspace_dir(clean_id, user_id)

            # Handle absolute paths more consistently
            if os.path.isabs(file_path):
                # Note: Let the path manager handle this - it has proper logic for absolute paths
                # that contain session IDs and workspace segments
                normalized_path = pm().normalize_path(file_path, session_id=clean_id)

                # If the normalized path is not absolute, it means the path manager determined
                # this was an external path and made it safe by making it relative
                if not os.path.isabs(normalized_path):
                    # Join with workspace dir to make it usable
                    normalized_path = os.path.join(str(workspace_dir), normalized_path)
            else:
                # Collapse a single redundant leading "workspace/" segment. The
                # confinement root IS the session workspace (basename 'workspace'),
                # so an agent that writes "workspace/brief.md" (following a literal
                # instruction) would otherwise nest to <ws>/workspace/brief.md.
                rel = file_path
                if os.path.basename(os.path.normpath(str(workspace_dir))) == "workspace":
                    _parts = rel.replace("\\", "/").lstrip("/").split("/", 1)
                    if len(_parts) == 2 and _parts[0] == "workspace":
                        rel = _parts[1]
                # For relative paths, first join with workspace directory then normalize
                candidate_path = os.path.join(str(workspace_dir), rel)
                normalized_path = pm().normalize_path(candidate_path, session_id=clean_id)

            # Final safety check - ensure we're still within the workspace.
            # FS_REALPATH_CONFINE (default on): confine on realpath (catches in-root
            # symlink escapes) and FAIL LOUD on an escape instead of silently
            # rewriting to basename (which wrote a different file than asked).
            # Set FS_REALPATH_CONFINE=off to restore the legacy basename rewrite.
            workspace_abs = os.path.abspath(str(workspace_dir))
            if os.getenv("FS_REALPATH_CONFINE", "on").strip().lower() not in ("0", "false", "off", "no"):
                from core.path_safety import is_within_root
                if not is_within_root(normalized_path, workspace_abs):
                    raise ServiceError(f"Path escapes workspace: {file_path}")
            elif not os.path.abspath(normalized_path).startswith(workspace_abs):
                self.logger.warning(f"Path would escape workspace: {file_path} -> {normalized_path}")
                # Use only the filename component to stay within workspace
                filename = os.path.basename(file_path)
                normalized_path = os.path.join(str(workspace_dir), filename)

            # Ensure parent directory exists
            parent_dir = os.path.dirname(normalized_path)
            os.makedirs(parent_dir, exist_ok=True)

            return normalized_path

        except Exception as e:
            self.logger.error(f"Path normalization error: {str(e)}")
            raise ServiceError(f"Path normalization error: {str(e)}")

    # ---------------------------------------------------------------------------
    # @action: list_directory
    # ---------------------------------------------------------------------------

    @BaseTool.action(
        'List files in a directory',
        param_model=ListDirectoryAction
    )
    async def list_directory(self, params: ListDirectoryAction, execution_context=None) -> str:
        """List files in a directory."""
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            directory = params.directory or "."

            # Get session_id from execution_context if available
            if execution_context and hasattr(execution_context, 'session_id') and execution_context.session_id:
                self.session_id = execution_context.session_id
                # Tenant fix: adopt the execution_context user_id too, so a delegated
                # sub-agent's virtual session resolves under the parent tenant in
                # _normalize_path (which reads self.user_id) instead of _anonymous_.
                if getattr(execution_context, 'user_id', None):
                    self.user_id = execution_context.user_id
            if execution_context and hasattr(execution_context, 'workspace_dir') and execution_context.workspace_dir:
                self.workspace_dir = execution_context.workspace_dir

            # Normalize the directory path
            directory = self._normalize_path(directory)

            if not os.path.exists(directory):
                # Create directory if it doesn't exist
                os.makedirs(directory, exist_ok=True)
                return f"Created and listed empty directory: {params.directory}"

            files = []
            if os.path.isdir(directory):
                for entry in os.scandir(directory):
                    if entry.is_file():
                        files.append(f"{entry.name}")
                    elif entry.is_dir():
                        files.append(f"{entry.name}/")
            else:
                # If path is a file, just return the file
                files = [Path(directory).name]

            files_str = "\n".join(sorted(files))

            # Use the original path in the message for agent clarity
            display_path = params.directory or "."
            return f"Directory listing for {display_path}:\n{files_str}"

        except Exception as e:
            self.logger.error(f"Error listing directory: {str(e)}")
            raise ServiceError(f"Failed to list directory: {str(e)}")

    # ---------------------------------------------------------------------------
    # @action: delete_file
    # ---------------------------------------------------------------------------

    @BaseTool.action(
        'Delete a file',
        param_model=DeleteFileAction
    )
    async def delete_file(self, params: DeleteFileAction, execution_context=None) -> str:
        """Delete a file."""
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            file_path = params.file_path

            # Get session_id from execution_context if available
            if execution_context and hasattr(execution_context, 'session_id') and execution_context.session_id:
                self.session_id = execution_context.session_id
                # Tenant fix: adopt the execution_context user_id too, so a delegated
                # sub-agent's virtual session resolves under the parent tenant in
                # _normalize_path (which reads self.user_id) instead of _anonymous_.
                if getattr(execution_context, 'user_id', None):
                    self.user_id = execution_context.user_id
            if execution_context and hasattr(execution_context, 'workspace_dir') and execution_context.workspace_dir:
                self.workspace_dir = execution_context.workspace_dir

            # Normalize file path
            file_path = self._normalize_path(file_path)

            # Check if file exists
            if not os.path.exists(file_path):
                return f"File {params.file_path} does not exist"

            # Delete the file
            os.remove(file_path)

            return f"Deleted file {params.file_path}"

        except Exception as e:
            self.logger.error(f"Error deleting file: {str(e)}")
            raise ServiceError(f"Failed to delete file: {str(e)}")

    # ---------------------------------------------------------------------------
    # @action: create_directory
    # ---------------------------------------------------------------------------

    @BaseTool.action(
        'Create a directory',
        param_model=CreateDirectoryAction
    )
    async def create_directory(self, params: CreateDirectoryAction, execution_context=None) -> str:
        """Create a directory."""
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            directory_path = params.directory_path

            # CRITICAL: Get session_id from execution_context if available
            if execution_context and hasattr(execution_context, 'session_id') and execution_context.session_id:
                self.session_id = execution_context.session_id
                # Tenant fix: adopt the execution_context user_id too, so a delegated
                # sub-agent's virtual session resolves under the parent tenant in
                # _normalize_path (which reads self.user_id) instead of _anonymous_.
                if getattr(execution_context, 'user_id', None):
                    self.user_id = execution_context.user_id
                self.logger.debug(f"Got session_id from execution_context: {execution_context.session_id[:8]}")

            # Also use workspace_dir from execution_context
            if execution_context and hasattr(execution_context, 'workspace_dir') and execution_context.workspace_dir:
                self.workspace_dir = execution_context.workspace_dir
                self.logger.debug(f"Got workspace_dir from execution_context")

            # Normalize directory path
            directory_path = self._normalize_path(directory_path)

            # Create the directory
            os.makedirs(directory_path, exist_ok=True)

            return f"Created directory {params.directory_path}"

        except Exception as e:
            self.logger.error(f"Error creating directory: {str(e)}")
            raise ServiceError(f"Failed to create directory: {str(e)}")

    # ---------------------------------------------------------------------------
    # Internal API test stub
    # ---------------------------------------------------------------------------

    async def _test_api_connection(self) -> None:
        """Test API connection."""
        # No API to test for document processor
        pass
