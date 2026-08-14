"""
Assignment Feedback Service — generates AI feedback for student submissions.

Consumed via the SQS feedback queue (enqueued by tutoria-api's companion-widget
endpoint). The result is stored on the AssignmentSubmissions row, which the
widget polls; the interaction is also recorded in the module's DynamoDB
conversation so chat follow-ups have the feedback as context.

AI provider selection: the module's configured model first, then the grading
fallback chain (same multi-provider helpers as grading_service).
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.models import Assignment, AssignmentSubmission, Module
from app.services.blob_storage import get_blob_storage
from app.services.quiz_extractor import extract_text_from_file

logger = logging.getLogger(__name__)

LANGUAGE_INSTRUCTIONS = {
    "pt-br": "CRITICAL INSTRUCTION: You MUST respond EXCLUSIVELY in Portuguese (Brazil). Even if the user writes in English or Spanish, your response MUST be in Portuguese (Brazil).",
    "en": "CRITICAL INSTRUCTION: You MUST respond EXCLUSIVELY in English.",
    "es": "CRITICAL INSTRUCTION: You MUST respond EXCLUSIVELY in Spanish.",
}


def _save_feedback_to_chat(
    conversation_id: str,
    student_id: int,
    module_id: int,
    question: str,
    response: str,
    provider: str,
    file_name: str,
    matricula: Optional[str],
) -> Optional[str]:
    """Record the feedback interaction in the ChatMessages table (90-day TTL)."""
    from app.services.dynamodb_service import get_chat_table

    table = get_chat_table()
    if table is None:
        logger.info("[DynamoDB disabled] Skipping feedback chat record")
        return None

    try:
        message_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        item = {
            "conversationId": conversation_id,
            "timestamp": int(time.time() * 1000),
            "messageId": message_id,
            "studentId": student_id,
            "moduleId": module_id,
            "question": question,
            "response": response,
            "modelUsed": provider,
            "provider": provider,
            "hasFile": True,
            "fileName": file_name,
            "createdAt": now.isoformat(),
            "ttl": int(time.time()) + (90 * 86400),
        }
        if matricula:
            item["matricula"] = matricula
        table.put_item(Item=item)
        return message_id
    except Exception as e:
        logger.warning(f"Failed to save feedback to DynamoDB: {e}")
        return None


def _build_feedback_prompt(
    assignment: Assignment,
    module: Module,
    assignment_text: str,
    submission_text: str,
    rubric_section: str,
) -> str:
    tutor_language = module.tutor_language or "pt-br"
    language_instruction = LANGUAGE_INSTRUCTIONS.get(tutor_language, LANGUAGE_INSTRUCTIONS["pt-br"])

    keywords_section = ""
    if assignment.keywords:
        kw_list = [k.strip() for k in assignment.keywords.split(",") if k.strip()]
        if kw_list:
            keywords_section = f"\nKey aspects to focus on: {', '.join(kw_list)}\n"

    assignment_context = f"""--- ASSIGNMENT CONTEXT ---
Title: {assignment.title}
Instructions: {assignment.description or "(No specific instructions provided)"}
{keywords_section}
Assignment Document Content:
{assignment_text}
{rubric_section}
Student's Submitted Work:
{submission_text}

