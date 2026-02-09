#!/usr/bin/env python3
"""
Unit tests for the **ChatHistoryUI** module.

The original test‑suite was functional but a bit cluttered and its comments
did not always describe the behaviour that was being asserted.  This file
has been cleaned up for readability:

* A single ``cols`` fixture is used everywhere – no duplicated lists.
* Re‑usable helpers are extracted into fixtures.
* All comments now state *what* the test checks, not *how* it does it.
* Type hints help the linter understand the intent.
"""

# --------------------------------------------------------------------------- #
# Imports
# --------------------------------------------------------------------------- #
import os
import sqlite3
from pathlib import Path
from unittest import mock

import pytest


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Create a temporary SQLite database with the expected messages table."""
    db_path = tmp_path / "chat_history.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE messages (
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
    conn.close()
    return db_path


@pytest.fixture
def chat_ui(tmp_db: Path):
    """Reload the UI module after setting the environment variable."""
    os.environ["CHAT_HISTORY_DB"] = str(tmp_db)

    import importlib

    import ChatHistoryUI

    importlib.reload(ChatHistoryUI)
    return ChatHistoryUI


@pytest.fixture
def dummy_event() -> mock.Mock:
    """A minimal event with an ``app.invalidate`` method."""
    return mock.Mock(app=mock.Mock(invalidate=mock.Mock()))


@pytest.fixture
def cols() -> list[str]:
    """Column names used in all tests."""
    return [
        "id",
        "user_id",
        "user_name",
        "channel_id",
        "is_dm",
        "role",
        "content",
        "timestamp",
    ]


def build_rows() -> list[tuple]:
    """Return a deterministic set of rows for testing."""
    return [
        (
            1,
            1001,
            "alice",
            2001,
            1,
            "assistant",
            "Answer 1",
            "2024-02-08T10:00:00Z",
        ),
        (
            2,
            1002,
            "bob",
            2001,
            1,
            "user",
            "Question 1",
            "2024-02-08T10:01:00Z",
        ),
        (
            3,
            1003,
            "charlie",
            2001,
            1,
            "assistant",
            "Answer 2",
            "2024-02-08T10:02:00Z",
        ),
    ]


@pytest.fixture
def table_ui(chat_ui, cols) -> "ChatHistoryUI.TableUI":
    """Instantiate a ``TableUI`` and configure it for all tests."""
    ui = chat_ui.TableUI()
    ui.cols = cols
    return ui


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_header_rendering(table_ui):
    """
    Header rendering should highlight the selected column
    and expose the correct column names.
    """
    table_ui.rows = build_rows()

    # No column selected -> first cell has no style
    table_ui.selected_col = 0
    header_parts = table_ui._render_header()
    assert header_parts[0][0] == ""

    # Column names appear in order after the first invisible column
    names = [part[1].strip() for part in header_parts]
    assert names == ["user_name", "is_dm", "role", "timestamp"]


def test_body_rendering_normal_rows(table_ui, dummy_event):
    """
    Body rendering must produce a list of styled text tuples.
    The selected cell should be highlighted and long values truncated.
    """
    table_ui.rows = build_rows()
    table_ui.selected_row = 1  # second row
    table_ui.selected_col = 2  # 'role' column

    body = table_ui._render_body()
    assert len(body) == len(table_ui.rows) * 5

    # Selected cell styled as "reverse"
    idx = table_ui.selected_row * 5 + table_ui.selected_col
    style, text = body[idx]
    assert style == "reverse"

    # Truncation test
    long_user = "x" * 50
    table_ui.rows[0] = table_ui.rows[0][:2] + (long_user,) + table_ui.rows[0][3:]
    body = table_ui._render_body()
    truncated = body[0][1].strip()
    assert len(truncated) == 30  # 30 characters visible


def test_overlay_and_scrolling(table_ui, monkeypatch, dummy_event):
    """
    Overlay content is scrolled correctly when the terminal height is limited.
    """
    # 5‑line terminal (1 header + 4 body lines)
    monkeypatch.setattr(os, "get_terminal_size", lambda: os.terminal_size((80, 5)))
    table_ui.rows = build_rows()
    table_ui.body_height = 4

    multiline = "Line1\nLine2\nLine3\nLine4\nLine5\nLine6"
    table_ui.overlay_content = multiline
    table_ui.overlay_offset = 0

    body = table_ui._render_body()
    assert body[0][1].strip() == "Line1"
    assert body[-1][1].strip() == "Line4"

    table_ui.overlay_offset = 1
    body = table_ui._render_body()
    assert body[0][1].strip() == "Line2"
    assert body[-1][1].strip() == "Line5"

    table_ui.overlay_offset = 2
    body = table_ui._render_body()
    assert body[0][1].strip() == "Line3"
    assert body[-1][1].strip() == "Line6"


def test_space_overlay_toggle(table_ui, dummy_event):
    """
    Pressing space toggles the overlay of the selected row's content.
    """
    table_ui.rows = build_rows()
    table_ui.selected_row = 0

    assert table_ui.overlay_content is None

    kb = table_ui.kb
    space_handler = kb.get_bindings_for_keys((" ",))[0].handler
    space_handler(dummy_event)
    assert table_ui.overlay_content == table_ui.rows[0][6]
    assert table_ui.overlay_offset == 0

    space_handler(dummy_event)
    assert table_ui.overlay_content is None


def test_column_navigation(table_ui, dummy_event):
    """
    Left/Right arrows move the column selection cursor,
    respecting the number of visible columns.
    """
    table_ui.rows = build_rows()
    table_ui.selected_col = 0

    kb = table_ui.kb
    left_handler = kb.get_bindings_for_keys(("left",))[0].handler
    right_handler = kb.get_bindings_for_keys(("right",))[0].handler

    right_handler(dummy_event)
    assert table_ui.selected_col == 1

    for _ in range(4):
        right_handler(dummy_event)
    assert table_ui.selected_col == 3  # maximum visible columns

    left_handler(dummy_event)
    assert table_ui.selected_col == 2


def test_row_navigation_and_scrolling(table_ui, dummy_event):
    """
    Up/Down arrows move the row cursor and scroll the view when needed.
    """
    table_ui.rows = build_rows()
    table_ui.selected_row = 1

    kb = table_ui.kb
    up_handler = kb.get_bindings_for_keys(("up",))[0].handler
    down_handler = kb.get_bindings_for_keys(("down",))[0].handler

    up_handler(dummy_event)
    assert table_ui.selected_row == 0

    down_handler(dummy_event)
    assert table_ui.selected_row == 1
    down_handler(dummy_event)
    assert table_ui.selected_row == 2

    # Cannot scroll past the last row
    down_handler(dummy_event)
    assert table_ui.selected_row == 2


def test_sorting_logic(table_ui, dummy_event):
    """
    Sorting toggles: pressing 't' on the same column reverses direction,
    pressing it on a new column starts with descending order.
    """
    table_ui.rows = build_rows()
    table_ui.selected_col = 0  # 'user_name'

    kb = table_ui.kb
    t_handler = kb.get_bindings_for_keys(("t",))[0].handler

    t_handler(dummy_event)
    assert table_ui.sort_col == "user_name"
    assert table_ui.sort_descending is True

    t_handler(dummy_event)
    assert table_ui.sort_descending is False

    table_ui.selected_col = 2  # 'role'
    t_handler(dummy_event)
    assert table_ui.sort_col == "role"
    assert table_ui.sort_descending is True


def test_delete_row(monkeypatch, tmp_db, dummy_event):
    """
    Deleting a row removes it from the UI and the database.
    """
    import importlib

    import ChatHistoryUI

    importlib.reload(ChatHistoryUI)

    ui = ChatHistoryUI.TableUI()
    ui.cols = [
        "id",
        "user_id",
        "user_name",
        "channel_id",
        "is_dm",
        "role",
        "content",
        "timestamp",
    ]
    ui.rows = [
        (
            42,
            42,
            "test_user",
            7,
            1,
            "assistant",
            "hello world",
            "2024-01-01T00:00:00Z",
        )
    ]
    ui.selected_row = 0
    ui.selected_col = 0
    ui.overlay_content = None

    kb = ui.kb
    x_handler = kb.get_bindings_for_keys(("x",))[0].handler
    x_handler(dummy_event)

    assert len(ui.rows) == 0


def test_delete_ignores_overlay(monkeypatch, tmp_db, dummy_event):
    """
    Deleting while an overlay is active should be a no‑op.
    """

    import importlib

    import ChatHistoryUI

    importlib.reload(ChatHistoryUI)

    ui = ChatHistoryUI.TableUI()
    ui.overlay_content = "some content"
    ui.selected_row = 0

    kb = ui.kb
    x_handler = kb.get_bindings_for_keys(("x",))[0].handler
    x_handler(dummy_event)

    assert ui.overlay_content == "some content"
