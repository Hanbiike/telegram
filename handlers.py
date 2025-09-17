"""Aiogram handlers for the finance tracking bot.

Commands:
- /start - greet and ensure user exists
- /add_expense <amount> <category> [description]
- /add_income <amount> <category> [description]
- /balance - show current balance
- /stats day|week|month - show stats summary
- /delete_last - delete last transaction
- /help - show help
"""

from __future__ import annotations

from typing import Optional, Tuple, List, Dict, Any

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from db import Database
import os
import json
import tempfile
from openai import OpenAI
import aiohttp
import subprocess
import shutil


router = Router()


# -------- Keyboards ---------

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➖ Расход"), KeyboardButton(text="➕ Доход")],
    [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📊 Статистика")],
        [
            KeyboardButton(text="🗑 Удалить последнюю"),
            KeyboardButton(text="❓ Помощь"),
        ],
    ],
    resize_keyboard=True,
)

EXPENSE_CATEGORIES: List[str] = [
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

INCOME_CATEGORIES: List[str] = [
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


def build_categories_kb(items: List[str]) -> ReplyKeyboardMarkup:
    rows: List[List[KeyboardButton]] = []
    row: List[KeyboardButton] = []
    for i, name in enumerate(items, start=1):
        row.append(KeyboardButton(text=name))
        if i % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    # Last row: custom + cancel
    rows.append([KeyboardButton(text="Пользовательская"), KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)


YES_NO_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")], [KeyboardButton(text="Отмена")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


STATS_PERIOD_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="День"), KeyboardButton(text="Неделя")],
        [KeyboardButton(text="Месяц"), KeyboardButton(text="Год")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)


# -------- States (FSM) ---------

class ExpenseStates(StatesGroup):
    amount = State()
    category = State()
    maybe_custom_category = State()
    need_description = State()
    description = State()


class IncomeStates(StatesGroup):
    amount = State()
    category = State()
    maybe_custom_category = State()
    need_description = State()
    description = State()


class VoiceConfirmStates(StatesGroup):
    awaiting_confirmation = State()


def _parse_add_args(args: Optional[str]) -> Tuple[float, str, Optional[str]]:
    """Parse args for add commands.

    Expected: <amount> <category> [description]
    Returns: (amount, category, description)
    """

    if not args:
        raise ValueError(
            "Неверный формат. Используйте: <сумма> <категория> [описание]"
        )
    parts = args.strip().split()
    if len(parts) < 2:
        raise ValueError(
            "Неверный формат. Используйте: <сумма> <категория> [описание]"
        )
    amount_str = parts[0].replace(",", ".")
    try:
        amount = float(amount_str)
    except ValueError as exc:
        raise ValueError("Сумма должна быть числом") from exc
    category = parts[1]
    description = " ".join(parts[2:]) if len(parts) > 2 else None
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")
    return amount, category, description


@router.message(Command("start"))
async def cmd_start(message: Message, db: Database) -> None:
    """Handle /start: ensure user and show help."""

    user = message.from_user
    assert user is not None
    name = (user.full_name or user.username or str(user.id)).strip()
    await db.ensure_user(telegram_id=user.id, name=name)
    await message.answer(
        "Привет! Я помогу учитывать доходы и расходы.\n\n"
        "Доступные команды:\n"
        "/add_expense <сумма> <категория> [описание]\n"
        "/add_income <сумма> <категория> [описание]\n"
        "/balance — показать баланс\n"
        "/stats day|week|month — статистика\n"
        "/delete_last — удалить последнюю запись\n"
        "/help — справка",
        reply_markup=MAIN_KB,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Show help message."""

    await message.answer(
        "Команды:\n"
        "/add_expense <сумма> <категория> [описание]\n"
        "/add_income <сумма> <категория> [описание]\n"
        "/balance\n"
        "/stats day|week|month\n"
        "/delete_last\n\n"
        "Голосовой ввод:\n"
        "— Отправьте голосовое сообщение. Бот распознает речь и предложит добавить транзакцию.\n"
        "— Шаги: 1) транскрибация (gpt-audio), 2) разбор в JSON (responses API), 3) подтверждение добавления.",
        reply_markup=MAIN_KB,
    )


# -------- Buttons entry points ---------

@router.message(F.text == "➖ Расход")
async def btn_expense(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ExpenseStates.amount)
    await message.answer(
        "Введите сумму расхода (например, 250.50):",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(F.text == "➕ Доход")
async def btn_income(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(IncomeStates.amount)
    await message.answer(
        "Введите сумму дохода (например, 1000):",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(F.text == "💰 Баланс")
async def btn_balance(message: Message, db: Database) -> None:
    await cmd_balance(message, db)


@router.message(F.text == "📊 Статистика")
async def btn_stats_hint(message: Message) -> None:
    await message.answer(
        "Выберите период статистики:", reply_markup=STATS_PERIOD_KB
    )


@router.message(F.text.in_({"День", "Неделя", "Месяц", "Год"}))
async def btn_stats_period(message: Message, db: Database) -> None:
    text = (message.text or "").strip().lower()
    map_period = {"день": "day", "неделя": "week", "месяц": "month", "год": "year"}
    period = map_period.get(text)
    if period is None:
        await message.answer("Неизвестный период.", reply_markup=MAIN_KB)
        return
    # Reuse stats command logic
    cmd_obj = CommandObject(command="stats", args=period)
    await cmd_stats(message, cmd_obj, db)


@router.message(F.text == "🗑 Удалить последнюю")
async def btn_delete_last(message: Message, db: Database) -> None:
    await cmd_delete_last(message, db)


@router.message(F.text == "❓ Помощь")
async def btn_help(message: Message) -> None:
    await cmd_help(message)


# -------- Voice handling ---------

def _openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return OpenAI(api_key=api_key)


VOICE_JSON_SYSTEM_PROMPT = (
    "Ты помощник по финансам. На входе текст пользовательского сообщения"
    " (распознанная речь). Твоя задача — вернуть JSON со структурой строго: "
    "{\"type\": \"expense|income\", \"amount\": number, \"category\": string, \"description\": string|null}. "
    "Категория должна быть из списка, если подходит: "
    "Расходы: [Еда, Транспорт, Жильё, Коммунальные, Связь, Здоровье, Одежда, Развлечения, Подарки, Прочее]; "
    "Доходы: [Зарплата, Фриланс, Подарки, Продажи, Проценты, Кэшбэк, Инвестиции, Премия, Соцвыплаты, Прочее]. "
    "Если явной категории нет — выбери 'Прочее'. Сумма — положительное число."
)


async def _download_voice_to_temp(message: Message) -> str:
    """Download voice file to a temp path and return the file path."""

    voice = message.voice or message.audio or None
    if voice is None:
        raise ValueError("Нет голосового файла для обработки")

    file = await message.bot.get_file(voice.file_id)
    file_url = f"https://api.telegram.org/file/bot{message.bot.token}/{file.file_path}"
    # Preserve extension if present, default to .oga
    _, ext = os.path.splitext(file.file_path or "")
    if not ext:
        ext = ".oga"
    fd, tmp_path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    async with aiohttp.ClientSession() as session:
        async with session.get(file_url) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                f.write(await resp.read())
    return tmp_path


def _convert_to_wav(src_path: str) -> str:
    """Convert audio file to WAV using ffmpeg. Returns new file path."""

    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg не найден. Установите ffmpeg (brew install ffmpeg) и повторите."
        )
    fd, dst_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    # Convert to mono 16k PCM for better ASR
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        src_path,
        "-ac",
        "1",
        "-ar",
        "16000",
        dst_path,
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        try:
            os.remove(dst_path)
        except Exception:
            pass
        raise RuntimeError(f"Ошибка конвертации аудио: {e.stderr.decode(errors='ignore')}")
    return dst_path


async def _transcribe_audio(audio_path: str) -> str:
    """Use OpenAI gpt-audio model to get transcription text."""

    client = _openai_client()
    with open(audio_path, "rb") as f:
        try:
            transcript = client.audio.transcriptions.create(
                model="gpt-4o-transcribe",  # gpt-audio family (если доступно)
                file=f,
                response_format="text",
            )
            return transcript
        except Exception:
            f.seek(0)
            # Fallback на whisper-1, если gpt-audio недоступна
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
            return transcript


async def _parse_finance_json(text: str) -> Dict[str, Any]:
    """Use responses API (e.g., GPT-5-nano) to extract strict JSON."""

    client = _openai_client()
    # We constrain the model to output JSON only
    resp = client.responses.create(
        model="gpt-4o-mini",  # placeholder for GPT-5 nano if available in your env
        input=[
            {
                "role": "system",
                "content": VOICE_JSON_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        #response_format={"type": "json_object"},
    )
    content = resp.output[0].content[0].text if hasattr(resp, "output") else json.dumps({})
    # Fallback: newer SDKs often provide resp.output_text for json
    try:
        data = json.loads(getattr(resp, "output_text", content))
    except Exception:
        data = {}
    return data


def _validate_voice_json(data: Dict[str, Any]) -> Optional[str]:
    """Validate parsed JSON, return error message or None if ok."""

    if not isinstance(data, dict):
        return "Неверный формат данных."
    tx_type_raw = str(data.get("type") or "").strip().lower()
    tx_type: Optional[str] = None
    if tx_type_raw in {"expense", "income"}:
        tx_type = tx_type_raw
    else:
        income_syn = {"доход", "income", "прибыль", "зачисление", "зарплата", "поступление"}
        expense_syn = {"расход", "expense", "трата", "покупка", "списание", "оплата"}
        if tx_type_raw in income_syn:
            tx_type = "income"
        elif tx_type_raw in expense_syn:
            tx_type = "expense"
    # Infer from category if still unknown
    category_peek = str(data.get("category") or "").strip()
    if not tx_type and category_peek:
        if category_peek in INCOME_CATEGORIES:
            tx_type = "income"
        elif category_peek in EXPENSE_CATEGORIES:
            tx_type = "expense"
    if not tx_type:
        return "Тип операции должен быть 'expense' или 'income'."
    data["type"] = tx_type
    try:
        amount = float(data.get("amount"))
    except Exception:
        return "Сумма должна быть числом."
    if amount <= 0:
        return "Сумма должна быть положительной."
    category = str(data.get("category") or "").strip()
    valid_cats = EXPENSE_CATEGORIES if tx_type == "expense" else INCOME_CATEGORIES
    if category not in valid_cats:
        # map common synonyms or default to 'Прочее'
        category = "Прочее"
        data["category"] = category
    # normalize description
    desc = data.get("description")
    if desc is not None and not isinstance(desc, str):
        data["description"] = str(desc)
    return None


@router.message(F.voice | F.audio)
async def handle_voice(message: Message, state: FSMContext, db: Database) -> None:
    """Handle incoming voice/audio message: transcribe -> parse -> confirm."""

    # Step 1: download and transcribe
    try:
        tmp_path = await _download_voice_to_temp(message)
    except Exception as e:
        await message.answer(f"Не удалось скачать голосовое: {e}", reply_markup=MAIN_KB)
        return
    try:
        # Convert if necessary (e.g., .oga/.ogg -> .wav)
        _, ext = os.path.splitext(tmp_path)
        wav_path = None
        try:
            if ext.lower() in {".oga", ".ogg", ".opus"}:
                wav_path = _convert_to_wav(tmp_path)
                text = await _transcribe_audio(wav_path)
            else:
                text = await _transcribe_audio(tmp_path)
        finally:
            if wav_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
    except Exception as e:
        await message.answer(f"Ошибка распознавания речи: {e}", reply_markup=MAIN_KB)
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    # Step 2: parse into JSON
    try:
        data = await _parse_finance_json(text)
    except Exception as e:
        await message.answer(f"Ошибка анализа текста: {e}", reply_markup=MAIN_KB)
        return

    err = _validate_voice_json(data)
    if err:
        await message.answer(f"Понял: {text}\n\nНо возникла ошибка: {err}", reply_markup=MAIN_KB)
        return

    # Step 3: confirmation
    tx_type = data.get("type")
    amount = float(data.get("amount"))
    category = str(data.get("category"))
    description = data.get("description")
    await state.update_data(voice_tx={
        "type": tx_type,
        "amount": amount,
        "category": category,
        "description": description,
    })
    await state.set_state(VoiceConfirmStates.awaiting_confirmation)
    lines = [
        "Это верно?",
        f"Тип: {'Расход' if tx_type=='expense' else 'Доход'}",
        f"Сумма: {amount:.2f}",
        f"Категория: {category}",
    ]
    if description:
        lines.append(f"Описание: {description}")
    await message.answer("\n".join(lines), reply_markup=YES_NO_KB)


@router.message(VoiceConfirmStates.awaiting_confirmation)
async def voice_confirm(message: Message, state: FSMContext, db: Database) -> None:
    answer = (message.text or "").strip().lower()
    if answer == "отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=MAIN_KB)
        return
    if answer not in {"да", "нет"}:
        await message.answer("Пожалуйста, выберите 'Да' или 'Нет'.", reply_markup=YES_NO_KB)
        return
    if answer == "нет":
        await state.clear()
        await message.answer("Хорошо, не добавляю.", reply_markup=MAIN_KB)
        return

    data = await state.get_data()
    voice_tx = data.get("voice_tx", {})
    tx_type = voice_tx.get("type")
    amount = float(voice_tx.get("amount", 0))
    category = str(voice_tx.get("category", "Прочее"))
    description = voice_tx.get("description")

    user = message.from_user
    assert user is not None
    user_id = await db.ensure_user(user.id, (user.full_name or "").strip() or str(user.id))
    await db.add_transaction(
        user_id=user_id,
        tx_type=tx_type,
        amount=amount,
        category=category,
        description=description,
    )
    await state.clear()
    sign = "+" if tx_type == "income" else "-"
    await message.answer(
        f"Добавлено: {sign}{amount:.2f} ({category}).",
        reply_markup=MAIN_KB,
    )


@router.message(Command("add_expense"))
async def cmd_add_expense(message: Message, command: CommandObject, db: Database) -> None:
    """Add an expense transaction."""

    user = message.from_user
    assert user is not None
    try:
        amount, category, description = _parse_add_args(command.args)
    except ValueError as e:
        await message.answer(str(e))
        return
    user_id = await db.ensure_user(user.id, (user.full_name or "").strip() or str(user.id))
    await db.add_transaction(
        user_id=user_id,
        tx_type="expense",
        amount=amount,
        category=category,
        description=description,
    )
    await message.answer(
        f"Добавлен расход: -{amount:.2f} в категории '{category}'.",
        reply_markup=MAIN_KB,
    )


@router.message(Command("add_income"))
async def cmd_add_income(message: Message, command: CommandObject, db: Database) -> None:
    """Add an income transaction."""

    user = message.from_user
    assert user is not None
    try:
        amount, category, description = _parse_add_args(command.args)
    except ValueError as e:
        await message.answer(str(e))
        return
    user_id = await db.ensure_user(user.id, (user.full_name or "").strip() or str(user.id))
    await db.add_transaction(
        user_id=user_id,
        tx_type="income",
        amount=amount,
        category=category,
        description=description,
    )
    await message.answer(
        f"Добавлен доход: +{amount:.2f} в категории '{category}'.",
        reply_markup=MAIN_KB,
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message, db: Database) -> None:
    """Show current balance for the user."""

    user = message.from_user
    assert user is not None
    user_id = await db.ensure_user(user.id, (user.full_name or "").strip() or str(user.id))
    balance = await db.get_balance(user_id)
    sign = "" if balance >= 0 else "-"
    await message.answer(f"Баланс: {sign}{abs(balance):.2f}", reply_markup=MAIN_KB)


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, db: Database) -> None:
    """Show stats for period: day|week|month."""

    user = message.from_user
    assert user is not None
    period = (command.args or "").strip().lower()
    if period not in {"day", "week", "month", "year"}:
        await message.answer(
            "Укажите период: /stats day|week|month|year",
            reply_markup=MAIN_KB,
        )
        return
    user_id = await db.ensure_user(user.id, (user.full_name or "").strip() or str(user.id))
    stats = await db.get_stats(user_id, period)
    income = stats["income_total"]
    expense = stats["expense_total"]
    lines = [
        f"Статистика за {period}:",
        f"Доходы: +{income:.2f}",
        f"Расходы: -{expense:.2f}",
    ]
    # Top categories (up to 5 for brevity)
    if stats["by_category"]["income"]:
        inc_top = ", ".join(
            f"{cat}: {amt:.2f}" for cat, amt in stats["by_category"]["income"][:5]
        )
        lines.append(f"Топ доходы: {inc_top}")
    if stats["by_category"]["expense"]:
        exp_top = ", ".join(
            f"{cat}: {amt:.2f}" for cat, amt in stats["by_category"]["expense"][:5]
        )
        lines.append(f"Топ расходы: {exp_top}")
    await message.answer("\n".join(lines), reply_markup=MAIN_KB)


@router.message(Command("delete_last"))
async def cmd_delete_last(message: Message, db: Database) -> None:
    """Delete the last transaction for the user."""

    user = message.from_user
    assert user is not None
    user_id = await db.ensure_user(user.id, (user.full_name or "").strip() or str(user.id))
    ok = await db.delete_last_transaction(user_id)
    if ok:
        await message.answer("Последняя транзакция удалена.", reply_markup=MAIN_KB)
    else:
        await message.answer("Нет транзакций для удаления.", reply_markup=MAIN_KB)


# -------- Expense flow ---------

@router.message(ExpenseStates.amount)
async def expense_enter_amount(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().replace(",", ".")
    if text.lower() == "отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=MAIN_KB)
        return
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Сумма должна быть положительным числом. Попробуйте ещё раз:")
        return
    await state.update_data(amount=amount)
    await state.set_state(ExpenseStates.category)
    await message.answer(
        "Выберите категорию расхода:",
        reply_markup=build_categories_kb(EXPENSE_CATEGORIES),
    )


@router.message(ExpenseStates.category)
async def expense_choose_category(message: Message, state: FSMContext) -> None:
    category = (message.text or "").strip()
    if category == "Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=MAIN_KB)
        return
    if category == "Пользовательская":
        await state.set_state(ExpenseStates.maybe_custom_category)
        await message.answer("Введите название категории:", reply_markup=ReplyKeyboardRemove())
        return
    # Validate category from list
    if category not in EXPENSE_CATEGORIES:
        await message.answer("Выберите категорию из клавиатуры или 'Пользовательская'.")
        return
    await state.update_data(category=category)
    await state.set_state(ExpenseStates.need_description)
    await message.answer("Добавить описание?", reply_markup=YES_NO_KB)


@router.message(ExpenseStates.maybe_custom_category)
async def expense_custom_category(message: Message, state: FSMContext) -> None:
    category = (message.text or "").strip()
    if not category:
        await message.answer("Название категории не может быть пустым. Введите ещё раз:")
        return
    await state.update_data(category=category)
    await state.set_state(ExpenseStates.need_description)
    await message.answer("Добавить описание?", reply_markup=YES_NO_KB)


@router.message(ExpenseStates.need_description)
async def expense_need_description(message: Message, state: FSMContext, db: Database) -> None:
    answer = (message.text or "").strip().lower()
    if answer == "отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=MAIN_KB)
        return
    if answer == "да":
        await state.set_state(ExpenseStates.description)
        await message.answer("Введите описание:", reply_markup=ReplyKeyboardRemove())
        return
    if answer == "нет":
        data = await state.get_data()
        await state.clear()
        await _finalize_expense(message, db, data, description=None)
        return
    await message.answer("Пожалуйста, выберите 'Да' или 'Нет'.", reply_markup=YES_NO_KB)


@router.message(ExpenseStates.description)
async def expense_description(message: Message, state: FSMContext, db: Database) -> None:
    description = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    await _finalize_expense(message, db, data, description=description or None)


async def _finalize_expense(
    message: Message, db: Database, data: dict, description: Optional[str]
) -> None:
    """Persist expense transaction using collected FSM data."""

    user = message.from_user
    assert user is not None
    amount = float(data.get("amount", 0))
    category = str(data.get("category"))
    user_id = await db.ensure_user(user.id, (user.full_name or "").strip() or str(user.id))
    await db.add_transaction(
        user_id=user_id,
        tx_type="expense",
        amount=amount,
        category=category,
        description=description,
    )
    await message.answer(
        f"Добавлен расход: -{amount:.2f} в категории '{category}'.",
        reply_markup=MAIN_KB,
    )


# -------- Income flow ---------

@router.message(IncomeStates.amount)
async def income_enter_amount(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().replace(",", ".")
    if text.lower() == "отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=MAIN_KB)
        return
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Сумма должна быть положительным числом. Попробуйте ещё раз:")
        return
    await state.update_data(amount=amount)
    await state.set_state(IncomeStates.category)
    await message.answer(
        "Выберите категорию дохода:",
        reply_markup=build_categories_kb(INCOME_CATEGORIES),
    )


@router.message(IncomeStates.category)
async def income_choose_category(message: Message, state: FSMContext) -> None:
    category = (message.text or "").strip()
    if category == "Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=MAIN_KB)
        return
    if category == "Пользовательская":
        await state.set_state(IncomeStates.maybe_custom_category)
        await message.answer("Введите название категории:", reply_markup=ReplyKeyboardRemove())
        return
    if category not in INCOME_CATEGORIES:
        await message.answer("Выберите категорию из клавиатуры или 'Пользовательская'.")
        return
    await state.update_data(category=category)
    await state.set_state(IncomeStates.need_description)
    await message.answer("Добавить описание?", reply_markup=YES_NO_KB)


@router.message(IncomeStates.maybe_custom_category)
async def income_custom_category(message: Message, state: FSMContext) -> None:
    category = (message.text or "").strip()
    if not category:
        await message.answer("Название категории не может быть пустым. Введите ещё раз:")
        return
    await state.update_data(category=category)
    await state.set_state(IncomeStates.need_description)
    await message.answer("Добавить описание?", reply_markup=YES_NO_KB)


@router.message(IncomeStates.need_description)
async def income_need_description(message: Message, state: FSMContext, db: Database) -> None:
    answer = (message.text or "").strip().lower()
    if answer == "отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=MAIN_KB)
        return
    if answer == "да":
        await state.set_state(IncomeStates.description)
        await message.answer("Введите описание:", reply_markup=ReplyKeyboardRemove())
        return
    if answer == "нет":
        data = await state.get_data()
        await state.clear()
        await _finalize_income(message, db, data, description=None)
        return
    await message.answer("Пожалуйста, выберите 'Да' или 'Нет'.", reply_markup=YES_NO_KB)


@router.message(IncomeStates.description)
async def income_description(message: Message, state: FSMContext, db: Database) -> None:
    description = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    await _finalize_income(message, db, data, description=description or None)


async def _finalize_income(
    message: Message, db: Database, data: dict, description: Optional[str]
) -> None:
    """Persist income transaction using collected FSM data."""

    user = message.from_user
    assert user is not None
    amount = float(data.get("amount", 0))
    category = str(data.get("category"))
    user_id = await db.ensure_user(user.id, (user.full_name or "").strip() or str(user.id))
    await db.add_transaction(
        user_id=user_id,
        tx_type="income",
        amount=amount,
        category=category,
        description=description,
    )
    await message.answer(
        f"Добавлен доход: +{amount:.2f} в категории '{category}'.",
        reply_markup=MAIN_KB,
    )



__all__ = ["router"]
