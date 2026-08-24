"""
Итоговый допуск к финальному зачёту (Этап 9).

Критерий допуска (из README, пороги в config):
  • пройдено тестов >= ADMISSION_TEST_REQUIRED (9);
  • правильных ответов суммарно >= ADMISSION_CORRECT_REQUIRED (81 из 90).
Оба условия одновременно → «допущен», иначе «не допущен».

Одна попытка на тест заложена в моделях (UniqueConstraint student_id+test_id),
поэтому число попыток студента = число пройденных им тестов.

Запрос — один агрегат по студентам с LEFT JOIN Attempt: студенты без попыток
тоже попадают в отчёт (пройдено 0, «не допущен») — так преподаватель видит
и тех, кто ещё не начинал.
"""
from collections import namedtuple

from sqlalchemy import case, func
from sqlalchemy.orm import Session

import config
from app.models import Attempt, Student


AdmissionRow = namedtuple(
    "AdmissionRow",
    [
        "student_id",
        "first_name",
        "last_name",
        "group",
        "email",
        "taken",        # пройдено тестов (число попыток)
        "passed",       # зачтено тестов
        "total_correct",  # суммарно правильных ответов
        "admitted",     # bool — допущен/не допущен
    ],
)


def _is_admitted(taken: int, total_correct: int) -> bool:
    """Допущен, если выполнены оба пороговых условия из конфига."""
    return (
        taken >= config.ADMISSION_TESTS_REQUIRED
        and total_correct >= config.ADMISSION_CORRECT_REQUIRED
    )


def admission_report(db: Session) -> list[AdmissionRow]:
    """Сводка по каждому студенту: прогресс по тестам + статус допуска.

    Сортировка: сначала не допущенные (чтобы обратить внимание), затем по фамилии.
    На защите нагляднее, когда «проблемные» студенты наверху.
    """
    passed_expr = case((Attempt.passed == True, 1), else_=0)  # noqa: E712

    rows = (
        db.query(
            Student.id,
            Student.first_name,
            Student.last_name,
            Student.group,
            Student.email,
            func.count(Attempt.id).label("taken"),
            func.coalesce(func.sum(passed_expr), 0).label("passed"),
            func.coalesce(func.sum(Attempt.score), 0).label("total_correct"),
        )
        .outerjoin(Attempt, Attempt.student_id == Student.id)
        .group_by(Student.id)
        .all()
    )

    result = []
    for sid, fn, ln, group, email, taken, passed, total in rows:
        taken = int(taken or 0)
        passed = int(passed or 0)
        total = int(total or 0)
        result.append(
            AdmissionRow(
                student_id=sid,
                first_name=fn,
                last_name=ln,
                group=group,
                email=email,
                taken=taken,
                passed=passed,
                total_correct=total,
                admitted=_is_admitted(taken, total),
            )
        )

    # Не допущенные — первыми, внутри — по фамилии.
    result.sort(key=lambda r: (r.admitted, r.last_name, r.first_name))
    return result


def admission_summary(report: list[AdmissionRow]) -> dict[str, int]:
    """Сводка по отчёту: всего студентов / допущено / не допущено."""
    total = len(report)
    admitted = sum(1 for r in report if r.admitted)
    return {
        "total": total,
        "admitted": admitted,
        "not_admitted": total - admitted,
    }


__all__ = [
    "AdmissionRow",
    "admission_report",
    "admission_summary",
    "_is_admitted",
]