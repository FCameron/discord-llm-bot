#!/usr/bin/env python3
"""
test_LlamaGPT.py – a fully‑isolated test‑suite for the *LlamaGPT* Discord bot.

The original test file exercised every public surface of the bot – text
chunking, the SQLite persistence layer, the Ollama HTTP integration and
the Discord event handlers – while keeping the external world (Discord,
Ollama, file system) completely mocked.

This rewritten version keeps the same test logic but cleans up the
structure, removes dead imports, normalises the comment style and
documents the intent of every fixture and test case.
"""
# --------------------------------------------------------------------------- #
# Imports
# --------------------------------------------------------------------------- #
import asyncio
import os
from unittest import mock

import aiohttp
import pytest


# --------------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db(tmp_path):
    """
    Provide a fresh :mod:`LlamaGPT` module that writes to a temporary SQLite
    database.

    The test suite patches the environment so the bot can initialise without
    a real Discord token, re‑imports the module with the DB path overridden
    and resets the atexit handler so the test run is isolated.
    """
    db_path = tmp_path / "chat_history.db"

    with mock.patch.dict(os.environ, {"DISCORD_TOKEN": "dummy"}):
        import importlib

        # Import the module *after* the environment variable is set
        import LlamaGPT

        # Override the DB path used by the module
        LlamaGPT.DB_PATH = str(db_path)

        # Re‑initialise the SQLite connection
        LlamaGPT.DB_CONN.close()
        LlamaGPT.DB_CONN = LlamaGPT.init_db()

        # Disable the real atexit handler – we want to keep the DB open for
        # the duration of the test.
        LlamaGPT.atexit.register = lambda *_, **__: None

        yield LlamaGPT

        # Final cleanup – close the DB once the fixture exits
        LlamaGPT.DB_CONN.close()


@pytest.fixture
def fake_user():
    """A minimal mock representing a Discord user."""
    user = mock.MagicMock()
    user.id = 123456
    user.name = "alice"
    user.__str__.return_value = "alice"
    user.bot = False
    # Provide a string for the mention attribute – required by the bot
    # logic that strips the mention from the user text.
    user.mention = ""  # or "<@999999>" if you want a realistic mention
    return user


@pytest.fixture
def fake_channel(fake_user):
    """A minimal mock representing a DM channel."""
    chan = mock.MagicMock()
    chan.id = 987654
    chan.guild = None
    chan.send = mock.AsyncMock()
    chan.reply = mock.AsyncMock()
    chan.name = "dm"
    return chan


@pytest.fixture
def fake_message(fake_user, fake_channel):
    """A minimal mock representing a Discord message sent in a DM."""
    msg = mock.MagicMock()
    msg.author = fake_user
    msg.content = "Hello @bot"
    msg.channel = fake_channel
    msg.guild = None
    msg.mentions = []
    msg.reply = mock.AsyncMock()
    return msg


@pytest.fixture
def fake_message_public(fake_user):
    """
    A minimal mock representing a message posted in a public channel
    that *mentions* the bot.
    """
    chan = mock.MagicMock()
    chan.id = 555
    chan.guild = mock.MagicMock()
    chan.guild.name = "TestGuild"
    chan.send = mock.AsyncMock()
    chan.reply = mock.AsyncMock()
    chan.name = "general"

    msg = mock.MagicMock()
    msg.author = fake_user
    msg.content = "Hey @bot can you help?"
    msg.channel = chan
    msg.guild = chan.guild
    msg.mentions = [mock.MagicMock(id=999999)]  # Bot user ID
    msg.reply = mock.AsyncMock()
    return msg


@pytest.fixture
def fake_client(LlamaGPT, fake_user):
    """
    A tiny wrapper that gives the tests access to the module‑level Discord
    client with a mocked ``user`` attribute.
    """
    client = LlamaGPT.client
    client.user = fake_user
    return client


# --------------------------------------------------------------------------- #
#  Tests
# --------------------------------------------------------------------------- #
def test_chunkify_simple():
    """Text shorter than the 2000‑char limit should not be split."""
    from LlamaGPT import chunk_text

    s = "a" * 2000
    assert chunk_text(s) == [s]


def test_chunkify_split_on_space():
    """Long text containing spaces is split at the nearest space."""
    from LlamaGPT import chunk_text

    s = " ".join(["word"] * 500)  # > 2000 chars
    chunks = chunk_text(s)

    # All chunks are within the size limit and concatenating them restores
    # the original text.
    assert all(len(c) <= 2000 for c in chunks)
    reassembled = "".join(c + " " for c in chunks[:-1]) + chunks[-1]
    assert reassembled == s.strip()


def test_chunkify_no_space_boundary(tmp_path):
    """If a word is longer than the limit it is cut in the middle."""
    from LlamaGPT import chunk_text

    s = "x" * 5000
    chunks = chunk_text(s)

    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == s


