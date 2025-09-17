"""Entry point for the Telegram finance bot using aiogram v3.

Environment variables:
- BOT_TOKEN: Telegram bot token
- MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB: MySQL config

Run:
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Dict, Any

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import Update
from aiogram.fsm.storage.memory import MemoryStorage

from db import Database
from handlers import router as handlers_router
from dotenv import load_dotenv


logging.basicConfig(level=logging.INFO)


class DBMiddleware(BaseMiddleware):
    """Inject Database instance into handler kwargs."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Any],
        event: Update,
        data: Dict[str, Any],
    ) -> Any:
        data["db"] = self.db
        return await handler(event, data)


@asynccontextmanager
async def lifespan(dp: Dispatcher, db: Database) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle: ensure schema and close db."""

    await db.connect()
    await db.ensure_schema()
    try:
        yield
    finally:
        await db.close()


async def main() -> None:
    # Load environment variables from .env if present
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())

    # Database
    db = Database.from_env()

    # Register routers
    dp.message.middleware(DBMiddleware(db))
    dp.include_router(handlers_router)

    # Run polling with lifespan management
    async with lifespan(dp, db):
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
