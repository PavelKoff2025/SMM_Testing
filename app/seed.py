"""
Инициализация базы данных SMM_testing.

Этап 1: создаёт таблицы из моделей.
Этап 2: добавлен флаг --load-demo — заливает демо-тест лекции 1 из JSON.

Запуск:
    cd SMM_testing
    source venv/bin/activate
    python -m app.seed                          # создать таблицы
    python -m app.seed --reset                  # удалить и пересоздать таблицы (стирает данные)
    python -m app.seed --load-demo              # залить демо-тест лекции 1 в БД
    python -m app.seed --reset --load-demo       # чистая БД + демо-тест (удобно при разработке)
    python -m app.seed --open-demo              # открыть демо-тест (status=open) — доступен студентам
    python -m app.seed --reset --load-demo --open-demo  # чистая БД + демо-тест сразу открытый
    python -m app.seed --demo-data              # демо-студенты с попытками (для демонстрации teacher-views)

Под капотом:
    --reset делает Base.metadata.drop_all() затем create_all().
    Без флага — только create_all() (идемпотентно: не трогает существующие таблицы).
    --load-demo вызывает services.test_loader.load_test_from_file с демо-JSON.
    --demo-data создаёт 2 демо-студентов с попытками (8/10, 5/10, таймаут, 10/10)
        для демонстрации аналитики/допуска/разбора. Идемпотентно по email-суффиксу _demo.
    Порядок: сначала таблицы, потом демо-данные.

Дефолты таймера (time_limit_seconds/cooldown_seconds) на новые тесты
проставляются автоматически через default в моделях (config.TEST_TIME_LIMIT_SECONDS,
config.TEST_COOLDOWN_SECONDS); существующие тесты получила миграция app/migrate.py.
"""
import sys

from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401  — регистрирует модели в Base.metadata


def init(reset: bool = False) -> None:
    if reset:
        print("Удаляю существующие таблицы...")
        Base.metadata.drop_all(bind=engine)
    print("Создаю таблицы...")
    Base.metadata.create_all(bind=engine)
    print("Готово. Таблицы SMM_testing созданы в data/smm_testing.db")


def load_demo() -> None:
    """Залить демо-тест лекции 1 из tests_data/lecture_01_demo.json."""
    from config import TESTS_DATA_DIR
    from app.services.test_loader import load_test_from_file

    demo_path = TESTS_DATA_DIR / "lecture_01_demo.json"
    print(f"Загружаю демо-тест из {demo_path} ...")
    db = SessionLocal()
    try:
        created, test = load_test_from_file(demo_path, db)
        if created:
            print(f"Создан тест #{test.id} «{test.lecture_title}» "
                  f"(статус: {test.status.value}, источник: {test.source.value})")
            # Подсчитаем вопросы для отчёта
            from app.models import Question
            q_count = db.query(Question).filter(Question.test_id == test.id).count()
            print(f"Вопросов в БД: {q_count}")
        else:
            print(f"Тест «{test.lecture_title}» уже есть в БД (#{test.id}) — пропущен.")
    finally:
        db.close()


def open_demo() -> None:
    """Открыть демо-тест лекции 1 (status=open) — чтобы он был виден студентам.

    Заменяет ручное действие преподавателя «Открыть доступ», которое в проде
    делается из кабинета преподавателя (Этап 5). Удобно при локальной разработке.
    """
    from app.models import Test, TestStatus

    db = SessionLocal()
    try:
        test = db.query(Test).filter(
            Test.lecture_title == "Лекция 1. Введение в SMM"
        ).one_or_none()
        if test is None:
            print("Демо-тест не найден — сначала загрузите его: python -m app.seed --load-demo")
            return
        test.status = TestStatus.open
        db.commit()
        print(f"Тест #{test.id} «{test.lecture_title}» открыт (status=open).")
    finally:
        db.close()


