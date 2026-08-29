from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from aqt.qt import QObject, QTimer, pyqtSignal


class TimerMode(str, Enum):
    IDLE = "idle"
    STUDY = "study"
    BREAK = "break"


@dataclass(frozen=True)
class TimerSnapshot:
    mode: TimerMode
    is_running: bool
    is_paused: bool
    has_started: bool
    elapsed_seconds: int
    total_seconds: int
    progress: float
    remaining_seconds: int


class TimerManager(QObject):
    tick = pyqtSignal(object)
    state_changed = pyqtSignal(object)
    session_completed = pyqtSignal(object)
    break_completed = pyqtSignal(object)
    # Dedicated pause/resume signals so the fatigue tracker can exclude
    # idle wall-clock time from response-time measurements.
    paused  = pyqtSignal()   # emitted when the timer transitions to paused
    resumed = pyqtSignal()   # emitted when the timer transitions out of pause

    def __init__(self, config: dict) -> None:
        super().__init__()
        self._config = config
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._emit_tick)

        self.mode = TimerMode.IDLE
        self._total_seconds = 1
        self._run_started_at: float | None = None
        self._pause_reasons: set[str] = set()
        self._completed_emitted = False
        # _fatigue_multiplier is applied only at the NEXT start_study() call, not
        # mid-session.  This is intentional: shortening an in-progress Pomodoro
        # would be jarring.  Changing adaptive_duration in Settings takes effect
        # at the start of the following session.
        self._fatigue_multiplier: float = 1.0
        # Anchor-based elapsed tracking.  Instead of accumulating increments
        # across every pause/resume cycle (which can drift), we record the
        # session start time and the total time spent paused, then compute:
        #   elapsed = wall_time_since_start − total_paused_time
        self._session_start_mono: float = 0.0   # monotonic time at _start()
        self._total_paused_secs: float = 0.0    # sum of all completed pause durations
        self._pause_started_at: float | None = None  # monotonic when current pause began

    @property
    def is_running(self) -> bool:
        return self._run_started_at is not None and not self._pause_reasons

    @property
    def is_paused(self) -> bool:
        return self.has_started and bool(self._pause_reasons)

    @property
    def has_started(self) -> bool:
        return self.mode != TimerMode.IDLE

    def update_config(self, config: dict) -> None:
        self._config = config
        if self.mode == TimerMode.STUDY:
            self._total_seconds = self._study_seconds()
        self._emit_state()

    def start_study(self) -> None:
        self._start(TimerMode.STUDY, self._study_seconds())

    def start_break(self, duration_seconds: int | None = None) -> None:
        total = duration_seconds if duration_seconds is not None else self._break_seconds()
        self._start(TimerMode.BREAK, total)

    def stop_idle(self) -> None:
        self._timer.stop()
        self.mode = TimerMode.IDLE
        self._total_seconds = 0
        self._run_started_at = None
        self._pause_reasons.clear()
        self._completed_emitted = False
        self._emit_state()

    def pause(self, reason: str = "manual") -> None:
        if not self.has_started or reason in self._pause_reasons:
            return
        if self._run_started_at is not None:
            _now = time.monotonic()
            # Anchor tracking: record when this pause started so we can
            # accumulate total paused time in resume() without drift.
            self._pause_started_at = _now
            self._run_started_at = None
        self._pause_reasons.add(reason)
        self._timer.stop()
        self._emit_state()
        self.paused.emit()

    def resume(self, reason: str = "manual") -> None:
        if not self.has_started:
            return
        self._pause_reasons.discard(reason)
        if self._pause_reasons:
            self._emit_state()
            return
        self._resume_running()

    def resume_all(self) -> None:
        """Clear every active pause reason and resume running.

        Used for explicit, user-initiated resume actions (e.g. the toolbar's
        Play/Resume button) where the user's intent is "start the timer
        running again" regardless of *why* it was paused. A plain resume()
        only clears one specific reason, so it silently does nothing when the
        timer is paused for a different reason (e.g. "editor" or
        "not_reviewing") — see toggle_manual_pause() below.
        """
        if not self.has_started or not self._pause_reasons:
            return
        self._pause_reasons.clear()
        self._resume_running()

    def _resume_running(self) -> None:
        """Shared tail of resume()/resume_all() once all reasons are clear."""
        if self._run_started_at is None:
            _now = time.monotonic()
            # Accumulate the just-completed pause duration so _elapsed_seconds()
            # can subtract total paused time from wall time for a drift-free result.
            if self._pause_started_at is not None:
                self._total_paused_secs += _now - self._pause_started_at
                self._pause_started_at = None
            self._run_started_at = _now
        self._timer.start()
        self._emit_state()
        self.resumed.emit()

    def toggle_manual_pause(self) -> None:
        """Toolbar Play/Pause button handler.

        BUG FIX: this used to check only for "manual" in _pause_reasons and
        call resume("manual") / pause("manual") accordingly. That meant that
        whenever the timer was paused for any OTHER reason (e.g. "editor"
        when auto_resume_after_editor is off, or "not_reviewing" if clicked
        from the deck browser), clicking the button — which correctly showed
        "Resume" because is_paused was True — would call pause("manual"),
        stacking a second pause reason on top instead of clearing the
        existing one. The timer then stayed paused with no working way to
        resume it from the UI. Now: if paused for ANY reason, resume_all()
        clears every reason so the button always does what it visibly claims
        to do; only pause when nothing is currently paused.
        """
        if self._pause_reasons:
            self.resume_all()
        else:
            self.pause("manual")

    def reset_to_start(self) -> None:
        """Reset elapsed time to zero without ending or changing the current session.

        The mode, total duration, and running/paused state are all preserved —
        only the anchor timestamps are rewound so the next snapshot shows
        elapsed=0 and remaining=total_seconds again.

        Called when the user holds the timer label (≥700 ms).
        """
        if self.mode == TimerMode.IDLE:
            return
        _now = time.monotonic()
        # Reset anchor: treat this instant as the new session start.
        self._session_start_mono = _now
        self._total_paused_secs  = 0.0
        # If currently paused, restart the pause window from now so that
        # _elapsed_seconds() accounts for the full pause from the new anchor.
        if self._pause_started_at is not None:
            self._pause_started_at = _now
        # Allow the completion signal to fire again when this new cycle ends.
        self._completed_emitted = False
        self._emit_state()

    def snapshot(self) -> TimerSnapshot:
        if self.mode == TimerMode.IDLE:
            return TimerSnapshot(
                mode=TimerMode.IDLE,
                is_running=False,
                is_paused=False,
                has_started=False,
                elapsed_seconds=0,
                total_seconds=0,
                progress=0.0,
                remaining_seconds=0,
            )
        elapsed = self._elapsed_seconds()
        total = max(1, self._total_seconds)
        progress = min(1.0, max(0.0, elapsed / total))
        return TimerSnapshot(
            mode=self.mode,
            is_running=self.is_running,
            is_paused=self.is_paused,
            has_started=self.has_started,
            elapsed_seconds=elapsed,
            total_seconds=total,
            progress=progress,
            remaining_seconds=max(0, total - elapsed),
        )

    # ── private ───────────────────────────────────────────────────────────────

    def _start(self, mode: TimerMode, total_seconds: int) -> None:
        self._timer.stop()
        self.mode = mode
        self._total_seconds = max(1, int(total_seconds))
        _now = time.monotonic()
        self._run_started_at = _now
        # Anchor reset: record session start and clear all pause accounting.
        self._session_start_mono = _now
        self._total_paused_secs = 0.0
        self._pause_started_at = None
        self._pause_reasons.clear()
        self._completed_emitted = False
        self._timer.start()
        self._emit_state()

    def _elapsed_seconds(self) -> int:
        """Return total active seconds since _start(), excluding pause time.

        Uses the session-start anchor (_session_start_mono) minus total
        accumulated pause time (_total_paused_secs) rather than summing
        increments across every pause/resume cycle.  This eliminates any
        theoretical floating-point accumulation drift in long sessions with
        many pause/resume cycles.
        """
        _now = time.monotonic()
        # Wall time since session start
        wall = _now - self._session_start_mono
        # Subtract all completed pause durations
        paused = self._total_paused_secs
        # If currently paused, also subtract the ongoing pause duration
        if self._pause_started_at is not None and self._run_started_at is None:
            paused += _now - self._pause_started_at
        return max(0, int(wall - paused))

    def set_fatigue_multiplier(self, multiplier: float) -> None:
        """Adjust the *next* study session's length based on current fatigue.

        Applied on the next start_study() call; does not shorten an in-progress
        session.  Clamped to [0.60, 1.20] to prevent extreme durations.
        """
        self._fatigue_multiplier = max(0.60, min(1.20, float(multiplier)))

    def _study_seconds(self) -> int:
        timer   = self._config.get("timer", {})
        minutes = timer.get("study_minutes", timer.get("focus_minutes", 25))
        base    = max(1, int(float(minutes) * 60))
        return max(60, int(base * self._fatigue_multiplier))

    def _break_seconds(self) -> int:
        timer = self._config.get("timer", {})
        return max(1, int(float(timer.get("break_minutes", 5)) * 60))

    def _emit_tick(self) -> None:
        snapshot = self.snapshot()
        self.tick.emit(snapshot)
        if snapshot.progress < 1.0 or self._completed_emitted:
            return
        self._completed_emitted = True
        self._timer.stop()
        self._run_started_at = None
        if snapshot.mode == TimerMode.BREAK:
            self.break_completed.emit(snapshot)
        elif snapshot.mode == TimerMode.STUDY:
            self.session_completed.emit(snapshot)

    def _emit_state(self) -> None:
        self.state_changed.emit(self.snapshot())
