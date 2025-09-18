# Telegram Finance Bot (aiogram + MySQL)

Бот учитывает доходы и расходы пользователей. Используется `aiogram v3` и `aiomysql`.

## Функционал
- Авторизация по Telegram ID (создание пользователя при первом обращении)
- Добавление расходов: `/add_expense <сумма> <категория> [описание]`
- Добавление доходов: `/add_income <сумма> <категория> [описание]`
- Баланс: `/balance`
- Статистика: `/stats day|week|month`
- Удалить последнюю запись: `/delete_last`

## Требования
- Python 3.10+
- MySQL 5.7+/8+

## Установка
```zsh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Переменные окружения
Создайте файл `.env` (или экспортируйте переменные), минимум:
```env
BOT_TOKEN=ваш_токен_бота
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=
MYSQL_DB=telegram_finance
# Для голосового ввода (OpenAI)
OPENAI_API_KEY=sk-...
```

Можно быстро создать БД:
```sql
CREATE DATABASE IF NOT EXISTS telegram_finance CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```
Таблицы создаются автоматически при старте бота.

## Запуск
```zsh
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)  # опционально, если используете .env
python bot.py
```

## Примечания
- Денежные суммы хранятся как DECIMAL(10,2).
- Все даты сохраняются как `created_at` (UTC на уровне приложения). Для простоты используются `CURRENT_TIMESTAMP` из MySQL.
- Голосовой ввод: отправьте голосовое или аудио-сообщение (OGG/MP3 и т.п.). Нужен `OPENAI_API_KEY`. На macOS ffmpeg обычно не обязателен, мы используем прямую загрузку файла в API; при необходимости установите `brew install ffmpeg`.

## Структура
- `bot.py` — точка входа, инициализация бота и БД
- `handlers.py` — обработчики команд
- `db.py` — работа с MySQL (aiomysql)

## Лицензия
MIT