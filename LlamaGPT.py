#!/usr/bin/env python3
"""
Discord bot that forwards @mentions to a local Ollama model.

Features
--------
* Prints the model’s “thinking” text before the final reply.
* Responds to private whispers (DMs).
* Persists every chat exchange in an SQLite database for persistence
  across restarts and easy querying.
"""

# --------------------------------------------------------------------------- #
# Imports
# --------------------------------------------------------------------------- #
import atexit
import datetime
import os
import sqlite3
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Tuple, Union

import aiohttp
import discord
from discord import Client, Intents, Message

# --------------------------------------------------------------------------- #
# 1. Constants / Configuration
# --------------------------------------------------------------------------- #
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # Required
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api")
MODEL_NAME = os.getenv("OLLAMA_MODEL", "gpt-oss:20B")

# SQLite database that holds the chat history
DB_PATH = "chat_history.db"


# --------------------------------------------------------------------------- #
# 2. Helper: split a string into Discord‑friendly chunks (≤ 2000 chars)
# --------------------------------------------------------------------------- #
def chunk_text(text: str, limit: int = 2000) -> List[str]:
    """
    Split *text* into a list of strings each no longer than *limit* characters.
    Splits on the last space before *limit* to avoid breaking words.
    """
    parts: List[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break

        cut = text.rfind(" ", 0, limit)
        if cut == -1:  # No space found → hard cut
            cut = limit

        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()

    return parts


# --------------------------------------------------------------------------- #
# 3. Database helpers
# --------------------------------------------------------------------------- #
def init_db() -> sqlite3.Connection:
    """
    Create the SQLite file and table if they do not exist.
    Returns a connection object that stays open for the lifetime of the bot.
    """
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


def _insert_message(
    conn: sqlite3.Connection,
    user_id: int,
    channel_id: int,
    is_dm: bool,
    role: str,
    content: str,
    user_name: str,
) -> None:
    """Insert a single chat message into the database."""
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


def fetch_recent_messages(
    conn: sqlite3.Connection,
    user_id: int | None = None,
    channel_id: int | None = None,
    limit: int = 20,
) -> List[Tuple[Any, ...]]:
    """
    Retrieve the *limit* most recent messages, optionally filtered by
    *user_id* or *channel_id*.
    """
    sql = "SELECT * FROM messages"
    params: List[Any] = []
    clauses: List[str] = []

    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)

    if channel_id is not None:
        clauses.append("channel_id = ?")
        params.append(channel_id)

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(sql, params)
    return cursor.fetchall()


# Global DB connection – opened once at import time
DB_CONN = init_db()


def close_db() -> None:
    """Close the SQLite connection on interpreter shutdown."""
    if DB_CONN:
        DB_CONN.close()


atexit.register(close_db)


# --------------------------------------------------------------------------- #
# 4. Discord client (test‑friendly)
# --------------------------------------------------------------------------- #
class _TestableClient(Client):
    """
    Subclass of :class:`discord.Client` that allows the unit‑test suite to
    monkey‑patch ``client.user`` by setting the property directly.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._user = None

    @property
    def user(self) -> discord.User | None:  # pragma: no cover
        return self._user

    @user.setter
    def user(self, value: discord.User | None) -> None:  # pragma: no cover
        self._user = value


intents = Intents.default()
intents.message_content = True
client = _TestableClient(intents=intents)

# Short in‑memory history used to build context for the model.
# The history is capped at 20 messages per channel/DM.
channel_histories: defaultdict[int, deque] = defaultdict(lambda: deque(maxlen=20))
dm_histories: defaultdict[int, deque] = defaultdict(lambda: deque(maxlen=20))


# --------------------------------------------------------------------------- #
# 5. Ollama helper
# --------------------------------------------------------------------------- #
async def ollama_chat(messages: List[Dict[str, str]]) -> Tuple[str, str | None]:
    """
    Send a conversation *messages* list to Ollama’s chat endpoint.
    Returns the assistant’s final reply and the optional “thinking” text.
    """
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,  # stream==True would give real‑time chunks
        "think": True,  # request the internal planning text
    }

    async with aiohttp.ClientSession() as session:
        resp = await session.post(f"{OLLAMA_URL}/chat", json=payload)

        if resp.status != 200:
            error_msg = await resp.text()
            raise RuntimeError(f"Ollama error {resp.status}: {error_msg}")

        data = await resp.json()
        return data["message"]["content"], data["message"].get("thinking")


# --------------------------------------------------------------------------- #
# 6. Message handling
# --------------------------------------------------------------------------- #
@client.event
async def on_message(message: Message) -> None:
    """Main entry point for every incoming message."""
    # Skip messages sent by the bot itself to avoid infinite loops
    if message.author.bot:
        return

    # Log the user message for debugging
    channel_name = message.guild.name if message.guild else "DM"
    print(f"[{channel_name}] {message.author} ({message.author.id}): {message.content}")

    # Persist the incoming user message
    _insert_message(
        DB_CONN,
        user_id=message.author.id,
        channel_id=message.channel.id,
        is_dm=message.guild is None,
        role="user",
        content=message.content,
        user_name=str(message.author),
    )

    # ----------------------------------------------------------------------- #
    # Handle direct messages (DMs)
    # ----------------------------------------------------------------------- #
    if message.guild is None:  # Private whisper
        history = list(dm_histories[message.author.id])
        history.append({"role": "user", "content": message.content})

        try:
            answer, thinking = await ollama_chat(history)
        except Exception as exc:
            await message.channel.send(f"⚠️ Error: {exc}")
            return

        # Log the internal “thinking” text if it exists
        if thinking:
            print(f"[Thinking] {thinking}")
            _insert_message(
                DB_CONN,
                user_id=message.author.id,
                channel_id=message.channel.id,
                is_dm=True,
                role="thinking",
                content=thinking,
                user_name=str(message.author),
            )

        print(f"[Assistant] {answer}")

        # Persist the assistant reply
        _insert_message(
            DB_CONN,
            user_id=message.author.id,
            channel_id=message.channel.id,
            is_dm=True,
            role="assistant",
            content=answer,
            user_name=str(message.author),
        )

        # Send the reply (split into chunks if it is too long)
        for i, chunk in enumerate(chunk_text(answer)):
            if i == 0:
                await message.reply(chunk)
            else:
                await message.channel.send(chunk)

        # Update the in‑memory history for future turns
        dm_histories[message.author.id].extend(
            history + [{"role": "assistant", "content": answer}]
        )
        return

    # ----------------------------------------------------------------------- #
    # Handle public channel messages that mention the bot
    # ----------------------------------------------------------------------- #
    if message.mentions:
        history = list(channel_histories[message.channel.id])
        history.append({"role": "user", "content": message.content})

        # Remove the bot’s mention from the user text so the model sees the
        # actual prompt content only.
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
            _insert_message(
                DB_CONN,
                user_id=message.author.id,
                channel_id=message.channel.id,
                is_dm=False,
                role="thinking",
                content=thinking,
                user_name=str(message.author),
            )

        print(f"[Assistant] {answer}")

        _insert_message(
            DB_CONN,
            user_id=message.author.id,
            channel_id=message.channel.id,
            is_dm=False,
            role="assistant",
            content=answer,
            user_name=str(message.author),
        )

        for i, chunk in enumerate(chunk_text(answer)):
            if i == 0:
                await message.reply(chunk)
            else:
                await message.channel.send(chunk)

        channel_histories[message.channel.id].extend(
            history + [{"role": "assistant", "content": answer}]
        )


# --------------------------------------------------------------------------- #
# 7. Run the bot
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable is missing")

    client.run(DISCORD_TOKEN)
