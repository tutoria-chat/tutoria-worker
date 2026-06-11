"""
Daily AI Summary Worker — runs daily at 4:00 AM (after the analytics
aggregation jobs at 2:30–3:30) and writes one AI-authored briefing per
university into DailyAISummaries, but ONLY for universities that had new
activity the previous day. Idempotent: skips (university, date) pairs that
already have a summary.
"""
import asyncio
import json
import logging
from datetime import date, timedelta

from app.core.database import SessionLocal
from app.models import (
    AnalyticsDailySummary,
    Course,
    DailyAISummary,
    Module,
    QuizAnalytic,
    TopicClassification,
    University,
)
from app.workers.scheduling import seconds_until_next_run

logger = logging.getLogger(__name__)

RUN_HOUR = 4  # 4:00 AM container time (after aggregation jobs)


def _build_prompt(university_name: str, day: date, stats: dict) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the daily briefing."""
    system_prompt = (
        "Você é um analista educacional escrevendo um briefing diário curto para "
        "gestores de uma instituição de ensino que usa tutoria por IA. "
        "Escreva em português (Brasil), tom profissional e direto. "
        "Responda APENAS com JSON válido no formato: "
        '{"summary": "<um parágrafo de 3-5 frases>", "highlights": ["<3 a 5 bullets curtos e acionáveis>"]}'
    )

    user_prompt = f"""Dados de {day.isoformat()} da instituição "{university_name}":

- Total de perguntas dos alunos: {stats['total_questions']}
- Alunos ativos (soma por módulo): {stats['total_students']}
- Módulos mais ativos: {json.dumps(stats['top_modules'], ensure_ascii=False)}
- Tópicos mais perguntados: {json.dumps(stats['top_topics'], ensure_ascii=False)}
- Conceitos com pior desempenho em quizzes (taxa de acerto %): {json.dumps(stats['worst_concepts'], ensure_ascii=False)}

Escreva o briefing destacando: volume e engajamento, onde os alunos estão com mais dúvidas,
conceitos que merecem reforço em aula, e qualquer sinal de atenção. Não invente dados."""

    return system_prompt, user_prompt


async def _generate_for_university(db, university: University, day: date, module_ids: list[int]) -> bool:
    """Generate and store one summary. Returns True if written."""
    # Idempotency
    existing = (
        db.query(DailyAISummary)
        .filter(DailyAISummary.university_id == university.id, DailyAISummary.date == day)
        .first()
    )
    if existing:
        return False

    rows = (
        db.query(AnalyticsDailySummary)
        .filter(AnalyticsDailySummary.date == day, AnalyticsDailySummary.module_id.in_(module_ids))
        .all()
    )
    if not rows or sum(r.question_count for r in rows) == 0:
        return False  # no new data — no summary

    modules = {m.id: m for m in db.query(Module).filter(Module.id.in_(module_ids)).all()}
    courses = {c.id: c for c in db.query(Course).filter(Course.university_id == university.id).all()}

    def module_label(module_id: int) -> str:
        module = modules.get(module_id)
        if not module:
            return f"Módulo {module_id}"
        course = courses.get(module.course_id)
        return f"{course.name} — {module.name}" if course else module.name

    top_modules = [
        {"modulo": module_label(r.module_id), "perguntas": r.question_count, "alunos": r.unique_students}
        for r in sorted(rows, key=lambda r: r.question_count, reverse=True)[:5]
    ]

    topics = (
        db.query(TopicClassification)
        .filter(TopicClassification.date == day, TopicClassification.module_id.in_(module_ids))
        .all()
    )
    topic_totals: dict[str, int] = {}
    for topic in topics:
        topic_totals[topic.topic_name] = topic_totals.get(topic.topic_name, 0) + topic.question_count
    top_topics = [
        {"topico": name, "perguntas": count}
        for name, count in sorted(topic_totals.items(), key=lambda kv: kv[1], reverse=True)[:6]
    ]

    quiz_rows = (
        db.query(QuizAnalytic)
        .filter(QuizAnalytic.module_id.in_(module_ids), QuizAnalytic.total_attempts >= 3)
        .order_by(QuizAnalytic.success_rate.asc())
        .limit(5)
        .all()
    )
    worst_concepts = [
        {"conceito": q.concept_name, "taxa_acerto": float(q.success_rate), "tentativas": q.total_attempts}
        for q in quiz_rows
        if float(q.success_rate) < 75
    ]

    stats = {
        "total_questions": sum(r.question_count for r in rows),
        "total_students": sum(r.unique_students for r in rows),
        "top_modules": top_modules,
        "top_topics": top_topics,
        "worst_concepts": worst_concepts,
    }

    system_prompt, user_prompt = _build_prompt(university.name, day, stats)

    from app.services.grading_service import _call_ai
    raw = await _call_ai(system_prompt, user_prompt, db)

    # Parse the JSON response (tolerate code fences)
    summary_text = raw.strip()
    highlights: list[str] = []
    try:
        cleaned = summary_text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned.removeprefix("json").strip()
        parsed = json.loads(cleaned)
        summary_text = str(parsed.get("summary", "")).strip() or raw.strip()
        highlights = [str(h) for h in parsed.get("highlights", []) if str(h).strip()][:6]
    except Exception:
        logger.warning("AI summary response was not valid JSON — storing raw text")

    db.add(DailyAISummary(
        university_id=university.id,
        date=day,
        summary_text=summary_text,
        highlights_json=json.dumps(highlights, ensure_ascii=False) if highlights else None,
        provider="chain",
    ))
    db.commit()
    logger.info("✅ Daily AI summary written — university=%s date=%s", university.name, day)
    return True


async def generate_daily_summaries() -> None:
    """One pass: summaries for yesterday, for every university with activity."""
    yesterday = date.today() - timedelta(days=1)
    db = SessionLocal()
    try:
        universities = db.query(University).all()
        written = 0
        for university in universities:
            try:
                module_ids = [
                    row[0]
                    for row in db.query(Module.id)
                    .join(Course, Module.course_id == Course.id)
                    .filter(Course.university_id == university.id)
                    .all()
                ]
                if not module_ids:
                    continue
                if await _generate_for_university(db, university, yesterday, module_ids):
                    written += 1
            except Exception as exc:
                db.rollback()
                logger.error("Daily AI summary failed for university %s: %s", university.id, exc, exc_info=True)
        logger.info("🧠 Daily AI summary pass complete — %s summaries written for %s", written, yesterday)
    finally:
        db.close()


async def run_ai_summary_worker() -> None:
    """Daily loop at RUN_HOUR (container time, after the aggregation jobs)."""
    logger.info("🧠 Daily AI summary worker started (runs daily at %02d:00)", RUN_HOUR)
    while True:
        try:
            await asyncio.sleep(seconds_until_next_run(RUN_HOUR))
            await generate_daily_summaries()
        except asyncio.CancelledError:
            logger.info("🛑 Daily AI summary worker cancelled")
            return
        except Exception as exc:
            logger.error("Daily AI summary worker error: %s — retrying tomorrow", exc, exc_info=True)
