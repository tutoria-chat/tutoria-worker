"""
Quiz Generator Service - Pre-generate quiz questions for modules

Generates quiz questions ONCE when a module is created, using AI to create
high-quality questions with detailed explanations for all options.

Uses Chat Completions API directly (not Assistants API) for faster,
more reliable quiz generation with proper JSON output.

Provider chain is configured via AIModels table (UseForFileExtraction=True),
falling back to config settings if no DB models are flagged — same logic
as quiz extraction and file extraction services.
"""
import logging
import json
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Module, Quiz, QuizDifficulty
from app.models.ai_model import AIModel
from app.services.document_context_service import get_document_context_service

logger = logging.getLogger(__name__)


class QuizGeneratorService:
    """Service for generating pre-generated quiz questions for modules."""

    def __init__(self, module: Module, db: Session):
        """
        Initialize quiz generator.

        Uses Chat Completions API directly (bypasses Assistants API)
        for simpler, faster, and more reliable quiz generation.

        Provider selection follows the same logic as quiz extraction
        and file extraction: DB-configured models first, then config fallback.

        Args:
            module: Module to generate quizzes for
            db: Database session
        """
        self.module = module
        self.db = db
        self._course_materials = ""  # Populated by generate_quiz_bank

    async def generate_quiz_bank(self, count: int = 50) -> List[Quiz]:
        """
        Generate a bank of quiz questions for the module.

        Creates 'count' questions with:
        - Mix of difficulty levels (40% easy, 40% medium, 20% hard)
        - 4-5 answer options (A, B, C, D, E)
        - Detailed explanations for ALL options (not just correct answer)
        - Based on module's course materials

        Args:
            count: Number of questions to generate (default: 50)

        Returns:
            List of Quiz objects (saved to database)

        Raises:
            Exception: If ALL difficulty levels fail (partial success is OK)
        """
        logger.info(f"Generating {count} quiz questions for module {self.module.id} ({self.module.name})")

        # Check if module has files
        if not self.module.files:
            logger.error(f"❌ Module {self.module.id} has NO FILES! Cannot generate meaningful quizzes.")
            raise ValueError(f"Module {self.module.id} has no files. Upload files first before generating quizzes.")

        active_files = [f for f in self.module.files if f.is_active]
        logger.info(f"📊 Module has {len(active_files)} active files: {[f.name for f in active_files]}")

        # Extract course materials from files (PDFs, YouTube transcripts, etc.)
        # This will extract on-demand if not pre-extracted
        doc_context_service = get_document_context_service(self.module, self.db)
        context_result = await doc_context_service.build_context_for_prompt(max_chars=50000)
        self._course_materials = context_result.get("context", "")
        files_used = context_result.get("files_used", [])

        if self._course_materials:
            logger.info(f"📚 Loaded {context_result.get('total_chars', 0)} chars of course materials from {len(files_used)} files")
        else:
            # Log details about why there's no content
            file_details = []
            for f in active_files:
                detail = f"'{f.name}': extracted={bool(f.extracted_text)}, transcript={bool(f.transcript_text)}, type={f.content_type}, source={f.source_type}, blob={f.blob_path}"
                file_details.append(detail)
                logger.error(f"❌ File {detail}")

            error_msg = f"Module {self.module.id} has {len(active_files)} files but no content could be extracted. Files: {file_details}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        # Calculate difficulty distribution
        easy_count = int(count * 0.4)  # 40%
        medium_count = int(count * 0.4)  # 40%
        hard_count = count - easy_count - medium_count  # Remaining (~20%)

        logger.info(f"Difficulty distribution: {easy_count} easy, {medium_count} medium, {hard_count} hard")

        all_quizzes = []
        errors = []

        # Generate each difficulty level - continue on partial failures
        for difficulty, diff_count, start in [
            (QuizDifficulty.EASY, easy_count, 1),
            (QuizDifficulty.MEDIUM, medium_count, easy_count + 1),
            (QuizDifficulty.HARD, hard_count, easy_count + medium_count + 1),
        ]:
            try:
                quizzes = await self._generate_questions_by_difficulty(
                    difficulty, diff_count, start_number=start
                )
                all_quizzes.extend(quizzes)
            except Exception as e:
                logger.error(f"Failed to generate {difficulty.value} questions: {e}")
                errors.append(f"{difficulty.value}: {str(e)[:200]}")
                self.db.rollback()
                continue

        if not all_quizzes:
            error_details = "; ".join(errors)
            raise Exception(
                f"Quiz generation failed for ALL difficulty levels. "
                f"Module: {self.module.id} ({self.module.name}). "
                f"Errors: {error_details}"
            )

        if errors:
            logger.warning(
                f"Partial quiz generation: {len(all_quizzes)} questions generated, "
                f"some difficulty levels failed: {'; '.join(errors)}"
            )

        logger.info(f"Successfully generated {len(all_quizzes)} quiz questions")
        return all_quizzes

    def _get_provider_chain(self) -> List[Dict[str, Any]]:
        """
        Get the ordered list of AI providers/models for quiz generation.

        Same logic as quiz extraction and file extraction:
        1. DB: AIModels with UseForFileExtraction=True (shared pool)
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
                logger.info(f"Quiz generation using DB-configured models: {[m['model'] for m in chain]}")
                return chain
        except Exception as e:
            logger.warning(f"Failed to query DB for extraction models: {e}")

        # Fallback to config settings
        primary = settings.FILE_PROCESSING_PROVIDER.lower()
        chain_str = getattr(settings, 'FILE_PROCESSING_FALLBACK_CHAIN', 'gemini,openai')
        fallbacks = [p.strip().lower() for p in chain_str.split(",") if p.strip()]

        for provider in [primary] + fallbacks:
            chain.append({"provider": provider, "model": None})

        logger.info(f"Quiz generation using config-based chain: {[m['provider'] for m in chain]}")
        return chain

    async def _call_ai_provider(self, provider: str, system_prompt: str, user_prompt: str, model_name: str = None) -> Optional[str]:
        """Call a specific AI provider for quiz generation with higher token limit."""
        from types import SimpleNamespace

        dummy_module = SimpleNamespace(
            id=self.module.id, ai_model=None, ai_model_id=None, name=self.module.name
        )

        max_tokens = 16384

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
            logger.warning(f"Unsupported quiz generation provider: {provider}")
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
            temperature=0.7,
        )
        return response.choices[0].message.content

    async def _call_ai_for_quiz(self, system_prompt: str, user_prompt: str) -> str:
        """
        Call AI for quiz generation using DB-configured provider chain with fallback.

        Same pattern as quiz extraction and file extraction services.

        Args:
            system_prompt: System message for quiz generation context
            user_prompt: User message with quiz generation instructions

        Returns:
            Raw response text from AI

        Raises:
            Exception: If all providers fail
        """
        chain = self._get_provider_chain()
        last_error = None

        for entry in chain:
            provider = entry["provider"]
            model_name = entry.get("model")
            try:
                logger.info(f"Quiz generation attempting provider: {provider} (model: {model_name or 'default'})")
                result = await self._call_ai_provider(provider, system_prompt, user_prompt, model_name)
                if result:
                    logger.info(f"Quiz generation succeeded with {model_name or provider}")
                    return result
            except Exception as e:
                last_error = e
                logger.warning(f"Quiz generation failed with {model_name or provider}: {type(e).__name__}: {str(e)[:200]}")
                continue

        raise Exception(f"Quiz generation failed with all providers. Last error: {last_error}")

    async def _generate_questions_by_difficulty(
        self,
        difficulty: QuizDifficulty,
        count: int,
        start_number: int
    ) -> List[Quiz]:
        """
        Generate quiz questions for a specific difficulty level.

        Uses Chat Completions API directly for reliable JSON generation.

        Args:
            difficulty: Question difficulty (EASY, MEDIUM, HARD)
            count: Number of questions to generate
            start_number: Starting question number

        Returns:
            List of Quiz objects

        Raises:
            Exception: If AI call fails after retries (propagated to caller)
        """
        if count == 0:
            return []

        logger.info(f"Generating {count} {difficulty.value} questions (starting at #{start_number})")

        # Build prompt for AI
        system_prompt = "You are an expert educational content creator specializing in quiz generation. Always respond with valid JSON."
        user_prompt = self._build_generation_prompt(difficulty, count)

        # Call AI directly via Chat Completions (with retry + fallback)
        response_text = await self._call_ai_for_quiz(system_prompt, user_prompt)

        if not response_text or not response_text.strip():
            raise Exception(f"Empty response from AI for {difficulty.value} questions")

        # Parse JSON response
        questions_data = self._parse_ai_response(response_text)

        if not questions_data:
            raise Exception(
                f"No questions parsed from AI response for {difficulty.value}. "
                f"Response preview: {response_text[:300]}"
            )

        logger.info(f"Parsed {len(questions_data)} {difficulty.value} questions from AI response")

        # Create Quiz objects
        quizzes = []
        parse_errors = 0
        for i, q_data in enumerate(questions_data[:count]):
            try:
                quiz = Quiz(
                    module_id=self.module.id,
                    question_number=start_number + i,
                    question_text=q_data["question"],
                    difficulty=difficulty.value,
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
                    concepts_covered=",".join(q_data.get("concepts", []))
                )

                self.db.add(quiz)
                quizzes.append(quiz)

            except (KeyError, TypeError) as e:
                parse_errors += 1
                logger.warning(f"Skipping malformed question {i+1}: {e}")
                continue

        if not quizzes:
            self.db.rollback()
            raise Exception(
                f"All {len(questions_data)} parsed questions had formatting errors "
                f"for {difficulty.value} difficulty"
            )

        # Commit all quizzes for this difficulty
        self.db.commit()
        logger.info(
            f"Saved {len(quizzes)} {difficulty.value} questions to database"
            + (f" ({parse_errors} skipped due to format errors)" if parse_errors else "")
        )

        return quizzes

    def _build_generation_prompt(self, difficulty: QuizDifficulty, count: int) -> str:
        """
        Build AI prompt for quiz generation.

        Args:
            difficulty: Question difficulty
            count: Number of questions to generate

        Returns:
            str: Prompt for AI
        """
        tutor_language = self.module.tutor_language or "pt-br"

        # Get module context (files, description)
        module_context = f"Module: {self.module.name}\nDescription: {self.module.description or 'N/A'}"

        # Include extracted course materials (PDFs, YouTube transcripts, etc.)
        course_materials = getattr(self, '_course_materials', '')
        if course_materials:
            # Truncate if too long to leave room for response
            max_materials_length = 40000
            if len(course_materials) > max_materials_length:
                course_materials = course_materials[:max_materials_length] + "\n[... content truncated ...]"
            module_context += f"\n\n=== COURSE MATERIALS ===\n{course_materials}\n=== END MATERIALS ==="

        # Note: OpenAI response_format: json_object requires the response to be a JSON OBJECT
        # (not a bare array), so we wrap in {"questions": [...]}
        prompts = {
            "pt-br": f"""{module_context}

