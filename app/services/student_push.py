"""
Push for TutorIA Estudantes (B2C app) students when a discipline upload finishes
extraction. Their files live in their personal course (Code "STU-{userId}") of
the consumer university; device tokens and the "updates" opt-out are in
DeviceTokens / StudentProfiles (schema owned by TutoriaApi). Best-effort: never raises.
"""
import logging

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _consumer_owner(db: Session, module_id) -> int:
    """User id owning the module's personal course, or 0 when it isn't a student-app file."""
    if not module_id:
        return 0
    row = db.execute(
        text(
            'SELECT c."Code", u."IsConsumer" FROM "Modules" m '
            'JOIN "Courses" c ON c."Id" = m."CourseId" '
            'JOIN "Universities" u ON u."Id" = c."UniversityId" '
            'WHERE m."Id" = :mid'
        ),
        {"mid": module_id},
    ).first()
    if not row or not row[1] or not (row[0] or "").startswith("STU-"):
        return 0
    try:
        return int(row[0][4:])
    except ValueError:
        return 0


async def notify_file_processed(db: Session, file, ok: bool) -> None:
    if not settings.STUDENT_APP_PUSH_ENABLED:
        return
    try:
        user_id = _consumer_owner(db, file.module_id)
        if not user_id:
            return
        opted_in = db.execute(
            text('SELECT "NotifyUpdates" FROM "StudentProfiles" WHERE "UserId" = :uid'), {"uid": user_id}
        ).scalar()
        if opted_in is False:
            return
        tokens = [r[0] for r in db.execute(
            text('SELECT "Token" FROM "DeviceTokens" WHERE "UserId" = :uid'), {"uid": user_id}
        ).all()]
        if not tokens:
            return
        name = file.name or file.file_name or "Seu arquivo"
        title = "Material pronto para estudar 📚" if ok else "Não consegui ler um arquivo"
        body = (f"{name} já pode ser usado no chat e nos flashcards." if ok
                else f"{name} não pôde ser processado. Tente enviar outra versão (PDF, foto nítida ou DOCX).")
        data = {"type": "file", "id": file.id, "url": f"/discipline/{file.module_id}"}
        messages = [{"to": t, "title": title, "body": body, "data": data, "sound": "default"} for t in tokens]
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if settings.STUDENT_APP_EXPO_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {settings.STUDENT_APP_EXPO_ACCESS_TOKEN}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(EXPO_PUSH_URL, json=messages, headers=headers)
            resp.raise_for_status()
            dead = [
                m["to"] for m, t in zip(messages, resp.json().get("data", []))
                if t.get("status") == "error" and (t.get("details") or {}).get("error") == "DeviceNotRegistered"
            ]
        if dead:
            db.execute(text('DELETE FROM "DeviceTokens" WHERE "Token" = ANY(:tokens)'), {"tokens": dead})
            db.commit()
    except Exception:
        logger.warning("Student upload push failed for file %s", getattr(file, "id", "?"), exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
