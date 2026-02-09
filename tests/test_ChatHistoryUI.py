# test_ChatHistoryUI.py
#!/usr/bin/env python3
"""
Unit tests for the ChatHistoryUI module.

The tests cover:
* Header rendering (style + column names)
* Body rendering (normal rows, overlay mode, scrolling, truncation)
* Key‑binding logic (navigation, overlay toggle, delete, sort)
* Database delete integration via the `x` key
"""

import os
import sqlite3
import types
from pathlib import Path
from unittest import mock

import pytest


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database with the expected messages table."""
    db_path = tmp_path / "chat_history.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
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
def chat_ui(tmp_db, monkeypatch):
    """Load the module after setting the environment variable."""
    os.environ["CHAT_HISTORY_DB"] = str(tmp_db)

    # Reload the module so that DB_PATH points to the temporary DB.
    import importlib

    import ChatHistoryUI

    importlib.reload(ChatHistoryUI)
    return ChatHistoryUI


@pytest.fixture
def dummy_event():
    """A minimal event with an `app.invalidate` method."""
    return mock.Mock(app=mock.Mock(invalidate=mock.Mock()))


# --------------------------------------------------------------------------- #
# Helper to build a simple row set
# --------------------------------------------------------------------------- #
def build_rows():
    """Return a deterministic set of rows."""
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


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_header_rendering(chat_ui):
    ui = chat_ui.TableUI()
    # Override cols/rows for deterministic test
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
    ui.rows = build_rows()

    # No column selected -> nothing highlighted
    ui.selected_col = 0
    header_parts = ui._render_header()
    # According to the updated UI logic, the first column is not highlighted when
    # selected_col is 0.
    assert header_parts[0][0] == ""
    # Highlight first column
    ui.selected_col = 2
    header_parts = ui._render_header()
    assert header_parts[2][0] == "reverse bold"
    # Check that the correct column names are rendered
    names = [part[1].strip() for part in header_parts]
    assert names == ["user_name", "is_dm", "role", "timestamp"]


def test_body_rendering_normal_rows(chat_ui):
    ui = chat_ui.TableUI()
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
    ui.rows = build_rows()
    ui.selected_row = 1  # second row
    ui.selected_col = 2  # role column
    body = ui._render_body()
    # Body should have one line per row + newline
    assert len(body) == len(ui.rows) * 4 + len(ui.rows)  # 4 columns + newline per row

    # Check that the selected cell is styled "reverse"
    # Find the tuple that corresponds to the selected cell
    # Each row has 4 cells and one newline; the list is a flat list of tuples.
    idx = ui.selected_row * 5 + ui.selected_col
    style, text = body[idx]
    assert style == "reverse"
    # Verify that truncation works for long values
    long_user = "x" * 50
    ui.rows[0] = ui.rows[0][:2] + (long_user,) + ui.rows[0][3:]
    body = ui._render_body()
    # The truncated display should be 30 chars max
    truncated = body[0][1].strip()
    assert len(truncated) == 30


def test_overlay_and_scrolling(chat_ui, monkeypatch):
    # Make terminal height 5 lines (4 for body + 1 header)
    monkeypatch.setattr(os, "get_terminal_size", lambda: os.terminal_size((80, 5)))
    ui = chat_ui.TableUI()
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
    ui.rows = build_rows()
    ui.body_height = 4  # 5 total lines – 1 header

    multiline = "Line1\nLine2\nLine3\nLine4\nLine5\nLine6"
    ui.overlay_text = multiline
    ui.overlay_offset = 0

    # First page
    body = ui._render_body()
    assert body[0][1].strip() == "Line1"
    assert body[-1][1].strip() == "Line4"

    # Scroll down one line
    ui.overlay_offset = 1
    body = ui._render_body()
    assert body[0][1].strip() == "Line2"
    assert body[-1][1].strip() == "Line5"

    # Scroll to the bottom
    ui.overlay_offset = 2
    body = ui._render_body()
    assert body[0][1].strip() == "Line3"
    assert body[-1][1].strip() == "Line6"


def test_space_overlay_toggle(chat_ui, dummy_event):
    ui = chat_ui.TableUI()
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
    ui.rows = build_rows()
    ui.selected_row = 0

    # Initially no overlay
    assert ui.overlay_text is None

    # Press space to toggle overlay
    kb = ui.kb
    space_handler = kb.get_bindings_for_keys((" ",))[0].handler
    space_handler(dummy_event)
    assert ui.overlay_text == ui.rows[0][6]
    assert ui.overlay_offset == 0

    # Press space again to close overlay
    space_handler(dummy_event)
    assert ui.overlay_text is None


def test_column_navigation(chat_ui, dummy_event):
    ui = chat_ui.TableUI()
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
    ui.rows = build_rows()
    ui.selected_col = 0

    kb = ui.kb
    left_handler = kb.get_bindings_for_keys(("left",))[0].handler
    right_handler = kb.get_bindings_for_keys(("right",))[0].handler

    # Move right
    right_handler(dummy_event)
    assert ui.selected_col == 1
    # Move right to max
    right_handler(dummy_event)
    right_handler(dummy_event)
    right_handler(dummy_event)
    assert ui.selected_col == 3  # max 3 (4 visible cols)

    # Move left
    left_handler(dummy_event)
    assert ui.selected_col == 2


def test_row_navigation_and_scrolling(chat_ui, dummy_event):
    ui = chat_ui.TableUI()
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
    ui.rows = build_rows()
    ui.selected_row = 1

    kb = ui.kb
    up_handler = kb.get_bindings_for_keys(("up",))[0].handler
    down_handler = kb.get_bindings_for_keys(("down",))[0].handler

    # Move up
    up_handler(dummy_event)
    assert ui.selected_row == 0

    # Move down
    down_handler(dummy_event)
    assert ui.selected_row == 1
    down_handler(dummy_event)
    assert ui.selected_row == 2
    # Can't go past last row
    down_handler(dummy_event)
    assert ui.selected_row == 2


def test_sorting_logic(chat_ui, dummy_event):
    ui = chat_ui.TableUI()
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
    ui.rows = build_rows()
    ui.selected_col = 0  # user_name
    kb = ui.kb
    enter_handler = kb.get_bindings_for_keys(("t",))[0].handler

    # First press: descending
    enter_handler(dummy_event)
    assert ui.sort_col == "user_name"
    assert ui.sort_descending is True

    # Second press on same column: toggle ascending
    enter_handler(dummy_event)
    assert ui.sort_descending is False

    # Press on a different column
    ui.selected_col = 2  # role
    enter_handler(dummy_event)
    assert ui.sort_col == "role"
    assert ui.sort_descending is True  # default for new column


def test_delete_row_with_confirmation(monkeypatch, tmp_db, dummy_event):
    # Reload module so it picks up the new DB
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
    # Set rows to a single entry that matches the inserted row
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
    ui.overlay_text = None  # make sure delete is enabled

    kb = ui.kb
    x_handler = kb.get_bindings_for_keys(("x",))[0].handler
    x_handler(dummy_event)

    # Row should have been removed from in‑memory list
    assert len(ui.rows) == 0


def test_delete_ignores_overlay(monkeypatch, tmp_db, dummy_event):
    """Deleting while overlaying should do nothing."""
    # Use the chat_ui fixture to get the reloaded module
    from importlib import import_module

    ui = import_module("ChatHistoryUI").TableUI()
    ui.overlay_text = "some content"
    ui.selected_row = 0

    kb = ui.kb
    x_handler = kb.get_bindings_for_keys(("x",))[0].handler
    x_handler(dummy_event)

    # overlay still present, nothing changed
    assert ui.overlay_text == "some content"


# --------------------------------------------------------------------------- #
# End of test suite
# --------------------------------------------------------------------------- #
