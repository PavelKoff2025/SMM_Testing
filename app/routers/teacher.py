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
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

import config
from app.database import get_db
from app.models import Attempt, Student, Test, TestSource, TestStatus
from app.services.ai_generation import AiConfigError, AiGenerationError, generate_test
from app.services.analytics import (
    attempt_breakdown,
    attempts_summary,
    difficulty_error_stats,
)
from app.services.pdf_parser import PdfParseError, extract_text
from app.services.scheduler import local_to_utc, utc_to_local
from app.services.test_loader import attach_questions, load_test_from_data

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
        # время расписания в московском времени для колонки «Расписание»
        t._scheduled_msk = (
            utc_to_local(t.scheduled_at).strftime("%d.%m.%Y %H:%M")
            if t.scheduled_at is not None
            else ""
        )

    stats = {
        "tests_total": len(tests),
        "draft": sum(1 for t in tests if t.status == TestStatus.draft),
        "scheduled": sum(1 for t in tests if t.status == TestStatus.scheduled),
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

    # Время расписания для шаблона: в московском времени (для показа и для
    # предзаполнения поля datetime-local, формат YYYY-MM-DDTHH:MM).
    scheduled_at_msk = None
    scheduled_at_value = None
    if test.scheduled_at is not None:
        local = utc_to_local(test.scheduled_at)
        scheduled_at_msk = local.strftime("%d.%m.%Y %H:%M (МСК)")
        scheduled_at_value = local.strftime("%Y-%m-%dT%H:%M")

    return _templates(request).TemplateResponse(
        request,
        "teacher_test.html",
        {
            "title": test.lecture_title,
            "test": test,
            "can_open": can_open,
            "questions_per_test": config.QUESTIONS_PER_TEST,
            "ai_mock": config.AI_MOCK,
            "scheduled_at_msk": scheduled_at_msk,
            "scheduled_at_value": scheduled_at_value,
            "schedule_tz": config.SCHEDULE_TZ,
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


# === Расписание (Этап 7): scheduled → автооткрытие по времени ===

# Допуск по времени: разрешаем планировать «в прошлое» не дальше чем на минуту,
# чтобы ручной ввод прямо сейчас не отбрасывался из-за секундного рассинхрона.
_SCHEDULE_PAST_GRACE = timedelta(minutes=1)


@router.post("/teacher/test/{test_id}/schedule")
async def test_schedule(
    test_id: int,
    request: Request,
    scheduled_at: str = Form(...),
    db: Session = Depends(get_db),
):
    """Запланировать автооткрытие теста (draft/closed/scheduled → scheduled).

    Преподаватель вводит дату/время в московской зоне (поле datetime-local).
    Сервер конвертирует Europe/Moscow → UTC и сохраняет в scheduled_at.
    Планировщик (app.services.scheduler) откроет тест, когда время наступит.
    """
    redir = _require_teacher(request)
    if redir:
        return redir

    test = db.get(Test, test_id)
    if test is None:
        raise HTTPException(404, "Тест не найден")

    def back(msg: str):
        _flash(request, msg, error=True)
        return RedirectResponse(f"/teacher/test/{test_id}", status_code=303)

    # Нельзя запланировать уже открытый тест — сначала закройте его.
    if test.status == TestStatus.open:
        return back("Тест уже открыт. Чтобы запланировать — сначала закройте его.")

    # Для автооткрытия у теста должны быть 10 вопросов.
    if len(test.questions) != config.QUESTIONS_PER_TEST:
        return back(
            f"Нельзя запланировать тест без {config.QUESTIONS_PER_TEST} вопросов "
            f"(сейчас {len(test.questions)})."
        )

    # Парсим «YYYY-MM-DDTHH:MM» из datetime-local как московское время.
    try:
        naive_local = datetime.strptime(scheduled_at.strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return back("Неверный формат даты/времени. Используйте поле выбора даты.")

    try:
        utc_dt = local_to_utc(naive_local)
    except Exception:
        return back(f"Не удалось распознать часовой пояс «{config.SCHEDULE_TZ}».")

    # Время должно быть в будущем (с допуском в минуту).
    if utc_dt < datetime.utcnow() - _SCHEDULE_PAST_GRACE:
        return back("Укажите время в будущем.")

    test.status = TestStatus.scheduled
    test.scheduled_at = utc_dt
    db.commit()
    msk = utc_to_local(utc_dt).strftime("%d.%m.%Y %H:%M")
    _flash(request, f"Тест «{test.lecture_title}» откроется автоматически: {msk} (МСК).")
    return RedirectResponse(f"/teacher/test/{test_id}", status_code=303)


@router.post("/teacher/test/{test_id}/unschedule")
async def test_unschedule(test_id: int, request: Request, db: Session = Depends(get_db)):
    """Отменить расписание теста (scheduled → draft, scheduled_at=None)."""
    redir = _require_teacher(request)
    if redir:
        return redir

    test = db.get(Test, test_id)
    if test is None:
        raise HTTPException(404, "Тест не найден")

    if test.status != TestStatus.scheduled:
        _flash(request, "Тест не запланирован — нечего отменять.", error=True)
        return RedirectResponse(f"/teacher/test/{test_id}", status_code=303)

    test.status = TestStatus.draft
    test.scheduled_at = None
    db.commit()
    _flash(request, f"Расписание теста «{test.lecture_title}» отменено, тест — черновик.")
    return RedirectResponse(f"/teacher/test/{test_id}", status_code=303)


# === AI-генерация вопросов из PDF (Этап 6) ===

@router.post("/teacher/test/{test_id}/generate")
async def test_generate(test_id: int, request: Request, db: Session = Depends(get_db)):
    """Сгенерировать 10 вопросов из PDF-презентации (GPT-4o-mini / мок).

    Защита:
      • только для вошедшего преподавателя;
      • только для source=pdf;
      • нельзя перегенерировать open/closed-тест (результаты студентов не должны
        рассинхронизироваться с вопросами);
      • pdf_path должен быть задан и файл должен существовать на диске.
    Любая ошибка на любом этапе → flash-сообщение, возврат в предпросмотр.
    """
    redir = _require_teacher(request)
    if redir:
        return redir

    test = db.get(Test, test_id)
    if test is None:
        raise HTTPException(404, "Тест не найден")

    # Универсальный «возврат с ошибкой» в предпросмотр.
    def back(msg: str):
        _flash(request, msg, error=True)
        return RedirectResponse(f"/teacher/test/{test_id}", status_code=303)

    if test.source != TestSource.pdf:
        return back("Генерация доступна только для тестов из PDF.")
    if test.status in (TestStatus.open, TestStatus.closed):
        return back(
            "Нельзя перегенерировать открытый или закрытый тест — "
            "это сломает уже сданные студентами результаты."
        )
    if not test.pdf_path:
        return back("У теста нет связанного PDF-файла.")

    pdf_path = config.UPLOADS_DIR / test.pdf_path
    if not pdf_path.exists():
        return back(f"PDF-файл не найден на диске: {test.pdf_path}")

    try:
        text = extract_text(pdf_path)
        data = generate_test(text, test.lecture_title)
        inserted = attach_questions(test, data, db)
    except PdfParseError as e:
        return back(f"Ошибка чтения PDF: {e}")
    except AiConfigError as e:
        return back(str(e))
    except AiGenerationError as e:
        return back(f"Ошибка генерации: {e}")
    except ValueError as e:
        return back(f"Модель вернула невалидный тест: {e}")
    except Exception as e:
        return back(f"Непредвиденная ошибка генерации: {e}")

    mode = "мок" if config.AI_MOCK else "GPT-4o-mini"
    _flash(request, f"Сгенерировано {inserted} вопросов из PDF ({mode}).")
    return RedirectResponse(f"/teacher/test/{test_id}", status_code=303)


# === Аналитика (Этап 8): сводная таблица попыток + ошибки по сложности ===

DIFF_LABELS = {"easy": "Лёгкий", "medium": "Средний", "logic": "Логика"}


@router.get("/teacher/analytics", response_class=HTMLResponse)
async def teacher_analytics(request: Request, db: Session = Depends(get_db)):
    """Аналитика для преподавателя: ошибки по сложности + сводная таблица попыток."""
    redir = _require_teacher(request)
    if redir:
        return redir

    diff_stats = difficulty_error_stats(db)
    attempts = attempts_summary(db)

    flash, err = _consume_flash(request)
    return _templates(request).TemplateResponse(
        request,
        "teacher_analytics.html",
        {
            "title": "Аналитика",
            "diff_stats": diff_stats,
            "diff_labels": DIFF_LABELS,
            "attempts": attempts,
            "questions_per_test": config.QUESTIONS_PER_TEST,
            "flash": flash,
            "error": err,
        },
    )


@router.get("/teacher/attempt/{attempt_id}", response_class=HTMLResponse)
async def teacher_attempt_view(attempt_id: int, request: Request, db: Session = Depends(get_db)):
    """Разбор одной попытки: все 10 вопросов с выбором студента и правильным ответом.

    Вид преподавателя — аналогичен result.html студента, но без студенческой
    навигации и с указанием ФИО/группы студента.
    """
    redir = _require_teacher(request)
    if redir:
        return redir

    attempt = db.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(404, "Попытка не найдена")

    rows = attempt_breakdown(attempt)
    test = attempt.test
    student = attempt.student

    return _templates(request).TemplateResponse(
        request,
        "teacher_attempt.html",
        {
            "title": "Разбор попытки",
            "attempt": attempt,
            "test": test,
            "student": student,
            "rows": rows,
            "diff_labels": DIFF_LABELS,
            "questions_per_test": config.QUESTIONS_PER_TEST,
        },
    )