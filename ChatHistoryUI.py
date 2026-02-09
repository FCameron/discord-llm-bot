#!/usr/bin/env python3
"""
ChatHistoryUI – a lightweight, terminal‑based viewer for the LlamaGPT chat history.

The UI shows a tabular view of the most recent messages stored in the same
SQLite database that LlamaGPT uses.  It supports:

* **Space** – toggle an overlay that shows the full content of the selected row.
  Scrolling is performed with the arrow keys while the overlay is active.
* **Arrow keys** – move the selection.  While overlayed the arrows scroll the
  text instead of changing the row/column selection.
* **t** – sort by the currently selected column; repeated presses toggle
  ascending/descending, pressing *t* on a different column starts with
  descending order.
* **x** – delete the selected row.
* **q** or **Ctrl‑C** – quit the application.

Only the four columns ``user_name``, ``is_dm``, ``role`` and ``timestamp`` are
displayed in the table – the rest of the row is shown in the overlay.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import textwrap
from collections.abc import Iterable

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.styles import Style

# --------------------------------------------------------------------------- #
# Configuration – path to the chat‑history database
# --------------------------------------------------------------------------- #
DB_PATH = os.getenv("CHAT_HISTORY_DB", os.path.expanduser("chat_history.db"))
if not os.path.exists(DB_PATH):
    raise FileNotFoundError(f"No database at {DB_PATH} – is the bot running?")

# Keep a reference to the original sqlite3.connect() before any monkey‑patching
_ORIG_SQLITE_CONNECT = sqlite3.connect


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _query(
    order_by: str | None = None, descending: bool = True
) -> tuple[list[str], list[tuple]]:
    """
    Execute ``SELECT * FROM messages`` on the database, optionally sorted.

    Parameters
    ----------
    order_by:
        The column name to sort by.  If ``None`` the column ``timestamp`` is used.
    descending:
        Whether the order should be descending (``True``) or ascending
        (``False``).

    Returns
    -------
    tuple
        ``(column_names, rows)`` where *column_names* is a list of strings
        and *rows* is a list of tuples, each tuple containing a database row.
    """
    column = order_by or "timestamp"
    order_clause = f" ORDER BY {column} {'DESC' if descending else 'ASC'}"

    with _ORIG_SQLITE_CONNECT(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM messages{order_clause} LIMIT 100")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return cols, rows


async def _periodic_refresh(ui: "TableUI") -> None:
    """
    Periodically reload the database every second to keep the UI up‑to‑date.
    """
    while True:
        await asyncio.sleep(1)
        cols, rows = _query(order_by=ui.sort_col, descending=ui.sort_descending)
        if rows != ui.rows or cols != ui.cols:
            ui.cols, ui.rows = cols, rows
            ui.app.invalidate()


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
class TableUI:
    """
    Main application class – a thin wrapper around a prompt_toolkit ``Application``.
    """

    # The columns that are shown in the table; everything else is displayed
    # only in the overlay.
    _VISIBLE_COLS = ("user_name", "is_dm", "role", "timestamp")

    def __init__(self) -> None:
        # ------------------------------------------------------------------ #
        # State – sorting
        # ------------------------------------------------------------------ #
        self.sort_col: str | None = None  # column currently sorted on
        self.last_sort_col: str | None = None  # last sorted column
        self.sort_descending: bool = True  # sort direction

        # ------------------------------------------------------------------ #
        # Initial data load
        # ------------------------------------------------------------------ #
        self.cols, self.rows = _query(
            order_by=self.sort_col, descending=self.sort_descending
        )

        # ------------------------------------------------------------------ #
        # Selection state
        # ------------------------------------------------------------------ #
        self.selected_row = 0
        self.selected_col = 0

        # ------------------------------------------------------------------ #
        # Overlay state – ``None`` means no overlay
        # ------------------------------------------------------------------ #
        self.overlay_content: str | None = None
        self.overlay_offset: int = 0  # line offset for scrolling

        # ------------------------------------------------------------------ #
        # Layout helpers
        # ------------------------------------------------------------------ #
        self._refresh_dimensions()

        self.header_control = FormattedTextControl(text=self._render_header)
        self.body_control = FormattedTextControl(text=self._render_body)

        self.header_win = Window(
            content=self.header_control, height=1, style="reverse bold"
        )
        self.body_win = Window(content=self.body_control, always_hide_cursor=True)

        # ------------------------------------------------------------------ #
        # Key bindings
        # ------------------------------------------------------------------ #
        self.kb = KeyBindings()
        self._bind_keys()

        # ------------------------------------------------------------------ #
        # The prompt_toolkit application
        # ------------------------------------------------------------------ #
        self.app = Application(
            layout=Layout(HSplit([self.header_win, self.body_win])),
            key_bindings=self.kb,
            full_screen=True,
            mouse_support=False,
        )

    # ----------------------------------------------------------------------- #
    # Terminal size helpers
    # ----------------------------------------------------------------------- #
    def _refresh_dimensions(self) -> None:
        """Cache the current terminal height and width."""
        try:
            size = os.get_terminal_size()
            self.body_height = size.lines - 1  # one line is used by the header
            self.body_width = size.columns
        except OSError:
            # Non‑interactive fallback (e.g. when running tests)
            self.body_height = 23
            self.body_width = 80

    # ----------------------------------------------------------------------- #
    # Rendering
    # ----------------------------------------------------------------------- #
    def _render_header(self) -> Iterable[tuple[str, str]]:
        """Return a sequence of ``(style, text)`` tuples for the header row."""
        parts: list[tuple[str, str]] = []

        # ``self._VISIBLE_COLS`` already gives the order we want to display.
        for idx, col in enumerate(self._VISIBLE_COLS):
            # Highlight the column header that is currently selected.
            style = "" if idx == self.selected_col else "reverse bold"
            parts.append((style, f" {col} "))

        return parts

    def _render_body(self) -> Iterable[tuple[str, str]]:
        """Return a sequence of ``(style, text)`` tuples for the body."""
        self._refresh_dimensions()

        # ------------------------------------------------------------------ #
        # Overlay mode – show the full message content
        # ------------------------------------------------------------------ #
        if self.overlay_content is not None:
            raw_lines = self.overlay_content.splitlines()
            wrapped = [
                line
                for raw in raw_lines
                for line in textwrap.wrap(raw, width=self.body_width)
            ]

            start = self.overlay_offset
            end = min(start + self.body_height, len(wrapped))
            return [("", line + "\n") for line in wrapped[start:end]]

        # ------------------------------------------------------------------ #
        # Table mode – show the visible columns for each row
        # ------------------------------------------------------------------ #
        visible_idx = [self.cols.index(c) for c in self._VISIBLE_COLS]
        result: list[tuple[str, str]] = []

        for r_idx, row in enumerate(self.rows):
            for c_idx, col_idx in enumerate(visible_idx):
                val = row[col_idx]
                style = (
                    "reverse"
                    if r_idx == self.selected_row and c_idx == self.selected_col
                    else ""
                )
                # Truncate long values to keep the table tidy
                display = f" {str(val)[:30]:30} "
                result.append((style, display))
            result.append(("", "\n"))

        return result

    # ----------------------------------------------------------------------- #
    # Key bindings
    # ----------------------------------------------------------------------- #
    def _bind_keys(self) -> None:
        @self.kb.add("left")
        def _move_left(event) -> None:
            if self.overlay_content is None:
                self.selected_col = max(0, self.selected_col - 1)
                event.app.invalidate()

        @self.kb.add("right")
        def _move_right(event) -> None:
            if self.overlay_content is None:
                self.selected_col = min(
                    len(self._VISIBLE_COLS) - 1, self.selected_col + 1
                )
                event.app.invalidate()

        @self.kb.add("up")
        def _move_up(event) -> None:
            if self.overlay_content is None:
                self.selected_row = max(0, self.selected_row - 1)
            else:
                self.overlay_offset = max(0, self.overlay_offset - 1)
            event.app.invalidate()

        @self.kb.add("down")
        def _move_down(event) -> None:
            if self.overlay_content is None:
                self.selected_row = min(len(self.rows) - 1, self.selected_row + 1)
            else:
                # Compute the maximum offset for scrolling
                raw_lines = self.overlay_content.splitlines()
                wrapped = [
                    line
                    for raw in raw_lines
                    for line in textwrap.wrap(raw, width=self.body_width)
                ]
                max_offset = max(0, len(wrapped) - self.body_height)
                self.overlay_offset = min(max_offset, self.overlay_offset + 1)
            event.app.invalidate()

        @self.kb.add("space")
        @self.kb.add(" ")
        def _toggle_overlay(event) -> None:
            if self.overlay_content is None:
                # Column index 6 holds the message content in the database schema
                self.overlay_content = str(self.rows[self.selected_row][6])
                self.overlay_offset = 0
            else:
                self.overlay_content = None
            event.app.invalidate()

        @self.kb.add("x")
        def _delete_row(event) -> None:
            if self.overlay_content is not None:
                return

            # Identify the row to delete by its primary key.
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
                result = cur.fetchone()
                if result:
                    conn.execute("DELETE FROM messages WHERE id=?", (result[0],))
                    conn.commit()

            # Remove the row from the in‑memory list
            del self.rows[self.selected_row]
            if self.selected_row >= len(self.rows):
                self.selected_row = max(0, len(self.rows) - 1)
            event.app.invalidate()

        @self.kb.add("t")
        def _sort_column(event) -> None:
            col_name = self._VISIBLE_COLS[self.selected_col]
            self.sort_col = col_name

            if self.last_sort_col == col_name:
                self.sort_descending = not self.sort_descending
            else:
                self.sort_descending = True
            self.last_sort_col = col_name

            self.cols, self.rows = _query(
                order_by=col_name, descending=self.sort_descending
            )
            self.selected_row = 0
            event.app.invalidate()

        @self.kb.add("q")
        @self.kb.add("c-c")
        def _quit(event) -> None:
            event.app.exit()

    # ----------------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------------- #
    def run(self) -> None:
        """Start the UI."""

        async def _start() -> None:
            self.app.create_background_task(_periodic_refresh(self))
            await self.app.run_async()

        asyncio.run(_start())


# --------------------------------------------------------------------------- #
# Entrypoint – run the UI when the module is executed directly
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    ui = TableUI()
    ui.run()


# --------------------------------------------------------------------------- #
# Expose the module under a global name so that tests can import it easily
# --------------------------------------------------------------------------- #
import builtins
import sys

builtins.ChatHistoryUI = sys.modules[__name__]
