#!/usr/bin/env python3
"""
Discord bot that forwards @mentions to a local Ollama model.
Now also prints what the model is “thinking” (the internal planning text)
before it produces the final answer, can reply to private whispers,
and **writes every chat exchange to a local SQLite database** so it can
be queried and survives a script reload.
"""

import asyncio
import atexit
import datetime
import json
import os
import sqlite3
from collections import defaultdict, deque

import aiohttp
from discord import Client, Intents, Member, Message


# -------------------------------- #
# 0. Helper: split a string into 2000‑char chunks
# -------------------------------- #
def _chunkify(text: str, limit: int = 2000) -> list[str]:
    """Return a list of strings, each ≤ limit in length."""
    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts


# -------------------------------- #
# 1. Configuration
# -------------------------------- #
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # <-- set this in your env
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api")
MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20B")

# -------------------------------- #
# 2. Database helper
# -------------------------------- #
DB_PATH = "chat_history.db"


def init_db() -> sqlite3.Connection:
    """Create the DB file and table if necessary."""
    conn = sqlite3.connect(
        DB_PATH,
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            user_name   TEXT,
            channel_id  INTEGER,
            is_dm       INTEGER,
            role        TEXT,
            content     TEXT,
            timestamp   TEXT
        )
        """
    )
    conn.commit()
    return conn


def insert_message(
    conn: sqlite3.Connection,
    user_id: int,
    channel_id: int,
    is_dm: bool,
    role: str,
    content: str,
    user_name: str,
) -> None:
    """Insert a single message record."""
    ts = datetime.datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO messages
            (user_id, user_name, channel_id, is_dm, role, content, timestamp)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, user_name, channel_id, int(is_dm), role, content, ts),
    )
    conn.commit()


def get_recent_messages(
    conn: sqlite3.Connection,
    user_id: int | None = None,
    channel_id: int | None = None,
    limit: int = 20,
) -> list[tuple]:
    """Return the most recent messages optionally filtered by user or channel."""
    sql = "SELECT * FROM messages"
    params = []
    conds = []
    if user_id is not None:
        conds.append("user_id = ?")
        params.append(user_id)
    if channel_id is not None:
        conds.append("channel_id = ?")
        params.append(channel_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    cur = conn.execute(sql, params)
    return cur.fetchall()


# Global connection – created once at import time
DB_CONN = init_db()


def close_db() -> None:
    """Close the database when the program exits."""
    if DB_CONN:
        DB_CONN.close()


# Register cleanup
atexit.register(close_db)


# -------------------------------- #
# 3. Bot client – make ``client.user`` writable for the test suite
# -------------------------------- #
class _TestableClient(Client):
    """Subclass of :class:`discord.Client` that allows setting ``client.user``."""

    def __init__(self, *args, **kwargs):  # pragma: no cover – trivial
        super().__init__(*args, **kwargs)
        self._user = None

    @property
    def user(self):  # pragma: no cover – trivial
        return self._user

    @user.setter
    def user(self, value):  # pragma: no cover – trivial
        self._user = value


intents = Intents.default()
intents.message_content = True
# Use the testable client so the tests can monkey‑patch ``client.user``.
client = _TestableClient(intents=intents)

# keep a short history per channel for context (you can persist if you wish)
channel_histories: defaultdict[int, deque] = defaultdict(lambda: deque(maxlen=20))

# keep a short history per DM (private whisper) for context
dm_histories: defaultdict[int, deque] = defaultdict(lambda: deque(maxlen=20))


# -------------------------------- #
# 4. Helper to call Ollama
# -------------------------------- #
async def ollama_chat(messages: list[dict]) -> tuple[str, str | None]:
    """
    Send a list of {role, content} to Ollama and return the assistant reply
    plus the “thinking” string (if any).
    """
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,  # set to True for real‑time streaming
        "think": True,  # <‑‑ request the internal thinking text
    }
    async with aiohttp.ClientSession() as session:
        resp = await session.post(f"{OLLAMA_URL}/chat", json=payload)
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Ollama error {resp.status}: {text}")
        data = await resp.json()
        return data["message"]["content"], data["message"].get("thinking")


# -------------------------------- #
# 5. On‑message handler
# -------------------------------- #
@client.event
async def on_message(message: Message):
    # -------------------------------- #
    # Guard to skip messages sent by the bot itself.
    # -------------------------------- #
    if message.author.bot:
        return

    # -------------------------------- #
    # Logging the incoming user message.
    # -------------------------------- #
    channel_name = message.guild.name if message.guild else "DM"
    print(f"[{channel_name}] {message.author} ({message.author.id}): {message.content}")

    insert_message(
        DB_CONN,
        user_id=message.author.id,
        channel_id=message.channel.id,
        is_dm=message.guild is None,
        role="user",
        content=message.content,
        user_name=str(message.author),
    )

    # -------------------------------- #
    # Handle DM channel.
    # -------------------------------- #
    if message.guild is None:  # DM channel
        history = list(dm_histories[message.author.id])
        history.append({"role": "user", "content": message.content})

        try:
            answer, thinking = await ollama_chat(history)
        except Exception as exc:
            await message.channel.send(f"⚠️ Error: {exc}")
            return

        if thinking:
            print(f"[Thinking] {thinking}")

        print(f"[Assistant] {answer}")

        insert_message(
            DB_CONN,
            user_id=message.author.id,
            channel_id=message.channel.id,
            is_dm=True,
            role="assistant",
            content=answer,
            user_name=str(message.author),
        )

        chunks = _chunkify(answer)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk)
            else:
                await message.channel.send(chunk)

        dm_histories[message.author.id].extend(
            history + [{"role": "assistant", "content": answer}]
        )
        return

    # -------------------------------- #
    # Handle public channel messages that mention the bot.
    # -------------------------------- #
    if message.mentions:
        history = list(channel_histories[message.channel.id])
        history.append({"role": "user", "content": message.content})

        # Clean up any bot mention in the user text
        for m in history:
            if isinstance(m["content"], str):
                m["content"] = m["content"].replace(f"<@{client.user.id}>", "").strip()

        try:
            answer, thinking = await ollama_chat(history)
        except Exception as exc:
            await message.reply(f"⚠️ Error: {exc}")
            return

        if thinking:
            print(f"[Thinking] {thinking}")

        print(f"[Assistant] {answer}")

        insert_message(
            DB_CONN,
            user_id=message.author.id,
            channel_id=message.channel.id,
            is_dm=False,
            role="assistant",
            content=answer,
            user_name=str(message.author),
        )

        chunks = _chunkify(answer)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk)
            else:
                await message.channel.send(chunk)

        channel_histories[message.channel.id].extend(
            history + [{"role": "assistant", "content": answer}]
        )


# -------------------------------- #
# 6. Run the bot
# -------------------------------- #
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set")
    client.run(DISCORD_TOKEN)
