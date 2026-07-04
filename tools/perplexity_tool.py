import logging
from typing import List, Optional, Dict, Any
import json
import aiohttp
import ssl
import certifi
from core.exceptions import ConfigurationError, APIError, AuthenticationError, ToolError
from core.config import BotConfig
from .base_tool import BaseTool
from tools.controller.views import PerplexitySearchAction, PerplexityAnalyzeAction, PerplexitySourcesAction

class PerplexityTool(BaseTool):
    """Service for interacting with Perplexity API."""
    
    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {
            'rate_limit_manager': 'Rate limit management'
        }
    
    @property
    def optional_services(self) -> Dict[str, str]:
        """Get optional services."""
        return {
            'cache_manager': 'Cache for API responses'  # Only need caching
        }

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        """Initialize Perplexity service."""
        super().__init__(name=name, config=config, container=container)
        
        # Initialize SSL context during construction
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.base_url = "https://api.perplexity.ai/chat/completions"
        
        # Enable if API key is present
        self._enabled = bool(getattr(config, 'perplexity_api_key', None))
        if not self._enabled:
            self.logger.warning("Perplexity API key not found, service will be disabled")

    @property
    def required_config(self) -> Dict[str, str]:
        """Get required configuration keys."""
        return {
            'perplexity_api_key': 'Perplexity API key'
        }
    
    async def _initialize(self) -> None:
        """Initialize Perplexity service."""
        try:
            # Services are automatically injected by BaseService
            # Just validate API key BEFORE registering actions
            if not self.config.perplexity_api_key or self.config.perplexity_api_key == "placeholder":
                self.logger.warning("Perplexity API key not configured")
                self._enabled = False
                # Mark service as unavailable but don't raise exception
                from tools.base_tool import ToolStatus
                self._status = ToolStatus.FAILED
                # Don't register actions for disabled service
                return

            # Only register actions if we have a valid API key
            # Call parent's _initialize to register decorated actions
            await super()._initialize()
            
            # Test API connection
            try:
                await self._test_api_connection()
            except Exception as e:
                self.logger.error(f"Failed to connect to Perplexity API: {e}")
                self._enabled = False
                from tools.base_tool import ToolStatus
                self._status = ToolStatus.FAILED
                return

        except Exception as e:
            self.logger.error(f"Failed to initialize Perplexity service: {e}")
            # Don't raise for optional services - mark as unavailable
            from tools.base_tool import ToolStatus
            self._status = ToolStatus.FAILED
            self._enabled = False

    async def _test_api_connection(self) -> None:
        """Test API connection."""
        try:
            # Make a minimal test request
            messages = [
                {"role": "user", "content": "test"}
            ]
            result = await self._make_request(messages)
            if "error" in result:
                raise APIError(f"API test failed: {result['error']}")
            self.logger.info("Perplexity API connection test successful")
        except AuthenticationError as e:
            self.logger.error(f"Perplexity API authentication failed: {e}")
            self.logger.error("The API key appears to be invalid. Please update PERPLEXITY_API_KEY in your .env file")
            self.logger.error("You can generate a new API key at: https://www.perplexity.ai/settings/api")
            raise
        except Exception as e:
            self.logger.error(f"Perplexity API connection test failed: {e}")
            raise

    async def _cleanup(self) -> None:
        """Cleanup Perplexity service resources."""
        try:
            # Close any open connections
            if hasattr(self, 'session') and self.session:
                await self.session.close()
                
            self.logger.info("Perplexity service cleaned up successfully")
            
        except Exception as e:
            self.logger.error(f"Error during Perplexity service cleanup: {e}")
            raise ToolError(f"Failed to cleanup Perplexity service: {e}")
            
    @BaseTool.action(
        'Search for information using Perplexity AI',
        param_model=PerplexitySearchAction
    )
    async def perplexity_search(self, params: PerplexitySearchAction):
        """Search using Perplexity API."""
        await self.ensure_initialized()
        
        if not self._enabled:
            raise ConfigurationError("Perplexity service is not enabled")
            
        try:
            query = params.query.strip()
            if not query:
                return "Please provide a search query."
            
            messages = [
                {"role": "system", "content": "You are a helpful AI assistant that provides accurate and concise information."},
                {"role": "user", "content": query}
            ]
            
            result = await self._make_request(messages)
            if "error" in result:
                return f"Error: {result['error']}"
            
            try:
                content = result["choices"][0]["message"]["content"]
                # Limit response size to prevent overwhelming the agent
                MAX_RESPONSE_LENGTH = 50000  # 50k chars (reasonable for search responses)
                if len(content) > MAX_RESPONSE_LENGTH:
                    self.logger.warning(f"Perplexity response truncated from {len(content)} to {MAX_RESPONSE_LENGTH} characters")
                    content = content[:MAX_RESPONSE_LENGTH] + "\n\n[Response truncated due to size...]"
                return content
            except (KeyError, IndexError) as e:
                return f"Error parsing response: {str(e)}"

        except AuthenticationError as e:
            self.logger.error(f"Perplexity authentication error: {str(e)}")
            self.logger.error("The API key is invalid. Please update PERPLEXITY_API_KEY in config/.env.development")
            self.logger.error("Generate a new key at: https://www.perplexity.ai/settings/api")
            raise APIError(f"Search failed: Invalid API key. Please update PERPLEXITY_API_KEY in the configuration.")
        except Exception as e:
            self.logger.error(f"Perplexity search error: {str(e)}")
            raise APIError(f"Search failed: {str(e)}")
            
    @BaseTool.action(
        'Get detailed analysis on a topic using Perplexity AI',
        param_model=PerplexityAnalyzeAction
    )
    async def perplexity_analyze(self, params: PerplexityAnalyzeAction):
        """Get detailed analysis from Perplexity."""
        await self.ensure_initialized()
        
        try:
            topic = params.topic.strip()
            if not topic:
                return "Please provide a topic to analyze."
            
            messages = [
                {"role": "system", "content": "You are an analytical AI assistant. Provide a detailed analysis of the given topic."},
                {"role": "user", "content": f"Please provide a detailed analysis of: {topic}"}
            ]
            
            result = await self._make_request(messages)
            if "error" in result:
                return result["error"]
            
            try:
                content = result["choices"][0]["message"]["content"]
                # Limit response size to prevent overwhelming the agent
                MAX_RESPONSE_LENGTH = 50000  # 50k chars (reasonable for analysis responses)
                if len(content) > MAX_RESPONSE_LENGTH:
                    self.logger.warning(f"Perplexity response truncated from {len(content)} to {MAX_RESPONSE_LENGTH} characters")
                    content = content[:MAX_RESPONSE_LENGTH] + "\n\n[Response truncated due to size...]"
                return content
            except (KeyError, IndexError) as e:
                return f"Error parsing response: {str(e)}"
        except Exception as e:
            self.logger.error(f"Perplexity analyze error: {str(e)}")
            raise
            
    @BaseTool.action(
        'Get trusted sources and references on a topic using Perplexity AI',
        param_model=PerplexitySourcesAction
    )
    async def perplexity_sources(self, params: PerplexitySourcesAction):
        """Get sources for a query."""
        await self.ensure_initialized()
        
        query = params.topic.strip()
        if not query:
            return "Please provide a query to get sources for."
            
        messages = [
            {"role": "system", "content": "You are a research assistant. Provide relevant sources and references for the query."},
            {"role": "user", "content": f"Please provide sources and references for: {query}"}
        ]
        
        result = await self._make_request(messages)
        if "error" in result:
            return result["error"]
            
        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            return f"Error parsing response: {str(e)}"

    async def ensure_initialized(self) -> None:
        """Ensure service is initialized."""
        if not self._initialized:
            await self.initialize()

    async def test_connection(self) -> bool:
        """Test if the Perplexity API is accessible and properly configured"""
        if not self.config.perplexity_api_key:
            return False
            
        try:
            # Make a minimal test request
            messages = [
                {"role": "user", "content": "test"}
            ]
            result = await self._make_request(messages)
            return "error" not in result
        except Exception as e:
            self.logger.warning(f"Perplexity API connection test failed: {str(e)}")
            return False
        
    async def _make_request(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """Make a request to the Perplexity API."""
        if not self.config.perplexity_api_key:
            raise ConfigurationError("Perplexity API key not configured")
            
        headers = {
            "Authorization": f"Bearer {self.config.perplexity_api_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": "sonar",
            "messages": messages
        }
        
        try:
            # Use SSL context in ClientSession
            conn = aiohttp.TCPConnector(ssl=self.ssl_context)
            async with aiohttp.ClientSession(connector=conn) as session:
                async with session.post(
                    self.base_url,
                    headers=headers,
                    json=data,
                    ssl=self.ssl_context
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 401:
                        raise AuthenticationError("Invalid Perplexity API key")
                    else:
                        error_text = await response.text()
                        self.logger.error(f"Perplexity API error: {error_text}")
                        raise APIError(f"API error: {response.status} - {error_text}")
        except aiohttp.ClientError as e:
            self.logger.error(f"Request error: {str(e)}")
            raise APIError(f"Request failed: {str(e)}")