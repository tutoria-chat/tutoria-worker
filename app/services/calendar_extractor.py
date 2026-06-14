"""
Calendar Extractor — pull candidate calendar events out of an uploaded document
(syllabus / schedule) using AI, for professor review before they become
CourseEvents. Mirrors the AI machinery of quiz_extractor.py.

Output per event: {title, type, date (YYYY-MM-DD), time (HH:MM|None), description}.
`type` is one of the CourseEvent types: test | assignment | holiday | field_event | other.
"""
import json
import logging
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_model import AIModel

logger = logging.getLogger(__name__)

EVENT_TYPES = {"test", "assignment", "holiday", "field_event", "other"}

SYSTEM_PROMPT = (
    "You extract academic calendar events from course documents (syllabi, schedules, "
    "plans). Return ONLY valid JSON. Do not invent events that aren't in the document."
)


class CalendarExtractorService:
    def __init__(self, db: Session, course_name: str = "", language: str = "pt-br", year_hint: Optional[int] = None):
        self.db = db
        self.course_name = course_name
        self.language = language or "pt-br"
        self.year_hint = year_hint

    async def extract_from_text(self, text: str, file_name: str) -> List[Dict[str, Any]]:
        """Run AI extraction over already-parsed document text. Returns candidate events."""
        content = (text or "").strip()
        if not content:
            return []

        year_line = (
            f"If a year is missing from a date, assume {self.year_hint}.\n"
            if self.year_hint else
            "If a year is missing, infer it from nearby context; otherwise assume the next occurrence.\n"
        )

        user_prompt = (
            f"Document: {file_name}\nCourse: {self.course_name or 'N/A'}\n"
            f"Reply language for titles/descriptions: {self.language}\n\n"
            "Extract every dated academic event (exams/tests, assignment due dates, holidays/breaks, "
            "field trips or special events, and anything else with a date). For each event return:\n"
            '- "title": short event name\n'
            '- "type": one of test | assignment | holiday | field_event | other '
            "(exam/prova/test→test; due date/entrega/trabalho→assignment; feriado/recesso/holiday→holiday; "
            "excursion/visita/palestra→field_event; otherwise→other)\n"
            '- "date": "YYYY-MM-DD"\n'
            '- "time": "HH:MM" 24h, or null if none given\n'
            '- "description": optional one-line note, or ""\n\n'
            f"{year_line}"
            "Only include events that genuinely have a date in the document. "
            'Respond with JSON exactly: {"events": [ ... ]}\n\n'
            "DOCUMENT CONTENT:\n"
            f"{content[:50000]}"
        )

        raw = await self._call_ai(SYSTEM_PROMPT, user_prompt)
        return self._parse(raw)

    # ---- AI plumbing (mirrors quiz_extractor) ----

    def _get_provider_chain(self) -> List[Dict[str, Any]]:
        chain: List[Dict[str, Any]] = []
        try:
            db_models = (
                self.db.query(AIModel)
                .filter(
                    AIModel.use_for_file_extraction == True,
                    AIModel.is_active == True,
                    AIModel.is_deprecated == False,
                )
                .all()
            )
            if db_models:
                return [{"provider": m.provider.lower(), "model": m.model_name} for m in db_models]
        except Exception as e:
            logger.warning(f"Failed to query DB for extraction models: {e}")

        primary = settings.FILE_PROCESSING_PROVIDER.lower()
        chain_str = getattr(settings, "FILE_PROCESSING_FALLBACK_CHAIN", "gemini,openai")
        fallbacks = [p.strip().lower() for p in chain_str.split(",") if p.strip()]
        for provider in [primary] + fallbacks:
            chain.append({"provider": provider, "model": None})
        return chain

    async def _call_ai_provider(self, provider: str, system_prompt: str, user_prompt: str, model_name: Optional[str]) -> Optional[str]:
        dummy_module = SimpleNamespace(id=0, ai_model=None, ai_model_id=None, name=self.course_name or "calendar")
        max_tokens = 8192

        if provider == "anthropic":
            try:
                from app.services.anthropic_service import AnthropicService
            except ImportError:
                logger.error("anthropic package not installed; skipping")
                return None
            service = AnthropicService(module=dummy_module, model_name=model_name, db=self.db)
            return (await service.answer_question_with_history([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ])).get("response", "")

        if provider == "openai":
            from app.services.ai_service import AIService
            service = AIService(module=dummy_module, db=self.db)
            if model_name:
                service.model_name = model_name
            client = service.async_client
            if not client:
                return None
        elif provider == "deepseek":
            from app.services.deepseek_service import DeepSeekService
            service = DeepSeekService(module=dummy_module, model_name=model_name, db=self.db)
            client = service.client
        elif provider == "gemini":
            from app.services.gemini_service import GeminiService
            service = GeminiService(module=dummy_module, model_name=model_name, db=self.db)
            client = service.client
        elif provider == "xai":
            from app.services.xai_service import XAIService
            service = XAIService(module=dummy_module, model_name=model_name, db=self.db)
            client = service.client
        else:
            logger.warning(f"Unsupported calendar extraction provider: {provider}")
            return None

        response = await client.chat.completions.create(
            model=service.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return response.choices[0].message.content

    async def _call_ai(self, system_prompt: str, user_prompt: str) -> str:
        last_error = None
        for entry in self._get_provider_chain():
            try:
                result = await self._call_ai_provider(entry["provider"], system_prompt, user_prompt, entry.get("model"))
                if result:
                    return result
            except Exception as e:
                last_error = e
                logger.warning(f"Calendar extraction failed with {entry['provider']}: {type(e).__name__}: {str(e)[:200]}")
                continue
        raise Exception(f"Calendar extraction failed with all providers. Last error: {last_error}")

    def _parse(self, response_text: str) -> List[Dict[str, Any]]:
        if not response_text:
            return []
        text = response_text.strip()
        # Strip ```json fences if present
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    return []
        if data is None:
            return []

        raw_events = data.get("events", data) if isinstance(data, dict) else data
        if not isinstance(raw_events, list):
            return []

        events: List[Dict[str, Any]] = []
        for item in raw_events:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            date = str(item.get("date", "")).strip()
            if not title or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                continue  # an event needs at least a title and a real date
            etype = str(item.get("type", "other")).strip().lower()
            if etype not in EVENT_TYPES:
                etype = "other"
            time_val = str(item.get("time", "") or "").strip()
            if not re.match(r"^\d{2}:\d{2}$", time_val):
                time_val = ""
            events.append({
                "title": title[:255],
                "type": etype,
                "date": date,
                "time": time_val,
                "description": str(item.get("description", "") or "").strip()[:2000],
            })
        return events
