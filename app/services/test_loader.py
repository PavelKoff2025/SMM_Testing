"""
Загрузчик теста из JSON в БД (Этап 2 + Этап 5).

Поток: данные → dict → Pydantic-валидация (TestIn) → транзакция
(создаём Test + 10 Question). Если валидация или вставка падает — откат,
в БД не остаётся «полузалитого» теста.

Идемпотентность: если тест с таким lecture_title уже есть — пропускаем
и возвращаем (False, существующий_тест), не дублируем.

Две точки входа (Этап 5): из файла (seed/CLI) и из dict (веб-форма
кабинета преподавателя) — общая логика в load_test_from_data.
"""
import json
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from config import PASS_THRESHOLD

from app.models import Test, TestSource, TestStatus, Question, Difficulty
from app.schemas import TestIn, validate_test


def load_test_from_data(data: dict, db: Session) -> tuple[bool, Optional[Test]]:
    """Загрузить тест из словаря (распарсенный JSON).

    Возвращает (created, test):
      • created=True, test=<новый Test>   — тест создан;
      • created=False, test=<существующий> — тест с таким lecture_title уже есть, пропущен.

    Поднимает ValueError / ValidationError при ошибках — вызывающий код
    (seed, роут преподавателя) решает, как реагировать.
    """
    # 1. Валидация структуры (10 вопросов, 4/4/2, индексы, номера) — до БД.
    test_in: TestIn = validate_test(data)

    # 2. Идемпотентность: не дублируем тест с тем же названием лекции.
    existing = db.query(Test).filter(Test.lecture_title == test_in.lecture_title).one_or_none()
    if existing is not None:
        return False, existing

    # 3. Транзакция: создаём Test + все Question; любая ошибка → откат.
    try:
        test = Test(
            lecture_title=test_in.lecture_title,
            status=TestStatus.draft,        # новый тест скрыт от студентов
            source=TestSource.json,         # загружен готовый JSON
            pass_threshold=PASS_THRESHOLD,  # из конфига, не хардкод
        )
        db.add(test)
        db.flush()  # получаем test.id до вставки вопросов

        for q in test_in.questions:
            db.add(Question(
                test_id=test.id,
                number=q.number,
                difficulty=Difficulty(q.difficulty),
                text=q.text,
                options=q.options,
                correct_answer=q.correct_answer,
            ))
        db.commit()
        return True, test
    except Exception:
        db.rollback()
        raise


def load_test_from_file(path: str | Path, db: Session) -> tuple[bool, Optional[Test]]:
    """Загрузить один тест из JSON-файла — обёртка над load_test_from_data.

    Поднимает FileNotFoundError / JSONDecodeError при ошибках чтения файла.
    """
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return load_test_from_data(raw, db)


__all__ = ["load_test_from_file", "load_test_from_data"]