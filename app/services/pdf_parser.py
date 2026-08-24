"""
Извлечение текста из PDF-презентаций (Этап 6).

pdfplumber достаёт текст постранично; мы склеиваем страницы в единый текст
с разделителями-метками номера страницы — это даёт LLM контекст «слайд N».
Результат обрезается до config.AI_MAX_TEXT_CHARS — защита от аномально
больших файлов (презентации обычно короткие, а GPT-4o-mini контекста хватит).

Ошибки открытия/чтения оборачиваем в PdfParseError — роут преподавателя
превратит его в flash-сообщение, а не в 500-ю ошибку.
"""
from pathlib import Path

import pdfplumber

import config


class PdfParseError(Exception):
    """PDF не удалось прочитать или в нём нет текста."""


def extract_text(pdf_path: Path) -> str:
    """Извлечь текст из PDF и вернуть единой строкой.

    Страницы склеиваются как «\\n\\n--- стр. N ---\\n\\n». Пустые страницы
    (extract_text вернул None/пусто) пропускаются. Если в итоге текста нет —
    PdfParseError (например, PDF — сканы без текстового слоя).

    Поднимает PdfParseError при ошибке открытия/чтения.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise PdfParseError(f"Файл PDF не найден: {pdf_path.name}")

    chunks: list[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    page_text = page.extract_text() or ""
                except Exception:
                    # Одна битая страница не должна валить весь PDF.
                    page_text = ""
                page_text = page_text.strip()
                if not page_text:
                    continue
                chunks.append(f"--- стр. {i} ---\n{page_text}")
    except PdfParseError:
        raise
    except Exception as e:
        raise PdfParseError(f"Не удалось открыть PDF ({pdf_path.name}): {e}") from e

    if not chunks:
        raise PdfParseError(
            f"В PDF «{pdf_path.name}» не найден текстовый слой. "
            "Возможно, это сканы без распознанного текста."
        )

    text = "\n\n".join(chunks)
    if len(text) > config.AI_MAX_TEXT_CHARS:
        text = text[: config.AI_MAX_TEXT_CHARS] + "\n\n[…текст обрезан по лимиту…]"
    return text


__all__ = ["extract_text", "PdfParseError"]