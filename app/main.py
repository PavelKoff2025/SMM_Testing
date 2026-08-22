"""
Точка входа FastAPI.

День 1: каркас с домашней страницей. Роуты студента и преподавателя
подключаются на следующих этапах.
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

import config
import app.models  # noqa: F401  — регистрирует модели в Base.metadata
from app.database import Base, engine

app = FastAPI(title="SMM Testing", docs_url="/docs")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# Создаём все таблицы при старте (Этап 1: модели определены)
Base.metadata.create_all(bind=engine)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Домашняя страница: список тестов (пока заглушка)."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "title": "SMM Testing",
            "questions_per_test": config.QUESTIONS_PER_TEST,
            "pass_threshold": config.PASS_THRESHOLD,
        },
    )


@app.get("/health")
async def health():
    """Проверка, что приложение живое."""
    return {"status": "ok"}


@app.get("/student", response_class=HTMLResponse)
async def student_cabinet(request: Request):
    """Кабинет студента: регистрация и прохождение тестов (каркас, Этап 3)."""
    return templates.TemplateResponse(request, "student.html", {"title": "Кабинет студента"})


@app.get("/teacher", response_class=HTMLResponse)
async def teacher_cabinet(request: Request):
    """Кабинет преподавателя: управление тестами и аналитика (каркас, Этап 5)."""
    return templates.TemplateResponse(request, "teacher.html", {"title": "Кабинет преподавателя"})