"""Compaction manager for message history and memory optimization.

Implements Anthropic-style context engineering with message-level compaction
to prevent context overflow in long-running task sessions.

Detection only: threshold checks + context-aware scaling.
The compact/archive/summarize action methods were removed (no external callers).

Reference:
    Anthropic Context Engineering: https://www.anthropic.com/engineering
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class CompactionManager:
    """Manages compaction thresholds and detection for message history.

    The CompactionManager provides threshold detection to decide when
    compaction is needed, with context-aware scaling per model.

    Attributes:
        soft_threshold: Soft limit percentage (trigger warning, default 0.35)
        hard_threshold: Hard limit percentage (trigger compaction, default 0.45)

    FIX #8: Updated docstring to match actual default values.
    OPTIMIZATION (Nov 14, 2025): Aggressive thresholds for early compression.
    """

    def __init__(
        self,
        soft_threshold: float = 0.35,  # OPTIMIZATION: Was 0.55, now 35% (early compression!)
        hard_threshold: float = 0.45,  # OPTIMIZATION: Was 0.65, now 45% (prevent overflow!)
        context_window: Optional[int] = None  # FIX #6: Context-aware scaling
    ):
        """Initialize compaction manager with context-aware compression.

        FIX #6 (Nov 26, 2025): Scale thresholds based on context window size.
        Large context models (1M+) can safely use higher thresholds.

        Philosophy: "Compress smart based on available context"
        - Small models (<100K): Aggressive thresholds (35%/45%)
        - Medium models (100K-200K): Moderate thresholds (45%/60%)
        - Large models (200K-1M): Relaxed thresholds (55%/70%)
        - Huge models (1M+): Very relaxed thresholds (65%/80%)

        Args:
            soft_threshold: Percentage to trigger warning (0.0-1.0, default 0.35)
            hard_threshold: Percentage to trigger compaction (0.0-1.0, default 0.45)
            context_window: Model context window size for adaptive scaling
        """
        # FIX #6: Scale thresholds based on context window
        if context_window is not None:
            soft_threshold, hard_threshold = self._scale_thresholds(
                context_window, soft_threshold, hard_threshold
            )

        self.soft_threshold = soft_threshold
        self.hard_threshold = hard_threshold
        self.context_window = context_window

        logger.info(
            f"CompactionManager initialized: "
            f"soft={soft_threshold:.0%}, hard={hard_threshold:.0%}"
            f"{f', context={context_window:,}' if context_window else ''}"
        )

    @staticmethod
    def _scale_thresholds(
        context_window: int,
        default_soft: float,
        default_hard: float,
    ) -> tuple:
        """Scale thresholds based on model context window size.

        Args:
            context_window: Model context window in tokens
            default_soft: Default soft threshold
            default_hard: Default hard threshold

        Returns:
            Tuple of (soft_threshold, hard_threshold)
        """
        if context_window >= 1_000_000:
            # 1M+ context (e.g., Gemini 1.5 Pro, Claude with extended context)
            # Can hold 6000+ messages, very relaxed thresholds
            return 0.65, 0.80
        elif context_window >= 200_000:
            # 200K-1M context (e.g., Claude 3.5, GPT-4 Turbo)
            return 0.55, 0.70
        elif context_window >= 100_000:
            # 100K-200K context
            return 0.45, 0.60
        else:
            # <100K context - keep aggressive defaults
            return default_soft, default_hard

    @classmethod
    def for_model(cls, model_name: str, **kwargs) -> 'CompactionManager':
        """Create a CompactionManager with thresholds scaled for a specific model.

        FIX #6: Factory method for context-aware compaction.

        Args:
            model_name: Name of the model (e.g., 'gpt-5', 'claude-sonnet-4-5')
            **kwargs: Additional arguments passed to __init__

        Returns:
            CompactionManager with appropriate thresholds
        """
        try:
            from modules.llm.model_registry import get_model_config
            config = get_model_config(model_name)
            if config and config.context_window:
                return cls(context_window=config.context_window, **kwargs)
        except Exception as e:
            logger.warning(f"Could not get model config for {model_name}: {e}")

        # Fallback to defaults
        return cls(**kwargs)

    def should_compact_messages(
        self,
        current_tokens: int,
        max_tokens: int
    ) -> Tuple[bool, float]:
        """Check if message compaction is needed.

        Args:
            current_tokens: Current token count
            max_tokens: Maximum allowed tokens

        Returns:
            Tuple of (should_compact, usage_ratio)
        """
        if max_tokens <= 0:
            return False, 0.0

        ratio = current_tokens / max_tokens

        if ratio >= self.hard_threshold:
            logger.warning(
                f"Hard threshold reached: {current_tokens}/{max_tokens} "
                f"({ratio:.1%}) >= {self.hard_threshold:.1%}"
            )
            return True, ratio

        if ratio >= self.soft_threshold:
            logger.info(
                f"Soft threshold reached: {current_tokens}/{max_tokens} "
                f"({ratio:.1%}) >= {self.soft_threshold:.1%}"
            )

        return False, ratio
