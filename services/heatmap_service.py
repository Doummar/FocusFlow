from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from aqt import mw

from ..core.metrics import effective_minutes, summarize_sessions
from ..data.session_repo import SessionRepo
from ..utils.logger import log


def _get_day_cutoff() -> int | None:
    """The timestamp (unix seconds) of the NEXT scheduler rollover, i.e.
    Anki's own `col.sched.day_cutoff` -- the single source of truth Anki's
    real pylib/anki/stats.py uses throughout (see todayStats() there:
    `(self.col.sched.day_cutoff - 86400) * 1000`). Returns None if this
    isn't available (defensive fallback for unexpected Anki API changes),
    in which case callers fall back to calendar-midnight bucketing rather
    than crash.
    """
    try:
        return int(mw.col.sched.day_cutoff)
    except Exception:
        return None


def scheduler_today(day_cutoff: int | None) -> date:
    """The calendar date Anki's scheduler currently considers 'today'.

    Identical arithmetic to pylib/anki/stats.py's todayStats(): day_cutoff
    is the timestamp of the upcoming rollover, so subtracting one day's
    worth of seconds gives the start of the CURRENT scheduler day. Falls
    back to date.today() (calendar day) if day_cutoff is unavailable.
    """
    if day_cutoff is None:
        return date.today()
    return date.fromtimestamp(day_cutoff - 86400)


def day_bounds_ms(day: date, day_cutoff: int | None, today: date) -> tuple[int, int]:
    """[start_ms, end_ms) for the scheduler-day labeled `day`.

    Uses the same day_cutoff - N*86400 arithmetic Anki's own stats module
    applies everywhere in pylib/anki/stats.py -- deliberately NOT calendar
    midnight, and deliberately not a DST-aware per-day recomputation,
    since Anki's own legacy stats code doesn't attempt that either; this
    matches its semantics (and its known limitations) exactly rather than
    "improving" on them and risking a new source of disagreement.

    Falls back to calendar-midnight bounds if day_cutoff is unavailable.
    """
    if day_cutoff is None:
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    days_ago = (today - day).days
    start_s = day_cutoff - 86400 * (days_ago + 1)
    end_s = day_cutoff - 86400 * days_ago
    return start_s * 1000, end_s * 1000


def study_day_from_revlog_id(revlog_id_ms: int, day_cutoff: int | None) -> date:
    """Map a single revlog.id (ms since epoch) to the scheduler-day it
    belongs to. Anki's own stats.py never needs this -- it only ever
    queries a single fixed N-day-old boundary -- but FocusFlow needs to
    classify many individual revlog rows, so this generalizes the exact
    same day_cutoff - N*86400 arithmetic to a per-row lookup instead of a
    single fixed cutoff. Falls back to calendar-day mapping (the previous
    behavior) if day_cutoff is unavailable.
    """
    if day_cutoff is None:
        return date.fromtimestamp(revlog_id_ms / 1000)
    row_s = revlog_id_ms / 1000
    days_ago = math.floor((day_cutoff - row_s) / 86400)
    bucket_start_s = day_cutoff - 86400 * (days_ago + 1)
    return date.fromtimestamp(bucket_start_s)


def activity_total(day_data: dict) -> int:
    """Anki's own definition of a day's total study activity: types
    0+1+2+3 (Learning + Review + Relearning + Filtered) combined --
    matching pylib/anki/stats.py's `WHERE type != REVLOG_RESCHED`, i.e.
    everything except Manual/Rescheduled entries.

    day_data (or a HeatmapDay/HeatmapStats-shaped object) already tracks
    new_cards (type 0), reviews_count (types 1+3 combined), and relearned
    (type 2) as separate components -- this is the ONE place they get
    summed into the headline total. Individual category values remain
    available unchanged everywhere they're already exposed.
    """
    get = day_data.get if isinstance(day_data, dict) else (
        lambda k, default=0: getattr(day_data, k, default))
    return get("new_cards", 0) + get("reviews_count", 0) + get("relearned", 0)


def raw_study_events(day_data: dict) -> int:
    """Raw, undeduplicated count of a day's study-relevant revlog rows
    across types 0-3 (Learning + Review + Relearning + Filtered) -- matches
    Anki's own LIVE studied_today() semantics (rslib/src/storage/revlog/
    studied_today.sql: a plain COUNT() excluding only Manual/Rescheduled,
    with no per-card deduplication anywhere), unlike activity_total() above,
    which deduplicates the type=0 component into distinct new cards.

    Used ONLY by the specific "Reviews" display surfaces that are meant to
    show an Anki-compatible raw study-event total (day-popup, week/month
    popup, _day_as_stat, the stats-table Reviews column, and the yearly
    summary). activity_total() and all of its existing callers -- goal
    tracking, again-rate calculations, Study Quality -- are unchanged and
    continue to use the hybrid figure exactly as before.
    """
    get = day_data.get if isinstance(day_data, dict) else (
        lambda k, default=0: getattr(day_data, k, default))
    return get("new_events", 0) + get("reviews_count", 0) + get("relearned", 0)


def _answer_quality(again: int, hard: int, good: int, easy: int,
                    weights: dict) -> float:
    """Normalise a weighted answer score to [0, 1]."""
    total = again + hard + good + easy
    if total == 0:
        return 0.0
    w_a = weights.get("again", -2)
    w_h = weights.get("hard",  -1)
    w_g = weights.get("good",   1)
    w_e = weights.get("easy",   2)
    raw = (again * w_a + hard * w_h + good * w_g + easy * w_e) / total
    lo  = min(w_a, w_h, w_g, w_e)
    hi  = max(w_a, w_h, w_g, w_e)
    return 0.5 if hi == lo else max(0.0, min(1.0, (raw - lo) / (hi - lo)))


def _effective_with_cap(items: list, cap_secs: int) -> float:
    """Sum effective minutes, capping per-card duration to avoid outliers."""
    result = 0.0
    for i in items:
        cards = max(1, getattr(i, "cards_done", 1))
        if cap_secs > 0:
            secs_per_card = i.duration / cards
            dur = (min(i.duration, cards * cap_secs)
                   if secs_per_card > cap_secs else i.duration)
        else:
            dur = i.duration
        result += effective_minutes(dur, i.effective_score)
    return result


