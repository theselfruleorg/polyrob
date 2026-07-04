"""Metrics collection and reporting for the bot."""

import logging
from typing import Optional, Dict, Any
from core.base_component import BaseComponent

class Metrics(BaseComponent):
    """Metrics collection and reporting."""
    
    def __init__(self, *args, **kwargs):
        """Initialize metrics component."""
        super().__init__(*args, **kwargs)
        self.logger = logging.getLogger(__name__)
        self._metrics = {}
        
    async def _initialize(self) -> None:
        """Initialize metrics component."""
        self.logger.info("Metrics initialized")
        
    async def _cleanup(self) -> None:
        """Clean up resources."""
        self.logger.info("Metrics cleaned up")
        
    def record(self, metric_name: str, value: Any, tags: Optional[Dict[str, str]] = None) -> None:
        """Record a metric value."""
        if metric_name not in self._metrics:
            self._metrics[metric_name] = []
        
        self._metrics[metric_name].append({
            'value': value,
            'tags': tags or {}
        })
        
    def get_metrics(self) -> Dict[str, Any]:
        """Get all recorded metrics."""
        return self._metrics 