"""Service that interacts with the Twitter API."""

import logging
import asyncio
import json
from typing import List, Optional, Dict, Any, Union, Callable
import tweepy  # type: ignore
from core.config import BotConfig
import aiohttp
from utils.rate_limit_manager import RateLimitManager
import time
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field
import math
import traceback
from .base_tool import BaseTool, ToolStatus
from core.exceptions import APIError, ConfigurationError, AuthenticationError, RateLimitError, ResourceNotFoundError, ToolError, ServiceError
import os

# Import action models from centralized location
from tools.controller.views import (
    TwitterSearchAction as TwitterSearchActionModel,
    TwitterGetUserAction as TwitterGetUserActionModel,
    TwitterGetTweetsAction as TwitterGetTweetsActionModel,
)


class DateTimeEncoder(json.JSONEncoder):
    """Custom JSON encoder for datetime objects."""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


# ---------------------------------------------------------------------------
# G1 — write-surface param models (Pydantic, extra="forbid").
# NOTE: this module deliberately has NO ``from __future__ import annotations`` —
# the Registry introspects action-closure first-param annotations; stringizing
# them breaks param-model routing. Keep concrete model classes; do not add it.
# ---------------------------------------------------------------------------
_TWEET_MAX = 280


# P6 (2026-07-02): 280 is the per-tweet WIRE limit, not the input cap. LLMs can't
# count characters, so a hard max_length=280 on the param model turned every long
# composition into a dead action (registry validation skipped the call; 'engage'
# goals completed having posted nothing). The tool now accepts longer text and
# auto-splits it into a chained thread; _TWEET_MAX_INPUT is a sanity cap only.
_TWEET_MAX_INPUT = 4000

_AUTOTHREAD_NOTE = (
    " Text over 280 chars is automatically split at word boundaries and posted "
    "as a numbered thread."
)


class TwitterPostAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=_TWEET_MAX_INPUT,
                      description="Tweet text. ≤280 chars posts a single tweet." + _AUTOTHREAD_NOTE)
    media_paths: Optional[List[str]] = Field(None, description="Local image/video file paths to upload + attach.")
    poll_options: Optional[List[str]] = Field(None, description="2-4 poll choices (creates a poll).")
    poll_duration_minutes: Optional[int] = Field(None, ge=5, le=10080, description="Poll duration in minutes.")


class TwitterReplyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tweet_id: str = Field(..., description="ID of the tweet to reply to.")
    text: str = Field(..., min_length=1, max_length=_TWEET_MAX_INPUT,
                      description="Reply text. ≤280 chars posts a single reply." + _AUTOTHREAD_NOTE)
    media_paths: Optional[List[str]] = Field(None, description="Local file paths to upload + attach.")


class TwitterQuoteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tweet_id: str = Field(..., description="ID of the tweet to quote.")
    text: str = Field(..., min_length=1, max_length=_TWEET_MAX_INPUT,
                      description="Quote text. ≤280 chars posts a single quote-tweet." + _AUTOTHREAD_NOTE)


class TwitterThreadAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    texts: List[str] = Field(..., min_length=1, description="Ordered tweet texts; chained as a thread.")
    media_paths: Optional[List[str]] = Field(None, description="Local file paths attached to the FIRST tweet.")


class TwitterDeleteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tweet_id: str = Field(..., description="ID of the tweet to delete.")


class TwitterTweetIdAction(BaseModel):
    """Shared model for like/unlike/retweet/unretweet (a single tweet id)."""
    model_config = ConfigDict(extra="forbid")
    tweet_id: str = Field(..., description="Target tweet id.")


class TwitterUserAction(BaseModel):
    """Shared model for follow/unfollow/mute/block (a username or numeric id)."""
    model_config = ConfigDict(extra="forbid")
    user: str = Field(..., description="Target username or numeric user id.")


class TwitterDMAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(..., description="Recipient username or numeric user id.")
    text: str = Field(..., min_length=1, max_length=10000, description="Direct-message text.")


class TwitterMentionsAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_results: int = Field(10, ge=5, le=100, description="Max mentions to fetch.")


class TwitterGetDMsAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    participant: Optional[str] = Field(
        None, description="Filter to the 1:1 conversation with this username or "
                          "numeric user id (default: all recent DM events).")
    max_results: int = Field(20, ge=1, le=100, description="Max DM events to fetch.")


class TwitterTimelineAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: str = Field(..., description="Username or numeric user id whose recent "
                                       "tweets to fetch.")
    max_results: int = Field(10, ge=5, le=100, description="Max tweets to fetch.")


class TwitterWhoamiAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Write actions gated behind TWITTER_ENABLED (reads + own-mentions/whoami stay on).
_TWITTER_WRITE_ACTIONS = frozenset({
    "twitter_post", "twitter_reply", "twitter_quote", "twitter_thread",
    "twitter_delete_tweet", "twitter_like", "twitter_unlike", "twitter_retweet",
    "twitter_unretweet", "twitter_follow", "twitter_unfollow", "twitter_mute",
    "twitter_unmute", "twitter_block", "twitter_dm",
})

_FALSEY = {"0", "false", "no", "off", ""}


def twitter_write_enabled() -> bool:
    """Gate for the Twitter WRITE surface (default OFF; ON under effective
    AUTONOMY_MODE=autonomous via _mode_capability_default). Explicit
    TWITTER_ENABLED always wins over the mode default."""
    raw = os.getenv("TWITTER_ENABLED")
    if raw is not None:
        return raw.strip().lower() not in _FALSEY
    from core.config_policy import _mode_capability_default
    return _mode_capability_default("TWITTER_ENABLED")


