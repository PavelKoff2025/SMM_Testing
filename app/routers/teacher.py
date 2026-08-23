"""
Роуты преподавателя (Этап 5): вход по паролю, дашборд, загрузка теста
(готовый JSON или PDF-презентация), предпросмотр, базовое открытие/закрытие.

Доступ — по паролю из config.TEACHER_PASSWORD (.env). Сессия хранится в той же
подписанной куке (SessionMiddleware), но под отдельным ключом "teacher",
независимо от студенческой.

Границы этапа:
  • AI-генерация вопросов из PDF и предпросмотр «до генерации» — Этап 6;
  • расписание (scheduled + APScheduler) — Этап 7;
  • сводная таблица попыток и аналитика по сложности — Этап 8;
  • допуск к зачёту — Этап 9.
Здесь — каркас кабинета с авторизацией, приёмом файлов и ручной публикацией.
"""
import hmac
import json
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

import config
from app.database import get_db
from app.models import Attempt, Student, Test, TestSource, TestStatus
from app.services.test_loader import load_test_from_data

router = APIRouter()

ALLOWED_JSON_EXT = (".json",)
ALLOWED_PDF_EXT = (".pdf",)


def _templates(request: Request):
    return request.app.state.templates


def _require_teacher(request: Request):
    """Если преподаватель не вошёл — редирект на форму входа, иначе None."""
    if not request.session.get("teacher"):
        return RedirectResponse("/teacher", status_code=303)
    return None


def _flash(request: Request, message: str, error: bool = False) -> None:
    """Однократное сообщение после редиректа (читается и гасится в дашборде)."""
    request.session["flash_error" if error else "flash"] = message


def _consume_flash(request: Request) -> tuple[str | None, str | None]:
    """Достать flash-сообщения из сессии и очистить их."""
    msg = request.session.pop("flash", None)
    err = request.session.pop("flash_error", None)
    return msg, err


# === Вход / выход ===

@router.get("/teacher", response_class=HTMLResponse)
async def teacher_index(request: Request, db: Session = Depends(get_db)):
    """Кабинет преподавателя: если не вошёл — форма входа, иначе дашборд."""
    if request.session.get("teacher"):
        return _dashboard(request, db)
    msg, err = _consume_flash(request)
    return _templates(request).TemplateResponse(
        request,
        "teacher.html",
        {
            "title": "Кабинет преподавателя",
            "teacher": False,
            "error": err,
            "flash": msg,
        },
    )


def _dashboard(request: Request, db: Session) -> HTMLResponse:
    """Собрать дашборд: статистика + список тестов."""
    tests = db.query(Test).order_by(Test.id).all()
    for t in tests:
        # число попыток по каждому тесту (без доп. запроса на каждый — простота важнее)
        t._attempts_count = db.query(Attempt).filter(Attempt.test_id == t.id).count()

    stats = {
        "tests_total": len(tests),
        "draft": sum(1 for t in tests if t.status == TestStatus.draft),
        "open": sum(1 for t in tests if t.status == TestStatus.open),
        "students": db.query(Student).count(),
        "attempts": db.query(Attempt).count(),
    }
    msg, err = _consume_flash(request)
    return _templates(request).TemplateResponse(
        request,
        "teacher.html",
        {
            "title": "Кабинет преподавателя",
            "teacher": True,
            "tests": tests,
            "stats": stats,
            "flash": msg,
            "error": err,
            "questions_per_test": config.QUESTIONS_PER_TEST,
        },
    )


@router.post("/teacher/login")
async def teacher_login(request: Request, password: str = Form(...)):
    """Проверка пароля преподавателя (constant-time), установка сессии."""
    ok = hmac.compare_digest(password, config.TEACHER_PASSWORD)
    if not ok:
        _flash(request, "Неверный пароль", error=True)
        return RedirectResponse("/teacher", status_code=303)
    request.session["teacher"] = True
    return RedirectResponse("/teacher", status_code=303)


@router.post("/teacher/logout")
async def teacher_logout(request: Request):
    request.session.pop("teacher", None)
    return RedirectResponse("/teacher", status_code=303)


# === Загрузка теста ===

@router.post("/teacher/upload/json")
async def upload_json(request: Request, file: UploadFile, db: Session = Depends(get_db)):
    """Загрузка готового JSON-теста (гибридный режим): валидация 4/4/2 + создание draft."""
    redir = _require_teacher(request)
    if redir:
        return redir

    name = (file.filename or "").lower()
    if not name.endswith(ALLOWED_JSON_EXT):
        _flash(request, "Нужен файл .json", error=True)
        return RedirectResponse("/teacher", status_code=303)

    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8"))
        created, test = load_test_from_data(data, db)
    except json.JSONDecodeError:
        _flash(request, "Файл не является корректным JSON", error=True)
        return RedirectResponse("/teacher", status_code=303)
    except Exception as e:
        _flash(request, f"Ошибка валидации теста: {e}", error=True)
        return RedirectResponse("/teacher", status_code=303)

    if created:
        _flash(request, f"Тест «{test.lecture_title}» создан (черновик, 10 вопросов).")
    else:
        _flash(request, f"Тест «{test.lecture_title}» уже существует — пропущен.")
    return RedirectResponse("/teacher", status_code=303)


