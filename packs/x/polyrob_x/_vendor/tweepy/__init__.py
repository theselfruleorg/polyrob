# Tweepy
# Copyright 2009-2023 Joshua Roesslein
# See LICENSE for details.

"""
Tweepy Twitter API library
"""
__version__ = '4.17.0'
__author__ = 'Joshua Roesslein'
__license__ = 'MIT'

from polyrob_x._vendor.tweepy.api import API
from polyrob_x._vendor.tweepy.auth import (
    AppAuthHandler, OAuthHandler, OAuth1UserHandler, OAuth2AppHandler,
    OAuth2BearerHandler, OAuth2UserHandler
)
from polyrob_x._vendor.tweepy.cache import Cache, FileCache, MemoryCache
from polyrob_x._vendor.tweepy.client import Client, Response
from polyrob_x._vendor.tweepy.cursor import Cursor
from polyrob_x._vendor.tweepy.direct_message_event import (
    DirectMessageEvent, DIRECT_MESSAGE_EVENT_FIELDS, DM_EVENT_FIELDS
)
from polyrob_x._vendor.tweepy.errors import (
    BadRequest, Forbidden, HTTPException, NotFound, TooManyRequests,
    TweepyException, TwitterServerError, Unauthorized
)
from polyrob_x._vendor.tweepy.list import List, LIST_FIELDS
from polyrob_x._vendor.tweepy.media import Media, MEDIA_FIELDS
from polyrob_x._vendor.tweepy.pagination import Paginator
from polyrob_x._vendor.tweepy.place import Place, PLACE_FIELDS
from polyrob_x._vendor.tweepy.poll import Poll, POLL_FIELDS
from polyrob_x._vendor.tweepy.space import PUBLIC_SPACE_FIELDS, Space, SPACE_FIELDS
from polyrob_x._vendor.tweepy.streaming import (
    StreamingClient, StreamResponse, StreamRule
)
from polyrob_x._vendor.tweepy.tweet import (
    PUBLIC_TWEET_FIELDS, ReferencedTweet, Tweet, TWEET_FIELDS
)
from polyrob_x._vendor.tweepy.user import User, USER_FIELDS

# Global, unauthenticated instance of API
api = API()