Gere {count} perguntas de múltipla escolha de nível {difficulty.value} baseadas nos materiais do curso.

REQUISITOS CRÍTICOS:
1. Cada pergunta deve ter 4-5 alternativas (A, B, C, D, E opcional)
2. EXPLICAÇÃO DETALHADA (2-4 frases) para TODAS as alternativas:
   - Por que a resposta CORRETA está certa (cite materiais do curso)
   - Por que as respostas ERRADAS estão erradas (erros conceituais comuns)
3. Perguntas claras e objetivas
4. Nível {difficulty.value}: {"conceitos básicos" if difficulty == QuizDifficulty.EASY else "aplicação prática" if difficulty == QuizDifficulty.MEDIUM else "análise crítica e síntese"}

Responda com JSON no formato:
{{"questions": [
  {{
    "question": "Texto da pergunta?",
    "options": {{
      "A": {{"text": "Alternativa A", "explanation": "Explicação detalhada de por que A está correta/errada"}},
      "B": {{"text": "Alternativa B", "explanation": "Explicação detalhada..."}},
      "C": {{"text": "Alternativa C", "explanation": "Explicação detalhada..."}},
      "D": {{"text": "Alternativa D", "explanation": "Explicação detalhada..."}}
    }},
    "correct_answer": "A",
    "concepts": ["conceito1", "conceito2"]
  }}
]}}

