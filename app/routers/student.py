"""
Роуты студента (Этап 3): регистрация/вход, кабинет, прохождение теста, результат.

Аутентификация — без пароля (по README): студент вводит имя, фамилию, группу и
корпоративную почту @misis.ru. Если почта новая — создаём студента, если уже есть —
это вход. Идентификатор студента хранится в подписанной сессионной куке
(SessionMiddleware, подключается в main.py).

Ограничения, заложенные в моделях (Этап 1):
  • одна попытка на тест — UniqueConstraint(student_id, test_id) на Attempt;
  • поэтому перед прохождением проверяем существующую попытку и при наличии —
    редирект на результат (перепройтение невозможно).
"""
import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import config
from app.database import get_db
from app.models import Answer, Attempt, Student, Test
from app.schemas import StudentRegister
from app.services.emailer import send_result_email
from app.services.queries import is_test_available, list_available_tests

router = APIRouter()
logger = logging.getLogger(__name__)


def _set_flash(request: Request, msg: str, error: bool = False) -> None:
    """Одноразовое сообщение через сессию (показывается после редиректа)."""
    request.session["flash"] = msg
    request.session["flash_error"] = error


def _take_flash(request: Request) -> tuple[str | None, bool]:
    """Достать и очистить flash-сообщение (msg, is_error). None если пусто."""
    msg = request.session.pop("flash", None)
    err = bool(request.session.pop("flash_error", False))
    return msg, err


def _templates(request: Request):
    """Шаблонизатор, общий для всего приложения (создаётся в main.py)."""
    return request.app.state.templates


def _current_student(request: Request, db: Session) -> Student | None:
    """Текущий студент по сессии или None, если не залогинен."""
    sid = request.session.get("student_id")
    if not sid:
        return None
    return db.get(Student, sid)


async def _maybe_send_result_email(student: Student, attempt: Attempt, test: Test) -> None:
    """Отправить результат студенту на почту, если SMTP_ENABLED=1.

    Не блокирует показ результата: любая ошибка SMTP логируется и глотается.
    Синхронный smtplib вызывается в потоке через asyncio.to_thread, чтобы не
    держать event loop на время SMTP-рукопожатия.
    """
    if not config.SMTP_ENABLED:
        return
    if not student.email or not config.SMTP_PASSWORD:
        # Без пароля приложения Яндекс отправка невозможна — тихо пропускаем.
        logger.warning("SMTP_ENABLED=1, но нет email студента или SMTP_PASSWORD — письмо не отправлено.")
        return

    questions = {q.number: q for q in test.questions}
    rows = []
    for a in sorted(attempt.answers, key=lambda x: x.question_number):
        q = questions.get(a.question_number)
        if q is None:
            continue
        student_txt = q.options[a.student_answer] if 0 <= a.student_answer < len(q.options) else "—"
        correct_txt = q.options[q.correct_answer] if 0 <= q.correct_answer < len(q.options) else "—"
        rows.append(
            {
                "number": a.question_number,
                "text": q.text,
                "student_answer": student_txt,
                "correct_answer": correct_txt,
                "is_correct": a.is_correct,
            }
        )

    try:
        await asyncio.to_thread(
            send_result_email, student.email, attempt, test, rows
        )
        logger.info("Письмо с результатом отправлено: %s (тест #%d, %d/10).",
                    student.email, test.id, attempt.score)
    except Exception:
        # SMTP недоступен/таймаут/невалидный адрес — НЕ ломаем прохождение теста.
        logger.exception("Не удалось отправить письмо с результатом на %s.", student.email)


@router.get("/student", response_class=HTMLResponse)
async def cabinet(request: Request, db: Session = Depends(get_db)):
    """Кабинет студента: залогинен — тесты + результаты; нет — форма регистрации."""
    student = _current_student(request, db)
    tests = list_available_tests(db) if student else []
    attempts = []
    if student:
        attempts = (
            db.query(Attempt)
            .filter(Attempt.student_id == student.id)
            .order_by(Attempt.finished_at.desc().nullslast())
            .all()
        )
    flash_msg, flash_err = _take_flash(request)
    return _templates(request).TemplateResponse(
        request,
        "student.html",
        {
            "title": "Кабинет студента",
            "student": student,
            "tests": tests,
            "attempts": attempts,
            "questions_per_test": config.QUESTIONS_PER_TEST,
            "pass_threshold": config.PASS_THRESHOLD,
            "error": None,
            "info": flash_msg,
            "info_error": flash_err,
            "form": None,
        },
    )


