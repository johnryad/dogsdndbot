"""Хранилище: партии, журнал действий и профили игроков.

- dnd_games   — одна строка JSON на чат: персонажи, хиты, сцена, бой, история.
- dnd_log     — всё, что происходило: заявки игроков, броски, ответы ведущего.
- dnd_players — профиль игрока между партиями: прошлые герои, их судьба и статистика.

Диск на Render стирается при каждом рестарте, поэтому в проде — внешний Postgres (Neon free).
Локально, без DATABASE_URL, — файл SQLite.
"""
import json
import asyncio
import logging
import sqlite3


class Storage:
    async def connect(self): ...
    async def load(self, chat_id: int) -> dict | None: ...
    async def save(self, chat_id: int, state: dict): ...
    async def active_chats(self) -> list[int]: ...
    async def log(self, chat_id: int, user_id: int | None, char: str, round_no: int, kind: str, text: str): ...
    async def recent_log(self, chat_id: int, user_id: int, char: str, limit: int) -> list[tuple[str, str]]: ...
    async def load_profile(self, user_id: int) -> dict | None: ...
    async def save_profile(self, user_id: int, profile: dict): ...
    async def wipe_chat(self, chat_id: int, player_ids: set[int]): ...
    async def close(self): ...


SQLITE_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS dnd_games (chat_id INTEGER PRIMARY KEY, state TEXT NOT NULL, "
    "status TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS dnd_log (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
    "user_id INTEGER, char TEXT, round INTEGER, kind TEXT NOT NULL, text TEXT NOT NULL, "
    "ts TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE INDEX IF NOT EXISTS dnd_log_chat ON dnd_log (chat_id, id)",
    "CREATE TABLE IF NOT EXISTS dnd_players (user_id INTEGER PRIMARY KEY, profile TEXT NOT NULL, "
    "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)",
]

POSTGRES_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS dnd_games (chat_id BIGINT PRIMARY KEY, state JSONB NOT NULL, "
    "status TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS dnd_log (id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, "
    "user_id BIGINT, char TEXT, round INTEGER, kind TEXT NOT NULL, text TEXT NOT NULL, "
    "ts TIMESTAMPTZ DEFAULT now())",
    "CREATE INDEX IF NOT EXISTS dnd_log_chat ON dnd_log (chat_id, id)",
    "CREATE TABLE IF NOT EXISTS dnd_players (user_id BIGINT PRIMARY KEY, profile JSONB NOT NULL, "
    "updated_at TIMESTAMPTZ DEFAULT now())",
]

# Записи журнала про игрока: его заявки и строки механики, где упомянут его герой
RECENT_SQL = (
    "SELECT kind, text FROM dnd_log WHERE chat_id = {0} AND kind IN ('action', 'mech') "
    "AND (user_id = {1} OR (kind = 'mech' AND text LIKE {2})) ORDER BY id DESC LIMIT {3}"
)


class SqliteStorage(Storage):
    def __init__(self, path: str):
        self.path = path
        self.db: sqlite3.Connection | None = None

    async def _run(self, sql: str, args: tuple = (), fetch: str | None = None, commit: bool = False):
        def work():
            cur = self.db.execute(sql, args)
            if commit:
                self.db.commit()
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
        return await asyncio.to_thread(work)

    async def connect(self):
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        for sql in SQLITE_SCHEMA:
            self.db.execute(sql)
        self.db.commit()

    async def load(self, chat_id):
        row = await self._run("SELECT state FROM dnd_games WHERE chat_id = ?", (chat_id,), "one")
        return json.loads(row[0]) if row else None

    async def save(self, chat_id, state):
        await self._run(
            "INSERT INTO dnd_games (chat_id, state, status) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET state = excluded.state, status = excluded.status, "
            "updated_at = CURRENT_TIMESTAMP",
            (chat_id, json.dumps(state, ensure_ascii=False), state.get("status", "")), commit=True,
        )

    async def active_chats(self):
        rows = await self._run("SELECT chat_id FROM dnd_games WHERE status IN ('lobby', 'active')", (), "all")
        return [r[0] for r in rows]

    async def log(self, chat_id, user_id, char, round_no, kind, text):
        await self._run("INSERT INTO dnd_log (chat_id, user_id, char, round, kind, text) VALUES (?, ?, ?, ?, ?, ?)",
                        (chat_id, user_id, char, round_no, kind, text), commit=True)

    async def recent_log(self, chat_id, user_id, char, limit):
        rows = await self._run(RECENT_SQL.format("?", "?", "?", "?"), (chat_id, user_id, f"%{char}%", limit), "all")
        return [(r[0], r[1]) for r in reversed(rows)]

    async def load_profile(self, user_id):
        row = await self._run("SELECT profile FROM dnd_players WHERE user_id = ?", (user_id,), "one")
        return json.loads(row[0]) if row else None

    async def save_profile(self, user_id, profile):
        await self._run(
            "INSERT INTO dnd_players (user_id, profile) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE "
            "SET profile = excluded.profile, updated_at = CURRENT_TIMESTAMP",
            (user_id, json.dumps(profile, ensure_ascii=False)), commit=True,
        )

    async def wipe_chat(self, chat_id, player_ids):
        await self._run("DELETE FROM dnd_games WHERE chat_id = ?", (chat_id,), commit=True)
        await self._run("DELETE FROM dnd_log WHERE chat_id = ?", (chat_id,), commit=True)
        for user_id in player_ids:
            profile = await self.load_profile(user_id)
            if profile is not None:
                profile["heroes"] = []
                await self.save_profile(user_id, profile)

    async def close(self):
        if self.db:
            self.db.close()


