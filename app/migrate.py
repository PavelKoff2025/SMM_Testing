"""
Идемпотентная миграция схемы SQLite (без Alembic).

В проекте нет Alembic — таблицы создаются Base.metadata.create_all, а миграции
накатываются вручную при старте (lifespan в main.py):

  • новые колонки на tests — через ALTER TABLE ADD COLUMN (SQLite умеет
    additive-добавление; данные не трогаются);
  • удаление UNIQUE(student_id, test_id) с attempts — SQLite НЕ умеет
    DROP CONSTRAINT, поэтому attempts пересоздаётся с копированием данных,
    если обнаружен старый констрейнт uq_student_test.

Старые попытки (при старой one-shot модели все были завершёнными) при
пересоздании помечаются: attempt_number=1 (старый констрейнт гарантировал
≤1 попытки на студент+тест), status='completed' (у них был finished_at).
deadline/cooldown_until — NULL (для завершённых попыток не нужны).

Все операции идемпотентны: повторный запуск ничего не ломает. Фолбэк для
dev-данных — `python -m app.seed --reset` (полная пересборка БД).
"""
import logging

from sqlalchemy import inspect, text

import config
from app.database import engine

logger = logging.getLogger("smm.migrate")


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def _has_unique_constraint(inspector, table: str, name: str) -> bool:
    """Есть ли UNIQUE-констрейнт с заданным именем (старая схема attempts)."""
    try:
        constraints = inspector.get_unique_constraints(table)
    except Exception:
        return False
    return any(c.get("name") == name for c in constraints)


def _add_column(table: str, column: str, ddl: str) -> None:
    """ADD COLUMN по одной (SQLite не требует, но так нагляднее и безопаснее)."""
    logger.info("Миграция: %s.%s — добавляю колонку", table, column)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def _recreate_attempts_without_unique() -> None:
    """Пересоздать attempts без UNIQUE(student_id, test_id), сохранив данные.

    Новый DDL соответствует модели (см. app/models.py): без констрейнта, со
    всеми новыми колонками. Типы приведены к тем, что создаёт SQLAlchemy в
    SQLite (SAEnum → VARCHAR), чтобы не было рассинхрона с ORM.
    """
    logger.info("Миграция: пересоздаю attempts без uq_student_test (перенос данных)")
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE attempts_new (
                id INTEGER PRIMARY KEY,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
                attempt_number INTEGER NOT NULL DEFAULT 1,
                status VARCHAR(20) NOT NULL DEFAULT 'in_progress',
                score INTEGER NOT NULL DEFAULT 0,
                passed BOOLEAN NOT NULL DEFAULT 0,
                started_at DATETIME NOT NULL,
                deadline DATETIME,
                finished_at DATETIME,
                cooldown_until DATETIME
            )
            """
        ))
        # Старые попытки все завершённые (finished_at был проставлен в submit).
        # attempt_number=1 — при старом UNIQUE констрейнте у пары студент+тест
        # не могло быть больше одной попытки.
        conn.execute(text(
            """
            INSERT INTO attempts_new
                (id, student_id, test_id, attempt_number, status,
                 score, passed, started_at, finished_at)
            SELECT id, student_id, test_id, 1, 'completed',
                   score, passed, started_at, finished_at
            FROM attempts
            """
        ))
        conn.execute(text("DROP TABLE attempts"))
        conn.execute(text("ALTER TABLE attempts_new RENAME TO attempts"))
    logger.info("Миграция: attempts пересоздана, данные перенесены")


def _migrate_tests(inspector) -> None:
    cols = _columns(inspector, "tests")
    if "time_limit_seconds" not in cols:
        _add_column(
            "tests", "time_limit_seconds",
            f"time_limit_seconds INTEGER NOT NULL DEFAULT {config.TEST_TIME_LIMIT_SECONDS}",
        )
    if "cooldown_seconds" not in cols:
        _add_column(
            "tests", "cooldown_seconds",
            f"cooldown_seconds INTEGER NOT NULL DEFAULT {config.TEST_COOLDOWN_SECONDS}",
        )


def _migrate_attempts(inspector) -> None:
    # Если ещё висит старый UNIQUE — пересоздаём таблицу (это покрывает и новые
    # колонки, и удаление констрейнта за один шаг). Иначе — additive-добавление
    # недостающих колонок (нормальный путь для свежей/уже-мигрированной БД).
    if _has_unique_constraint(inspector, "attempts", "uq_student_test"):
        _recreate_attempts_without_unique()
        return

    cols = _columns(inspector, "attempts")
    if "attempt_number" not in cols:
        _add_column("attempts", "attempt_number", "attempt_number INTEGER NOT NULL DEFAULT 1")
    if "status" not in cols:
        _add_column("attempts", "status", "status VARCHAR(20) NOT NULL DEFAULT 'in_progress'")
    if "deadline" not in cols:
        _add_column("attempts", "deadline", "deadline DATETIME")
    if "cooldown_until" not in cols:
        _add_column("attempts", "cooldown_until", "cooldown_until DATETIME")


def run_migrations() -> None:
    """Точка входа: вызывается из lifespan после Base.metadata.create_all.

    create_all только создаёт недостающие таблицы — оно не меняет существующие.
    Поэтому миграции (новые колонки, снятие UNIQUE) делаем здесь вручную.
    Если таблиц ещё нет (совсем свежий запуск), create_all уже создал их по
    новой модели — проверка колонок/констрейнтов корректно пропустит работу.
    """
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    if "tests" in existing:
        _migrate_tests(inspector)
    if "attempts" in existing:
        _migrate_attempts(inspector)
    logger.info("Миграция схемы завершена")


__all__ = ["run_migrations"]