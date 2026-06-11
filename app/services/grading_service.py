"""
Grading Service - AI-powered batch grading of student written answers.

Input  : JSON file uploaded to S3 (Moodle-compatible format)
Output : CSV  → S3  (matricula,email,cmid,slot,nota,comentario,max_mark,name,question_text,answer)

Flow:
  1. Load GradingJob from DB, set status → processing
  2. Download input JSON from S3
  3. Parse JSON with field normalizer (handles snake/camel/Pascal)
  4. Load course context: all modules → all active files → extracted text
  5. For each student: batch-grade all non-empty answers in ONE AI call
     - Empty answers → nota=0, comentario="Resposta não fornecida" (no AI call)
  6. Build CSV and upload to S3
  7. Update DB: status=completed or failed
"""

import asyncio
import csv
import io
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.models import Module, File
from app.models.ai_model import AIModel
from app.models.grading_job import GradingJob

logger = logging.getLogger(__name__)

# Maximum course context characters sent to the AI (prevents context window overflow)
MAX_CONTEXT_CHARS = 25_000


# ---------------------------------------------------------------------------
# HTML stripper + base64 image extractor
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_BASE64_IMG_RE = re.compile(
    r'<img[^>]+src=["\']?(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)["\']?',
    re.IGNORECASE,
)
# Cap images per question to avoid blowing API limits (base64 images are large)
_MAX_IMAGES_PER_QUESTION = 3


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = _HTML_TAG_RE.sub(" ", html)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _extract_base64_images(html: str) -> list[str]:
    """Return a list of data URIs ('data:image/png;base64,...') found in HTML."""
    return _BASE64_IMG_RE.findall(html)[:_MAX_IMAGES_PER_QUESTION]


def _get_question_text(q: dict) -> str:
    """
    Return the best available question text.
    Prefers plain-text 'question_text'; falls back to stripping HTML from
    'question_text_html' (present in newer Moodle exports).
    """
    plain = str(q.get("question_text") or "").strip()
    if plain:
        return plain
    html = str(q.get("question_text_html") or "").strip()
    return _strip_html(html) if html else ""


def _get_question_images(q: dict) -> list[str]:
    """
    Extract base64 images embedded in the question HTML and/or answer HTML.
    Covers both question context images and image-based student answers.
    """
    images: list[str] = []
    for field in ("question_text_html", "answer_html"):
        html = str(q.get(field) or "").strip()
        if html:
            images.extend(_extract_base64_images(html))
    # Also handle plain answer field if it looks like HTML with an img tag
    answer_raw = str(q.get("answer") or q.get("answer_text") or "").strip()
    if answer_raw and "<img" in answer_raw:
        images.extend(_extract_base64_images(answer_raw))
    return images[:_MAX_IMAGES_PER_QUESTION]


def _get_answer(q: dict) -> str:
    """
    Return the student's answer, accepting several field names used across
    Moodle export versions. Strips HTML if the answer contains markup.
    """
    raw = str(
        q.get("answer")
        or q.get("answer_text")
        or q.get("response")
        or q.get("responsesummary")
        or ""
    ).strip()
    # If the answer is HTML (e.g. rich text editor output), strip tags for the text portion
    if raw and ("<" in raw and ">" in raw):
        return _strip_html(raw)
    return raw


# ---------------------------------------------------------------------------
# Key normalizer
# ---------------------------------------------------------------------------

def _to_snake(key: str) -> str:
    """Convert camelCase / PascalCase / _prefixed to snake_case."""
    key = key.lstrip("_")          # strip Moodle's internal _ prefix
    key = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", key)
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return key.lower()