@dataclass(frozen=True)
class HeatmapDay:
    day: date
    minutes: float
    effective_minutes: float
    quality: float
    sessions: int
    is_future: bool = False
    due_cards: int = 0
    new_cards: int = 0
    new_events: int = 0     # raw (undeduplicated) type=0 row count -- see raw_study_events()
    reviews_count: int = 0
    relearned: int = 0
    review_time_seconds: int = 0
    again_count: int = 0   # ease=1 presses from revlog
    hard_count: int  = 0   # ease=2 presses from revlog
    easy_count: int  = 0   # ease=4 presses from revlog
    is_goal_reached: bool = False
    # Remaining cards for today from the live scheduler; 0 for all other days.
    new_due: int = 0        # new cards still to introduce today
    lrn_due: int = 0        # learning/relearning steps still due today
    rev_due: int = 0        # mature reviews still due today
    # Distinct cards behind the DISPLAYED "Reviews" figure (reviews_count +
    # relearned combined — see heatmap_widget.py's day-popup/tab rendering,
    # which shows that sum, not raw reviews_count alone). Includes type 1
    # (review), type 2 (relearning), and type 3 (filtered/cram) revlog rows.
    # A card reviewed/relearned several times this day (lapse-and-relearn
    # cycles, repeated cram-deck study) still only counts once here — see
    # "Reviews" vs "Cards Reviewed" in the UI.
    cards_reviewed: int = 0


@dataclass(frozen=True)
class HeatmapStats:
    label: str
    minutes: float
    effective_minutes: float
    quality: float
    productivity: float
    sessions: int
    new_cards: int = 0
    reviews_count: int = 0
    review_time_minutes: float = 0.0
    # Distinct cards behind the displayed "Reviews" figure for this period
    # (see HeatmapDay's cards_reviewed above — same relearn-inclusive scope).
    cards_reviewed: int = 0
    # The exact [start_date, end_date] (inclusive) this period covers — the
    # same boundaries stats() already computes to run its own queries, kept
    # on the result so callers (the browse-click feature, see ffBrowseStat
    # in heatmap_widget.py and _browse_query_for_range in __init__.py) can
    # reuse them instead of recomputing "what date range is 'Weekly'?"
    # themselves.
    start_date: date | None = None
    end_date: date | None = None
    # Raw (undeduplicated) type=0 row count for this period -- see
    # raw_study_events(). Appended at the end (not next to new_cards above)
    # because one construction site below uses positional arguments;
    # inserting a field mid-list there would silently misalign every field
    # after it. Left at its default (0) by every current construction site --
    # nothing reads it yet, it exists only for structural parity with
    # HeatmapDay.new_events.
    new_events: int = 0
    # The OLD activity_total()-based aggregate (distinct new cards + raw
    # review/relearn/filtered events) for this period, kept SEPARATE from
    # reviews_count above. reviews_count now holds the raw, Anki-matching
    # Reviews total (raw_study_events()) for display; this field exists
    # specifically so callers that need the pre-existing hybrid figure --
    # e.g. week_again_rt in heatmap_widget.py, which must keep using the
    # same denominator scope as today_again_rt -- have somewhere to read it
    # from without reviews_count having to serve two different purposes.
    activity_total: int = 0
    # Weighted again-press total for this period (types 0-3 revlog rows with
    # ease=1), same activity_total()-scoped ag_tot already computed inside
    # stats()'s _nc_rc_rt() for the Study Quality "answers"/"retention"
    # formulas below -- just not previously attached to the returned object.
    # Added so heatmap_widget.py's deck-browser "elevated again rate" banner
    # can read a real week-average again count instead of silently falling
    # through getattr()'s default of 0 (HeatmapStats had no again_count
    # field at all, so week_again_rt was always computed as 0/week_total,
    # i.e. always 0.0, regardless of the actual week). Appended at the end,
    # after activity_total, for the same positional-construction-safety
    # reason new_events above is appended where it is.
    again_count: int = 0
    # Distinct cards touched today across ALL revlog types (0=new/learning,
    # 1=review, 2=relearning, 3=filtered/cram) — the all-inclusive companion
    # to cards_reviewed above, which deliberately EXCLUDES type=0. Added for
    # the deck-browser "Daily Status" line (heatmap_widget.py), which needs
    # an honest "cards studied today" figure; cards_reviewed alone would
    # silently undercount on any day involving new cards, since it was
    # designed to mirror the narrower "Reviews" stat, not total study
    # activity. Appended at the end, after again_count, for the same
    # positional-construction-safety reason documented on new_events and
    # again_count above — stats() below is the only call site and it sets
    # this via keyword, but existing positional args must not shift.
    cards_studied: int = 0


