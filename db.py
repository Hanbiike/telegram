"""Database access layer for the finance tracking bot.

This module provides asynchronous database operations using aiomysql.
It encapsulates connection pooling, schema initialization, and
CRUD-like helpers needed by the bot handlers.

Tables:
- users(id, telegram_id, name)
- transactions(id, user_id, type, amount, category, description, created_at)

Design notes:
- We use DECIMAL(10,2) for monetary values to avoid float rounding issues.
- Timestamps are stored in UTC using MySQL CURRENT_TIMESTAMP (naive),
  and comparisons are done using UTC datetimes in the application.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import aiomysql


@dataclass
class DBConfig:
    """Configuration for MySQL connection pool."""

    host: str
    port: int
    user: str
    password: str
    database: str
    minsize: int = 1
    maxsize: int = 10


class Database:
    """Async MySQL database helper using aiomysql pool."""

    def __init__(self, config: DBConfig) -> None:
        self._config = config
        self._pool: Optional[aiomysql.Pool] = None

    @classmethod
    def from_env(cls) -> "Database":
        """Create a Database instance by reading environment variables.

        Expected environment variables:
            MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB
        """

        host = os.getenv("MYSQL_HOST", "127.0.0.1")
        port = int(os.getenv("MYSQL_PORT", "3306"))
        user = os.getenv("MYSQL_USER", "root")
        password = os.getenv("MYSQL_PASSWORD", "")
        database = os.getenv("MYSQL_DB", "telegram_finance")
        cfg = DBConfig(
            host=host, port=port, user=user, password=password, database=database
        )
        return cls(cfg)

    async def connect(self) -> None:
        """Initialize the connection pool if not already initialized."""

        if self._pool is None:
            self._pool = await aiomysql.create_pool(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password,
                db=self._config.database,
                minsize=self._config.minsize,
                maxsize=self._config.maxsize,
                autocommit=True,
                charset="utf8mb4",
            )

    async def close(self) -> None:
        """Close the connection pool."""

        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def ensure_schema(self) -> None:
        """Create required tables if they don't exist."""

        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        telegram_id BIGINT NOT NULL UNIQUE,
                        name VARCHAR(255) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS transactions (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        user_id INT NOT NULL,
                        type ENUM('expense','income') NOT NULL,
                        amount DECIMAL(10,2) NOT NULL,
                        category VARCHAR(255) NOT NULL,
                        description TEXT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_transactions_user
                            FOREIGN KEY (user_id) REFERENCES users(id)
                            ON DELETE CASCADE,
                        INDEX idx_user_created_at (user_id, created_at),
                        INDEX idx_user_type_created (user_id, type, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    """
                )

    async def ensure_user(self, telegram_id: int, name: str) -> int:
        """Ensure a user exists; create if needed and return internal user id."""

        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM users WHERE telegram_id=%s",
                    (telegram_id,),
                )
                row = await cur.fetchone()
                if row:
                    return int(row[0])
                await cur.execute(
                    "INSERT INTO users(telegram_id, name) VALUES(%s, %s)",
                    (telegram_id, name),
                )
                user_id = cur.lastrowid
                return int(user_id)

    async def add_transaction(
        self,
        user_id: int,
        tx_type: str,
        amount: float,
        category: str,
        description: Optional[str] = None,
    ) -> int:
        """Insert a transaction and return its id.

        Parameters
        ----------
        user_id: int
            Internal user id from table users.id
        tx_type: str
            Either 'expense' or 'income'
        amount: float
            Positive amount, stored as DECIMAL(10,2)
        category: str
            Transaction category
        description: Optional[str]
            Optional free text
        """

        if tx_type not in {"expense", "income"}:
            raise ValueError("tx_type must be 'expense' or 'income'")
        if amount <= 0:
            raise ValueError("amount must be positive")
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    (
                        "INSERT INTO transactions"
                        "(user_id, type, amount, category, description)"
                        " VALUES(%s, %s, %s, %s, %s)"
                    ),
                    (user_id, tx_type, amount, category, description),
                )
                tx_id = cur.lastrowid
                return int(tx_id)

    async def get_balance(self, user_id: int) -> float:
        """Return current balance = sum(incomes) - sum(expenses)."""

        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    (
                        "SELECT type, COALESCE(SUM(amount), 0)"
                        " FROM transactions"
                        " WHERE user_id=%s"
                        " GROUP BY type"
                    ),
                    (user_id,),
                )
                income_sum = 0.0
                expense_sum = 0.0
                async for row in cur:
                    tx_type, total = row[0], float(row[1] or 0)
                    if tx_type == "income":
                        income_sum += total
                    elif tx_type == "expense":
                        expense_sum += total
                return round(income_sum - expense_sum, 2)

    async def _period_start(self, period: str) -> datetime:
        """Compute UTC start datetime for a period: day|week|month|year."""

        now = datetime.utcnow()
        if period == "day":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "week":
            # Monday is 0
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            delta_days = start_of_day.weekday()
            return start_of_day - timedelta(days=delta_days)
        if period == "month":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if period == "year":
            return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        raise ValueError("period must be one of: day, week, month")

    async def get_stats(self, user_id: int, period: str) -> Dict[str, Any]:
        """Return stats for the given period.

        Result example:
        {
            'period': 'week',
            'from': datetime,
            'income_total': 123.45,
            'expense_total': 67.89,
            'by_category': {
                'income': [(category, total), ...],
                'expense': [(category, total), ...],
            }
        }
        """

        start = await self._period_start(period)
        await self.connect()
        assert self._pool is not None
        income_total = 0.0
        expense_total = 0.0
        by_category: Dict[str, list[Tuple[str, float]]] = {
            "income": [],
            "expense": [],
        }

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Totals
                await cur.execute(
                    (
                        "SELECT type, COALESCE(SUM(amount), 0)"
                        " FROM transactions"
                        " WHERE user_id=%s AND created_at >= %s"
                        " GROUP BY type"
                    ),
                    (user_id, start),
                )
                rows = await cur.fetchall()
                for tx_type, total in rows:
                    total_f = float(total or 0)
                    if tx_type == "income":
                        income_total = total_f
                    elif tx_type == "expense":
                        expense_total = total_f

                # By category (top 10 each)
                await cur.execute(
                    (
                        "SELECT type, category, COALESCE(SUM(amount), 0) AS total"
                        " FROM transactions"
                        " WHERE user_id=%s AND created_at >= %s"
                        " GROUP BY type, category"
                        " ORDER BY type, total DESC"
                        " LIMIT 50"
                    ),
                    (user_id, start),
                )
                rows = await cur.fetchall()
                for tx_type, category, total in rows:
                    by_category[tx_type].append((str(category), float(total or 0)))

        return {
            "period": period,
            "from": start,
            "income_total": round(income_total, 2),
            "expense_total": round(expense_total, 2),
            "by_category": by_category,
        }

    async def delete_last_transaction(self, user_id: int) -> bool:
        """Delete the last transaction by created_at for the user.

        Returns True if a transaction was deleted, False otherwise.
        """

        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    (
                        "SELECT id FROM transactions"
                        " WHERE user_id=%s"
                        " ORDER BY created_at DESC, id DESC"
                        " LIMIT 1"
                    ),
                    (user_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return False
                tx_id = int(row[0])
                await cur.execute("DELETE FROM transactions WHERE id=%s", (tx_id,))
                return True


__all__ = ["DBConfig", "Database"]
