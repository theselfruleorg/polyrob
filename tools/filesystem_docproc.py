"""Document and web processing mixin for FileSystem tool.

This module contains web scraping, HTTP fetching, SSL context helpers, LLM document
analysis, and text/cache utilities extracted from filesystem.py.
The DocProcessingMixin is composed into FileSystem via multiple-inheritance.

IMPORTANT: Do NOT add `from __future__ import annotations` to this file.
The registry inspects first-param annotations on @action closures via issubclass()
and stringized annotations break that routing.
"""

import re
import ssl
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional

import aiohttp
from bs4 import BeautifulSoup  # type: ignore

from core.exceptions import APIError, ServiceError
from tools.controller.views import DocProcessAction, DocAnalyzeAction


class DocProcessingMixin:
    """Mixin providing document processing, web scraping, and LLM analysis for FileSystem."""

    # ---------------------------------------------------------------------------
    # process_document (internal, not an @action)
    # ---------------------------------------------------------------------------

    async def process_document(self, params: DocProcessAction) -> str:
        """Process a document with optional LLM enhancement."""
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            # Basic processing
            processed = await self._basic_process(params.content)

            # Enhanced processing with LLM if available
            if self.llm_client and params.enhance:
                try:
                    prompt = {
                        "system": "You are a document processing assistant. Enhance and structure the following content while maintaining its key information.",
                        "messages": [
                            {"role": "user", "content": processed}
                        ]
                    }
                    # FIXED: Get intelligent max_tokens from model registry
                    max_tokens_value = params.max_tokens if hasattr(params, 'max_tokens') else None
                    if max_tokens_value is None:
                        try:
                            from modules.llm.model_registry import get_model_config
                            # Try to get model name from params or use a default
                            model_name = getattr(params, 'model', None) or getattr(params, 'model_name', None)
                            if model_name:
                                model_config = get_model_config(model_name)
                                if model_config and model_config.max_completion_tokens:
                                    max_tokens_value = model_config.max_completion_tokens
                        except Exception:
                            pass

                        # Fallback to conservative default instead of 1000
                        if max_tokens_value is None:
                            max_tokens_value = 8000

                    enhanced = await self.llm_client.generate_response(
                        prompt=prompt,
                        max_tokens=max_tokens_value,
                        temperature=params.temperature if hasattr(params, 'temperature') else 0.3
                    )
                    return enhanced
                except Exception as e:
                    self.logger.warning(f"LLM enhancement failed: {e}")
                    return processed
            return processed

        except Exception as e:
            self.logger.error(f"Error processing document: {e}")
            raise ServiceError(f"Failed to process document: {str(e)}")

    async def _basic_process(self, content: str) -> str:
        """Basic document processing without LLM."""
        return content.strip()

    # ---------------------------------------------------------------------------
    # Web metadata / content extraction
    # ---------------------------------------------------------------------------

    async def _extract_web_metadata(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Extract metadata from web page."""
        metadata = {
            'title': '',
            'description': '',
            'keywords': '',
            'author': '',
            'date': ''
        }

        # Extract title
        if soup.title:
            metadata['title'] = soup.title.string.strip() if soup.title.string else ''

        # Extract meta tags
        for meta in soup.find_all('meta'):
            # Extract description
            if meta.get('name') and meta.get('name').lower() == 'description' and meta.get('content'):
                metadata['description'] = meta.get('content').strip()

            # Extract keywords
            if meta.get('name') and meta.get('name').lower() == 'keywords' and meta.get('content'):
                metadata['keywords'] = meta.get('content').strip()

            # Extract author
            if meta.get('name') and meta.get('name').lower() == 'author' and meta.get('content'):
                metadata['author'] = meta.get('content').strip()

            # Extract Open Graph metadata
            if meta.get('property') and meta.get('property').startswith('og:') and meta.get('content'):
                key = meta.get('property')[3:]  # Remove 'og:' prefix
                metadata[f'og_{key}'] = meta.get('content').strip()

                # Use OG title/description as fallback if main ones are missing
                if key == 'title' and not metadata['title']:
                    metadata['title'] = meta.get('content').strip()
                if key == 'description' and not metadata['description']:
                    metadata['description'] = meta.get('content').strip()

        return metadata

    async def _extract_main_content(self, soup: BeautifulSoup) -> str:
        """Extract main content from web page."""
        # Try to find main content container
        main_tags = [
            soup.find('main'),
            soup.find('article'),
            soup.find(id='content'),
            soup.find(id='main-content'),
            soup.find(class_='content'),
            soup.find(class_='main-content'),
            soup.find('div', class_='article')
        ]

        # Use the first valid main container found
        main_container = next((tag for tag in main_tags if tag is not None), None)

        if main_container:
            # Remove script, style, nav, and other non-content elements
            for tag in main_container.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                tag.decompose()

            # Get text content
            content = main_container.get_text(separator='\n')
        else:
            # Fallback: use body
            body = soup.find('body')
            if body:
                # Remove script, style, nav, and other non-content elements
                for tag in body.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                    tag.decompose()

                # Get text content
                content = body.get_text(separator='\n')
            else:
                # Last resort: get all text
                content = soup.get_text(separator='\n')

        # Clean up content
        content = re.sub(r'\n{3,}', '\n\n', content)  # Remove excess newlines
        content = re.sub(r'\s+', ' ', content)  # Normalize whitespace

        return content.strip()

    # ---------------------------------------------------------------------------
    # Cache helpers
    # ---------------------------------------------------------------------------

    async def _get_cached_result(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Get cached result if available."""
        if self.cache:  # Use the cache property from BaseTool
            try:
                return await self.cache.get(cache_key)
            except Exception as e:
                self.logger.warning(f"Cache retrieval failed: {str(e)}")
        return None

    async def _cache_result(self, cache_key: str, result: Dict[str, Any]) -> None:
        """Cache result for future use."""
        if self.cache:  # Use the cache property from BaseTool
            try:
                await self.cache.set(cache_key, result, ttl=self.cache_ttl)
            except Exception as e:
                self.logger.warning(f"Cache storage failed: {str(e)}")

    # ---------------------------------------------------------------------------
    # Text cleaning
    # ---------------------------------------------------------------------------

    async def _clean_text(self, text: str) -> str:
        """Clean and normalize text content."""
        if not text:
            return ""

        try:
            import unicodedata

            # Convert to string if not already
            if not isinstance(text, str):
                text = str(text)

            # Basic normalization
            text = unicodedata.normalize('NFKC', text)

            # Remove control characters except newlines and tabs
            text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)

            # Normalize line endings
            text = text.replace('\r\n', '\n').replace('\r', '\n')

            # Remove repeated line breaks
            text = re.sub(r'\n{3,}', '\n\n', text)

            # Normalize whitespace without removing meaningful line breaks
            text = re.sub(r'[^\S\n]+', ' ', text)  # Replace horizontal whitespace with single space

            # Remove leading/trailing whitespace on each line
            lines = [line.strip() for line in text.split('\n')]
            text = '\n'.join(lines)

            # Final trim
            text = text.strip()

            return text

        except Exception as e:
            self.logger.warning(f"Text cleaning failed: {e}")
            # Return the original if cleaning fails
            return text.strip() if isinstance(text, str) else str(text).strip()

    # ---------------------------------------------------------------------------
    # LLM document analysis (internal, not an @action)
    # ---------------------------------------------------------------------------

    async def analyze_document(self, params: DocAnalyzeAction) -> Dict[str, Any]:
        """Analyze a document using the LLM."""
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            text = params.text
            analysis_type = params.analysis_type if hasattr(params, 'analysis_type') else 'general'

            # Check cache if available
            if hasattr(self, 'cache') and self.cache is not None:
                cache_key = f"doc_analysis:{analysis_type}:{text[:100]}"
                cached = self.cache.get(cache_key)
                if cached:
                    self.logger.debug("Using cached document analysis result")
                    return cached

            # Check if rate limiter is available
            rate_limiter = getattr(self, 'rate_limiter', None)

            # Check if LLM client is available
            if not hasattr(self, 'llm_client') or self.llm_client is None:
                raise ServiceError("LLM client not available for document analysis")

            # Prepare analysis prompt
            prompt = self._get_analysis_prompt(text, analysis_type)

            # Process with or without rate limiting
            if rate_limiter:
                async with rate_limiter.request_context('document_analysis'):
                    response = await self.llm_client.generate_response(
                        prompt=prompt,
                        max_tokens=self.config.max_tokens,
                        temperature=self.config.temperature
                    )
            else:
                response = await self.llm_client.generate_response(
                    prompt=prompt,
                    max_tokens=self.config.max_tokens,
                    temperature=self.config.temperature
                )

            # Verify response is not None
            if response is None:
                raise ServiceError("LLM returned None response for document analysis")

            # Parse response into structured format
            analysis = self._parse_analysis_response(response)

            # Cache result if cache is available
            if hasattr(self, 'cache') and self.cache is not None:
                cache_key = f"doc_analysis:{analysis_type}:{text[:100]}"
                self.cache.set(cache_key, analysis)

            return analysis

        except Exception as e:
            self.logger.error(f"Error analyzing document: {str(e)}")
            raise ServiceError(f"Document analysis failed: {str(e)}")

    def _get_analysis_prompt(self, text: str, analysis_type: str) -> str:
        """Get the appropriate prompt for document analysis."""
        prompts = {
            'general': f"Please analyze the following text and provide key insights:\n\n{text}",
            'summary': f"Please provide a concise summary of the following text:\n\n{text}",
            'sentiment': f"Please analyze the sentiment of the following text:\n\n{text}",
            'topics': f"Please identify the main topics discussed in the following text:\n\n{text}"
        }
        return prompts.get(analysis_type, prompts['general'])

    def _parse_analysis_response(self, response: str) -> Dict[str, Any]:
        """Parse LLM response into structured analysis format."""
        # For now, return simple format
        if not response:
            return {
                'raw_analysis': '',
                'timestamp': datetime.now().isoformat()
            }

        return {
            'raw_analysis': response,
            'timestamp': datetime.now().isoformat() if not hasattr(self, 'logger') or not self.logger.handlers else self.logger.handlers[0].formatter.converter()
        }
