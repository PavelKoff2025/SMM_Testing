"""
Общие запросы к БД, переиспользуемые из роутов (Этап 3).

Вынесены сюда, чтобы не дублировать логику «доступные тесты» между
главной страницей (main.py) и кабинетом студента (routers/student.py).

Правило доступности теста (из README, жизненный цикл):
  • status == open                       — доступен;
  • status == scheduled и время наступило — доступен;
  • draft / scheduled (время не пришло)   — скрыт от студентов.

Поточный режим с таймером (День 1): хелперы попыток — старт, кулдаун,
сохранение ответа, финализация (completed/timed_out), лучшая попытка.
Все времена — наивный UTC (консистентно с datetime.utcnow() по проекту).
"""
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

import config
from app.models import (
    Answer,
    Attempt,
    AttemptStatus,
    Question,
    Test,
    TestStatus,
)


def list_available_tests(db: Session) -> list[Test]:
    """Все тесты, которые студент сейчас может проходить, по порядку id."""
    now = datetime.utcnow()
    return (
        db.query(Test)
        .filter(
            (Test.status == TestStatus.open)
            | (
                (Test.status == TestStatus.scheduled)
                & (Test.scheduled_at.isnot(None))
                & (Test.scheduled_at <= now)
            )
        )
        .order_by(Test.id)
        .all()
    )


def is_test_available(test: Test) -> bool:
    """Доступен ли конкретный тест студенту прямо сейчас."""
    if test.status == TestStatus.open:
        return True
    if (
        test.status == TestStatus.scheduled
        and test.scheduled_at is not None
        and test.scheduled_at <= datetime.utcnow()
    ):
        return True
    return False


# === Поточный режим: попытки, таймер, кулдаун ===

def next_attempt_number(db: Session, student_id: int, test_id: int) -> int:
    """Порядковый номер следующей попытки: max(attempt_number)+1, минимум 1."""
    last = (
        db.query(func.max(Attempt.attempt_number))
        .filter(Attempt.student_id == student_id, Attempt.test_id == test_id)
        .scalar()
    )
    return int(last or 0) + 1


def get_cooldown(db: Session, student_id: int, test_id: int) -> datetime | None:
    """Активный кулдаун: max(cooldown_until) среди timed_out попыток, если > now.

    Возвращает момент (наивный UTC), до которого нельзя начать новую попытку,
    или None, если кулдауна нет. Кулдаун ставится ТОЛЬКО при таймауте.
    Берём максимум по всем timed_out попыткам — на случай нескольких (хотя
    после первой timed_out следующая и так ждёт кулдаун, так что максимум
    совпадает с последней; max — подстраховка от ручных правок БД).
    """
    latest = (
        db.query(func.max(Attempt.cooldown_until))
        .filter(
            Attempt.student_id == student_id,
            Attempt.test_id == test_id,
            Attempt.status == AttemptStatus.timed_out,
        )
        .scalar()
    )
    if latest is None:
        return None
    return latest if latest > datetime.utcnow() else None


def start_attempt(db: Session, student: object, test: Test) -> Attempt:
    """Создать новую попытку in_progress с deadline = now + time_limit.

    Вызывающий обязан до этого убедиться, что кулдауна нет (get_cooldown == None)
    и нет уже идущей in_progress попытки (get_in_progress == None).
    started_at и deadline задаём явно в Python, чтобы не зависеть от
    server_default=func.now() и получить согласованную пару.
    """
    now = datetime.utcnow()
    attempt = Attempt(
        student_id=student.id,
        test_id=test.id,
        attempt_number=next_attempt_number(db, student.id, test.id),
        status=AttemptStatus.in_progress,
        score=0,
        passed=False,
        started_at=now,
        deadline=now + timedelta(seconds=test.time_limit_seconds),
        finished_at=None,
        cooldown_until=None,
    )
    db.add(attempt)
    db.flush()  # нужен attempt.id до возможной вставки ответов
    return attempt


def get_in_progress(db: Session, student_id: int, test_id: int) -> Attempt | None:
    """Текущая незавершённая попытка (in_progress) — её можно «продолжить»."""
    return (
        db.query(Attempt)
        .filter(
            Attempt.student_id == student_id,
            Attempt.test_id == test_id,
            Attempt.status == AttemptStatus.in_progress,
        )
        .order_by(Attempt.attempt_number.desc())
        .first()
    )


