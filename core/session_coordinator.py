"""SessionCoordinator — extracted from __init__.py (code-quality issues #13, #14).

Previously, __init__.py mixed three unrelated concerns in one flat namespace:

  ① Session / goal state tracking
      session_count, total_cards_today, total_study_seconds_today,
      goal_reported, fatigue_suggested, auto_collapsed_this_session

  ② Goal detection and DB persistence
      _check_goal_reached(), _check_and_mark_all_due_goal()

  ③ Popup lifecycle management + break orchestration
      _set_active_popup(), _close_active_popup(), _start_break(),
      and the 80-line _on_session_completed() that tangled all three.

This module owns all of the above.  __init__.py is reduced to Anki hook
registration, service wiring, and thin command stubs.

Issue #14 specifically: _on_session_completed is split into two methods with
no shared mutable state:

    _compute_report(snapshot, session, score) -> SessionReport
        Pure data computation.  Reads globals, checks goals, queries the DB,
        and returns an immutable dataclass.  No popup creation, no side effects
        other than the single `goal_reported = True` write.

    _show_popup(report: SessionReport) -> None
        Pure UI.  Reads the dataclass fields and constructs exactly one popup.
        No DB access, no goal-checking, no timer manipulation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..core.fatigue_tracker import FatigueTracker, FatigueSnapshot
    from ..core.timer_manager import TimerManager, TimerSnapshot
    from ..services.heatmap_service import HeatmapService
    from ..services.session_service import SessionService
    from ..utils.config_manager import ConfigManager
    from ..utils.sound_player import SoundPlayer
    from ..ui.ui_toolbar import FocusFlowToolbar

from .timer_manager import TimerMode as _TimerMode
from ..utils.logger import log


# ── session report dataclass (issue #14) ─────────────────────────────────────

@dataclass(frozen=True)
class SessionReport:
    """Immutable snapshot of everything needed to drive the post-session popup.

    Produced by SessionCoordinator._compute_report() and consumed by
    SessionCoordinator._show_popup() so the two steps share no mutable state.
    """
    score:           float
    session_count:   int
    is_long:         bool
    long_mins:       int
    norm_mins:       int
    eff_secs:        int
    eff_mins:        float
    cards_done:      int
    goal_hit:        Optional[str]
    today_rate:      float
    week_avg:        float
    new_today:       int
    reviews_today:   int
    relearned_today: int
    cur_streak:      int
    lng_streak:      int
    long_label:      str
    norm_label:      str
    # Raw (undeduplicated) type=0 event count for today -- see
    # raw_study_events() in heatmap_service.py. Added specifically so
    # GoalReachedPopup's Reviews figure can use the same raw definition as
    # every other Reviews display, instead of the distinct-card new_today.
    # Appended at the end: SessionReport is constructed with keywords only
    # (see _compute_report() below), so there is no positional-order risk,
    # but appending keeps the same convention used for HeatmapStats.
    new_events_today: int


# ── coordinator class ─────────────────────────────────────────────────────────

class SessionCoordinator:
    """Owns all session-level mutable state and orchestrates the
    timer → popup → break → repeat loop.

    Lifecycle
    ---------
    1. Created once in ``_init()`` with the services available at startup.
    2. ``initialize_services()`` called from ``_on_profile_open()`` to inject
       services that only exist after a profile loads.
    3. ``reset_daily_state()`` called from ``_on_profile_open()`` to restore
       today's counters from the DB so they survive Anki restarts.
    """

    def __init__(
        self,
        config_mgr: "ConfigManager",
        timer_mgr:  "TimerManager",
        fatigue:    "FatigueTracker",
        sound:      "SoundPlayer",
    ) -> None:
        self._config_mgr = config_mgr
        self._timer_mgr  = timer_mgr
        self._fatigue    = fatigue
        self._sound      = sound

        # Injected after profile opens (unavailable at addon load time)
        self._session_svc: Optional["SessionService"]   = None
        self._heatmap_svc: Optional["HeatmapService"]   = None
        self._toolbar:     Optional["FocusFlowToolbar"] = None

        # ── session state (was scattered module-level globals) ────────────────
        self.session_count:               int  = 0
        self.total_cards_today:           int  = 0
        self.total_study_seconds_today:   int  = 0
        self.goal_reported:               bool = False
        self.fatigue_suggested:           bool = False
        self.auto_collapsed_this_session: bool = False
        # UI display state — previously module-level globals in __init__.py
        self.current_mode: str  = "Studying"
        self.editor_open:  bool = False

        self._active_popup: Optional[object] = None
        self._tick_count: int = 0  # throttle update_conditions to every 2 s

        # "All due cleared today" goal has no fixed target of its own, so we
        # snapshot the due count the first time we see it each day and treat
        # that as 100% — this lets the progress bar work for that condition
        # too, not just cards/sessions/time which already have an explicit
        # numeric target.
        self._due_baseline_today: Optional[int] = None

    # ── service injection ─────────────────────────────────────────────────────

    def initialize_services(
        self,
        session_svc: "SessionService",
        heatmap_svc: "HeatmapService",
        toolbar:     "FocusFlowToolbar",
    ) -> None:
        """Called from _on_profile_open() once all services are ready."""
        self._session_svc = session_svc
        self._heatmap_svc = heatmap_svc
        self._toolbar     = toolbar

    def set_toolbar(self, toolbar: "FocusFlowToolbar") -> None:
        """Inject or replace the toolbar reference.

        Called from _on_main_window_init() so the toolbar can be registered
        before the first profile opens, without poking the private _toolbar
        attribute from outside this class.
        """
        self._toolbar = toolbar

    def reset_daily_state(
        self,
        session_count: int = 0,
        cards_today:   int = 0,
        secs_today:    int = 0,
    ) -> None:
        """Restore today's counters from the DB so they survive Anki restarts."""
        self.session_count             = session_count
        self.total_cards_today         = cards_today
        self.total_study_seconds_today = secs_today
        self.goal_reported             = False
        self.fatigue_suggested         = False
        self.auto_collapsed_this_session = False
        self._due_baseline_today       = None

    # ── popup management ──────────────────────────────────────────────────────

    @property
    def active_popup(self) -> Optional[object]:
        return self._active_popup

    def set_active_popup(self, popup: object) -> None:
        self._active_popup = popup
        try:
            popup.finished.connect(self._clear_active_popup)  # type: ignore[union-attr]
        except Exception as exc:
            log.debug("popup.finished.connect failed (popup already closed?): %s", exc)

    def _clear_active_popup(self, *_) -> None:
        self._active_popup = None

    def close_active_popup(self) -> None:
        if self._active_popup is not None:
            try:
                self._active_popup.close()  # type: ignore[union-attr]
            except Exception as exc:
                log.debug("popup.close() failed (widget already destroyed?): %s", exc)
            self._active_popup = None

    # ── timer signal handlers (issue #13) ────────────────────────────────────

    def on_tick(self, snapshot: "TimerSnapshot") -> None:
        if self._toolbar:
            self._toolbar.set_timer(snapshot)
        # Throttle update_conditions to every 10 ticks (≈ 2 s) to avoid
        # running mw.col.sched.counts() on every 200 ms timer tick.
        # Card answers and reviewer-exit events call update_conditions()
        # directly, so goal display stays accurate between ticks.
        self._tick_count += 1
        if self._tick_count % 10 == 0:
            self.update_conditions()

    def on_state_changed(self, snapshot: "TimerSnapshot") -> None:
        if self._toolbar:
            self._toolbar.set_timer(snapshot)
            if snapshot.mode == _TimerMode.IDLE:
                self._toolbar.set_idle()

    def on_session_completed(self, snapshot: "TimerSnapshot") -> None:
        """Top-level timer signal handler — delegates to pure compute + pure UI.

        The only imperative work done here is finishing the session record,
        handling the empty-session guard, and updating the running counters.
        Everything else is delegated to _compute_report / _show_popup (issue #14).
        """
        if self._sound:
            self._sound.play()

        score   = self._fatigue.current_score if self._fatigue else 1.0
        session = None
        if self._session_svc:
            session = self._session_svc.finish_session(
                effective_score=score,
                duration_seconds=snapshot.elapsed_seconds,
            )

        # Empty-session guard: nothing was answered — restart silently.
        # start_session() is called here because _on_card_shown guards it
        # behind `not _timer_mgr.has_started`, which is already True once
        # start_study() runs below.
        if session is None or session.cards_done == 0:
            if self._session_svc:
                self._session_svc.start_session()
            self._timer_mgr.stop_idle()
            self._timer_mgr.start_study()
            return

        # A new session row was just written to the DB (finish_session()
        # only inserts when cards_done > 0, which is guaranteed past this
        # point). Invalidate before _compute_report() below reads any
        # cached session/revlog data (goal_streak, today_again_rate, etc.)
        # so it reflects the session that just completed, not a stale cache.
        if self._heatmap_svc:
            self._heatmap_svc.invalidate_cache()

        self.session_count              += 1
        self.total_study_seconds_today  += int(snapshot.elapsed_seconds)
        self._timer_mgr.stop_idle()
        self.close_active_popup()

        report = self._compute_report(snapshot, session, score)
        self._show_popup(report)

    def on_break_completed(self, snapshot: "TimerSnapshot") -> None:
        from aqt import mw
        from ..ui.popups import BreakFinishedPopup

        if self._sound:
            self._sound.play()
        if self._fatigue:
            self._fatigue.on_break_taken()
            self._apply_fatigue_to_timer()
        if self._toolbar and self._fatigue:
            self._toolbar.set_fatigue(
                self._fatigue.current_state, self._fatigue.current_score
            )
        self.close_active_popup()
        popup = BreakFinishedPopup(on_continue=self.cmd_resume_after_break, parent=mw)
        self.set_active_popup(popup)
        popup.show()

    # ── report computation (issue #14 — pure data, no side effects) ──────────

    def _compute_report(
        self,
        snapshot: "TimerSnapshot",
        session:  object,
        score:    float,
    ) -> SessionReport:
        """Gather all data needed by _show_popup without touching any UI.

        The only permitted write is ``self.goal_reported = True`` which must
        happen before the DB write so a concurrent on_session_completed tick
        cannot create a duplicate goal record (issue #7).
        """
        profile   = self._config_mgr.data
        timer_cfg = profile.get("timer", {})
        sbl       = int(timer_cfg.get("sessions_before_long_break", 4))
        is_long   = (self.session_count % sbl == 0)
        long_mins = int(timer_cfg.get("long_break_minutes", 15))
        norm_mins = int(timer_cfg.get("break_minutes", 5))
        ec        = profile.get("end_conditions", {})
        eff_secs  = int(snapshot.elapsed_seconds * score)
        eff_mins  = snapshot.elapsed_seconds / 60.0 * score
        cards_done = getattr(session, "cards_done", 0)

        # Persist goal BEFORE reading streak so today counts in goal_streak().
        goal_hit = self.check_goal_reached()
        if goal_hit and not self.goal_reported:
            self.goal_reported = True   # set BEFORE the DB write (issue #7)
            try:
                if self._heatmap_svc:
                    self._heatmap_svc.repo.mark_goal_reached(date.today())
                    self._heatmap_svc.refresh_goal_days()
            except Exception as exc:
                log.error("goal persist failed: %s", exc)

        today_rate = week_avg = 0.0
        new_today = reviews_today = relearned_today = 0
        new_events_today = 0
        cur_streak = lng_streak = 0

        if self._heatmap_svc:
            try:
                today_rate = self._heatmap_svc.today_again_rate()
                week_avg   = self._heatmap_svc.week_avg_again_rate()
                today      = date.today()
                rl         = self._heatmap_svc._revlog_by_day(today, today)
                day_data   = rl.get(today, {})
                new_today       = day_data.get("new_cards",     0)
                reviews_today   = day_data.get("reviews_count", 0)
                relearned_today = day_data.get("relearned",     0)
                new_events_today = day_data.get("new_events",   0)
            except Exception as exc:
                log.error("session stats gather failed: %s", exc)
            try:
                cur_streak, lng_streak = self._heatmap_svc.goal_streak(
                    ec, force_today=bool(goal_hit)
                )
            except Exception as exc:
                log.error("goal_streak failed: %s", exc)

        return SessionReport(
            score=score,
            session_count=self.session_count,
            is_long=is_long,
            long_mins=long_mins,
            norm_mins=norm_mins,
            eff_secs=eff_secs,
            eff_mins=eff_mins,
            cards_done=cards_done,
            goal_hit=goal_hit,
            today_rate=today_rate,
            week_avg=week_avg,
            new_today=new_today,
            reviews_today=reviews_today,
            relearned_today=relearned_today,
            cur_streak=cur_streak,
            lng_streak=lng_streak,
            long_label=f"{long_mins}-min long break",
            norm_label=f"{norm_mins}-min break",
            new_events_today=new_events_today,
        )

    # ── popup display (issue #14 — pure UI, no DB or timer calls) ────────────

    def _show_popup(self, r: SessionReport) -> None:
        """Create and display the appropriate popup from a SessionReport.

        Reads r's fields only — no mutable state changes, no DB access.
        """
        from aqt import mw
        from ..ui.popups import (
            BreakChoicePopup, GoalReachedPopup, SessionSummaryPopup,
        )

        if r.goal_hit:
            popup = GoalReachedPopup(
                goal_description=r.goal_hit,
                effective_minutes=r.eff_mins,
                quality_score=r.score,
                again_rate=r.today_rate,
                avg_7day_again_rate=r.week_avg,
                current_streak=r.cur_streak,
                longest_streak=r.lng_streak,
                new_cards=r.new_today,
                reviews=r.reviews_today,
                relearned=r.relearned_today,
                raw_new_events=r.new_events_today,
                on_continue=self.cmd_skip_break,
                on_done=lambda: None,
                parent=mw,
            )
            self.set_active_popup(popup)
            popup.show()
            return

        show_report = (
            self._config_mgr.data
            .get("fatigue", {})
            .get("show_daily_report", True)
        )
        if not show_report:
            if r.is_long:
                self.start_break(
                    r.long_mins * 60, r.long_label, r.cards_done, r.eff_secs, r.score
                )
            else:
                self.start_break(
                    r.norm_mins * 60, r.norm_label, r.cards_done, r.eff_secs, r.score
                )
            return

        elapsed_raw = int(r.eff_secs / r.score) if r.score else r.eff_secs

        if r.is_long:
            popup = BreakChoicePopup(
                session_num=r.session_count,
                long_break_label=r.long_label,
                normal_break_label=r.norm_label,
                cards_done=r.cards_done,
                duration_seconds=elapsed_raw,
                fatigue_score=r.score,
                on_long_break=lambda: self.start_break(
                    r.long_mins * 60, r.long_label, r.cards_done, r.eff_secs, r.score
                ),
                on_normal_break=lambda: self.start_break(
                    r.norm_mins * 60, r.norm_label, r.cards_done, r.eff_secs, r.score
                ),
                on_skip_break=self.cmd_skip_break,
                parent=mw,
            )
        else:
            popup = SessionSummaryPopup(
                session_num=r.session_count,
                break_label=r.norm_label,
                duration_seconds=elapsed_raw,
                fatigue_score=r.score,
                again_rate=r.today_rate,
                avg_7day_again_rate=r.week_avg,
                on_start_break=lambda: self.start_break(
                    r.norm_mins * 60, r.norm_label, r.cards_done, r.eff_secs, r.score
                ),
                on_skip_break=self.cmd_skip_break,
                parent=mw,
            )
        self.set_active_popup(popup)
        popup.show()

    # ── goal management ───────────────────────────────────────────────────────

    @staticmethod
    def _authoritative_queue_counts() -> tuple[int, int, int, int]:
        """Return (new, learning, review, total) cards Anki considers
        available for the current study day — i.e. respecting the deck's
        daily new-card limit, the review-limit-throttles-new-cards rule,
        suspended/buried cards, and Anki's own day cutoff. These are the
        same numbers the deck browser and reviewer are built from. This is
        the single source of truth for "Due" everywhere in FocusFlow —
        the toolbar's Due display, the Done/Remaining display mode,
        and both "all due cards finished" checks below all
        call this instead of maintaining their own definition.

        BUG FIX: previously computed via
        len(mw.col.find_cards("is:due OR is:new")), which is NOT
        authoritative. "is:new" matches every unseen card in the collection
        regardless of the deck's daily new-card limit, and — confirmed by
        direct testing against the real Anki scheduler backend — does not
        exclude suspended or buried cards either, since it filters on card
        TYPE rather than queue. That let the toolbar's Due count include
        cards Anki had already decided to defer to a future day, or that
        were suspended/buried and unreachable regardless.

        mw.col.sched.counts() was verified — against the real Anki
        scheduler backend, on both this add-on's minimum supported version
        (23.10) and a current version — to already return the correct,
        live (new, learning, review) tuple with no reset() call needed, in
        every tested state: daily limit under/over/exhausted, review cards
        due today vs. tomorrow, near-term vs. far-out intraday learning
        cards, suspended cards, buried cards, mid-session after answering,
        and across a simulated day cutover.

        Falls back to the day-cutoff-aware SQL already proven in
        heatmap_service.py if sched.counts() is ever unavailable or
        (unexpectedly) empty — the same defensive pattern already used
        there, kept here for consistency and as a safety net. Known minor
        gap in that fallback only: it can under-count intraday learning
        cards due within the next few minutes but not yet due by strict
        timestamp comparison (sched.counts() has no such gap) — acceptable
        for a fallback path that should rarely trigger.
        """
        from aqt import mw
        if not (mw and mw.col):
            return (0, 0, 0, 0)
        try:
            new_c, lrn_c, rev_c = mw.col.sched.counts()
            if (new_c, lrn_c, rev_c) != (0, 0, 0):
                return (int(new_c), int(lrn_c), int(rev_c),
                        int(new_c) + int(lrn_c) + int(rev_c))
        except Exception as exc:
            log.debug("sched.counts() failed in _authoritative_queue_counts: %s", exc)
        try:
            import time as _time
            today_ord = mw.col.sched.today
            now_ts = int(_time.time())
            rev = mw.col.db.scalar(
                "SELECT count() FROM cards WHERE queue = 2 AND due <= ?", today_ord) or 0
            lrn = mw.col.db.scalar(
                "SELECT count() FROM cards WHERE (queue = 1 AND due <= ?) OR (queue = 3 AND due <= ?)",
                now_ts, today_ord) or 0
            new = mw.col.db.scalar("SELECT count() FROM cards WHERE queue = 0") or 0
            return (int(new), int(lrn), int(rev), int(new) + int(lrn) + int(rev))
        except Exception as exc:
            log.debug("SQL fallback failed in _authoritative_queue_counts: %s", exc)
            return (0, 0, 0, 0)

    def check_goal_reached(self) -> Optional[str]:
        """Return a human-readable goal description if any goal is met, else None."""
        if self._config_mgr is None or self._session_svc is None:
            return None
        ec = self._config_mgr.data.get("end_conditions", {})
        if ec.get("cards_enabled", False):
            target = int(ec.get("cards_target", 25))
            if self.total_cards_today >= target:
                return f"Cards goal reached — {target} cards reviewed"
        if ec.get("sessions_enabled", False):
            target = int(ec.get("sessions_target", 4))
            if self.session_count >= target:
                return f"Sessions goal reached — {target} sessions completed"
        if ec.get("max_time_enabled", False):
            max_s = int(float(ec.get("max_time_minutes", 25)) * 60)
            if self.total_study_seconds_today >= max_s:
                return (
                    f"Time goal reached — "
                    f"{int(ec.get('max_time_minutes', 25))} minutes"
                )
        if ec.get("all_due_enabled", False):
            try:
                if self._authoritative_queue_counts()[3] == 0:
                    return "All due cards finished for today"
            except Exception as exc:
                log.debug("authoritative queue count unavailable in check_goal_reached: %s", exc)
        return None

    def check_and_mark_all_due_goal(self) -> None:
        """Detect and record the all_due_enabled goal on reviewer exit.

        Called every time the user leaves the reviewer.  At that moment
        mw.col.sched.counts() reflects the true remaining queue — zero means
        all due cards are done.

        _goal_reported is checked BEFORE mark_goal_reached() writes to the DB.
        This ordering is load-bearing: on_session_completed also sets
        goal_reported = True, so this guard prevents a double-write when the
        user finishes the last card at the same time the Pomodoro timer fires.
        goal_reported = True is set BEFORE the DB write for the same reason.
        """
        if self.goal_reported:
            return
        if self._config_mgr is None or self._heatmap_svc is None:
            return
        ec = self._config_mgr.data.get("end_conditions", {})
        if not ec.get("all_due_enabled", False):
            return
        try:
            from aqt import mw
            if not (mw and mw.col):
                return
            # Uses the same _authoritative_queue_counts() as
            # check_goal_reached() above, so the two "all due" checks can
            # never disagree (previously this used sum(mw.col.sched.counts())
            # directly while check_goal_reached used a raw find_cards
            # search — two different definitions of "done").
            if self._authoritative_queue_counts()[3] > 0:
                return
            self.goal_reported = True   # set BEFORE the DB write
            self._heatmap_svc.repo.mark_goal_reached(date.today())
            self._heatmap_svc.refresh_goal_days()
        except Exception as exc:
            log.error("all_due goal check failed: %s", exc)

    # ── commands ──────────────────────────────────────────────────────────────

    def cmd_skip_break(self) -> None:
        self.close_active_popup()
        if self._timer_mgr:
            self._timer_mgr.stop_idle()
        if self._toolbar:
            self._toolbar.set_idle()

    def cmd_resume_after_break(self) -> None:
        self.close_active_popup()
        if self._timer_mgr:
            self._timer_mgr.stop_idle()
        if self._toolbar:
            self._toolbar.set_idle()

    def start_break_manual(self, is_long: bool) -> None:
        """User-initiated break from the toolbar's right-click menu — 'Short
        break' / 'Long break' — independent of the normal end-of-session
        flow, so there's no just-finished session data (cards/effective
        time/quality) to attach; those are passed as zero."""
        if self._config_mgr is None:
            return
        t = self._config_mgr.data.get("timer", {})
        if is_long:
            mins  = int(t.get("long_break_minutes", 15))
            label = f"{mins}-minute long break"
        else:
            mins  = int(t.get("break_minutes", 5))
            label = f"{mins}-minute break"
        self.start_break(mins * 60, label, 0, 0, 0.0)

    def start_break(
        self,
        duration_seconds: int,
        label:      str,
        cards_done: int   = 0,
        eff_secs:   int   = 0,
        quality:    float = 0.0,
    ) -> None:
        """Start a break timer and show the running-break popup.

        on_break_taken() is NOT called here — fatigue recovery happens exactly
        once in on_break_completed() when the break timer fires.
        """
        from aqt import mw
        from ..ui.popups import BreakRunningPopup

        self.close_active_popup()
        if self._timer_mgr:
            self._timer_mgr.start_break(duration_seconds=duration_seconds)
        popup = BreakRunningPopup(
            break_seconds=duration_seconds,
            break_label=label,
            cards_done=cards_done,
            effective_secs=eff_secs,
            quality_score=quality,
            on_end_early=self.cmd_skip_break,
            parent=mw,
        )
        # Drive the popup countdown from the TimerManager tick signal so there
        # is exactly one clock source; disconnect cleanly when popup closes.
        if self._timer_mgr:
            self._timer_mgr.tick.connect(popup.update_from_tick)
            def _disconnect() -> None:
                try:
                    self._timer_mgr.tick.disconnect(popup.update_from_tick)
                except Exception as exc:
                    log.debug("tick.disconnect failed (signal already gone?): %s", exc)
            popup.finished.connect(_disconnect)
        self.set_active_popup(popup)
        popup.show()

    def show_fatigue_suggestion(self) -> None:
        if self._active_popup is not None:
            return
        from aqt import mw
        from ..ui.popups import FatigueSuggestionPopup

        popup = FatigueSuggestionPopup(
            on_take_break=self._cmd_fatigue_break, parent=mw
        )
        self.set_active_popup(popup)
        popup.show()

    def _cmd_fatigue_break(self) -> None:
        self.close_active_popup()
        brk_mins = int(
            self._config_mgr.data.get("timer", {}).get("break_minutes", 5)
        )
        self.start_break(brk_mins * 60, f"{brk_mins}-min break")

    # ── conditions toolbar update ─────────────────────────────────────────────

    @staticmethod
    def _progress_bar(pct: int, segments: int = 8) -> str:
        pct = max(0, min(100, pct))
        filled = round(segments * pct / 100)
        return f"{'█' * filled}{'░' * (segments - filled)} {pct}%"

    def _pace_prediction(self, remaining_cards: int) -> str | None:
        """'At your current pace you'll finish today's reviews in ~17 minutes.'

        Uses today's average seconds-per-card as the pace. Returns None when
        there isn't enough data yet (no cards answered today) or nothing left
        to predict.

        UNUSED as of 1.0.15 — no longer called from update_conditions().
        total_study_seconds_today only advances at the end of a completed
        Pomodoro session while total_cards_today advances live per card, so
        this average drifts during an in-progress session and jumps once it
        completes (see investigation notes). Left in place rather than
        deleted per the 1.0.15 HUD-fix scope, in case a future update
        addresses the underlying pace calculation properly.
        """
        if remaining_cards <= 0 or self.total_cards_today <= 0:
            return None
        avg_secs_per_card = self.total_study_seconds_today / self.total_cards_today
        if avg_secs_per_card <= 0:
            return None
        predicted_secs = avg_secs_per_card * remaining_cards
        total_mins = int(round(predicted_secs / 60))
        if total_mins <= 0:
            return "At your current pace you'll finish today's reviews in under a minute."
        if total_mins == 1:
            return "At your current pace you'll finish today's reviews in about 1 minute."
        hours, mins = divmod(total_mins, 60)
        if hours <= 0:
            return f"At your current pace you'll finish today's reviews in about {mins} minutes."
        hour_word = "hour" if hours == 1 else "hours"
        if mins == 0:
            return f"At your current pace you'll finish today's reviews in about {hours} {hour_word}."
        min_word = "minute" if mins == 1 else "minutes"
        return (f"At your current pace you'll finish today's reviews in about "
                f"{hours} {hour_word} {mins} {min_word}.")

    def _eta_short_text(self, remaining_cards: int) -> str:
        """Compact toolbar text for the 'Estimated time to finish' goal
        display mode — e.g. '~17m left' or '~1h 26m left' once past an hour.
        Falls back to a plain remaining count when there isn't enough pace
        data yet (no cards answered today).

        UNUSED as of 1.0.15 — "Estimated time to finish" was removed from
        the Goal display choices (see _pace_prediction() above for why).
        Left in place rather than deleted per the 1.0.15 HUD-fix scope.
        """
        if remaining_cards <= 0:
            return "All caught up"
        if self.total_cards_today <= 0:
            return f"{remaining_cards} left"
        avg_secs_per_card = self.total_study_seconds_today / self.total_cards_today
        if avg_secs_per_card <= 0:
            return f"{remaining_cards} left"
        total_mins = int(round(avg_secs_per_card * remaining_cards / 60))
        if total_mins <= 0:
            return "<1m left"
        hours, mins = divmod(total_mins, 60)
        # BUG FIX: was always showing raw minutes (e.g. "86 min left"),
        # which reads as an unrealistic/confusing number once it crosses an
        # hour. Break into hours+minutes past 60, matching how people
        # actually read a time estimate ("1h 26m left", not "86 min left").
        if hours > 0:
            return f"~{hours}h {mins}m left" if mins else f"~{hours}h left"
        return f"~{mins}m left"

    def update_conditions(self) -> None:
        if self._toolbar is None or self._config_mgr is None or self._session_svc is None:
            return

        ec    = self._config_mgr.data.get("end_conditions", {})
        parts: list[str] = []
        goal_just_hit = False
        # (done, target, remaining) for whichever single goal is active — used
        # to render a progress bar (or the Done/Remaining display) instead
        # of a raw count. Left None when zero or 2+ goals are active, since
        # a single compact display can't represent more than one goal.
        _goal: tuple[int, int, int] | None = None
        _active_goal_count = 0
        # (new, learning, review) breakdown behind the current authoritative
        # due total — only set by the all_due branch below, since the
        # Done/Remaining display is specifically about that goal (see
        # _authoritative_queue_counts()).
        _due_breakdown: tuple[int, int, int] | None = None

        if ec.get("cards_enabled", False):
            target = int(ec.get("cards_target", 25))
            rem    = max(0, target - self.total_cards_today)
            parts.append(f"{rem} cards")
            _active_goal_count += 1
            _goal = (self.total_cards_today, target, rem)
            if rem == 0 and not self.goal_reported:
                goal_just_hit = True
        if ec.get("sessions_enabled", False):
            target = int(ec.get("sessions_target", 4))
            rem    = max(0, target - self.session_count)
            parts.append(f"{rem} sessions")
            _active_goal_count += 1
            if rem == 0 and not self.goal_reported:
                goal_just_hit = True
        if ec.get("max_time_enabled", False):
            max_s = int(float(ec.get("max_time_minutes", 25)) * 60)
            rem   = max(0, max_s - self.total_study_seconds_today)
            parts.append(f"{rem // 60}m {rem % 60:02d}s")
            _active_goal_count += 1
            if rem == 0 and not self.goal_reported:
                goal_just_hit = True
        if ec.get("all_due_enabled", False):
            try:
                # BUG FIX: previously len(mw.col.find_cards("is:due OR
                # is:new")), which is not authoritative — see
                # _authoritative_queue_counts() for the full explanation.
                # sched.counts() is the same source Anki's own deck
                # browser/reviewer are built from.
                new_c, lrn_c, rev_c, total = self._authoritative_queue_counts()
                if self._due_baseline_today is None and total > 0:
                    self._due_baseline_today = total
                if total > 0:
                    parts.append(f"{total} due")
                    _active_goal_count += 1
                    baseline = self._due_baseline_today or total
                    done = max(0, baseline - total)
                    _goal = (done, baseline, total)
                    _due_breakdown = (new_c, lrn_c, rev_c)
                elif not self.goal_reported:
                    goal_just_hit = True
            except Exception as exc:
                log.debug("authoritative queue count failed in update_conditions: %s", exc)

        _tb_cfg = self._config_mgr.data.get("toolbar", {})
        _goal_mode = _tb_cfg.get("goal_display", "count")
        # BUG FIX: "eta" (Estimated time to finish) was removed as a Goal
        # display choice in 1.0.15 — its pace math proved unreliable (see
        # investigation notes). "new_learn" (New/Learning breakdown) was
        # removed in a later version — a faithful 3-number breakdown with
        # real words never fit the toolbar's 120px HUD area even at
        # realistic values, and abbreviating it wasn't acceptable either.
        # Config saved with either old value is migrated to "count" in
        # ConfigManager._normalize_profile(); this is a second, defensive
        # check so any unrecognized value here can never silently blank the
        # display.
        if _goal_mode not in ("bar", "count", "done_remaining"):
            _goal_mode = "count"

        if (_active_goal_count == 1 and _goal is not None
                and _goal_mode in ("bar", "done_remaining")):
            done, target, remaining = _goal
            pct = 0 if target <= 0 else round(100 * done / target)
            if _goal_mode == "bar":
                self._toolbar.set_conditions([self._progress_bar(pct)])
                self._toolbar.set_conditions_tooltip(f"Today's Goal — {pct}% complete.")
            elif _goal_mode == "done_remaining" and _due_breakdown is not None:
                # "Done" = cards actually answered today (FocusFlow's own
                # existing study-day counter — the same one the Cards goal
                # above uses), deliberately NOT the baseline/total-based
                # `done` above, which can move for reasons other than the
                # user answering a card (e.g. a day cutover pulling more
                # cards into the due window). Mixing the two would make
                # this number mean different things depending on what else
                # changed in the queue.
                done_today = self.total_cards_today
                denom = done_today + remaining
                pct2 = round(100 * done_today / denom) if denom > 0 else 0
                self._toolbar.set_conditions([f"{done_today} / {denom} due"])
                self._toolbar.set_conditions_tooltip(f"Today's Goal — {pct2}% complete.")
            else:
                self._toolbar.set_conditions(parts)
                self._toolbar.set_conditions_tooltip(None)
        else:
            self._toolbar.set_conditions(parts)
            # BUG FIX: previously showed _pace_prediction() here, which uses
            # the same average-seconds-per-card estimate as the removed
            # "eta" display and shares its accuracy problems (see
            # investigation notes) — it was quietly active on every default
            # install via this exact fallback, not just via the rarely-used
            # eta option. No tooltip is more honest than an unreliable one.
            self._toolbar.set_conditions_tooltip(None)

        # Provide immediate in-session UI feedback when a goal is hit mid-session,
        # without writing to the DB (that happens at on_session_completed time).
        if goal_just_hit:
            try:
                self._toolbar.set_goal_reached()
            except Exception as exc:
                log.debug("toolbar.set_goal_reached() unavailable: %s", exc)

    # ── fatigue-adaptive timer duration ──────────────────────────────────────

    def apply_fatigue_to_timer(self, snap: "Optional[FatigueSnapshot]" = None) -> None:
        """Adjust next study session duration based on current fatigue state.

        Multipliers:  focused → 1.00 · drifting → 0.85 · low_quality → 0.70
        Called after each card answer (with snap) and after a break completes
        (without snap, so the recovered state is reflected immediately).
        """
        if self._timer_mgr is None or self._config_mgr is None:
            return
        if not self._config_mgr.data.get("fatigue", {}).get("adaptive_duration", True):
            self._timer_mgr.set_fatigue_multiplier(1.0)
            return
        state = snap.state if snap is not None else (
            self._fatigue.current_state if self._fatigue else "focused"
        )
        mult = {"focused": 1.0, "drifting": 0.85, "low_quality": 0.70}.get(state, 1.0)
        self._timer_mgr.set_fatigue_multiplier(mult)

    # private alias used inside this module to avoid long names
    _apply_fatigue_to_timer = apply_fatigue_to_timer