class TwitterTool(BaseTool):
    """Service that interacts with the Twitter API."""

    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {
            'rate_limit_manager': 'Rate limit management',
        }

    @property
    def optional_services(self) -> Dict[str, str]:
        """Get optional services."""
        return {
            'cache_manager': 'Cache management service',
            # Optional: used only for persistence when a server container provides it.
            # NOT required to post/read — keeping it optional lets the lightweight
            # headless/CLI container load twitter (which has no database_manager).
            'database_manager': 'Database management',
        }

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        """Initialize TwitterTool."""
        super().__init__(name=name, config=config, container=container)
        
        # Initialize API client
        self.client = None
        self.api_v1 = None  # v1.1 tweepy.API for media upload (G1)
        self._initialized = False
        self._lazy_initialized = False
        self._lazy_init_lock = asyncio.Lock()
        # Per-class sliding-window rate-limit state (G1).
        self._write_times: List[float] = []
        self._dm_times: List[float] = []
        
        # Get Twitter config
        twitter_config = config.get_twitter_config()
        self.api_key = twitter_config.get('api_key')
        self.api_secret = twitter_config.get('api_secret')
        self.access_token = twitter_config.get('access_token')
        self.access_token_secret = twitter_config.get('access_token_secret')
        self.bearer_token = twitter_config.get('bearer_token')

        # Check credentials
        missing_creds = []
        for name, value in twitter_config.items():
            if not value:
                missing_creds.append(name.upper())
        
        if missing_creds:
            self.logger.warning(f"Missing Twitter credentials: {', '.join(missing_creds)}")
            self._enabled = False
        else:
            self._enabled = True

    async def initialize(self) -> None:
        """Initialize the Twitter service using the BaseService implementation.
        
        This ensures proper dependency injection and status tracking.
        """
        # Use the base class initialize which handles service dependencies
        # and calls _initialize() internally
        await super().initialize()
    
    async def _initialize(self) -> None:
        """Initialize Twitter service."""
        try:
            # Set a flag to indicate we're in initialization
            self._initializing = True
            
            # Initialize API client using the credentials from BotConfig
            self.client = tweepy.Client(
                bearer_token=self.bearer_token,
                consumer_key=self.api_key,
                consumer_secret=self.api_secret,
                access_token=self.access_token,
                access_token_secret=self.access_token_secret,
                wait_on_rate_limit=False  # We handle rate limiting ourselves
            )
            self._init_v1_client()

            # Test connection
            await self._test_api_connection()
            # No need to set _status here as the BaseService initialize method will do it
            
        except Exception as e:
            self._status = ToolStatus.FAILED
            self._error_message = str(e)
            raise ToolError(f"Failed to initialize Twitter service: {e}")
        finally:
            # Clear the initialization flag
            self._initializing = False

    async def _test_api_connection(self) -> None:
        """Test the connection to the Twitter API."""
        try:
            # Test connection by getting the authenticated user's profile
            me = await self._make_request(
                func=self.client.get_me,
                endpoint_type='users'  # Endpoint type for rate limiting
            )
            
            if me and hasattr(me, 'data'):
                self.logger.info("Twitter client initialized successfully")
            else:
                self.logger.warning("Twitter API returned empty response during initialization")
                
        except RateLimitError as e:
            # Don't fail initialization due to rate limits
            self.logger.warning(f"Twitter API rate limit hit during initialization: {e}")
            # We'll consider this a successful init since the service can be used later
            
        except Exception as e:
            self.logger.warning(f"Twitter API test failed: {e}")
            # Only raise if this isn't during initialization
            if not getattr(self, '_initializing', False):
                raise

    async def _cleanup(self) -> None:
        """Clean up Twitter service resources."""
        try:
            if self.client:
                self.client = None
            self.logger.info(f"{self.name} cleaned up successfully")
        except Exception as e:
            self.logger.error(f"Failed to cleanup {self.name}: {e}")
            raise

    async def _initialize_client(self) -> None:
        """Initialize the Twitter API client."""
        try:
            import tweepy # type: ignore
            
            # Initialize API client with credentials
            self.client = tweepy.Client(
                bearer_token=self.bearer_token,
                consumer_key=self.api_key,
                consumer_secret=self.api_secret,
                access_token=self.access_token,
                access_token_secret=self.access_token_secret,
                wait_on_rate_limit=False  # Changed to False to handle rate limits ourselves
            )
            self._init_v1_client()

            # Test connection with rate limit handling
            try:
                me = await self._make_request(
                    func=self.client.get_me,
                    endpoint_type='users'
                )
                
                if me and hasattr(me, 'data'):
                    self.logger.info("Twitter client initialized successfully")
                else:
                    self.logger.warning("Twitter API returned empty response during initialization")
                    
            except RateLimitError as e:
                # Log warning but continue initialization
                self.logger.warning(
                    f"Twitter API rate limit hit during initialization. Service will be enabled "
                    f"but some features may be temporarily unavailable: {str(e)}"
                )
            except Exception as e:
                self.logger.warning(f"Twitter API test failed during initialization: {str(e)}")
                
            # Enable service even if test fails
            self._enabled = True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize Twitter client: {e}")
            self._enabled = False

    async def _lazy_init(self) -> None:
        """Lazily initialize the Twitter client when first needed."""
        if self._lazy_initialized:
            return
            
        async with self._lazy_init_lock:
            if self._lazy_initialized:
                return
                
            try:
                # Test connection with rate limiting
                me = await self._make_request(
                    func=self.client.get_me,
                    endpoint_type='users'
                )
                
                if not me or (hasattr(me, 'data') and not me.data):
                    raise ServiceError("Failed to get user data from Twitter API")
                
                self._lazy_initialized = True
                self.logger.info("Twitter client connection test successful")
                
            except RateLimitError as e:
                # Log warning but don't fail initialization
                self.logger.warning(f"Rate limit encountered during Twitter connection test: {e}")
                self._lazy_initialized = True
            except Exception as e:
                self.logger.error(f"Failed to test Twitter connection: {e}")
                raise

    async def _execute_request(self, func, *args, **kwargs):
        """Execute the actual request.
        
        This method directly executes the API call and handles possible rate limit errors.
        
        Args:
            func: The function to call
            *args: Function arguments
            **kwargs: Keyword arguments for the function
        """
        try:
            # Ensure we have a callable function
            if not callable(func):
                raise ValueError(f"No callable function provided to _execute_request: func={func}, args={args}")
            
            response = func(*args, **kwargs)
            
            # Check remaining rate limit
            if hasattr(response, 'response') and hasattr(response.response, 'headers'):
                remaining = response.response.headers.get('x-rate-limit-remaining')
                if remaining and int(remaining) < 10:
                    self.logger.warning(f"Twitter API rate limit low: {remaining} requests remaining")
            
            return response
            
        except tweepy.errors.TooManyRequests:
            raise RateLimitError(
                message="Twitter rate limit exceeded",
                service="twitter"
            )
        except Exception as e:
            self.logger.error(f"Error executing Twitter request: {e}")
            raise

    async def _make_request(self, func: callable, endpoint_type: str, *args, **kwargs) -> Any:
        """Make a rate-limited request."""
        if not self.client:
            raise ConfigurationError("Twitter client not initialized")
        
        try:
            # Use the rate_limiter property from BaseService
            if self.rate_limiter:
                return await self.rate_limiter.execute_with_rate_limit(
                    service='twitter',
                    func=func,  # Pass func directly as the function parameter
                    endpoint_type=endpoint_type,
                    *args,      # Pass args directly
                    **kwargs    # Pass kwargs directly
                )
            else:
                # Fallback to direct execution if rate limiter is not available
                self.logger.warning("Rate limiter not available, executing request directly")
                return await self._execute_request(func, *args, **kwargs)
        except RateLimitError:
            if getattr(self.rate_limiter, '_initialization_mode', False):
                self.logger.warning("Rate limit hit during initialization")
                return None
            raise
        except Exception as e:
            raise APIError(f"Twitter request failed: {str(e)}")

    async def get_tweet(self, tweet_id: str) -> Optional[Dict]:
        """Fetch a single tweet by ID.
        
        Args:
            tweet_id: The ID of the tweet to fetch
            
        Returns:
            Dictionary containing tweet data or None if not found
            
        Raises:
            tweepy.errors.TweepyException: For Twitter API errors
            ValueError: For invalid tweet ID format
        """
        try:
            if not tweet_id or not str(tweet_id).strip():
                raise ValueError("Tweet ID cannot be empty")
                
            if not self.client:
                raise RuntimeError("Twitter client not initialized")

            response = await self._make_request(
                func=self.client.get_tweet,
                endpoint_type='tweets',
                id=tweet_id,
                expansions=["author_id", "referenced_tweets.id"],
                tweet_fields=["created_at", "text", "author_id", "conversation_id", "referenced_tweets", "public_metrics"],
                user_fields=["username", "name", "description"]
            )
            
            if not response or not hasattr(response, 'data') or not response.data:
                self.logger.warning(f"No tweet data found for ID: {tweet_id}")
                return None

            tweet_data = response.data
                
            # Format tweet data
            tweet = {
                "id": tweet_id,
                "text": tweet_data.text,
                "author_id": tweet_data.author_id,
                "created_at": tweet_data.created_at.isoformat() if hasattr(tweet_data, 'created_at') else None,
                "conversation_id": getattr(tweet_data, 'conversation_id', None),
                "public_metrics": getattr(tweet_data, 'public_metrics', {})
            }
            
            # Add referenced tweets if available
            if hasattr(tweet_data, 'referenced_tweets') and tweet_data.referenced_tweets:
                tweet["referenced_tweets"] = [
                    {"type": ref.type, "id": ref.id}
                    for ref in tweet_data.referenced_tweets
                ]
            
            # Add author info if available
            if response.includes and "users" in response.includes:
                author = next(
                    (u for u in response.includes["users"] if u.id == tweet_data.author_id),
                    None
                )
                if author:
                    tweet["author"] = {
                        "id": author.id,
                        "username": author.username,
                        "name": author.name,
                        "description": author.description
                    }
                    
            return tweet

        except tweepy.errors.NotFound:
            self.logger.warning(f"Tweet not found: {tweet_id}")
            return None
        except tweepy.errors.TweepyException as e:
            self.logger.error(f"Twitter API error getting tweet {tweet_id}: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error getting tweet {tweet_id}: {str(e)}", exc_info=True)
            raise

    async def get_user_timeline(self, username_or_id: str, max_results: int = 10) -> Optional[List[Dict]]:
        """Get a user's timeline tweets.
        
        Args:
            username_or_id: Username or user ID to get timeline for
            max_results: Maximum number of tweets to return
            
        Returns:
            List of tweet dictionaries or None if error
        """
        try:
            # First get user ID if username provided
            if not str(username_or_id).isdigit():
                user = await self.get_user_by_identifier(username_or_id)
                if not user:
                    self.logger.error(f"Could not find user: {username_or_id}")
                    return None
                user_id = user['id']
            else:
                user_id = username_or_id

            response = await self._make_request(
                func=self.client.get_users_tweets,
                endpoint_type='tweets',
                id=user_id,  # Use resolved user_id
                max_results=max_results,
                tweet_fields=['created_at', 'text', 'public_metrics'],
                expansions=['author_id']
            )
            
            if response and hasattr(response, 'data'):
                return [
                    {
                        'id': str(tweet.id),
                        'text': tweet.text,
                        'created_at': tweet.created_at.isoformat()
                        if getattr(tweet, 'created_at', None) else None,
                        'metrics': getattr(tweet, 'public_metrics', {})
                    }
                    for tweet in response.data
                ]
            return None
            
        except Exception as e:
            self.logger.error(f"Error getting user timeline: {str(e)}", exc_info=True)
            return None

    async def get_conversation_thread(self, conversation_id: str, max_results: int = 10) -> Optional[Dict]:
        """Get tweets in a conversation thread.
        
        Args:
            conversation_id: ID of the conversation to fetch
            max_results: Maximum number of tweets to fetch (default: 10)
            
        Returns:
            Dict containing thread data or None if not found
        """
        try:
            # Ensure max_results is within valid range
            max_results = max(10, min(100, max_results))
            
            # Get initial tweet using _make_request wrapper
            initial_tweet = await self._make_request(
                func=self.client.get_tweet,
                endpoint_type='tweets',
                id=conversation_id,
                tweet_fields=['author_id', 'conversation_id', 'created_at', 'text', 'referenced_tweets'],
                expansions=['author_id', 'referenced_tweets.id', 'referenced_tweets.id.author_id'],
                user_fields=['id', 'name', 'username', 'description']
            )
            
            if not initial_tweet or not hasattr(initial_tweet, 'data'):
                self.logger.warning(f"Conversation {conversation_id} not found")
                return None
            
            # Get conversation tweets with expansions
            conversation_tweets = await self._make_request(
                func=self.client.search_recent_tweets,
                endpoint_type='search',
                query=f"conversation_id:{conversation_id}",
                max_results=max_results,
                tweet_fields=['author_id', 'conversation_id', 'created_at', 'text', 'referenced_tweets'],
                expansions=['author_id', 'referenced_tweets.id', 'referenced_tweets.id.author_id'],
                user_fields=['id', 'name', 'username', 'description']
            )
            
            # Initialize thread data structure
            thread_data = {
                'data': [],
                'includes': {'users': [], 'tweets': []},
                'user_map': {}  # New: mapping of user_id to user data
            }
            
            # Build user map from initial tweet
            if hasattr(initial_tweet, 'includes') and 'users' in initial_tweet.includes:
                for user in initial_tweet.includes['users']:
                    user_dict = {
                        'id': str(user.id),
                        'username': user.username,
                        'name': user.name,
                        'description': getattr(user, 'description', None)
                    }
                    thread_data['user_map'][str(user.id)] = user_dict
                    thread_data['includes']['users'].append(user_dict)
            
            # Add initial tweet data with username
            if hasattr(initial_tweet, 'data'):
                author_id = str(initial_tweet.data.author_id)
                tweet_dict = {
                    'id': str(initial_tweet.data.id),
                    'text': initial_tweet.data.text,
                    'author_id': author_id,
                    'username': thread_data['user_map'].get(author_id, {}).get('username'),
                    'created_at': initial_tweet.data.created_at.isoformat() if hasattr(initial_tweet.data, 'created_at') else None,
                    'conversation_id': initial_tweet.data.conversation_id,
                    'referenced_tweets': [
                        {'type': ref.type, 'id': ref.id}
                        for ref in initial_tweet.data.referenced_tweets
                    ] if hasattr(initial_tweet.data, 'referenced_tweets') else []
                }
                thread_data['data'].append(tweet_dict)
            
            # Process conversation tweets
            if conversation_tweets and hasattr(conversation_tweets, 'data'):
                # Update user map with any new users
                if hasattr(conversation_tweets, 'includes') and 'users' in conversation_tweets.includes:
                    for user in conversation_tweets.includes['users']:
                        if str(user.id) not in thread_data['user_map']:
                            user_dict = {
                                'id': str(user.id),
                                'username': user.username,
                                'name': user.name,
                                'description': getattr(user, 'description', None)
                            }
                            thread_data['user_map'][str(user.id)] = user_dict
                            thread_data['includes']['users'].append(user_dict)
                
                # Add conversation tweets with usernames
                existing_ids = {t['id'] for t in thread_data['data']}
                for tweet in conversation_tweets.data:
                    if str(tweet.id) not in existing_ids:
                        author_id = str(tweet.author_id)
                        tweet_dict = {
                            'id': str(tweet.id),
                            'text': tweet.text,
                            'author_id': author_id,
                            'username': thread_data['user_map'].get(author_id, {}).get('username'),
                            'created_at': tweet.created_at.isoformat() if hasattr(tweet, 'created_at') else None,
                            'conversation_id': tweet.conversation_id,
                            'referenced_tweets': [
                                {'type': ref.type, 'id': ref.id}
                                for ref in tweet.referenced_tweets
                            ] if hasattr(tweet, 'referenced_tweets') else []
                        }
                        thread_data['data'].append(tweet_dict)
                
                # Sort tweets by creation time
                thread_data['data'].sort(
                    key=lambda x: x.get('created_at', ''),
                    reverse=False  # Oldest first
                )
            
            self.logger.debug(
                f"[THREAD] Found {len(thread_data['data'])} tweets and {len(thread_data['user_map'])} users "
                f"in conversation {conversation_id}"
            )
            return thread_data
            
        except Exception as e:
            self.logger.error(f"Error fetching conversation thread: {str(e)}", exc_info=True)
            return None

    async def get_user_by_identifier(self, identifier: str) -> Optional[Dict]:
        """Get user data by username or ID.
        
        Args:
            identifier: Username or user ID
            
        Returns:
            Dictionary containing user data or None if not found
        """
        try:
            if not identifier:
                raise ValueError("User identifier cannot be empty")
                
            if not self.client:
                raise RuntimeError("Twitter client not initialized")

            # Convert identifier to string if it's an integer
            str_identifier = str(identifier)

            response = await self._make_request(
                func=self.client.get_user,
                endpoint_type='users',  # Specify endpoint type for rate limiting
                id=str_identifier if str_identifier.isdigit() else None,
                username=str_identifier if not str_identifier.isdigit() else None,
                user_fields=["description", "created_at", "public_metrics", "protected", "verified"]
            )
            
            if not response or not hasattr(response, 'data') or not response.data:
                self.logger.warning(f"No user data found for identifier: {identifier}")
                return None

            user_data = response.data
            
            # Format user data
            user = {
                "id": user_data.id,
                "username": user_data.username,
                "name": user_data.name,
                "description": user_data.description,
                "created_at": user_data.created_at.isoformat() if hasattr(user_data, 'created_at') else None,
                "public_metrics": getattr(user_data, 'public_metrics', {}),
                "protected": getattr(user_data, 'protected', False),
                "verified": getattr(user_data, 'verified', False)
            }
            
            return user

        except tweepy.errors.NotFound:
            self.logger.warning(f"User not found: {identifier}")
            return None
        except tweepy.errors.TweepyException as e:
            self.logger.error(f"Twitter API error getting user {identifier}: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error getting user data: {str(e)}", exc_info=True)
            raise

    async def get_user_profile_async(self, username: str) -> Optional[Dict]:
        """Get a user's profile data asynchronously.
        
        Args:
            username: Twitter username
            
        Returns:
            Dictionary containing user profile data or None if not found
        """
        return await self.get_user_by_identifier(username)

    async def get_engagement_profiles(self, username: Optional[str] = None) -> List[Dict]:
        """Get profiles for engagement based on configured criteria.
        
        Args:
            username: Optional username to get specific profile
            
        Returns:
            List of engagement profile dictionaries
            
        Raises:
            tweepy.errors.TweepyException: For Twitter API errors
        """
        try:
            self.logger.debug(f"Getting engagement profiles for username: {username}")
            profiles = []
            
            if username:
                # Get user profile data
                self.logger.debug(f"Fetching user profile for: {username}")
                user = await self.get_user_profile_async(username)
                
                if user:
                    # Get recent tweets for engagement analysis
                    tweets = await self.get_user_timeline(user['id'], max_results=10) or []
                    
                    # Extract metrics from user data
                    metrics = user.get('public_metrics', {})
                    
                    profile = {
                        'id': user['id'],
                        'username': user['username'],
                        'name': user['name'],
                        'description': user['description'],
                        'followers_count': metrics.get('followers_count', 0),
                        'following_count': metrics.get('following_count', 0),
                        'tweet_count': metrics.get('tweet_count', 0),
                        'engagement_score': self._calculate_engagement_score(user, tweets),
                        'recent_tweets': tweets
                    }
                    self.logger.debug(f"Created profile for {username}")
                    profiles.append(profile)
                else:
                    self.logger.warning(f"No user profile found for username: {username}")
            
            return profiles
            
        except tweepy.errors.TweepyException as e:
            self.logger.error(f"Twitter API error getting engagement profiles: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error getting engagement profiles: {str(e)}", exc_info=True)
            # Return empty list on error instead of raising
            return []

    def _calculate_engagement_score(self, user: Dict, tweets: Optional[List[Dict]] = None) -> float:
        """Calculate engagement score based on user metrics and recent tweets.
        
        Args:
            user: User profile data
            tweets: Optional list of recent tweets
            
        Returns:
            float: Engagement score between 0 and 1
        """
        try:
            score = 0.0
            weights = {
                'followers': 0.3,
                'tweets': 0.2,
                'recent_activity': 0.5
            }
            
            # Base score from follower count (log scale to handle large numbers)
            followers = user.get('followers_count', 0)
            if followers > 0:
                score += weights['followers'] * min(1.0, (1 + math.log10(followers)) / 7.0)
            
            # Activity score from tweet count
            tweet_count = user.get('tweet_count', 0)
            if tweet_count > 0:
                score += weights['tweets'] * min(1.0, (1 + math.log10(tweet_count)) / 5.0)
            
            # Recent activity score from provided tweets
            if tweets:
                recent_score = 0.0
                for tweet in tweets:
                    metrics = tweet.get('public_metrics', {})
                    likes = metrics.get('like_count', 0)
                    retweets = metrics.get('retweet_count', 0)
                    replies = metrics.get('reply_count', 0)
                    
                    # Calculate engagement rate for this tweet
                    engagement = likes + (retweets * 2) + (replies * 3)  # Weight replies higher
                    if followers > 0:
                        engagement_rate = min(1.0, engagement / followers)
                        recent_score += engagement_rate
                
                # Average the recent tweet scores
                if len(tweets) > 0:
                    recent_score /= len(tweets)
                    score += weights['recent_activity'] * recent_score
            
            return min(1.0, max(0.0, score))
            
        except Exception as e:
            self.logger.error(f"Error calculating engagement score: {str(e)}")
            return 0.5  # Return middle score on error

    async def get_authenticated_user_id(self) -> Optional[str]:
        """Get the authenticated user's ID.
        
        Returns:
            String containing the user ID or None if failed
        """
        try:
            user_profile = await self.get_user_profile()
            if user_profile and user_profile.get('data', {}).get('id'):
                return user_profile['data']['id']
            return None
            
        except Exception as e:
            self.logger.error(f"Error getting authenticated user ID: {str(e)}", exc_info=True)
            return None

    async def create_tweet(self, text: str, media_ids: Optional[List[str]] = None) -> Optional[Dict]:
        """Alias for post() method to maintain compatibility.
        
        Args:
            text: Tweet text content
            media_ids: Optional list of media IDs to attach
            
        Returns:
            Dictionary containing the created tweet data or None if failed
        """
        return await self.post(text=text, media_ids=media_ids)

    async def like(self, tweet_id: str) -> bool:
        """Like a tweet.
        
        Args:
            tweet_id: ID of the tweet to like
            
        Returns:
            bool: True if successful, False otherwise
            
        Raises:
            ValueError: If tweet_id is invalid
            tweepy.errors.TweepyException: For Twitter API errors
        """
        try:
            response = await self._make_request(
                func=self.client.like,
                endpoint_type='likes',
                tweet_id=tweet_id
            )
            return bool(response and response.data and response.data.get('liked'))
        except Exception as e:
            self.logger.error(f"Error liking tweet {tweet_id}: {str(e)}")
            return False

    async def unlike(self, tweet_id: str) -> bool:
        """Unlike a tweet.
        
        Args:
            tweet_id: ID of the tweet to unlike
            
        Returns:
            bool: True if successful, False otherwise
            
        Raises:
            ValueError: If tweet_id is invalid
            tweepy.errors.TweepyException: For Twitter API errors
        """
        try:
            if not tweet_id or not str(tweet_id).strip():
                raise ValueError("Tweet ID cannot be empty")
                
            if not self.client:
                raise RuntimeError("Twitter client not initialized")
                
            response = await self._make_request(
                func=self.client.unlike,
                tweet_id=tweet_id
            )
            
            if response and hasattr(response, 'data') and response.data:
                self.logger.info(f"Successfully unliked tweet {tweet_id}")
                return True
                
            self.logger.warning(f"Failed to unlike tweet {tweet_id}")
            return False
            
        except tweepy.errors.Forbidden as e:
            self.logger.warning(f"Not allowed to unlike tweet {tweet_id}: {str(e)}")
            return False
        except tweepy.errors.NotFound:
            self.logger.warning(f"Tweet not found: {tweet_id}")
            return False
        except tweepy.errors.TweepyException as e:
            self.logger.error(f"Twitter API error unliking tweet: {str(e)}")
            raise
        except ValueError as e:
            self.logger.error(str(e))
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error unliking tweet: {str(e)}", exc_info=True)
            raise

    async def retweet(self, tweet_id: str) -> bool:
        """Retweet a tweet.
        
        Args:
            tweet_id: ID of the tweet to retweet
            
        Returns:
            bool: True if successful, False otherwise
            
        Raises:
            ValueError: If tweet_id is invalid
            tweepy.errors.TweepyException: For Twitter API errors
        """
        try:
            response = await self._make_request(
                func=self.client.retweet,
                endpoint_type='retweets',
                tweet_id=tweet_id
            )
            return bool(response and response.data and response.data.get('retweeted'))
        except Exception as e:
            self.logger.error(f"Error retweeting tweet {tweet_id}: {str(e)}")
            return False

    async def unretweet(self, tweet_id: str) -> bool:
        """Remove a retweet.
        
        Args:
            tweet_id: ID of the tweet to unretweet
            
        Returns:
            bool: True if successful, False otherwise
            
        Raises:
            ValueError: If tweet_id is invalid
            tweepy.errors.TweepyException: For Twitter API errors
        """
        try:
            if not tweet_id or not str(tweet_id).strip():
                raise ValueError("Tweet ID cannot be empty")
                
            if not self.client:
                raise RuntimeError("Twitter client not initialized")
                
            response = await self._make_request(
                func=self.client.unretweet,
                tweet_id=tweet_id
            )
            
            if response and hasattr(response, 'data') and response.data:
                self.logger.info(f"Successfully unretweeted tweet {tweet_id}")
                return True
                
            self.logger.warning(f"Failed to unretweet tweet {tweet_id}")
            return False
            
        except tweepy.errors.Forbidden as e:
            self.logger.warning(f"Not allowed to unretweet tweet {tweet_id}: {str(e)}")
            return False
        except tweepy.errors.NotFound:
            self.logger.warning(f"Tweet not found: {tweet_id}")
            return False
        except tweepy.errors.TweepyException as e:
            self.logger.error(f"Twitter API error unretweeting tweet: {str(e)}")
            raise
        except ValueError as e:
            self.logger.error(str(e))
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error unretweeting tweet: {str(e)}", exc_info=True)
            raise

    async def reply(self, tweet_id: str, text: str) -> Optional[Dict]:
        """Reply to a tweet.
        
        Args:
            tweet_id: ID of the tweet to reply to
            text: Reply text content
            
        Returns:
            Dict: Created tweet data or None if failed
            
        Raises:
            ValueError: If tweet_id or text is invalid
            tweepy.errors.TweepyException: For Twitter API errors
        """
        try:
            response = await self._make_request(
                func=self.client.create_tweet,
                endpoint_type='replies',
                text=text,
                in_reply_to_tweet_id=tweet_id
            )
            
            if response and hasattr(response, 'data'):
                return {
                    'id': response.data['id'],
                    'text': response.data['text']
                }
            return None
        except Exception as e:
            self.logger.error(f"Error replying to tweet {tweet_id}: {str(e)}")
            return None

    async def quote_tweet(self, tweet_id: str, text: str) -> Optional[Dict]:
        """Quote a tweet.
        
        Args:
            tweet_id: ID of tweet to quote
            text: Text content for the quote tweet
            
        Returns:
            Dict containing the created tweet data or None if failed
        """
        try:
            # Verify tweet exists first
            tweet = await self.get_tweet(tweet_id)
            if not tweet:
                self.logger.warning(f"Could not find tweet {tweet_id} to quote")
                return None
                
            # Create quote tweet
            response = await self._make_request(
                func=self.client.create_tweet,
                endpoint_type='tweets',  # Add endpoint type
                text=text,
                quote_tweet_id=tweet_id
            )
            
            if response and hasattr(response, 'data'):
                tweet_data = response.data
                self.logger.info(f"Successfully quoted tweet {tweet_id}")
                return {
                    'data': {
                        'id': str(tweet_data.id),
                        'text': tweet_data.text,
                        'created_at': tweet_data.created_at.isoformat() if hasattr(tweet_data, 'created_at') else None,
                        'conversation_id': getattr(tweet_data, 'conversation_id', None),
                        'quoted_tweet_id': tweet_id
                    }
                }
            
            self.logger.warning(f"Failed to quote tweet {tweet_id}")
            return None
            
        except Exception as e:
            self.logger.error(f"Unexpected error quoting tweet: {str(e)}", exc_info=True)
            return None

    async def get_tweets_batch(self, tweet_ids: List[str]) -> Dict[str, Dict]:
        """Get multiple tweets by their IDs.
        
        Args:
            tweet_ids: List of tweet IDs to fetch
            
        Returns:
            Dict mapping tweet IDs to tweet data
        """
        try:
            self.logger.debug(f"Processing batch of {len(tweet_ids)} tweets")
            
            response = await self._make_request(
                func=self.client.get_tweets,
                endpoint_type='tweets',  # Add endpoint_type parameter
                ids=tweet_ids,
                tweet_fields=['created_at', 'text', 'referenced_tweets', 'public_metrics']
            )
            
            if not response or not response.data:
                return {}
                
            # Convert list of tweets to dict keyed by ID
            return {str(tweet.id): tweet.data for tweet in response.data}
            
        except Exception as e:
            self.logger.error(f"Unexpected error fetching tweets batch: {str(e)}")
            if hasattr(e, '__traceback__'):
                self.logger.error(traceback.format_exc())
            return {}

    def _filter_new_tweets(self, conversation_id: str, tweets: List[Dict]) -> List[Dict]:
        """Filter out already processed tweets.
        
        Args:
            conversation_id: ID of the conversation
            tweets: List of tweets to filter
            
        Returns:
            List of unprocessed tweets
            
        Raises:
            ValueError: If conversation_id is invalid or tweets is invalid
        """
        try:
            if not conversation_id or not str(conversation_id).strip():
                raise ValueError("Conversation ID cannot be empty")
                
            if not isinstance(tweets, list):
                raise ValueError(f"tweets must be a list, got {type(tweets)}")
                
            if not hasattr(self, 'active_conversations'):
                self.logger.warning("No active conversations found")
                return []
                
            if conversation_id not in self.active_conversations:
                self.logger.warning(f"Conversation {conversation_id} not found in active conversations")
                return []
                
            processed_tweets = self.active_conversations[conversation_id].get('processed_tweets', set())
            new_tweets = []
            
            for tweet in tweets:
                if not isinstance(tweet, dict):
                    self.logger.warning(f"Invalid tweet format: {type(tweet)}")
                    continue
                    
                tweet_id = tweet.get('id')
                if not tweet_id:
                    self.logger.warning("Tweet missing ID")
                    continue
                    
                if tweet_id not in processed_tweets:
                    # Check if it's a reply to a tweet we've processed
                    referenced_tweets = tweet.get('referenced_tweets', [])
                    is_reply = any(
                        ref.get('type') == 'replied_to' 
                        for ref in referenced_tweets 
                        if isinstance(ref, dict)
                    )
                    
                    if is_reply:
                        # Add replies to new tweets even if the parent is processed
                        new_tweets.append(tweet)
                    else:
                        # For non-replies, add if not processed
                        new_tweets.append(tweet)
            
            self.logger.debug(
                f"Found {len(new_tweets)} new tweets out of {len(tweets)} total tweets in conversation {conversation_id}"
            )
            return new_tweets
            
        except Exception as e:
            self.logger.error(f"Error filtering tweets: {str(e)}", exc_info=True)
            return []

    async def post(self, text: str, media_ids: Optional[List[str]] = None) -> Optional[Dict]:
        """Post a new tweet.
        
        Args:
            text: Tweet text content
            media_ids: Optional list of media IDs to attach
            
        Returns:
            Dictionary containing the created tweet data or None if failed
        """
        try:
            # Build kwargs for create_tweet
            kwargs = {'text': text}
            if media_ids and len(media_ids) > 0:
                kwargs['media_ids'] = media_ids

            response = await self._make_request(
                func=self.client.create_tweet,
                endpoint_type='tweets',
                **kwargs
            )
            
            if response and hasattr(response, 'data'):
                tweet_data = response.data
                if isinstance(tweet_data, dict):
                    return {
                        'data': {
                            'id': str(tweet_data.get('id')),
                            'text': tweet_data.get('text'),
                            'created_at': tweet_data.get('created_at')
                        }
                    }
                else:
                    return {
                        'data': {
                            'id': str(tweet_data.id),
                            'text': tweet_data.text,
                            'created_at': tweet_data.created_at.isoformat() if hasattr(tweet_data, 'created_at') else None
                        }
                    }
            return None
            
        except Exception as e:
            self.logger.error(f"Error posting tweet: {str(e)}", exc_info=True)
            return None

    async def get_user_profile(self) -> Optional[Dict]:
        """Get the authenticated user's profile.
        
        Returns:
            Dictionary containing user profile data or None if failed
        """
        try:
            response = await self._make_request(
                func=self.client.get_me,
                endpoint_type='users',
                user_fields=['id', 'name', 'username', 'description', 'public_metrics']
            )
            
            if response and hasattr(response, 'data'):
                user_data = response.data
                return {
                    'data': {
                        'id': str(user_data.id),
                        'name': user_data.name,
                        'username': user_data.username,
                        'description': user_data.description,
                        'public_metrics': getattr(user_data, 'public_metrics', {})
                    }
                }
            return None
            
        except Exception as e:
            self.logger.error(f"Error getting user profile: {str(e)}", exc_info=True)
            return None

    async def get_user_following(self, user_id: str, max_results: int = 100) -> Optional[Dict]:
        """Get list of users that the specified user follows.
        
        Args:
            user_id: User ID to get following list for
            max_results: Maximum number of results to return
            
        Returns:
            Dictionary containing following user data or None if failed
        """
        try:
            response = await self._make_request(
                func=self.client.get_users_following,
                endpoint_type='users',
                id=user_id,
                max_results=max_results,
                user_fields=['id', 'name', 'username', 'description', 'public_metrics']
            )
            
            if response and hasattr(response, 'data'):
                following_data = []
                for user in response.data:
                    following_data.append({
                        'id': str(user.id),
                        'name': user.name,
                        'username': user.username,
                        'description': user.description,
                        'public_metrics': getattr(user, 'public_metrics', {})
                    })
                    
                return {
                    'data': following_data,
                    'meta': {
                        'result_count': len(following_data)
                    }
                }
            return None
            
        except Exception as e:
            self.logger.error(f"Error getting user following: {str(e)}", exc_info=True)
            return None

    async def get_own_user_id(self) -> Optional[str]:
        """Alias for get_authenticated_user_id() to maintain compatibility.
        
        Returns:
            String containing the user ID or None if failed
        """
        return await self.get_authenticated_user_id()

    async def search(
        self,
        query: str,
        max_results: int = 10,
        include_replies: bool = False,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """Search for tweets.
        
        Args:
            query: Search query
            max_results: Maximum number of results to return (must be between 10 and 100)
            include_replies: Whether to include replies
            **kwargs: Additional search parameters
            
        Returns:
            List of tweet dictionaries
            
        Raises:
            APIError: If search fails
            ConfigurationError: If service is not enabled
            ValueError: If max_results is not between 10 and 100
        """
        if not self._enabled:
            raise ConfigurationError("Twitter service is not enabled - missing credentials")
            
        # Validate max_results
        if max_results < 10:
            self.logger.warning(f"Adjusting max_results from {max_results} to minimum allowed value of 10")
            max_results = 10
        elif max_results > 100:
            self.logger.warning(f"Adjusting max_results from {max_results} to maximum allowed value of 100")
            max_results = 100
            
        await self._ensure_initialized()
        
        try:
            # Build query
            if not include_replies:
                query = f"{query} -is:reply"
                
            # Execute search with rate limit handling
            try:
                response = await self._make_request(
                    func=self.client.search_recent_tweets,
                    endpoint_type='search',
                    query=query,
                    max_results=max_results,
                    tweet_fields=['created_at', 'public_metrics', 'entities'],
                    expansions=['author_id'],
                    user_fields=['username', 'name', 'profile_image_url'],
                    **kwargs
                )
                
                # Format response
                tweets = []
                users = {user.id: user for user in response.includes['users']} if response.includes else {}
                
                for tweet in response.data or []:
                    author = users.get(tweet.author_id)
                    tweets.append({
                        'id': tweet.id,
                        'text': tweet.text,
                        'created_at': tweet.created_at.isoformat() if hasattr(tweet, 'created_at') else None,
                        'metrics': tweet.public_metrics,
                        'author': {
                            'id': author.id,
                            'username': author.username,
                            'name': author.name,
                            'profile_image_url': author.profile_image_url
                        } if author else None,
                        'entities': tweet.entities
                    })
                    
                # If original max_results was less than 10, truncate results
                if len(tweets) > max_results:
                    tweets = tweets[:max_results]
                    
                return tweets
                
            except tweepy.errors.TooManyRequests as e:
                # Extract reset time from headers
                reset_time = None
                if hasattr(e, 'response') and hasattr(e.response, 'headers'):
                    reset_time = e.response.headers.get('x-rate-limit-reset')
                wait_time = int(reset_time) - int(time.time()) if reset_time else 900
                
                # Raise RateLimitError instead of sleeping
                raise RateLimitError(f"Twitter rate limit exceeded. Wait time: {wait_time} seconds")
                
        except Exception as e:
            if isinstance(e, RateLimitError):
                raise
            self.logger.error(f"Twitter search error: {str(e)}")
            raise APIError(f"Twitter search failed: {str(e)}")

    async def get_user(self, username: str) -> Dict[str, Any]:
        """Get user information.
        
        Args:
            username: Twitter username
            
        Returns:
            User information dictionary
            
        Raises:
            APIError: If user lookup fails
        """
        await self._ensure_initialized()
        
        try:
            response = self.client.get_user(
                username=username,
                user_fields=['description', 'public_metrics', 'profile_image_url']
            )
            
            if not response.data:
                raise APIError(f"User {username} not found")
                
            user = response.data
            return {
                'id': user.id,
                'username': user.username,
                'name': user.name,
                'description': user.description,
                'metrics': user.public_metrics,
                'profile_image_url': user.profile_image_url
            }
            
        except tweepy.TweepyException as e:
            raise APIError(f"Twitter user lookup failed: {str(e)}")
        except Exception as e:
            raise APIError(f"Error during Twitter user lookup: {str(e)}")
            
    async def get_tweets(
        self,
        user_id: str,
        max_results: int = 10,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """Get tweets from a user.
        
        Args:
            user_id: Twitter user ID
            max_results: Maximum number of tweets to return
            **kwargs: Additional parameters
            
        Returns:
            List of tweet dictionaries
            
        Raises:
            APIError: If tweet lookup fails
        """
        await self._ensure_initialized()
        
        try:
            response = self.client.get_users_tweets(
                id=user_id,
                max_results=max_results,
                tweet_fields=['created_at', 'public_metrics', 'entities'],
                **kwargs
            )
            
            tweets = []
            for tweet in response.data or []:
                tweets.append({
                    'id': tweet.id,
                    'text': tweet.text,
                    'created_at': tweet.created_at.isoformat(),
                    'metrics': tweet.public_metrics,
                    'entities': tweet.entities
                })
                
            return tweets
            
        except tweepy.TweepyException as e:
            raise APIError(f"Twitter tweets lookup failed: {str(e)}")
        except Exception as e:
            raise APIError(f"Error during Twitter tweets lookup: {str(e)}")

    # =====================================================================
    # G1 — FULL write surface: v1.1 client, helpers, gating, actions
    # =====================================================================

    def _init_v1_client(self) -> None:
        """Build a v1.1 ``tweepy.API`` (OAuth1.0a) for media upload. Fail-open."""
        try:
            if not (self.api_key and self.api_secret and self.access_token and self.access_token_secret):
                self.api_v1 = None
                return
            auth = tweepy.OAuth1UserHandler(
                self.api_key, self.api_secret, self.access_token, self.access_token_secret
            )
            self.api_v1 = tweepy.API(auth)
        except Exception as e:  # pragma: no cover - defensive
            self.api_v1 = None
            self.logger.warning(f"Twitter v1.1 API (media) unavailable: {e}")

    # --- safety knobs ----------------------------------------------------

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        from core.env import int_env
        return int_env(name, default)

    def _write_cap(self) -> int:
        return self._env_int("TWITTER_WRITE_MAX_PER_HOUR", 15)

    def _dm_cap(self) -> int:
        return self._env_int("TWITTER_DM_MAX_PER_HOUR", 5)

    def _require_approval(self) -> bool:
        return os.getenv("TWITTER_REQUIRE_APPROVAL", "true").strip().lower() not in _FALSEY

    def _rate_available(self, *, is_dm: bool) -> bool:
        now = time.time()
        bucket = self._dm_times if is_dm else self._write_times
        cutoff = now - 3600
        bucket[:] = [t for t in bucket if t >= cutoff]
        cap = self._dm_cap() if is_dm else self._write_cap()
        return len(bucket) < cap

    def _rate_record(self, *, is_dm: bool) -> None:
        (self._dm_times if is_dm else self._write_times).append(time.time())

    async def _precheck_write(self, action_name: str, params, execution_context,
                              *, is_dm: bool = False):
        """Rate-limit + approval gate. Returns an error ActionResult to block, or None to proceed."""
        if not self._rate_available(is_dm=is_dm):
            cap = self._dm_cap() if is_dm else self._write_cap()
            kind = "DMs" if is_dm else "writes"
            return self.create_action_result(
                error=f"Twitter rate limit reached: max {cap}/hour for {kind}. Try later.",
                include_in_memory=True,
            )
        if self._require_approval():
            approved = False
            try:
                from tools.controller.approval import get_approval_provider_or_deny, AutoApprover
                try:
                    import tools.controller.approval_interactive  # noqa: F401  # register interactive_cli
                except Exception:
                    pass
                # H9: fail-CLOSED on an unknown provider; and don't SILENTLY auto-approve —
                # TWITTER_REQUIRE_APPROVAL with APPROVAL_PROVIDER unset resolves to AutoApprover
                # (approves everything). Warn loudly so an operator relying on approval knows the
                # write is proceeding with no human gate.
                provider = get_approval_provider_or_deny(os.getenv("APPROVAL_PROVIDER"))
                if isinstance(provider, AutoApprover):
                    self.logger.warning(
                        f"⚠️ Twitter write '{action_name}' auto-approved: TWITTER_REQUIRE_APPROVAL "
                        f"is on but APPROVAL_PROVIDER is unset/auto (no human gate). Set "
                        f"APPROVAL_PROVIDER=interactive_cli for a real prompt."
                    )
                timeout = float(os.getenv("APPROVAL_TIMEOUT_SEC", "30"))
                pdict = params.model_dump() if hasattr(params, "model_dump") else dict(params or {})
                approved = await asyncio.wait_for(
                    provider.request(action_name, pdict, execution_context), timeout=timeout
                )
            except Exception as e:  # timeout / provider error → fail-closed deny
                self.logger.warning(f"twitter approval denied (error) for {action_name}: {e}")
                approved = False
            if not approved:
                return self.create_action_result(
                    error=f"Twitter write '{action_name}' blocked: approval denied.",
                    include_in_memory=True,
                )
        self._rate_record(is_dm=is_dm)
        return None

    # --- internal write helpers -----------------------------------------

    async def _upload_media(self, media_paths: List[str]) -> List[str]:
        """Upload local media via v1.1 and return media_id strings."""
        if not getattr(self, "api_v1", None):
            raise RuntimeError("media upload requires the v1.1 API client (OAuth1.0a creds)")
        ids: List[str] = []
        for path in media_paths:
            media = await self._make_request(
                func=self.api_v1.media_upload, endpoint_type="media", filename=path
            )
            mid = getattr(media, "media_id", None) or getattr(media, "media_id_string", None)
            ids.append(str(mid))
        return ids

    @staticmethod
    def _tweet_from_response(resp) -> Dict[str, Any]:
        if not resp or not hasattr(resp, "data") or not resp.data:
            return {}
        data = resp.data
        if isinstance(data, dict):
            return {"id": str(data.get("id")), "text": data.get("text")}
        return {"id": str(getattr(data, "id", "")), "text": getattr(data, "text", None)}

    async def _compose_tweet(self, *, text: str, media_paths: Optional[List[str]] = None,
                             poll_options: Optional[List[str]] = None,
                             poll_duration_minutes: Optional[int] = None,
                             in_reply_to_tweet_id: Optional[str] = None,
                             quote_tweet_id: Optional[str] = None) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"text": text}
        if media_paths:
            kwargs["media_ids"] = await self._upload_media(media_paths)
        if poll_options:
            kwargs["poll_options"] = list(poll_options)
            if poll_duration_minutes:
                kwargs["poll_duration_minutes"] = int(poll_duration_minutes)
        if in_reply_to_tweet_id:
            kwargs["in_reply_to_tweet_id"] = in_reply_to_tweet_id
        if quote_tweet_id:
            kwargs["quote_tweet_id"] = quote_tweet_id
        resp = await self._make_request(func=self.client.create_tweet, endpoint_type="tweets", **kwargs)
        return self._tweet_from_response(resp)

    async def _resolve_user_id(self, user: str) -> Optional[str]:
        """Return a numeric user id; pass digit strings through, else resolve username."""
        if str(user).isdigit():
            return str(user)
        info = await self.get_user_by_identifier(user)
        if info and info.get("id"):
            return str(info["id"])
        return None

    @staticmethod
    def _split_tweet_text(text: str, limit: int = _TWEET_MAX) -> List[str]:
        """Split text into ≤``limit``-char parts at word boundaries, numbered (i/n).

        P6: the do-what-a-human-would-do fallback for over-long compositions —
        a single part is returned unchanged; multiple parts each carry an
        " (i/n)" counter and every numbered part fits the wire limit.
        """
        text = (text or "").strip()
        if len(text) <= limit:
            return [text]
        reserve = 9  # room for " (99/99)"
        chunk_limit = max(1, limit - reserve)
        parts: List[str] = []
        cur = ""
        for word in text.split():
            # hard-break pathological words longer than a whole chunk
            while len(word) > chunk_limit:
                if cur:
                    parts.append(cur)
                    cur = ""
                parts.append(word[:chunk_limit])
                word = word[chunk_limit:]
            if not cur:
                cur = word
            elif len(cur) + 1 + len(word) <= chunk_limit:
                cur += " " + word
            else:
                parts.append(cur)
                cur = word
        if cur:
            parts.append(cur)
        n = len(parts)
        if n == 1:
            return parts
        return [f"{p} ({i}/{n})" for i, p in enumerate(parts, 1)]

    async def _compose_thread(self, parts: List[str], *,
                              media_paths: Optional[List[str]] = None,
                              in_reply_to_tweet_id: Optional[str] = None,
                              quote_tweet_id: Optional[str] = None,
                              poll_options: Optional[List[str]] = None,
                              poll_duration_minutes: Optional[int] = None) -> List[str]:
        """Post ``parts`` as a chained thread; media/poll/quote ride the head tweet."""
        prev_id = in_reply_to_tweet_id
        ids: List[str] = []
        for i, part in enumerate(parts):
            tweet = await self._compose_tweet(
                text=part,
                media_paths=media_paths if i == 0 else None,
                poll_options=poll_options if i == 0 else None,
                poll_duration_minutes=poll_duration_minutes if i == 0 else None,
                quote_tweet_id=quote_tweet_id if i == 0 else None,
                in_reply_to_tweet_id=prev_id,
            )
            prev_id = tweet.get("id")
            ids.append(str(prev_id))
        return ids

    def _ok(self, msg: str):
        return self.create_action_result(extracted_content=msg, include_in_memory=True)

    def _err(self, msg: str):
        return self.create_action_result(error=msg, include_in_memory=True)

    def _check_ready(self):
        if not self._enabled:
            return self._err("Twitter service not available - missing credentials")
        if not twitter_write_enabled():
            return self._err("Twitter write surface disabled (set TWITTER_ENABLED=true)")
        return None

    # --- compose actions -------------------------------------------------

    @BaseTool.action("Post a tweet (optionally with media and/or a poll).", param_model=TwitterPostAction)
    async def twitter_post(self, params: TwitterPostAction, execution_context=None):
        notready = self._check_ready()
        if notready:
            return notready
        block = await self._precheck_write("twitter_post", params, execution_context)
        if block:
            return block
        try:
            parts = self._split_tweet_text(params.text)
            if len(parts) == 1:
                tweet = await self._compose_tweet(
                    text=parts[0], media_paths=params.media_paths,
                    poll_options=params.poll_options, poll_duration_minutes=params.poll_duration_minutes,
                )
                return self._ok(f"🐦 Posted tweet {tweet.get('id')}")
            ids = await self._compose_thread(
                parts, media_paths=params.media_paths,
                poll_options=params.poll_options, poll_duration_minutes=params.poll_duration_minutes,
            )
            return self._ok(
                f"🐦 Text exceeded 280 chars — posted as a {len(ids)}-tweet thread: {', '.join(ids)}"
            )
        except Exception as e:
            return self._err(f"Error posting tweet: {e}")

    @BaseTool.action("Reply to a tweet (optionally with media).", param_model=TwitterReplyAction)
    async def twitter_reply(self, params: TwitterReplyAction, execution_context=None):
        notready = self._check_ready()
        if notready:
            return notready
        block = await self._precheck_write("twitter_reply", params, execution_context)
        if block:
            return block
        try:
            parts = self._split_tweet_text(params.text)
            if len(parts) == 1:
                tweet = await self._compose_tweet(
                    text=parts[0], media_paths=params.media_paths,
                    in_reply_to_tweet_id=params.tweet_id,
                )
                return self._ok(f"🐦 Replied with tweet {tweet.get('id')}")
            ids = await self._compose_thread(
                parts, media_paths=params.media_paths,
                in_reply_to_tweet_id=params.tweet_id,
            )
            return self._ok(
                f"🐦 Reply exceeded 280 chars — posted as a {len(ids)}-tweet reply thread: {', '.join(ids)}"
            )
        except Exception as e:
            return self._err(f"Error replying: {e}")

    @BaseTool.action("Quote-tweet an existing tweet.", param_model=TwitterQuoteAction)
    async def twitter_quote(self, params: TwitterQuoteAction, execution_context=None):
        notready = self._check_ready()
        if notready:
            return notready
        block = await self._precheck_write("twitter_quote", params, execution_context)
        if block:
            return block
        try:
            parts = self._split_tweet_text(params.text)
            if len(parts) == 1:
                tweet = await self._compose_tweet(text=parts[0], quote_tweet_id=params.tweet_id)
                return self._ok(f"🐦 Quoted tweet {tweet.get('id')}")
            # head tweet quotes; the remainder chains as a reply thread
            ids = await self._compose_thread(parts, quote_tweet_id=params.tweet_id)
            return self._ok(
                f"🐦 Quote exceeded 280 chars — posted as a {len(ids)}-tweet thread: {', '.join(ids)}"
            )
        except Exception as e:
            return self._err(f"Error quoting: {e}")

    @BaseTool.action("Post a thread: a list of tweets chained as replies.", param_model=TwitterThreadAction)
    async def twitter_thread(self, params: TwitterThreadAction, execution_context=None):
        notready = self._check_ready()
        if notready:
            return notready
        block = await self._precheck_write("twitter_thread", params, execution_context)
        if block:
            return block
        try:
            prev_id = None
            ids: List[str] = []
            for i, text in enumerate(params.texts):
                tweet = await self._compose_tweet(
                    text=text,
                    media_paths=params.media_paths if i == 0 else None,
                    in_reply_to_tweet_id=prev_id,
                )
                prev_id = tweet.get("id")
                ids.append(str(prev_id))
            return self._ok(f"🐦 Posted thread of {len(ids)} tweets: {', '.join(ids)}")
        except Exception as e:
            return self._err(f"Error posting thread: {e}")

    @BaseTool.action("Delete one of your tweets.", param_model=TwitterDeleteAction)
    async def twitter_delete_tweet(self, params: TwitterDeleteAction, execution_context=None):
        notready = self._check_ready()
        if notready:
            return notready
        block = await self._precheck_write("twitter_delete_tweet", params, execution_context)
        if block:
            return block
        try:
            await self._make_request(func=self.client.delete_tweet, endpoint_type="tweets", id=params.tweet_id)
            return self._ok(f"🐦 Deleted tweet {params.tweet_id}")
        except Exception as e:
            return self._err(f"Error deleting tweet: {e}")

    # --- engagement actions ---------------------------------------------

    async def _engage(self, action_name: str, func_name: str, id_kwarg: str,
                      tweet_id: str, params, execution_context):
        notready = self._check_ready()
        if notready:
            return notready
        block = await self._precheck_write(action_name, params, execution_context)
        if block:
            return block
        try:
            # id_kwarg differs per endpoint: tweepy uses ``tweet_id`` for like/retweet
            # but ``source_tweet_id`` for unretweet (verified vs tweepy 4.14).
            await self._make_request(func=getattr(self.client, func_name),
                                     endpoint_type="tweets", **{id_kwarg: tweet_id})
            return self._ok(f"🐦 {action_name} ok for {tweet_id}")
        except Exception as e:
            return self._err(f"Error in {action_name}: {e}")

    @BaseTool.action("Like a tweet.", param_model=TwitterTweetIdAction)
    async def twitter_like(self, params: TwitterTweetIdAction, execution_context=None):
        return await self._engage("twitter_like", "like", "tweet_id", params.tweet_id, params, execution_context)

    @BaseTool.action("Unlike a tweet.", param_model=TwitterTweetIdAction)
    async def twitter_unlike(self, params: TwitterTweetIdAction, execution_context=None):
        return await self._engage("twitter_unlike", "unlike", "tweet_id", params.tweet_id, params, execution_context)

    @BaseTool.action("Retweet a tweet.", param_model=TwitterTweetIdAction)
    async def twitter_retweet(self, params: TwitterTweetIdAction, execution_context=None):
        return await self._engage("twitter_retweet", "retweet", "tweet_id", params.tweet_id, params, execution_context)

    @BaseTool.action("Remove a retweet.", param_model=TwitterTweetIdAction)
    async def twitter_unretweet(self, params: TwitterTweetIdAction, execution_context=None):
        return await self._engage("twitter_unretweet", "unretweet", "source_tweet_id", params.tweet_id, params, execution_context)

    # --- relationship actions -------------------------------------------

    async def _relate(self, action_name: str, func_name: str, user: str, params, execution_context):
        notready = self._check_ready()
        if notready:
            return notready
        block = await self._precheck_write(action_name, params, execution_context)
        if block:
            return block
        try:
            uid = await self._resolve_user_id(user)
            if not uid:
                return self._err(f"Could not resolve user '{user}'")
            await self._make_request(func=getattr(self.client, func_name),
                                     endpoint_type="users", target_user_id=uid)
            return self._ok(f"🐦 {action_name} ok for {user}")
        except Exception as e:
            return self._err(f"Error in {action_name}: {e}")

    @BaseTool.action("Follow a user.", param_model=TwitterUserAction)
    async def twitter_follow(self, params: TwitterUserAction, execution_context=None):
        return await self._relate("twitter_follow", "follow_user", params.user, params, execution_context)

    @BaseTool.action("Unfollow a user.", param_model=TwitterUserAction)
    async def twitter_unfollow(self, params: TwitterUserAction, execution_context=None):
        return await self._relate("twitter_unfollow", "unfollow_user", params.user, params, execution_context)

    @BaseTool.action("Mute a user.", param_model=TwitterUserAction)
    async def twitter_mute(self, params: TwitterUserAction, execution_context=None):
        return await self._relate("twitter_mute", "mute", params.user, params, execution_context)

    @BaseTool.action("Unmute a previously muted user.", param_model=TwitterUserAction)
    async def twitter_unmute(self, params: TwitterUserAction, execution_context=None):
        return await self._relate("twitter_unmute", "unmute", params.user, params, execution_context)

    @BaseTool.action("Block a user. NOTE: X API v2 blocking is Enterprise-only — "
                     "on the pay-per-use/basic tiers this returns 403 (mute instead).",
                     param_model=TwitterUserAction)
    async def twitter_block(self, params: TwitterUserAction, execution_context=None):
        return await self._relate("twitter_block", "block", params.user, params, execution_context)

    # --- DM action -------------------------------------------------------

    @BaseTool.action("Send a direct message to a user.", param_model=TwitterDMAction)
    async def twitter_dm(self, params: TwitterDMAction, execution_context=None):
        notready = self._check_ready()
        if notready:
            return notready
        block = await self._precheck_write("twitter_dm", params, execution_context, is_dm=True)
        if block:
            return block
        try:
            uid = await self._resolve_user_id(params.recipient)
            if not uid:
                return self._err(f"Could not resolve recipient '{params.recipient}'")
            await self._make_request(func=self.client.create_direct_message, endpoint_type="dm",
                                     participant_id=uid, text=params.text)
            return self._ok(f"🐦 DM sent to {params.recipient}")
        except Exception as e:
            return self._err(f"Error sending DM: {e}")

    # --- act-on-context reads (always available) ------------------------

    @BaseTool.action("Get recent mentions of your own account.", param_model=TwitterMentionsAction)
    async def twitter_get_mentions(self, params: TwitterMentionsAction, execution_context=None):
        if not self._enabled:
            return self._err("Twitter service not available - missing credentials")
        try:
            me_id = await self.get_authenticated_user_id()
            if not me_id:
                return self._err("Could not resolve authenticated user id")
            resp = await self._make_request(func=self.client.get_users_mentions, endpoint_type="tweets",
                                            id=me_id, max_results=params.max_results,
                                            tweet_fields=["created_at", "text", "author_id"])
            items = []
            if resp and getattr(resp, "data", None):
                items = [{"id": str(t.id), "text": t.text} for t in resp.data]
            return self._ok(f"🐦 Mentions:\n{json.dumps(items, indent=2)}")
        except Exception as e:
            return self._err(f"Error getting mentions: {e}")

    @BaseTool.action("Read your recent X direct messages (newest first; optionally "
                     "only the 1:1 conversation with one participant). DM reads are "
                     "rate-limited to 15/15min by X — don't poll.",
                     param_model=TwitterGetDMsAction)
    async def twitter_get_dms(self, params: TwitterGetDMsAction, execution_context=None):
        if not self._enabled:
            return self._err("Twitter service not available - missing credentials")
        try:
            kwargs: Dict[str, Any] = dict(
                dm_event_fields=["id", "event_type", "text", "sender_id",
                                 "dm_conversation_id", "created_at"],
                event_types="MessageCreate",
                max_results=params.max_results,
            )
            if params.participant:
                uid = await self._resolve_user_id(params.participant)
                if not uid:
                    return self._err(
                        f"Could not resolve participant '{params.participant}'")
                kwargs["participant_id"] = uid
            resp = await self._make_request(
                func=self.client.get_direct_message_events, endpoint_type="dm",
                **kwargs)
            items = []
            for ev in (getattr(resp, "data", None) or []):
                data = dict(getattr(ev, "data", None) or {})
                items.append({
                    "id": str(data.get("id") or getattr(ev, "id", "") or ""),
                    "text": data.get("text"),
                    "sender_id": str(data.get("sender_id") or ""),
                    "dm_conversation_id": data.get("dm_conversation_id"),
                    "created_at": data.get("created_at"),
                })
            return self._ok(f"🐦 DM events ({len(items)}):\n{json.dumps(items, indent=2)}")
        except Exception as e:
            return self._err(f"Error reading DMs: {e}")

    @BaseTool.action("Get a user's recent tweets (their timeline).",
                     param_model=TwitterTimelineAction)
    async def twitter_get_timeline(self, params: TwitterTimelineAction, execution_context=None):
        if not self._enabled:
            return self._err("Twitter service not available - missing credentials")
        try:
            tweets = await self.get_user_timeline(params.user,
                                                  max_results=params.max_results)
            if tweets is None:
                return self._err(f"Could not fetch timeline for '{params.user}'")
            return self._ok(f"🐦 Timeline for {params.user} ({len(tweets)}):\n"
                            f"{json.dumps(tweets, indent=2)}")
        except Exception as e:
            return self._err(f"Error getting timeline: {e}")

    @BaseTool.action("Show your own authenticated Twitter profile.", param_model=TwitterWhoamiAction)
    async def twitter_whoami(self, params: TwitterWhoamiAction, execution_context=None):
        if not self._enabled:
            return self._err("Twitter service not available - missing credentials")
        try:
            profile = await self.get_user_profile()
            return self._ok(f"🐦 You are:\n{json.dumps(profile, indent=2)}")
        except Exception as e:
            return self._err(f"Error getting profile: {e}")

    def get_actions(self) -> Dict[str, Any]:
        """Expose actions; hide the WRITE surface unless TWITTER_ENABLED=true.

        Reads (search/get_user/get_tweets/mentions/whoami) are always available.
        Default-OFF: with the flag unset the write actions are absent — byte-identical
        to the pre-G1 surface.
        """
        actions = super().get_actions()
        if not twitter_write_enabled():
            for name in _TWITTER_WRITE_ACTIONS:
                actions.pop(name, None)
        return actions

    async def close(self) -> None:
        """Cleanup method."""
        self._initialized = False
        self.client = None
        self.api_v1 = None

    async def _ensure_initialized(self) -> None:
        """Ensure service is initialized."""
        if not self._initialized:
            await self._initialize()

    @BaseTool.action("Search for tweets on Twitter", param_model=TwitterSearchActionModel)
    async def twitter_search(self, params: TwitterSearchActionModel):
        """Search for tweets on Twitter.
        
        Args:
            params: Twitter search parameters
            
        Returns:
            ActionResult containing search results
        """
        try:
            if not self._enabled:
                return self.create_action_result(
                    error="Twitter service not available - missing credentials",
                    include_in_memory=True
                )
            
            search_results = await self.search(
                query=params.query,
                max_results=params.max_results,
                include_replies=params.include_replies
            )
            
            msg = f'🐦 Twitter search results for "{params.query}":\n{json.dumps(search_results, indent=2)}'
            self.logger.info(f'Twitter search performed for: {params.query}')
            return self.create_action_result(
                extracted_content=msg,
                include_in_memory=True
            )
        except Exception as e:
            error_msg = f"Error performing Twitter search: {str(e)}"
            self.logger.error(error_msg)
            return self.create_action_result(
                error=error_msg, 
                include_in_memory=True
            )
    
    @BaseTool.action("Get Twitter user information", param_model=TwitterGetUserActionModel)
    async def twitter_get_user(self, params: TwitterGetUserActionModel):
        """Get Twitter user information.
        
        Args:
            params: Twitter user parameters
            
        Returns:
            ActionResult containing user information
        """
        try:
            if not self._enabled:
                return self.create_action_result(
                    error="Twitter service not available - missing credentials",
                    include_in_memory=True
                )
            
            user_info = await self.get_user_by_identifier(params.username)
            if not user_info:
                return self.create_action_result(
                    error=f"Twitter user {params.username} not found", 
                    include_in_memory=True
                )
            
            msg = f'🐦 Twitter user information for "{params.username}":\n{json.dumps(user_info, indent=2)}'
            self.logger.info(f'Twitter user info retrieved for: {params.username}')
            return self.create_action_result(
                extracted_content=msg, 
                include_in_memory=True
            )
        except Exception as e:
            error_msg = f"Error getting Twitter user: {str(e)}"
            self.logger.error(error_msg)
            return self.create_action_result(
                error=error_msg, 
                include_in_memory=True
            )
    
    @BaseTool.action("Get tweets from a Twitter user", param_model=TwitterGetTweetsActionModel)
    async def twitter_get_tweets(self, params: TwitterGetTweetsActionModel):
        """Get tweets from a Twitter user.
        
        Args:
            params: Twitter tweets parameters
            
        Returns:
            ActionResult containing tweets
        """
        try:
            if not self._enabled:
                return self.create_action_result(
                    error="Twitter service not available - missing credentials",
                    include_in_memory=True
                )
            
            tweets = await self.get_user_timeline(
                username_or_id=params.user_id,
                max_results=params.max_results
            )
            
            if not tweets:
                return self.create_action_result(
                    error=f"No tweets found for user ID {params.user_id}", 
                    include_in_memory=True
                )
            
            msg = f'🐦 Tweets from user ID {params.user_id}:\n{json.dumps(tweets, indent=2)}'
            self.logger.info(f'Twitter tweets retrieved for user ID: {params.user_id}')
            return self.create_action_result(
                extracted_content=msg, 
                include_in_memory=True
            )
        except Exception as e:
            error_msg = f"Error getting tweets: {str(e)}"
            self.logger.error(error_msg)
            return self.create_action_result(
                error=error_msg, 
                include_in_memory=True
            )
            
    def create_action_result(self, extracted_content=None, error=None, include_in_memory=False, is_done=False):
        """Create a properly structured action result.
        
        This helper method ensures consistent action results.
        """
        try:
            # Import ActionResult from canonical location
            from tools.controller.types import ActionResult
            return ActionResult(
                extracted_content=extracted_content,
                error=error,
                include_in_memory=include_in_memory,
                is_done=is_done
            )
        except ImportError:
            # Fallback to a dictionary if ActionResult can't be imported
            return {
                "extracted_content": extracted_content,
                "error": error,
                "include_in_memory": include_in_memory,
                "is_done": is_done
            }