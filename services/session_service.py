from __future__ import annotations

import time
from datetime import datetime

from ..core.metrics import StudySession
from ..data.session_repo import SessionRepo


class SessionService:
    def __init__(self) -> None:
        self.repo = SessionRepo()
        self._start_time: int | None = None
        self.cards_done = 0
        self.creating_seconds = 0          # time spent adding/editing cards
        self._creating_start: float | None = None

    def ensure_ready(self) -> None:
        self.repo.ensure_schema()

    @property
    def has_active_session(self) -> bool:
        return self._start_time is not None

    def start_session(self) -> None:
        if self._start_time is None:
            self._start_time = int(time.time())
            self.cards_done = 0
            self.creating_seconds = 0

    def record_card(self) -> None:
        self.start_session()
        self.cards_done += 1

    def editor_opened(self) -> None:
        self._creating_start = time.monotonic()

    def editor_closed(self) -> None:
        if self._creating_start is not None:
            self.creating_seconds += int(time.monotonic() - self._creating_start)
            self._creating_start = None

    def finish_session(
        self,
        effective_score: float,
        duration_seconds: int | None = None,
    ) -> StudySession | None:
        if self._start_time is None:
            return None
        self.editor_closed()   # close any open editor period
        end = int(time.time())
        duration = max(0, int(duration_seconds if duration_seconds is not None else end - self._start_time))
        session = StudySession(
            start_time=self._start_time,
            end_time=end,
            duration=duration,
            cards_done=self.cards_done,
            effective_score=max(0.0, min(1.0, effective_score)),
        )
        if session.cards_done > 0:   # only save if at least one card was reviewed
            self.repo.insert(session)
        self._start_time = None
        self.cards_done = 0
        return session

    def abandon_session(self) -> None:
        self._start_time = None
        self.cards_done = 0
        self.creating_seconds = 0
        self._creating_start = None

    def sessions_completed_today(self) -> int:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.repo.count_since(int(today.timestamp()))
