"""
Точка входа FastAPI.

День 1: каркас с домашней страницей.
Этап 3: подключены SessionMiddleware (сессия студента) и роутер студента
        (app.routers.student): регистрация, кабинет, прохождение теста, результат.
Этап 5: подключён роутер преподавателя.
Этап 7: подключён планировщик APScheduler (lifespan) — автооткрытие
        тестов по расписанию (scheduled → open).
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import config
import app.models  # noqa: F401  — регистрирует модели в Base.metadata
from app.database import Base, engine, get_db
from app.routers import student
from app.routers import teacher
from app.services.queries import list_available_tests
from app.services.scheduler import shutdown_scheduler, start_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл приложения: проверка секретов, создание таблиц, планировщик.

    lifespan-контекст пришёл на смену устаревшим on_event('startup'/'shutdown'):
    одна точка, где виден и старт, и останов, — нагляднее и безопаснее.
    """
    # В prod — падаем при небезопасных дефолтах секретов (см. config.ensure_prod_secrets).
    config.ensure_prod_secrets()
    # Создаём все таблицы при старте (модели определены на этапе 1).
    Base.metadata.create_all(bind=engine)
    # Планировщик: догоняющая проверка + регулярный перевод scheduled→open.
    start_scheduler(app)
    try:
        yield
    finally:
        shutdown_scheduler()


# /docs и /redoc только в dev — в prod структуру API не светим.
_docs_url = "/docs" if not config.IS_PROD else None
_redoc_url = "/redoc" if not config.IS_PROD else None
app = FastAPI(title="SMM Testing", docs_url=_docs_url, redoc_url=_redoc_url, lifespan=lifespan)

# Сессионная кука (подписанная) — хранит student_id после входа.
# https_only в prod (кука только по HTTPS); samesite=lax частично защищает от CSRF
# (блокирует cross-site POST) — для одной группы достаточно; полная токен-защита
# отложена (см. README «Известные ограничения»).
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    https_only=config.IS_PROD,
    same_site="lax",
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.state.templates = templates  # общий доступ из роутеров через request.app.state

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# Роуты студента и преподавателя.
app.include_router(student.router)
app.include_router(teacher.router)


# === Кастомные страницы ошибок (HTML вместо JSON) ===

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """404/405/… → HTML-страница, а не JSON {"detail": ...} (server-rendered UI)."""
    code = exc.status_code
    if code == 404:
        message = "Страница или тест не найдены."
    elif code == 405:
        message = "Метод не поддерживается."
    else:
        message = exc.detail or "Ошибка запроса."
    return templates.TemplateResponse(
        request,
        "error.html",
        {"title": f"Ошибка {code}", "error_code": code, "error_message": message},
        status_code=code,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Любая непредвиденная ошибка → HTML 500 + логирование, без раскрытия стека."""
    logger.exception("Неперехваченная ошибка: %s %s", request.method, request.url.path)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "title": "Ошибка сервера",
            "error_code": 500,
            "error_message": "Внутренняя ошибка сервера. Результаты тестов сохранены.",
        },
        status_code=500,
    )


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