import json
import os
from typing import List, Optional, Union
import logging

logger = logging.getLogger(__name__)

def get_channels_file_path() -> str:
    """Get the path to the subscription channels JSON file."""
    # Get the directory of the current file
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # Go up one level to the modules directory, then to data
    data_dir = os.path.join(os.path.dirname(os.path.dirname(current_dir)), 'data')
    # Create data directory if it doesn't exist
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, 'subscription_channels.json')

def get_channels_from_file() -> List[str]:
    """Get list of subscription channels from JSON file."""
    try:
        file_path = get_channels_file_path()
        if not os.path.exists(file_path):
            # Create empty channels file if it doesn't exist
            save_channels_to_file([])
            return []
            
        with open(file_path, 'r') as f:
            data = json.load(f)
            return data.get('channels', [])
    except Exception as e:
        logger.error(f"Error reading channels from file: {e}")
        return []

def save_channels_to_file(channels: List[str]) -> bool:
    """Save list of subscription channels to JSON file.
    
    Args:
        channels: List of channel usernames to save
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        file_path = get_channels_file_path()
        with open(file_path, 'w') as f:
            json.dump({'channels': channels}, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving channels to file: {e}")
        return False

def validate_channel_username(username: str) -> bool:
    """Validate channel username format.
    
    Args:
        username: Channel username to validate
        
    Returns:
        bool: True if valid, False otherwise
    """
    # Remove @ if present
    username = username.lstrip('@')
    # Basic validation - should be at least 5 chars and contain only allowed chars
    return len(username) >= 5 and username.replace('_', '').isalnum()

def deserialize_preferences(preferences_data: Optional[Union[str, dict]]) -> dict:
    """Deserialize preferences data from JSON string or dict."""
    if not preferences_data:
        return {}
    
    if isinstance(preferences_data, dict):
        return preferences_data
        
    try:
        if isinstance(preferences_data, str):
            return json.loads(preferences_data)
    except json.JSONDecodeError:
        logging.error(f"Failed to deserialize preferences: {preferences_data}")
        return {}
        
    return {}

def serialize_preferences(preferences: Optional[Union[str, dict]]) -> str:
    """Serialize preferences to JSON string."""
    if not preferences:
        return '{}'
        
    if isinstance(preferences, str):
        try:
            # Validate it's a valid JSON string
            json.loads(preferences)
            return preferences
        except json.JSONDecodeError:
            return '{}'
            
    if isinstance(preferences, dict):
        try:
            return json.dumps(preferences)
        except Exception:
            return '{}'
            
    return '{}'

def init_auto_tables():
    """Initialize auto-generated database tables."""
    # Your table initialization logic here
    pass