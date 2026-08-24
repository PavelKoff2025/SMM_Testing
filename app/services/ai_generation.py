"""
Генерация теста из текста лекции через GPT-4o-mini (Этап 6).

generate_test(text, lecture_title) -> dict в формате TestIn:
  {
    "lecture_title": "...",
    "questions": [
      {"number": 1, "difficulty": "easy|medium|logic",
       "text": "...", "options": ["...", "..."],
       "correct_answer": 0},  # индекс 0-based
      ... ещё 9 ...
    ]
  }

Сама валидация (10 вопросов, 4/4/2, индексы, номера) НЕ делается здесь —
это задача test_loader через app.schemas.TestIn. Разделение ответственности:
  • ai_generation — «достать JSON из LLM»;
  • test_loader   — «проверить и записать в БД».

Два режима (config.AI_MOCK):
  • AI_MOCK=True  — возвращает фиксированный демо-тест без вызова OpenAI
    (разработка/демонстрация без API-ключа);
  • AI_MOCK=False — реальный вызов OpenAI (требует config.OPENAI_API_KEY).
"""
import json
import logging

import config

logger = logging.getLogger(__name__)


class AiConfigError(Exception):
    """Невозможно вызвать LLM: нет API-ключа при выключенном мок-режиме."""


class AiGenerationError(Exception):
    """LLM вернула пустой или не-JSON ответ."""


# --- Системный промпт для GPT-4o-mini ---------------------------------------
# Требования продиктованы форматом TestIn (app/schemas.py):
#   • ровно 10 вопросов, номер 1..10 без пропусков/повторов;
#   • ровно 4 easy + 4 medium + 2 logic;
#   • correct_answer — индекс варианта (0-based), обязан указывать на
#     существующий элемент options.
_SYSTEM_PROMPT = (
    "Ты — методист и составляешь тесты по курсу «Взаимодействие с социальными "
    "медиа» (SMM) для студентов 2 курса бакалавриата. "
    "По тексту лекции составь тест из 10 вопросов с выбором одного ответа.\n\n"
    "Жёсткие требования:\n"
    "1) Ровно 10 вопросов, номера от 1 до 10 без пропусков и повторов.\n"
    "2) Сложность: ровно 4 вопроса «easy» (фактология прямо из лекции), "
    "4 вопроса «medium» (требуют понимания), 2 вопроса «logic» (на смекалку "
    "и логику, с опорой на материал).\n"
    "3) У каждого вопроса минимум 2 и максимум 5 вариантов ответа.\n"
    "4) correct_answer — это ИНДЕКС правильного варианта (0-based), он обязан "
    "указывать на существующий элемент массива options.\n"
    "5) Сложность — только из значений: easy, medium, logic.\n"
    "6) Вопросы и варианты — на русском, корректные по факту лекции.\n"
    "7) ВОПРОСЫ ТОЛЬКО ПО СУЩЕСТВУ МАТЕРИАЛА ЛЕКЦИИ — определения, формулы, "
    "метрики, методы, причины-следствия, расчёты. ЗАПРЕЩЕНЫ вопросы про "
    "метаданные: автор лекции, название, дата, номер слайда, оформление "
    "презентации, структуру курса, личность преподавателя. Вопрос «кто автор "
    "лекции» и подобные — никогда не появляются.\n\n"
    "Верни СТРОГО JSON (без пояснений, без markdown) вида:\n"
    "{\n"
    "  \"lecture_title\": \"<название лекции>\",\n"
    "  \"questions\": [\n"
    "    {\"number\": 1, \"difficulty\": \"easy\", \"text\": \"...\", "
    "\"options\": [\"...\", \"...\"], \"correct_answer\": 0}\n"
    "  ]\n"
    "}"
)

