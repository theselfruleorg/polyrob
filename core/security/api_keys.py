"""API-key scope and UTC expiry rules, shared by issuers and authenticators."""
import json
from datetime import datetime, timezone

DEFAULT_SCOPES = ('read', 'write')
DEFAULT_EXPIRY_DAYS = 90
MAX_EXPIRY_DAYS = 365


def scopes(value):
    if isinstance(value, str):
        value = json.loads(value)
    if (not isinstance(value, (list, tuple)) or not value or len(value) > 2
            or any(not isinstance(v, str) or v not in DEFAULT_SCOPES for v in value)
            or len(set(value)) != len(value)):
        raise ValueError('API key scopes must contain read, write, or both')
    return list(value)


def expiry_timestamp(value):
    if not value:
        raise ValueError('API key requires an expiry; replace legacy keys')
    expiry = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry.timestamp()


def required_scope(method):
    return 'read' if method.upper() in ('GET', 'HEAD', 'OPTIONS') else 'write'
