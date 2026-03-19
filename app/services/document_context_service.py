"""
Document Context Service - Extract and prepare document context for AI prompts

Replaces OpenAI file_search and Anthropic Files API by:
1. Extracting text from PDF files locally
2. Chunking documents appropriately
3. Building context strings for AI prompts
"""
import logging
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

from app.models import Module, File
from app.services.blob_storage import get_blob_storage
from app.utils.pdf_extractor import get_pdf_extractor
from app.core.config import settings

logger = logging.getLogger(__name__)


class DocumentContextService:
    """
    Extracts and prepares document context from module files.

    Handles:
    - PDF text extraction (local, no API uploads)
    - Text file extraction
    - YouTube transcripts
    - Uses pre-computed AI summaries for large files
    """

    def __init__(self, module: Module, db: Optional[Session] = None):
        self.module = module
        self.db = db
        self.blob_storage = get_blob_storage()
        self.pdf_extractor = get_pdf_extractor(
            chunk_size=settings.PDF_CHUNK_SIZE if hasattr(settings, 'PDF_CHUNK_SIZE') else 4000,
            chunk_overlap=settings.PDF_CHUNK_OVERLAP if hasattr(settings, 'PDF_CHUNK_OVERLAP') else 200,
            max_chunks=settings.PDF_MAX_CHUNKS if hasattr(settings, 'PDF_MAX_CHUNKS') else 20
        )

    async def extract_file_content(self, file: File) -> Optional[str]:
        """
        Extract text content from a single file.
        Uses pre-extracted text from database if available (FAST).
        Falls back to on-demand extraction if needed (SLOW).
        """
        try:
            logger.info(f"Processing file: {file.name} (type={file.content_type}, source={file.source_type}, active={file.is_active}, blob_path={file.blob_path})")

            # FAST PATH: Use pre-extracted text from database
            if file.extracted_text:
                logger.info(f"Using pre-extracted text: {file.name} ({file.extracted_word_count} words)")
                return file.extracted_text

            # Handle YouTube transcripts
            if file.source_type == 'youtube':
                if file.transcript_text:
                    logger.info(f"Using YouTube transcript: {file.name}")
                    return file.transcript_text
                else:
                    logger.error(f"YouTube video has no transcript! Run transcription first: {file.name} (status={file.transcription_status})")
                    return None

            # Handle text files (small, can extract on-the-fly)
            if file.content_type and 'text' in file.content_type.lower():
                logger.info(f"Downloading text file: {file.name}")
                file_content = await self.blob_storage.get_file_content(file.blob_path)
                if file_content:
                    return file_content.decode('utf-8', errors='ignore')
                return None

            # Check if this is a PDF (by content_type OR file extension)
            is_pdf = (
                (file.content_type and 'pdf' in file.content_type.lower()) or
                (file.name and file.name.lower().endswith('.pdf')) or
                (file.file_type and file.file_type.lower() == 'pdf')
            )

            # SLOW PATH: Extract PDF on-demand (fallback only)
            if is_pdf:
                logger.warning(f"PDF not pre-extracted, extracting on-demand (SLOW): {file.name}")

                pdf_content = await self.blob_storage.get_file_content(file.blob_path)

                if not pdf_content:
                    logger.error(f"Failed to download PDF from blob storage: {file.name} (path={file.blob_path})")
                    return None

                logger.info(f"Downloaded PDF ({len(pdf_content)} bytes), extracting text...")

                extracted_text = self.pdf_extractor.extract_text_from_pdf(pdf_content, file.name)

                if not extracted_text:
                    logger.error(f"PDF extraction returned empty text: {file.name}")
                    return None

                logger.info(f"Extracted {len(extracted_text)} chars from PDF: {file.name}")

                # Save to database for next time (if we have db session)
                if self.db:
                    try:
                        file.extracted_text = extracted_text
                        file.extracted_at = __import__('datetime').datetime.utcnow()
                        file.extracted_word_count = len(extracted_text.split())
                        self.db.commit()
                        logger.info(f"Saved extracted text to database for future use")
                    except Exception as save_error:
                        logger.error(f"Failed to save extracted text: {save_error}")
                        self.db.rollback()

                return extracted_text

            # Unsupported file type
            logger.warning(f"Unsupported file type for {file.name}: content_type={file.content_type}, source_type={file.source_type}")
            return None

        except Exception as e:
            logger.error(f"Failed to extract content from {file.name}: {e}", exc_info=True)
            return None

    def _get_context_content(self, file: File, full_content: str) -> str:
        """
        Get the best content for context: use pre-computed summary for large files,
        or the full extracted text if it fits.
        """
        summary_threshold = getattr(settings, 'DOCUMENT_SUMMARY_THRESHOLD', 25000)

        if len(full_content) > summary_threshold and file.summarized_text:
            logger.info(f"Using pre-computed summary for '{file.name}' ({len(file.summarized_text)} chars instead of {len(full_content)})")
            return file.summarized_text

        return full_content

    async def get_all_documents_text(self, max_total_chars: int = 30000) -> str:
        """
        Extract text from all active module files and combine.
        Uses pre-computed AI summaries (from SummarizedText column) for large files.
        """
        if not self.module.files:
            logger.warning(f"Module {self.module.id} ({self.module.name}) has NO FILES attached!")
            return ""

        all_files = list(self.module.files)
        active_files = [f for f in all_files if f.is_active]

        logger.info(f"Module {self.module.id}: {len(all_files)} total files, {len(active_files)} active")

        if not active_files:
            inactive_names = [f.name for f in all_files]
            logger.warning(f"All {len(all_files)} files are inactive: {inactive_names}")
            return ""

        logger.info(f"Extracting context from {len(active_files)} active files: {[f.name for f in active_files]}")

        document_parts = []
        total_chars = 0

        for file in active_files:
            if total_chars >= max_total_chars:
                logger.warning(f"Reached max context size ({max_total_chars} chars), skipping remaining files")
                break

            # Extract content
            full_content = await self.extract_file_content(file)

            if not full_content:
                continue

            # Use summary if file is large and has one, otherwise use full content
            content = self._get_context_content(file, full_content)

            remaining_space = max_total_chars - total_chars

            # Truncate if content still exceeds remaining space
            if len(content) > remaining_space:
                logger.warning(f"Truncating '{file.name}' from {len(content)} to {remaining_space} chars")
                content = content[:remaining_space] + "\n[... content truncated ...]"

            document_parts.append(f"=== {file.name} ===\n{content}\n")
            total_chars += len(content)

            logger.info(f"Included {len(content)} chars from {file.name} (total: {total_chars})")

        if not document_parts:
            logger.warning("No content extracted from any files")
            return ""

        combined_context = "\n".join(document_parts)

        logger.info(f"Built document context: {len(combined_context)} chars from {len(document_parts)} files")

        return combined_context

    async def build_context_for_prompt(self, question: str = "", max_chars: int = 30000) -> Dict[str, Any]:
        """
        Build complete context for AI prompt.
        """
        context = await self.get_all_documents_text(max_total_chars=max_chars)

        active_files = [f.name for f in self.module.files if f.is_active] if self.module.files else []

        return {
            "context": context,
            "files_used": active_files,
            "total_chars": len(context)
        }


# Singleton instances (cached per module)
_document_context_cache: Dict[int, DocumentContextService] = {}


def get_document_context_service(module: Module, db: Optional[Session] = None) -> DocumentContextService:
    """Get or create DocumentContextService instance for a module."""
    if module.id not in _document_context_cache:
        _document_context_cache[module.id] = DocumentContextService(module, db)

    return _document_context_cache[module.id]
