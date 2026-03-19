"""
Response Formatting Service

Post-processes AI responses into clean Markdown/LaTeX using a DB-configured
formatting model (AIModels where UseForFormatting=True).

If no formatting model is configured, returns None and the caller uses the raw response.
"""
import logging
from typing import Optional
from sqlalchemy.orm import Session

from app.models.ai_model import AIModel

logger = logging.getLogger(__name__)

FORMATTING_SYSTEM_PROMPT = """You are a text formatter for an educational AI platform. Your only job is to reformat the given response — do not change its meaning or add content.

Rules:
- Inline math expressions → $...$ (e.g. $E = mc^2$)
- Block/display equations → $$...$$ on their own line
- Code snippets → fenced code blocks with the language tag
- Structure with Markdown: headers (##, ###), bold (**), italic (*), bullet lists, numbered lists
- Keep all original content and explanations intact
- Return ONLY the reformatted text, nothing else"""


def get_formatting_model(db: Session) -> Optional[AIModel]:
    """Return the active DB-configured formatting model, or None if not set."""
    try:
        return (
            db.query(AIModel)
            .filter(
                AIModel.use_for_formatting == True,
                AIModel.is_active == True,
                AIModel.is_deprecated == False,
            )
            .first()
        )
    except Exception as e:
        logger.warning(f"Could not query formatting model: {e}")
        return None


async def format_response(text: str, model: AIModel, db: Session) -> Optional[str]:
    """
    Format a response using the configured formatting model.

    Args:
        text: Raw response text from the primary AI
        model: AIModel with use_for_formatting=True
        db: DB session for service init

    Returns:
        Formatted text, or None if formatting failed
    """
    if not text or not text.strip():
        return None

    from types import SimpleNamespace
    dummy_module = SimpleNamespace(id=0, ai_model=model, ai_model_id=model.id, name="formatter")

    messages = [
        {"role": "system", "content": FORMATTING_SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]

    provider = model.provider.lower()
    try:
        service = _get_service(provider, dummy_module, model.model_name, db)
        if service is None:
            return None
        result = await service.answer_question_with_history(messages)
        formatted = result.get("response", "").strip()
        if formatted and len(formatted) > 20:
            logger.info(f"Formatted response via {model.model_name} ({len(text)} → {len(formatted)} chars)")
            return formatted
    except Exception as e:
        logger.warning(f"Formatting failed with {model.model_name}: {e}")

    return None


def _get_service(provider: str, dummy_module, model_name: str, db: Session):
    """Instantiate the right provider service for the formatting model."""
    if provider == "openai":
        from app.services.ai_service import AIService
        svc = AIService(module=dummy_module, db=db)
        svc.model_name = model_name
        return svc
    elif provider == "gemini":
        from app.services.gemini_service import GeminiService
        return GeminiService(module=dummy_module, model_name=model_name, db=db)
    elif provider == "deepseek":
        from app.services.deepseek_service import DeepSeekService
        return DeepSeekService(module=dummy_module, model_name=model_name, db=db)
    elif provider == "bedrock":
        from app.services.bedrock_service import BedrockService
        return BedrockService(module=dummy_module, db=db)
    elif provider == "xai":
        from app.services.xai_service import XAIService
        return XAIService(module=dummy_module, model_name=model_name, db=db)
    elif provider == "anthropic":
        try:
            from app.services.anthropic_service import AnthropicService
            return AnthropicService(module=dummy_module, model_name=model_name, db=db)
        except ImportError:
            logger.error("anthropic package not installed")
            return None
    else:
        logger.warning(f"Unknown formatting provider: {provider}")
        return None
