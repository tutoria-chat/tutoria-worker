"""
YouTube transcription service

Strategy:
1. Free captions from YouTube (youtube-transcript-api) — instant, $0
2. Fallback: yt-dlp audio download + Whisper transcription — free (local model)
   or cheap (OpenAI Whisper API) if local model not installed
"""

import os
import re
import logging
import tempfile
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class YouTubeService:

    def __init__(self, openai_client=None):
        # openai_client kept for backward compat — Whisper API uses it if local not available
        self.openai_client = openai_client

    # ------------------------------------------------------------------
    # Video ID extraction
    # ------------------------------------------------------------------

    def extract_video_id(self, url: str) -> Optional[str]:
        """Extract 11-char video ID from any YouTube URL format."""
        patterns = [
            r'(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})',
            r'youtube\.com\/watch\?.*v=([a-zA-Z0-9_-]{11})',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        logger.warning(f"Could not extract video ID from URL: {url}")
        return None

    # ------------------------------------------------------------------
    # Strategy 1: free YouTube captions
    # ------------------------------------------------------------------

    def get_free_transcript(self, video_id: str, language: str = 'pt') -> Optional[Dict[str, Any]]:
        """
        Fetch captions directly from YouTube (manual > auto-generated).
        Free, instant — no audio download needed.
        """
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            from youtube_transcript_api._errors import (
                TranscriptsDisabled, NoTranscriptFound, VideoUnavailable, TooManyRequests
            )

            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

            transcript = None
            source = None

            # Prefer manually-created captions (higher quality)
            try:
                transcript = transcript_list.find_manually_created_transcript([language, 'en'])
                source = 'youtube_manual'
            except NoTranscriptFound:
                try:
                    transcript = transcript_list.find_generated_transcript([language, 'en'])
                    source = 'youtube_auto'
                except NoTranscriptFound:
                    return None

            entries = transcript.fetch()
            full_text = ' '.join(e['text'] for e in entries)
            duration_seconds = int(entries[-1]['start'] + entries[-1]['duration']) if entries else 0

            logger.info(f"✅ Free transcript for {video_id}: {len(full_text)} chars, source={source}")
            return {
                'text': full_text,
                'source': source,
                'duration_seconds': duration_seconds,
                'language': transcript.language_code,
                'word_count': len(full_text.split()),
                'cost_usd': 0.0,
            }

        except TranscriptsDisabled:
            logger.warning(f"Transcripts disabled for {video_id}")
        except VideoUnavailable:
            logger.warning(f"Video unavailable: {video_id}")
        except TooManyRequests:
            logger.warning(f"YouTube rate-limited transcript request for {video_id}")
        except ImportError:
            logger.error("youtube-transcript-api not installed: pip install youtube-transcript-api")
        except Exception as e:
            logger.warning(f"Free transcript failed for {video_id}: {type(e).__name__}: {e}")

        return None

    # ------------------------------------------------------------------
    # Strategy 2a: audio download via yt-dlp
    # ------------------------------------------------------------------

    def download_audio(self, video_url: str) -> Optional[str]:
        """
        Download YouTube audio in native format (m4a preferred) via yt-dlp.
        No ffmpeg needed — OpenAI Whisper API accepts m4a/webm directly.

        Tries iOS player client first (most reliable), then Android as fallback.
        Returns the path to the downloaded audio file, or None on failure.
        """
        try:
            import yt_dlp
        except ImportError:
            logger.error("yt-dlp not installed: pip install yt-dlp")
            return None

        video_id = self.extract_video_id(video_url)
        if not video_id:
            return None

        temp_dir = tempfile.gettempdir()
        output_template = os.path.join(temp_dir, f"{video_id}.%(ext)s")

        base_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio',
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
        }

        for client in ('ios', 'android'):
            opts = {
                **base_opts,
                'extractor_args': {'youtube': {'player_client': [client]}},
            }
            try:
                logger.info(f"Downloading audio for {video_id} (client={client})...")
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                    ext = info.get('ext', 'm4a')

                audio_path = os.path.join(temp_dir, f"{video_id}.{ext}")
                if os.path.exists(audio_path):
                    size_mb = os.path.getsize(audio_path) / 1024 / 1024
                    logger.info(f"✅ Audio downloaded: {audio_path} ({size_mb:.1f} MB)")
                    return audio_path

            except yt_dlp.utils.DownloadError as e:
                err = str(e)
                if 'unavailable' in err.lower() or 'private' in err.lower():
                    logger.error(f"Video {video_id} is unavailable or private")
                    return None
                logger.warning(f"Download attempt ({client}) failed: {err[:120]}")
            except Exception as e:
                logger.warning(f"Download attempt ({client}) error: {type(e).__name__}: {e}")

        logger.error(f"Audio download failed for {video_id} after all attempts")
        return None

    # ------------------------------------------------------------------
    # Strategy 2b: Whisper transcription
    # ------------------------------------------------------------------

    def transcribe_with_whisper(self, audio_path: str, language: str = 'pt') -> Optional[Dict[str, Any]]:
        """Transcribe an audio file using the OpenAI Whisper API."""
        try:
            from openai import OpenAI
            from app.core.config import settings

            client = self.openai_client or OpenAI(api_key=settings.OPENAI_API_KEY)
            file_size_mb = os.path.getsize(audio_path) / 1024 / 1024
            logger.info(f"Transcribing with OpenAI Whisper API ({file_size_mb:.1f} MB)...")

            with open(audio_path, 'rb') as f:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language=language,
                )

            text = transcript.text.strip()
            # Estimate cost: $0.006 per minute, assume 128 kbps mp3 → ~1 MB/min
            duration_minutes = file_size_mb  # rough estimate
            cost_usd = round(duration_minutes * 0.006, 4)

            logger.info(f"✅ OpenAI Whisper API transcription: {len(text)} chars, ~${cost_usd:.4f}")
            return {
                'text': text,
                'source': 'whisper_api',
                'word_count': len(text.split()),
                'cost_usd': cost_usd,
                'language': language,
            }
        except Exception as e:
            logger.error(f"OpenAI Whisper API failed: {type(e).__name__}: {e}")

        return None

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def process_youtube_video(self, video_url: str, language: str = 'pt-br') -> Optional[Dict[str, Any]]:
        """
        Full pipeline:
        1. Try free YouTube captions (instant, $0)
        2. Fallback: yt-dlp download → Whisper transcription
        """
        video_id = self.extract_video_id(video_url)
        if not video_id:
            logger.error(f"Invalid YouTube URL: {video_url}")
            return None

        # Normalize language: 'pt-br' → 'pt' for YouTube/Whisper APIs
        lang = language.split('-')[0]

        # Strategy 1: free captions
        logger.info(f"[{video_id}] Trying free YouTube captions...")
        result = self.get_free_transcript(video_id, lang)
        if result:
            result['video_id'] = video_id
            return result

        logger.warning(f"[{video_id}] No free captions — downloading audio for Whisper...")

        # Strategy 2: download + Whisper
        audio_path = None
        try:
            audio_path = self.download_audio(video_url)
            if not audio_path:
                logger.error(f"[{video_id}] Audio download failed")
                return None

            result = self.transcribe_with_whisper(audio_path, lang)
            if result:
                result['video_id'] = video_id
                return result

            logger.error(f"[{video_id}] Whisper transcription failed")
            return None

        finally:
            # Always clean up the temp audio file
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                    logger.debug(f"Cleaned up temp file: {audio_path}")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Metadata (title for file naming)
    # ------------------------------------------------------------------

    def get_video_metadata(self, video_url: str) -> Optional[Dict[str, Any]]:
        """Get video title and duration without downloading audio."""
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
                info = ydl.extract_info(video_url, download=False)
                return {
                    'video_id': info.get('id'),
                    'title': info.get('title'),
                    'duration_seconds': info.get('duration'),
                    'uploader': info.get('uploader'),
                }
        except Exception as e:
            logger.warning(f"Could not get video metadata: {e}")
            return None
