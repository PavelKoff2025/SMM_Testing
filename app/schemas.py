"""
Pydantic-схема теста и валидация JSON-файла (Этап 2).

Формат одного теста (10 вопросов в пропорции 4 easy / 4 medium / 2 logic):

{
  "lecture_title": "Лекция 1. Введение в SMM",
  "questions": [
    {
      "number": 1,                  # 1..10
      "difficulty": "easy",          # easy | medium | logic
      "text": "Текст вопроса?",
      "options": ["A", "B", "C", "D"],  # минимум 2 варианта
      "correct_answer": 0            # индекс правильного варианта, 0-based
    },
    ... ещё 9 вопросов ...
  ]
}

Эта же модель используется на входе загрузчика (test_loader) и в будущем —
в роутах студента/преподавателя, чтобы не дублировать правила проверки.

Pydantic v2: field_validator — для одного поля, model_validator(mode="after") —
для проверок, которым нужны несколько полей сразу (индекс vs длина options,
пропорция 4/4/2, номера 1..10 без пропусков).
"""
from collections import Counter

from pydantic import BaseModel, Field, field_validator, model_validator

from config import QUESTION_STRUCTURE, QUESTIONS_PER_TEST, STUDENT_EMAIL_DOMAIN

# Допустимые значения сложности — те же, что в models.Difficulty.
_DIFFICULTIES = ("easy", "medium", "logic")


class QuestionIn(BaseModel):
    """Один вопрос теста во входном JSON."""
    number: int = Field(ge=1, le=QUESTIONS_PER_TEST, description="Номер вопроса 1..10")
    difficulty: str = Field(description="easy | medium | logic")
    text: str = Field(min_length=1, max_length=1024, description="Текст вопроса")
    options: list[str] = Field(min_length=2, description="Варианты ответа, минимум 2")
    correct_answer: int = Field(ge=0, description="Индекс правильного варианта, 0-based")

    @field_validator("difficulty")
    @classmethod
    def _check_difficulty(cls, v: str) -> str:
        if v not in _DIFFICULTIES:
            raise ValueError(
                f"difficulty должен быть одним из {_DIFFICULTIES}, получено '{v}'"
            )
        return v

    @model_validator(mode="after")
    def _check_correct_index(self) -> "QuestionIn":
        # correct_answer — индекс варианта, должен указывать на существующий option.
        if self.correct_answer >= len(self.options):
            raise ValueError(
                f"correct_answer={self.correct_answer} выходит за пределы "
                f"options (всего {len(self.options)})"
            )
        return self


class TestIn(BaseModel):
    """Целый тест во входном JSON: 10 вопросов в пропорции 4/4/2."""
    lecture_title: str = Field(min_length=1, max_length=255, description="Название лекции")
    questions: list[QuestionIn] = Field(description="Ровно 10 вопросов")

    @field_validator("questions")
    @classmethod
    def _check_count(cls, v: list[QuestionIn]) -> list[QuestionIn]:
        if len(v) != QUESTIONS_PER_TEST:
            raise ValueError(
                f"Должно быть ровно {QUESTIONS_PER_TEST} вопросов, получено {len(v)}"
            )
        return v

    @model_validator(mode="after")
    def _check_structure(self) -> "TestIn":
        # 1. Пропорция сложности: 4 easy / 4 medium / 2 logic (из конфига).
        counts = Counter(q.difficulty for q in self.questions)
        for diff, expected in QUESTION_STRUCTURE.items():
            actual = counts.get(diff, 0)
            if actual != expected:
                raise ValueError(
                    f"Пропорция сложности нарушена: {diff} должно быть {expected}, "
                    f"получено {actual}. Факт: {dict(counts)}"
                )

        # 2. Номера 1..10 без пропусков и повторов.
        numbers = sorted(q.number for q in self.questions)
        expected_numbers = list(range(1, QUESTIONS_PER_TEST + 1))
        if numbers != expected_numbers:
            raise ValueError(
                f"Номера вопросов должны быть 1..{QUESTIONS_PER_TEST} без пропусков "
                f"и повторов, получено {numbers}"
            )

        return self


def validate_test(data: dict) -> TestIn:
    """Удобная обёртка: парсит dict → TestIn, поднимает ValidationError при ошибке.

    Используется загрузчиком (test_loader.load_test_from_file) так, чтобы
    сообщение об ошибке валидации шло в лог/консоль одной строкой.
    """
    return TestIn.model_validate(data)


class StudentRegister(BaseModel):
    """Данные формы регистрации/входа студента.

    Регистрация без пароля (по README): имя, фамилия, группа, почта @misis.ru.
    Если почта уже есть в БД — это «вход» существующего студента, иначе — создание.
    Валидация домена почты здесь, чтобы правило жило в одном месте (как у тестов).
    """
    first_name: str = Field(min_length=1, max_length=64)
    last_name: str = Field(min_length=1, max_length=64)
    group: str = Field(min_length=1, max_length=32)
    email: str = Field(min_length=3, max_length=128)

    @field_validator("first_name", "last_name", "group", mode="before")
    @classmethod
    def _strip(cls, v: str) -> str:
        # mode="before" — обрезаем пробелы ДО проверки min_length,
        # иначе строка из одних пробелов прошла бы валидацию, а стала бы пустой.
        return v.strip() if isinstance(v, str) else v

    @field_validator("email", mode="before")
    @classmethod
    def _email_domain(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or not v.endswith(STUDENT_EMAIL_DOMAIN):
            raise ValueError(f"Нужна корпоративная почта {STUDENT_EMAIL_DOMAIN}")
        return v