# -*- coding: utf-8 -*-
"""
Voice processing utilities: transcription and transaction parsing.

This module uses OpenAI API for speech-to-text and LLM-based parsing
to extract transaction data from natural language input.

Requirements:
- pip install openai pydantic
- Set OPENAI_API_KEY env var (or use any supported auth method)

Public helpers:
- transcribe_file_to_text(path: str, *, language: str | None = None, model: str | None = None) -> Optional[str]
- parse_transaction_text(text: str, *, model: str | None = None) -> Optional[Transaction]
"""

from __future__ import annotations

from typing import Literal, Optional

import os
from openai import OpenAI
from pydantic import BaseModel, field_validator, ValidationError

# ---- Domain constants -------------------------------------------------------

EXPENSE_CATEGORIES: list[str] = [
    "Еда",
    "Транспорт",
    "Жильё",
    "Коммунальные",
    "Связь",
    "Здоровье",
    "Одежда",
    "Развлечения",
    "Подарки",
    "Прочее",
]

INCOME_CATEGORIES: list[str] = [
    "Зарплата",
    "Фриланс",
    "Подарки",
    "Продажи",
    "Проценты",
    "Кэшбэк",
    "Инвестиции",
    "Премия",
    "Соцвыплаты",
    "Прочее",
]

# Подберите модели под вашу подписку/квоты:
# - STT: "gpt-4o-transcribe" (качество) или "gpt-4o-mini-transcribe" (скорость/дешевле)
# - Structured output: быстрый недорогой — "gpt-4o-mini" или "gpt-5-nano" (если доступен в вашем аккаунте)
DEFAULT_TRANSCRIBE_MODEL = os.getenv("VOICE_STT_MODEL", "gpt-4o-transcribe")
DEFAULT_PARSE_MODEL = os.getenv("VOICE_PARSE_MODEL", "gpt-4o-mini")

# ---- Prompt for the model (used as 'instructions' for Responses API) --------

PROMPT = (
    "Ты — помощник для классификации финансовых транзакций.\n\n"
    "Преобразуй ввод пользователя в JSON-объект по схеме Transaction.\n\n"
    "Требования:\n"
    "- type: одно из [\"income\",\"expense\"].\n"
    "- sum: число (float).\n"
    "- category: строго из разрешённых списков, новых значений не добавлять.\n"
    "- description: необязательное поле.\n\n"
    "Категории:\n"
    "Расходы (если type=expense): "
    f"{EXPENSE_CATEGORIES}\n"
    "Доходы (если type=income): "
    f"{INCOME_CATEGORIES}\n\n"
    "Выводи только валидный JSON без пояснений."
)

# ---- Pydantic schema with cross-field validation ----------------------------

class Transaction(BaseModel):
    type: Literal["income", "expense"]
    sum: float
    category: Literal[
        "Еда",
        "Транспорт",
        "Жильё",
        "Коммунальные",
        "Связь",
        "Здоровье",
        "Одежда",
        "Развлечения",
        "Подарки",
        "Зарплата",
        "Фриланс",
        "Продажи",
        "Проценты",
        "Кэшбэк",
        "Инвестиции",
        "Премия",
        "Соцвыплаты",
        "Прочее",
    ]
    description: Optional[str] = None

    @field_validator("category")
    @classmethod
    def _check_category_vs_type(cls, v: str, info):
        t = info.data.get("type")
        if t == "expense" and v not in EXPENSE_CATEGORIES:
            raise ValueError(f"для type=expense допустимы только: {EXPENSE_CATEGORIES}")
        if t == "income" and v not in INCOME_CATEGORIES:
            raise ValueError(f"для type=income допустимы только: {INCOME_CATEGORIES}")
        return v


# ---- OpenAI client factory ---------------------------------------------------

def _get_client() -> Optional[OpenAI]:
    """
    Create OpenAI client if API key is present; else return None.
    The SDK берет ключ из окружения по умолчанию, явная передача не обязательна.
    """
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        # Конструктор сам возьмёт OPENAI_API_KEY и прочие опции
        return OpenAI()
    except Exception:
        return None


# ---- Speech-to-text ----------------------------------------------------------

def transcribe_file_to_text(
    path: str,
    *,
    language: str | None = None,
    model: str | None = None,
) -> Optional[str]:
    """Transcribe an audio file to text using OpenAI STT.

    Args:
        path: путь к аудиофайлу (wav/mp3/m4a/…).
        language: ISO-код языка (например, "ru") — опционально.
        model: переопределить модель STT; по умолчанию DEFAULT_TRANSCRIBE_MODEL.

    Returns:
        Распознанный текст или None при ошибке.
    """
    client = _get_client()
    if client is None:
        return None

    try:
        stt_model = model or DEFAULT_TRANSCRIBE_MODEL
        with open(path, "rb") as f:
            res = client.audio.transcriptions.create(
                model=stt_model,
                file=f,
                language=language,
            )
        # у Transcription-объекта есть поле .text
        return getattr(res, "text", None)
    except Exception:
        return None


# ---- LLM parsing to Transaction ---------------------------------------------

def parse_transaction_text(
    text: str,
    *,
    model: str | None = None,
) -> Optional[Transaction]:
    """Parse free-form text into a Transaction via Responses API + structured output."""
    client = _get_client()
    if client is None:
        return None

    try:
        parse_model = model or DEFAULT_PARSE_MODEL

        # Схемный парсинг через Responses API: SDK вернёт pydantic-экземпляр
        parsed = client.responses.parse(
            model=parse_model,
            instructions=PROMPT,
            input=text,
            text_format=Transaction,  # <-- ключ: строгая схема
        )

        # Унифицированный доступ: в новых версиях это parsed.output_parsed
        if getattr(parsed, "output_parsed", None) is not None:
            return parsed.output_parsed  # type: ignore[return-value]

        # Fallback: если вдруг вернулся чистый текст — попробуем распарсить вручную
        output_text = getattr(parsed, "output_text", None)
        if output_text:
            try:
                import json
                return Transaction(**json.loads(output_text))
            except Exception:
                return None

        return None

    except ValidationError:
        # модель вернула JSON, но он не проходит схему
        return None
    except Exception:
        return None


__all__ = [
    "EXPENSE_CATEGORIES",
    "INCOME_CATEGORIES",
    "Transaction",
    "transcribe_file_to_text",
    "parse_transaction_text",
]