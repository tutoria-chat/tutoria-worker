"""Generating questions for a module adds to its bank: it never deletes the
questions already there, and it tells the AI which ones exist so a second
run brings new questions instead of repeats."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import QuizDifficulty
from app.services import quiz_generator
from app.services.quiz_generator import QuizGeneratorService


def _module():
    return SimpleNamespace(
        id=7, course_id=3, name="Mod", description="", tutor_language="pt-br",
        files=[SimpleNamespace(is_active=True, name="a.pdf")],
    )


def _db(existing_texts, max_number=12):
    db = MagicMock()
    texts_query = MagicMock()
    texts_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
        (t,) for t in existing_texts
    ]
    numbers_query = MagicMock()
    numbers_query.filter.return_value.order_by.return_value.first.return_value = (max_number,)
    db.query.side_effect = [texts_query, numbers_query]
    return db, texts_query


def test_generating_again_keeps_existing_questions_and_avoids_them():
    db, texts_query = _db(["Qual é a capital?", "O que é um átomo?"])
    service = QuizGeneratorService(_module(), db)
    prompts = []

    async def fake_generate(difficulty, count, start_number):
        prompts.append(service._build_generation_prompt(difficulty, count))
        return [SimpleNamespace(question_text=f"Nova {difficulty.value} {start_number}")]

    context = MagicMock()
    context.build_context_for_prompt = AsyncMock(return_value={"context": "material", "files_used": ["a.pdf"]})
    with patch.object(quiz_generator, "DocumentContextService", return_value=context), \
         patch.object(service, "_generate_questions_by_difficulty", side_effect=fake_generate):
        quizzes = asyncio.run(service.generate_quiz_bank(count=10))

    assert len(quizzes) == 3
    # Only the two reads (existing texts, highest number): no delete query.
    assert db.query.call_count == 2
    texts_query.filter.return_value.delete.assert_not_called()
    # Every level is told about the old questions; later levels also about the
    # ones generated earlier in the same run.
    assert all("Qual é a capital?" in p and "O que é um átomo?" in p for p in prompts)
    assert "Nova easy 13" in prompts[1] and "Nova medium 17" in prompts[2]
    assert "QUESTÕES QUE JÁ EXISTEM" in prompts[0]


def test_first_generation_has_no_avoid_list():
    service = QuizGeneratorService(_module(), MagicMock())
    prompt = service._build_generation_prompt(QuizDifficulty.EASY, 4)
    assert "JÁ EXISTEM" not in prompt


def test_avoid_list_is_bounded():
    service = QuizGeneratorService(_module(), MagicMock())
    service._existing_questions = [f"q{i}" for i in range(1000)]
    prompt = service._build_generation_prompt(QuizDifficulty.HARD, 4)
    assert prompt.count("\n- q") == quiz_generator._MAX_EXISTING_QUESTIONS