def last_answered_number(attempt: Attempt) -> int:
    """Номер последнего отвеченного вопроса (max question_number в answers) или 0.

    Forward-only: следующий доступный вопрос = last_answered_number + 1.
    """
    if not attempt.answers:
        return 0
    return max(a.question_number for a in attempt.answers)


def is_deadline_passed(attempt: Attempt, now: datetime | None = None) -> bool:
    """Истёк ли лимит времени (серверная проверка, не зависит от клиента)."""
    if attempt.deadline is None:
        return False
    return (now or datetime.utcnow()) > attempt.deadline


def save_answer(
    db: Session, attempt: Attempt, question: Question, chosen_index: int
) -> Answer:
    """Сохранить/обновить ответ на вопрос с мгновенным вычислением is_correct.

    Upsert: если ответ на этот вопрос уже есть (повторный POST, двойной клик) —
    обновляем, иначе создаём. Жёсткая валидация индекса — на стороне роута.
    """
    existing = (
        db.query(Answer)
        .filter(
            Answer.attempt_id == attempt.id,
            Answer.question_number == question.number,
        )
        .one_or_none()
    )
    is_correct = chosen_index == question.correct_answer
    if existing is not None:
        existing.student_answer = chosen_index
        existing.is_correct = is_correct
        db.flush()
        return existing
    answer = Answer(
        attempt_id=attempt.id,
        question_number=question.number,
        difficulty=question.difficulty,
        student_answer=chosen_index,
        is_correct=is_correct,
    )
    db.add(answer)
    db.flush()
    return answer


def finalize_timed_out(attempt: Attempt, now: datetime | None = None) -> None:
    """Закрыть попытку по таймауту: status=timed_out, выставить кулдаун.

    finished_at = deadline (момент, когда время вышло — честнее, чем «now»,
    который может быть сильно позже из-за ленивой проверки). cooldown_until =
    deadline + test.cooldown_seconds. score/passed пересчитываются из ответов,
    чтобы студент видел честный результат даже при таймауте (неотвеченные = 0).
    Вызывающий делает commit.
    """
    now = now or datetime.utcnow()
    attempt.status = AttemptStatus.timed_out
    attempt.finished_at = attempt.deadline or now
    attempt.cooldown_until = (attempt.deadline or now) + timedelta(
        seconds=attempt.test.cooldown_seconds
    )
    _recompute_score(attempt)


def finalize_completed(attempt: Attempt, now: datetime | None = None) -> None:
    """Завершить попытку студентом: status=completed, score/passed из ответов."""
    now = now or datetime.utcnow()
    attempt.status = AttemptStatus.completed
    attempt.finished_at = now
    _recompute_score(attempt)


def _recompute_score(attempt: Attempt) -> None:
    """Пересчитать score/passed из Answer-строк (неотвеченные = неверные)."""
    attempt.score = sum(1 for a in attempt.answers if a.is_correct)
    attempt.passed = attempt.score >= attempt.test.pass_threshold


def get_best_attempt(db: Session, student_id: int, test_id: int) -> Attempt | None:
    """Лучшая попытка по тесту (max score) среди завершённых/таймаутных.

    Используется в кабинете и допуске. in_progress не учитываются — у них
    ещё не финализирован score. При равном score берём последнюю по времени.
    """
    return (
        db.query(Attempt)
        .filter(
            Attempt.student_id == student_id,
            Attempt.test_id == test_id,
            Attempt.status.in_([AttemptStatus.completed, AttemptStatus.timed_out]),
        )
        .order_by(Attempt.score.desc(), Attempt.finished_at.desc().nullslast())
        .first()
    )


__all__ = [
    "list_available_tests",
    "is_test_available",
    "next_attempt_number",
    "get_cooldown",
    "start_attempt",
    "get_in_progress",
    "last_answered_number",
    "is_deadline_passed",
    "save_answer",
    "finalize_timed_out",
    "finalize_completed",
    "get_best_attempt",
]