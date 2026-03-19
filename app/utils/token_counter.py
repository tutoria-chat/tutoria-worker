"""
Token counting utilities for OpenAI and Anthropic APIs.

This module provides accurate token counting to track costs and optimize prompts.
"""
import tiktoken
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)


class TokenCounter:
    """Token counter for OpenAI and Anthropic API requests."""

    def __init__(self):
        """Initialize token counter with OpenAI encoder."""
        try:
            self.openai_encoder = tiktoken.encoding_for_model("gpt-4o")
        except Exception as e:
            logger.warning(f"Failed to load tiktoken encoder: {e}")
            self.openai_encoder = None

    def count_openai_tokens(self, messages: List[Dict[str, str]]) -> dict:
        """
        Count tokens for OpenAI API request.

        OpenAI token counting includes:
        - 4 tokens per message overhead
        - Content tokens (actual message text)
        - 2 tokens for assistant reply priming

        Args:
            messages: List of message dicts with 'role' and 'content' keys

        Returns:
            dict: {
                "prompt_tokens": int,
                "estimated": bool (False = accurate count, True = estimation)
            }
        """
        if not self.openai_encoder:
            logger.warning("OpenAI encoder not available, using character-based estimation")
            return self._estimate_tokens_by_chars(messages)

        try:
            total_tokens = 0

            for message in messages:
                # 4 tokens per message overhead
                total_tokens += 4

                # Count tokens in content
                content = message.get("content", "")
                if content:
                    total_tokens += len(self.openai_encoder.encode(content))

                # Count tokens in role (typically 1 token)
                role = message.get("role", "")
                if role:
                    total_tokens += len(self.openai_encoder.encode(role))

            # 2 tokens for assistant reply priming
            total_tokens += 2

            return {
                "prompt_tokens": total_tokens,
                "estimated": False
            }

        except Exception as e:
            logger.error(f"Token counting error: {e}")
            return self._estimate_tokens_by_chars(messages)

    def count_anthropic_tokens(self, messages: List[Dict[str, str]], system_prompt: Optional[str] = None) -> dict:
        """
        Estimate tokens for Anthropic API request.

        Anthropic uses similar tokenization to GPT models.
        Rough estimation: 1 token ≈ 4 characters (English text)

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            system_prompt: Optional system prompt to include in count

        Returns:
            dict: {
                "prompt_tokens": int,
                "estimated": bool (always True for Anthropic)
            }
        """
        try:
            total_chars = 0

            # Count system prompt
            if system_prompt:
                total_chars += len(system_prompt)

            # Count messages
            for message in messages:
                content = message.get("content", "")
                total_chars += len(content)

            # Rough estimation: 1 token = 4 characters
            estimated_tokens = total_chars // 4

            # Add overhead (Anthropic has similar message overhead to OpenAI)
            estimated_tokens += len(messages) * 4

            return {
                "prompt_tokens": estimated_tokens,
                "estimated": True
            }

        except Exception as e:
            logger.error(f"Token counting error: {e}")
            return {
                "prompt_tokens": 0,
                "estimated": True
            }

    def _estimate_tokens_by_chars(self, messages: List[Dict[str, str]]) -> dict:
        """
        Fallback estimation when tiktoken is unavailable.

        Uses character count divided by 4 as rough approximation.

        Args:
            messages: List of message dicts

        Returns:
            dict: Token count estimation
        """
        try:
            total_chars = sum(len(msg.get("content", "")) for msg in messages)
            estimated_tokens = total_chars // 4

            return {
                "prompt_tokens": estimated_tokens,
                "estimated": True
            }
        except Exception as e:
            logger.error(f"Estimation error: {e}")
            return {
                "prompt_tokens": 0,
                "estimated": True
            }

    def count_text_tokens(self, text: str) -> int:
        """
        Count tokens in a single text string.

        Useful for counting system prompts, individual messages, etc.

        Args:
            text: Text to count tokens in

        Returns:
            int: Token count
        """
        if not self.openai_encoder:
            # Fallback to character-based estimation
            return len(text) // 4

        try:
            return len(self.openai_encoder.encode(text))
        except Exception as e:
            logger.error(f"Token counting error: {e}")
            return len(text) // 4


# Singleton instance for reuse
_token_counter = None


def get_token_counter() -> TokenCounter:
    """
    Get singleton TokenCounter instance.

    Returns:
        TokenCounter: Shared token counter instance
    """
    global _token_counter
    if _token_counter is None:
        _token_counter = TokenCounter()
    return _token_counter