class HeatmapService:
    def __init__(self, repo: SessionRepo) -> None:
        self.repo = repo
        # Revlog cache keyed on (sched_today_ordinal, start_ms, end_ms).
        # Invalidated after each card answer and on profile open.
        self._revlog_cache: dict[tuple, dict] = {}
        # Cache for all_year_stats — keyed on (years_tuple, today) so it auto-expires
        # at midnight and is explicitly cleared by invalidate_cache().
        self._year_stats_cache: dict[tuple, dict[int, dict]] = {}
        # Cache for _distinct_cards_reviewed()'s period-level COUNT(DISTINCT cid)
        # queries — keyed the same way as _revlog_cache, same invalidation.
        self._distinct_cards_cache: dict[tuple, int] = {}
        # Cache for _distinct_new_cards()'s period-level COUNT(DISTINCT cid)
        # WHERE type=0 queries. Deliberately a SEPARATE dict from
        # _distinct_cards_cache above: stats() calls both methods with the
        # exact same (start_day, end_day) for every period, and they'd
        # produce identical cache keys despite being different queries
        # (type IN (1,2,3) vs type=0) if they shared one dict.
        self._distinct_new_cards_cache: dict[tuple, int] = {}
        # Cache for _distinct_cards_studied()'s period-level COUNT(DISTINCT
        # cid) WHERE type IN (0,1,2,3) queries — again a SEPARATE dict from
        # both caches above, same collision reasoning: stats() calls all
        # three distinct-card methods with the identical (start_day, end_day)
        # for every period, and they'd collide on cache key despite being
        # three different queries if they shared a dict.
        self._distinct_cards_studied_cache: dict[tuple, int] = {}
        # PERFORMANCE FIX: stats(), stats_for_year() (called once per navigable
        # year by all_year_stats()), and goal_streak() each independently
        # called self.repo.all_sessions() — a full read of every FocusFlow
        # session ever recorded — every time this class's other caches were
        # cold (which is every single time, right after any card is answered,
        # since invalidate_cache() clears everything else). For one deck-
        # browser render that's up to (2 + number of navigable years)
        # identical full-table reads of data that cannot change mid-render.
        # Cached here instead, using the same lifecycle as the caches above:
        # cleared by invalidate_cache(), which is now also called right after
        # a session is durably written (see on_session_completed() in
        # session_coordinator.py and _cmd_log_manual_session() in
        # __init__.py) so this cache can never go stale relative to the DB.
        self._all_sessions_cache: list | None = None
        # Dates on which the user explicitly reached their configured goal.
        # Loaded from DB on init, refreshed when a goal fires.
        self._goal_dates: set[date] = self._load_goal_dates()

    def invalidate_cache(self) -> None:
        """Clear cached revlog/session data.  Call whenever the revlog or
        the FocusFlow sessions table may have changed."""
        self._revlog_cache.clear()
        self._year_stats_cache.clear()
        self._distinct_cards_cache.clear()
        self._distinct_new_cards_cache.clear()
        self._distinct_cards_studied_cache.clear()
        self._all_sessions_cache = None

    def _get_all_sessions(self) -> list:
        """Cached wrapper around repo.all_sessions() — see the comment on
        _all_sessions_cache in __init__ for why this exists."""
        if self._all_sessions_cache is None:
            self._all_sessions_cache = self.repo.all_sessions()
        return self._all_sessions_cache

    def refresh_goal_days(self) -> None:
        """Reload goal-reached dates from the DB.  Call after marking a new date."""
        self._goal_dates = self._load_goal_dates()

    def _load_goal_dates(self) -> set[date]:
        try:
            return self.repo.goal_reached_days()
        except Exception as exc:
            log.warning("goal_reached_days load failed: %s", exc)
            return set()

    def days(self, past_count: int = 365, future_count: int = 30,
             end_conditions: dict | None = None) -> list[HeatmapDay]:
        day_cutoff = _get_day_cutoff()
        today     = scheduler_today(day_cutoff)
        start_day = today - timedelta(days=past_count - 1)
        start_ts, _ = day_bounds_ms(start_day, day_cutoff, today)
        _, end_ts   = day_bounds_ms(today, day_cutoff, today)
        start_ts //= 1000
        end_ts //= 1000

        sessions = self.repo.sessions_between(start_ts, end_ts)
        session_buckets: dict[date, list] = {}
        for s in sessions:
            d = study_day_from_revlog_id(int(s.start_time) * 1000, day_cutoff)
            session_buckets.setdefault(d, []).append(s)

        revlog = self._revlog_by_day(start_day, today)

        ec = end_conditions or {}
        has_goal = bool(ec) and any([
            ec.get("cards_enabled"),
            ec.get("sessions_enabled"),
            ec.get("max_time_enabled"),
            ec.get("all_due_enabled"),
        ])

        past_days: list[HeatmapDay] = []
        for offset in range(past_count):
            day   = start_day + timedelta(days=offset)
            items = session_buckets.get(day, [])
            mins  = sum(i.duration for i in items) / 60.0
            eff   = sum(effective_minutes(i.duration, i.effective_score) for i in items)
            quality = (eff / mins) if mins else 0.0
            rl = revlog.get(day, {})

            if not has_goal:
                # No goal configured — any FocusFlow study day qualifies
                is_goal_reached = len(items) > 0
            else:
                # A day qualifies ONLY when the configured goal was explicitly
                # reached inside FocusFlow (stored in _goal_dates via
                # mark_goal_reached), OR when the FocusFlow session data for
                # that day already satisfies the goal conditions (backward-compat
                # for history recorded before the goal-dates table existed).
                # Anki revlog is intentionally NOT used here — reviewing cards
                # outside FocusFlow does not count toward the goal streak.
                is_goal_reached = (
                    day in self._goal_dates
                    or (bool(items) and self._day_met_goal(items, ec))
                )

            # For today only: count remaining due cards so the heatmap can show
            # "X still due today".  sched.counts() returns (0,0,0) outside a
            # review session in Anki 25.x (v3 scheduler), so we use direct SQL
            # queries for reviews and learning cards — these are always accurate
            # regardless of context — and fall back to sched.counts() for new
            # cards only (where daily-limit logic makes SQL unreliable).
            _day_new_due = _day_lrn_due = _day_rev_due = 0
            if day == today and mw is not None and mw.col is not None:
                try:
                    import time as _t
                    _today_ord = mw.col.sched.today
                    _now_ts    = int(_t.time())

                    # Review cards due today or overdue (queue=2, due=ordinal day)
                    _day_rev_due = int(mw.col.db.scalar(
                        "SELECT count() FROM cards WHERE queue = 2 AND due <= ?",
                        _today_ord) or 0)

                    # Learning cards:
                    #   queue=1 (intraday)   — due is a unix timestamp
                    #   queue=3 (day-learn)  — due is an ordinal day
                    _day_lrn_due = int(mw.col.db.scalar(
                        "SELECT count() FROM cards "
                        "WHERE (queue = 1 AND due <= ?) "
                        "   OR (queue = 3 AND due <= ?)",
                        _now_ts, _today_ord) or 0)

                    # New cards: try sched.counts() first (respects daily limits
                    # and is accurate within a study session).  Outside a session
                    # the v3 scheduler returns (0,0,0), so we fall back to a direct
                    # queue=0 SQL count — consistent with find_cards("is:new") used
                    # in the toolbar due-count (coordinator.py).
                    try:
                        _cnt = mw.col.sched.counts()
                        _day_new_due = int(_cnt[0]) if _cnt and sum(_cnt) > 0 else 0
                    except Exception:
                        _day_new_due = 0
                    if _day_new_due == 0:
                        try:
                            # Fallback: direct count of all cards in the new queue.
                            # May slightly exceed the daily new-card limit for large
                            # decks, but matches what find_cards("is:new") returns
                            # and is far more accurate than showing zero.
                            _day_new_due = int(mw.col.db.scalar(
                                "SELECT count() FROM cards WHERE queue = 0") or 0)
                        except Exception:
                            _day_new_due = 0
                except Exception:
                    pass

            past_days.append(HeatmapDay(
                    day=day, minutes=mins, effective_minutes=eff,
                    quality=quality, sessions=len(items),
                    is_future=False,
                    due_cards=_day_new_due + _day_lrn_due + _day_rev_due,
                    new_cards=rl.get("new_cards", 0),
                    new_events=rl.get("new_events", 0),
                    reviews_count=rl.get("reviews_count", 0),
                    relearned=rl.get("relearned", 0),
                    review_time_seconds=rl.get("review_time_ms", 0) // 1000,
                    again_count=rl.get("again_count", 0),
                    hard_count=rl.get("hard_count", 0),
                    easy_count=rl.get("easy_count", 0),
                    is_goal_reached=is_goal_reached,
                    new_due=_day_new_due,
                    lrn_due=_day_lrn_due,
                    rev_due=_day_rev_due,
                    cards_reviewed=rl.get("cards_reviewed", 0),
                ))

        future_due = self._future_due_counts(future_count)
        future_days: list[HeatmapDay] = []
        for offset in range(1, future_count + 1):
            day = today + timedelta(days=offset)
            future_days.append(HeatmapDay(
                day=day, minutes=0.0, effective_minutes=0.0,
                quality=0.0, sessions=0, is_future=True,
                due_cards=future_due.get(day, 0),
            ))

        return past_days + future_days

    def stats(self, measurement: dict | None = None) -> list[HeatmapStats]:
        day_cutoff   = _get_day_cutoff()
        today        = scheduler_today(day_cutoff)
        today_start  = datetime.combine(today, datetime.min.time())
        week_start   = today_start - timedelta(days=today_start.weekday())
        month_start  = today_start.replace(day=1)
        year_start   = today_start.replace(month=1, day=1)
        all_sessions = self._get_all_sessions()
        meas = measurement or {}
        qsrc = meas.get("quality_source", "focus")
        cap  = int(meas.get("eff_time_cap_secs", 90))

        def _nc_rc_rt(start: date, end: date) -> tuple[int, int, float, int]:
            rl = self._revlog_by_day(start, end)
            nc = sum(v.get("new_cards", 0) for v in rl.values())
            # Reviews total (displayed figure) = raw, undeduplicated types
            # 0+1+2+3 -- matches Anki's own LIVE studied_today() semantics via
            # raw_study_events(). Deliberately NOT activity_total() here: that
            # helper deduplicates type=0 into distinct new cards, which is the
            # exact mismatch this fix addresses for the Reviews statistic.
            rc = sum(raw_study_events(v) for v in rl.values())
            rt_mins = sum(v.get("review_time_ms", 0) for v in rl.values()) / 60000.0
            # Study Quality denominator -- intentionally still activity_total()
            # (unchanged, out of scope for this fix; see raw_study_events()'s
            # docstring for which callers stay on which helper).
            rl_tot = sum(activity_total(v) for v in rl.values())
            ag_tot = sum(v.get("again_count", 0) for v in rl.values())
            hd_tot = sum(v.get("hard_count", 0) for v in rl.values())
            ez_tot = sum(v.get("easy_count", 0) for v in rl.values())
            return nc, rc, rt_mins, rl_tot, ag_tot, hd_tot, ez_tot

        ranges = [
            ("Daily",          today_start,                       today),
            # Trailing windows (today inclusive) — distinct from the
            # calendar-based Weekly/Monthly below, used by the "Default
            # Statistics View" setting's "Last 7 Days"/"Last 30 Days".
            ("Last7",          today_start - timedelta(days=6),  today),
            ("Last30",         today_start - timedelta(days=29), today),
            ("Weekly",         week_start,            today),
            ("Monthly",        month_start,           today),
            (str(today.year),  year_start,            today),
            ("All-time",       datetime(2000, 1, 1),  today),
        ]
        result = []
        for label, start_dt, end_d in ranges:
            ts = int(start_dt.timestamp())
            items = [s for s in all_sessions if s.start_time >= ts]
            summary = summarize_sessions(items)
            nc, rc, rt_mins, rl_tot, ag_tot, hd_tot, ez_tot = _nc_rc_rt(start_dt.date(), end_d)
            dc = self._distinct_cards_reviewed(start_dt.date(), end_d)
            # All-inclusive distinct-card count (adds type=0 on top of dc's
            # type IN (1,2,3)) — backs the deck-browser "Daily Status" line's
            # "studied" figure. See _distinct_cards_studied()'s docstring.
            dcs = self._distinct_cards_studied(start_dt.date(), end_d)
            # Period-level New Cards must be a true distinct count over the
            # whole range, same reasoning as dc above: nc (from _nc_rc_rt)
            # sums each day's already-deduplicated new_cards count, which
            # double-counts any card whose type=0 events span multiple
            # days. nc itself is left untouched here and below -- it plays
            # no role in the Study Quality branches (all of which read
            # rl_tot/ag_tot/hd_tot/ez_tot, never nc), so this only changes
            # what gets attached to HeatmapStats.new_cards.
            distinct_nc = self._distinct_new_cards(start_dt.date(), end_d)

            # BUG FIX: this used to always use summarize_sessions()'s
            # uncapped "focus" calculation regardless of the Measurement
            # tab's quality_source/time-cap settings, while clicking an
            # individual day (days_for_year(), below) already respected
            # them — so "Today" in the stats cards and clicking today's own
            # cell could show different Effective time / Study Quality
            # numbers for literally the same day. Same formula now used in
            # both places.
            study_minutes = summary.study_minutes
            if qsrc == "answers" and rl_tot > 0:
                gd_tot = max(0, rl_tot - ag_tot - hd_tot - ez_tot)
                quality = _answer_quality(ag_tot, hd_tot, gd_tot, ez_tot,
                                           meas.get("ans_weights", {}))
                eff_minutes = study_minutes * quality
            elif qsrc == "retention" and rl_tot > 0:
                quality = max(0.0, 1.0 - ag_tot / rl_tot)
                eff_minutes = study_minutes * quality
            else:  # "focus" (default: fatigue-weighted effective time)
                eff_minutes = (_effective_with_cap(items, cap) if cap > 0
                                else summary.effective_minutes)
                quality = (eff_minutes / study_minutes) if study_minutes else 0.0

            result.append(HeatmapStats(
                label, study_minutes, eff_minutes,
                quality, summary.fatigue_adjusted_productivity,
                summary.sessions, distinct_nc, rc, rt_mins, dc,
                start_dt.date(), end_d,
                activity_total=rl_tot,
                again_count=ag_tot,
                cards_studied=dcs,
            ))
        return result

    def today_again_rate(self) -> float:
        """Actual again rate (ease=1) from today's revlog."""
        today = scheduler_today(_get_day_cutoff())
        rl = self._revlog_by_day(today, today)
        day_data = rl.get(today, {})
        total = activity_total(day_data)
        if total == 0:
            return 0.0
        return day_data.get("again_count", 0) / total

    def week_avg_again_rate(self) -> float:
        """Average again rate over past 7 days from revlog."""
        today = scheduler_today(_get_day_cutoff())
        start = today - timedelta(days=6)
        rl = self._revlog_by_day(start, today)
        total = sum(activity_total(v) for v in rl.values())
        again = sum(v.get("again_count", 0) for v in rl.values())
        return (again / total) if total else 0.0

    # ── private ───────────────────────────────────────────────────────────────

    def _revlog_by_day(self, start_day: date, end_day: date) -> dict[date, dict]:
        result: dict[date, dict] = {}
        if mw is None or mw.col is None:
            return result

        day_cutoff = _get_day_cutoff()
        today = scheduler_today(day_cutoff)
        start_ms, _ = day_bounds_ms(start_day, day_cutoff, today)
        _, end_ms   = day_bounds_ms(end_day, day_cutoff, today)

        # Cache key includes today's scheduler ordinal so the entry auto-expires at rollover
        try:
            today_ord = int(mw.col.sched.today)
        except Exception as exc:
            log.debug("sched.today unavailable, cache key uses 0: %s", exc)
            today_ord = 0
        cache_key = (today_ord, start_ms, end_ms)
        if cache_key in self._revlog_cache:
            return self._revlog_cache[cache_key]

        try:
            # cid added so we can count DISTINCT new cards per day (type=0 fires
            # once per learning step, not once per card; deduplicating by cid gives
            # "unique new cards introduced" rather than "total learning reps").
            # Same cid column also powers cards_reviewed below — "Cards Reviewed"
            # is the distinct-card companion to "Reviews" (reviews_count): a card
            # answered several times the same day (relearning cycles, repeated
            # filtered/cram-deck study) still only counts once here.
            rows = mw.col.db.all(
                "SELECT id, type, ease, time, cid FROM revlog WHERE id >= ? AND id < ?",
                start_ms, end_ms,
            )
            seen_new:    dict[date, set] = {}  # day → set of cids already counted as new
            seen_review: dict[date, set] = {}  # day → set of cids already counted as reviewed
            for row_id, row_type, row_ease, row_time, row_cid in rows:
                day = study_day_from_revlog_id(int(row_id), day_cutoff)
                if day not in result:
                    result[day] = {"new_cards": 0, "new_events": 0, "reviews_count": 0,
                                   "relearned": 0, "review_time_ms": 0,
                                   "again_count": 0, "hard_count": 0, "easy_count": 0,
                                   "cards_reviewed": 0}
                if row_type == 0:
                    # REVLOG_LRN — new_cards counts each card only once per day
                    # (distinct-card figure, unchanged). new_events is the raw,
                    # undeduplicated row count for the same type=0 rows -- used
                    # only by raw_study_events() below for the Reviews figure,
                    # so a card with several same-day learning steps still
                    # contributes once to new_cards but once per row here.
                    seen = seen_new.setdefault(day, set())
                    if row_cid not in seen:
                        seen.add(row_cid)
                        result[day]["new_cards"] += 1
                    result[day]["new_events"] += 1
                elif row_type == 1:
                    # REVLOG_REV — mature review
                    result[day]["reviews_count"] += 1
                    seen_r = seen_review.setdefault(day, set())
                    if row_cid not in seen_r:
                        seen_r.add(row_cid)
                        result[day]["cards_reviewed"] += 1
                elif row_type == 2:
                    # REVLOG_RELRN — lapsed card re-entering learning. Not
                    # folded into reviews_count itself (today_again_rate,
                    # week_avg_again_rate, and _nc_rc_rt above all already
                    # add relearned separately when they need a relearn-
                    # inclusive total — folding it in here too would double
                    # it for every one of those). It DOES count toward
                    # cards_reviewed though: a card that lapsed and got
                    # relearned today was still a card you reviewed today,
                    # and the display layer (heatmap_widget.py's "cards"
                    # value) already combines reviews_count + relearned for
                    # the same reason, so this keeps cards_reviewed's scope
                    # matching whatever "Reviews" actually displays.
                    result[day]["relearned"] += 1
                    seen_r = seen_review.setdefault(day, set())
                    if row_cid not in seen_r:
                        seen_r.add(row_cid)
                        result[day]["cards_reviewed"] += 1
                elif row_type == 3:
                    # REVLOG_FILTERED (cram/filtered deck) — real study, count as reviews.
                    result[day]["reviews_count"] += 1
                    seen_r = seen_review.setdefault(day, set())
                    if row_cid not in seen_r:
                        seen_r.add(row_cid)
                        result[day]["cards_reviewed"] += 1
                # type=4 (REVLOG_MANUAL / "set due date") is not actual study — intentionally ignored.
                ease = int(row_ease)
                if   ease == 1: result[day]["again_count"] += 1
                elif ease == 2: result[day]["hard_count"]  += 1
                elif ease == 4: result[day]["easy_count"]  += 1
                result[day]["review_time_ms"] += max(0, int(row_time))
        except Exception:
            log.exception("revlog query failed")
            return result

        self._revlog_cache[cache_key] = result
        return result

    def _distinct_cards_reviewed(self, start_day: date, end_day: date) -> int:
        """COUNT(DISTINCT cid) of review + relearning + filtered/cram revlog
        rows (type 1, 2, and 3) within [start_day, end_day] inclusive.

        Deliberately matches the *displayed* "Reviews" figure's scope, not
        HeatmapDay.reviews_count's raw scope (type 1+3 only) — the tab-level
        Reviews number (see _nc_rc_rt above) already folds relearned (type 2)
        in, and the day-popup's "Reviews" value does the same at the display
        layer in heatmap_widget.py (reviews_count + relearned), specifically
        so the two labels stay directly comparable — "Cards Reviewed" should
        count the distinct cards behind whatever "Reviews" says, not a
        narrower slice of it.

        This is the period-level ("Today"/"Weekly"/"Monthly"/... tabs, see
        stats() below) companion to HeatmapDay.cards_reviewed. It's a
        *separate* query rather than derived from _revlog_by_day's per-day
        breakdown on purpose: distinct-card counts don't sum across days
        (a card reviewed on both Monday and Tuesday would double-count if
        you added two daily distinct-counts together), so a period total
        needs its own COUNT(DISTINCT ...) over the whole range. Uses plain
        SQL aggregation rather than pulling every row into Python, per the
        same reasoning as the rest of this class's revlog access.
        """
        if mw is None or mw.col is None:
            return 0
        day_cutoff = _get_day_cutoff()
        today = scheduler_today(day_cutoff)
        start_ms, _ = day_bounds_ms(start_day, day_cutoff, today)
        _, end_ms   = day_bounds_ms(end_day, day_cutoff, today)
        try:
            today_ord = int(mw.col.sched.today)
        except Exception as exc:
            log.debug("sched.today unavailable, cache key uses 0: %s", exc)
            today_ord = 0
        cache_key = (today_ord, start_ms, end_ms)
        if cache_key in self._distinct_cards_cache:
            return self._distinct_cards_cache[cache_key]
        try:
            count = int(mw.col.db.scalar(
                "SELECT COUNT(DISTINCT cid) FROM revlog "
                "WHERE id >= ? AND id < ? AND type IN (1, 2, 3)",
                start_ms, end_ms,
            ) or 0)
        except Exception:
            log.exception("distinct cards reviewed query failed")
            return 0
        self._distinct_cards_cache[cache_key] = count
        return count

    def _distinct_new_cards(self, start_day: date, end_day: date) -> int:
        """COUNT(DISTINCT cid) of new-card/learning revlog rows (type 0)
        within [start_day, end_day] inclusive.

        Mirrors _distinct_cards_reviewed() immediately above -- same
        rationale: distinct-card counts don't sum across days (a card whose
        type=0 learning events span more than one calendar day, e.g. a
        multi-day learning step before graduation, would be double-counted
        if you added two daily distinct-counts together), so a period-level
        total needs its own COUNT(DISTINCT ...) over the whole range rather
        than summing HeatmapDay.new_cards across days as stats() and
        stats_for_year() previously did. HeatmapDay.new_cards itself is
        unaffected and remains correct for single-day use (day-popup,
        calendar-cell coloring).
        """
        if mw is None or mw.col is None:
            return 0
        day_cutoff = _get_day_cutoff()
        today = scheduler_today(day_cutoff)
        start_ms, _ = day_bounds_ms(start_day, day_cutoff, today)
        _, end_ms   = day_bounds_ms(end_day, day_cutoff, today)
        try:
            today_ord = int(mw.col.sched.today)
        except Exception as exc:
            log.debug("sched.today unavailable, cache key uses 0: %s", exc)
            today_ord = 0
        cache_key = (today_ord, start_ms, end_ms)
        if cache_key in self._distinct_new_cards_cache:
            return self._distinct_new_cards_cache[cache_key]
        try:
            count = int(mw.col.db.scalar(
                "SELECT COUNT(DISTINCT cid) FROM revlog "
                "WHERE id >= ? AND id < ? AND type = 0",
                start_ms, end_ms,
            ) or 0)
        except Exception:
            log.exception("distinct new cards query failed")
            return 0
        self._distinct_new_cards_cache[cache_key] = count
        return count

    def _distinct_cards_studied(self, start_day: date, end_day: date) -> int:
        """COUNT(DISTINCT cid) of ALL revlog rows (type 0, 1, 2, and 3 —
        new/learning, review, relearning, and filtered/cram) within
        [start_day, end_day] inclusive.

        This is the all-inclusive companion to _distinct_cards_reviewed()
        immediately above, which deliberately restricts to type IN (1,2,3)
        to mirror the "Reviews" stat. That narrower scope makes
        _distinct_cards_reviewed() the wrong source for an honest "cards
        studied today" figure — on any day involving new cards it would
        undercount, since brand-new cards (type=0) are excluded there by
        design. This method exists specifically to back the deck-browser
        "Daily Status" line (see heatmap_widget.py), without altering
        _distinct_cards_reviewed()'s existing meaning or any of its
        existing callers (e.g. the "Cards Reviewed" stat card).

        Same rationale as its siblings for being a standalone COUNT(DISTINCT
        ...) query rather than summed from _revlog_by_day's per-day
        breakdown: distinct-card counts don't sum across days without
        double-counting a card studied on more than one day in the range.
        """
        if mw is None or mw.col is None:
            return 0
        day_cutoff = _get_day_cutoff()
        today = scheduler_today(day_cutoff)
        start_ms, _ = day_bounds_ms(start_day, day_cutoff, today)
        _, end_ms   = day_bounds_ms(end_day, day_cutoff, today)
        try:
            today_ord = int(mw.col.sched.today)
        except Exception as exc:
            log.debug("sched.today unavailable, cache key uses 0: %s", exc)
            today_ord = 0
        cache_key = (today_ord, start_ms, end_ms)
        if cache_key in self._distinct_cards_studied_cache:
            return self._distinct_cards_studied_cache[cache_key]
        try:
            count = int(mw.col.db.scalar(
                "SELECT COUNT(DISTINCT cid) FROM revlog "
                "WHERE id >= ? AND id < ? AND type IN (0, 1, 2, 3)",
                start_ms, end_ms,
            ) or 0)
        except Exception:
            log.exception("distinct cards studied query failed")
            return 0
        self._distinct_cards_studied_cache[cache_key] = count
        return count

    def _future_due_counts(self, days: int) -> dict[date, int]:
        result: dict[date, int] = {}
        if mw is None or mw.col is None:
            return result
        try:
            today      = scheduler_today(_get_day_cutoff())
            today_ord  = mw.col.sched.today
            cutoff_ord = today_ord + days
            rows = mw.col.db.all(
                "SELECT due, COUNT(*) FROM cards "
                "WHERE queue IN (2, 3) AND due > ? AND due <= ? GROUP BY due",
                today_ord, cutoff_ord,
            )
            for due_ord, count in rows:
                offset = int(due_ord) - today_ord
                result[today + timedelta(days=offset)] = result.get(today + timedelta(days=offset), 0) + int(count)
            today_ts  = int(datetime.combine(today, datetime.min.time()).timestamp())
            cutoff_ts = today_ts + days * 86400
            lrn_rows = mw.col.db.all(
                "SELECT due, COUNT(*) FROM cards "
                "WHERE queue = 1 AND due > ? AND due <= ? GROUP BY (due / 86400)",
                today_ts, cutoff_ts,
            )
            for due_ts, count in lrn_rows:
                day = date.fromtimestamp(int(due_ts))
                result[day] = result.get(day, 0) + int(count)
        except Exception:
            log.exception("future due query failed")
        return result

    def days_for_year(self, year: int, future_count: int = 30,
                     measurement: dict | None = None,
                     end_conditions: dict | None = None) -> list[HeatmapDay]:
        """Return all days for a calendar year (Jan 1 – Dec 31).
        If year is the current year, appends future_count forecast days."""
        from datetime import date as _date, timedelta as _td
        day_cutoff = _get_day_cutoff()
        today = scheduler_today(day_cutoff)
        start = _date(year, 1, 1)
        # End = Dec 31, but cap at today for current year
        end_past = _date(year, 12, 31)
        if end_past > today:
            end_past = today

        # Session data
        start_ts, _ = day_bounds_ms(start, day_cutoff, today)
        _, end_ts   = day_bounds_ms(end_past, day_cutoff, today)
        start_ts //= 1000
        end_ts //= 1000
        sessions = self.repo.sessions_between(start_ts, end_ts)
        buckets: dict[_date, list] = {}
        for s in sessions:
            d = study_day_from_revlog_id(int(s.start_time) * 1000, day_cutoff)
            buckets.setdefault(d, []).append(s)

        revlog = self._revlog_by_day(start, end_past)

        # Goal-reached logic mirrors days() — needed to populate is_goal_reached
        ec = end_conditions or {}
        has_goal = any([
            ec.get("cards_enabled"),
            ec.get("sessions_enabled"),
            ec.get("max_time_enabled"),
            ec.get("all_due_enabled"),
        ])

        result: list[HeatmapDay] = []
        current = start
        while current <= end_past:
            items  = buckets.get(current, [])
            rl     = revlog.get(current, {})
            mins   = sum(i.duration for i in items) / 60.0
            meas   = measurement or {}
            qsrc   = meas.get("quality_source", "focus")
            rl_tot = activity_total(rl)
            if qsrc == "answers" and rl_tot > 0:
                _ag = rl.get("again_count", 0)
                _hd = rl.get("hard_count",  0)
                _ez = rl.get("easy_count",  0)
                _gd = max(0, rl_tot - _ag - _hd - _ez)
                quality = _answer_quality(_ag, _hd, _gd, _ez,
                                         meas.get("ans_weights", {}))
                eff = mins * quality
            elif qsrc == "retention" and rl_tot > 0:
                quality = max(0.0, 1.0 - rl.get("again_count", 0) / rl_tot)
                eff = mins * quality
            else:  # "focus" (default: fatigue-weighted effective time)
                cap = int(meas.get("eff_time_cap_secs", 90))
                eff = (_effective_with_cap(items, cap) if cap > 0
                       else sum(effective_minutes(i.duration, i.effective_score)
                                for i in items))
                quality = (eff / mins) if mins else 0.0
            if not items:
                is_goal_reached = False
            elif not has_goal:
                is_goal_reached = True
            else:
                is_goal_reached = (
                    current in self._goal_dates
                    or self._day_met_goal(items, ec)
                )
            # For today, compute remaining due cards (review + learning + new).
            # Past days always stay 0 — only today has a meaningful "remaining" count.
            #
            # BUG FIX: this used to call mw.col.sched.reset() before
            # sched.counts() to force an accurate read outside an active
            # review session. reset() is a mutating scheduler call — it can
            # reshuffle/reorder the live review queue, which is risky to
            # trigger as a side effect of simply rendering the heatmap panel
            # (e.g. a deck-browser refresh firing while a review session is
            # already in progress). days() above solves the exact same
            # "sched.counts() returns (0,0,0) outside a review session"
            # problem via direct, non-mutating SQL for review/learning cards,
            # falling back to sched.counts() only for new cards (which respects
            # per-day limits and can't be gotten accurately any other way).
            # Reuse that same approach here instead.
            _due_today = 0
            if current == today and mw and mw.col:
                try:
                    import time as _t
                    _today_ord = mw.col.sched.today
                    _now_ts    = int(_t.time())

                    _rev_due = int(mw.col.db.scalar(
                        "SELECT count() FROM cards WHERE queue = 2 AND due <= ?",
                        _today_ord) or 0)

                    _lrn_due = int(mw.col.db.scalar(
                        "SELECT count() FROM cards "
                        "WHERE (queue = 1 AND due <= ?) "
                        "   OR (queue = 3 AND due <= ?)",
                        _now_ts, _today_ord) or 0)

                    try:
                        _cnt = mw.col.sched.counts()
                        _new_due = int(_cnt[0]) if _cnt and sum(_cnt) > 0 else 0
                    except Exception:
                        _new_due = 0
                    if _new_due == 0:
                        try:
                            _new_due = int(mw.col.db.scalar(
                                "SELECT count() FROM cards WHERE queue = 0") or 0)
                        except Exception:
                            _new_due = 0

                    _due_today = _new_due + _lrn_due + _rev_due
                except Exception:
                    _due_today = 0
            result.append(HeatmapDay(
                day=current, minutes=mins, effective_minutes=eff,
                quality=quality, sessions=len(items),
                is_future=False, due_cards=_due_today,
                new_cards=rl.get("new_cards", 0),
                new_events=rl.get("new_events", 0),
                reviews_count=rl.get("reviews_count", 0),
                relearned=rl.get("relearned", 0),
                review_time_seconds=rl.get("review_time_ms", 0) // 1000,
                again_count=rl.get("again_count", 0),
                hard_count=rl.get("hard_count", 0),
                easy_count=rl.get("easy_count", 0),
                is_goal_reached=is_goal_reached,
                cards_reviewed=rl.get("cards_reviewed", 0),
            ))
            current += _td(days=1)

        # Remaining days of current year as empty past placeholders
        if year == today.year:
            current = today + _td(days=1)
            end_year = _date(year, 12, 31)
            future_due = self._future_due_counts(future_count)
            while current <= end_year:
                due = future_due.get(current, 0)
                result.append(HeatmapDay(
                    day=current, minutes=0.0, effective_minutes=0.0,
                    quality=0.0, sessions=0, is_future=True,
                    due_cards=due,
                ))
                current += _td(days=1)
        else:
            # Future year — all forecast
            if year > today.year:
                # BUG FIX: this was hardcoded to 365 regardless of what the
                # caller asked for, so even with the new "show all future
                # due cards" option turned on (future_count set far higher
                # — see _due_forecast_window() in __init__.py), navigating
                # more than a year out from today still showed a
                # completely blank grid, since the query never looked that
                # far ahead in the first place. Using future_count here
                # too — same as the "year == today.year" branch just
                # above — means "unlimited" actually reaches as far as it
                # promises, while the default/limited setting still
                # correctly shows next-to-nothing for a year that far out
                # (which is the whole point of a *limited* forecast).
                future_due = self._future_due_counts(future_count)
                current = start
                while current <= _date(year, 12, 31):
                    due = future_due.get(current, 0)
                    result.append(HeatmapDay(
                        day=current, minutes=0.0, effective_minutes=0.0,
                        quality=0.0, sessions=0, is_future=True,
                        due_cards=due,
                    ))
                    current += _td(days=1)
            else:
                # Past year — fill remaining days as empty
                current = end_past + _td(days=1)
                while current <= _date(year, 12, 31):
                    result.append(HeatmapDay(
                        day=current, minutes=0.0, effective_minutes=0.0,
                        quality=0.0, sessions=0,
                    ))
                    current += _td(days=1)

        return result

    def sessions_today_count(self) -> int:
        """Count of FocusFlow sessions completed today."""
        day_cutoff = _get_day_cutoff()
        today = scheduler_today(day_cutoff)
        start_ts, _ = day_bounds_ms(today, day_cutoff, today)
        return self.repo.count_since(start_ts // 1000)

    def cards_today(self) -> int:
        """Total study activity today (types 0+1+2+3) from the revlog.

        Replaces direct callers of the private ``_revlog_by_day`` so the cache
        and query logic stay encapsulated inside HeatmapService.
        """
        today = scheduler_today(_get_day_cutoff())
        rl = self._revlog_by_day(today, today)
        dd = rl.get(today, {})
        return activity_total(dd)

    def stats_for_year(self, year: int) -> dict:
        """Compute study stats for a specific calendar year."""
        from datetime import date as _date
        day_cutoff = _get_day_cutoff()
        today   = scheduler_today(day_cutoff)
        start_d = _date(year, 1, 1)
        end_d   = min(_date(year, 12, 31), today)
        start_ts, _ = day_bounds_ms(start_d, day_cutoff, today)
        _, end_ts   = day_bounds_ms(end_d, day_cutoff, today)
        start_ts //= 1000
        end_ts //= 1000
        sessions = [s for s in self._get_all_sessions()
                    if start_ts <= s.start_time < end_ts]
        summary  = summarize_sessions(sessions)
        rl = self._revlog_by_day(start_d, end_d)
        return {
            "minutes":   round(summary.study_minutes, 1),
            "effective": round(summary.effective_minutes, 1),
            "quality":   summary.average_quality,
            "sessions":  summary.sessions,
            "new_cards": self._distinct_new_cards(start_d, end_d),
            "reviews":   sum(raw_study_events(v) for v in rl.values()),  # raw types 0+1+2+3
            "cards_reviewed": self._distinct_cards_reviewed(start_d, end_d),
        }

    def goal_streak(self, end_conditions: dict, force_today: bool = False) -> tuple[int, int]:
        """
        Returns (current_streak, longest_streak) based on goal completion.

        A day qualifies if:
          - No goal is configured: any day with study sessions counts.
          - A goal is configured: the day is in the stored goal_dates table
            (written when _check_goal_reached fires) OR it can be computed
            from session data (backward compatibility for existing history).

        `all_due_enabled` goals are only captured via stored dates because
        the "all cards finished" state cannot be reconstructed from the revlog.

        `force_today` -- pass True when the caller has just confirmed the goal
        was reached today.  This guarantees today counts even if the DB write
        or refresh_goal_days() has not propagated yet (e.g. a commit/GC race).
        """
        day_cutoff = _get_day_cutoff()
        today = scheduler_today(day_cutoff)
        all_s = self._get_all_sessions()

        has_goal = any([
            end_conditions.get("cards_enabled"),
            end_conditions.get("sessions_enabled"),
            end_conditions.get("max_time_enabled"),
            end_conditions.get("all_due_enabled"),
        ])

        # Group sessions by date
        day_map: dict[date, list] = {}
        for s in all_s:
            d = study_day_from_revlog_id(int(s.start_time) * 1000, day_cutoff)
            day_map.setdefault(d, []).append(s)

        qual_days: set[date] = set()
        if not has_goal:
            # No goal configured — any day with FocusFlow sessions counts
            qual_days = {d for d, items in day_map.items() if items}
        else:
            # Stored dates are ground truth: written by mark_goal_reached()
            # when FocusFlow detects the user hit their configured target.
            qual_days = set(self._goal_dates)

            # Backward-compat: sessions recorded before the goal-dates table
            # existed can still satisfy the goal conditions directly.
            for d, items in day_map.items():
                if d not in qual_days and self._day_met_goal(items, end_conditions):
                    qual_days.add(d)

            # Anki revlog is intentionally NOT consulted here: reviewing cards
            # outside a FocusFlow session does not count toward the goal streak.

        # force_today: caller confirmed the goal was just reached this session —
        # include today even if the DB write hasn't propagated yet.
        if force_today:
            qual_days.add(today)

        if not qual_days:
            return 0, 0

        # BUG FIX: this used to start the backward count at `today` — so if
        # today's goal simply hasn't been finished YET (it's midday, due
        # cards remain, etc.), `today in qual_days` is False on the very
        # first check and the whole streak instantly shows as 0, discarding
        # any number of genuinely-completed consecutive prior days. The
        # user still has until midnight to finish today and keep the streak
        # alive, so today shouldn't get to break it before the day is even
        # over. If today's goal IS already met, count from today as before
        # (extending the streak). Otherwise, count from yesterday instead —
        # showing the streak exactly as it stood at the start of today.
        # Only a genuinely missed PRIOR day (yesterday also not in
        # qual_days) now correctly drops it to 0; an in-progress today does
        # not.
        current = 0
        check = today if today in qual_days else today - timedelta(days=1)
        while check in qual_days:
            current += 1
            check -= timedelta(days=1)

        sorted_days = sorted(qual_days)
        longest = run = 0
        for i, d in enumerate(sorted_days):
            if i == 0 or (d - sorted_days[i - 1]).days == 1:
                run += 1
                longest = max(longest, run)
            else:
                run = 1
        longest = max(longest, run)

        return current, longest

    @staticmethod
    def _day_met_goal(sessions: list, ec: dict) -> bool:
        """Return True if the FocusFlow session data for a day meets the goal.

        Uses only FocusFlow session records — cards answered during a timer,
        completed sessions, and timed study duration.  Anki revlog is not
        consulted: reviewing cards outside FocusFlow does not count.

        This method is the backward-compat path for history that was recorded
        before the goal-dates table existed.  Going forward, mark_goal_reached()
        is the primary source of truth.
        """
        cards_done = sum(s.cards_done for s in sessions)
        duration   = sum(s.duration   for s in sessions)
        if ec.get("cards_enabled") and cards_done >= int(ec.get("cards_target", 25)):
            return True
        if ec.get("sessions_enabled") and len(sessions) >= int(ec.get("sessions_target", 4)):
            return True
        if ec.get("max_time_enabled"):
            max_s = int(float(ec.get("max_time_minutes", 25)) * 60)
            if duration >= max_s:
                return True
        return False

    def all_year_stats(self, years: list[int]) -> dict[int, dict]:
        """Return stats_for_year for each year in *years*, with per-day caching.

        The cache key includes today's scheduler date so entries auto-expire
        at rollover (not calendar midnight), matching the revlog cache's
        self-invalidation strategy.  Explicit invalidation (e.g. after a
        card answer) is handled by invalidate_cache().
        """
        cache_key = (tuple(sorted(years)), scheduler_today(_get_day_cutoff()))
        if cache_key in self._year_stats_cache:
            return self._year_stats_cache[cache_key]
        result = {yr: self.stats_for_year(yr) for yr in years}
        self._year_stats_cache[cache_key] = result
        return result
