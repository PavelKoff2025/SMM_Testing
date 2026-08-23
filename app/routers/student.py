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
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

import config
from app.database import get_db
from app.models import Answer, Attempt, Student, Test
from app.schemas import StudentRegister
from app.services.queries import is_test_available, list_available_tests

router = APIRouter()


def _templates(request: Request):
    """Шаблонизатор, общий для всего приложения (создаётся в main.py)."""
    return request.app.state.templates


def _current_student(request: Request, db: Session) -> Student | None:
    """Текущий студент по сессии или None, если не залогинен."""
    sid = request.session.get("student_id")
    if not sid:
        return None
    return db.get(Student, sid)


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
        db.commit()
        db.refresh(student)

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
    db.flush()  # нужен attempt.id до вставки ответов

    score = 0
    for q in test.questions:
        raw = form.get(f"q{q.number}")
        chosen = int(raw) if raw is not None else -1
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
    db.commit()

    return RedirectResponse(f"/student/result/{attempt.id}", status_code=303)


@router.get("/student/result/{attempt_id}", response_class=HTMLResponse)
async def result(attempt_id: int, request: Request, db: Session = Depends(get_db)):
    """Страница результата: оценка + построчный разбор ответов."""
    student = _current_student(request, db)
    if student is None:
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