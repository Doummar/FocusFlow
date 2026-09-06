from __future__ import annotations

import random
from datetime import date
from typing import Any, Callable

from aqt.qt import (
    QButtonGroup, QComboBox, QDateEdit, QDialog, QDoubleSpinBox, QFont,
    QFrame, QGroupBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QRadioButton, QSlider, Qt, QVBoxLayout, QDate,
    QWidget,
)

# Locale-independent weekday/month names.
#
# BUG FIX: the session-complete header used date.strftime("%A, %d %b %Y"),
# which is locale-dependent — on a non-English OS locale (e.g. Danish) this
# silently rendered in that language with different casing/abbreviations
# (e.g. "Thursday" -> "torsdag", "Jul" -> "jul"). FocusFlow's UI is
# English-only, so these are spelled out explicitly instead.
_WEEKDAY_FULL = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _fmt_full_date(d: date) -> str:
    """Locale-independent 'Thursday, 16 Jul 2026' style label."""
    return f"{_WEEKDAY_FULL[d.weekday()]}, {d.day:02d} {_MONTH_ABBR[d.month - 1]} {d.year}"


_SUGGESTIONS = [
    "Look away from the screen for 20 seconds.",
    "Stretch your body.", "Drink some water.",
    "Walk around for a minute.", "Rest your eyes — look far away.",
    "Roll your shoulders back.", "Take five slow, deep breaths.",
    "Stand up and shake out your hands.",
]

def _h1() -> QFont:
    f = QFont(); f.setPointSize(12); f.setBold(True); return f

def _sep() -> QFrame:
    f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken); return f

def _fmt(s: int) -> str:
    m, sec = divmod(s, 60); return f"{m:02d}:{sec:02d}"

def _fmt_mins(minutes: float) -> str:
    total = int(round(minutes))
    if total < 60: return f"{total}m"
    h, m = divmod(total, 60)
    return f"{h}h {m}m" if m else f"{h}h"

def _fmt_secs(seconds: int) -> str:
    return _fmt_mins(seconds / 60.0)

_FLAGS = Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint
# Minimal stylesheet — only the GroupBox border, using palette() so it
# works in both Anki light and dark themes without hard-coding colours.
_STYLE = (
    "QGroupBox{font-weight:600;border:1px solid palette(mid);"
    "border-radius:4px;margin-top:8px;padding-top:10px}"
    "QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px}"
)

def _again_rate_html(again_rate: float, avg: float) -> str:
    """↑ red when worse (rate up), ↓ green when better (rate down)."""
    delta = again_rate - avg
    if delta > 0.02:
        arrow, color = f"↑ +{delta:.0%}", "#a03030"   # higher = worse
    elif delta < -0.02:
        arrow, color = f"↓ {delta:.0%}", "#2e7a40"    # lower = better
    else:
        arrow, color = "= same", "#888"
    return (
        f"<span style='font-weight:600'>{again_rate:.0%}</span>"
        f"&nbsp;&nbsp;<span style='color:{color};font-weight:600'>{arrow}</span>"
        f"&nbsp;<span style='font-size:10px'>avg {avg:.0%}</span>"
    )

# ── SessionSummaryPopup ───────────────────────────────────────────────────────

