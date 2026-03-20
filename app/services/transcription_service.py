"""
Transcription service

Orchestrates YouTube video transcription and integrates with database and AI services
"""

import logging
import io
from datetime import datetime
from typing import Optional, Dict
from sqlalchemy.orm import Session

from app.models.file import File
from app.models.module import Module
from app.services.youtube_service import YouTubeService
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)


class TranscriptionService:
    """
    Service for managing video transcriptions

    Handles:
    - YouTube video processing
    - Database persistence
    - OpenAI vector store integration
    """

    def __init__(self, db: Session, openai_client=None):
        """
        Initialize transcription service

        Args:
            db: Database session
            openai_client: OpenAI client instance
        """
        self.db = db
        self.openai_client = openai_client
        self.youtube_service = YouTubeService(openai_client)

    def transcribe_existing_file(self, file_id: int, youtube_url: str, language: str = 'pt-br') -> Dict[str, any]:
        """
        Transcribe a YouTube video for an EXISTING file record (called by background job)

        This method does NOT check for duplicates or create files - it assumes the file already exists.

        Args:
            file_id: ID of the existing File record
            youtube_url: YouTube video URL
            language: Target language code

        Returns:
            dict with transcription result
        """
        from datetime import datetime

        # Get existing file
        file = self.db.query(File).filter(File.id == file_id).first()
        if not file:
            raise ValueError(f"File {file_id} not found")

        # Extract video ID
        video_id = self.youtube_service.extract_video_id(youtube_url)
        if not video_id:
            raise ValueError(f"Invalid YouTube URL: {youtube_url}")

        # Update status to processing
        file.transcription_status = 'processing'
        self.db.commit()

        logger.info(f"Starting transcription for file {file_id}, video {video_id}")

        # Get transcript using YouTube service
        result = self.youtube_service.process_youtube_video(youtube_url, language)

        if not result or not result.get('text'):
            file.transcription_status = 'failed'
            self.db.commit()
            raise ValueError("Failed to get transcript from YouTube")

        # Update file with transcript
        file.transcript_text = result['text']
        file.transcript_language = language
        file.transcript_word_count = result['word_count']
        file.video_duration_seconds = result.get('duration_seconds')
        file.transcription_cost_usd = result.get('cost_usd')
        file.transcription_status = 'completed'
        file.transcripted_at = datetime.utcnow()
        self.db.commit()

        logger.info(f"Transcription completed for file {file_id}: {file.transcript_word_count} words")

        return {
            'file_id': file.id,
            'status': 'completed',
            'transcript': result['text'],
            'word_count': result['word_count'],
            'duration_seconds': result.get('duration_seconds'),
            'source': result['source'],
            'language': language,
            'cost_usd': result.get('cost_usd', 0.0)
        }

    def process_youtube_url(
        self,
        youtube_url: str,
        module_id: int,
        language: str = 'pt-br',
        custom_name: Optional[str] = None
    ) -> Dict[str, any]:
        """
        Process YouTube URL and create transcript file

        Flow:
        1. Extract video ID
        2. Get transcript (free or Whisper)
        3. Save to database
        4. Add to OpenAI vector store
        5. Return results

        Args:
            youtube_url: YouTube video URL
            module_id: Module to attach transcript to
            language: Transcript language
            custom_name: Optional custom name for the file

        Returns:
            Dict with results including file_id, cost, word_count, etc.

        Raises:
            ValueError: If URL is invalid or module not found
            Exception: If transcription fails
        """
        # Validate module exists
        module = self.db.query(Module).filter(Module.id == module_id).first()
        if not module:
            raise ValueError(f"Module {module_id} not found")

        # Extract video ID
        video_id = self.youtube_service.extract_video_id(youtube_url)
        if not video_id:
            raise ValueError(f"Invalid YouTube URL: {youtube_url}")

        # Check if this video already exists for this module (by video ID, not exact URL)
        # This handles different URL formats for the same video (e.g., with/without query params)
        all_youtube_files = self.db.query(File).filter(
            File.module_id == module_id,
            File.source_type == 'youtube',
            File.is_active == True
        ).all()

        existing = None
        for file in all_youtube_files:
            if file.source_url:
                existing_video_id = self.youtube_service.extract_video_id(file.source_url)
                if existing_video_id == video_id:
                    existing = file
                    break

        if existing:
            logger.warning(f"YouTube video {video_id} already exists for module {module_id}")
            return {
                'file_id': existing.id,
                'status': 'already_exists',
                'transcript': existing.transcript_text,
                'word_count': existing.transcript_word_count,
                'cost_usd': 0.0
            }

        # Get video metadata (title for better naming)
        metadata = self.youtube_service.get_video_metadata(youtube_url)
        video_title = metadata.get('title') if metadata else f"YouTube {video_id}"

        # Create file record with pending status
        file_name = custom_name or f"{video_title}"
        file_record = File(
            name=file_name,
            file_type='youtube_transcript',
            file_name=f"{video_id}.txt",
            source_type='youtube',
            source_url=youtube_url,
            module_id=module_id,
            transcription_status='processing',
            transcript_language=language,
            is_active=True
        )

        self.db.add(file_record)
        self.db.commit()
        self.db.refresh(file_record)

        logger.info(f"Created file record {file_record.id} for YouTube video {video_id}")

        try:
            # Process YouTube video (get transcript)
            logger.info(f"Processing YouTube video {video_id}...")
            result = self.youtube_service.process_youtube_video(youtube_url, language)

            if not result:
                raise Exception("Failed to get transcript from YouTube")

            # Update file record with transcript
            file_record.transcript_text = result['text']
            file_record.transcription_status = 'completed'
            file_record.transcripted_at = datetime.utcnow()
            file_record.video_duration_seconds = result.get('duration_seconds')
            file_record.transcript_word_count = result.get('word_count')
            file_record.transcript_language = result.get('language', language)
            file_record.transcription_cost_usd = result.get('cost_usd')

            self.db.commit()

            logger.info(
                f"✅ Transcript saved for {video_id}: "
                f"{result['word_count']} words, "
                f"source: {result['source']}, "
                f"cost: ${result.get('cost_usd', 0):.4f}"
            )

            # Add to OpenAI vector store for RAG
            try:
                self._add_transcript_to_vector_store(file_record, module)
                logger.info(f"Added transcript to vector store for module {module_id}")
            except Exception as e:
                logger.error(f"Failed to add transcript to vector store: {e}")
                # Don't fail the whole operation if vector store fails
                # Transcript is still accessible in database

            return {
                'file_id': file_record.id,
                'status': 'completed',
                'transcript': result['text'],
                'word_count': result['word_count'],
                'duration_seconds': result.get('duration_seconds'),
                'source': result['source'],
                'cost_usd': result.get('cost_usd', 0),
                'language': result.get('language')
            }

        except Exception as e:
            # Mark as failed
            file_record.transcription_status = 'failed'
            self.db.commit()

            logger.error(f"Transcription failed for {video_id}: {e}")
            raise

    def _add_transcript_to_vector_store(self, file_record: File, module: Module):
        """
        Add transcript to OpenAI vector store for RAG

        Args:
            file_record: File record with transcript
            module: Module to add to
        """
        if not self.openai_client:
            logger.warning("OpenAI client not initialized, skipping vector store")
            return

        if not file_record.transcript_text:
            logger.warning(f"No transcript text for file {file_record.id}")
            return

        # Create AIService instance for this module
        ai_service = AIService(module, self.db, self.openai_client)

        # Get or create vector store for module
        vector_store_id = ai_service._get_or_create_vector_store()
        if not vector_store_id:
            logger.error(f"Could not get/create vector store for module {module.id}")
            return

        # Create text file from transcript
        transcript_bytes = file_record.transcript_text.encode('utf-8')
        transcript_file = io.BytesIO(transcript_bytes)
        transcript_file.name = f"youtube_{file_record.id}_transcript.txt"

        # Upload to OpenAI
        try:
            openai_file = self.openai_client.files.create(
                file=transcript_file,
                purpose="assistants"
            )

            # Cache OpenAI file ID
            file_record.openai_file_id = openai_file.id
            self.db.commit()

            logger.info(f"Uploaded transcript to OpenAI: {openai_file.id}")

            # Add to vector store
            self.openai_client.beta.vector_stores.files.create(
                vector_store_id=vector_store_id,
                file_id=openai_file.id
            )

            logger.info(
                f"Added transcript to vector store {vector_store_id} "
                f"for module {module.id}"
            )

        except Exception as e:
            logger.error(f"Error adding transcript to vector store: {e}")
            raise

    def get_transcription_status(self, file_id: int) -> Optional[Dict[str, any]]:
        """
        Get status of a transcription

        Args:
            file_id: File ID to check

        Returns:
            Dict with status information or None if not found
        """
        file_record = self.db.query(File).filter(File.id == file_id).first()
        if not file_record:
            return None

        return {
            'file_id': file_record.id,
            'name': file_record.name,
            'status': file_record.transcription_status,
            'word_count': file_record.transcript_word_count,
            'duration_seconds': file_record.video_duration_seconds,
            'language': file_record.transcript_language,
            'source_url': file_record.source_url,
            'source_type': file_record.source_type,
            'completed_at': file_record.transcripted_at.isoformat() if file_record.transcripted_at else None,
            'has_transcript': bool(file_record.transcript_text)
        }

    def retry_failed_transcription(self, file_id: int) -> Dict[str, any]:
        """
        Retry a failed transcription

        Args:
            file_id: File ID to retry

        Returns:
            Dict with results

        Raises:
            ValueError: If file not found or not in failed state
        """
        file_record = self.db.query(File).filter(File.id == file_id).first()
        if not file_record:
            raise ValueError(f"File {file_id} not found")

        if file_record.transcription_status != 'failed':
            raise ValueError(f"File {file_id} is not in failed state")

        if file_record.source_type != 'youtube':
            raise ValueError(f"File {file_id} is not a YouTube video")

        logger.info(f"Retrying transcription for file {file_id}")

        # Reset status
        file_record.transcription_status = 'processing'
        self.db.commit()

        # Process again
        return self.process_youtube_url(
            file_record.source_url,
            file_record.module_id,
            file_record.transcript_language or 'pt-br',
            file_record.name
        )

    def delete_transcription(self, file_id: int) -> bool:
        """
        Delete a transcription (soft delete)

        Args:
            file_id: File ID to delete

        Returns:
            True if deleted successfully
        """
        file_record = self.db.query(File).filter(File.id == file_id).first()
        if not file_record:
            return False

        # Soft delete
        file_record.is_active = False
        self.db.commit()

        logger.info(f"Soft deleted transcription file {file_id}")

        # TODO: Also remove from OpenAI vector store if needed
        # This would require tracking vector store file IDs

        return True