# --- Мок-тест (AI_MOCK=True) ------------------------------------------------
# Реалистичный тест по SMM, чтобы цепочку можно было пройти «как студенту».
# lecture_title подставляется из аргумента — название теста остаётся своим.
_MOCK_QUESTIONS = [
    {"number": 1, "difficulty": "easy",
     "text": "Что такое SMM в широком смысле?",
     "options": ["Только запуск рекламы в соцсетях",
                 "Комплексная работа с присутствием бренда в социальных медиа",
                 "Создание вирусных роликов",
                 "Рассылка email-писем"],
     "correct_answer": 1},
    {"number": 2, "difficulty": "easy",
     "text": "Какой формат публикаций в VK имеет лимит символов в записи «на стене»?",
     "options": ["До 1 000 символов", "До 16 380 символов",
                 "До 280 символов", "Без лимита"],
     "correct_answer": 1},
    {"number": 3, "difficulty": "easy",
     "text": "Что такое контент-план?",
     "options": ["Договор с подрядчиком", "Расписание публикаций с темами и форматами",
                 "Бюджет на рекламу", "Отчёт по охватам"],
     "correct_answer": 1},
    {"number": 4, "difficulty": "easy",
     "text": "Какая метрика показывает число уникальных пользователей, видевших пост?",
     "options": ["CTR", "Охват (reach)", "CPC", "Доля рынка"],
     "correct_answer": 1},
    {"number": 5, "difficulty": "medium",
     "text": "Бренд публикует экспертный лонгрид раз в неделю и получает рост аудитории, "
             "но вовлечённость падает. Какая вероятная причина?",
     "options": ["Контент слишком сложный без опоры на формат площадки",
                 "Слишком частая публикация", "Нет бюджета на продвижение",
                 "Площадка закрыла API"],
     "correct_answer": 0},
    {"number": 6, "difficulty": "medium",
     "text": "CTR рекламы 0,4% при отраслевом бенчмарке 1,2%. Что правкивает В ПЕРВУЮ очередь?",
     "options": ["Ставку конверсии на сайте", "Креатив и таргетинг объявления",
                 "Скорость загрузки приложения", "Частоту показов ретаргета"],
     "correct_answer": 1},
    {"number": 7, "difficulty": "medium",
     "text": "Сообщество растёт ботами ради «красивой цифры». К чему это ведёт в алгоритмах VK/Telegram?",
     "options": ["К росту органических охватов", "К снижению вовлечённости и просадке охватов",
                 "К автоматическому верификации", "Ни к чему — боты не влияют на алгоритмы"],
     "correct_answer": 1},
    {"number": 8, "difficulty": "medium",
     "text": "Зачем бренду tone of voice (ToV)?",
     "options": ["Чтобы юристы одобряли тексты", "Чтобы бренд звучал узнаваемо и консистентно во всех каналах",
                 "Чтобы уложиться в лимит символов", "Чтобы поднять CTR"],
     "correct_answer": 1},
    {"number": 9, "difficulty": "logic",
     "text": "Охват поста 10 000, вовлечённых (лайки+комменты+репосты) 50. "
             "ER (engagement rate) по охвату равен?",
     "options": ["0,5%", "5%", "50%", "0,05%"],
     "correct_answer": 0},
    {"number": 10, "difficulty": "logic",
     "text": "Студент ведёт TG-канал. Охваты растут, но подписчики не приходят. "
             "Какое действие логически противоречит цели роста подписчиков?",
     "options": ["Кросс-промо с соседним каналом ниши",
                 "Закрыть канал и сделать вход только по ссылке-приглашению",
                 "Публиковать репосты с указанием источника",
                 "Добавить канал в каталог TG-каналов"],
     "correct_answer": 1},
]


def _mock_test(lecture_title: str) -> dict:
    return {"lecture_title": lecture_title, "questions": _MOCK_QUESTIONS}


def generate_test(text: str, lecture_title: str) -> dict:
    """Вернуть тест как dict в формате TestIn.

    text — извлечённый из PDF текст лекции (используется в реальном режиме);
    lecture_title — название лекции, подставляется в результат.
    """
    if config.AI_MOCK:
        logger.warning(
            "AI_MOCK=1: генерация возвращает демо-тест без вызова OpenAI "
            "(lecture_title=%r).", lecture_title
        )
        return _mock_test(lecture_title)

    if not config.OPENAI_API_KEY:
        raise AiConfigError(
            "Не задан OPENAI_API_KEY и AI_MOCK выключен. "
            "Добавьте ключ в .env или включите AI_MOCK=1."
        )

    return _call_openai(text, lecture_title)


def _call_openai(text: str, lecture_title: str) -> dict:
    """Реальный вызов GPT-4o-mini. Импорт openai — здесь, чтобы не тащить "
    зависимость в мок-режим (и при отсутствии пакета мок всё равно работает)."""
    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    user_prompt = (
        f"Название лекции: {lecture_title}.\n\n"
        f"Текст лекции (извлечён из PDF):\n{text}\n\n"
        "Составь тест по жёстким требованиям из системного промпта и верни JSON."
    )

    try:
        resp = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            response_format={"type": "json_object"},
            temperature=0.4,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as e:
        raise AiGenerationError(f"Ошибка вызова OpenAI: {e}") from e

    content = resp.choices[0].message.content
    if not content or not content.strip():
        raise AiGenerationError("OpenAI вернул пустой ответ.")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        # Показываем первые символы, чтобы понять, что вернулось.
        snippet = (content[:200] + "…") if len(content) > 200 else content
        raise AiGenerationError(
            f"OpenAI вернул не-JSON: {e}. Начало ответа: {snippet}"
        ) from e

    # Гарантируем поле lecture_title, если модель его пропустила.
    data.setdefault("lecture_title", lecture_title)
    return data


__all__ = ["generate_test", "AiConfigError", "AiGenerationError"]