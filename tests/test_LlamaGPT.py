#!/usr/bin/env python3
"""
Comprehensive test‑suite for LlamaGPT.py.

The tests cover:

* ``_chunkify`` – correct splitting and edge‑cases.
* Database helpers – ``init_db``, ``insert_message`` and ``get_recent_messages``.
* The Ollama integration – successful response and error handling.
* The message‑handling logic for both DMs and public channels.
* Database persistence of both user and assistant messages.

All external interactions (Discord objects, HTTP calls and atexit hooks) are
fully mocked so that the tests run in isolation and do not depend on a real
Discord server or an Ollama instance.
"""

import asyncio
import json
import os
import sqlite3
import sys
import types
from pathlib import Path
from unittest import mock

import aiohttp
import pytest


# -------------------------------- #
# Helper fixtures
# -------------------------------- #
@pytest.fixture
def tmp_db(tmp_path):
    """Return a path to a temporary SQLite database and a fresh module."""
    db_path = tmp_path / "chat_history.db"

    # Ensure we are loading the module *after* setting the DB_PATH
    with mock.patch.dict(os.environ, {"DISCORD_TOKEN": "dummy"}):
        # Re‑import the module with a temporary DB_PATH
        import importlib

        import LlamaGPT

        # Override the DB_PATH used by the module
        LlamaGPT.DB_PATH = str(db_path)
        # Re‑initialise the DB connection
        LlamaGPT.DB_CONN.close()
        LlamaGPT.DB_CONN = LlamaGPT.init_db()
        # Register a dummy atexit handler so the tests do not interfere
        LlamaGPT.atexit.register = lambda *_, **__: None
        yield LlamaGPT
        # Cleanup
        LlamaGPT.DB_CONN.close()


@pytest.fixture
def fake_user():
    """Simple mock for a Discord user."""
    u = mock.MagicMock()
    u.id = 123456
    u.name = "alice"
    u.__str__.return_value = "alice"
    u.bot = False
    return u


@pytest.fixture
def fake_channel(fake_user):
    """Simple mock for a Discord channel."""
    ch = mock.MagicMock()
    ch.id = 987654
    ch.guild = None
    ch.send = mock.AsyncMock()
    ch.reply = mock.AsyncMock()
    ch.name = "dm"
    return ch


@pytest.fixture
def fake_message(fake_user, fake_channel):
    """Simple mock for a Discord message."""
    m = mock.MagicMock()
    m.author = fake_user
    m.content = "Hello @bot"
    m.channel = fake_channel
    m.guild = None
    m.mentions = []
    m.reply = mock.AsyncMock()
    return m


@pytest.fixture
def fake_message_public(fake_user):
    """Mock for a public message that mentions the bot."""
    ch = mock.MagicMock()
    ch.id = 555
    ch.guild = mock.MagicMock()
    ch.guild.name = "TestGuild"
    ch.send = mock.AsyncMock()
    ch.reply = mock.AsyncMock()
    ch.name = "general"

    m = mock.MagicMock()
    m.author = fake_user
    m.content = "Hey @bot can you help?"
    m.channel = ch
    m.guild = ch.guild
    m.mentions = [mock.MagicMock(id=999999)]  # bot user
    m.reply = mock.AsyncMock()
    return m


@pytest.fixture
def fake_client(LlamaGPT, fake_user):
    """Mock for the Discord client."""
    client = LlamaGPT.client
    client.user = fake_user
    return client


# -------------------------------- #
# Tests for the helper functions
# -------------------------------- #
def test_chunkify_simple():
    from LlamaGPT import _chunkify

    s = "a" * 2000
    assert _chunkify(s) == [s]


def test_chunkify_split_on_space():
    from LlamaGPT import _chunkify

    s = " ".join(["word"] * 500)  # > 2000 chars
    chunks = _chunkify(s)
    # All chunks should be <= 2000 and contiguous
    for c in chunks[:-1]:
        assert len(c) <= 2000
    assert len(chunks[-1]) <= 2000
    # Reassemble
    assert "".join(c + " " for c in chunks[:-1]) + chunks[-1] == s.strip()


def test_chunkify_no_space_boundary(tmp_path):
    from LlamaGPT import _chunkify

    # A long word that needs to be cut mid‑word
    s = "x" * 5000
    chunks = _chunkify(s)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == s


# -------------------------------- #
# Tests for database helpers
# -------------------------------- #
def test_insert_and_fetch(tmp_db, fake_user):
    # Insert a user message
    tmp_db.insert_message(
        tmp_db.DB_CONN,
        user_id=fake_user.id,
        channel_id=111,
        is_dm=False,
        role="user",
        content="Hello",
        user_name=str(fake_user),
    )
    # Insert an assistant message
    tmp_db.insert_message(
        tmp_db.DB_CONN,
        user_id=fake_user.id,
        channel_id=111,
        is_dm=False,
        role="assistant",
        content="Hi there",
        user_name=str(fake_user),
    )

    # Fetch without filters
    rows = tmp_db.get_recent_messages(tmp_db.DB_CONN, limit=10)
    assert len(rows) == 2
    assert rows[0][6] == "Hi there"  # content column
    assert rows[1][6] == "Hello"

    # Filter by user
    rows_user = tmp_db.get_recent_messages(tmp_db.DB_CONN, user_id=fake_user.id)
    assert len(rows_user) == 2

    # Filter by channel
    rows_chan = tmp_db.get_recent_messages(tmp_db.DB_CONN, channel_id=111)
    assert len(rows_chan) == 2

    # Filter by both
    rows_both = tmp_db.get_recent_messages(
        tmp_db.DB_CONN, user_id=fake_user.id, channel_id=111
    )
    assert len(rows_both) == 2

    # Non‑existent filter
    rows_none = tmp_db.get_recent_messages(tmp_db.DB_CONN, user_id=9999)
    assert rows_none == []


