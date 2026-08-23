"""
Точка входа FastAPI.

День 1: каркас с домашней страницей.
Этап 3: подключены SessionMiddleware (сессия студента) и роутер студента
        (app.routers.student): регистрация, кабинет, прохождение теста, результат.
Роуты преподавателя подключаются на этапе 5.
"""
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

import config
import app.models  # noqa: F401  — регистрирует модели в Base.metadata
from app.database import Base, engine, get_db
from app.routers import student
from app.services.queries import list_available_tests

app = FastAPI(title="SMM Testing", docs_url="/docs")

# Сессионная кука (подписанная) — хранит student_id после входа.
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.state.templates = templates  # общий доступ из роутеров через request.app.state

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# Создаём все таблицы при старте (модели определены на этапе 1).
Base.metadata.create_all(bind=engine)

# Роуты студента.
app.include_router(student.router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    """Домашняя страница: публичный список открытых тестов."""
    tests = list_available_tests(db)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "title": "SMM Testing",
            "questions_per_test": config.QUESTIONS_PER_TEST,
            "pass_threshold": config.PASS_THRESHOLD,
            "tests": tests,
        },
    )


@app.get("/health")
async def health():
    """Проверка, что приложение живое."""
    return {"status": "ok"}


@app.get("/teacher", response_class=HTMLResponse)
async def teacher_cabinet(request: Request):
    """Кабинет преподавателя: управление тестами и аналитика (каркас, Этап 5)."""
    return templates.TemplateResponse(request, "teacher.html", {"title": "Кабинет преподавателя"})