def _normalize(obj: Any) -> Any:
    """Recursively normalize dict keys to snake_case."""
    if isinstance(obj, dict):
        return {_to_snake(k): _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Course context builder
# ---------------------------------------------------------------------------

async def _build_course_context(course_id: int, db: Session) -> str:
    """
    Load extracted text from all active files across all active modules of
    the course. Prefers summarized_text for large files to stay under token limits.
    """
    modules = (
        db.query(Module)
        .options(joinedload(Module.files))
        .filter(Module.course_id == course_id, Module.is_active == True)
        .all()
    )

    parts: list[str] = []
    total_chars = 0

    for module in modules:
        active_files: list[File] = [f for f in (module.files or []) if f.is_active]
        for file in active_files:
            if total_chars >= MAX_CONTEXT_CHARS:
                break

            # Pick best available text
            text = file.summarized_text if (
                file.summarized_text and
                file.extracted_text and
                len(file.extracted_text) > 25_000
            ) else file.extracted_text

            if not text and file.transcript_text:
                text = file.transcript_text

            if not text:
                continue

            remaining = MAX_CONTEXT_CHARS - total_chars
            snippet = text[:remaining]
            parts.append(f"=== {file.name} ===\n{snippet}\n")
            total_chars += len(snippet)

        if total_chars >= MAX_CONTEXT_CHARS:
            break

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# AI provider chain (mirrors quiz_extractor.py pattern)
# ---------------------------------------------------------------------------

def _get_provider_chain(db: Session) -> list[dict]:
    """DB-configured UseForFileExtraction models first, then config fallback."""
    try:
        db_models = (
            db.query(AIModel)
            .filter(
                AIModel.use_for_file_extraction == True,
                AIModel.is_active == True,
                AIModel.is_deprecated == False,
            )
            .all()
        )
        if db_models:
            chain = [{"provider": m.provider.lower(), "model": m.model_name} for m in db_models]
            logger.info("Grading using DB-configured models: %s", [c["model"] for c in chain])
            return chain
    except Exception as exc:
        logger.warning("Failed to query DB for extraction models: %s", exc)

    primary = settings.FILE_PROCESSING_PROVIDER.lower()
    fallback_str = getattr(settings, "FILE_PROCESSING_FALLBACK_CHAIN", "openai")
    fallbacks = [p.strip().lower() for p in fallback_str.split(",") if p.strip()]
    chain = [{"provider": p, "model": None} for p in [primary] + fallbacks]
    logger.info("Grading using config-based chain: %s", [c["provider"] for c in chain])
    return chain


def _build_user_content(user_prompt: str, images: list[str], provider: str) -> Any:
    """
    Build the user message content for the given provider.
    Returns a plain string when there are no images (universal compatibility).
    Returns a multimodal list when images are present, formatted per provider.
    DeepSeek has no vision support — images are ignored (noted in prompt instead).
    """
    if not images or provider == "deepseek":
        return user_prompt

    if provider == "anthropic":
        content: list = []
        for data_uri in images:
            try:
                header, b64data = data_uri.split(";base64,", 1)
                media_type = header.split(":", 1)[1]   # e.g. "image/png"
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64data},
                })
            except Exception:
                pass  # malformed URI — skip
        content.append({"type": "text", "text": user_prompt})
        return content

    # OpenAI-compatible format (openai, gemini, xai)
    content = [{"type": "text", "text": user_prompt}]
    for data_uri in images:
        content.append({"type": "image_url", "image_url": {"url": data_uri}})
    return content


async def _call_provider(
    provider: str,
    model_name: Optional[str],
    system_prompt: str,
    user_prompt: str,
    db: Session,
    images: Optional[list[str]] = None,
) -> Optional[str]:
    """Call a single AI provider and return raw text response."""
    from types import SimpleNamespace

    images = images or []
    dummy = SimpleNamespace(id=0, ai_model=None, ai_model_id=None, name="grading")

    if provider == "anthropic":
        try:
            from app.services.anthropic_service import AnthropicService
        except ImportError:
            logger.error("anthropic package not installed; skipping")
            return None
        svc = AnthropicService(module=dummy, model_name=model_name, db=db)
        user_content = _build_user_content(user_prompt, images, "anthropic")
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]
        result = await svc.answer_question_with_history(messages)
        return result.get("response", "")

    if provider == "openai":
        from app.services.ai_service import AIService
        svc = AIService(module=dummy, db=db)
        if model_name:
            svc.model_name = model_name
        client = svc.async_client
    elif provider == "deepseek":
        from app.services.deepseek_service import DeepSeekService
        svc = DeepSeekService(module=dummy, model_name=model_name, db=db)
        client = svc.client
    elif provider == "gemini":
        from app.services.gemini_service import GeminiService
        svc = GeminiService(module=dummy, model_name=model_name, db=db)
        client = svc.client
    elif provider == "xai":
        from app.services.xai_service import XAIService
        svc = XAIService(module=dummy, model_name=model_name, db=db)
        client = svc.client
    else:
        logger.warning("Unsupported grading provider: %s", provider)
        return None

    user_content = _build_user_content(user_prompt, images, provider)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    response = await client.chat.completions.create(
        model=svc.model_name,
        messages=messages,
        max_tokens=8192,
        temperature=0.1,
    )
    return response.choices[0].message.content


