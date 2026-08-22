"""
SQLAlchemy-модели SMM_testing.

Этап 1: схема данных под спецификацию README.
Ключевые решения (согласовано с автором):
  • Одна попытка на тест — UniqueConstraint(student_id, test_id) на Attempt.
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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

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

    Одна попытка на тест — UniqueConstraint(student_id, test_id).
    """
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("student_id", "test_id", name="uq_student_test"),
    )

    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    test_id = Column(Integer, ForeignKey("tests.id", ondelete="CASCADE"), nullable=False)
    score = Column(Integer, nullable=False, default=0)  # число правильных из 10
    passed = Column(Boolean, nullable=False, default=False)  # score >= pass_threshold
    started_at = Column(DateTime, nullable=False, server_default=func.now())
    finished_at = Column(DateTime, nullable=True)

    student = relationship("Student", back_populates="attempts")
    test = relationship("Test", back_populates="attempts")
    answers = relationship("Answer", back_populates="attempt", cascade="all, delete-orphan",
                           order_by="Answer.question_number")

    def __repr__(self):
        return f"<Attempt student={self.student_id} test={self.test_id} score={self.score} passed={self.passed}>"


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