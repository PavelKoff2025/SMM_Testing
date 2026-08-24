"""
Отправка результата теста студенту на корпоративную почту (Этап 10б/Email).

Яндекс SMTP (smtp.yandex.ru, порт 465 SSL) с ящика преподавателя. Требуется
пароль приложения Яндекс (не основной пароль) — в .env: SMTP_PASSWORD.

Письмо — plain text (UTF-8), без HTML: так проще пройти спам-фильтры Яндекса и
не тащить зависимость от HTML-шаблона письма. Содержит:
  • название теста (лекция);
  • количество правильных из 10 и оценку (зачёт/незачёт);
  • перечень вопросов, на которые студент ответил неверно, с правильным ответом.

Отправка — синхронная (smtplib). Из async-роута вызывается через asyncio.to_thread,
чтобы не блокировать event loop. Любая ошибка SMTP логируется и НЕ пробрасывается:
результат теста студент видит в кабинете в любом случае, email — не критичный канал.
"""
import logging
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

import config

logger = logging.getLogger(__name__)


def _build_message(to_email: str, test, attempt, wrong_rows: list[dict]) -> MIMEText:
    """Собрать plain-text письмо с результатом и разбором неверных ответов."""
    passed_txt = "ЗАЧЁТ" if attempt.passed else "НЕЗАЧЁТ"
    lines = [
        f"Здравствуйте!",
        "",
        f"Вы прошли тест по лекции «{test.lecture_title}».",
        f"Результат: {attempt.score} из {config.QUESTIONS_PER_TEST} правильных — {passed_txt}.",
        f"Порог зачёта: {test.pass_threshold} правильных ответов.",
        "",
    ]

    if not wrong_rows:
        lines.append("Вы ответили на все вопросы верно. Так держать!")
    else:
        lines.append("Вопросы, на которые вы ответили неверно:")
        lines.append("")
        for r in wrong_rows:
            lines.append(f"  Вопрос {r['number']}. {r['text']}")
            lines.append(f"    Ваш ответ: {r['student_answer']}")
            lines.append(f"    Правильный: {r['correct_answer']}")
            lines.append("")

    lines.append("—")
    lines.append(f"{config.SMTP_FROM_NAME}")
    lines.append("НИТУ МИСИС, курс «Взаимодействие с социальными медиа»")

    body = "\n".join(lines)
    subject = f"Результат теста: {test.lecture_title} — {attempt.score}/{config.QUESTIONS_PER_TEST} ({passed_txt})"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((config.SMTP_FROM_NAME, config.SMTP_USER))
    msg["To"] = to_email
    msg["Reply-To"] = config.SMTP_USER
    return msg


def send_result_email(to_email: str, attempt, test, rows: list[dict]) -> None:
    """Отправить письмо с результатом. Поднимает исключения SMTP — ловить наверху.

    rows — список dict с ключами number, text, student_answer, correct_answer,
    is_correct (формат как в routers.student.result). Берём только неверные.
    """
    wrong_rows = [r for r in rows if not r["is_correct"]]
    msg = _build_message(to_email, test, attempt, wrong_rows)

    # Порт 465 → SMTP_SSL (неявный TLS). Порт 587 → STARTTLS.
    if config.SMTP_PORT == 465:
        smtp_ctx = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT)
    else:
        smtp_ctx = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT)
        smtp_ctx.starttls()

    with smtp_ctx as smtp:
        smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(msg)


__all__ = ["send_result_email"]