def _make_attempt(db, student, test, n_correct: int, timed_out: bool, started_offset_min: int):
    """Создать и финализировать демо-попытку с n_correct правильными ответами.

    Прямая запись в БД (без HTTP) — для демонстрации teacher-views и потока.
    started_at = now - started_offset_min, deadline = started_at + time_limit.
    При timed_out deadline уже в прошлом → cooldown_until = deadline + 24ч.
    """
    from datetime import datetime, timedelta

    from app.models import Answer, Attempt, AttemptStatus
    from app.services.queries import (
        finalize_completed,
        finalize_timed_out,
        next_attempt_number,
    )

    num = next_attempt_number(db, student.id, test.id)
    started = datetime.utcnow() - timedelta(minutes=started_offset_min)
    attempt = Attempt(
        student_id=student.id,
        test_id=test.id,
        attempt_number=num,
        status=AttemptStatus.in_progress,
        started_at=started,
        deadline=started + timedelta(seconds=test.time_limit_seconds),
    )
    db.add(attempt)
    db.flush()  # нужен attempt.id для Answer

    qs = sorted(test.questions, key=lambda q: q.number)
    for i, q in enumerate(qs):
        correct = i < n_correct
        # верный ответ или соседний вариант (заведомо неверный)
        chosen = q.correct_answer if correct else (q.correct_answer + 1) % len(q.options)
        db.add(Answer(
            attempt_id=attempt.id,
            question_number=q.number,
            student_answer=chosen,
            is_correct=correct,
            difficulty=q.difficulty,
        ))
    db.flush()

    if timed_out:
        finalize_timed_out(attempt)
    else:
        finalize_completed(attempt)
    db.commit()
    return attempt


def demo_data() -> None:
    """Создать демо-студентов с попытками для демонстрации teacher-views (День 3).

    Двоих студентов с разными сценариями:
      • Петров: тест #2 — две попытки (8/10 зачёт, 5/10 незачёт → лучшая 8);
        тест #3 — таймаут 0/10 (кулдаун 24ч). Показывает перепрохождение,
        выбор лучшей попытки, статус timed_out, колонку «Попыток».
      • Смирнова: тест #2 — 10/10 зачёт. Одна попытка, для контраста в аналитике.

    Демо-студенты помечены суффиксом _demo в email — их легко узнать и удалить.
    Не трогает реальные тесты и студентов. Идемпотентно по email: повторный
    запуск пропускается, если демо-студенты уже есть.
    """
    from app.models import Student, Test

    db = SessionLocal()
    try:
        tests = db.query(Test).order_by(Test.id).all()
        if len(tests) < 2:
            print("Нужно минимум 2 теста для демо-данных — сначала сгенерируйте тесты.")
            return
        if db.query(Student).filter(Student.email.like("%_demo@misis.ru")).count():
            print("Демо-студенты уже есть — пропущено. Удалите их (или --reset), чтобы пересоздать.")
            return

        t_first, t_second = tests[0], tests[1]

        petrov = Student(first_name="Иван", last_name="Петров", group="МО-201",
                         email="petrov_demo@misis.ru")
        smirnova = Student(first_name="Анна", last_name="Смирнова", group="МО-201",
                           email="smirnova_demo@misis.ru")
        db.add_all([petrov, smirnova])
        db.commit()
        db.refresh(petrov); db.refresh(smirnova)

        # Петров: тест #2 — две попытки (8/10, затем 5/10 → лучшая 8).
        a1 = _make_attempt(db, petrov, t_first, n_correct=8, timed_out=False, started_offset_min=120)
        a2 = _make_attempt(db, petrov, t_first, n_correct=5, timed_out=False, started_offset_min=90)
        # Петров: тест #3 — таймаут (0/10, кулдаун 24ч).
        a3 = _make_attempt(db, petrov, t_second, n_correct=0, timed_out=True, started_offset_min=10)
        # Смирнова: тест #2 — 10/10 зачёт.
        a4 = _make_attempt(db, smirnova, t_first, n_correct=10, timed_out=False, started_offset_min=60)

        print(f"Демо-данные созданы: 2 студента, 4 попытки.")
        print(f"  Петров: тест «{t_first.lecture_title}» — попытка 1: {a1.score}/10, "
              f"попытка 2: {a2.score}/10 (лучшая {max(a1.score, a2.score)});")
        print(f"  Петров: тест «{t_second.lecture_title}» — таймаут {a3.score}/10 (кулдаун 24ч);")
        print(f"  Смирнова: тест «{t_first.lecture_title}» — {a4.score}/10 зачёт.")
        print("Логин преподавателя для просмотра: /teacher (TEACHER_PASSWORD из .env).")
    finally:
        db.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    reset = "--reset" in args
    load = "--load-demo" in args
    open_flag = "--open-demo" in args
    demo = "--demo-data" in args
    init(reset=reset)
    if load:
        load_demo()
    if open_flag:
        open_demo()
    if demo:
        demo_data()