async def _call_ai(system_prompt: str, user_prompt: str, db: Session, images: Optional[list[str]] = None) -> str:
    """Try each provider in the chain; raise if all fail."""
    chain = _get_provider_chain(db)
    last_error = None
    for entry in chain:
        try:
            result = await _call_provider(
                entry["provider"], entry.get("model"), system_prompt, user_prompt, db, images
            )
            if result:
                return result
        except Exception as exc:
            last_error = exc
            logger.warning("Grading: provider %s failed: %s", entry["provider"], exc)
    raise RuntimeError(f"Grading: all AI providers failed. Last error: {last_error}")


# ---------------------------------------------------------------------------
# Parse AI grading response
# ---------------------------------------------------------------------------

def _parse_grading_response(text: str, questions: list[dict]) -> list[dict]:
    """
    Parse the AI JSON response into [{slot, grade, comment}].
    Handles code fences and partial responses.
    Falls back to 0/error-comment for any question the AI didn't cover.
    """
    cleaned = text.strip()
    code_block = re.search(r"```(?:json)?\s*\n?(.*?)```", cleaned, re.DOTALL)
    if code_block:
        cleaned = code_block.group(1).strip()

    parsed: list[dict] = []
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            parsed = data
        elif isinstance(data, dict) and "grades" in data:
            parsed = data["grades"]
        elif isinstance(data, dict) and "results" in data:
            parsed = data["results"]
    except json.JSONDecodeError:
        # Try to extract JSON array from partial response
        start = cleaned.find("[")
        end = cleaned.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass

    # Index by slot for O(1) lookup
    by_slot: dict[int, dict] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        slot = item.get("slot")
        if slot is not None:
            by_slot[int(slot)] = item

    results: list[dict] = []
    for q in questions:
        slot = int(q["slot"])
        max_mark = _parse_decimal(q.get("max_mark", "1") or "1")
        if slot in by_slot:
            raw_grade = _parse_decimal(str(by_slot[slot].get("grade", 0)))
            # Clamp to [0, max_mark]
            grade = max(Decimal("0"), min(raw_grade, max_mark))
            comment = str(by_slot[slot].get("comment", "")).strip() or "—"
        else:
            grade = Decimal("0")
            comment = "Não avaliado"
        results.append({"slot": slot, "grade": grade, "comment": comment})

    return results


def _parse_decimal(value: str) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return Decimal("0")


def _format_nota(grade) -> str:
    """
    Convert a Decimal/float grade to a clean decimal string with period separator.
    Strips unnecessary trailing zeros: 1.50 → "1.5", 2.00 → "2", 0.75 → "0.75".
    """
    try:
        d = Decimal(str(grade)).normalize()
        # normalize() removes trailing zeros; convert back to fixed-point if needed
        formatted = format(d, "f")
        return formatted
    except Exception:
        return "0"


def _sanitize_comment(text: str) -> str:
    """
    Strip newlines and excessive whitespace from AI-generated comments.
    Newlines inside a CSV field would break row parsing in most tools.
    """
    if not text:
        return "—"
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _get_s3():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


async def _download_s3_json(s3_key: str) -> list:
    s3 = _get_s3()
    response = await asyncio.to_thread(
        s3.get_object, Bucket=settings.S3_BUCKET_NAME, Key=s3_key
    )
    raw = response["Body"].read()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("Input JSON must be an array of student submissions")
    return _normalize(data)


async def _upload_s3_csv(s3_key: str, csv_bytes: bytes) -> None:
    s3 = _get_s3()
    await asyncio.to_thread(
        s3.put_object,
        Bucket=settings.S3_BUCKET_NAME,
        Key=s3_key,
        Body=csv_bytes,
        ContentType="text/csv",
    )


# ---------------------------------------------------------------------------
# Main grading logic
# ---------------------------------------------------------------------------

