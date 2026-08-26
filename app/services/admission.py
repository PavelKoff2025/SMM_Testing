"""
Итоговый допуск к финальному зачёту (Этап 9).

Критерий допуска (из README, пороги в config):
  • пройдено тестов >= ADMISSION_TESTS_REQUIRED (9);
  • правильных ответов суммарно >= ADMISSION_CORRECT_REQUIRED (81 из 90).
Оба условия одновременно → «допущен», иначе «не допущен».

Поточный режим с перепрохождением (День 1): несколько попыток на тест.
В зачёт идёт ЛУЧШАЯ попытка по каждому тесту: total_correct = Σ по тестам
max(score); taken = число тестов с ≥1 завершённой/таймаутной попыткой;
passed = число тестов, где лучшая попытка ≥ pass_threshold. in_progress
попытки не учитываются (score ещё не финализирован).

Запрос — два уровня: подзапрос «лучшая попытка на студент+тест», затем
агрегат по студентам с LEFT JOIN к нему (студенты без попыток попадают в
отчёт с taken=0, «не допущен» — преподаватель видит и тех, кто не начинал).
"""
from collections import namedtuple

from sqlalchemy import func
from sqlalchemy.orm import Session

import config
from app.models import Attempt, AttemptStatus, Student


AdmissionRow = namedtuple(
    "AdmissionRow",
    [
        "student_id",
        "first_name",
        "last_name",
        "group",
        "email",
        "taken",        # пройдено тестов (с ≥1 завершённой попыткой)
        "passed",       # зачтено тестов (лучшая попытка ≥ pass_threshold)
        "total_correct",  # суммарно правильных (по лучшим попыткам)
        "attempts_count",  # всего завершённых/таймаутных попыток (День 3)
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
    # Лучшая попытка на (студент, тест) среди завершённых/таймаутных.
    # best_score = max(score); best_passed = max(passed) — корректно отражает
    # «зачёл ли лучший результат порог»: если максимум score ≥ pass_threshold,
    # то именно у той попытки passed=True, значит max(passed)=1.
    best = (
        db.query(
            Attempt.student_id.label("sid"),
            Attempt.test_id.label("tid"),
            func.max(Attempt.score).label("best_score"),
            func.max(Attempt.passed).label("best_passed"),
        )
        .filter(Attempt.status.in_([AttemptStatus.completed, AttemptStatus.timed_out]))
        .group_by(Attempt.student_id, Attempt.test_id)
        .subquery()
    )

    # Всего попыток по студенту (завершённых/таймаутных) — колонка «Попыток»:
    # преподаватель видит, кто перепроходил. Считаем отдельно, не по best.
    att_count = (
        db.query(
            Attempt.student_id.label("sid"),
            func.count(Attempt.id).label("cnt"),
        )
        .filter(Attempt.status.in_([AttemptStatus.completed, AttemptStatus.timed_out]))
        .group_by(Attempt.student_id)
        .subquery()
    )

    rows = (
        db.query(
            Student.id,
            Student.first_name,
            Student.last_name,
            Student.group,
            Student.email,
            func.count(best.c.tid).label("taken"),  # COUNT ненулевых tid
            func.coalesce(func.sum(best.c.best_passed), 0).label("passed"),
            func.coalesce(func.sum(best.c.best_score), 0).label("total_correct"),
            func.coalesce(att_count.c.cnt, 0).label("attempts_count"),
        )
        .outerjoin(best, best.c.sid == Student.id)
        .outerjoin(att_count, att_count.c.sid == Student.id)
        .group_by(Student.id)
        .all()
    )

    result = []
    for sid, fn, ln, group, email, taken, passed, total, att_cnt in rows:
        taken = int(taken or 0)
        passed = int(passed or 0)
        total = int(total or 0)
        att_cnt = int(att_cnt or 0)
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
                attempts_count=att_cnt,
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