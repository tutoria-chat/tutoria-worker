"""
PDF Text Extraction and Chunking

Extracts text from PDF files and chunks them for AI context windows.
Replaces dependency on provider file upload APIs (OpenAI file_search, Anthropic Files API).
"""
import logging
from typing import List, Dict, Any, Optional
import io
from pypdf import PdfReader

logger = logging.getLogger(__name__)


class PDFTextExtractor:
    """Extract and chunk text from PDF files."""

    def __init__(
        self,
        chunk_size: int = 4000,  # ~1000 tokens per chunk
        chunk_overlap: int = 200,  # Overlap to maintain context
        max_chunks: int = 20  # Limit total chunks to avoid token overflow
    ):
        """
        Initialize PDF text extractor.

        Args:
            chunk_size: Maximum characters per chunk
            chunk_overlap: Characters to overlap between chunks
            max_chunks: Maximum number of chunks to extract
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_chunks = max_chunks

    def extract_text_from_pdf(self, pdf_content: bytes, file_name: str = "document.pdf") -> Optional[str]:
        """
        Extract all text from a PDF file.

        Args:
            pdf_content: PDF file as bytes
            file_name: Name of the file (for logging)

        Returns:
            str: Extracted text or None if extraction fails
        """
        try:
            logger.info(f"📄 Extracting text from PDF: {file_name}")

            # Create PDF reader from bytes
            pdf_stream = io.BytesIO(pdf_content)
            reader = PdfReader(pdf_stream)

            # Extract text from all pages
            text_parts = []
            total_pages = len(reader.pages)

            logger.info(f"   Total pages: {total_pages}")

            for page_num, page in enumerate(reader.pages, start=1):
                try:
                    page_text = page.extract_text()
                    if page_text and page_text.strip():
                        # Add page separator for context
                        text_parts.append(f"--- Page {page_num} ---\n{page_text}\n")
                except Exception as page_error:
                    logger.warning(f"⚠️  Failed to extract text from page {page_num}: {page_error}")
                    continue

            # Combine all text
            full_text = "\n".join(text_parts)

            logger.info(f"✅ Extracted {len(full_text)} characters from {total_pages} pages")

            return full_text if full_text.strip() else None

        except Exception as e:
            logger.error(f"❌ Failed to extract text from PDF {file_name}: {e}")
            return None

    def chunk_text(self, text: str, metadata: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """
        Split text into overlapping chunks.

        Args:
            text: Text to chunk
            metadata: Optional metadata to include with each chunk (file name, source, etc.)

        Returns:
            List[Dict]: List of chunks with text and metadata
        """
        if not text or not text.strip():
            return []

        chunks = []
        text_length = len(text)
        start = 0
        chunk_index = 0

        logger.info(f"📦 Chunking text ({text_length} characters)")

        while start < text_length and chunk_index < self.max_chunks:
            # Calculate end position
            end = start + self.chunk_size

            # Extract chunk
            chunk_text = text[start:end]

            # Try to break at a sentence boundary (period, question mark, exclamation)
            if end < text_length:
                # Look for sentence ending within the last 200 characters
                last_period = chunk_text.rfind('. ', max(0, self.chunk_size - 200))
                last_question = chunk_text.rfind('? ', max(0, self.chunk_size - 200))
                last_exclamation = chunk_text.rfind('! ', max(0, self.chunk_size - 200))

                best_break = max(last_period, last_question, last_exclamation)

                if best_break > 0:
                    # Break at sentence boundary
                    chunk_text = chunk_text[:best_break + 2]  # Include the punctuation
                    end = start + best_break + 2

            # Create chunk object
            chunk = {
                "text": chunk_text.strip(),
                "chunk_index": chunk_index,
                "start_char": start,
                "end_char": end,
                "length": len(chunk_text)
            }

            # Add metadata if provided
            if metadata:
                chunk.update(metadata)

            chunks.append(chunk)

            # Move to next chunk (with overlap)
            start = end - self.chunk_overlap
            chunk_index += 1

        logger.info(f"✅ Created {len(chunks)} chunks (max: {self.max_chunks})")

        return chunks

    def extract_and_chunk_pdf(
        self,
        pdf_content: bytes,
        file_name: str = "document.pdf",
        metadata: Optional[Dict[str, str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Extract text from PDF and split into chunks.

        Args:
            pdf_content: PDF file as bytes
            file_name: Name of the file
            metadata: Optional metadata for chunks

        Returns:
            List[Dict]: List of text chunks with metadata
        """
        # Extract text
        full_text = self.extract_text_from_pdf(pdf_content, file_name)

        if not full_text:
            logger.warning(f"⚠️  No text extracted from {file_name}")
            return []

        # Add file name to metadata
        chunk_metadata = metadata or {}
        chunk_metadata["file_name"] = file_name

        # Chunk text
        chunks = self.chunk_text(full_text, chunk_metadata)

        return chunks


# Singleton instance
_pdf_extractor = None


def get_pdf_extractor(
    chunk_size: int = 4000,
    chunk_overlap: int = 200,
    max_chunks: int = 20
) -> PDFTextExtractor:
    """
    Get or create singleton PDFTextExtractor instance.

    Args:
        chunk_size: Maximum characters per chunk
        chunk_overlap: Characters to overlap between chunks
        max_chunks: Maximum number of chunks

    Returns:
        PDFTextExtractor: Singleton instance
    """
    global _pdf_extractor
    if _pdf_extractor is None:
        _pdf_extractor = PDFTextExtractor(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            max_chunks=max_chunks
        )
    return _pdf_extractor
