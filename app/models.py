"""
SQLAlchemy-модели SMM_testing.

Этап 1: схема данных под спецификацию README.
Поточный режим с таймером и перепрохождением (День 1):
  • Несколько попыток на тест — UniqueConstraint(student_id, test_id) УБРАН.
  • Attempt живёт от старта (in_progress) до завершения (completed) или
    истечения таймера (timed_out). deadline = started_at + time_limit_seconds.
  • Кулдаун cooldown_until = deadline + cooldown_seconds ставится ТОЛЬКО при
    таймауте. В зачёт/допуск идёт ЛУЧШАЯ попытка по каждому тесту.
  • Правильный ответ и ответ студента хранятся как индекс варианта (0-based).
  • Варианты ответов — JSON-колонка (список строк), SQLite хранит как TEXT.
"""
import enum

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    Enum as SAEnum,
    func,
)
from sqlalchemy.orm import relationship

import config
from app.database import Base


# === Перечисления (хранятся как VARCHAR с CHECK на уровне SQLite) ===

class TestStatus(enum.Enum):
    """Жизненный цикл теста."""
    draft = "draft"        # создан, скрыт от студентов
    scheduled = "scheduled"  # задано время открытия
    open = "open"          # доступен студентам
    closed = "closed"      # приём ответов окончен


class TestSource(enum.Enum):
    """Способ создания теста."""
    pdf = "pdf"   # сгенерирован из PDF-презентации
    json = "json"  # загружен готовым JSON-файлом


class Difficulty(enum.Enum):
    """Сложность вопроса: 4 easy / 4 medium / 2 logic на тест."""
    easy = "easy"
    medium = "medium"
    logic = "logic"


class AttemptStatus(enum.Enum):
    """Жизненный цикл попытки.

    in_progress — студент начал, идёт таймер; answers наполняются по мере
                  прохождения вопросов (forward-only).
    completed   — студент ответил на все вопросы и завершил сам (или JS-таймер
                  доработал, но в пределах лимита).
    timed_out   — истёк time_limit_seconds; попытка закрыта принудительно,
                  выставлен cooldown_until = deadline + cooldown_seconds.
    """
    in_progress = "in_progress"
    completed = "completed"
    timed_out = "timed_out"


# === Модели ===

class Student(Base):
    """Студент: регистрируется перед прохождением теста."""
    __tablename__ = "students"

    id = Column(Integer, primary_key=True)
    first_name = Column(String(64), nullable=False)
    last_name = Column(String(64), nullable=False)
    group = Column(String(32), nullable=False)
    email = Column(String(128), nullable=False, unique=True)  # @misis.ru, проверка домена в роуте
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    attempts = relationship("Attempt", back_populates="student", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Student {self.last_name} {self.first_name} ({self.group})>"


class Test(Base):
    """Тест по лекции: 10 вопросов в пропорции 4/4/2."""
    __tablename__ = "tests"

    id = Column(Integer, primary_key=True)
    lecture_title = Column(String(255), nullable=False)
    status = Column(SAEnum(TestStatus), nullable=False, default=TestStatus.draft)
    scheduled_at = Column(DateTime, nullable=True)  # когда scheduled → open
    source = Column(SAEnum(TestSource), nullable=False, default=TestSource.json)
    pdf_path = Column(String(255), nullable=True)  # путь к загруженной PDF (если source=pdf)
    pass_threshold = Column(Integer, nullable=False, default=7)  # зачёт при >= N из 10
    # Лимит времени на весь тест и кулдаун после таймаута (по образцу pass_threshold:
    # per-test колонка с дефолтом из config — можно менять для отдельного теста).
    time_limit_seconds = Column(Integer, nullable=False, default=config.TEST_TIME_LIMIT_SECONDS)
    cooldown_seconds = Column(Integer, nullable=False, default=config.TEST_COOLDOWN_SECONDS)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    questions = relationship("Question", back_populates="test", cascade="all, delete-orphan",
                             order_by="Question.number")
    attempts = relationship("Attempt", back_populates="test", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Test #{self.id} '{self.lecture_title}' [{self.status.value}]>"


class Question(Base):
    """Вопрос теста: текст, варианты (JSON), индекс правильного ответа."""
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True)
    test_id = Column(Integer, ForeignKey("tests.id", ondelete="CASCADE"), nullable=False)
    number = Column(Integer, nullable=False)  # 1..10
    difficulty = Column(SAEnum(Difficulty), nullable=False)
    text = Column(String(1024), nullable=False)
    options = Column(JSON, nullable=False)        # ["вариант A", "вариант B", ...]
    correct_answer = Column(Integer, nullable=False)  # индекс правильного варианта (0-based)

    test = relationship("Test", back_populates="questions")

    def __repr__(self):
        return f"<Question #{self.number} [{self.difficulty.value}] test={self.test_id}>"


class Attempt(Base):
    """Попытка прохождения теста студентом.

    Несколько попыток разрешены (UniqueConstraint убран). attempt_number —
    порядковый номер у студента по этому тесту. status живёт от in_progress
    (старт) до completed (сам завершил) или timed_out (истёк таймер).
    deadline = started_at + time_limit_seconds — момент, после которого
    попытка принудительно закрывается; хранится явно, чтобы серверная проверка
    «now > deadline» была детерминированной без пересчёта. cooldown_until
    выставляется ТОЛЬКО при timed_out; пока now < cooldown_until, новую попытку
    начать нельзя.
    """
    __tablename__ = "attempts"

    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    test_id = Column(Integer, ForeignKey("tests.id", ondelete="CASCADE"), nullable=False)
    attempt_number = Column(Integer, nullable=False, default=1)  # 1, 2, 3… у студента по тесту
    status = Column(SAEnum(AttemptStatus), nullable=False, default=AttemptStatus.in_progress)
    score = Column(Integer, nullable=False, default=0)  # число правильных из 10
    passed = Column(Boolean, nullable=False, default=False)  # score >= pass_threshold
    started_at = Column(DateTime, nullable=False, server_default=func.now())
    deadline = Column(DateTime, nullable=True)   # started_at + time_limit_seconds (наивный UTC)
    finished_at = Column(DateTime, nullable=True)  # completed/timed_out: момент закрытия
    cooldown_until = Column(DateTime, nullable=True)  # только при timed_out: deadline + cooldown

    student = relationship("Student", back_populates="attempts")
    test = relationship("Test", back_populates="attempts")
    answers = relationship("Answer", back_populates="attempt", cascade="all, delete-orphan",
                           order_by="Answer.question_number")

    def __repr__(self):
        return (f"<Attempt #{self.attempt_number} student={self.student_id} test={self.test_id} "
                f"status={self.status.value if self.status else '?'} score={self.score}>")


class Answer(Base):
    """Ответ студента на один вопрос в рамках попытки."""
    __tablename__ = "answers"

    id = Column(Integer, primary_key=True)
    attempt_id = Column(Integer, ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False)
    question_number = Column(Integer, nullable=False)  # 1..10
    difficulty = Column(SAEnum(Difficulty), nullable=False)
    student_answer = Column(Integer, nullable=False)  # индекс выбранного варианта (0-based)
    is_correct = Column(Boolean, nullable=False, default=False)

    attempt = relationship("Attempt", back_populates="answers")

    def __repr__(self):
        return f"<Answer q#{self.question_number} correct={self.is_correct} attempt={self.attempt_id}>"