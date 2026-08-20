"""
Точка входа FastAPI.

День 1: каркас с домашней страницей. Роуты студента и преподавателя
подключаются на следующих этапах.
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

import config
from app.database import Base, engine

app = FastAPI(title="SMM Testing", docs_url="/docs")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Создаём таблицы, когда они появятся в models.py (Этап 1)
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