"""
Конфигурация проекта SMM_testing.

Все изменяемые параметры (порог зачёта, структура вопросов, допуск к зачёту)
вынесены сюда — логика приложения их читает, а не хардкодит.
Секреты (API-ключи, пароли) берутся из переменных окружения (.env).
"""
import os
from pathlib import Path

# Загружаем .env, если есть (локальная разработка)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# === Пути ===
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = BASE_DIR / "uploads"
TESTS_DATA_DIR = BASE_DIR / "tests_data"
DB_PATH = DATA_DIR / "smm_testing.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

# === Параметры теста ===
QUESTIONS_PER_TEST = 10
PASS_THRESHOLD = 7  # зачёт при >= 7 правильных из 10
# Структура вопросов по сложности (всего = 10)
QUESTION_STRUCTURE = {"easy": 4, "medium": 4, "logic": 2}

# === Таймер и перепрохождение ===
# На весь тест даётся TEST_TIME_LIMIT_SECONDS секунд (по ТЗ — 3 минуты).
# Таймер идёт от старта попытки (Attempt.started_at); за TIMER_DANGER_SECONDS
# до истечения становится красным. При превышении — попытка закрывается как
# timed_out, и следующая открывается ровно через TEST_COOLDOWN_SECONDS (24 ч).
# Кулдаун действует ТОЛЬКО при таймауте (не при незачёте — решение автора).
# В зачёт/допуск идёт ЛУЧШАЯ попытка по каждому тесту.
TEST_TIME_LIMIT_SECONDS = int(os.getenv("TEST_TIME_LIMIT_SECONDS", "180"))
TEST_COOLDOWN_SECONDS = int(os.getenv("TEST_COOLDOWN_SECONDS", str(24 * 3600)))
TIMER_DANGER_SECONDS = int(os.getenv("TIMER_DANGER_SECONDS", "30"))

# === Допуск к финальному зачёту ===
ADMISSION_TESTS_REQUIRED = 9        # студент должен пройти все 9 тестов
ADMISSION_CORRECT_REQUIRED = 81     # и набрать минимум 81 правильный ответ
ADMISSION_TOTAL = 90                # всего вопросов по курсу (9 тестов × 10)

# === Студенты ===
STUDENT_EMAIL_DOMAIN = "@misis.ru"  # проверка домена при регистрации

# === Окружение ===
# dev — локальная разработка/демо: допускаются рабочие дефолты секретов.
# prod — боевое: обязательна проверка, что TEACHER_PASSWORD и SECRET_KEY заданы
#        и не равны небезопасным дефолтам (иначе старт падает — см. ensure_prod_secrets).
APP_ENV = os.getenv("APP_ENV", "dev").strip().lower()
IS_PROD = APP_ENV == "prod"

# === Преподаватель (доступ к кабинету) ===
# В prod пароль обязан быть задан через env и отличаться от дефолта.
_DEFAULT_TEACHER_PASSWORD = "changeme"
TEACHER_PASSWORD = os.getenv("TEACHER_PASSWORD", _DEFAULT_TEACHER_PASSWORD)

# === OpenAI (генерация тестов из PDF) ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# Мок-режим: AI_MOCK=1 — генерация возвращает фиксированный демо-тест без
# вызова OpenAI (удобно для разработки/демонстрации без API-ключа).
# AI_MOCK=0 + заполненный OPENAI_API_KEY — реальный вызов GPT-4o-mini.
AI_MOCK = os.getenv("AI_MOCK", "1") == "1"
# Лимит символов извлекаемого из PDF текста — защита от аномально больших
# файлов (GPT-4o-mini держит 128k токенов, но презентации короткие).
AI_MAX_TEXT_CHARS = int(os.getenv("AI_MAX_TEXT_CHARS", "32000"))

# === Отправка результатов (Яндекс SMTP) ===
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "pkarikov@yandex.ru")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Павел Кофф — преподаватель")
# Выключатель отправки: 0 — не пытаться отправлять (результат виден в кабинете),
# 1 — отправлять через SMTP. По умолчанию выключено, чтобы не падать без пароля.
SMTP_ENABLED = os.getenv("SMTP_ENABLED", "0") == "1"
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "20"))

# === Сессия студента (подписанная кука) ===
# Секрет для подписи сессионной куки (SessionMiddleware). В проде — через .env.
_DEFAULT_SECRET_KEY = "dev-secret-change-me"
SECRET_KEY = os.getenv("SECRET_KEY", _DEFAULT_SECRET_KEY)

# Лимит размера загружаемого файла (PDF/JSON) — защита от OOM/DoS.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(26 * 1024 * 1024)))


def ensure_prod_secrets() -> None:
    """Проверка секретов в prod: старт должен падать, если безопасность невозможна.

    В dev — no-op (рабочие дефолты допустимы для локальной разработки/демо).
    В prod — TEACHER_PASSWORD и SECRET_KEY обязаны быть заданы и не равны
    небезопасным дефолтам. Вызывается из lifespan (main.py) при старте.
    """
    if not IS_PROD:
        return
    problems: list[str] = []
    if TEACHER_PASSWORD == _DEFAULT_TEACHER_PASSWORD:
        problems.append("TEACHER_PASSWORD не задан (используется дефолт 'changeme')")
    if not SECRET_KEY or SECRET_KEY == _DEFAULT_SECRET_KEY:
        problems.append("SECRET_KEY не задан (используется публично известный дефолт)")
    if problems:
        raise RuntimeError(
            "Запуск в prod отменён — небезопасные секреты: "
            + "; ".join(problems)
            + ". Задайте их в .env."
        )

# === Расписание тестов (Этап 7, APScheduler) ===
# Часовой пояс ввода/вывода для преподавателя: форму datetime-local трактуем
# как это локальное время, внутри храним и сравниваем UTC.
SCHEDULE_TZ = os.getenv("SCHEDULE_TZ", "Europe/Moscow")
# Как часто планировщик проверяет, не наступило ли время scheduled-тестов.
SCHEDULER_INTERVAL_SECONDS = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "30"))