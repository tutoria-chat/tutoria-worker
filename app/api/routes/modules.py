from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File as FastAPIFile, Header, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
import logging

from app.core.database import get_db, SessionLocal
from app.core.config import settings
from app.core.super_admin_auth import get_current_professor_or_super_admin
from app.models import Module as ModuleModel, Course as CourseModel

logger = logging.getLogger(__name__)


async def _require_professor_or_internal_key(
    x_internal_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Accepts either:
    1. X-Internal-Api-Key header — for .NET backend service-to-service calls
    2. JWT Authorization header — for dashboard/UI calls (professor or super admin)
    """
    # Path 1: Internal API key (service-to-service)
    if x_internal_api_key:
        if not settings.INTERNAL_API_KEY:
            raise HTTPException(status_code=500, detail="Internal API key not configured")
        if x_internal_api_key != settings.INTERNAL_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid internal API key")
        return {"type": "internal", "is_admin": True}

    # Path 2: JWT auth (professor or super admin)
    if authorization:
        from app.core.security import verify_token
        token_data = verify_token(authorization)
        return await get_current_professor_or_super_admin(token_data=token_data, db=db)

    raise HTTPException(status_code=401, detail="Authentication required (JWT token or X-Internal-Api-Key header)")


def require_professor_or_internal_key():
    """Returns the dependency for professor-or-internal-key auth."""
    return _require_professor_or_internal_key


router = APIRouter(prefix="/api/v2/modules", tags=["modules"])


class GenerateQuizResponse(BaseModel):
    """Response for quiz generation."""
    status: str
    message: str
    quiz_count: int
    module_id: int


@router.post("/{module_id}/generate-quizzes", response_model=GenerateQuizResponse, status_code=status.HTTP_202_ACCEPTED)
async def generate_quiz_bank(
    module_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(require_professor_or_internal_key()),
    count: int = 50,
    upsert: bool = False
):
    """
    Queue quiz generation for a module (fire-and-forget).

    Returns 202 Accepted immediately and generates quizzes in background.
    Called by TutoriaApi (.NET) when a module is created or updated.

    Args:
        module_id: ID of the module to generate quizzes for
        count: Number of questions to generate (default: 50)
        upsert: If True, regenerates quizzes even if they exist (default: False)
    """
    from sqlalchemy.orm import joinedload
    from app.models import Quiz

    try:
        module = db.query(ModuleModel).options(
            joinedload(ModuleModel.files)
        ).filter(ModuleModel.id == module_id, ModuleModel.is_active == True).first()
        if not module:
            raise HTTPException(status_code=404, detail="Module not found")

        # Plan gating: super admins and internal calls bypass
        if current_user.get("type") not in ("internal", "super_admin"):
            course = db.query(CourseModel).filter(CourseModel.id == module.course_id).first()
            if course:
                from app.core.plan_enforcement import check_feature_access
                if not check_feature_access(db, course.university_id, "ai_quizzes"):
                    raise HTTPException(
                        status_code=403,
                        detail="AI quiz generation is not available on your current plan. Please upgrade to Premium or Enterprise."
                    )

        # Check if AI-generated quizzes already exist (don't count question bank / uploaded quizzes)
        existing_count = db.query(Quiz).filter(
            Quiz.module_id == module_id,
            Quiz.source == "ai_generated"
        ).count()

        if existing_count > 0 and not upsert:
            return GenerateQuizResponse(
                status="skipped",
                message=f"Module already has {existing_count} quizzes. Use upsert=true to regenerate.",
                quiz_count=existing_count,
                module_id=module_id
            )

        # Queue background task
        logger.info(f"Queuing quiz generation for module {module_id} ({module.name}), count={count}, upsert={upsert}")

        background_tasks.add_task(
            _generate_quizzes_background,
            module_id=module_id,
            count=count,
            upsert=upsert,
            existing_count=existing_count
        )

        return GenerateQuizResponse(
            status="pending",
            message=f"Quiz generation queued for module {module_id}. Processing in background.",
            quiz_count=existing_count if not upsert else 0,
            module_id=module_id
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error queuing quiz generation for module {module_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to queue quiz generation: {type(e).__name__}: {str(e)[:500]}"
        )


def _generate_quizzes_background(
    module_id: int,
    count: int,
    upsert: bool,
    existing_count: int
):
    """Background task for quiz generation. Runs in a separate thread with its own DB session."""
    import asyncio
    from sqlalchemy.orm import joinedload
    from app.models import Quiz

    db = SessionLocal()
    try:
        module = db.query(ModuleModel).options(
            joinedload(ModuleModel.files)
        ).filter(ModuleModel.id == module_id, ModuleModel.is_active == True).first()

        if not module:
            logger.error(f"Module {module_id} not found (or soft-deleted) in background quiz task")
            return

        # Delete existing AI-generated quizzes if upserting (never touch question bank / uploaded quizzes)
        if upsert and existing_count > 0:
            logger.info(f"Upsert mode: Deleting {existing_count} existing AI-generated quizzes for module {module_id}")
            db.query(Quiz).filter(Quiz.module_id == module_id, Quiz.source == "ai_generated").delete()
            db.commit()

        action = "Regenerating" if upsert and existing_count > 0 else "Generating"
        logger.info(f"{action} {count} quiz questions for module {module_id} ({module.name})")

        from app.services.quiz_generator import QuizGeneratorService
        generator = QuizGeneratorService(module, db)

        # Run the async generator in a new event loop (background tasks run in threads)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            quizzes = loop.run_until_complete(generator.generate_quiz_bank(count=count))
        finally:
            loop.close()

        logger.info(f"Successfully generated {len(quizzes)} quizzes for module {module_id}")

    except Exception as e:
        logger.error(f"Background quiz generation failed for module {module_id}: {type(e).__name__}: {e}")
    finally:
        db.close()


class ExtractTextResponse(BaseModel):
    status: str
    message: str
    extracted_count: int
    failed_count: int
    module_id: int


class ExtractFileTextResponse(BaseModel):
    status: str
    message: str
    file_id: int
    word_count: int | None = None


@router.post("/{module_id}/extract-text", response_model=ExtractTextResponse)
async def extract_module_files_text(
    module_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_professor_or_super_admin),
):
    """
    Extract text from all PDF files in a module and store in database.
    This improves RAG performance by pre-extracting text instead of doing it on every chat.

    Args:
        module_id: Module ID
        force: Re-extract even if already extracted (default: False)

    Returns:
        ExtractTextResponse with extraction results
    """
    import logging
    from app.models import File
    from app.services.document_extraction_service import DocumentExtractionService

    logger = logging.getLogger(__name__)

    try:
        # Get module (skip soft-deleted)
        module = db.query(ModuleModel).filter(ModuleModel.id == module_id, ModuleModel.is_active == True).first()
        if not module:
            raise HTTPException(status_code=404, detail="Module not found")

        # Get all PDF files in module (match by file_type, content_type, or filename)
        from sqlalchemy import or_
        files_query = db.query(File).filter(
            File.module_id == module_id,
            File.is_active == True,
            or_(
                File.file_type.in_(['pdf', 'PDF', 'application/pdf']),
                File.content_type == 'application/pdf',
                File.file_name.ilike('%.pdf')
            )
        )

        # If not force mode, only get files that haven't been successfully processed yet
        if not force:
            from sqlalchemy import or_ as _or
            files_query = files_query.filter(
                _or(File.processing_status != 'ready', File.processing_status == None)
            )

        files = files_query.all()

        if not files:
            message = "No files to extract" if not force else "All files already extracted"
            logger.info(f"✅ {message} for module {module_id}")
            return ExtractTextResponse(
                status="skipped",
                message=message,
                extracted_count=0,
                failed_count=0,
                module_id=module_id
            )

        logger.info(f"📄 Extracting text from {len(files)} files in module {module_id} ({module.name})")

        # Extract text from each file
        service = DocumentExtractionService(db)
        extracted_count = 0
        failed_count = 0

        for file in files:
            success = await service.extract_and_store(file, force=force)
            if success:
                extracted_count += 1
            else:
                failed_count += 1

        logger.info(f"✅ Extraction complete: {extracted_count} succeeded, {failed_count} failed")

        return ExtractTextResponse(
            status="success" if failed_count == 0 else "partial",
            message=f"Extracted text from {extracted_count}/{len(files)} files",
            extracted_count=extracted_count,
            failed_count=failed_count,
            module_id=module_id
        )

    except Exception as e:
        logger.error(f"❌ Error extracting text for module {module_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to extract text: {str(e)}"
        )


@router.post("/files/{file_id}/extract-text", response_model=ExtractFileTextResponse)
async def extract_file_text(
    file_id: int,
    force: bool = False,
    use_ai: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(_require_professor_or_internal_key),
):
    """
    Extract text from a specific file and store in database.

    TRIGGER THIS ON FILE UPLOAD for immediate extraction!

    Args:
        file_id: File ID
        force: Re-extract even if already extracted (default: False)
        use_ai: Use OpenAI Vision for complex PDFs with images/tables (default: False, costs money)

    Returns:
        ExtractFileTextResponse with extraction result
    """
    import logging
    from app.models import File
    from app.services.document_extraction_service import DocumentExtractionService

    logger = logging.getLogger(__name__)

    try:
        # Get file
        file = db.query(File).filter(File.id == file_id).first()
        if not file:
            raise HTTPException(status_code=404, detail="File not found")

        # Check if already successfully processed (unless force mode)
        if file.processing_status == 'ready' and not force:
            logger.info(f"✅ File {file_id} already processed ({file.extracted_word_count} words)")
            return ExtractFileTextResponse(
                status="skipped",
                message="File already processed (use force=true to re-extract)",
                file_id=file_id,
                word_count=file.extracted_word_count
            )

        logger.info(f"📄 Extracting text from file {file_id} ({file.file_name})...")

        # Extract text
        service = DocumentExtractionService(db)
        success = await service.extract_and_store(file, force=force, use_openai=use_ai)

        if not success:
            raise HTTPException(
                status_code=500,
                detail="Failed to extract text from file"
            )

        logger.info(f"✅ Successfully extracted {file.extracted_word_count} words from file {file_id}")

        return ExtractFileTextResponse(
            status="success",
            message=f"Extracted {file.extracted_word_count} words",
            file_id=file_id,
            word_count=file.extracted_word_count
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error extracting text from file {file_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to extract text: {str(e)}"
        )
