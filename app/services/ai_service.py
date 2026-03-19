import io
import logging
import re
from typing import List, Optional, Dict, Any
from openai import OpenAI, AsyncOpenAI
import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Module, File
from app.services.blob_storage import get_blob_storage
from app.utils.token_counter import get_token_counter
from app.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)


# Default model — gpt-5.4-nano (launched 2026-03-17)
DEFAULT_OPENAI_MODEL = "gpt-5.4-nano"


def _clean_citation_markers(text: str) -> str:
    """
    Remove OpenAI citation markers from response text.

    OpenAI's file_search tool adds citations like 【10:1†source】 to indicate
    where information came from. We remove these for cleaner user-facing responses.
    """
    pattern = r'【\d+:\d+†source】'
    cleaned = re.sub(pattern, '', text)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned)
    return cleaned.strip()


class AIService:
    """
    AI Service using OpenAI's Responses API with file_search tool.

    - Uses the Responses API (replaces the legacy Assistants API)
    - Vector stores are still created/managed the same way
    - No more per-request thread/assistant overhead
    - Files are cached in OpenAI after first upload
    """

    def __init__(self, module: Module, db: Optional['Session'] = None, api_key: Optional[str] = None):
        self.module = module
        self.db = db
        self.blob_storage = get_blob_storage()

        self.provider = "openai"
        self.model_name = DEFAULT_OPENAI_MODEL

        if module.ai_model and module.ai_model.model_name:
            self.model_name = module.ai_model.model_name
            logger.info(f"Using DB model: {self.model_name} (provider: {module.ai_model.provider})")

        openai_key = api_key or settings.OPENAI_API_KEY
        self.client = OpenAI(api_key=openai_key)
        self.async_client = AsyncOpenAI(api_key=openai_key)

    # ------------------------------------------------------------------
    # File / Vector Store helpers (unchanged structure, fully async)
    # ------------------------------------------------------------------

    async def _upload_file_to_openai(self, file: File) -> Optional[str]:
        """
        Upload a file to OpenAI's Files API (with caching).

        Content priority (cheapest first, avoids unnecessary S3 downloads):
          1. YouTube videos    → transcript_text (stored at transcription time)
          2. Large documents   → summarized_text (AI summary stored at extraction time)
          3. Normal documents  → extracted_text  (plain text stored at extraction time)
          4. Fallback          → raw file bytes from S3 (only if extraction never ran)

        Returns the OpenAI file_id.
        """
        if file.openai_file_id:
            try:
                logger.info(f"🔍 Verifying cached OpenAI file_id for {file.name}: {file.openai_file_id}")
                retrieved_file = await self.async_client.files.retrieve(file.openai_file_id)
                logger.info(f"✅ Using cached OpenAI file_id for {file.name}: {file.openai_file_id}")
                logger.debug(f"   File status: {retrieved_file.status}, bytes: {retrieved_file.bytes}")
                return file.openai_file_id
            except Exception as e:
                logger.warning(f"⚠️ Cached file_id invalid for {file.name}, will re-upload: {e}")
                file.openai_file_id = None
                if self.db:
                    try:
                        self.db.add(file)
                        self.db.flush()
                        self.db.commit()
                    except Exception as commit_error:
                        logger.error(f"❌ Failed to clear invalid file_id: {commit_error}")
                        self.db.rollback()

        try:
            if file.source_type == 'youtube' and file.transcript_text:
                # YouTube: use stored transcript text directly
                logger.info(f"📺 Using YouTube transcript for {file.name}")
                file_content = file.transcript_text.encode('utf-8')
                file_stream = io.BytesIO(file_content)
                file_stream.name = f"{file.name}.txt"

            elif file.summarized_text:
                # Large document: use the AI-generated summary stored during extraction.
                # This avoids uploading the raw PDF and saves both bandwidth and OpenAI tokens.
                logger.info(
                    f"📝 Using stored summary for {file.name} "
                    f"({len(file.summarized_text)} chars) — skipping S3 download"
                )
                file_content = file.summarized_text.encode('utf-8')
                file_stream = io.BytesIO(file_content)
                file_stream.name = f"{file.name}.txt"

            elif file.extracted_text:
                # Normal document: use pre-extracted plain text stored during processing.
                # Much cheaper than re-uploading the raw binary every session.
                logger.info(
                    f"📄 Using extracted text for {file.name} "
                    f"({len(file.extracted_text)} chars) — skipping S3 download"
                )
                file_content = file.extracted_text.encode('utf-8')
                file_stream = io.BytesIO(file_content)
                file_stream.name = f"{file.name}.txt"

            else:
                # Fallback: file hasn't been processed yet — download raw from S3.
                # This should only happen for files that were uploaded before the
                # extraction pipeline ran, or if extraction failed.
                logger.warning(
                    f"⚠️  No extracted text for {file.name} (processing_status={file.processing_status!r}), "
                    f"falling back to raw S3 download"
                )
                file_content = await self.blob_storage.get_file_content(file.blob_path)
                if file_content is None:
                    logger.error(f"❌ Could not download file from S3: {file.name}")
                    return None
                logger.info(f"✅ Downloaded {len(file_content)} bytes for {file.name}")
                file_stream = io.BytesIO(file_content)
                file_stream.name = file.file_name

            logger.info(f"⬆️  Uploading {file_stream.name} to OpenAI Files API...")
            openai_file = await self.async_client.files.create(
                file=file_stream,
                purpose="assistants"
            )
            file_stream.close()
            del file_content

            logger.info(f"✅ Uploaded to OpenAI: {file.name} → {openai_file.id}")
            return openai_file.id

        except Exception as e:
            logger.error(f"❌ Error uploading {file.name} to OpenAI: {type(e).__name__}: {e}")
            try:
                if 'file_stream' in locals():
                    file_stream.close()
                if 'file_content' in locals():
                    del file_content
            except Exception:
                pass
            return None

    def _vector_stores_api(self):
        """
        Return the vector stores API object, compatible across openai SDK versions.

        In openai<1.62, vector stores lived under client.beta.vector_stores.
        In openai>=1.62 they were promoted to stable: client.vector_stores.
        Try the stable path first; fall back to beta for older installs.
        """
        if hasattr(self.async_client, 'vector_stores'):
            return self.async_client.vector_stores
        # Legacy beta path (openai < 1.62)
        return self.async_client.beta.vector_stores

    async def _get_or_create_vector_store(self) -> Optional[str]:
        """Get or create a Vector Store for this module. Returns the vector_store_id."""
        vs_api = self._vector_stores_api()

        if self.module.openai_vector_store_id:
            try:
                logger.info(f"🔍 Verifying vector store: {self.module.openai_vector_store_id}")
                vs = await vs_api.retrieve(self.module.openai_vector_store_id)
                logger.info(f"✅ Using existing vector store: {vs.id}")
                return vs.id
            except Exception as e:
                logger.warning(f"⚠️ Vector store invalid, creating new one: {e}")
                self.module.openai_vector_store_id = None
                if self.db:
                    try:
                        self.db.commit()
                    except Exception as commit_error:
                        logger.error(f"Failed to clear invalid vector store ID: {commit_error}")
                        self.db.rollback()

        try:
            logger.info(f"🗂️  Creating new vector store for module {self.module.id}: {self.module.name}")
            vs = await vs_api.create(
                name=f"Module {self.module.id}: {self.module.name}",
                expires_after={"anchor": "last_active_at", "days": 365}
            )
            logger.info(f"✅ Created vector store: {vs.id}")
            return vs.id
        except Exception as e:
            logger.error(f"❌ Error creating vector store: {e}")
            return None

    async def _sync_files_to_vector_store(self, vector_store_id: str) -> bool:
        """Upload all active module files to the vector store. Returns True if successful."""
        try:
            active_files = [f for f in self.module.files if f.is_active]
            if not active_files:
                logger.warning(f"No active files for module {self.module.id}")
                return True

            file_ids = []
            for file in active_files:
                file_id = await self._upload_file_to_openai(file)
                if file_id:
                    file_ids.append(file_id)
                    file.openai_file_id = file_id

            if not file_ids:
                logger.error("No files could be uploaded to OpenAI")
                return False

            await self._vector_stores_api().file_batches.create(
                vector_store_id=vector_store_id,
                file_ids=file_ids
            )

            if self.db:
                try:
                    for file in active_files:
                        self.db.add(file)
                    self.db.flush()
                    self.db.commit()
                    logger.info(f"💾 Committed {len(file_ids)} openai_file_id(s) to database")
                except Exception as e:
                    logger.error(f"Failed to commit openai_file_ids: {e}")
                    self.db.rollback()

            logger.info(f"✅ Synced {len(file_ids)} files to vector store")
            return True

        except Exception as e:
            logger.error(f"Error syncing files to vector store: {e}")
            return False

    async def _ensure_vector_store_ready(self) -> Optional[str]:
        """
        Get (or create) the vector store and populate it with files if it's new.
        Returns the vector_store_id, or None if unavailable.
        """
        had_store = bool(self.module.openai_vector_store_id)
        vector_store_id = await self._get_or_create_vector_store()
        if not vector_store_id:
            return None

        # Only sync files when the store was just created
        if not had_store:
            await self._sync_files_to_vector_store(vector_store_id)
            self.module.openai_vector_store_id = vector_store_id
            if self.db:
                try:
                    self.db.commit()
                    logger.info(f"💾 Saved openai_vector_store_id: {vector_store_id}")
                except Exception as e:
                    logger.error(f"Failed to save vector_store_id: {e}")
                    self.db.rollback()

        return vector_store_id

    # ------------------------------------------------------------------
    # Responses API helpers
    # ------------------------------------------------------------------

    def _build_request_kwargs(
        self,
        messages: List[Dict[str, str]],
        vector_store_id: Optional[str] = None,
        max_output_tokens: int = 2048,
    ) -> Dict[str, Any]:
        """
        Build kwargs for responses.create() / responses.stream().

        Key Responses API rules (verified against docs):
        - System prompts go in `instructions=`, NOT as {"role": "system"} in `input`
        - `input` accepts only "user" and "assistant" roles
        - file_search tool: {"type": "file_search", "vector_store_ids": [...]}
        """
        instructions: Optional[str] = None
        input_messages: List[Dict[str, str]] = []

        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "system":
                # Responses API uses top-level `instructions` for system prompts
                instructions = content
            elif role in ("user", "assistant"):
                input_messages.append({"role": role, "content": content})

        kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "input": input_messages,
            "max_output_tokens": max_output_tokens,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if vector_store_id:
            kwargs["tools"] = [{"type": "file_search", "vector_store_ids": [vector_store_id]}]

        return kwargs

    # ------------------------------------------------------------------
    # Responses API — main chat methods
    # ------------------------------------------------------------------

    async def answer_question(self, question: str) -> str:
        """Answer a single question (no history) using the Responses API."""
        try:
            result = await self.answer_question_with_history([
                {"role": "user", "content": question}
            ])
            return result.get("response", "Desculpe, não consegui gerar uma resposta.")
        except Exception as e:
            logger.error(f"Error in answer_question: {e}")
            return f"Desculpe, ocorreu um erro ao processar sua pergunta: {str(e)}"

    async def answer_question_with_history(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        Answer with conversation history using the Responses API.

        Returns:
            {"response": str, "tokens": {"input": int, "output": int, "total": int}}
        """
        input_tokens = 0
        try:
            if settings.TOKEN_COUNTING_ENABLED:
                token_counter = get_token_counter()
                counts = token_counter.count_openai_tokens(messages)
                input_tokens = counts["prompt_tokens"]
                logger.info(f"📊 Estimated input tokens: {input_tokens}")

            vector_store_id = await self._ensure_vector_store_ready()
            kwargs = self._build_request_kwargs(messages, vector_store_id=vector_store_id)
            response = await self.async_client.responses.create(**kwargs)

            text = response.output_text or ""
            cleaned = _clean_citation_markers(text)

            output_tokens = response.usage.output_tokens if response.usage else 0
            total_tokens = response.usage.total_tokens if response.usage else (input_tokens + output_tokens)
            logger.info(f"📊 Tokens — in: {input_tokens}, out: {output_tokens}, total: {total_tokens}")
            logger.info("✅ Generated response via Responses API")

            return {
                "response": cleaned,
                "tokens": {"input": input_tokens, "output": output_tokens, "total": total_tokens}
            }

        except Exception as e:
            logger.error(f"Error in answer_question_with_history: {e}")
            return {
                "response": f"Desculpe, ocorreu um erro ao processar sua pergunta: {str(e)}",
                "tokens": {"input": input_tokens, "output": 0, "total": input_tokens}
            }

    async def answer_question_with_history_stream(self, messages: List[Dict[str, str]]):
        """
        Streaming response using the Responses API.

        Yields text chunks as they arrive.
        """
        try:
            if settings.TOKEN_COUNTING_ENABLED:
                token_counter = get_token_counter()
                token_counter.count_openai_tokens(messages)

            vector_store_id = await self._ensure_vector_store_ready()
            kwargs = self._build_request_kwargs(messages, vector_store_id=vector_store_id)

            # Use create(stream=True) for async — documented async streaming pattern.
            # responses.stream() context manager is sync-only in the SDK helpers.
            stream = await self.async_client.responses.create(**kwargs, stream=True)
            async for event in stream:
                if event.type == "response.output_text.delta":
                    cleaned = _clean_citation_markers(event.delta)
                    if cleaned:
                        yield cleaned

        except Exception as e:
            logger.error(f"Streaming error: {e}")
            yield f"Error: {str(e)}"

    # ------------------------------------------------------------------
    # Data analysis
    # ------------------------------------------------------------------

    async def analyze_data(self, question: str, dataframe: pd.DataFrame, filename: str) -> str:
        """Analyze uploaded data file and answer question about it."""
        try:
            data_summary = f"""
Arquivo: {filename}
Formato: {dataframe.shape}
Colunas: {list(dataframe.columns)}
Tipos de dados: {dataframe.dtypes.to_dict()}

Primeiras 10 linhas:
{dataframe.head(10).to_string()}

Estatísticas resumidas:
{dataframe.describe().to_string()}
"""
            enhanced_question = f"""
O usuário enviou um arquivo de dados chamado "{filename}" e tem a seguinte pergunta:

{question}

Aqui estão os dados do arquivo:

{data_summary}

Por favor, analise os dados e responda à pergunta do usuário. Se relevante, conecte a análise com os conceitos dos materiais do curso.
"""
            return await self.answer_question(enhanced_question)

        except Exception as e:
            logger.error(f"Error analyzing data: {e}")
            return f"Erro ao analisar dados: {str(e)}"

    # ------------------------------------------------------------------
    # File sync
    # ------------------------------------------------------------------

    async def sync_module_files(self):
        """Force re-sync of all module files to the OpenAI vector store."""
        try:
            if not self.module.openai_vector_store_id:
                logger.info("No vector store exists yet, will be created on next question")
                return True

            success = await self._sync_files_to_vector_store(
                self.module.openai_vector_store_id
            )
            if success:
                logger.info(f"✅ Re-synced files for module {self.module.id}")
            return success

        except Exception as e:
            logger.error(f"Error syncing module files: {e}")
            return False
