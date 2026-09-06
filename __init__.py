"""FocusFlow v41 — PyQt6/Qt6, Anki 25.x

SPDX-License-Identifier: MIT
Copyright (c) 2026 Adel Aitah
See the LICENSE file at the root of this add-on for the full license text.

__init__.py responsibilities (after code-quality refactor):
  • Import and wire Anki hooks
  • Bootstrap services on profile open / close
  • Thin command stubs that delegate to SessionCoordinator
  • Settings / config application

Session state, goal checking, popup orchestration, break management, and the
timer signal handlers all live in core/session_coordinator.py (issue #13).
_on_session_completed has been split into _compute_report + _show_popup there
(issue #14).  All print() calls replaced with the centralized logger (issue #15).

Code-quality improvements applied (v41 patch):
  • PyQt6 ApplicationState enum fix — use .value, not int() (#bugfix)
  • _current_mode / _editor_open moved into SessionCoordinator (#2)
  • Toolbar injected via coordinator.set_toolbar(), not _coordinator._toolbar (#2)
  • Double resume("not_reviewing") in _on_card_shown fixed (#7)
  • _cmd_open_settings documents and handles heatmap_service=None (#8)
  • ConfigManager defers mw import to avoid early-load crash (#1)
  • config.json ships with color_mode key to match schema (#5)
  • _normalize_config warns on unknown top-level keys and profile renames (#5)
  • timer_manager: dead _elapsed_before_pause field removed (#4)
  • timer_manager: stop_idle uses 0 for _total_seconds sentinel (#4)
  • fatigue_tracker: baseline/recent windows guaranteed non-overlapping (#3)
  • fatigue_tracker: on_break_taken blends pre-break RTs toward median (#3)
  • fatigue_tracker: SD floor comment expanded; session_again_rate documented (#3)
  • Tests: vacuous conditional assertion fixed; time.sleep replaced with
    monkeypatched FakeClock for deterministic CI runs (#6)
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from aqt import gui_hooks, mw, dialogs
from aqt.qt import QAction, QMessageBox, QTimer

from .core.fatigue_tracker import FatigueTracker
from .core.metrics import StudySession
from .core.session_coordinator import SessionCoordinator
from .core.timer_manager import TimerMode, TimerManager
from .data.session_repo import SessionRepo
from .services.heatmap_service import (
    HeatmapService, scheduler_today, day_bounds_ms, _get_day_cutoff,
)
from .services.session_service import SessionService
from .ui.heatmap_widget import (
    HeatmapDialog, build_heatmap_html, _compute_streaks, render_single_year_svg,
)
from .ui.popups import ManualSessionDialog, ExperienceWizardDialog, NoteReminderDialog
from .ui.settings_dialog import SettingsDialog
from .ui.ui_toolbar import FocusFlowToolbar
from .utils.config_manager import ConfigManager, strip_lone_surrogates
from .utils.logger import log
from .utils.sound_player import SoundPlayer

_ADDON_DIR   = Path(__file__).parent
_MODULE_NAME = __name__

# Locale-independent weekday/month abbreviations for note & reminder labels.
#
# BUG FIX: date_label used to be built with day.strftime("%a, ") / ("%b %Y").
# strftime's %a/%b are locale-dependent — on a machine whose Windows display
# language/region isn't English (e.g. Danish), Python picks up the OS locale
# for time formatting and %a/%b silently render in that language, with a
# different abbreviation length and casing, e.g. "Thu, 16 Jul 2026" became
# "to, 16 jul 2026" (Danish "torsdag" abbreviates to "to", "juli" to "jul",
# both lower-case). FocusFlow's UI is English-only, so we spell these out
# ourselves instead of trusting the OS locale.
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _fmt_note_date(d: date) -> str:
    """Locale-independent 'Thu, 16 Jul 2026' style label."""
    return f"{_WEEKDAY_ABBR[d.weekday()]}, {d.day} {_MONTH_ABBR[d.month - 1]} {d.year}"

# Clear stale .pyc caches to prevent WinError-32 partial-install corruption
try:
    import shutil as _sh
    for _pc in _ADDON_DIR.rglob("__pycache__"): _sh.rmtree(_pc, ignore_errors=True)
    del _sh, _pc
except Exception:
    pass

# ── service globals ───────────────────────────────────────────────────────────

_config_mgr:   Optional[ConfigManager]      = None
_timer_mgr:    Optional[TimerManager]       = None
_fatigue:      Optional[FatigueTracker]     = None
_session_svc:  Optional[SessionService]     = None
_heatmap_svc:  Optional[HeatmapService]     = None
_sound:        Optional[SoundPlayer]        = None
_toolbar:      Optional[FocusFlowToolbar]   = None
_coordinator:  Optional[SessionCoordinator] = None

# _current_mode and _editor_open moved into SessionCoordinator (improvement #2)


# ── bootstrap ─────────────────────────────────────────────────────────────────

def _init() -> None:
    global _config_mgr, _timer_mgr, _fatigue, _sound, _coordinator

    _config_mgr  = ConfigManager(module_name=_MODULE_NAME, addon_dir=_ADDON_DIR)
    profile      = _config_mgr.data
    _timer_mgr   = TimerManager(profile)
    _fatigue     = FatigueTracker(profile)
    _sound       = SoundPlayer(_ADDON_DIR / "assets" / "ding.wav", profile)
    _coordinator = SessionCoordinator(_config_mgr, _timer_mgr, _fatigue, _sound)

    # Wire timer signals to coordinator handlers (issue #13)
    _timer_mgr.tick.connect(_coordinator.on_tick)
    _timer_mgr.state_changed.connect(_coordinator.on_state_changed)
    _timer_mgr.session_completed.connect(_coordinator.on_session_completed)
    _timer_mgr.break_completed.connect(_coordinator.on_break_completed)

    # Pause awareness: exclude idle wall-clock time from response-time
    # measurements so a manual pause does not trigger a spurious fatigue
    # alert on the first card answered after the timer resumes.
    _timer_mgr.paused.connect(_fatigue.on_timer_paused)
    _timer_mgr.resumed.connect(_fatigue.on_timer_resumed)

    gui_hooks.profile_did_open.append(_on_profile_open)
    gui_hooks.profile_will_close.append(_on_profile_will_close)
    gui_hooks.main_window_did_init.append(_on_main_window_init)
    gui_hooks.reviewer_did_show_question.append(_on_card_shown)
    gui_hooks.reviewer_did_answer_card.append(_on_card_answered)
    gui_hooks.reviewer_will_end.append(_on_reviewer_will_end)
    gui_hooks.editor_did_init.append(_on_editor_did_init)
    gui_hooks.editor_did_load_note.append(_on_editor_did_load_note)
    gui_hooks.state_did_change.append(_on_anki_state_change)
    gui_hooks.deck_browser_will_render_content.append(_on_deck_browser_render)
    gui_hooks.theme_did_change.append(_on_theme_changed)
    gui_hooks.webview_did_receive_js_message.append(_on_webview_message)
    gui_hooks.operation_did_execute.append(_on_operation_executed)

    # Auto-pause when the Anki window loses focus so idle wall-clock time is
    # not counted toward the study session.  This keeps time-tracking honest
    # and prevents the fatigue score from being diluted by away-from-desk time.
    try:
        from aqt.qt import QApplication
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(_on_application_state_changed)
    except Exception as exc:
        log.debug("applicationStateChanged hook unavailable: %s", exc)


def _on_profile_open() -> None:
    global _session_svc, _heatmap_svc
    try:
        repo         = SessionRepo()
        _session_svc = SessionService()
        _session_svc.ensure_ready()
        _heatmap_svc = HeatmapService(repo)

        # Restore today's counters from the DB so they survive Anki restarts.
        session_count = _heatmap_svc.sessions_today_count()

        cards_today = 0
        try:
            cards_today = _heatmap_svc.cards_today()
        except Exception as exc:
            log.error("cards_today restore failed: %s", exc)

        secs_today = 0
        try:
            _day_cutoff = _get_day_cutoff()
            _today = scheduler_today(_day_cutoff)
            today_start_ms, today_end_ms = day_bounds_ms(_today, _day_cutoff, _today)
            today_sesh  = repo.sessions_between(today_start_ms // 1000, today_end_ms // 1000)
            secs_today  = sum(int(s.duration) for s in today_sesh)
        except Exception as exc:
            log.error("secs_today restore failed: %s", exc)

        if _coordinator:
            _coordinator.initialize_services(_session_svc, _heatmap_svc, _toolbar)
            _coordinator.reset_daily_state(session_count, cards_today, secs_today)

        if _toolbar and _config_mgr:
            _indicator = _config_mgr.data.get("fatigue", {}).get("indicator", "dot")
            _toolbar.set_indicator_mode(_indicator)
        if mw and mw.deckBrowser:
            mw.deckBrowser.refresh()

        if _config_mgr and not _config_mgr.wizard_completed():
            QTimer.singleShot(0, _maybe_show_experience_wizard)
        else:
            QTimer.singleShot(200, _maybe_show_day_reminder)

    except Exception:
        log.exception("profile_did_open failed")


def _maybe_show_day_reminder() -> None:
    """Outlook-style reminder: for each note, work out the date it should
    actually remind on (the event date itself, or 1 day / week / month
    before it) and show any that land on today. Runs once when Anki opens
    — if you reopen Anki the same day, due reminders will show again, same
    as any calendar app would on a fresh launch — UNLESS the user explicitly
    dismissed that reminder earlier today, in which case it stays quiet
    until its next occurrence (see "Dismiss" vs "Remind Me Later" below)."""
    if mw is None or _config_mgr is None:
        return
    try:
        from datetime import date as _date, timedelta as _timedelta
        today = _date.today()
        today_str = today.isoformat()
        hm_cfg = _config_mgr.heatmap_config()
        notes = hm_cfg.get("day_notes") or {}

        dismissed = hm_cfg.get("dismissed_reminders") or {}
        dismissed_events: set[str] = set()
        if dismissed.get("date") == today_str:
            dismissed_events = set(dismissed.get("events") or [])

        due: list[tuple[_date, str]] = []
        for date_str, entry in notes.items():
            try:
                event_day = _date.fromisoformat(date_str)
            except ValueError:
                continue
            if isinstance(entry, dict):
                text   = strip_lone_surrogates(str(entry.get("text", "")))
                offset = str(entry.get("remind_offset", "same"))
            else:
                text, offset = strip_lone_surrogates(str(entry or "")), "same"
            if not text:
                continue
            if offset == "day_before":
                remind_on = event_day - _timedelta(days=1)
            elif offset == "week_before":
                remind_on = event_day - _timedelta(weeks=1)
            elif offset == "month_before":
                # Simple 30-day approximation — a real calendar-month
                # subtraction needs extra edge-case handling (Jan 31 -> Feb
                # 31 doesn't exist) that isn't worth it for a reminder nudge.
                remind_on = event_day - _timedelta(days=30)
            else:
                remind_on = event_day
            if remind_on == today and date_str not in dismissed_events:
                due.append((event_day, text))
        if not due:
            return
        due.sort()
        lines = [f"\U0001F4CC {_fmt_note_date(d)}: {t}" for d, t in due]

        box = QMessageBox(mw)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(
            "FocusFlow reminder" if len(due) == 1 else "FocusFlow reminders"
        )
        box.setText("\n\n".join(lines))
        # "Remind Me Later" (default) leaves the reminder untouched, so it
        # naturally reappears next time Anki opens today, same as before.
        # "Dismiss" is the new option: it silences these specific reminders
        # for the rest of today without deleting the underlying notes —
        # they'll show again on their next real occurrence.
        later_btn    = box.addButton("Remind Me Later", QMessageBox.ButtonRole.RejectRole)
        dismiss_btn  = box.addButton("Dismiss", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(later_btn)
        box.exec()
        if box.clickedButton() is dismiss_btn:
            try:
                newly_dismissed = dismissed_events | {d.isoformat() for d, _ in due}
                _config_mgr.update_heatmap_config({
                    "dismissed_reminders": {
                        "date": today_str,
                        "events": sorted(newly_dismissed),
                    }
                })
                _config_mgr.save()
            except Exception as exc:
                log.error("dismissed_reminders save failed: %s", exc)
    except Exception:
        log.exception("day reminder failed")


def _maybe_show_experience_wizard() -> None:
    """First-run only: let the user pick Basic / Advanced / Expert before
    they ever see the full Settings dialog. Never shown again once a choice
    is recorded — the Experience dropdown in Settings takes over from there."""
    if mw is None or _config_mgr is None or _config_mgr.wizard_completed():
        return
    try:
        dlg = ExperienceWizardDialog(parent=mw)
        dlg.exec()  # closing any way (Get Started or the window X) picks the current selection
        _config_mgr.mark_wizard_completed(dlg.chosen_mode())
        _apply_config()
    except Exception:
        log.exception("experience wizard failed")


def _on_profile_will_close() -> None:
    if _session_svc and _session_svc.has_active_session:
        score = _fatigue.current_score if _fatigue else 1.0
        duration_seconds = None
        if _timer_mgr and _timer_mgr.mode == TimerMode.STUDY:
            duration_seconds = _timer_mgr.snapshot().elapsed_seconds
        _session_svc.finish_session(
            effective_score=score,
            duration_seconds=duration_seconds,
        )
    if _timer_mgr:
        _timer_mgr.stop_idle()


# ── main window ───────────────────────────────────────────────────────────────

def _on_main_window_init() -> None:
    global _toolbar
    if mw is None:
        return
    _inject_menu()
    profile  = _config_mgr.data  # type: ignore[union-attr]
    _toolbar = FocusFlowToolbar(profile)
    _toolbar.play_pause_requested.connect(_cmd_play_pause)
    _toolbar.reset_requested.connect(_cmd_reset_timer)
    _toolbar.skip_requested.connect(_coordinator.cmd_skip_break if _coordinator else lambda: None)
    _toolbar.break_requested.connect(
        _coordinator.start_break_manual if _coordinator else lambda is_long: None)
    _toolbar.profile_switch_requested.connect(_cmd_switch_profile)
    _toolbar.collapse_changed.connect(_on_collapse_changed)
    _toolbar.position_changed.connect(_on_position_changed)
    _sync_toolbar_profiles()
    _toolbar.set_idle()
    if _coordinator:
        # Use the public accessor so toolbar is registered through the same
        # path as profile-open injection — no direct attribute poking.
        _coordinator.set_toolbar(_toolbar)
    _toolbar.set_mode(_coordinator.current_mode if _coordinator else "Studying")


def _inject_menu() -> None:
    if mw is None:
        return
    tools = mw.form.menuTools
    for act in tools.actions():
        if act.objectName() == "ff_settings_action":
            return
    tools.addSeparator()
    a1 = QAction("FocusFlow", mw)
    a1.setObjectName("ff_settings_action")
    a1.triggered.connect(_cmd_open_settings)
    tools.addAction(a1)


# ── deck browser heatmap ──────────────────────────────────────────────────────

# Anki's own scheduler never pushes a card's next review further out than
# this (its maximum interval is 100 years); using the same number for
# "unlimited" means the forecast query is genuinely exhaustive rather than
# an arbitrary large-but-still-technically-capped guess.
_MAX_SCHEDULER_INTERVAL_DAYS = 36500


def _due_forecast_window(hm_cfg: dict) -> int:
    """How many days ahead of today to compute due-card forecasts for —
    reads the "due_forecast_days" / "due_forecast_unlimited" heatmap
    settings (see the Heatmap settings tab). Shared by both the eager
    current-year render (_build_days_by_year below) and the lazy per-year
    fetch (_on_webview_message's "ff_get_year:" handler), so turning
    "unlimited" on covers a year you've already navigated to as well as
    the one that's current right now."""
    if hm_cfg.get("due_forecast_unlimited", False):
        return _MAX_SCHEDULER_INTERVAL_DAYS
    return int(hm_cfg.get("due_forecast_days", 30) or 30)


def _build_days_by_year(past_years: int = 2,
                        measurement: dict | None = None,
                        end_conditions: dict | None = None,
                        full_history: bool = False,
                        due_forecast_days: int = 30) -> dict[int, list]:
    if _heatmap_svc is None:
        return {}
    today    = scheduler_today(_get_day_cutoff())
    cur_year = today.year

    # Determine which years have session data with a single indexed DB read.
    # Also compute the earliest year so users who have data older than
    # `past_years` can still navigate to it in the heatmap (when
    # full_history is on).
    try:
        years_with_data = _heatmap_svc.repo.distinct_years()
    except Exception as exc:
        log.warning("distinct_years failed, defaulting to current year only: %s", exc)
        years_with_data = {cur_year}

    first_data_year = min(years_with_data) if years_with_data else cur_year
    # PERFORMANCE FIX: this used to always reach back to first_data_year —
    # for anyone with more than `past_years` of history (i.e. almost any
    # established user), that meant EVERY year since their first-ever
    # review got a full SVG grid built and injected into the deck browser
    # panel, hidden via display:none for all but the current year, on
    # *every single refresh*. For a multi-year user that's megabytes of
    # markup regenerated constantly for years nobody's looking at 99% of
    # the time. Default now caps at `past_years` back from today; the full
    # range is still available by turning on "Show full history in heatmap
    # navigation" in Settings, for anyone who specifically wants deep
    # multi-year browsing and is fine trading some lightness for it.
    if full_history:
        start_year = min(first_data_year, cur_year - past_years)
    else:
        start_year = cur_year - past_years

    result: dict[int, list] = {}
    # BUG FIX: this used to stop at cur_year, so last_year in
    # build_heatmap_html() (heatmap_widget.py) was always exactly the
    # current year — the ▶ "next" button was disabled the moment the panel
    # loaded, with no way to navigate forward at all. That made the
    # due-forecast work above pointless for anyone wanting to check next
    # year specifically (e.g. "how much do I have due in 2027"), since
    # there was no way to ever get there. Always including one year ahead
    # here — same lazy-placeholder treatment as every past year already
    # gets, fetched on demand via "ff_get_year:" the first time ▶ is
    # actually clicked — costs nothing until that click happens, regardless
    # of whether the forecast setting below is the default 30 days or
    # "unlimited".
    for yr in range(start_year, cur_year + 2):
        # Always include every year in the range as a key — even years with
        # no FocusFlow session data — so the ◀/▶ navigation buttons in the
        # heatmap are never permanently disabled and know the correct range.
        # PERFORMANCE FIX: only the CURRENT year's day-list is built here now.
        # Every other year used to be built eagerly too (full revlog query +
        # per-day loop), even though the resulting SVG grid was hidden via
        # display:none 99% of the time — the same waste the full_history fix
        # above targets, just also happening in the *default* 3-year window.
        # Other years are now built on demand, the first time the user
        # actually clicks to one, via _on_webview_message's "ff_get_year:"
        # handler (which calls this same days_for_year()) and the updated
        # ffNav() JS in heatmap_widget.py. An empty list here is exactly
        # what build_heatmap_html() already treats as "skip this year for
        # now" — see its "if not days: continue" guard.
        if yr != cur_year:
            result[yr] = []
            continue
        try:
            result[yr] = _heatmap_svc.days_for_year(
                yr,
                # BUG FIX: was hardcoded to 30 with no way to see further
                # out — cards due more than a month ahead just showed as
                # plain uncoloured cells with nothing to click, even though
                # the data to forecast them further was already available.
                # Now reads the user-configurable "due_forecast_days"
                # heatmap setting (still defaults to 30).
                future_count=due_forecast_days,
                measurement=measurement,
                end_conditions=end_conditions,
            )
        except Exception as exc:
            log.warning("days_for_year(%d) failed: %s", yr, exc)
            result[yr] = []
    return result


def _year_render_config() -> tuple[dict, dict | None]:
    """The subset of _on_deck_browser_render()'s setup that a single lazily-
    fetched year (see _on_webview_message's "ff_get_year:" handler) also
    needs — factored out so the two can't drift out of sync."""
    hm_cfg = _config_mgr.heatmap_config() if _config_mgr else {}
    meas_cfg = None
    if _config_mgr:
        try:
            meas_cfg = _config_mgr.data.get("measurement")
        except Exception:
            pass
    return hm_cfg, meas_cfg


# Registry of clickable statistics: maps a "kind" (sent from JS — see
# window._ffClickableStats in heatmap_widget.py, and the popup rows'
# onclick wiring) to the Anki search term that finds the matching cards.
#
# To add a NEW clickable statistic later: add one entry here, and one
# entry to window._ffClickableStats in heatmap_widget.py (or wire a popup
# row / other click target to call ffBrowseStat(key, start, end) with a
# matching key). Nothing else needs to change — _browse_query_for_range()
# below, the "ff_browse_range:" handler, and the JS-side wiring are all
# already generic over whatever keys exist here.
#
# "reviewed" and "cards_reviewed" intentionally map to the same term:
# Anki's Browser lists cards, not individual revlog events, so there is no
# query that can make it show one row per *review event* the way the
# "Reviews" stat counts them — clicking either one opens the same set of
# distinct cards that were reviewed, which is the most useful thing the
# Browser can show for both.
#
# "due" is NOT in here — see the dedicated branch in
# _browse_query_for_range() below. Every term in this registry counts
# something that already happened, relative to today ("introduced N days
# ago"), which is why the shared A/-B subtraction trick works for all of
# them. A forecast day is the opposite direction (N days from now, not
# ago), so it needs Anki's prop:due=N search instead of term:N, and can't
# share that subtraction logic.
_BROWSE_TERMS = {
    # "new", "reviewed" and "cards_reviewed" are all handled by the exact
    # cid: branch at the top of _browse_query_for_range() below (see that
    # function's docstring) and never actually reach this dict lookup —
    # kept here as documentation of the historical introduced:N/rated:N
    # relative-day mechanism they used to use, and as a fallback shape if
    # that branch is ever bypassed.
    "new":            "introduced",
    "reviewed":       "rated",
    "cards_reviewed": "rated",
}


def _browse_query_for_range(kind: str, start_str: str, end_str: str) -> str:
    """Build an Anki Browser search for a registered statistic over an
    inclusive [start_date, end_date] range — a single day is just
    start_str == end_str.

    Two different mechanisms are in play, depending on kind:

    "new" / "reviewed" / "cards_reviewed" build an EXACT search: the
    distinct card IDs are resolved directly from revlog (same query shape
    as HeatmapService._distinct_new_cards / _distinct_cards_reviewed —
    type = 0 for "new", type IN (1,2,3) for the other two, the
    day_bounds_ms()-derived [start_ms, end_ms) window, same rollover-aware
    boundaries the heatmap itself uses) and opened as "cid:1,2,3,...".
    "reviewed"/"cards_reviewed" originally used an earlier "rated:N
    -rated:M" relative-day approach: that technique silently drops any
    card that was reviewed again more recently than the target range,
    since rated:N matches "has ANY revlog entry in the last N days" and
    the subtraction removes cards matching both terms — Anki's own manual
    notes this "might not work every time" for isolating a specific day.
    "new" originally used the analogous "introduced:N -introduced:M"
    approach; introduced:N turned out to have its own, different failure
    mode — it is defined as the single earliest revlog row ever recorded
    for a card, scanned across that card's ENTIRE history, not "first
    type=0 row within this specific day's window" the way FocusFlow's own
    New Cards summary (HeatmapDay.new_cards / _distinct_new_cards) counts
    it. A card with any earlier revlog row at all — from an import that
    carried scheduling data, a duplicated/copied note, a reset that didn't
    purge history, or (structurally, per Bug #3) a card whose learning
    steps span more than one calendar day — could be missing entirely from
    introduced:N for a day the summary correctly counted it on, opening an
    apparently-empty Browser window for a real, non-zero count. The cid:
    approach used for all three now has no such blind spot — every
    still-existing card with a matching revlog row in the window is
    included, and it is guaranteed to always agree with whatever the
    corresponding summary figure counted, since both read the exact same
    query shape. It cannot include cards that have since been deleted
    (Anki's Browser has no card row to render for those — a `cards`-table
    lookup is unavoidable for any search that opens actual Browser rows,
    "cid:" included), but it recovers everything short of that.

    "due" (prop:due=N, see the dedicated branch below) still uses Anki's
    own relative search term — it looks forward from today rather than
    back, so there is no revlog window to build a cid: list from in the
    first place.

    This replaces the old day-only _browse_query_for_day(); passing the
    same date as both start and end reproduces its exact behaviour (kept
    working as a thin wrapper below for anything still calling it).
    """
    from datetime import date as _date, timedelta as _td

    # BUG FIX: clicking a future (forecast) day's "Due" row had nothing to
    # open — this kind didn't exist at all, since every term above only
    # ever looks backward from today. prop:due=N is Anki's own forward-
    # looking equivalent ("due in exactly N days"); per Anki's manual it
    # matches review cards and day-granularity ("interday") learning cards
    # for that offset, which is exactly the queue scope this addon's own
    # forecast count (_future_due_counts in heatmap_service.py) is built
    # from — sub-day learning steps and brand-new unseen cards aren't
    # "due" on a specific future calendar date in Anki's own scheduling
    # model either, so there's nothing more specific to search for those.
    if kind == "due":
        try:
            start_day = _date.fromisoformat(start_str)
            end_day   = _date.fromisoformat(end_str)
        except ValueError:
            log.debug("_browse_query_for_range: malformed date %r/%r", start_str, end_str)
            return ""
        if end_day < start_day:
            start_day, end_day = end_day, start_day
        today = scheduler_today(_get_day_cutoff())
        offsets = sorted({
            (start_day + _td(days=i) - today).days
            for i in range((end_day - start_day).days + 1)
        })
        offsets = [o for o in offsets if o >= 0]
        if not offsets:
            return ""  # entirely in the past — nothing to forecast
        if len(offsets) == 1:
            return f"prop:due={offsets[0]}"
        return "(" + " or ".join(f"prop:due={o}" for o in offsets) + ")"

    if kind in ("reviewed", "cards_reviewed", "new"):
        try:
            start_day = _date.fromisoformat(start_str)
            end_day   = _date.fromisoformat(end_str)
        except ValueError:
            log.debug("_browse_query_for_range: malformed date %r/%r", start_str, end_str)
            return ""
        if end_day < start_day:
            start_day, end_day = end_day, start_day
        if mw is None or mw.col is None:
            return ""
        day_cutoff = _get_day_cutoff()
        today = scheduler_today(day_cutoff)
        # Same [start_ms, end_ms) window HeatmapService._distinct_cards_reviewed
        # / _distinct_new_cards and the heatmap itself use — day_bounds_ms() is
        # rollover-aware, so this always matches what the clicked day's own
        # figures counted, with no separate rollover-mismatch risk.
        start_ms, _ = day_bounds_ms(start_day, day_cutoff, today)
        _, end_ms   = day_bounds_ms(end_day, day_cutoff, today)
        # type = 0 (Learning) for "new", matching _distinct_new_cards() exactly;
        # type IN (1, 2, 3) for "reviewed"/"cards_reviewed", unchanged.
        type_clause = "type = 0" if kind == "new" else "type IN (1, 2, 3)"
        try:
            cids = mw.col.db.list(
                "SELECT DISTINCT cid FROM revlog "
                f"WHERE id >= ? AND id < ? AND {type_clause}",
                start_ms, end_ms,
            )
        except Exception:
            log.exception("_browse_query_for_range: distinct-cid query failed")
            return ""
        if not cids:
            return ""  # nothing new/reviewed in range (or nothing left that still exists)
        return "cid:" + ",".join(str(c) for c in cids)

    term = _BROWSE_TERMS.get(kind)
    if term is None:
        log.debug("_browse_query_for_range: unknown kind %r", kind)
        return ""
    try:
        start_day = _date.fromisoformat(start_str)
        end_day   = _date.fromisoformat(end_str)
    except ValueError:
        log.debug("_browse_query_for_range: malformed date %r/%r", start_str, end_str)
        return ""
    today = scheduler_today(_get_day_cutoff())
    if end_day > today:
        end_day = today  # clamp — nothing to browse past today
    if start_day > end_day:
        return ""  # fully future range, or a malformed inverted one
    n_start = (today - start_day).days + 1
    if end_day >= today:
        return f"{term}:{n_start}"
    n_end_excl = (today - end_day).days
    if n_end_excl <= 0:
        return f"{term}:{n_start}"
    return f"{term}:{n_start} -{term}:{n_end_excl}"


def _browse_query_for_day(kind: str, date_str: str) -> str:
    """Backward-compatible single-day wrapper around
    _browse_query_for_range() — kept in case anything still calls this
    directly by name."""
    return _browse_query_for_range(kind, date_str, date_str)


def _open_note_dialog(day) -> None:
    """Right-click a heatmap cell -> proper Qt dialog (not window.prompt())
    for adding/editing a note and choosing when its reminder should fire."""
    if mw is None or _config_mgr is None:
        return
    date_str = day.isoformat()
    notes = dict(_config_mgr.heatmap_config().get("day_notes") or {})
    existing = notes.get(date_str, "")
    # Back-compat: v99 stored notes as plain strings with no offset choice.
    if isinstance(existing, dict):
        existing_text   = strip_lone_surrogates(str(existing.get("text", "")))
        existing_offset = str(existing.get("remind_offset", "same"))
    else:
        existing_text   = strip_lone_surrogates(str(existing or ""))
        existing_offset = "same"

    date_label = _fmt_note_date(day)
    dlg = NoteReminderDialog(date_label, existing_text, existing_offset, parent=mw)
    if dlg.exec() != NoteReminderDialog.DialogCode.Accepted:
        return
    try:
        if dlg.was_deleted():
            notes.pop(date_str, None)
        else:
            text = strip_lone_surrogates(dlg.note_text())
            if text:
                notes[date_str] = {"text": text, "remind_offset": dlg.remind_offset()}
            else:
                notes.pop(date_str, None)   # saved blank = same as deleting
        _config_mgr.update_heatmap_config({"day_notes": notes})
        _config_mgr.save()
        if mw.deckBrowser:
            mw.deckBrowser.refresh()
    except Exception:
        log.exception("day note save failed")


def _on_webview_message(
    handled: tuple[bool, object], message: str, context: object
) -> tuple[bool, object]:
    """Save heatmap color-mode selection when the user changes the dropdown,
    the last-clicked date for the "Remember Last Selection" default stats
    view, and per-day notes/reminders added via right-click."""
    if message.startswith("ff_edit_note:"):
        date_str = message.split(":", 1)[1].strip()
        try:
            from datetime import date as _date
            day = _date.fromisoformat(date_str)  # validate before opening the dialog
        except ValueError:
            log.debug("ff_edit_note: ignoring malformed date %r", date_str)
            return (True, None)
        _open_note_dialog(day)
        return (True, None)
    if message.startswith("ff_select_date:"):
        date_str = message.split(":", 1)[1].strip()
        try:
            from datetime import date as _date
            _date.fromisoformat(date_str)  # validate before persisting
            if _config_mgr:
                _config_mgr.update_heatmap_config({"last_selected_date": date_str})
                _config_mgr.save()
        except ValueError:
            log.debug("ff_select_date: ignoring malformed date %r", date_str)
        except Exception as exc:
            log.error("last_selected_date save failed: %s", exc)
        return (True, None)
    if message.startswith("ff_layout_order:"):
        # Persist the drag-to-reorder layout — sent whenever the user drops
        # a block (heatmap / progress bar / stat cards / streak row) into a
        # new position while "Enable drag-to-reorder layout" is on.
        order_str = message.split(":", 1)[1].strip()
        _valid_ids = {"heatmap", "progress", "cards", "streak"}
        order = [b for b in order_str.split(",") if b in _valid_ids]
        if order and _config_mgr:
            try:
                _config_mgr.update_heatmap_config({"layout_block_order": order})
                _config_mgr.save()
            except Exception as exc:
                log.error("layout_block_order save failed: %s", exc)
        return (True, None)
    if message.startswith("ff_get_year:"):
        # PERFORMANCE FIX companion — see render_single_year_svg() in
        # heatmap_widget.py and _build_days_by_year() above. Only the
        # current year's grid ships with the initial panel HTML; every
        # other year is fetched on demand right here, the first time the
        # user actually navigates to it, instead of always being built (and
        # thrown away unseen) on every single deck-browser refresh.
        year_str = message.split(":", 1)[1].strip()
        try:
            year = int(year_str)
        except ValueError:
            log.debug("ff_get_year: ignoring malformed year %r", year_str)
            return (True, "")
        html = ""
        if _heatmap_svc is not None:
            try:
                hm_cfg, meas_cfg = _year_render_config()
                end_conditions = _config_mgr.data.get("end_conditions", {}) if _config_mgr else {}
                # BUG FIX: this was hardcoded to 0 unconditionally, so a
                # forecast/due window configured via due_forecast_days (or
                # "unlimited", see _due_forecast_window above) only ever
                # took effect on the current year's *eager* render — flip
                # forward to next year (or however far "unlimited" would
                # otherwise reach) and every cell there showed as plain,
                # uncoloured, unclickable, even for cards genuinely due
                # that far out. A year fully in the past never has a
                # "future" to forecast, so this only changes behaviour for
                # the current year or later.
                future_count = _due_forecast_window(hm_cfg) if year >= scheduler_today(_get_day_cutoff()).year else 0
                days = _heatmap_svc.days_for_year(
                    year,
                    future_count=future_count,
                    measurement=meas_cfg,
                    end_conditions=end_conditions,
                )
                html = render_single_year_svg(year, days, hm_cfg)
            except Exception:
                log.exception("ff_get_year: failed to render %d", year)
        return (True, html)
    if message.startswith("ff_browse_range:"):
        # "kind:start:end" — kind is a key in _BROWSE_TERMS (currently
        # "new" / "reviewed" / "cards_reviewed"), start/end are inclusive
        # ISO dates. Sent by window.ffBrowseStat() for any registered
        # clickable stat (see window._ffClickableStats and the popup rows'
        # onclick wiring in heatmap_widget.py) — always for whatever
        # period is CURRENTLY being displayed (a single day, a clicked
        # week/month label, or the current Today/Week/Month/All-time
        # view), since that's what start/end are threaded through from.
        # Opens Anki's own Browser, pre-filled with a search for exactly
        # that range — no extra addon-side computation or stored state
        # beyond what was already on screen, so this stays light.
        try:
            _, kind, start_str, end_str = message.split(":", 3)
        except ValueError:
            log.debug("ff_browse_range: malformed message %r", message)
            return (True, None)
        query = _browse_query_for_range(kind.strip(), start_str.strip(), end_str.strip())
        if query and mw:
            dialogs.open("Browser", mw, search=(query,))
        return (True, None)
    if message.startswith("ff_browse:"):
        # Backward-compatible single-day form — kept working in case
        # anything still sends it, but window.ffBrowseDay() (the only
        # thing that ever sent this) now just forwards to ffBrowseStat()
        # and this bridge message isn't emitted by current code anymore.
        try:
            kind, date_str = message.split(":", 1)[1].split(":", 1)
        except ValueError:
            log.debug("ff_browse: malformed message %r", message)
            return (True, None)
        query = _browse_query_for_day(kind.strip(), date_str.strip())
        if query and mw:
            dialogs.open("Browser", mw, search=(query,))
        return (True, None)
    if not message.startswith("ff_color_mode:"):
        return handled
    mode = message.split(":", 1)[1].strip()
    if mode not in ("reviews", "quality", "time"):
        return (True, None)
    try:
        if _config_mgr:
            _config_mgr.update_heatmap_config({"color_mode": mode})
            _config_mgr.save()
    except Exception as exc:
        log.error("color_mode save failed: %s", exc)
    return (True, None)


def _on_deck_browser_render(deck_browser, content) -> None:
    if _heatmap_svc is None:
        return
    try:
        hm_cfg, _meas_cfg = _year_render_config()
        end_conditions = _config_mgr.data.get("end_conditions", {}) if _config_mgr else {}
        days_by_year = _build_days_by_year(
            past_years=2, measurement=_meas_cfg,
            end_conditions=end_conditions,
            full_history=bool(hm_cfg.get("heatmap_full_history", False)),
            due_forecast_days=_due_forecast_window(hm_cfg),
        )
        period_stats = year_stats = None
        cur_streak = lng_streak = 0

        try:
            period_stats = _heatmap_svc.stats(measurement=_meas_cfg)
            year_stats   = _heatmap_svc.all_year_stats(sorted(days_by_year.keys()))
        except Exception as exc:
            log.error("stats failed: %s", exc)

        try:
            ec       = end_conditions
            all_days = _heatmap_svc.days(
                past_count=365, future_count=0, end_conditions=ec
            )
            cur_streak, lng_streak = _compute_streaks(all_days)
            goal_cs, goal_ls       = _heatmap_svc.goal_streak(ec)
            cur_streak = max(cur_streak, goal_cs)
            lng_streak = max(lng_streak, goal_ls)
        except Exception as exc:
            log.error("streak failed: %s", exc)

        _theme_pref = str(hm_cfg.get("theme", "system") or "system")
        if _theme_pref == "dark":
            _night = True
        elif _theme_pref == "light":
            _night = False
        else:
            try:
                from aqt.theme import theme_manager as _tm  # type: ignore[import]
                _night = bool(_tm.night_mode)
            except Exception as exc:
                log.debug("theme_manager unavailable, defaulting to light: %s", exc)
                _night = False

        html = build_heatmap_html(
            days_by_year, hm_cfg,
            period_stats=period_stats,
            year_stats=year_stats,
            current_streak=cur_streak,
            longest_streak=lng_streak,
            color_mode=hm_cfg.get("color_mode", "reviews"),
            night_mode=_night,
        )
        if html:
            # BUG FIX ("multi calendar" / duplicated panel): this hook can
            # fire more than once for the same DeckBrowserContent object —
            # e.g. several refreshes stacking up in quick succession during
            # an active timer session. Since we mutate content.stats with
            # "+=" below, firing twice silently doubled the entire panel,
            # including the calendar grid, producing two stacked heatmaps.
            # Guard against re-appending onto a content.stats that already
            # has our block in it.
            if 'id="ff-heatmap"' in content.stats:
                return
            _hide_native_stats = bool(hm_cfg.get("hide_native_stats_line", False))
            if _hide_native_stats:
                content.stats = html
            else:
                content.stats += html

    except Exception:
        log.exception("heatmap render failed")


# ── Anki state hooks ──────────────────────────────────────────────────────────

def _on_theme_changed() -> None:
    if _toolbar:
        _toolbar.refresh_theme()


def _on_anki_state_change(new_state: str, old_state: str) -> None:
    if new_state == "review":
        if _coordinator:
            _coordinator.editor_open = False
        _set_mode("Studying")
        if _toolbar and not _toolbar.isVisible():
            _toolbar.show_for_reviewer()
        if _timer_mgr and _timer_mgr.is_paused:
            _timer_mgr.resume("editor")
            _timer_mgr.resume("not_reviewing")

    elif new_state in ("deckBrowser", "overview"):
        if _coordinator:
            _coordinator.auto_collapsed_this_session = False
            # Safety net alongside _on_reviewer_will_end's cancellation —
            # covers any transition to a non-Reviewer screen that a pending
            # fatigue-reminder timer should not survive.
            _coordinator.cancel_pending_fatigue_suggestion()
        if _timer_mgr and _timer_mgr.is_running:
            _timer_mgr.pause("not_reviewing")
        if _toolbar:
            _toolbar.hide()
        if _coordinator:
            _coordinator.check_and_mark_all_due_goal()

    elif new_state == "profileManager":
        if _timer_mgr:
            _timer_mgr.stop_idle()


def _on_application_state_changed(state) -> None:
    """Auto-pause/resume when the Anki application loses or regains focus.

    Pauses on anything that is not ApplicationActive so that wall-clock time
    away from the desk is not counted as study time.  Resumes only on Active,
    and only if the pause was caused by this handler (pause_reason "focus_lost")
    so manual pauses are not accidentally cleared.
    """
    try:
        from aqt.qt import Qt
        active = (state == Qt.ApplicationState.ApplicationActive)
    except AttributeError:
        active = False

    if not active:
        if _timer_mgr and _timer_mgr.is_running and not _timer_mgr.is_paused:
            _timer_mgr.pause("focus_lost")
            log.debug("auto-paused: application lost focus (state=%s)", state)
    else:
        if _timer_mgr and _timer_mgr.is_paused:
            _timer_mgr.resume("focus_lost")
            log.debug("auto-resumed: application regained focus")


def _on_card_shown(card) -> None:
    if _fatigue:
        _fatigue.start_card(card)

    editor_was_open = _coordinator.editor_open if _coordinator else False

    if editor_was_open:
        if _coordinator:
            _coordinator.editor_open = False
        if _session_svc:
            _session_svc.editor_closed()
        if _timer_mgr and _timer_mgr.is_paused:
            profile = _config_mgr.data  # type: ignore[union-attr]
            if profile.get("timer", {}).get("auto_resume_after_editor", True):
                _timer_mgr.resume("editor")
            # Resume the not_reviewing pause as well — we are now in the reviewer.
            # Done here (inside the editor branch) to avoid the duplicate call
            # that previously existed after this block (improvement #7).
            _timer_mgr.resume("not_reviewing")
    elif _timer_mgr and _timer_mgr.is_paused:
        # Not coming from editor — only resume the not_reviewing pause.
        _timer_mgr.resume("not_reviewing")

    try:
        mode = "Learning" if card.queue in (0, 1) else "Studying"
    except Exception as exc:
        log.debug("card.queue read failed, defaulting to Studying: %s", exc)
        mode = "Studying"
    _set_mode(mode)

    if _toolbar and not _toolbar.isVisible():
        _toolbar.show_for_reviewer()
        if _coordinator and not _coordinator.auto_collapsed_this_session and _config_mgr:
            if _config_mgr.data.get("toolbar", {}).get("auto_collapse_on_start", False):
                _coordinator.auto_collapsed_this_session = True
                QTimer.singleShot(80, lambda: _toolbar and _toolbar.set_collapsed(True))

    if _timer_mgr and not _timer_mgr.has_started:
        if _session_svc:
            _session_svc.start_session()
        _timer_mgr.start_study()


def _on_card_answered(reviewer, card, ease: int) -> None:
    if _session_svc:
        _session_svc.record_card()
    if _coordinator:
        _coordinator.total_cards_today += 1
    if _heatmap_svc:
        _heatmap_svc.invalidate_cache()
    if _fatigue:
        snap = _fatigue.record_answer(card, ease)
        if _toolbar:
            _toolbar.set_fatigue(snap.state, snap.score)
        if _coordinator:
            if (_fatigue.should_break_now()
                    and not _coordinator.fatigue_suggested
                    and _config_mgr
                    and _config_mgr.data.get("fatigue", {}).get("soft_break_prompt", True)):
                _coordinator.fatigue_suggested = True
                _coordinator.schedule_fatigue_suggestion()
            elif not _fatigue.should_break_now():
                _coordinator.fatigue_suggested = False
            _coordinator.apply_fatigue_to_timer(snap)
    if _coordinator:
        _coordinator.update_conditions()


def _on_operation_executed(changes, handler) -> None:
    # Any completed collection operation (undo, card/note deletion,
    # suspend/unsuspend, bury/unbury, etc.) can change revlog or card data
    # that HeatmapService's caches were built from. _on_card_answered above
    # only covers new reviews; this covers everything else that can leave
    # those caches stale. Unconditional invalidation, no filtering on
    # `changes` -- see Bug #5B investigation for why.
    if _heatmap_svc:
        _heatmap_svc.invalidate_cache()


def _on_reviewer_will_end() -> None:
    if _timer_mgr and _timer_mgr.is_running:
        _timer_mgr.pause("not_reviewing")
    if _coordinator:
        _coordinator.cancel_pending_fatigue_suggestion()


def _on_editor_did_init(editor) -> None:
    if _coordinator:
        _coordinator.editor_open = True
    _set_mode("Creating")
    if _session_svc:
        _session_svc.editor_opened()
    if _fatigue:
        _fatigue.editor_opened()
    if _timer_mgr and _timer_mgr.is_running:
        _timer_mgr.pause("editor")


def _on_editor_did_load_note(editor) -> None:
    if _coordinator:
        _coordinator.editor_open = True
    if _fatigue:
        _fatigue.editor_opened()


# ── commands ──────────────────────────────────────────────────────────────────

def _cmd_play_pause() -> None:
    if _timer_mgr is None:
        return
    if _timer_mgr.mode == TimerMode.IDLE:
        if _session_svc:
            _session_svc.start_session()
        _timer_mgr.start_study()
    # BUG FIX: this used to only check STUDY, so the toolbar's Play/Pause
    # button — labelled "Pause"/"Resume" during a break too, see
    # FocusFlowToolbar.set_snapshot() — silently did nothing when clicked
    # mid-break.
    elif _timer_mgr.mode in (TimerMode.STUDY, TimerMode.BREAK):
        _timer_mgr.toggle_manual_pause()


def _cmd_reset_timer() -> None:
    """Hold-on-timer handler: restart the current Pomodoro from zero."""
    if _timer_mgr is None:
        return
    if _timer_mgr.mode in (TimerMode.STUDY, TimerMode.BREAK):
        _timer_mgr.reset_to_start()


def _cmd_switch_profile(name: str) -> None:
    if _config_mgr is None:
        return
    try:
        _config_mgr.switch_profile(name)
    except ValueError:
        return
    _apply_config()


def _cmd_open_settings() -> None:
    if mw is None or _config_mgr is None:
        return
    # _heatmap_svc is None before the first profile opens.  SettingsDialog
    # accepts None and disables heatmap-specific controls gracefully, so we
    # pass it through rather than bailing out — the user can still edit all
    # non-heatmap settings even before a profile has been loaded.
    dlg = SettingsDialog(
        _config_mgr.full_config(),
        heatmap_service=_heatmap_svc,   # may be None — dialog handles this
        parent=mw,
    )
    dlg.changed.connect(_on_settings_changed)
    dlg.saved.connect(_on_settings_saved)
    dlg.test_sound_requested.connect(lambda: _sound and _sound.play())
    dlg.manual_session_requested.connect(_cmd_log_manual_session)
    result = dlg.exec()
    # PERFORMANCE FIX: the debounce timer below is only ever meant to cover
    # the interval between live-edit events while the dialog is open. If the
    # dialog closes mid-debounce (e.g. the user drags a slider then
    # immediately hits Save/Cancel/Esc before the 250ms window elapses),
    # stop it here rather than letting it fire afterwards — Save already
    # calls _apply_config() itself via _on_settings_saved, and Cancel
    # shouldn't apply a change that was never committed.
    if _settings_debounce is not None:
        _settings_debounce.stop()
    if result != SettingsDialog.DialogCode.Accepted:
        _apply_settings_debounced()


def _cmd_log_manual_session() -> None:
    if mw is None or _session_svc is None:
        return
    dlg = ManualSessionDialog(parent=mw)
    if dlg.exec() != dlg.DialogCode.Accepted:
        return
    data    = dlg.session_data()
    session = StudySession(
        start_time=data["start_time"], end_time=data["end_time"],
        duration=data["duration"],     cards_done=data["cards_done"],
        effective_score=data["effective_score"],
    )
    try:
        _session_svc.repo.insert(session)
        if _heatmap_svc:
            _heatmap_svc.invalidate_cache()
        if mw and mw.deckBrowser:
            mw.deckBrowser.refresh()
    except Exception:
        log.exception("manual session insert failed")


# ── settings callbacks ────────────────────────────────────────────────────────

# PERFORMANCE FIX: SettingsDialog emits `changed` on every single widget edit
# for live preview — every checkbox click, every spinner tick, and (worst
# case) every pixel of a slider drag, which can fire dozens of `valueChanged`
# events per second (Volume, and the 4 Again/Hard/Good/Easy weight sliders in
# Measurement). _on_settings_changed used to call _apply_config() — which
# includes SessionCoordinator.update_conditions(), and when "All due cards
# finished" is enabled (the default) that runs a real mw.col.find_cards(...)
# collection query — synchronously, immediately, on every one of those
# events. Dragging one slider could fire that expensive query dozens of
# times in under a second, backing up Anki's event queue right as the user
# closes the dialog and starts reviewing. Debouncing collapses any burst of
# rapid edits into a single _apply_config() call ~250ms after the last one,
# which still feels instant for live preview but cuts the number of
# find_cards() calls from "one per pixel dragged" to "one per pause."
_settings_debounce: "Optional[QTimer]" = None
_settings_pending_config: dict | None = None


def _apply_settings_debounced() -> None:
    global _settings_pending_config
    if _config_mgr and _settings_pending_config is not None:
        _config_mgr.replace_full_config(_settings_pending_config)
        _apply_config()
    _settings_pending_config = None


def _on_settings_changed(config: dict) -> None:
    global _settings_debounce, _settings_pending_config
    if not _config_mgr:
        return
    _settings_pending_config = config
    if _settings_debounce is None:
        _settings_debounce = QTimer(mw)
        _settings_debounce.setSingleShot(True)
        _settings_debounce.timeout.connect(_apply_settings_debounced)
    _settings_debounce.start(250)


def _on_settings_saved(config: dict) -> None:
    global _settings_pending_config
    if _settings_debounce is not None:
        _settings_debounce.stop()
    _settings_pending_config = None
    if _config_mgr:
        _config_mgr.replace_full_config(config)
        _config_mgr.save()
        _apply_config()
        if mw and mw.deckBrowser:
            mw.deckBrowser.refresh()


def _apply_config() -> None:
    if _config_mgr is None:
        return
    profile = _config_mgr.data
    if _timer_mgr:  _timer_mgr.update_config(profile)
    if _fatigue:    _fatigue.update_config(profile)
    if _sound:      _sound.update_config(profile)
    if _toolbar:
        _toolbar.apply_config(profile)
        _indicator = profile.get("fatigue", {}).get("indicator", "dot")
        _toolbar.set_indicator_mode(_indicator)
    _sync_toolbar_profiles()
    if _coordinator:
        _coordinator.update_conditions()


# ── toolbar helpers ───────────────────────────────────────────────────────────

def _set_mode(mode: str) -> None:
    if _coordinator is None:
        return
    if mode == _coordinator.current_mode:
        return
    _coordinator.current_mode = mode
    if _toolbar:
        _toolbar.set_mode(mode)


def _sync_toolbar_profiles() -> None:
    if _toolbar is None or _config_mgr is None:
        return
    _toolbar.set_profiles(
        _config_mgr.profile_names(),
        _config_mgr.active_profile_name(),
    )


def _on_collapse_changed(state: int) -> None:
    if _config_mgr:
        _config_mgr.set("toolbar.collapsed",     state == 0)
        _config_mgr.set("toolbar.display_state", state)
        _config_mgr.save()


def _on_position_changed(x: int, y: int) -> None:
    if _config_mgr and _config_mgr.data.get("toolbar", {}).get("remember_position", True):
        _config_mgr.set("toolbar.position", [x, y])
        _config_mgr.save()


# ── entry point ───────────────────────────────────────────────────────────────

try:
    _init()
except Exception:
    log.exception("Failed to initialise FocusFlow")
    try:
        def _emergency_menu() -> None:
            if mw:
                act = QAction("[FocusFlow — load error]", mw)
                act.setEnabled(False)
                mw.form.menuTools.addAction(act)
        gui_hooks.main_window_did_init.append(_emergency_menu)
    except Exception:
        pass
