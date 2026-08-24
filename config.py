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

# === Допуск к финальному зачёту ===
ADMISSION_TESTS_REQUIRED = 9        # студент должен пройти все 9 тестов
ADMISSION_CORRECT_REQUIRED = 81     # и набрать минимум 81 правильный ответ
ADMISSION_TOTAL = 90                # всего вопросов по курсу (9 тестов × 10)

# === Студенты ===
STUDENT_EMAIL_DOMAIN = "@misis.ru"  # проверка домена при регистрации

# === Преподаватель (доступ к кабинету) ===
TEACHER_PASSWORD = os.getenv("TEACHER_PASSWORD", "changeme")

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

# === Сессия студента (подписанная кука) ===
# Секрет для подписи сессионной куки (SessionMiddleware). В проде — через .env.
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")