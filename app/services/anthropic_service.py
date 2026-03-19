import io
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
from anthropic import Anthropic, AsyncAnthropic
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Module, File
from app.services.blob_storage import get_blob_storage
from app.utils.token_counter import get_token_counter
from app.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)


class AnthropicService:
    """
    AI Service using Anthropic's Claude API with Files API (Beta).
    - Files are uploaded once using Files API and referenced by file_id
    - Files are downloaded from Azure Blob Storage and uploaded to Anthropic
    - Supports document context and conversation history
    - Uses beta header: anthropic-beta: files-api-2025-04-14
    """

    def __init__(self, module: Module, model_name: str = "claude-haiku-4-5", db: Optional[Session] = None, api_key: Optional[str] = None):
        self.module = module
        self.model_name = model_name
        self.db = db  # Database session for persisting file_id cache
        self.blob_storage = get_blob_storage()

        # Use provided API key or fall back to settings
        anthropic_key = api_key or settings.ANTHROPIC_API_KEY

        # Initialize Anthropic client
        if not anthropic_key:
            logger.error("ANTHROPIC_API_KEY not configured")
            raise ValueError("ANTHROPIC_API_KEY is required for AnthropicService")

        _beta_headers = {"anthropic-beta": "files-api-2025-04-14"}

        # Sync client — kept only for any legacy/internal callers that haven't
        # been migrated yet.  New code should use async_client.
        self.client = Anthropic(
            api_key=anthropic_key,
            default_headers=_beta_headers,
        )

        # Async client — used by all async def methods so the event loop is
        # never blocked while waiting for Anthropic's network response.
        self.async_client = AsyncAnthropic(
            api_key=anthropic_key,
            default_headers=_beta_headers,
        )
        logger.info(f"Initialized AnthropicService with model: {self.model_name}")
        logger.info(f"Files API enabled with beta header: files-api-2025-04-14")

    async def _upload_file_to_anthropic(self, file: File) -> Optional[str]:
        """
        Upload a file to Anthropic's Files API (with caching).

        For YouTube videos: Uses transcript_text from database (creates in-memory text file)
        For regular files: Downloads from Azure Blob Storage first, then uploads to Anthropic

        Returns the file_id.

        Uses beta header: anthropic-beta: files-api-2025-04-14
        """
        # Check if we already have this file in Anthropic
        if file.anthropic_file_id:
            try:
                # Verify the file still exists in Anthropic using retrieve_metadata
                logger.info(f"🔍 Verifying cached Anthropic file_id for {file.name}: {file.anthropic_file_id}")
                retrieved_file = await self.async_client.beta.files.retrieve_metadata(file.anthropic_file_id)
                logger.info(f"✅ Using cached Anthropic file_id for {file.name}: {file.anthropic_file_id}")
                logger.debug(f"   Filename: {retrieved_file.filename}, Size: {retrieved_file.size_bytes} bytes")
                return file.anthropic_file_id
            except Exception as e:
                logger.warning(f"⚠️ Cached file_id invalid for {file.name}, will re-upload: {e}")
                file.anthropic_file_id = None

                # Commit the NULL to database to avoid repeated failed retrievals
                if self.db:
                    try:
                        self.db.add(file)
                        self.db.flush()
                        self.db.commit()
                        logger.info(f"💾 Cleared invalid anthropic_file_id for: {file.name}")
                    except Exception as commit_error:
                        logger.error(f"❌ Failed to clear invalid file_id: {commit_error}")
                        self.db.rollback()

        try:
            # Check if this is a YouTube video with transcript
            if file.source_type == 'youtube' and file.transcript_text:
                logger.info(f"📺 Using YouTube transcript for {file.name}")
                logger.debug(f"   Transcript length: {len(file.transcript_text)} characters")

                # Create a text file from the transcript IN MEMORY
                file_content = file.transcript_text.encode('utf-8')
                file_stream = io.BytesIO(file_content)
                file_stream.seek(0)

                # Determine content type (text/plain for transcripts)
                content_type = "text/plain"
                file_name = f"{file.name}.txt"

                logger.info(f"✅ Created transcript text file: {len(file_content)} bytes")
            else:
                # Download file from Azure Blob Storage
                logger.info(f"📥 Downloading {file.name} from Azure Blob Storage...")
                file_content = await self.blob_storage.get_file_content(file.blob_path)

                if file_content is None:
                    logger.error(f"❌ Could not download file from Azure: {file.name}")
                    return None

                logger.info(f"✅ Downloaded {len(file_content)} bytes for {file.name}")

                # Create a file-like object for Anthropic
                file_stream = io.BytesIO(file_content)
                file_stream.seek(0)  # Ensure stream is at the beginning

                # Determine content type
                content_type = file.content_type or "application/octet-stream"
                file_name = file.file_name

            # Upload to Anthropic using Files API (Beta)
            logger.info(f"⬆️  Uploading {file_name} ({content_type}) to Anthropic Files API...")
            logger.debug(f"  File size: {len(file_content)} bytes")
            logger.debug(f"  Content-Type: {content_type}")

            anthropic_file = await self.async_client.beta.files.upload(
                file=(file_name, file_stream, content_type)
            )

            # Clean up the in-memory file stream and content
            file_stream.close()
            del file_content
            logger.debug(f"   🗑️ Cleaned up in-memory file stream and content")

            logger.info(f"✅ Successfully uploaded to Anthropic: {file.name}")
            logger.info(f"   File ID: {anthropic_file.id}")

            # Verify the upload by retrieving the file metadata
            try:
                retrieved = await self.async_client.beta.files.retrieve_metadata(anthropic_file.id)
                logger.info(f"✅ Verified file exists in Anthropic: {retrieved.filename} ({retrieved.size_bytes} bytes)")
            except Exception as verify_error:
                logger.warning(f"⚠️  Could not verify uploaded file: {verify_error}")

            return anthropic_file.id

        except Exception as e:
            logger.error(f"❌ Error uploading file {file.name} to Anthropic: {e}")
            logger.error(f"   Exception type: {type(e).__name__}")
            logger.error(f"   Exception details: {str(e)}")

            # Clean up file stream and content even on error
            try:
                if 'file_stream' in locals():
                    file_stream.close()
                if 'file_content' in locals():
                    del file_content
                logger.debug(f"   🗑️ Cleaned up file stream and content after error")
            except:
                pass

            return None

    async def _get_or_upload_module_files(self) -> List[Dict[str, Any]]:
        """
        Upload all active module files to Anthropic and return document blocks.
        Returns list of document blocks for use in messages.
        """
        try:
            active_files = [f for f in self.module.files if f.is_active]

            if not active_files:
                logger.warning(f"⚠️ No active files for module {self.module.id}")
                return []

            logger.info(f"📁 Processing {len(active_files)} active files for module {self.module.id}")

            # Debug: Log current state of file IDs
            for f in active_files:
                cached_status = f"CACHED: {f.anthropic_file_id[:20]}..." if f.anthropic_file_id else "NOT CACHED (NULL)"
                logger.info(f"  📄 File: {f.name} (id={f.id}) - {cached_status}")

            document_blocks = []

            for file in active_files:
                file_id = await self._upload_file_to_anthropic(file)
                if file_id:
                    # Update file record with cached file_id
                    file.anthropic_file_id = file_id

                    # Commit to database to persist the cached file_id
                    if self.db:
                        try:
                            # Ensure the file object is in the session
                            self.db.add(file)
                            # Flush to write changes immediately
                            self.db.flush()
                            # Commit the transaction
                            self.db.commit()
                            logger.info(f"  💾 COMMITTED anthropic_file_id={file_id[:20]}... for: {file.name} (file.id={file.id})")

                            # Verify the commit worked by refreshing from database
                            self.db.refresh(file)
                            if file.anthropic_file_id == file_id:
                                logger.info(f"  ✅ VERIFIED: File ID persisted correctly in database")
                            else:
                                logger.error(f"  ⚠️ WARNING: File ID NOT persisted! DB shows: {file.anthropic_file_id}")
                        except Exception as e:
                            logger.error(f"  ❌ Failed to commit anthropic_file_id for {file.name}: {e}")
                            self.db.rollback()

                    # Create document block for this file
                    document_blocks.append({
                        "type": "document",
                        "source": {
                            "type": "file",
                            "file_id": file_id
                        },
                        "title": file.name,
                        "citations": {"enabled": True}
                    })
                    logger.debug(f"  ✅ Added document block for: {file.name} (file_id: {file_id})")
                else:
                    logger.error(f"  ❌ Failed to upload file: {file.name}")

            logger.info(f"✅ Prepared {len(document_blocks)} document blocks for Anthropic")
            return document_blocks

        except Exception as e:
            logger.error(f"❌ Error preparing document blocks: {e}")
            return []

    async def answer_question(self, question: str) -> str:
        """
        Answer a question using Anthropic's Claude API with file context.
        """
        try:
            # Only upload/attach files via Files API if local PDF extraction is NOT being used.
            # When USE_LOCAL_PDF_EXTRACTION is enabled, document text is already in the system prompt.
            document_blocks = []
            if not settings.USE_LOCAL_PDF_EXTRACTION:
                document_blocks = await self._get_or_upload_module_files()

            # Build instructions with module's system prompt
            system_prompt = self.module.system_prompt or "Você é um assistente educacional útil e interativo."
            tutor_language = self.module.tutor_language or "pt-br"

            language_instructions = {
                "pt-br": "Sempre responda em português brasileiro (PT-BR).",
                "en": "Always respond in English.",
                "es": "Siempre responde en español."
            }

            instructions = f"""{system_prompt}

{language_instructions.get(tutor_language, language_instructions["pt-br"])}

IMPORTANTE: O contexto do curso e materiais didáticos serão fornecidos junto com as perguntas do aluno. Use esses materiais para responder com precisão.
"""

            # Build message content with documents and question
            message_content = []

            # Add document blocks first (if any)
            if document_blocks:
                message_content.extend(document_blocks)

            # Add user question as text block
            message_content.append({
                "type": "text",
                "text": question
            })

            # Call Claude API with documents (async — never blocks the event loop)
            response = await self.async_client.messages.create(
                model=self.model_name,
                max_tokens=2048,  # Optimized for scale (was 8192) - reduces token usage by 75%
                system=instructions,
                messages=[
                    {
                        "role": "user",
                        "content": message_content
                    }
                ]
            )

            # Check if response was truncated
            if response.stop_reason == "max_tokens":
                logger.warning(f"⚠️ Response was truncated due to max_tokens limit!")

            # Extract response text - concatenate ALL content blocks (for citations)
            if response.content and len(response.content) > 0:
                # When citations are enabled, response comes in multiple TextBlocks
                # We need to concatenate all of them
                response_text = ""
                for block in response.content:
                    if hasattr(block, 'text'):
                        response_text += block.text
                return response_text

            return "Desculpe, não consegui gerar uma resposta."

        except Exception as e:
            logger.error(f"Error in answer_question: {e}")
            return f"Desculpe, ocorreu um erro ao processar sua pergunta: {str(e)}"

    @retry_with_backoff(
        max_retries=3 if settings.ENABLE_RETRY_LOGIC else 1,
        base_delay=settings.RETRY_BASE_DELAY if hasattr(settings, 'RETRY_BASE_DELAY') else 1.0,
        max_delay=settings.RETRY_MAX_DELAY if hasattr(settings, 'RETRY_MAX_DELAY') else 10.0,
        exceptions=(Exception,)  # Catch all exceptions, but check for specific Anthropic errors
    )
    async def _call_anthropic_with_retry(self, model: str, max_tokens: int, system: str, messages: List[Dict]):
        """
        Call Anthropic Messages API with automatic retry on transient errors.

        Args:
            model: Model name (e.g., "claude-haiku-4-5")
            max_tokens: Maximum completion tokens
            system: System prompt
            messages: Conversation messages

        Returns:
            Response object from Anthropic

        Raises:
            Exception: Re-raises exception after max retries
        """
        try:
            response = await self.async_client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                timeout=settings.API_TIMEOUT_SECONDS if hasattr(settings, 'API_TIMEOUT_SECONDS') else 30
            )
            return response

        except Exception as e:
            # Log the specific error type for debugging
            logger.error(f"Anthropic API error: {type(e).__name__}: {str(e)[:200]}")
            raise

    async def answer_question_with_history_stream(self, messages: List[Dict[str, str]]):
        """
        Answer a question with streaming response.
        Yields chunks of the response as they're generated.

        Args:
            messages: List of conversation messages

        Yields:
            str: Response chunks
        """
        try:
            # Only upload/attach files if local PDF extraction is NOT being used
            document_blocks = []
            if not settings.USE_LOCAL_PDF_EXTRACTION:
                document_blocks = await self._get_or_upload_module_files()

            # Extract system prompt
            system_prompt = None
            conversation_messages = []

            for msg in messages:
                if msg["role"] == "system":
                    system_prompt = msg["content"]
                else:
                    conversation_messages.append(msg)

            # Use passed system prompt (includes document context from widget.py)
            if not system_prompt:
                system_prompt = self.module.system_prompt or "Você é um assistente educacional útil e interativo."

            tutor_language = self.module.tutor_language or "pt-br"

            course_context_instructions = {
                "pt-br": """
INSTRUÇÕES IMPORTANTES:
- O contexto do curso (COURSE MATERIALS) foi fornecido acima. Use APENAS esse conteúdo para responder.
- Responda SOMENTE sobre o conteúdo do curso. Se a pergunta não for sobre o curso, redirecione educadamente.
- Cite os materiais quando responder.
- Seja claro, educativo e encorajador.
- Se a informação não estiver nos materiais, diga claramente que não encontrou essa informação no conteúdo do curso.
""",
                "en": """
IMPORTANT INSTRUCTIONS:
- Course context (COURSE MATERIALS) was provided above. Use ONLY that content to answer.
- Answer ONLY about course content. If the question is not about the course, politely redirect.
- Cite materials when answering.
- Be clear, educational and encouraging.
- If information is not in the materials, clearly say you couldn't find it in the course content.
""",
                "es": """
INSTRUCCIONES IMPORTANTES:
- El contexto del curso (COURSE MATERIALS) fue proporcionado arriba. Use SOLO ese contenido para responder.
- Responda SOLO sobre el contenido del curso. Si la pregunta no es sobre el curso, redirija educadamente.
- Cite los materiales al responder.
- Sea claro, educativo y alentador.
- Si la información no está en los materiales, diga claramente que no la encontró en el contenido del curso.
"""
            }

            enhanced_system = f"""{system_prompt}

{course_context_instructions.get(tutor_language, course_context_instructions["pt-br"])}
"""

            # Convert messages to Anthropic format
            anthropic_messages = []
            last_user_index = None
            for i in range(len(conversation_messages) - 1, -1, -1):
                if conversation_messages[i]["role"] == "user":
                    last_user_index = i
                    break

            for i, msg in enumerate(conversation_messages):
                if i == last_user_index and msg["role"] == "user" and document_blocks:
                    content = document_blocks + [{"type": "text", "text": msg["content"]}]
                else:
                    content = msg["content"]

                anthropic_messages.append({
                    "role": msg["role"],
                    "content": content
                })

            # Stream the response (async — each chunk yields control back to the event loop)
            async with self.async_client.messages.stream(
                model=self.model_name,
                max_tokens=2048,
                system=enhanced_system,
                messages=anthropic_messages
            ) as stream:
                async for text in stream.text_stream:
                    yield text

        except Exception as e:
            logger.error(f"Anthropic streaming error: {e}")
            yield f"Error: {str(e)}"

    async def answer_question_with_history(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        Answer a question with conversation history context.
        Uses Claude Messages API with document context.

        Args:
            messages: List of {"role": "system|user|assistant", "content": "..."}
                      First message should be system prompt, followed by conversation history

        Returns:
            dict: {
                "response": str,
                "tokens": {
                    "input": int,
                    "output": int,
                    "total": int
                }
            }
        """
        try:
            logger.info(f"🤖 Anthropic answer_question_with_history called with {len(messages)} messages")

            # Count input tokens if token counting is enabled
            input_tokens = 0
            if settings.TOKEN_COUNTING_ENABLED:
                token_counter = get_token_counter()
                token_counts = token_counter.count_anthropic_tokens(messages)
                input_tokens = token_counts["prompt_tokens"]
                logger.info(f"📊 Input tokens (estimated): {input_tokens}")

            # Only upload/attach files via Files API if local PDF extraction is NOT being used.
            # When USE_LOCAL_PDF_EXTRACTION is enabled, document text is already injected into
            # the system prompt by the widget route — attaching files here would be redundant
            # and roughly DOUBLE the input token cost on every request.
            document_blocks = []
            if not settings.USE_LOCAL_PDF_EXTRACTION:
                document_blocks = await self._get_or_upload_module_files()
            else:
                logger.info("📝 Skipping file uploads — using local PDF extraction (text already in system prompt)")

            # Extract system prompt (first message should be system role)
            system_prompt = None
            conversation_messages = []

            for msg in messages:
                if msg["role"] == "system":
                    system_prompt = msg["content"]
                else:
                    conversation_messages.append(msg)

            # Enhance system prompt with module settings
            if not system_prompt:
                system_prompt = self.module.system_prompt or "Você é um assistente educacional útil e interativo."

            # Determine language from module settings
            tutor_language = self.module.tutor_language or "pt-br"
            language_instructions = {
                "pt-br": "IMPORTANTE: Sempre responda em português brasileiro (PT-BR). Use linguagem clara e educativa.",
                "en": "IMPORTANT: Always respond in English. Use clear and educational language.",
                "es": "IMPORTANTE: Siempre responde en español. Usa un lenguaje claro y educativo."
            }

            file_handling_instructions = {
                "pt-br": "NUNCA mencione os arquivos ou documentos que você tem acesso em suas respostas. Use apenas o conteúdo deles de forma natural, sem fazer referência aos arquivos em si.",
                "en": "NEVER mention the files or documents you have access to in your responses. Only use their content naturally, without referring to the files themselves.",
                "es": "NUNCA menciones los archivos o documentos a los que tienes acceso en tus respuestas. Solo usa su contenido de forma natural, sin hacer referencia a los archivos en sí."
            }

            dynamic_conversation_instructions = {
                "pt-br": """
CONVERSAÇÃO DINÂMICA E INTERATIVA:
- Após responder uma pergunta, se houver histórico de conversação, ofereça opções ao aluno:
  * "Gostaria de aprofundar mais neste tópico?"
  * "Quer fazer um quiz para testar seu conhecimento sobre o que acabamos de discutir?"
  * "Tem mais alguma dúvida sobre este assunto?"

QUANDO OFERECER QUIZ (SEMPRE SUGIRA EXPLICITAMENTE):
- Se o aluno perguntar como "praticar", "testar conhecimento", "estudar mais", "aprender melhor", "avaliar", "verificar se entendi"
- Se o aluno disser "quero praticar", "quero testar", "vamos praticar", etc.
- Após explicar um conceito importante
- IMPORTANTE: SEMPRE mencione o quiz como opção principal quando o aluno pedir recomendações de estudo/prática

MODO QUIZ:
- Quando o aluno pedir um quiz, aceitar fazer um, ou você sugerir, crie IMEDIATAMENTE 3-4 perguntas de múltipla escolha
- NÃO dê sugestões genéricas de estudo quando o aluno pedir para praticar - OFEREÇA O QUIZ DIRETAMENTE
- Formato das perguntas:
  * Pergunta clara e objetiva baseada nos materiais do curso
  * 4-5 alternativas (A, B, C, D, E)
  * Uma alternativa correta
  * Peça ao aluno para responder com a letra (A, B, C, D ou E)
- Após o aluno responder, corrija imediatamente:
  * Se CORRETO: Parabenize e explique por que está correto com base nos materiais
  * Se ERRADO: Indique a resposta correta (com a letra) e explique detalhadamente o conceito, usando os materiais do curso como referência
- Após cada pergunta corrigida, continue com a próxima pergunta do quiz automaticamente
""",
                "en": """
DYNAMIC AND INTERACTIVE CONVERSATION:
- After answering a question, if there's conversation history, offer options to the student:
  * "Would you like to dive deeper into this topic?"
  * "Want to take a quiz to test your knowledge about what we just discussed?"
  * "Do you have any other questions about this subject?"

WHEN TO OFFER QUIZ (ALWAYS SUGGEST EXPLICITLY):
- If student asks how to "practice", "test knowledge", "study more", "learn better", "evaluate", "verify understanding"
- If student says "I want to practice", "I want to test", "let's practice", etc.
- After explaining an important concept
- IMPORTANT: ALWAYS mention quiz as main option when student asks for study/practice recommendations

QUIZ MODE:
- When student requests a quiz, accepts one, or you suggest it, create IMMEDIATELY 3-4 multiple choice questions
- DO NOT give generic study suggestions when student asks to practice - OFFER THE QUIZ DIRECTLY
- Question format:
  * Clear and objective question based on course materials
  * 4-5 alternatives (A, B, C, D, E)
  * One correct alternative
  * Ask student to respond with the letter (A, B, C, D or E)
- After the student answers, correct immediately:
  * If CORRECT: Congratulate and explain why it's correct based on materials
  * If WRONG: Indicate the correct answer (with the letter) and explain the concept in detail, using course materials as reference
- After each corrected question, continue automatically with the next quiz question
""",
                "es": """
CONVERSACIÓN DINÁMICA E INTERACTIVA:
- Después de responder una pregunta, si hay historial de conversación, ofrece opciones al estudiante:
  * "¿Te gustaría profundizar más en este tema?"
  * "¿Quieres hacer un quiz para evaluar tu conocimiento sobre lo que acabamos de discutir?"
  * "¿Tienes alguna otra duda sobre este tema?"

CUÁNDO OFRECER QUIZ (SIEMPRE SUGERIR EXPLÍCITAMENTE):
- Si el estudiante pregunta cómo "practicar", "evaluar conocimiento", "estudiar más", "aprender mejor", "verificar si entendí"
- Si el estudiante dice "quiero practicar", "quiero evaluar", "vamos a practicar", etc.
- Después de explicar un concepto importante
- IMPORTANTE: SIEMPRE menciona el quiz como opción principal cuando el estudiante pida recomendaciones de estudio/práctica

MODO QUIZ:
- Cuando el estudiante pida un quiz, acepte hacer uno, o lo sugieras, crea INMEDIATAMENTE 3-4 preguntas de opción múltiple
- NO des sugerencias genéricas de estudio cuando el estudiante pida practicar - OFRECE EL QUIZ DIRECTAMENTE
- Formato de las preguntas:
  * Pregunta clara y objetiva basada en los materiales del curso
  * 4-5 alternativas (A, B, C, D, E)
  * Una alternativa correcta
  * Pide al estudiante que responda con la letra (A, B, C, D o E)
- Después de que el estudiante responda, corrige inmediatamente:
  * Si CORRECTO: Felicita y explica por qué está correcto con base en los materiales
  * Si INCORRECTO: Indica la respuesta correcta (con la letra) y explica detalladamente el concepto, usando los materiales del curso como referencia
- Después de cada pregunta corregida, continúa automáticamente con la siguiente pregunta del quiz
"""
            }

            enhanced_system = f"""{system_prompt}

{language_instructions.get(tutor_language, language_instructions["pt-br"])}

{file_handling_instructions.get(tutor_language, file_handling_instructions["pt-br"])}

{dynamic_conversation_instructions.get(tutor_language, dynamic_conversation_instructions["pt-br"])}

ACESSO AOS MATERIAIS:
Você tem acesso aos materiais do curso através dos documentos fornecidos. Use-os SEMPRE para responder às perguntas com precisão.

REGRAS IMPORTANTES:
- Sempre cite os materiais do curso quando responder
- Quando não souber a resposta ou a informação não estiver disponível nos materiais, indique isso claramente
- Seja encorajador e paciente com os alunos
- Adapte o nível de complexidade às respostas do aluno
"""

            # Convert conversation messages to Anthropic format
            anthropic_messages = []

            # Find the index of the LAST user message (the current question)
            last_user_index = None
            for i in range(len(conversation_messages) - 1, -1, -1):
                if conversation_messages[i]["role"] == "user":
                    last_user_index = i
                    break

            for i, msg in enumerate(conversation_messages):
                # Add document blocks to the LAST (current) user message, not the first
                if i == last_user_index and msg["role"] == "user" and document_blocks:
                    content = document_blocks + [{"type": "text", "text": msg["content"]}]
                    logger.info(f"📎 Adding {len(document_blocks)} document blocks to current message")
                else:
                    content = msg["content"]

                anthropic_messages.append({
                    "role": msg["role"],
                    "content": content
                })

            # Call Claude API with conversation history (with retry logic if enabled)
            response = await self._call_anthropic_with_retry(
                model=self.model_name,
                max_tokens=2048,  # Optimized for scale (was 8192) - reduces token usage by 75%
                system=enhanced_system,
                messages=anthropic_messages
            )

            # Extract token usage from response (Anthropic provides this)
            output_tokens = 0
            total_tokens = 0
            if hasattr(response, 'usage') and response.usage:
                input_tokens = response.usage.input_tokens if hasattr(response.usage, 'input_tokens') else input_tokens
                output_tokens = response.usage.output_tokens if hasattr(response.usage, 'output_tokens') else 0
                total_tokens = input_tokens + output_tokens
                logger.info(f"📊 Total tokens: {total_tokens} (in: {input_tokens}, out: {output_tokens})")

            # Check if response was truncated
            if response.stop_reason == "max_tokens":
                logger.warning(f"⚠️ Response was truncated due to max_tokens limit!")

            # Extract response text - concatenate ALL content blocks (for citations)
            if response.content and len(response.content) > 0:
                # When citations are enabled, response comes in multiple TextBlocks
                # We need to concatenate all of them
                response_text = ""
                for block in response.content:
                    if hasattr(block, 'text'):
                        response_text += block.text

                return {
                    "response": response_text,
                    "tokens": {
                        "input": input_tokens,
                        "output": output_tokens,
                        "total": total_tokens
                    }
                }

            return {
                "response": "Desculpe, não consegui gerar uma resposta.",
                "tokens": {"input": input_tokens, "output": output_tokens, "total": total_tokens}
            }

        except Exception as e:
            logger.error(f"Error in answer_question_with_history: {e}")
            return {
                "response": f"Desculpe, ocorreu um erro ao processar sua pergunta: {str(e)}",
                "tokens": {"input": input_tokens, "output": 0, "total": input_tokens}
            }

    async def sync_module_files(self) -> bool:
        """
        Force upload/re-upload of all module files to Anthropic.
        Useful when files are updated or added.
        """
        try:
            document_blocks = await self._get_or_upload_module_files()

            if document_blocks:
                logger.info(f"Successfully synced {len(document_blocks)} files for module {self.module.id}")
                return True
            else:
                logger.warning(f"No files synced for module {self.module.id}")
                return False

        except Exception as e:
            logger.error(f"Error syncing module files: {e}")
            return False
