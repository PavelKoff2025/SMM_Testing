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

Под капотом:
    --reset делает Base.metadata.drop_all() затем create_all().
    Без флага — только create_all() (идемпотентно: не трогает существующие таблицы).
    --load-demo вызывает services.test_loader.load_test_from_file с демо-JSON.
    Порядок: сначала таблицы, потом демо-данные.
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


if __name__ == "__main__":
    args = sys.argv[1:]
    reset = "--reset" in args
    load = "--load-demo" in args
    init(reset=reset)
    if load:
        load_demo()