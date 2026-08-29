from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from aqt import mw

from ..core.metrics import StudySession


_SCHEMA_VERSION = 2  # bump when adding columns or tables


class SessionRepo:
    def __init__(self) -> None:
        self._db_path: Path | None = None

    @property
    def db_path(self) -> Path:
        if self._db_path is None:
            profile = Path(mw.pm.profileFolder())
            self._db_path = profile / "focusflow" / "focusflow_sessions.sqlite"
        return self._db_path

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Idempotent, forward-only schema migration via PRAGMA user_version.

        v1 — initial sessions table
        v2 — focusflow_goal_dates: one row per calendar day a goal was reached
        """
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if ver < 1:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS focusflow_sessions (
                    id              INTEGER PRIMARY KEY,
                    start_time      INTEGER NOT NULL,
                    end_time        INTEGER NOT NULL,
                    duration        INTEGER NOT NULL,
                    cards_done      INTEGER NOT NULL,
                    effective_score REAL    NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ff_start "
                "ON focusflow_sessions(start_time)"
            )
            conn.execute("PRAGMA user_version = 1")
        if ver < 2:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS focusflow_goal_dates (
                    day TEXT PRIMARY KEY   -- ISO-8601: YYYY-MM-DD
                )
                """
            )
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def insert(self, session: StudySession) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO focusflow_sessions
                    (start_time, end_time, duration, cards_done, effective_score)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session.start_time,
                    session.end_time,
                    session.duration,
                    session.cards_done,
                    session.effective_score,
                ),
            )

    def sessions_between(self, start_ts: int, end_ts: int) -> list[StudySession]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT start_time, end_time, duration, cards_done, effective_score
                FROM focusflow_sessions
                WHERE start_time >= ? AND start_time < ?
                ORDER BY start_time
                """,
                (start_ts, end_ts),
            ).fetchall()
        return [StudySession(*r) for r in rows]

    def all_sessions(self) -> list[StudySession]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT start_time, end_time, duration, cards_done, effective_score
                FROM focusflow_sessions
                ORDER BY start_time
                """
            ).fetchall()
        return [StudySession(*r) for r in rows]

    def count_since(self, start_ts: int) -> int:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM focusflow_sessions WHERE start_time >= ?",
                (start_ts,),
            ).fetchone()
        return int(row[0] if row else 0)

    def distinct_years(self) -> set[int]:
        """Return the set of calendar years that have at least one session.

        Uses a single indexed scan of start_time rather than loading every row.
        """
        self.ensure_schema()
        with self._connect() as conn:
            # BUG FIX: was missing the 'localtime' modifier, so this bucketed
            # by UTC calendar year while every other day/year grouping in the
            # addon (heatmap_service.py's datetime.fromtimestamp(...).date())
            # uses local time. For anyone not at UTC+0, a session close to a
            # local New Year's could land in a different year here than in
            # the day-grid itself, throwing off the earliest-navigable-year
            # calculation (_build_days_by_year in __init__.py) by one.
            rows = conn.execute(
                "SELECT DISTINCT CAST(strftime('%Y', datetime(start_time, 'unixepoch', 'localtime')) AS INTEGER) "
                "FROM focusflow_sessions"
            ).fetchall()
        return {int(r[0]) for r in rows}

    def mark_goal_reached(self, day: date) -> None:
        """Record that the user met their goal on *day*.  Idempotent (INSERT OR IGNORE)."""
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO focusflow_goal_dates (day) VALUES (?)",
                (day.isoformat(),),
            )

    def goal_reached_days(self) -> set[date]:
        """Return all dates where a goal was explicitly recorded as reached."""
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute("SELECT day FROM focusflow_goal_dates").fetchall()
        return {date.fromisoformat(r[0]) for r in rows}

    @contextmanager
    def _connect(self):
        """Open a connection for the duration of a single `with` block.

        BUG FIX: this used to just `return sqlite3.connect(...)`, and every
        call site used it as `with self._connect() as conn:`. That relies on
        sqlite3.Connection's own context-manager protocol, which only commits
        (on success) or rolls back (on exception) — it does NOT close the
        connection. Every single DB operation in this class was leaking a
        connection handle, relying on Python's garbage collector to close it
        eventually. Wrapping this as a generator-based context manager keeps
        every existing `with self._connect() as conn:` call site working
        unchanged, while now guaranteeing the connection is actually closed
        (in `finally`) once that `with` block exits, in addition to still
        committing/rolling back the transaction as before.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