class SessionSummaryPopup(QDialog):
    def __init__(
        self,
        session_num: int,
        break_label: str,
        duration_seconds: int,
        fatigue_score: float,
        again_rate: float,
        avg_7day_again_rate: float,
        on_start_break: Callable | None = None,
        on_skip_break:  Callable | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._on_start_break = on_start_break
        self._on_skip_break  = on_skip_break
        self.setWindowTitle("FocusFlow — Session complete")
        self.setWindowFlags(_FLAGS); self.setFixedWidth(380)
        self.setModal(False); self.setStyleSheet(_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16); root.setSpacing(10)

        today_str = _fmt_full_date(date.today())
        header = QLabel(f"Session {session_num}  ·  {today_str}")
        header.setFont(_h1()); root.addWidget(header)

        tip = QLabel(random.choice(_SUGGESTIONS)); tip.setWordWrap(True)
        tip.setStyleSheet("font-style:italic;")
        root.addWidget(tip); root.addWidget(_sep())

        eff_s = int(duration_seconds * fatigue_score)
        mg = QGroupBox("Session", self); mf = QVBoxLayout(mg); mf.setSpacing(5)
        self._mrow(mf, "Study time",     _fmt_secs(duration_seconds))
        self._mrow(mf, "Effective time", f"{_fmt_secs(eff_s)}  ({fatigue_score:.0%})")

        ar = QHBoxLayout()
        ar.addWidget(self._lbl("Again rate")); ar.addStretch()
        al = QLabel(_again_rate_html(again_rate, avg_7day_again_rate))
        al.setTextFormat(Qt.TextFormat.RichText); ar.addWidget(al)
        mf.addLayout(ar); root.addWidget(mg)
        root.addWidget(_sep())

        bn = QLabel(f"Take a {break_label}?")
        root.addWidget(bn)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        skip = QPushButton("Skip break"); skip.clicked.connect(self._do_skip)
        btn_row.addWidget(skip); btn_row.addStretch()
        start = QPushButton(f"Start {break_label} →")
        start.setDefault(True)
        start.clicked.connect(self._do_start); btn_row.addWidget(start)
        root.addLayout(btn_row)

    def _do_start(self) -> None:
        self.accept()
        if self._on_start_break: self._on_start_break()

    def _do_skip(self) -> None:
        self.accept()
        if self._on_skip_break: self._on_skip_break()

    def _mrow(self, layout, label: str, value: str) -> None:
        r = QHBoxLayout(); r.addWidget(self._lbl(label)); r.addStretch()
        v = QLabel(value); v.setStyleSheet("font-weight:600;")
        r.addWidget(v); layout.addLayout(r)

    def _lbl(self, text: str) -> QLabel:
        l = QLabel(text); return l

# ── BreakChoicePopup ──────────────────────────────────────────────────────────

class BreakChoicePopup(QDialog):
    """Shown at long-break milestone — let user choose long or normal break."""
    def __init__(
        self,
        session_num: int,
        long_break_label: str,
        normal_break_label: str,
        cards_done: int,
        duration_seconds: int,
        fatigue_score: float,
        on_long_break:   Callable | None = None,
        on_normal_break: Callable | None = None,
        on_skip_break:   Callable | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("FocusFlow — Break time")
        self.setWindowFlags(_FLAGS)
        self.setMinimumWidth(360)   # floor only — adjustSize() sets the real width
        self.setModal(False); self.setStyleSheet(_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16); root.setSpacing(10)

        header = QLabel(f"Session {session_num} complete")
        header.setFont(_h1()); root.addWidget(header)

        tip = QLabel(random.choice(_SUGGESTIONS)); tip.setWordWrap(True)
        tip.setStyleSheet("font-style:italic;")
        root.addWidget(tip); root.addWidget(_sep())

        # Session quick stats
        sg = QGroupBox("Session", self); sf = QVBoxLayout(sg); sf.setSpacing(4)
        eff_s = int(duration_seconds * fatigue_score)
        self._row(sf, "Cards reviewed", str(cards_done))
        self._row(sf, "Effective time", f"{_fmt_secs(eff_s)}  ({fatigue_score:.0%})")
        root.addWidget(sg); root.addWidget(_sep())

        note = QLabel("You've reached a long break milestone. Choose your break:")
        note.setWordWrap(True)
        root.addWidget(note)

        # Primary actions — each on its own full-width row so long labels always fit
        long_btn = QPushButton(f"Long break  —  {long_break_label}")
        long_btn.setDefault(True)
        long_btn.clicked.connect(self._make_handler(on_long_break))
        root.addWidget(long_btn)

        normal = QPushButton(f"Short break  —  {normal_break_label}")
        normal.clicked.connect(self._make_handler(on_normal_break))
        root.addWidget(normal)

        # Secondary action — right-aligned, visually de-emphasised
        skip_row = QHBoxLayout(); skip_row.addStretch()
        skip = QPushButton("Skip break")
        skip.clicked.connect(self._make_handler(on_skip_break))
        skip_row.addWidget(skip)
        root.addLayout(skip_row)

        self.adjustSize()   # let Qt compute the exact width the content needs

    def _make_handler(self, cb: Callable | None):
        def handler():
            self.accept()
            if cb: cb()
        return handler

    def _row(self, layout, label: str, value: str) -> None:
        r = QHBoxLayout(); r.addWidget(self._lbl(label)); r.addStretch()
        v = QLabel(value); v.setStyleSheet("font-weight:600;")
        r.addWidget(v); layout.addLayout(r)

    def _lbl(self, text: str) -> QLabel:
        l = QLabel(text); return l

# ── GoalReachedPopup ──────────────────────────────────────────────────────────

class GoalReachedPopup(QDialog):
    def __init__(
        self,
        goal_description: str,
        effective_minutes: float,
        quality_score: float,
        again_rate: float,
        avg_7day_again_rate: float,
        current_streak: int,
        longest_streak: int,
        new_cards: int,
        reviews: int,
        relearned: int,
        raw_new_events: int,
        on_continue: Callable | None = None,
        on_done:     Callable | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("FocusFlow — Goal reached")
        self.setWindowFlags(_FLAGS); self.setFixedWidth(420)
        self.setModal(False); self.setStyleSheet(_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16); root.setSpacing(12)

        title = QLabel("Goal reached"); title.setFont(_h1()); root.addWidget(title)
        gl = QLabel(goal_description)
        gl.setStyleSheet("font-style:italic;")
        root.addWidget(gl); root.addWidget(_sep())

        qg = QGroupBox("Study Quality", self); qf = QVBoxLayout(qg); qf.setSpacing(5)
        self._row(qf, "Effective time", _fmt_mins(effective_minutes))
        self._row(qf, "Focus score",    f"{quality_score:.0%}")
        ar = QHBoxLayout(); ar.addWidget(self._lbl("Again rate")); ar.addStretch()
        al = QLabel(_again_rate_html(again_rate, avg_7day_again_rate))
        al.setTextFormat(Qt.TextFormat.RichText); ar.addWidget(al)
        qf.addLayout(ar); root.addWidget(qg)

        # Reviews total = raw_study_events() = raw (undeduplicated) type=0
        # events + reviews_count (types 1+3) + relearned (type 2), matching
        # Anki's own LIVE studied_today() semantics and the same canonical
        # figure every other "Reviews" display in the addon uses (day-popup,
        # week/month-label popup, the panel's metric card, HeatmapDialog's
        # stats table). Deliberately NOT new_cards (that's the distinct-card
        # count shown separately below as "New learned"). "New learned" and
        # "Relearned" stay as their own breakdown rows too — they're
        # informational sub-detail, not excluded from the total above them.
        _reviews_combined = raw_new_events + reviews + relearned
        cg = QGroupBox("Cards today", self); cf = QVBoxLayout(cg); cf.setSpacing(5)
        if _reviews_combined: self._row(cf, "Reviews",     str(_reviews_combined))
        if new_cards: self._row(cf, "New learned", str(new_cards))
        if relearned: self._row(cf, "Relearned",   str(relearned))
        if not (_reviews_combined or new_cards or relearned):
            self._row(cf, "Reviewed", "—")
        root.addWidget(cg)

        sg = QGroupBox("Streak", self); sf = QVBoxLayout(sg); sf.setSpacing(5)
        self._row(sf, "Current streak", f"{current_streak} day{'s' if current_streak != 1 else ''}")
        self._row(sf, "Longest streak", f"{longest_streak} day{'s' if longest_streak != 1 else ''}")
        root.addWidget(sg); root.addWidget(_sep())

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        done = QPushButton("Done for today"); done.clicked.connect(lambda: (self.accept(), on_done and on_done()))
        btn_row.addWidget(done); btn_row.addStretch()
        cont = QPushButton("Continue studying →")
        cont.setDefault(True)
        cont.clicked.connect(lambda: (self.accept(), on_continue and on_continue()))
        btn_row.addWidget(cont); root.addLayout(btn_row)

    def _row(self, layout, label: str, value: str) -> None:
        r = QHBoxLayout(); r.addWidget(self._lbl(label)); r.addStretch()
        v = QLabel(value); v.setStyleSheet("font-weight:600;")
        r.addWidget(v); layout.addLayout(r)

    def _lbl(self, text: str) -> QLabel:
        l = QLabel(text); return l

# ── BreakRunningPopup ─────────────────────────────────────────────────────────

class BreakRunningPopup(QDialog):
    """Break countdown display.

    This popup is intentionally display-only.  It has no internal timer.
    All updates come from TimerManager.tick via update_from_tick().
    Break completion is owned entirely by _on_break_completed() in __init__.py.
    """

    def __init__(
        self,
        break_seconds: int,
        break_label: str,
        cards_done: int = 0,
        effective_secs: int = 0,
        quality_score: float = 0.0,
        on_end_early: Callable | None = None,
        stay_on_top: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._on_end_early = on_end_early
        self._total        = max(break_seconds, 1)
        self.setWindowTitle("FocusFlow — Break")
        # _FLAGS (module-level, shared by every popup in this file) always
        # includes WindowStaysOnTopHint. This is the one popup with a
        # user-facing preference for that specifically (Settings -> Timer ->
        # "Break popup"), so it chooses its own flags instead of using
        # _FLAGS directly — every other popup class here is unaffected.
        _break_popup_flags = _FLAGS if stay_on_top else Qt.WindowType.Window
        self.setWindowFlags(_break_popup_flags); self.setFixedWidth(340)
        self.setModal(False); self.setStyleSheet(_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16); root.setSpacing(10)

        title = QLabel(f"Break — {break_label}"); title.setFont(_h1())
        root.addWidget(title)

        if cards_done or effective_secs:
            stats_lbl = QLabel(
                f"{cards_done} cards · {_fmt_secs(effective_secs)} effective"
                f" · {quality_score:.0%} quality"
            )
            stats_lbl.setStyleSheet("font-size:11px;")
            root.addWidget(stats_lbl)

        tip = QLabel(random.choice(_SUGGESTIONS)); tip.setWordWrap(True)
        tip.setStyleSheet("font-style:italic;"); root.addWidget(tip)
        root.addWidget(_sep())

        self._cd = QLabel(_fmt(self._total))
        self._cd.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont(); f.setPointSize(32); f.setBold(True); self._cd.setFont(f)
        root.addWidget(self._cd)

        from aqt.qt import QProgressBar
        self._bar = QProgressBar(); self._bar.setRange(0, self._total)
        self._bar.setValue(self._total); self._bar.setTextVisible(False)
        self._bar.setMaximumHeight(4); root.addWidget(self._bar)
        root.addWidget(_sep())

        row = QHBoxLayout(); row.addStretch()
        end = QPushButton("End break early"); end.clicked.connect(self._do_end_early)
        row.addWidget(end); root.addLayout(row)
        self._update_color(self._total)

    def update_from_tick(self, snapshot: Any) -> None:
        """Receive a TimerSnapshot from TimerManager.tick and refresh the display."""
        remaining = snapshot.remaining_seconds
        self._cd.setText(_fmt(remaining))
        self._bar.setValue(remaining)
        self._update_color(remaining)

    def _update_color(self, remaining: int) -> None:
        c = ("rgba(180,60,60,0.85)" if remaining == 0 else
             "rgba(200,130,0,0.85)" if remaining <= 10 else
             "rgba(60,140,80,0.85)")
        self._cd.setStyleSheet(f"color:{c};")

    def _do_end_early(self) -> None:
        self.accept()
        if self._on_end_early: self._on_end_early()

# ── BreakFinishedPopup ────────────────────────────────────────────────────────

class BreakFinishedPopup(QDialog):
    """Break-finished prompt.

    Deliberately has no knowledge of TimerManager/SessionCoordinator state —
    this is a display-only dialog, same philosophy as BreakRunningPopup above.
    Both the "Continue studying" button and the window X close it via Qt's
    normal accept()/reject() lifecycle; the caller (SessionCoordinator)
    connects to the dialog's `finished` signal to run the same cleanup
    exactly once regardless of which of those two paths the user takes —
    see on_break_completed() in session_coordinator.py.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FocusFlow — Break finished")
        self.setWindowFlags(_FLAGS); self.setFixedWidth(300)
        self.setModal(False); self.setStyleSheet(_STYLE)
        root = QVBoxLayout(self); root.setContentsMargins(22, 18, 22, 16); root.setSpacing(10)
        title = QLabel("Break finished"); title.setFont(_h1()); root.addWidget(title)
        sub = QLabel("Ready to continue?"); root.addWidget(sub)
        root.addWidget(_sep())
        row = QHBoxLayout(); row.addStretch()
        cont = QPushButton("Continue studying"); cont.setDefault(True)
        cont.clicked.connect(self.accept)
        row.addWidget(cont); root.addLayout(row)

# ── FatigueSuggestionPopup ────────────────────────────────────────────────────

class FatigueSuggestionPopup(QDialog):
    def __init__(self, on_take_break=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FocusFlow"); self.setWindowFlags(_FLAGS)
        self.setFixedWidth(320); self.setModal(False); self.setStyleSheet(_STYLE)
        root = QVBoxLayout(self); root.setContentsMargins(20, 16, 20, 14); root.setSpacing(8)
        title = QLabel("Focus quality dropping"); title.setFont(_h1()); root.addWidget(title)
        msg = QLabel("Your answers suggest mental fatigue.\nA short break may help restore focus.")
        msg.setWordWrap(True); root.addWidget(msg)
        root.addWidget(_sep())
        row = QHBoxLayout()
        dismiss = QPushButton("Dismiss"); dismiss.clicked.connect(self.accept); row.addWidget(dismiss)
        row.addStretch()
        brk = QPushButton("Take a break"); brk.setDefault(True)
        brk.clicked.connect(lambda: (self.accept(), on_take_break and on_take_break()))
        row.addWidget(brk); root.addLayout(row)

# ── ManualSessionDialog ───────────────────────────────────────────────────────

class ManualSessionDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FocusFlow — Log Past Session")
        self.setWindowFlags(Qt.WindowType.Window)
        self.setFixedWidth(360); self.setModal(True); self.setStyleSheet(_STYLE)
        root = QVBoxLayout(self); root.setContentsMargins(20, 18, 20, 16); root.setSpacing(12)
        title = QLabel("Log a past study session"); title.setFont(_h1()); root.addWidget(title)
        note = QLabel("Use this if you studied without FocusFlow running.")
        note.setWordWrap(True); note.setStyleSheet("font-size:11px;")
        root.addWidget(note); root.addWidget(_sep())
        from aqt.qt import QFormLayout, QSpinBox
        form = QFormLayout(); form.setSpacing(8)
        self.date_edit = QDateEdit(self); self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate()); self.date_edit.setMaximumDate(QDate.currentDate())
        form.addRow("Date:", self.date_edit)
        self.duration = QDoubleSpinBox(self); self.duration.setRange(1, 480)
        self.duration.setDecimals(0); self.duration.setValue(25); self.duration.setSuffix(" min")
        form.addRow("Study duration:", self.duration)
        self.cards = QSpinBox(self); self.cards.setRange(0, 9999); self.cards.setValue(0)
        form.addRow("Cards reviewed:", self.cards)
        self.quality = QSlider(Qt.Orientation.Horizontal, self)
        self.quality.setRange(1, 10); self.quality.setValue(7)
        self.quality_lbl = QLabel("7/10 (Good)", self); self.quality.valueChanged.connect(self._on_quality)
        qrow = QHBoxLayout(); qrow.addWidget(self.quality); qrow.addWidget(self.quality_lbl)
        form.addRow("Session quality:", qrow)
        root.addLayout(form); root.addWidget(_sep())
        btns = QHBoxLayout()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        save   = QPushButton("Save session"); save.setDefault(True); save.clicked.connect(self.accept)
        btns.addWidget(cancel); btns.addStretch(); btns.addWidget(save); root.addLayout(btns)

    def _on_quality(self, v: int) -> None:
        labels = {1:"Very poor",2:"Poor",3:"Below avg",4:"Fair",5:"Average",
                  6:"Above avg",7:"Good",8:"Very good",9:"Excellent",10:"Perfect"}
        self.quality_lbl.setText(f"{v}/10 ({labels.get(v,'')})")

    def session_data(self) -> dict:
        from datetime import datetime as _dt, date as _date
        qd = self.date_edit.date(); d = _date(qd.year(), qd.month(), qd.day())
        start_ts = int(_dt.combine(d, _dt.min.time()).timestamp())
        dur = int(self.duration.value() * 60)
        return {"date": d, "start_time": start_ts, "end_time": start_ts + dur,
                "duration": dur, "cards_done": self.cards.value(),
                "effective_score": self.quality.value() / 10.0}


# ── ExperienceWizardDialog ────────────────────────────────────────────────────

class ExperienceWizardDialog(QDialog):
    """One-time first-run wizard letting a new user pick how much of
    FocusFlow's settings surface they want to see. Shown once; the choice
    (and everything else) can always be changed later from the Experience
    dropdown at the top of Settings."""

    _OPTIONS = [
        ("beginner", "Beginner  (Recommended)",
         "First-time users — only the essentials: Timer, Heatmap, Streak, Theme."),
        ("basic", "Basic",
         "Most users — adds Goals, basic display options, simple customization."),
        ("intermediate", "Intermediate",
         "Regular users — adds Fatigue detection, Measurement method, Timer customization."),
        ("advanced", "Advanced",
         "Power users — adds Heatmap customization, detailed statistics, more display options."),
        ("expert", "Expert",
         "Enthusiasts — everything: detection signals, weight tuning, hidden and experimental options."),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Welcome to FocusFlow")
        self.setMinimumWidth(420)
        self.setStyleSheet(_STYLE)

        root = QVBoxLayout(self)
        title = QLabel("Choose your experience")
        title.setFont(_h1())
        root.addWidget(title)

        sub = QLabel("You can change this anytime from Settings.")
        sub.setStyleSheet("color:palette(mid);font-size:10px;font-style:italic;margin-bottom:6px")
        root.addWidget(sub)

        self._group = QButtonGroup(self)
        self._radios: dict[str, QRadioButton] = {}
        for key, label, desc in self._OPTIONS:
            row = QVBoxLayout()
            rb = QRadioButton(label, self)
            rb.setStyleSheet("font-weight:600")
            row.addWidget(rb)
            dl = QLabel(desc, self)
            dl.setWordWrap(True)
            dl.setStyleSheet("color:palette(mid);font-size:10px;margin-left:22px")
            row.addWidget(dl)
            root.addLayout(row)
            self._group.addButton(rb)
            self._radios[key] = rb
        self._radios["beginner"].setChecked(True)

        root.addWidget(_sep())
        btns = QHBoxLayout()
        btns.addStretch()
        start = QPushButton("Get Started", self)
        start.setDefault(True)
        start.clicked.connect(self.accept)
        btns.addWidget(start)
        root.addLayout(btns)

    def chosen_mode(self) -> str:
        for key, rb in self._radios.items():
            if rb.isChecked():
                return key
        return "beginner"


# ── NoteReminderDialog ─────────────────────────────────────────────────────

class NoteReminderDialog(QDialog):
    """Clean, native replacement for the old window.prompt() note editor —
    reopening a day that already has a note pre-fills the text and the
    saved reminder timing so both are editable, not just the text.

    Opens in a read-only "view" mode when a note already exists (date +
    note text + an Edit button) so a quick glance doesn't drop you straight
    into an editable form. Clicking Edit reveals the full editor. Creating
    a brand-new note (no existing text) skips straight to the editor since
    there's nothing to view yet."""

    REMIND_OPTIONS = [
        ("same",         "On this day"),
        ("day_before",   "1 day before"),
        ("week_before",  "1 week before"),
        ("month_before", "1 month before"),
    ]

    def __init__(self, date_label: str, existing_text: str = "",
                 existing_offset: str = "same", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FocusFlow — Note & Reminder")
        self.setMinimumWidth(380)
        self._deleted = False

        root = QVBoxLayout(self)

        title = QLabel(f"<b>{date_label}</b>", self)
        title.setFont(_h1())
        root.addWidget(title)
        root.addWidget(_sep())

        # ── View mode: read-only note text + an Edit button ────────────────
        self.view_container = QWidget(self)
        view_l = QVBoxLayout(self.view_container)
        view_l.setContentsMargins(0, 0, 0, 0)
        self.view_text = QLabel(existing_text, self.view_container)
        self.view_text.setWordWrap(True)
        self.view_text.setStyleSheet("padding:4px 2px")
        view_l.addWidget(self.view_text)
        view_btn_row = QHBoxLayout()
        view_btn_row.addStretch()
        close_btn = QPushButton("Close", self.view_container)
        close_btn.clicked.connect(self.reject)
        view_btn_row.addWidget(close_btn)
        edit_btn = QPushButton("Edit", self.view_container)
        edit_btn.setDefault(True)
        edit_btn.clicked.connect(self._enter_edit_mode)
        view_btn_row.addWidget(edit_btn)
        view_l.addLayout(view_btn_row)
        root.addWidget(self.view_container)

        # ── Edit mode: the full editor (unchanged from before) ─────────────
        self.edit_container = QWidget(self)
        edit_l = QVBoxLayout(self.edit_container)
        edit_l.setContentsMargins(0, 0, 0, 0)

        edit_l.addWidget(QLabel("Note", self.edit_container))
        self.text_edit = QPlainTextEdit(self.edit_container)
        self.text_edit.setPlainText(existing_text)
        self.text_edit.setPlaceholderText("e.g. Biology exam today — review chapters 4-6")
        self.text_edit.setFixedHeight(90)
        edit_l.addWidget(self.text_edit)

        remind_row = QHBoxLayout()
        remind_row.addWidget(QLabel("Remind me:", self.edit_container))
        self.remind_combo = QComboBox(self.edit_container)
        for key, label in self.REMIND_OPTIONS:
            self.remind_combo.addItem(label, key)
        _idx = next((i for i, (k, _) in enumerate(self.REMIND_OPTIONS) if k == existing_offset), 0)
        self.remind_combo.setCurrentIndex(_idx)
        remind_row.addWidget(self.remind_combo, 1)
        edit_l.addLayout(remind_row)

        edit_l.addWidget(_sep())
        btn_row = QHBoxLayout()
        if existing_text:
            delete_btn = QPushButton("Delete note", self.edit_container)
            delete_btn.clicked.connect(self._on_delete)
            btn_row.addWidget(delete_btn)
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel", self.edit_container)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save", self.edit_container)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.accept)
        btn_row.addWidget(save_btn)
        edit_l.addLayout(btn_row)
        root.addWidget(self.edit_container)

        # A brand-new note has nothing to view yet — go straight to the editor.
        if existing_text:
            self.edit_container.setVisible(False)
        else:
            self.view_container.setVisible(False)

    def _enter_edit_mode(self) -> None:
        self.view_container.setVisible(False)
        self.edit_container.setVisible(True)
        self.text_edit.setFocus()
        self.adjustSize()

    def _on_delete(self) -> None:
        self._deleted = True
        self.accept()

    def was_deleted(self) -> bool:
        return self._deleted

    def note_text(self) -> str:
        return self.text_edit.toPlainText().strip()

    def remind_offset(self) -> str:
        return self.remind_combo.currentData() or "same"