def test_insert_and_fetch(tmp_db, fake_user):
    """Insert and retrieve user/assistant messages from the DB."""
    # Insert a user message
    tmp_db._insert_message(
        tmp_db.DB_CONN,
        user_id=fake_user.id,
        channel_id=111,
        is_dm=False,
        role="user",
        content="Hello",
        user_name=str(fake_user),
    )
    # Insert an assistant message
    tmp_db._insert_message(
        tmp_db.DB_CONN,
        user_id=fake_user.id,
        channel_id=111,
        is_dm=False,
        role="assistant",
        content="Hi there",
        user_name=str(fake_user),
    )

    # Retrieve all messages
    rows = tmp_db.fetch_recent_messages(tmp_db.DB_CONN, limit=10)
    assert len(rows) == 2
    # The assistant message should appear first (most recent)
    assert rows[0][6] == "Hi there"
    assert rows[1][6] == "Hello"

    # Various filter combinations
    assert len(tmp_db.fetch_recent_messages(tmp_db.DB_CONN, user_id=fake_user.id)) == 2
    assert len(tmp_db.fetch_recent_messages(tmp_db.DB_CONN, channel_id=111)) == 2
    assert (
        len(
            tmp_db.fetch_recent_messages(
                tmp_db.DB_CONN, user_id=fake_user.id, channel_id=111
            )
        )
        == 2
    )
    assert tmp_db.fetch_recent_messages(tmp_db.DB_CONN, user_id=9999) == []


@pytest.mark.asyncio
async def test_ollama_chat_success(LlamaGPT):
    """`ollama_chat` returns the assistant content on HTTP 200."""

    async def fake_post(*_, **__):
        class Response:
            status = 200

            async def json(self):
                return {"message": {"content": "I am fine.", "thinking": "Reasoning."}}

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
        assert thinking == "Reasoning."


@pytest.mark.asyncio
async def test_ollama_chat_error(LlamaGPT):
    """Non‑200 responses raise a RuntimeError."""

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


@pytest.mark.asyncio
async def test_on_message_dm_success(tmp_db, fake_message, fake_client):
    """A DM triggers a reply and both messages are persisted."""

    async def fake_ollama(messages):
        return ("Answer text", "Thinking text")

    LlamaGPT.ollama_chat = fake_ollama

    await LlamaGPT.on_message(fake_message)

    # One direct reply – the assistant content is short enough
    assert fake_message.reply.call_count == 1
    assert fake_message.channel.send.call_count == 0

    # Check DB contains both user and assistant rows
    rows = tmp_db.fetch_recent_messages(
        tmp_db.DB_CONN,
        user_id=fake_message.author.id,
        channel_id=fake_message.channel.id,
        limit=2,
    )
    assert len(rows) == 2
    assert rows[0][5] == "assistant"
    assert rows[0][6] == "Answer text"


@pytest.mark.asyncio
async def test_on_message_dm_error(tmp_db, fake_message, fake_client):
    """If the assistant call fails, the bot sends an error to the DM."""

    async def fake_ollama(messages):
        raise RuntimeError("Something went wrong")

    LlamaGPT.ollama_chat = fake_ollama

    await LlamaGPT.on_message(fake_message)

    # The error is forwarded as a normal message in the DM channel
    assert fake_message.channel.send.call_count == 1
    sent_text = fake_message.channel.send.call_args[0][0]
    assert "⚠️ Error" in sent_text


@pytest.mark.asyncio
async def test_on_message_dm_chunks(tmp_db, fake_message, fake_client):
    """Long assistant responses are split across reply and channel.send."""
    long_answer = "A" * 4000  # > 2000 chars

    async def fake_ollama(messages):
        return (long_answer, None)

    LlamaGPT.ollama_chat = fake_ollama

    await LlamaGPT.on_message(fake_message)

    # Two chunks: one reply (first 2000 chars) and one channel.send (last 2000)
    assert fake_message.reply.call_count == 1
    assert fake_message.channel.send.call_count == 1

    first_chunk = fake_message.reply.call_args[0][0]
    second_chunk = fake_message.channel.send.call_args[0][0]
    assert len(first_chunk) == 2000
    assert len(second_chunk) == 2000


@pytest.mark.asyncio
async def test_on_message_public_success(tmp_db, fake_message_public, fake_client):
    """A public message that mentions the bot triggers a reply."""

    async def fake_ollama(messages):
        return ("Public answer", None)

    LlamaGPT.ollama_chat = fake_ollama

    await LlamaGPT.on_message(fake_message_public)

    assert fake_message_public.reply.call_count == 1
    assert fake_message_public.channel.send.call_count == 0

    rows = tmp_db.fetch_recent_messages(
        tmp_db.DB_CONN, channel_id=fake_message_public.channel.id, limit=2
    )
    assert len(rows) == 2
    assert rows[0][5] == "assistant"
    assert rows[0][6] == "Public answer"


@pytest.mark.asyncio
async def test_on_message_public_no_mention(tmp_db, fake_message_public, fake_client):
    """If the bot is not mentioned, the message is ignored."""
    fake_message_public.mentions = []
    await LlamaGPT.on_message(fake_message_public)

    fake_message_public.reply.assert_not_called()
    fake_message_public.channel.send.assert_not_called()

    rows = tmp_db.fetch_recent_messages(
        tmp_db.DB_CONN, channel_id=fake_message_public.channel.id, limit=20
    )
    assert all(r[5] != "assistant" for r in rows)


@pytest.mark.asyncio
async def test_on_message_public_error(tmp_db, fake_message_public, fake_client):
    """Assistant errors are sent back as a reply in public channels."""

    async def fake_ollama(messages):
        raise RuntimeError("Bad request")

    LlamaGPT.ollama_chat = fake_ollama

    await LlamaGPT.on_message(fake_message_public)

    fake_message_public.reply.assert_called_once()
    sent_text = fake_message_public.reply.call_args[0][0]
    assert "⚠️ Error" in sent_text
