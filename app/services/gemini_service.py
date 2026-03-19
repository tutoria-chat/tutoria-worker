"""
Gemini Service - OpenAI-compatible API via AsyncOpenAI

Provides access to Google Gemini models (gemini-2.0-flash, gemini-2.0-flash-lite, etc.)
through the OpenAI-compatible chat completions endpoint exposed by
Google's generative language API.

Uses the same interface as AnthropicService/BedrockService/DeepSeekService
for drop-in provider swapping in the widget chat pipeline.
"""
import asyncio
import logging
import random
from typing import AsyncGenerator, Dict, Any, List, Optional

from openai import AsyncOpenAI
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Module

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"


class GeminiService:
    """
    AI Service using Google Gemini's OpenAI-compatible API.

    - Uses openai.AsyncOpenAI with custom base_url pointed at Gemini
    - Supports conversation history and streaming
    - Implements retry with exponential backoff + jitter on transient errors
    - Supports DB-managed API keys via KeyManager
    """

    def __init__(
        self,
        module: Module,
        model_name: Optional[str] = None,
        db: Optional[Session] = None,
        api_key: Optional[str] = None,
    ):
        self.module = module
        self.db = db

        # Determine model name: explicit arg > module AI model (if same provider) > default
        if model_name:
            self.model_name = model_name
        elif module.ai_model and module.ai_model.model_name and getattr(module.ai_model, 'provider', '').lower() == 'gemini':
            self.model_name = module.ai_model.model_name
        else:
            self.model_name = DEFAULT_GEMINI_MODEL

        # Resolve API key: explicit arg > DB-managed key > env var
        self._key_manager = None
        self._key_id: Optional[int] = None
        resolved_key = api_key

        if not resolved_key and settings.USE_DB_PROVIDER_KEYS and db:
            try:
                from app.services.key_manager import KeyManager

                self._key_manager = KeyManager(db)
                result = self._key_manager.get_next_key("gemini")
                if result:
                    self._key_id, resolved_key = result
                    logger.info(f"Using DB-managed Gemini key (id={self._key_id})")
            except Exception as e:
                logger.warning(f"Failed to load DB-managed Gemini key: {e}")

        if not resolved_key:
            resolved_key = settings.GEMINI_API_KEY

        if not resolved_key:
            logger.error("No Gemini API key available (env, DB, or explicit)")
            raise ValueError("GEMINI_API_KEY is required for GeminiService")

        # Initialize AsyncOpenAI with Gemini base URL
        self.client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=settings.GEMINI_BASE_URL,
        )

        logger.info(
            f"Initialized GeminiService (model={self.model_name}, "
            f"base_url={settings.GEMINI_BASE_URL})"
        )

    # ------------------------------------------------------------------
    # Retry helper
    # ------------------------------------------------------------------

    async def _call_with_retry(self, coro_factory, description: str = "Gemini API call"):
        """
        Execute an async callable with exponential backoff + jitter.

        Retries on HTTP 429, 500, 502, 503 errors.

        Args:
            coro_factory: A zero-arg callable that returns a new awaitable each time.
            description: Human-readable label for log messages.

        Returns:
            The result of the awaitable.
        """
        max_retries = settings.MAX_RETRIES
        base_delay = settings.RETRY_BASE_DELAY
        max_delay = settings.RETRY_MAX_DELAY

        last_exception = None

        for attempt in range(1, max_retries + 1):
            try:
                result = await coro_factory()
                # Mark key as successful if using DB-managed keys
                if self._key_manager and self._key_id:
                    try:
                        self._key_manager.mark_key_success("gemini", self._key_id)
                    except Exception:
                        pass
                return result

            except Exception as e:
                last_exception = e
                error_str = str(e)
                error_type = type(e).__name__

                # Check if this is a retryable error
                retryable = False
                if "429" in error_str or "rate_limit" in error_str.lower():
                    retryable = True
                elif any(code in error_str for code in ["500", "502", "503"]):
                    retryable = True
                elif "timeout" in error_str.lower() or "connection" in error_str.lower():
                    retryable = True

                if not retryable or attempt == max_retries:
                    logger.error(
                        f"{description} failed after {attempt} attempt(s): "
                        f"{error_type}: {error_str[:200]}"
                    )
                    # Mark key as failed if using DB-managed keys
                    if self._key_manager and self._key_id:
                        try:
                            self._key_manager.mark_key_failed("gemini", self._key_id)
                        except Exception:
                            pass
                    raise

                # Exponential backoff with jitter
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                jitter = random.uniform(0, delay * 0.5)
                wait_time = delay + jitter

                logger.warning(
                    f"{description} attempt {attempt}/{max_retries} failed "
                    f"({error_type}: {error_str[:100]}), retrying in {wait_time:.1f}s"
                )
                await asyncio.sleep(wait_time)

        raise last_exception

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    async def answer_question_with_history(
        self, messages: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """
        Answer a question with conversation history.

        Args:
            messages: List of {"role": "system|user|assistant", "content": "..."}

        Returns:
            dict: {
                "response": str,
                "tokens": {
                    "prompt_tokens": int,
                    "completion_tokens": int,
                    "total_tokens": int
                }
            }
        """
        try:
            logger.info(
                f"Gemini answer_question_with_history called "
                f"(model={self.model_name}, messages={len(messages)})"
            )

            async def _make_request():
                return await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    max_tokens=2048,
                    temperature=0.7,
                )

            response = await self._call_with_retry(
                _make_request, description="Gemini chat completion"
            )

            # Extract response text
            response_text = response.choices[0].message.content or ""

            # Extract token usage
            prompt_tokens = 0
            completion_tokens = 0
            total_tokens = 0
            if response.usage:
                prompt_tokens = response.usage.prompt_tokens or 0
                completion_tokens = response.usage.completion_tokens or 0
                total_tokens = response.usage.total_tokens or 0

            logger.info(
                f"Gemini response generated "
                f"(tokens: prompt={prompt_tokens}, completion={completion_tokens}, total={total_tokens})"
            )

            return {
                "response": response_text,
                "tokens": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }

        except Exception as e:
            logger.error(f"Gemini answer_question_with_history error: {e}")
            return {
                "response": f"Error communicating with Gemini: {str(e)}",
                "tokens": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def answer_question_with_history_stream(
        self, messages: List[Dict[str, str]]
    ) -> AsyncGenerator[str, None]:
        """
        Answer a question with streaming response.

        Args:
            messages: List of conversation messages.

        Yields:
            str: Response text chunks.
        """
        try:
            logger.info(
                f"Gemini streaming called "
                f"(model={self.model_name}, messages={len(messages)})"
            )

            stream = await self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                max_tokens=2048,
                temperature=0.7,
                stream=True,
            )

            async for chunk in stream:
                if (
                    chunk.choices
                    and chunk.choices[0].delta
                    and chunk.choices[0].delta.content
                ):
                    yield chunk.choices[0].delta.content

            # Mark success after full stream
            if self._key_manager and self._key_id:
                try:
                    self._key_manager.mark_key_success("gemini", self._key_id)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Gemini streaming error: {e}")
            # Mark failure
            if self._key_manager and self._key_id:
                try:
                    self._key_manager.mark_key_failed("gemini", self._key_id)
                except Exception:
                    pass
            yield f"Error: {str(e)}"
