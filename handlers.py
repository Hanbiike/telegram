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

from typing import Optional, Tuple, List

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
from voice import (
    transcribe_file_to_text,
    parse_transaction_text,
    Transaction as VoiceTransaction,
)
from aiogram.types import Voice as TgVoice, Audio as TgAudio, Document as TgDocument
import tempfile
import os
import asyncio
import subprocess


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
    """States for confirming voice-recognized transactions."""
    confirm = State()


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
        "/delete_last",
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


# -------- Voice input ---------

async def _download_file(message: Message, file_id: str) -> str:
    """Download Telegram file, convert to WAV if needed, and return path."""

    bot = message.bot
    file = await bot.get_file(file_id)

    # исходное расширение (по пути телеги)
    ext = os.path.splitext(file.file_path or "")[1].lower() or ".oga"

    # сохраняем исходный файл во временный путь
    fd, src_path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    await bot.download_file(file.file_path, destination=src_path)

    # если это ogg/opus/oga — конвертируем в wav 16k mono
    if ext in {".oga", ".ogg", ".opus"}:
        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        # ffmpeg командой в фоне
        cmd = [
            "ffmpeg",
            "-y",                # перезаписать если есть
            "-i", src_path,      # входной файл
            "-ar", "16000",      # sample rate 16kHz
            "-ac", "1",          # моно
            wav_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        # можно удалить оригинал
        try:
            os.remove(src_path)
        except OSError:
            pass

        return wav_path

    # если уже поддерживаемое расширение — возвращаем как есть
    return src_path


@router.message(F.voice | F.audio)
async def on_voice_or_audio(message: Message, db: Database, state: FSMContext) -> None:
    """Handle voice or audio message: transcribe and parse transaction."""

    file_id = None
    if message.voice:
        file_id = message.voice.file_id
    elif message.audio:
        file_id = message.audio.file_id
    else:
        await message.answer("Не удалось распознать голосовое сообщение.", reply_markup=MAIN_KB)
        return

    await message.answer("Обрабатываю голосовое сообщение…")
    try:
        path = await _download_file(message, file_id)
        text = transcribe_file_to_text(path)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

    if not text:
        await message.answer(
            "Не удалось распознать речь. Попробуйте ещё раз или используйте команды.",
            reply_markup=MAIN_KB,
        )
        return

    vt = parse_transaction_text(text)
    if vt is None:
        await message.answer(
            "Не удалось понять транзакцию из текста. Скажите, например: 'расход 200 еда обед'",
            reply_markup=MAIN_KB,
        )
        return

    # Сохраняем данные транзакции для подтверждения
    await state.set_state(VoiceConfirmStates.confirm)
    await state.update_data(
        amount=float(vt.sum),
        category=vt.category,
        tx_type=vt.type,
        description=vt.description,
        original_text=text,
    )
    
    sign = "+" if vt.type == "income" else "-"
    tx_type_ru = "доход" if vt.type == "income" else "расход"
    desc_text = f", описание: {vt.description}" if vt.description else ""
    
    await message.answer(
        f"Распознанная транзакция:\n"
        f"{tx_type_ru.capitalize()}: {sign}{vt.sum:.2f}\n"
        f"Категория: {vt.category}{desc_text}\n"
        f"Текст: \"{text}\"\n\n"
        f"Сохранить эту транзакцию?",
        reply_markup=YES_NO_KB,
    )


@router.message(VoiceConfirmStates.confirm)
async def voice_confirm_transaction(message: Message, state: FSMContext, db: Database) -> None:
    """Handle confirmation of voice-recognized transaction."""
    
    answer = (message.text or "").strip().lower()
    data = await state.get_data()
    
    if answer == "отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=MAIN_KB)
        return
    
    if answer == "да":
        user = message.from_user
        assert user is not None
        user_id = await db.ensure_user(user.id, (user.full_name or "").strip() or str(user.id))
        
        amount = data["amount"]
        category = data["category"]
        tx_type = data["tx_type"]
        description = data.get("description")
        
        await db.add_transaction(
            user_id=user_id,
            tx_type=tx_type,
            amount=amount,
            category=category,
            description=description,
        )
        
        sign = "+" if tx_type == "income" else "-"
        await state.clear()
        await message.answer(
            f"Транзакция сохранена: {sign}{amount:.2f} '{category}'",
            reply_markup=MAIN_KB,
        )
        return
    
    if answer == "нет":
        await state.clear()
        await message.answer(
            "Транзакция отменена. Попробуйте записать голосовое сообщение ещё раз или используйте команды.",
            reply_markup=MAIN_KB,
        )
        return
    
    await message.answer("Пожалуйста, выберите 'Да' или 'Нет'.", reply_markup=YES_NO_KB)


__all__ = ["router"]