@router.post("/student/register")
async def register(
    request: Request,
    db: Session = Depends(get_db),
    first_name: str = Form(...),
    last_name: str = Form(...),
    group: str = Form(...),
    email: str = Form(...),
):
    """Регистрация нового студента или вход существующего (по почте)."""
    raw = {"first_name": first_name, "last_name": last_name, "group": group, "email": email}
    try:
        data = StudentRegister(**raw)
    except ValidationError as e:
        # Не отдаём 422 JSON — перерисовываем форму с понятным сообщением.
        msg = "; ".join(err["msg"] for err in e.errors())
        return _templates(request).TemplateResponse(
            request,
            "student.html",
            {
                "title": "Кабинет студента",
                "student": None,
                "tests": [],
                "attempts": [],
                "questions_per_test": config.QUESTIONS_PER_TEST,
                "pass_threshold": config.PASS_THRESHOLD,
                "error": msg,
                "info": None,
                "info_error": False,
                "form": raw,
            },
        )

    student = db.query(Student).filter(Student.email == data.email).one_or_none()
    if student is None:
        student = Student(
            first_name=data.first_name,
            last_name=data.last_name,
            group=data.group,
            email=data.email,
        )
        db.add(student)
        try:
            db.commit()
            db.refresh(student)
        except IntegrityError:
            # Гонка: кто-то создал студента с этой почтой параллельно.
            # Откатываемся и считаем, что это вход существующего студента.
            db.rollback()
            student = db.query(Student).filter(Student.email == data.email).one_or_none()
            if student is None:
                # Совсем вырожденный случай — безопасно уходим в кабинет.
                return RedirectResponse("/student", status_code=303)

    # Регенерация сессии при входе — защита от session fixation: очищаем
    # предыдущую куку, Starlette перевыпустит подпись с новыми данными.
    request.session.clear()
    request.session["student_id"] = student.id
    request.session["student_name"] = f"{student.first_name} {student.last_name}"
    return RedirectResponse("/student", status_code=303)


@router.post("/student/logout")
async def logout(request: Request):
    """Выход: очищаем сессию и возвращаем на страницу входа."""
    request.session.clear()
    return RedirectResponse("/student", status_code=303)


@router.get("/student/test/{test_id}", response_class=HTMLResponse)
async def take_test(test_id: int, request: Request, db: Session = Depends(get_db)):
    """Страница прохождения теста: 10 вопросов с радио-вариантами."""
    student = _current_student(request, db)
    if student is None:
        _set_flash(request, "Чтобы пройти тест — войдите как студент (имя, фамилия, группа, почта @misis.ru).")
        return RedirectResponse("/student", status_code=303)

    test = db.get(Test, test_id)
    if test is None or not is_test_available(test):
        raise HTTPException(404, "Тест не найден или недоступен")

    # Одна попытка на тест: если уже проходил — показываем результат.
    existing = (
        db.query(Attempt)
        .filter(Attempt.student_id == student.id, Attempt.test_id == test_id)
        .one_or_none()
    )
    if existing is not None:
        return RedirectResponse(f"/student/result/{existing.id}", status_code=303)

    return _templates(request).TemplateResponse(
        request,
        "take.html",
        {"title": test.lecture_title, "test": test, "student": student},
    )


