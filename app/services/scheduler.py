"""
Расписание доступа к тестам (Этап 7): APScheduler переводит тесты
из статуса `scheduled` в `open`, когда наступает заданное время.

Архитектурные решения (согласовано с автором):
  • Источник правды — наша БД (tests.scheduled_at), а не внутреннее
    хранилище APScheduler. Поэтому используем MemoryJobStore (дефолт):
    перезапуск приложения ничего не теряет — при старте запускаем
    «догоняющую» проверку и открываем всё, чьё время уже пришло.
  • Время хранится и сравнивается в UTC (наивный UTC, консистентно с
    datetime.utcnow() в queries.py / student.py). Преподаватель вводит
    время по Москве — конвертация Europe/Moscow ↔ UTC через zoneinfo.
  • `queries.py` дополнительно считает scheduled-тест с наступившим
    временем доступным — это подстраховка в окне до 30 сек между тиками
    планировщика. Здесь мы делаем настоящий перевод статуса scheduled→open.

Запуск/останов — через lifespan FastAPI (см. app/main.py).
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

import config
from app.database import SessionLocal
from app.models import Test, TestStatus

logger = logging.getLogger("smm.scheduler")

# Планировщик — один экземпляр на всё приложение.
_scheduler: AsyncIOScheduler | None = None


# === Временные зоны ===

def _tz() -> ZoneInfo:
    """Зона ввода/вывода для преподавателя (по умолчанию Москва)."""
    return ZoneInfo(config.SCHEDULE_TZ)


def local_to_utc(naive_local: datetime) -> datetime:
    """Наивное локальное время (от формы datetime-local) → наивный UTC.

    Храним наивный UTC, чтобы сравнения с datetime.utcnow() работали напрямую.
    """
    aware_local = naive_local.replace(tzinfo=_tz())
    aware_utc = aware_local.astimezone(timezone.utc)
    # Сбрасываем tzinfo — в БД и в сравнениях храним наивный UTC.
    return aware_utc.replace(tzinfo=None)


def utc_to_local(naive_utc: datetime) -> datetime:
    """Наивный UTC → наивное локальное время (для показа преподавателю)."""
    aware_utc = naive_utc.replace(tzinfo=timezone.utc)
    return aware_utc.astimezone(_tz()).replace(tzinfo=None)


# === Перевод scheduled → open ===

def open_due_scheduled_tests(db: Session) -> int:
    """Открыть все тесты в статусе scheduled, чьё время наступило.

    Возвращает число переведённых тестов. Используется и как регулярная
    задача планировщика, и как «догоняющая» проверка при старте приложения.
    Тест обязан иметь 10 вопросов (schedule это гарантирует, но проверяем
    на случай ручных правок БД — не открываем «пустой» тест).
    """
    now = datetime.utcnow()
    due = (
        db.query(Test)
        .filter(
            Test.status == TestStatus.scheduled,
            Test.scheduled_at.isnot(None),
            Test.scheduled_at <= now,
        )
        .all()
    )
    opened = 0
    for test in due:
        if len(test.questions) == config.QUESTIONS_PER_TEST:
            test.status = TestStatus.open
            test.scheduled_at = None
            opened += 1
            logger.info("Автооткрытие: тест #%d «%s»", test.id, test.lecture_title)
        else:
            # Расписание повисло на тесте без вопросов — откатываем в draft,
            # чтобы он не «застрял» в scheduled навсегда.
            test.status = TestStatus.draft
            test.scheduled_at = None
            logger.warning(
                "Автооткрытие отменено: тест #%d «%s» без %d вопросов → draft",
                test.id,
                test.lecture_title,
                config.QUESTIONS_PER_TEST,
            )
    if opened:
        db.commit()
    return opened


async def _tick():
    """Регулярная задача планировщика: открыть тесты, чьё время пришло."""
    db = SessionLocal()
    try:
        open_due_scheduled_tests(db)
    except Exception:
        logger.exception("Ошибка в тике планировщика")
        db.rollback()
    finally:
        db.close()


# === Жизненный цикл планировщика ===

def start_scheduler(app) -> None:
    """Запустить планировщик: догоняющая проверка + интервал-задача."""
    global _scheduler
    if _scheduler is not None:
        return

    # Догоняющая проверка: открываем тесты, чьё время пришло, пока сервер не работал.
    db = SessionLocal()
    try:
        opened = open_due_scheduled_tests(db)
        if opened:
            logger.info("Стартовая проверка: открыто %d тестов по расписанию.", opened)
    finally:
        db.close()

    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(
        _tick,
        "interval",
        seconds=config.SCHEDULER_INTERVAL_SECONDS,
        id="open-due-scheduled",
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    _scheduler = sched
    logger.info("Планировщик запущен (интервал %d сек).", config.SCHEDULER_INTERVAL_SECONDS)


def shutdown_scheduler() -> None:
    """Остановить планировщик при выключении приложения."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Планировщик остановлен.")


__all__ = [
    "local_to_utc",
    "utc_to_local",
    "open_due_scheduled_tests",
    "start_scheduler",
    "shutdown_scheduler",
]