class GradingService:
    def __init__(self, db: Session):
        self.db = db

    async def process_job(self, job_id: int, course_id: int) -> None:
        """
        Full processing cycle for one grading job.
        Updates DB status throughout. Catches all errors to ensure DB is always updated.
        """
        job: Optional[GradingJob] = self.db.query(GradingJob).filter(GradingJob.id == job_id).first()
        if not job:
            logger.error("GradingJob %s not found in DB", job_id)
            return

        logger.info("🎓 Starting grading job %s for course %s", job_id, course_id)

        # Mark processing
        job.status = "processing"
        job.updated_at = datetime.now(timezone.utc)
        self.db.commit()

        try:
            await self._run(job, course_id)
        except Exception as exc:
            logger.error("❌ Grading job %s failed: %s", job_id, exc, exc_info=True)
            job.status = "failed"
            job.error_message = str(exc)[:2000]
            job.processed_at = datetime.now(timezone.utc)
            job.updated_at = datetime.now(timezone.utc)
            self.db.commit()

    async def _run(self, job: GradingJob, course_id: int) -> None:
        # 1. Download + parse input JSON
        if not job.input_s3_key:
            raise ValueError("GradingJob has no input_s3_key")

        logger.info("Downloading input JSON: %s", job.input_s3_key)
        submissions = await _download_s3_json(job.input_s3_key)
        logger.info("Parsed %d student submission(s)", len(submissions))

        job.total_submissions = len(submissions)
        self.db.commit()

        # 2. Build course context (all modules, all files)
        logger.info("Building course context for course %s…", course_id)
        course_context = await _build_course_context(course_id, self.db)
        logger.info("Course context: %d chars", len(course_context))

        # 3. Grade each student (pass grading_criteria from job)
        grading_criteria: Optional[str] = getattr(job, "grading_criteria", None)
        csv_rows: list[dict] = []
        processed = 0

        for submission in submissions:
            try:
                rows = await self._grade_submission(submission, course_context, grading_criteria)
                csv_rows.extend(rows)
                processed += 1
                job.processed_submissions = processed
                self.db.commit()
            except Exception as exc:
                logger.error(
                    "Failed to grade submission for student %s: %s",
                    submission.get("id") or submission.get("matricula", "?"),
                    exc,
                    exc_info=True,
                )
                # Add zero-graded rows with error comment so the CSV is still complete
                matricula = str(
                    submission.get("id") or submission.get("matricula")
                    or submission.get("userid") or submission.get("idnumber") or ""
                )
                email = str(submission.get("email", ""))
                sub_cmid = str(submission.get("quiz_cmid") or submission.get("cmid") or "")
                sub_name = str(submission.get("name", ""))
                for q in submission.get("questions", []):
                    q_cmid = sub_cmid or str(q.get("quiz_cmid") or q.get("cmid") or "")
                    csv_rows.append({
                        "matricula": matricula,
                        "email": email,
                        "cmid": q_cmid,
                        "slot": str(q.get("slot", "")),
                        "nota": "0",
                        "comentario": _sanitize_comment(f"Erro ao processar: {str(exc)[:200]}"),
                        "max_mark": str(q.get("max_mark", "") or ""),
                        "name": sub_name,
                        "question_text": _get_question_text(q),
                        "answer": _get_answer(q),
                    })
                processed += 1
                job.processed_submissions = processed
                self.db.commit()

        # 4. Build and upload CSV
        result_key = f"grading-jobs/{course_id}/{job.id}/result.csv"
        csv_bytes = _build_csv(csv_rows)
        logger.info("Uploading result CSV (%d bytes) to %s", len(csv_bytes), result_key)
        await _upload_s3_csv(result_key, csv_bytes)

        # 5. Mark completed
        job.status = "completed"
        job.result_s3_key = result_key
        job.processed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        self.db.commit()

        logger.info(
            "✅ Grading job %s completed — %d submissions, %d CSV rows",
            job.id, processed, len(csv_rows),
        )

    async def _grade_submission(self, submission: dict, course_context: str, grading_criteria: Optional[str] = None) -> list[dict]:
        """
        Grade all questions for one student. Returns list of CSV-row dicts.
        Empty answers get 0 without an AI call.
        All non-empty answers are batched into a single AI call per student.
        """
        # Accept several common field names for student identifier
        matricula = str(
            submission.get("id")
            or submission.get("matricula")
            or submission.get("userid")
            or submission.get("idnumber")
            or submission.get("student_id")
            or ""
        )
        name = str(submission.get("name", ""))
        email = str(submission.get("email", ""))
        # Submission-level cmid (falls back to question-level per question)
        submission_cmid = str(submission.get("quiz_cmid") or submission.get("cmid") or "")
        questions: list[dict] = submission.get("questions", [])

        rows: list[dict] = []
        ai_questions: list[dict] = []

        for q in questions:
            slot = str(q.get("slot", ""))
            answer = _get_answer(q)
            max_mark = str(q.get("max_mark", "1") or "1")
            question_text = _get_question_text(q)
            # Prefer question-level cmid when submission-level is absent
            cmid = submission_cmid or str(q.get("quiz_cmid") or q.get("cmid") or "")

            if not answer:
                # Empty answer — no AI needed
                rows.append({
                    "matricula": matricula,
                    "email": email,
                    "cmid": cmid,
                    "slot": slot,
                    "nota": "0",
                    "comentario": "Resposta não fornecida",
                    "max_mark": max_mark,
                    "name": name,
                    "question_text": question_text,
                    "answer": answer,
                })
            else:
                ai_questions.append({
                    "slot": q.get("slot"),
                    "question_text": question_text,
                    "answer": answer,
                    "max_mark": max_mark,
                    "cmid": cmid,
                    "images": _get_question_images(q),
                })

        if not ai_questions:
            return rows

        # Single AI call for all non-empty answers of this student
        graded = await self._call_ai_for_student(ai_questions, course_context, grading_criteria)

        for result in graded:
            # Retrieve the original question data for this slot
            orig_q = next(
                (q for q in ai_questions if str(q["slot"]) == str(result["slot"])),
                {},
            )
            rows.append({
                "matricula": matricula,
                "email": email,
                "cmid": orig_q.get("cmid", submission_cmid),
                "slot": str(result["slot"]),
                "nota": _format_nota(result["grade"]),
                "comentario": _sanitize_comment(result["comment"]),
                "max_mark": orig_q.get("max_mark", ""),
                "name": name,
                "question_text": orig_q.get("question_text", ""),
                "answer": orig_q.get("answer", ""),
            })

        return rows

    async def _call_ai_for_student(
        self, questions: list[dict], course_context: str, grading_criteria: Optional[str] = None
    ) -> list[dict]:
        """
        One AI call that grades all questions for a single student.
        Returns [{slot, grade, comment}] — one entry per question.
        Passes base64 images (from question HTML or answer HTML) as multimodal content.
        """
        # Collect all images across this student's questions, tagged by slot
        all_images: list[str] = []
        for q in questions:
            imgs = q.get("images") or []
            all_images.extend(imgs)

        # Build questions payload; note image presence per slot so the model knows
        questions_payload = []
        for q in questions:
            entry: dict = {
                "slot": q["slot"],
                "question": q["question_text"],
                "answer": q["answer"],
                "max_mark": q["max_mark"],
            }
            if q.get("images"):
                entry["note"] = f"This question/answer includes {len(q['images'])} image(s) attached to this message."
            questions_payload.append(entry)

        questions_json = json.dumps(questions_payload, ensure_ascii=False, indent=2)

        context_section = (
            f"Course content:\n{course_context}\n\n" if course_context.strip() else ""
        )

        criteria_section = (
            f"Grading criteria defined by the professor:\n{grading_criteria.strip()}\n\n"
            if grading_criteria and grading_criteria.strip()
            else ""
        )

        image_note = (
            f"\nNote: {len(all_images)} image(s) are attached to this message. "
            "Use them to evaluate questions/answers that reference visual content.\n"
            if all_images else ""
        )

        system_prompt = (
            "You are an academic grader. Evaluate each student answer based on the "
            "course content provided and the question. "
            "When images are attached, use them to interpret visual questions or answers. "
            "Assign a decimal grade between 0 and max_mark. "
            "Write brief, constructive feedback in the SAME LANGUAGE as the question. "
            "Return ONLY valid JSON — no markdown, no explanation outside JSON."
        )

        user_prompt = (
            f"{context_section}"
            f"{criteria_section}"
            f"{image_note}"
            f"Grade the following student answers:\n{questions_json}\n\n"
            "Return a JSON array:\n"
            '[{"slot": <number>, "grade": <decimal 0..max_mark>, "comment": "<feedback>"}]\n'
            "One entry per question. No other text."
        )

        response_text = await _call_ai(system_prompt, user_prompt, self.db, images=all_images or None)
        return _parse_grading_response(response_text, questions)


# ---------------------------------------------------------------------------
# CSV builder
# ---------------------------------------------------------------------------

def _build_csv(rows: list[dict]) -> bytes:
    """
    Serialize rows to UTF-8-with-BOM CSV bytes.
    Header: matricula,email,cmid,slot,nota,comentario,max_mark,name,question_text,answer

    - UTF-8 BOM (﻿) ensures Excel on Windows (Portuguese locale) opens correctly.
    - QUOTE_ALL ensures every field is quoted, preventing any in-field comma from
      being misinterpreted as a column separator.
    """
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["matricula", "email", "cmid", "slot", "nota", "comentario", "max_mark", "name", "question_text", "answer"],
        extrasaction="ignore",
        lineterminator="\r\n",   # Windows-style line endings for Excel compatibility
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    # Prepend UTF-8 BOM so Excel auto-detects encoding
    return "﻿".encode("utf-8") + output.getvalue().encode("utf-8")