@router.post("/teacher/upload/pdf")
async def upload_pdf(
    request: Request,
    file: UploadFile,
    lecture_title: str = Form(...),
    db: Session = Depends(get_db),
):
    """Загрузка PDF-презентации: сохранение файла + создание draft-теста без вопросов.

    Вопросы генерируются из PDF на Этапе 6 (AI, GPT-4o-mini). Пока тест хранится
    как черновик с pdf_path, открыть его студентам нельзя (нет 10 вопросов).
    """
    redir = _require_teacher(request)
    if redir:
        return redir

    name = (file.filename or "").lower()
    if not name.endswith(ALLOWED_PDF_EXT):
        _flash(request, "Нужен файл .pdf", error=True)
        return RedirectResponse("/teacher", status_code=303)

    lecture_title = lecture_title.strip()
    if not lecture_title:
        _flash(request, "Укажите название лекции", error=True)
        return RedirectResponse("/teacher", status_code=303)

    # Идемпотентность: не создаём дубль по названию лекции.
    existing = db.query(Test).filter(Test.lecture_title == lecture_title).one_or_none()
    if existing is not None:
        _flash(request, f"Тест «{lecture_title}» уже существует — пропущен.")
        return RedirectResponse("/teacher", status_code=303)

    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{file.filename}"
    saved_path = config.UPLOADS_DIR / stored_name
    content = await file.read()
    saved_path.write_bytes(content)

    try:
        test = Test(
            lecture_title=lecture_title,
            status=TestStatus.draft,
            source=TestSource.pdf,
            pdf_path=stored_name,
            pass_threshold=config.PASS_THRESHOLD,
        )
        db.add(test)
        db.commit()
        db.refresh(test)
        _flash(
            request,
            f"PDF «{lecture_title}» загружен. Вопросы сгенерируются на этапе 6 "
            f"(тест #{test.id}, черновик).",
        )
    except Exception as e:
        db.rollback()
        _flash(request, f"Ошибка сохранения теста: {e}", error=True)
    return RedirectResponse("/teacher", status_code=303)


# === Предпросмотр и публикация ===

@router.get("/teacher/test/{test_id}", response_class=HTMLResponse)
async def teacher_test_preview(test_id: int, request: Request, db: Session = Depends(get_db)):
    """Предпросмотр теста: вопросы с правильными ответами + действия публикации."""
    redir = _require_teacher(request)
    if redir:
        return redir

    test = db.get(Test, test_id)
    if test is None:
        raise HTTPException(404, "Тест не найден")

    can_open = len(test.questions) == config.QUESTIONS_PER_TEST
    flash, err = _consume_flash(request)
    return _templates(request).TemplateResponse(
        request,
        "teacher_test.html",
        {
            "title": test.lecture_title,
            "test": test,
            "can_open": can_open,
            "questions_per_test": config.QUESTIONS_PER_TEST,
            "flash": flash,
            "error": err,
        },
    )


@router.post("/teacher/test/{test_id}/open")
async def test_open(test_id: int, request: Request, db: Session = Depends(get_db)):
    """Открыть тест студентам (draft/closed → open). Требуется 10 вопросов."""
    redir = _require_teacher(request)
    if redir:
        return redir

    test = db.get(Test, test_id)
    if test is None:
        raise HTTPException(404, "Тест не найден")
    if len(test.questions) != config.QUESTIONS_PER_TEST:
        _flash(request, "Нельзя открыть тест без 10 вопросов (сгенерируйте из PDF).", error=True)
        return RedirectResponse(f"/teacher/test/{test_id}", status_code=303)

    test.status = TestStatus.open
    test.scheduled_at = None
    db.commit()
    _flash(request, f"Тест «{test.lecture_title}» открыт студентам.")
    return RedirectResponse(f"/teacher/test/{test_id}", status_code=303)


@router.post("/teacher/test/{test_id}/close")
async def test_close(test_id: int, request: Request, db: Session = Depends(get_db)):
    """Закрыть тест (open → closed): приём ответов окончен."""
    redir = _require_teacher(request)
    if redir:
        return redir

    test = db.get(Test, test_id)
    if test is None:
        raise HTTPException(404, "Тест не найден")

    test.status = TestStatus.closed
    db.commit()
    _flash(request, f"Тест «{test.lecture_title}» закрыт.")
    return RedirectResponse(f"/teacher/test/{test_id}", status_code=303)