You are reviewing the student's work against the assignment requirements above.
Provide structured feedback: strengths, areas for improvement, and specific suggestions.
Be constructive and specific. Do NOT assign a numerical grade.
--- END ASSIGNMENT CONTEXT ---"""

    parts = [p for p in [language_instruction, module.system_prompt, assignment_context] if p]
    return "\n\n".join(parts)


async def _call_feedback_ai(module: Module, system_prompt: str, user_prompt: str, db: Session) -> str:
    """
    Call the module's configured AI model first; fall back to the grading
    provider chain if it fails.
    """
    from app.services.grading_service import _call_provider, _get_provider_chain

    chain = []
    if module.ai_model and module.ai_model.provider:
        chain.append({"provider": module.ai_model.provider, "model": module.ai_model.model_name})
    chain.extend(_get_provider_chain(db))

    last_error: Optional[Exception] = None
    seen = set()
    for entry in chain:
        key = (entry["provider"], entry.get("model"))
        if key in seen:
            continue
        seen.add(key)
        try:
            result = await _call_provider(entry["provider"], entry.get("model"), system_prompt, user_prompt, db)
            if result:
                return result
        except Exception as exc:
            last_error = exc
            logger.warning("Feedback: provider %s failed: %s", entry["provider"], exc)

    raise RuntimeError(f"Feedback: all AI providers failed. Last error: {last_error}")


async def process_feedback_job(
    db: Session,
    submission_id: int,
    conversation_id: str,
    matricula: Optional[str] = None,
    module_id: Optional[int] = None,
) -> None:
    """
    Generate feedback for a submission and store it on the row.
    Raises on transient errors (SQS will redeliver); marks the row "failed"
    and swallows permanent generation errors after the final retry is up to SQS/DLQ.
    """
    submission = (
        db.query(AssignmentSubmission)
        .filter(AssignmentSubmission.id == submission_id)
        .first()
    )
    if not submission:
        raise ValueError(f"Submission {submission_id} not found")

    # Idempotency: SQS is at-least-once — skip if a previous delivery finished
    if submission.status == "completed" and submission.feedback_text:
        logger.info("⏭️  Feedback already completed for submission %s — skipping", submission_id)
        return

    assignment = db.query(Assignment).filter(Assignment.id == submission.assignment_id).first()
    if not assignment:
        raise ValueError(f"Assignment {submission.assignment_id} not found for submission {submission_id}")

    # Assignments belong to the course, not a module — but feedback still needs a
    # module for its AI model and system prompt. Use the module the student was in
    # when they submitted (carried on the queue message); fall back to the course's
    # first active module for messages enqueued before module_id was threaded through.
    module_query = db.query(Module).options(joinedload(Module.ai_model))
    module = module_query.filter(Module.id == module_id).first() if module_id else None
    if not module:
        module = (
            module_query
            .filter(Module.course_id == assignment.course_id, Module.is_active == True)
            .order_by(Module.id)
            .first()
        )
    if not module:
        raise ValueError(
            f"No module found for course {assignment.course_id} (submission {submission_id})"
        )

    blob_storage = get_blob_storage()

    # Student submission file
    submission_bytes = await blob_storage.get_file_content(submission.s3_key)
    if not submission_bytes:
        raise RuntimeError(f"Could not download submission file {submission.s3_key}")

    try:
        submission_text = extract_text_from_file(submission_bytes, submission.original_file_name)
    except Exception as e:
        logger.warning(f"Failed to extract submission text: {e}")
        submission_text = "(Could not extract submission text)"

    # Assignment document (prefer cached extracted_text)
    assignment_text = assignment.extracted_text or ""
    if not assignment_text:
        assignment_bytes = await blob_storage.get_file_content(assignment.s3_key)
        if assignment_bytes:
            try:
                assignment_text = extract_text_from_file(assignment_bytes, assignment.original_file_name)
            except Exception as e:
                logger.warning(f"Failed to extract assignment text: {e}")
        if not assignment_text:
            assignment_text = "(Could not extract assignment document text)"

    # Optional rubric
    rubric_section = ""
    if assignment.rubric_s3_key:
        try:
            rubric_bytes = await blob_storage.get_file_content(assignment.rubric_s3_key)
            if rubric_bytes:
                rubric_text = extract_text_from_file(
                    rubric_bytes, assignment.rubric_original_file_name or "rubric.pdf"
                )
                rubric_section = f"\nEvaluation Rubric / Grading Criteria:\n{rubric_text}\n"
        except Exception as e:
            logger.warning(f"Failed to extract rubric text: {e}")

    system_prompt = _build_feedback_prompt(assignment, module, assignment_text, submission_text, rubric_section)
    feedback_request_msg = f"Quero feedback sobre meu trabalho na atividade: **{assignment.title}**"

    ai_response = await _call_feedback_ai(module, system_prompt, feedback_request_msg, db)

    # Post-format into clean markdown when a formatting model is configured
    try:
        from app.services.formatting_service import get_formatting_model, format_response
        fmt_model = get_formatting_model(db)
        if fmt_model:
            formatted = await format_response(ai_response, fmt_model, db)
            if formatted:
                ai_response = formatted
    except Exception as e:
        logger.warning(f"Feedback formatting skipped: {e}")

    # Record in the module's conversation so chat follow-ups have context
    provider = (module.ai_model.provider if module.ai_model else None) or "openai"
    await asyncio.to_thread(
        _save_feedback_to_chat,
        conversation_id,
        submission.student_id or 0,
        module.id,
        feedback_request_msg,
        ai_response,
        provider,
        submission.original_file_name,
        matricula,
    )

    submission.feedback_text = ai_response
    submission.feedback_generated_at = datetime.now(timezone.utc)
    submission.status = "completed"
    db.commit()
    logger.info("✅ Feedback generated — submission_id=%s module_id=%s", submission_id, module.id)