Gere EXATAMENTE {count} perguntas.""",

            "en": f"""{module_context}

Generate {count} {difficulty.value}-level multiple choice questions based on the course materials.

CRITICAL REQUIREMENTS:
1. Each question must have 4-5 options (A, B, C, D, E optional)
2. DETAILED EXPLANATION (2-4 sentences) for ALL options:
   - Why the CORRECT answer is right (cite course materials)
   - Why the WRONG answers are wrong (common conceptual errors)
3. Clear and objective questions
4. {difficulty.value} level: {"basic concepts" if difficulty == QuizDifficulty.EASY else "practical application" if difficulty == QuizDifficulty.MEDIUM else "critical analysis and synthesis"}

Respond with JSON in the format:
{{"questions": [
  {{
    "question": "Question text?",
    "options": {{
      "A": {{"text": "Option A", "explanation": "Detailed explanation of why A is correct/incorrect"}},
      "B": {{"text": "Option B", "explanation": "Detailed explanation..."}},
      "C": {{"text": "Option C", "explanation": "Detailed explanation..."}},
      "D": {{"text": "Option D", "explanation": "Detailed explanation..."}}
    }},
    "correct_answer": "A",
    "concepts": ["concept1", "concept2"]
  }}
]}}

Generate EXACTLY {count} questions.""",

            "es": f"""{module_context}

