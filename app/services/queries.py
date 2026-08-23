"""
Общие запросы к БД, переиспользуемые из роутов (Этап 3).

Вынесены сюда, чтобы не дублировать логику «доступные тесты» между
главной страницей (main.py) и кабинетом студента (routers/student.py).

Правило доступности теста (из README, жизненный цикл):
  • status == open                       — доступен;
  • status == scheduled и время наступило — доступен;
  • draft / scheduled (время не пришло)   — скрыт от студентов.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Test, TestStatus


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


__all__ = ["list_available_tests", "is_test_available"]