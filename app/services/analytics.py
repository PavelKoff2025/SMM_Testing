"""
Аналитика для кабинета преподавателя (Этап 8).

Три запроса:
  • attempts_summary  — сводная таблица всех попыток (студент + тест + оценка
                         + число ошибок), для страницы /teacher/analytics.
  • difficulty_error_stats — агрегат ошибок по сложности (easy/medium/logic):
                         на каких уровнях студенты ошибаются чаще.
  • attempt_breakdown — построчный разбор одной попытки (для /teacher/attempt/{id}),
                         по формату совпадает с rows в routers/student.py (result.html).

Ключевое решение: агрегат по сложности считается прямо по таблице Answer —
в ней с этапа 1 хранится denormalized `difficulty` (индекс сложности копируется
из Question при сохранении ответа). Поэтому JOIN с Question не нужен, запрос
простой и быстрый даже на полной таблице ответов.
"""
from collections import namedtuple
from datetime import datetime

from sqlalchemy import func, case
from sqlalchemy.orm import Session

from app.models import Answer, Attempt, Student, Test


# === Сводная таблица попыток ===

AttemptRow = namedtuple(
    "AttemptRow",
    ["id", "student_name", "group", "lecture_title", "test_id",
     "attempt_number", "status", "score", "pass_threshold", "passed",
     "finished_at", "wrong_count"],
)


def attempts_summary(db: Session) -> list[AttemptRow]:
    """Все попытки одной плоской таблицей, свежие сверху.

    Число неверных ответов считаем подзапросом по Answer — дешевле, чем
    тянуть все ответы в память и фильтровать в Python. Передаём attempt_number
    и status — преподаватель видит, какая по счёту попытка и завершилась ли
    она по таймауту (День 3).
    """
    wrong_subq = (
        db.query(
            Answer.attempt_id,
            func.count().label("wrong"),
        )
        .filter(Answer.is_correct == False)  # noqa: E712
        .group_by(Answer.attempt_id)
        .subquery()
    )

    rows = (
        db.query(
            Attempt.id,
            Student.first_name,
            Student.last_name,
            Student.group,
            Test.lecture_title,
            Test.id,
            Attempt.attempt_number,
            Attempt.status,
            Attempt.score,
            Test.pass_threshold,
            Attempt.passed,
            Attempt.finished_at,
            func.coalesce(wrong_subq.c.wrong, 0),
        )
        .join(Student, Attempt.student_id == Student.id)
        .join(Test, Attempt.test_id == Test.id)
        .outerjoin(wrong_subq, wrong_subq.c.attempt_id == Attempt.id)
        .order_by(Attempt.finished_at.desc().nullslast(), Attempt.id.desc())
        .all()
    )

    return [
        AttemptRow(
            id=r[0],
            student_name=f"{r[2]} {r[1]}",  # Фамилия Имя — привычный порядок
            group=r[3],
            lecture_title=r[4],
            test_id=r[5],
            attempt_number=r[6],
            status=r[7].value if r[7] is not None else "",
            score=r[8],
            pass_threshold=r[9],
            passed=r[10],
            finished_at=r[11],
            wrong_count=r[12],
        )
        for r in rows
    ]


# === Агрегат ошибок по сложности ===

DiffStat = namedtuple("DiffStat", ["difficulty", "total", "wrong", "error_rate"])


def difficulty_error_stats(db: Session) -> dict[str, DiffStat]:
    """Ошибки по уровням сложности: {easy/medium/logic: DiffStat}.

    Гарантирует наличие всех трёх ключей, даже если по какому-то уровню
    ответов ещё нет (total=0, error_rate=0) — шаблон не падает на пустой БД.
    """
    # case(... else_=0) — суммируем только неверные; func.count — всего ответов.
    wrong_expr = case((Answer.is_correct == False, 1), else_=0)  # noqa: E712
    rows = (
        db.query(
            Answer.difficulty,
            func.count().label("total"),
            func.sum(wrong_expr).label("wrong"),
        )
        .group_by(Answer.difficulty)
        .all()
    )

    # Ответы хранятся как enum-значения (Difficulty). Ключи — строки.
    result: dict[str, DiffStat] = {}
    for diff_enum, total, wrong in rows:
        total = int(total or 0)
        wrong = int(wrong or 0)
        rate = round(wrong / total * 100, 1) if total else 0.0
        result[diff_enum.value] = DiffStat(diff_enum.value, total, wrong, rate)

    # Заполняем недостающие уровни нулями (на случай пустой/неполной БД).
    for key in ("easy", "medium", "logic"):
        result.setdefault(key, DiffStat(key, 0, 0, 0.0))
    return result


# === Разбор одной попытки ===

BreakdownRow = namedtuple(
    "BreakdownRow",
    ["number", "text", "difficulty", "student_answer", "correct_answer", "is_correct"],
)


def attempt_breakdown(attempt: Attempt) -> list[BreakdownRow]:
    """Построчный разбор попытки: выбор студента vs правильный ответ.

    Формат совпадает с rows в routers/student.py (result.html), поэтому
    teacher_attempt.html использует те же поля/классы. Не требует db —
    attempt.answers и attempt.test.questions загружаются через relationship.
    """
    test = attempt.test
    questions = {q.number: q for q in test.questions}

    rows = []
    for a in sorted(attempt.answers, key=lambda x: x.question_number):
        q = questions.get(a.question_number)
        if q is None:
            continue
        student_txt = q.options[a.student_answer] if 0 <= a.student_answer < len(q.options) else "—"
        correct_txt = q.options[q.correct_answer] if 0 <= q.correct_answer < len(q.options) else "—"
        rows.append(
            BreakdownRow(
                number=a.question_number,
                text=q.text,
                difficulty=a.difficulty.value,
                student_answer=student_txt,
                correct_answer=correct_txt,
                is_correct=a.is_correct,
            )
        )
    return rows


__all__ = [
    "AttemptRow",
    "attempts_summary",
    "DiffStat",
    "difficulty_error_stats",
    "BreakdownRow",
    "attempt_breakdown",
]