Genera {count} preguntas de opción múltiple de nivel {difficulty.value} basadas en los materiales del curso.

REQUISITOS CRÍTICOS:
1. Cada pregunta debe tener 4-5 opciones (A, B, C, D, E opcional)
2. EXPLICACIÓN DETALLADA (2-4 frases) para TODAS las opciones:
   - Por qué la respuesta CORRECTA es correcta (cita materiales del curso)
   - Por qué las respuestas INCORRECTAS son incorrectas (errores conceptuales comunes)
3. Preguntas claras y objetivas
4. Nivel {difficulty.value}: {"conceptos básicos" if difficulty == QuizDifficulty.EASY else "aplicación práctica" if difficulty == QuizDifficulty.MEDIUM else "análisis crítico y síntesis"}

Responde con JSON en el formato:
{{"questions": [
  {{
    "question": "Texto de la pregunta?",
    "options": {{
      "A": {{"text": "Opción A", "explanation": "Explicación detallada de por qué A es correcta/incorrecta"}},
      "B": {{"text": "Opción B", "explanation": "Explicación detallada..."}},
      "C": {{"text": "Opción C", "explanation": "Explicación detallada..."}},
      "D": {{"text": "Opción D", "explanation": "Explicación detallada..."}}
    }},
    "correct_answer": "A",
    "concepts": ["concepto1", "concepto2"]
  }}
]}}

Genera EXACTAMENTE {count} preguntas."""
        }

        return prompts.get(tutor_language, prompts["pt-br"])

    def _parse_ai_response(self, response_text: str) -> List[Dict[str, Any]]:
        """
        Parse AI response to extract quiz questions.

        Handles multiple formats:
        - {"questions": [...]}  (OpenAI json_object mode)
        - [...]                 (bare array from Anthropic)
        - ```json ... ```       (markdown-wrapped)

        Args:
            response_text: AI response (should be JSON)

        Returns:
            List of question dictionaries

        Raises:
            ValueError: If response cannot be parsed
        """
        import re

        try:
            # Strip markdown code blocks if present
            cleaned = response_text.strip()
            code_block_match = re.search(r'```(?:json)?\s*\n?(.*?)```', cleaned, re.DOTALL)
            if code_block_match:
                cleaned = code_block_match.group(1).strip()

            # Try parsing as JSON object first ({"questions": [...]})
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict) and "questions" in parsed:
                    questions = parsed["questions"]
                    if isinstance(questions, list):
                        logger.info(f"Parsed {len(questions)} questions from JSON object format")
                        return questions
                elif isinstance(parsed, list):
                    logger.info(f"Parsed {len(parsed)} questions from JSON array format")
                    return parsed
            except json.JSONDecodeError:
                pass

            # Fallback: find JSON array in response
            start_idx = cleaned.find("[")
            end_idx = cleaned.rfind("]") + 1

            if start_idx == -1 or end_idx <= 0:
                raise ValueError(f"No JSON found in response. Preview: {cleaned[:300]}")

            json_text = cleaned[start_idx:end_idx]
            questions = json.loads(json_text)

            if not isinstance(questions, list):
                raise ValueError("Extracted JSON is not an array")

            logger.info(f"Parsed {len(questions)} questions from extracted JSON array")
            return questions

        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing error: {e}")
            logger.error(f"Response preview: {response_text[:500]}")

            # Try to fix common JSON issues: trailing commas
            try:
                fixed = re.sub(r',\s*([}\]])', r'\1', cleaned)
                parsed = json.loads(fixed)
                if isinstance(parsed, dict) and "questions" in parsed:
                    return parsed["questions"]
                if isinstance(parsed, list):
                    logger.info(f"Parsed {len(parsed)} questions after fixing trailing commas")
                    return parsed
            except Exception:
                pass

            raise ValueError(f"Failed to parse AI response as JSON: {e}")

        except Exception as e:
            logger.error(f"Error parsing AI response: {e}")
            raise ValueError(f"Failed to parse AI response: {e}")