class PostgresStorage(Storage):
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        import asyncpg
        # statement_cache_size=0 — у Neon/Supabase пулер pgbouncer, кэш запросов он не переваривает
        self.pool = await asyncpg.create_pool(self.dsn, min_size=0, max_size=3, statement_cache_size=0)
        for sql in POSTGRES_SCHEMA:
            await self.pool.execute(sql)

    async def load(self, chat_id):
        row = await self.pool.fetchval("SELECT state::text FROM dnd_games WHERE chat_id = $1", chat_id)
        return json.loads(row) if row else None

    async def save(self, chat_id, state):
        await self.pool.execute(
            "INSERT INTO dnd_games (chat_id, state, status) VALUES ($1, $2::jsonb, $3) "
            "ON CONFLICT (chat_id) DO UPDATE SET state = EXCLUDED.state, status = EXCLUDED.status, updated_at = now()",
            chat_id, json.dumps(state, ensure_ascii=False), state.get("status", ""),
        )

    async def active_chats(self):
        rows = await self.pool.fetch("SELECT chat_id FROM dnd_games WHERE status IN ('lobby', 'active')")
        return [r["chat_id"] for r in rows]

    async def log(self, chat_id, user_id, char, round_no, kind, text):
        await self.pool.execute(
            "INSERT INTO dnd_log (chat_id, user_id, char, round, kind, text) VALUES ($1, $2, $3, $4, $5, $6)",
            chat_id, user_id, char, round_no, kind, text,
        )

    async def recent_log(self, chat_id, user_id, char, limit):
        rows = await self.pool.fetch(RECENT_SQL.format("$1", "$2", "$3", "$4"), chat_id, user_id, f"%{char}%", limit)
        return [(r["kind"], r["text"]) for r in reversed(rows)]

    async def load_profile(self, user_id):
        row = await self.pool.fetchval("SELECT profile::text FROM dnd_players WHERE user_id = $1", user_id)
        return json.loads(row) if row else None

    async def save_profile(self, user_id, profile):
        await self.pool.execute(
            "INSERT INTO dnd_players (user_id, profile) VALUES ($1, $2::jsonb) ON CONFLICT (user_id) "
            "DO UPDATE SET profile = EXCLUDED.profile, updated_at = now()",
            user_id, json.dumps(profile, ensure_ascii=False),
        )

    async def wipe_chat(self, chat_id, player_ids):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM dnd_games WHERE chat_id = $1", chat_id)
                await conn.execute("DELETE FROM dnd_log WHERE chat_id = $1", chat_id)
                for user_id in player_ids:
                    row = await conn.fetchrow("SELECT profile FROM dnd_players WHERE user_id = $1 FOR UPDATE", user_id)
                    if row:
                        profile = row["profile"]
                        profile["heroes"] = []
                        await conn.execute(
                            "UPDATE dnd_players SET profile = $2::jsonb, updated_at = now() WHERE user_id = $1",
                            user_id, json.dumps(profile, ensure_ascii=False),
                        )

    async def close(self):
        if self.pool:
            await self.pool.close()


def make_storage(database_url: str | None, sqlite_path: str = "dnd.sqlite3") -> Storage:
    if database_url and database_url.startswith(("postgres://", "postgresql://")):
        logging.info("ДнД: состояние храню в Postgres")
        return PostgresStorage(database_url)
    logging.warning(f"ДнД: DATABASE_URL не задан, храню в {sqlite_path} (на Render сотрётся при рестарте!)")
    return SqliteStorage(sqlite_path)
