"""
Подключение к SQLite и базовый класс моделей.

check_same_thread=False нужен, чтобы FastAPI могла использовать
одно соединение из разных потоков (uvicorn). Для SQLite локально это безопасно.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from config import DATABASE_URL, DATA_DIR

# Гарантируем, что папка data/ существует (для файла БД)
DATA_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    """Зависимость FastAPI: выдаёт сессию БД и закрывает её после запроса."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()