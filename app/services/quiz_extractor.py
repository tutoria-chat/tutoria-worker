"""
Quiz Extractor Service - Extract quiz questions from uploaded files.

Accepts uploaded files (PDF, DOCX, XLSX, CSV) containing quiz questions,
uses AI to analyze and extract structured questions, and saves them to the database.
"""
import logging
import json
import csv
import io
import asyncio
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Module, Quiz, QuizDifficulty
from app.models.ai_model import AIModel

logger = logging.getLogger(__name__)


class QuizExtractorService:
    """Service for extracting quiz questions from uploaded files."""

    def __init__(self, module: Module, db: Session):
        self.module = module
        self.db = db

    async def extract_from_text(self, file_content: str, file_name: str) -> List[Dict[str, Any]]:
        """
        Extract quiz questions from text content using AI.

        Args:
            file_content: Text content from the uploaded file
            file_name: Original file name for context

        Returns:
            List of extracted question dictionaries (not yet saved to DB)

        Raises:
            Exception: If extraction fails
        """
        logger.info(f"Extracting quiz questions from file: {file_name}")

        tutor_language = self.module.tutor_language or "pt-br"

        system_prompt = (
            "You are an expert at analyzing educational documents and extracting "
            "quiz/test questions. Extract ALL questions found in the document. "
            "For each question, identify the options, correct answer, and generate "
            "explanations for why each option is correct or incorrect. "
            "IMPORTANT: The correct_answer field must reflect the ACTUAL correct answer "
            "for each question (A, B, C, D, or E) — do NOT default to A. "
            "Always respond with valid JSON."
        )

        user_prompt = f"""Analyze the following document and extract ALL quiz/test questions found in it.

Document: {file_name}
Module: {self.module.name}
Language: {tutor_language}

CONTENT:
{file_content[:50000]}

For each question found, structure it as follows. If the correct answer is not explicitly marked, use your best judgment based on the content.

CRITICAL: The "correct_answer" field MUST be the actual correct option letter for each question. Do NOT always use "A" — each question has its own correct answer which could be A, B, C, D, or E.

Respond with JSON:
{{"questions": [
  {{
    "question": "The question text exactly as found",
    "options": {{
      "A": {{"text": "Option A text", "explanation": "Why A is correct/incorrect (2-3 sentences)"}},
      "B": {{"text": "Option B text", "explanation": "Why B is correct/incorrect"}},
      "C": {{"text": "Option C text", "explanation": "Why C is correct/incorrect"}},
      "D": {{"text": "Option D text", "explanation": "Why D is correct/incorrect"}}
    }},
    "correct_answer": "B",
    "difficulty": "easy|medium|hard",
    "concepts": ["concept1", "concept2"]
  }}
]}}

Extract ALL questions. If a question has 5 options, include option E. The correct_answer varies per question — identify it from the document content."""

        # Call AI
        response_text = await self._call_ai(system_prompt, user_prompt)

        if not response_text:
            raise Exception("Empty response from AI during quiz extraction")

        # Parse response
        questions = self._parse_response(response_text)

        if not questions:
            raise Exception(
                f"No questions could be extracted from {file_name}. "
                "Make sure the file contains quiz/test questions with answer options."
            )

        logger.info(f"Extracted {len(questions)} questions from {file_name}")
        return questions

    def save_extracted_questions(
        self,
        questions: List[Dict[str, Any]],
        uploaded_file_id: Optional[int] = None
    ) -> List[Quiz]:
        """
        Save extracted questions to the database.

        Args:
            questions: List of question dicts from extract_from_text
            uploaded_file_id: ID of the uploaded quiz file (for tracking)

        Returns:
            List of saved Quiz objects
        """
        # Get the next question number for this module
        max_num = self.db.query(Quiz.question_number).filter(
            Quiz.module_id == self.module.id
        ).order_by(Quiz.question_number.desc()).first()
        start_number = (max_num[0] + 1) if max_num else 1

        quizzes = []
        for i, q_data in enumerate(questions):
            try:
                difficulty_str = q_data.get("difficulty", "medium").lower()
                if difficulty_str not in ("easy", "medium", "hard"):
                    difficulty_str = "medium"

                quiz = Quiz(
                    module_id=self.module.id,
                    question_number=start_number + i,
                    question_text=q_data["question"],
                    difficulty=QuizDifficulty(difficulty_str),
                    option_a=q_data["options"]["A"]["text"],
                    option_b=q_data["options"]["B"]["text"],
                    option_c=q_data["options"]["C"]["text"],
                    option_d=q_data["options"]["D"]["text"],
                    option_e=q_data["options"].get("E", {}).get("text"),
                    correct_answer=q_data["correct_answer"],
                    explanation_a=q_data["options"]["A"]["explanation"],
                    explanation_b=q_data["options"]["B"]["explanation"],
                    explanation_c=q_data["options"]["C"]["explanation"],
                    explanation_d=q_data["options"]["D"]["explanation"],
                    explanation_e=q_data["options"].get("E", {}).get("explanation"),
                    concepts_covered=",".join(q_data.get("concepts", [])),
                    source="uploaded",
                    uploaded_file_id=uploaded_file_id,
                )

                self.db.add(quiz)
                quizzes.append(quiz)
            except (KeyError, TypeError) as e:
                logger.warning(f"Skipping malformed extracted question {i+1}: {e}")
                continue

        if quizzes:
            self.db.commit()
            logger.info(f"Saved {len(quizzes)} extracted questions to database")

        return quizzes

    def _get_provider_chain(self) -> List[Dict[str, Any]]:
        """
        Get the ordered list of AI providers/models for quiz extraction.

        Priority:
        1. DB: AIModels with UseForFileExtraction=True (same models used for file processing)
        2. Config fallback: FILE_PROCESSING_PROVIDER + FILE_PROCESSING_FALLBACK_CHAIN
        """
        chain = []

        # Try DB-configured models first
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
                for model in db_models:
                    chain.append({
                        "provider": model.provider.lower(),
                        "model": model.model_name,
                    })
                logger.info(f"Quiz extraction using DB-configured models: {[m['model'] for m in chain]}")
                return chain
        except Exception as e:
            logger.warning(f"Failed to query DB for extraction models: {e}")

        # Fallback to config settings
        primary = settings.FILE_PROCESSING_PROVIDER.lower()
        chain_str = getattr(settings, 'FILE_PROCESSING_FALLBACK_CHAIN', 'gemini,openai')
        fallbacks = [p.strip().lower() for p in chain_str.split(",") if p.strip()]

        for provider in [primary] + fallbacks:
            chain.append({"provider": provider, "model": None})

        logger.info(f"Quiz extraction using config-based chain: {[m['provider'] for m in chain]}")
        return chain

    async def _call_ai_provider(self, provider: str, system_prompt: str, user_prompt: str, model_name: str = None) -> Optional[str]:
        """Call a specific AI provider for quiz extraction with higher token limit."""
        from types import SimpleNamespace

        dummy_module = SimpleNamespace(
            id=self.module.id, ai_model=None, ai_model_id=None, name=self.module.name
        )

        # Quiz extraction needs more tokens than regular chat (large JSON output)
        max_tokens = 32768

        if provider == "anthropic":
            try:
                from app.services.anthropic_service import AnthropicService
            except ImportError:
                logger.error("anthropic package not installed; skipping anthropic provider")
                return None
            service = AnthropicService(module=dummy_module, model_name=model_name, db=self.db)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            result = await service.answer_question_with_history(messages)
            return result.get("response", "")

        if provider == "openai":
            # AIService has separate sync/async clients; use async_client
            from app.services.ai_service import AIService
            service = AIService(module=dummy_module, db=self.db)
            if model_name:
                service.model_name = model_name
            client = service.async_client
            if not client:
                logger.warning("OpenAI async_client not available, falling back to sync")
                return None
        elif provider == "deepseek":
            from app.services.deepseek_service import DeepSeekService
            service = DeepSeekService(module=dummy_module, model_name=model_name, db=self.db)
            client = service.client  # Already AsyncOpenAI
        elif provider == "gemini":
            from app.services.gemini_service import GeminiService
            service = GeminiService(module=dummy_module, model_name=model_name, db=self.db)
            client = service.client  # Already AsyncOpenAI
        elif provider == "xai":
            from app.services.xai_service import XAIService
            service = XAIService(module=dummy_module, model_name=model_name, db=self.db)
            client = service.client  # Already AsyncOpenAI
        else:
            logger.warning(f"Unsupported quiz extraction provider: {provider}")
            return None

        # All OpenAI-compatible providers use the same chat completions API
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        response = await client.chat.completions.create(
            model=service.model_name,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return response.choices[0].message.content

    async def _call_ai(self, system_prompt: str, user_prompt: str) -> str:
        """Call AI for quiz extraction using DB-configured provider chain with fallback."""
        chain = self._get_provider_chain()
        last_error = None

        for entry in chain:
            provider = entry["provider"]
            model_name = entry.get("model")
            try:
                logger.info(f"Quiz extraction attempting provider: {provider} (model: {model_name or 'default'})")
                result = await self._call_ai_provider(provider, system_prompt, user_prompt, model_name)
                if result:
                    return result
            except Exception as e:
                last_error = e
                logger.warning(f"Quiz extraction failed with {model_name or provider}: {type(e).__name__}: {str(e)[:200]}")
                continue

        raise Exception(f"Quiz extraction failed with all providers. Last error: {last_error}")

    def _parse_response(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse AI response to extract questions.

        Handles:
        - Clean JSON objects/arrays
        - JSON wrapped in ```json code fences
        - Truncated responses (salvages complete questions)
        """
        import re

        cleaned = response_text.strip()

        # Strip markdown code fences if present
        code_block = re.search(r'```(?:json)?\s*\n?(.*?)```', cleaned, re.DOTALL)
        if code_block:
            cleaned = code_block.group(1).strip()
        elif cleaned.startswith("```"):
            # Truncated response — opening fence but no closing fence
            cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned).strip()

        # Attempt 1: Parse as complete JSON
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and "questions" in parsed:
                return parsed["questions"]
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

        # Attempt 2: Find and parse a JSON array
        start = cleaned.find("[")
        end = cleaned.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass

        # Attempt 3: Salvage complete questions from truncated JSON.
        # The AI response was likely cut off by max_tokens. We try to close
        # the JSON at the last complete question object boundary.
        start = cleaned.find("[")
        if start >= 0:
            array_content = cleaned[start:]
            # Find the last complete object by looking for '}' followed by
            # either ',' or end-of-content (meaning another question would follow).
            last_complete = -1
            depth = 0
            for i, ch in enumerate(array_content):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        last_complete = i
            if last_complete > 0:
                truncated_fix = array_content[:last_complete + 1].rstrip(", \n\r\t") + "]"
                # Also close an outer {"questions": ...} wrapper if needed
                if cleaned[:start].strip().endswith('"questions":'):
                    truncated_fix += "}"
                try:
                    salvaged = json.loads(truncated_fix)
                    if isinstance(salvaged, list) and salvaged:
                        logger.warning(
                            f"AI response was truncated — salvaged {len(salvaged)} "
                            f"complete question(s) from partial JSON."
                        )
                        return salvaged
                except json.JSONDecodeError:
                    pass

        raise ValueError(f"Could not parse AI response as JSON. Preview: {cleaned[:300]}")


def extract_text_from_file(file_content: bytes, file_name: str) -> str:
    """
    Extract text from various file formats.

    Args:
        file_content: Raw file bytes
        file_name: File name with extension

    Returns:
        Extracted text content
    """
    ext = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else ""

    if ext == "txt":
        return file_content.decode("utf-8", errors="replace")

    elif ext == "csv":
        text = file_content.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = []
        for row in reader:
            rows.append(" | ".join(row))
        return "\n".join(rows)

    elif ext == "pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(file_content))
            pages = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n\n".join(pages)
        except ImportError:
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(io.BytesIO(file_content))
                pages = []
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        pages.append(text)
                return "\n\n".join(pages)
            except ImportError:
                raise ImportError("pypdf or PyPDF2 required for PDF extraction")

    elif ext in ("docx", "doc"):
        try:
            from docx import Document
            doc = Document(io.BytesIO(file_content))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n\n".join(paragraphs)
        except ImportError:
            raise ImportError("python-docx required for DOCX extraction")

    elif ext in ("xlsx", "xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(file_content), read_only=True)
            rows = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    cell_values = [str(c) if c is not None else "" for c in row]
                    if any(v.strip() for v in cell_values):
                        rows.append(" | ".join(cell_values))
            return "\n".join(rows)
        except ImportError:
            raise ImportError("openpyxl required for XLSX extraction")

    else:
        # Try as plain text
        return file_content.decode("utf-8", errors="replace")
