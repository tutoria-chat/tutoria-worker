"""
Smart Model Routing - Route questions to appropriate models based on complexity

Routes simple questions to cheap models (GPT-4o-mini, Claude Haiku)
Routes complex questions to expensive models (GPT-4o, Claude Sonnet)

Criteria for complexity:
- Token count
- Question length
- Keywords (analysis, compare, evaluate, etc.)
"""
import logging
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


class ModelRouter:
    """Routes questions to appropriate AI models based on complexity."""

    # Keywords that indicate complex questions
    COMPLEX_KEYWORDS = {
        "pt-br": ["analise", "compare", "avalie", "explique detalhadamente", "qual a diferença", "por que", "como funciona", "demonstre"],
        "en": ["analyze", "compare", "evaluate", "explain in detail", "what's the difference", "why", "how does", "demonstrate"],
        "es": ["analiza", "compara", "evalúa", "explica detalladamente", "cuál es la diferencia", "por qué", "cómo funciona", "demuestra"]
    }

    # Simple question patterns
    SIMPLE_KEYWORDS = {
        "pt-br": ["o que é", "defina", "quando", "quem", "onde", "sim ou não", "verdadeiro ou falso"],
        "en": ["what is", "define", "when", "who", "where", "yes or no", "true or false"],
        "es": ["qué es", "define", "cuándo", "quién", "dónde", "sí o no", "verdadero o falso"]
    }

    def __init__(self, token_threshold: int = 50):
        """
        Initialize model router.

        Args:
            token_threshold: Questions with < this many tokens use cheap model
        """
        self.token_threshold = token_threshold

    def route_question(
        self,
        question: str,
        language: str = "pt-br",
        token_count: int = 0
    ) -> Tuple[str, str]:
        """
        Determine which model to use for a question.

        Args:
            question: User's question
            language: Language code
            token_count: Estimated token count (if available)

        Returns:
            Tuple[str, str]: (model_type, reasoning)
                model_type: "cheap" or "expensive"
                reasoning: Why this model was chosen
        """
        question_lower = question.lower()

        # Check token count first (fastest check)
        if token_count > 0 and token_count < self.token_threshold:
            return ("cheap", f"Token count ({token_count}) below threshold ({self.token_threshold})")

        # Check for simple question patterns
        simple_keywords = self.SIMPLE_KEYWORDS.get(language, self.SIMPLE_KEYWORDS["pt-br"])
        for keyword in simple_keywords:
            if keyword in question_lower:
                return ("cheap", f"Simple question pattern detected: '{keyword}'")

        # Check for complex question patterns
        complex_keywords = self.COMPLEX_KEYWORDS.get(language, self.COMPLEX_KEYWORDS["pt-br"])
        for keyword in complex_keywords:
            if keyword in question_lower:
                return ("expensive", f"Complex question pattern detected: '{keyword}'")

        # Check question length (very short = simple)
        if len(question) < 50:
            return ("cheap", "Very short question")

        # Check question length (very long = complex)
        if len(question) > 200:
            return ("expensive", "Very long question")

        # Default: use expensive model (conservative)
        return ("expensive", "Default routing (uncertain complexity)")

    def get_model_for_provider(
        self,
        provider: str,
        model_type: str
    ) -> str:
        """
        Get specific model name for a provider and type.

        Args:
            provider: "openai" or "anthropic"
            model_type: "cheap" or "expensive"

        Returns:
            str: Model name
        """
        models = {
            "openai": {
                "cheap": "gpt-4o-mini",
                "expensive": "gpt-4o"
            },
            "anthropic": {
                "cheap": "claude-haiku-4-5",
                "expensive": "claude-sonnet-4-5"
            }
        }

        provider_models = models.get(provider, models["openai"])
        return provider_models.get(model_type, provider_models["expensive"])


# Singleton instance
_router = None


def get_model_router() -> ModelRouter:
    """Get singleton ModelRouter instance."""
    global _router
    if _router is None:
        from app.core.config import settings
        threshold = settings.CHEAP_MODEL_THRESHOLD if hasattr(settings, 'CHEAP_MODEL_THRESHOLD') else 50
        _router = ModelRouter(token_threshold=threshold)
    return _router