# -------------------------------- #
# Tests for Ollama integration
# -------------------------------- #
@pytest.mark.asyncio
async def test_ollama_chat_success(LlamaGPT):
    # Patch the session to return a controlled response
    async def fake_post(*_, **__):
        class Response:
            status = 200

            async def json(self):
                return {
                    "message": {
                        "content": "I am fine.",
                        "thinking": "Reasoning about the request.",
                    }
                }

            async def text(self):
                return ""

        return Response()

    with mock.patch(
        "aiohttp.ClientSession.post", new=mock.AsyncMock(side_effect=fake_post)
    ):
        answer, thinking = await LlamaGPT.ollama_chat(
            [{"role": "user", "content": "Hi"}]
        )
        assert answer == "I am fine."
        assert thinking == "Reasoning about the request."


@pytest.mark.asyncio
async def test_ollama_chat_error(LlamaGPT):
    async def fake_post(*_, **__):
        class Response:
            status = 500

            async def text(self):
                return "Server error"

        return Response()

    with mock.patch(
        "aiohttp.ClientSession.post", new=mock.AsyncMock(side_effect=fake_post)
    ):
        with pytest.raises(RuntimeError) as exc:
            await LlamaGPT.ollama_chat([{"role": "user", "content": "Hi"}])
        assert "Ollama error 500" in str(exc.value)


# -------------------------------- #
# Tests for message handling (DM)
# -------------------------------- #
@pytest.mark.asyncio
async def test_on_message_dm_success(tmp_db, fake_message, fake_client):
    # Replace the ollama_chat function to return deterministic data
    async def fake_ollama(messages):
        return ("Answer text", "Thinking text")

    LlamaGPT.ollama_chat = fake_ollama

    # Run the event handler
    await LlamaGPT.on_message(fake_message)

    # The DM should have replied (reply called once)
    assert fake_message.reply.call_count == 1
    # And a subsequent send if needed (none, because answer < 2000 chars)
    assert fake_message.channel.send.call_count == 0

    # Verify that both user and assistant messages are in the DB
    rows = tmp_db.get_recent_messages(
        tmp_db.DB_CONN,
        user_id=fake_message.author.id,
        channel_id=fake_message.channel.id,
        limit=2,
    )
    assert len(rows) == 2
    # The last message should be the assistant
    assert rows[0][5] == "assistant"
    assert rows[0][6] == "Answer text"


@pytest.mark.asyncio
async def test_on_message_dm_error(tmp_db, fake_message, fake_client):
    async def fake_ollama(messages):
        raise RuntimeError("Something went wrong")

    LlamaGPT.ollama_chat = fake_ollama

    await LlamaGPT.on_message(fake_message)

    # Should have sent an error message to the channel
    assert fake_message.channel.send.call_count == 1
    sent_text = fake_message.channel.send.call_args[0][0]
    assert "⚠️ Error" in sent_text


# -------------------------------- #
# Tests for message handling (public channel)
# -------------------------------- #
@pytest.mark.asyncio
async def test_on_message_public_success(tmp_db, fake_message_public, fake_client):
    async def fake_ollama(messages):
        return ("Public answer", None)

    LlamaGPT.ollama_chat = fake_ollama

    # Run the handler
    await LlamaGPT.on_message(fake_message_public)

    # The bot should reply to the original message
    assert fake_message_public.reply.call_count == 1
    assert fake_message_public.channel.send.call_count == 0

    # DB contains user + assistant messages
    rows = tmp_db.get_recent_messages(
        tmp_db.DB_CONN, channel_id=fake_message_public.channel.id, limit=2
    )
    assert len(rows) == 2
    assert rows[0][5] == "assistant"
    assert rows[0][6] == "Public answer"


@pytest.mark.asyncio
async def test_on_message_public_no_mention(tmp_db, fake_message_public, fake_client):
    # Change the mentions list so the bot is not mentioned
    fake_message_public.mentions = []
    await LlamaGPT.on_message(fake_message_public)
    # Nothing should happen
    fake_message_public.reply.assert_not_called()
    fake_message_public.channel.send.assert_not_called()
    rows = tmp_db.get_recent_messages(
        tmp_db.DB_CONN, channel_id=fake_message_public.channel.id, limit=20
    )
    # No new rows should have been inserted
    assert all(r[5] != "assistant" for r in rows)


@pytest.mark.asyncio
async def test_on_message_public_error(tmp_db, fake_message_public, fake_client):
    async def fake_ollama(messages):
        raise RuntimeError("Bad request")

    LlamaGPT.ollama_chat = fake_ollama

    await LlamaGPT.on_message(fake_message_public)

    # Error message should be sent as a reply to the user
    fake_message_public.reply.assert_called_once()
    sent_text = fake_message_public.reply.call_args[0][0]
    assert "⚠️ Error" in sent_text


# -------------------------------- #
# Tests for chunked replies
# -------------------------------- #
@pytest.mark.asyncio
async def test_on_message_dm_chunks(tmp_db, fake_message, fake_client):
    long_answer = "A" * 4000  # > 2000 chars

    async def fake_ollama(messages):
        return (long_answer, None)

    LlamaGPT.ollama_chat = fake_ollama

    await LlamaGPT.on_message(fake_message)

    # Two chunks should be sent: one reply and one send
    assert fake_message.reply.call_count == 1
    assert fake_message.channel.send.call_count == 1
    # Verify the first chunk is 2000 chars and second is 2000 chars
    first_chunk = fake_message.reply.call_args[0][0]
    second_chunk = fake_message.channel.send.call_args[0][0]
    assert len(first_chunk) == 2000
    assert len(second_chunk) == 2000
