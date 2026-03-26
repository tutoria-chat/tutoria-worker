"""
Topic Classification Service

Uses the same 3-tier model selection pattern as DocumentExtractionService:
1. DB: Query AIModels where use_for_topic_classification=True
2. Config: Fall back to TOPIC_CLASSIFICATION_PROVIDER + TOPIC_CLASSIFICATION_FALLBACK_CHAIN
3. Execution: Loop through chain using provider services (AIService, GeminiService, etc.)
"""
import json
import logging
from typing import List, Dict, Any, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_model import AIModel

logger = logging.getLogger(__name__)

CLASSIFICATION_PROMPT = """You are an expert at categorizing educational questions into academic topics.

Given the following student questions from a course module, classify them by academic topic.

Return a JSON object with a single key "topics" containing an array. Each element should have:
- "topic": the topic name (short, 2-5 words)
- "count": how many questions belong to this topic
- "samples": array of up to 3 example questions for this topic

Questions:
{questions}

Return ONLY valid JSON. No markdown, no explanation."""


class TopicClassificationService:
    def __init__(self, db: Session):
        self.db = db

    def _get_provider_chain(self) -> List[Dict[str, Any]]:
        """Get the ordered list of AI providers/models for topic classification.
        Same 3-tier pattern as DocumentExtractionService._get_provider_chain()."""
        chain = []

        # Tier 1: DB-configured models
        try:
            db_models = (
                self.db.query(AIModel)
                .filter(
                    AIModel.use_for_topic_classification == True,
                    AIModel.is_active == True,
                    AIModel.is_deprecated == False,
                )
                .all()
            )
            if db_models:
                for model in db_models:
                    chain.append({
                        "provider": model.provider.lower(),
                        "model": model.model_name,
                    })
                logger.info(f"Using DB-configured topic classification models: {[m['model'] for m in chain]}")
                return chain
        except Exception as e:
            logger.warning(f"Failed to query DB for topic classification models: {e}")

        # Tier 2: Config fallback
        primary = settings.TOPIC_CLASSIFICATION_PROVIDER.lower()
        fallback_str = getattr(settings, 'TOPIC_CLASSIFICATION_FALLBACK_CHAIN', 'openai')
        fallbacks = [p.strip().lower() for p in fallback_str.split(",") if p.strip()]

        for provider in [primary] + fallbacks:
            chain.append({"provider": provider, "model": None})

        logger.info(f"Using config-based topic classification chain: {[c['provider'] for c in chain]}")
        return chain

    async def _call_ai_provider(self, provider: str, prompt_text: str, model_name: Optional[str] = None) -> Optional[str]:
        """Call a specific AI provider for topic classification.
        Same pattern as DocumentExtractionService._call_ai_provider()."""
        from types import SimpleNamespace

        dummy_module = SimpleNamespace(
            id=0, ai_model=None, ai_model_id=None, name="topic_classification"
        )

        messages = [
            {"role": "user", "content": prompt_text}
        ]

        try:
            if provider == "deepseek":
                from app.services.deepseek_service import DeepSeekService
                service = DeepSeekService(module=dummy_module, model_name=model_name, db=self.db)
            elif provider == "gemini":
                from app.services.gemini_service import GeminiService
                service = GeminiService(module=dummy_module, model_name=model_name, db=self.db)
            elif provider == "openai":
                from app.services.ai_service import AIService
                service = AIService(module=dummy_module, db=self.db)
                if model_name:
                    service.model_name = model_name
            elif provider == "anthropic":
                from app.services.anthropic_service import AnthropicService
                service = AnthropicService(module=dummy_module, model_name=model_name, db=self.db)
            else:
                logger.warning(f"Unknown provider for topic classification: {provider}")
                return None

            result = await service.answer_question_with_history(messages)
            return result.get("response", "")

        except Exception as e:
            logger.warning(f"Topic classification failed with {model_name or provider}: {e}")
            return None

    async def classify(self, questions: List[str]) -> List[Dict[str, Any]]:
        """Classify a list of questions into academic topics using the provider chain."""
        # Build prompt with questions (limit to 200 to stay under token limits)
        question_text = "\n".join(f"- {q}" for q in questions[:200])
        prompt = CLASSIFICATION_PROMPT.format(questions=question_text)

        chain = self._get_provider_chain()

        for entry in chain:
            provider = entry["provider"]
            model_name = entry.get("model")

            try:
                response = await self._call_ai_provider(provider, prompt, model_name)
                if not response:
                    continue

                # Parse JSON response
                # Try to extract JSON from response (may be wrapped in markdown code block)
                clean = response.strip()
                if clean.startswith("```"):
                    # Remove markdown code block
                    lines = clean.split("\n")
                    clean = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

                data = json.loads(clean)
                topics = data.get("topics", data if isinstance(data, list) else [])

                if topics:
                    logger.info(f"Topic classification succeeded with {model_name or provider}: {len(topics)} topics")
                    return topics

            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse topic classification JSON from {model_name or provider}: {e}")
                continue
            except Exception as e:
                logger.warning(f"Topic classification failed with {model_name or provider}: {e}")
                continue

        logger.error("Topic classification failed with all providers in chain")
        return []
