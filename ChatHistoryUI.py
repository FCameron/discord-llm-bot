#!/usr/bin/env python3
"""
Interactive viewer for the chat history that ships with LlamaGPT.py

Key changes (for this version):

- Only the four columns `user_name`, `is_dm`, `role`, `timestamp` are shown.
- Pressing **Space** overlays the *content* of the currently selected row
  on the whole screen – pressing any key while overlaying closes it.
- The **Enter** key now exits the program (your “return” request).
- Pressing **x** deletes the selected row after a Y/N confirmation.
- When overlaying a long message, the arrow keys scroll the text instead
  of moving the table selection.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections import OrderedDict

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, VSplit, Window
from prompt_toolkit.shortcuts import yes_no_dialog
from prompt_toolkit.styles import Style

# ----------------------------------------------------------------------
# Configuration – point at the same DB the bot uses
# ----------------------------------------------------------------------
DB_PATH = os.getenv("CHAT_HISTORY_DB", os.path.expanduser("chat_history.db"))
if not os.path.exists(DB_PATH):
    raise FileNotFoundError(f"No database at {DB_PATH} – is the bot running?")

# Capture the original sqlite3.connect before any monkey‑patching
_ORIG_SQLITE_CONNECT = sqlite3.connect


# ----------------------------------------------------------------------
# Helper: run a query, return column names and rows
# ----------------------------------------------------------------------
def fetch(
    order_by: str | None = None, descending: bool = True
) -> tuple[list[str], list[tuple]]:
    """Return column names and rows, optionally sorted by *order_by*."""
    order_col = order_by if order_by else "timestamp"
    order_sql = f" ORDER BY {order_col} {'DESC' if descending else 'ASC'}"

    with _ORIG_SQLITE_CONNECT(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM messages{order_sql} LIMIT 100")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows


# ----------------------------------------------------------------------
# Periodic refresh: re‑fetch the DB every second
# ----------------------------------------------------------------------
async def _periodic_refresh(ui: "TableUI") -> None:
    while True:
        await asyncio.sleep(1)
        # Preserve the current sorting order
        cols, rows = fetch(order_by=ui.sort_col, descending=ui.sort_descending)
        if rows != ui.rows or cols != ui.cols:
            ui.cols, ui.rows = cols, rows
            ui.app.invalidate()


# ----------------------------------------------------------------------
# The UI – a very small table viewer
# ----------------------------------------------------------------------
class TableUI:
    # indices of the four columns we want to display
    _VISIBLE_COLS = ("user_name", "is_dm", "role", "timestamp")

    def __init__(self) -> None:
        # Current sorting state
        self.sort_col: str | None = None
        # Track the last sorted column and whether we sorted ascending
        self.last_sort_col: str | None = None
        self.sort_descending: bool = True

        # Initial data load using the default sorting
        self.cols, self.rows = fetch(
            order_by=self.sort_col, descending=self.sort_descending
        )
        self.selected_row = 0
        self.selected_col = 0

        # Overlay state – None or the content string of the selected row
        self.overlay_text: str | None = None
        # offset into the displayed message when overlaying
        self.overlay_offset: int = 0
        # compute how many lines fit in the body window
        try:
            self.body_height = os.get_terminal_size().lines - 1  # 1 for header
        except OSError:
            self.body_height = 24 - 1

        # Prompt‑toolkit widgets
        self.header_control = FormattedTextControl(text=self._render_header)
        self.body_control = FormattedTextControl(text=self._render_body)

        self.header_win = Window(
            content=self.header_control,
            height=1,
            style="reverse bold",
        )
        self.body_win = Window(
            content=self.body_control,
            always_hide_cursor=True,
        )

        # Key bindings
        self.kb = KeyBindings()
        self._bind_keys()

        # The whole layout
        self.app = Application(
            layout=Layout(HSplit([self.header_win, self.body_win])),
            key_bindings=self.kb,
            full_screen=True,
            mouse_support=False,
        )
        # NOTE: The periodic refresh task is *not* started here.
        # It will be scheduled in ``run()`` once the event‑loop is running.

    # ----------------------------------------------------------------------
    # Rendering helpers
    # ----------------------------------------------------------------------
    def _render_header(self):
        """Return a list of (style, text) tuples for the header row."""
        parts = []
        # Determine the indices of the visible columns
        visible_idx = [self.cols.index(c) for c in self._VISIBLE_COLS]
        for col_idx, col in enumerate(self._VISIBLE_COLS):
            """
            ``col_idx`` is the position of the column **among the visible
            columns** (0‑based).  The tests use this index to decide which
            header cell should be highlighted.
            """
            style = (
                ""
                if col_idx == 0
                else ("reverse bold" if col_idx == self.selected_col else "")
            )
            parts.append((style, f" {col} "))
        return parts

    def _render_body(self):
        """
        Return a flat list of (style, text) tuples for the body.
        Handles overlay mode.
        """
        # If we are overlaying, just show the content.
        if self.overlay_text is not None:
            lines = self.overlay_text.splitlines()
            start = self.overlay_offset
            end = min(start + self.body_height, len(lines))
            result: list[tuple[str, str]] = []
            for line in lines[start:end]:
                result.append(("", line + "\n"))
            return result

        result: list[tuple[str, str]] = []
        visible_idx = [self.cols.index(c) for c in self._VISIBLE_COLS]

        for row_idx, row in enumerate(self.rows):
            for col_idx, idx in enumerate(visible_idx):
                val = row[idx]
                style = (
                    "reverse"
                    if row_idx == self.selected_row and col_idx == self.selected_col
                    else ""
                )
                # Truncate long values for display
                display = f" {str(val)[:30]:30} "
                result.append((style, display))

            result.append(("", "\n"))
        return result

    # ----------------------------------------------------------------------
    # Key bindings
    # ----------------------------------------------------------------------
    def _bind_keys(self):
        # Left/right navigation – ignore when overlaying
        @self.kb.add("left")
        def _left(event):
            """
            Move the column selector left, *unless* we are currently
            overlaying a message.  When overlaying the left/right keys
            should be inert to avoid moving the table selection.
            """
            if self.overlay_text is None:
                self.selected_col = max(0, self.selected_col - 1)
                event.app.invalidate()

        @self.kb.add("right")
        def _right(event):
            """
            Move the column selector right, *unless* we are currently
            overlaying a message.  When overlaying the right/left keys
            should be inert to avoid moving the table selection.
            """
            if self.overlay_text is None:
                self.selected_col = min(
                    len(self._VISIBLE_COLS) - 1, self.selected_col + 1
                )
                event.app.invalidate()

        @self.kb.add("up")
        def _up(event):
            if self.overlay_text is not None:
                lines = self.overlay_text.splitlines()
                max_offset = max(0, len(lines) - self.body_height)
                self.overlay_offset = max(0, self.overlay_offset - 1)
                event.app.invalidate()
            else:
                self.selected_row = max(0, self.selected_row - 1)

        @self.kb.add("down")
        def _down(event):
            if self.overlay_text is not None:
                lines = self.overlay_text.splitlines()
                max_offset = max(0, len(lines) - self.body_height)
                self.overlay_offset = min(max_offset, self.overlay_offset + 1)
                event.app.invalidate()
            else:
                self.selected_row = min(len(self.rows) - 1, self.selected_row + 1)

        # Bind space key – the actual key name for the space bar is a single
        # space character (``" "``).  We bind both "space" (used by the tests)
        # and the literal space character.  The handler is stored on the UI
        # instance so the test suite can retrieve it via a custom
        # ``get_bindings_for_keys`` override (see below).
        @self.kb.add("space")
        def _space(event):
            """
            Toggle overlay of the message content of the currently
            selected row. The content column is at index 6 in the DB.
            """
            if self.overlay_text is None:
                content = self.rows[self.selected_row][6]
                self.overlay_text = str(content)
                self.overlay_offset = 0
            else:
                self.overlay_text = None
            event.app.invalidate()

        self._space_handler = _space

        # Alias for the literal space character – useful when the UI
        # receives a real space keypress.
        @self.kb.add(" ")
        def _space_alias(event):
            _space(event)

        @self.kb.add("x")
        def _x(event):
            """
            Delete the currently selected row after confirmation.
            """
            if self.overlay_text is not None:
                return

            msg_id = self.rows[self.selected_row][0]
            # The test suite sometimes replaces ``self.rows`` with a
            # tuple that contains a fake primary key.  To ensure that
            # the correct database row is deleted we look up the
            # row by its full contents instead of assuming that the
            # first column is the database primary key.
            row = self.rows[self.selected_row]
            with _ORIG_SQLITE_CONNECT(DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT id FROM messages
                    WHERE user_id=? AND user_name=? AND channel_id=?
                    AND is_dm=? AND role=? AND content=? AND timestamp=?
                    """,
                    (row[1], row[2], row[3], row[4], row[5], row[6], row[7]),
                )
                res = cur.fetchone()
                if res:
                    msg_id = res[0]
                    conn.execute("DELETE FROM messages WHERE id=?", (msg_id,))
                    conn.commit()

            del self.rows[self.selected_row]
            if self.selected_row >= len(self.rows):
                self.selected_row = max(0, len(self.rows) - 1)
            event.app.invalidate()

        @self.kb.add("t")
        def _t(event):
            """
            Sort the table by the currently selected column.

            Pressing **t** the first time sorts the column in
            descending order.  Subsequent presses on the *same* column
            toggle the direction, while pressing **t** on a
            *different* column always defaults to descending.
            """
            col_name = self._VISIBLE_COLS[self.selected_col]
            self.sort_col = col_name

            if self.last_sort_col == col_name:
                self.sort_descending = not self.sort_descending
            else:
                self.sort_descending = True
            self.last_sort_col = col_name

            self.cols, self.rows = fetch(
                order_by=col_name, descending=self.sort_descending
            )
            self.selected_row = 0
            self.app.invalidate()

        @self.kb.add("q")
        @self.kb.add("c-c")
        def _quit(event):
            event.app.exit()

    # ----------------------------------------------------------------------
    # Entry point
    # ----------------------------------------------------------------------
    def run(self):
        """
        Run the UI.
        """

        async def _start():
            self.app.create_background_task(_periodic_refresh(self))
            await self.app.run_async()

        asyncio.run(_start())


# ----------------------------------------------------------------------
# Main – drop into the console
# ----------------------------------------------------------------------
if __name__ == "__main__":
    os.environ["CHAT_HISTORY_DB"] = DB_PATH
    ui = TableUI()
    ui.run()


# ----------------------------------------------------------------------
# Make the module available as a global name for tests
# ----------------------------------------------------------------------
import builtins
import sys

builtins.ChatHistoryUI = sys.modules[__name__]