@router.post("/student/test/{test_id}/submit")
async def submit_test(test_id: int, request: Request, db: Session = Depends(get_db)):
    """Приём ответов: считаем правильные, сохраняем Attempt + 10 Answer, редирект на результат."""
    student = _current_student(request, db)
    if student is None:
        _set_flash(request, "Сессия истекла — войдите снова, чтобы сдать тест.")
        return RedirectResponse("/student", status_code=303)

    test = db.get(Test, test_id)
    if test is None or not is_test_available(test):
        raise HTTPException(404, "Тест не найден или недоступен")

    # Защита от повторной отправки (двойной клик / возврат в браузере).
    existing = (
        db.query(Attempt)
        .filter(Attempt.student_id == student.id, Attempt.test_id == test_id)
        .one_or_none()
    )
    if existing is not None:
        return RedirectResponse(f"/student/result/{existing.id}", status_code=303)

    form = await request.form()

    attempt = Attempt(student_id=student.id, test_id=test.id, score=0, passed=False)
    db.add(attempt)
    try:
        db.flush()  # нужен attempt.id до вставки ответов; на race — IntegrityError
    except IntegrityError:
        # Двойной сабмит (две вкладки/двойной клик): UNIQUE(student_id, test_id).
        db.rollback()
        existing = (
            db.query(Attempt)
            .filter(Attempt.student_id == student.id, Attempt.test_id == test_id)
            .one_or_none()
        )
        if existing is not None:
            return RedirectResponse(f"/student/result/{existing.id}", status_code=303)
        return RedirectResponse("/student", status_code=303)

    # Считаем правильные и формируем ответы с жёсткой валидацией: нечисловой или
    # выходящий за пределы options ответ → -1 (считается неверным, в БД не мусор).
    score = 0
    for q in test.questions:
        raw = form.get(f"q{q.number}")
        try:
            chosen = int(raw) if raw is not None else -1
        except (ValueError, TypeError):
            chosen = -1
        if not (0 <= chosen < len(q.options)):
            chosen = -1
        is_correct = chosen == q.correct_answer
        if is_correct:
            score += 1
        db.add(
            Answer(
                attempt_id=attempt.id,
                question_number=q.number,
                difficulty=q.difficulty,
                student_answer=chosen,
                is_correct=is_correct,
            )
        )

    attempt.score = score
    attempt.passed = score >= test.pass_threshold
    attempt.finished_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:
        # Подстраховка: UNIQUE мог сработать только на commit.
        db.rollback()
        existing = (
            db.query(Attempt)
            .filter(Attempt.student_id == student.id, Attempt.test_id == test_id)
            .one_or_none()
        )
        if existing is not None:
            return RedirectResponse(f"/student/result/{existing.id}", status_code=303)
        return RedirectResponse("/student", status_code=303)

    # Email-отправка результата студенту (если SMTP_ENABLED=1). Не блокирует
    # показ результата: ошибка SMTP логируется, студент видит результат в кабинете.
    _maybe_send_result_email(student, attempt, test)

    return RedirectResponse(f"/student/result/{attempt.id}", status_code=303)


@router.get("/student/result/{attempt_id}", response_class=HTMLResponse)
async def result(attempt_id: int, request: Request, db: Session = Depends(get_db)):
    """Страница результата: оценка + построчный разбор ответов."""
    student = _current_student(request, db)
    if student is None:
        _set_flash(request, "Войдите, чтобы увидеть результат теста.")
        return RedirectResponse("/student", status_code=303)

    attempt = db.get(Attempt, attempt_id)
    if attempt is None or attempt.student_id != student.id:
        raise HTTPException(404, "Результат не найден")

    test = attempt.test
    questions = {q.number: q for q in test.questions}

    rows = []
    for a in sorted(attempt.answers, key=lambda x: x.question_number):
        q = questions.get(a.question_number)
        if q is None:
            continue
        student_txt = q.options[a.student_answer] if 0 <= a.student_answer < len(q.options) else "—"
        correct_txt = q.options[q.correct_answer] if 0 <= q.correct_answer < len(q.options) else "—"
        rows.append(
            {
                "number": a.question_number,
                "text": q.text,
                "difficulty": a.difficulty.value,
                "student_answer": student_txt,
                "correct_answer": correct_txt,
                "is_correct": a.is_correct,
            }
        )

    return _templates(request).TemplateResponse(
        request,
        "result.html",
        {
            "title": "Результат",
            "attempt": attempt,
            "test": test,
            "student": student,
            "rows": rows,
            "questions_per_test": config.QUESTIONS_PER_TEST,
        },
    )