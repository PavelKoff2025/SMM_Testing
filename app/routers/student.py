"""
Роуты студента: регистрация/вход, кабинет, поточное прохождение теста, результат.

Аутентификация — без пароля (по README): студент вводит имя, фамилию, группу и
корпоративную почту @misis.ru. Если почта новая — создаём студента, если уже
есть — это вход. Идентификатор студента хранится в подписанной сессионной куке
(SessionMiddleware, подключается в main.py).

Поточный режим с таймером (День 1):
  • Несколько попыток на тест (UniqueConstraint убран). Attempt живёт от старта
    (in_progress) до completed (сам завершил) или timed_out (истёк таймер).
  • Кулдаун 24ч — ТОЛЬКО при таймауте. В зачёт/допуск идёт ЛУЧШАЯ попытка.
  • Вопросы открываются по одному, навигация только вперёд (forward-only):
    следующий вопрос = last_answered_number + 1; попытка прыгнуть вперёд/
    назад bounce'ит на актуальный вопрос.
  • Серверная защита от таймаута ленивая: при любом запросе к идущей попытке
    проверяем now > deadline → принудительно timed_out. Это покрывает закрытие
    вкладки (JS-таймер не отправил timeout). Доп. подстраховка-«дворник» —
    APScheduler (День 3).

Эндпоинты потока:
  GET  /student/test/{test_id}                     — оркестратор: продолжить /
                                                     стартовать / показать кулдаун
  GET  /student/test/{attempt_id}/q/{n}             — вопрос N + прогресс + таймер
  POST /student/test/{attempt_id}/q/{n}             — сохранить ответ, перейти далее
  POST /student/test/{attempt_id}/timeout           — явный таймаут от JS-таймера
  GET  /student/result/{attempt_id}                 — результат + разбор + напоминание
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
from app.services.queries import (
    get_best_attempt,
    get_cooldown,
    get_in_progress,
    is_deadline_passed,
    is_test_available,
    last_answered_number,
    list_available_tests,
    finalize_completed,
    finalize_timed_out,
    save_answer,
    start_attempt,
)
from app.services.scheduler import utc_to_local

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


def _format_msk(dt: datetime | None) -> str:
    """Наивный UTC → строка по Москве для напоминаний о кулдауне/дате."""
    if dt is None:
        return ""
    return utc_to_local(dt).strftime("%d.%m.%Y %H:%M (МСК)")


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


def _questions_by_number(test: Test) -> dict[int, object]:
    """{question.number: question} — для быстрого доступа по номеру."""
    return {q.number: q for q in test.questions}


def _validate_chosen(raw) -> int:
    """Жёсткая валидация индекса варианта: нечисловой/вне диапазона → -1.

    -1 считается неверным; в БД не мусор, а честная «не отвечено/ошибка».
    """
    try:
        chosen = int(raw) if raw is not None else -1
    except (ValueError, TypeError):
        return -1
    return chosen


@router.get("/student", response_class=HTMLResponse)
async def cabinet(request: Request, db: Session = Depends(get_db)):
    """Кабинет студента: залогинен — тесты + состояние попыток; нет — форма регистрации.

    Для каждого доступного теста готовим state: best_attempt, in_progress,
    cooldown_until (+ cooldown_until_msk — формат по Москве) — чтобы шаблон
    рисует «Начать»/«Продолжить (в. N)»/«Доступно с {дата}» и лучший результат.
    """
    student = _current_student(request, db)
    tests = list_available_tests(db) if student else []
    attempts = []
    test_states = []
    if student:
        attempts = (
            db.query(Attempt)
            .filter(Attempt.student_id == student.id)
            .order_by(Attempt.finished_at.desc().nullslast())
            .all()
        )
        for t in tests:
            cooldown = get_cooldown(db, student.id, t.id)
            test_states.append(
                {
                    "test": t,
                    "best": get_best_attempt(db, student.id, t.id),
                    "in_progress": get_in_progress(db, student.id, t.id),
                    "cooldown_until": cooldown,
                    # Форматируем на сервере — шаблон не имеет доступа к utc_to_local,
                    # а единое место правды совпадает с next_available на странице результата.
                    "cooldown_until_msk": _format_msk(cooldown) if cooldown else None,
                }
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
            "test_states": test_states,
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
                "test_states": [],
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


@router.get("/student/test/{test_id}")
async def take_test(test_id: int, request: Request, db: Session = Depends(get_db)):
    """Оркестратор начала/продолжения теста.

      1. Есть in_progress попытка — продолжить с последнего отвеченного вопроса
         (с ленивой проверкой таймаута).
      2. Иначе есть активный кулдаун — назад в кабинет с напоминанием о дате.
      3. Иначе — создать новую попытку и перейти к вопросу 1.
    """
    student = _current_student(request, db)
    if student is None:
        _set_flash(request, "Чтобы пройти тест — войдите как студент (имя, фамилия, группа, почта @misis.ru).")
        return RedirectResponse("/student", status_code=303)

    test = db.get(Test, test_id)
    if test is None or not is_test_available(test):
        raise HTTPException(404, "Тест не найден или недоступен")

    # 1. Продолжить идущую попытку.
    active = get_in_progress(db, student.id, test_id)
    if active is not None:
        if is_deadline_passed(active):
            # Время истекло, пока студент был вне приложения — закрываем.
            finalize_timed_out(active)
            db.commit()
            await _maybe_send_result_email(student, active, test)
            return RedirectResponse(f"/student/result/{active.id}", status_code=303)
        nxt = last_answered_number(active) + 1
        # Если все отвечены, но попытка чудом in_progress — финализируем.
        if nxt > config.QUESTIONS_PER_TEST:
            finalize_completed(active)
            db.commit()
            await _maybe_send_result_email(student, active, test)
            return RedirectResponse(f"/student/result/{active.id}", status_code=303)
        return RedirectResponse(f"/student/test/{active.id}/q/{nxt}", status_code=303)

    # 2. Кулдаун после таймаута.
    cooldown = get_cooldown(db, student.id, test_id)
    if cooldown is not None:
        _set_flash(
            request,
            f"Время на попытку истекло. Следующая попытка по тесту «{test.lecture_title}» "
            f"будет доступна {_format_msk(cooldown)}.",
        )
        return RedirectResponse("/student", status_code=303)

    # 3. Новая попытка.
    attempt = start_attempt(db, student, test)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _set_flash(request, "Не удалось начать попытку — попробуйте ещё раз.")
        return RedirectResponse("/student", status_code=303)
    return RedirectResponse(f"/student/test/{attempt.id}/q/1", status_code=303)


@router.get("/student/test/{attempt_id}/q/{n}", response_class=HTMLResponse)
async def question_view(attempt_id: int, n: int, request: Request, db: Session = Depends(get_db)):
    """Показать вопрос N с прогрессом и таймером. Forward-only + ленивый таймаут."""
    student = _current_student(request, db)
    if student is None:
        _set_flash(request, "Сессия истекла — войдите снова, чтобы продолжить тест.")
        return RedirectResponse("/student", status_code=303)

    attempt = db.get(Attempt, attempt_id)
    if attempt is None or attempt.student_id != student.id:
        raise HTTPException(404, "Попытка не найдена")

    # Завершённая попытка — на результат.
    if attempt.status.value != "in_progress":
        return RedirectResponse(f"/student/result/{attempt_id}", status_code=303)

    # Ленивая проверка таймаута: время вышло — закрываем как timed_out.
    if is_deadline_passed(attempt):
        finalize_timed_out(attempt)
        db.commit()
        await _maybe_send_result_email(student, attempt, attempt.test)
        return RedirectResponse(f"/student/result/{attempt_id}", status_code=303)

    # Forward-only: можно смотреть только следующий за последним отвеченным.
    expected = last_answered_number(attempt) + 1
    if expected > config.QUESTIONS_PER_TEST:
        # Все отвечены, но попытка ещё открыта — финализируем (защита от рассинхрона).
        finalize_completed(attempt)
        db.commit()
        await _maybe_send_result_email(student, attempt, attempt.test)
        return RedirectResponse(f"/student/result/{attempt_id}", status_code=303)
    if n != expected:
        # Прыжок вперёд/назад — возвращаем на актуальный вопрос.
        return RedirectResponse(f"/student/test/{attempt_id}/q/{expected}", status_code=303)

    questions = _questions_by_number(attempt.test)
    question = questions.get(n)
    if question is None:
        raise HTTPException(404, "Вопрос не найден")

    # Оставшееся время в секундах — основа для клиентского таймера (День 2).
    remaining = max(0, int((attempt.deadline - datetime.utcnow()).total_seconds()))
    total = attempt.test.time_limit_seconds
    progress_answered = last_answered_number(attempt)

    return _templates(request).TemplateResponse(
        request,
        "take.html",
        {
            "title": attempt.test.lecture_title,
            "test": attempt.test,
            "attempt": attempt,
            "student": student,
            "question": question,
            "q_number": n,
            "q_total": config.QUESTIONS_PER_TEST,
            "remaining_seconds": remaining,
            "time_limit_seconds": total,
            "danger_seconds": config.TIMER_DANGER_SECONDS,
            "progress_answered": progress_answered,
        },
    )


@router.post("/student/test/{attempt_id}/q/{n}")
async def answer_question(attempt_id: int, n: int, request: Request, db: Session = Depends(get_db)):
    """Сохранить ответ на вопрос N и перейти к следующему (или завершить)."""
    student = _current_student(request, db)
    if student is None:
        _set_flash(request, "Сессия истекла — войдите снова.")
        return RedirectResponse("/student", status_code=303)

    attempt = db.get(Attempt, attempt_id)
    if attempt is None or attempt.student_id != student.id:
        raise HTTPException(404, "Попытка не найдена")

    if attempt.status.value != "in_progress":
        return RedirectResponse(f"/student/result/{attempt_id}", status_code=303)

    # Ленивый таймаут и на POST: время вышло — не принимаем ответ, закрываем.
    if is_deadline_passed(attempt):
        finalize_timed_out(attempt)
        db.commit()
        await _maybe_send_result_email(student, attempt, attempt.test)
        return RedirectResponse(f"/student/result/{attempt_id}", status_code=303)

    # Forward-only: принимаем ответ только на актуальный вопрос.
    expected = last_answered_number(attempt) + 1
    if n != expected:
        return RedirectResponse(f"/student/test/{attempt_id}/q/{expected}", status_code=303)

    questions = _questions_by_number(attempt.test)
    question = questions.get(n)
    if question is None:
        raise HTTPException(404, "Вопрос не найден")

    form = await request.form()
    chosen = _validate_chosen(form.get("answer"))
    if not (0 <= chosen < len(question.options)):
        chosen = -1

    save_answer(db, attempt, question, chosen)

    if n < config.QUESTIONS_PER_TEST:
        db.commit()
        return RedirectResponse(f"/student/test/{attempt_id}/q/{n + 1}", status_code=303)

    # Последний вопрос — завершаем попытку.
    finalize_completed(attempt)
    db.commit()
    await _maybe_send_result_email(student, attempt, attempt.test)
    return RedirectResponse(f"/student/result/{attempt_id}", status_code=303)


@router.post("/student/test/{attempt_id}/timeout")
async def timeout_attempt(attempt_id: int, request: Request, db: Session = Depends(get_db)):
    """Явный таймаут от JS-таймера (День 2). Идемпотентен: повторный POST — на результат."""
    student = _current_student(request, db)
    if student is None:
        return RedirectResponse("/student", status_code=303)

    attempt = db.get(Attempt, attempt_id)
    if attempt is None or attempt.student_id != student.id:
        raise HTTPException(404, "Попытка не найдена")

    if attempt.status.value == "in_progress":
        finalize_timed_out(attempt)
        db.commit()
        await _maybe_send_result_email(student, attempt, attempt.test)

    return RedirectResponse(f"/student/result/{attempt_id}", status_code=303)


@router.get("/student/result/{attempt_id}", response_class=HTMLResponse)
async def result(attempt_id: int, request: Request, db: Session = Depends(get_db)):
    """Страница результата: оценка + построчный разбор + напоминание о кулдауне."""
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

    # Напоминание о следующей попытке — только при действующем кулдауне.
    next_available = None
    if attempt.cooldown_until and attempt.cooldown_until > datetime.utcnow():
        next_available = _format_msk(attempt.cooldown_until)

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
            "next_available": next_available,
        },
    )