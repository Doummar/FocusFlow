from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from aqt.qt import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFontComboBox, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPushButton, QRadioButton, QScrollArea,
    QSlider, QSpinBox, QTabWidget,
    Qt, QVBoxLayout, QWidget, pyqtSignal,
)

from ..services.heatmap_service import HeatmapService
from ..utils.config_manager import _PROFILE_DEFAULTS
from .heatmap_widget import COLOR_SCHEME_LABELS, COLOR_SCHEMES


def _addon_version() -> str:
    """Read human_version from manifest.json, falling back gracefully."""
    try:
        manifest = Path(__file__).resolve().parent.parent / "manifest.json"
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("human_version", "?"))
    except Exception:
        return "?"


def _hline() -> QFrame:
    f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f



class _WeightSliderRow(QWidget):
    """Single labelled slider row matching the Aura answer-weight style.

    Shows:  Label ──── [slider] ──── ±N
    The value label is colour-coded: red (negative), green (positive), grey (zero).
    """
    valueChanged = pyqtSignal(int)

    def __init__(self, label: str, lo: int, hi: int, default: int,
                 parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(8)

        self._lbl = QLabel(label)
        self._lbl.setFixedWidth(52)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setMinimum(lo)
        self._slider.setMaximum(hi)
        self._slider.setValue(default)
        self._slider.setTickPosition(QSlider.TickPosition.NoTicks)

        self._val = QLabel(self._fmt(default), self)
        self._val.setFixedWidth(36)
        self._val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._val.setStyleSheet(self._css(default))

        row.addWidget(self._lbl)
        row.addWidget(self._slider, 1)
        row.addWidget(self._val)
        self._slider.valueChanged.connect(self._on_change)

    @staticmethod
    def _fmt(v: int) -> str:
        return f"+{v}" if v > 0 else str(v)

    @staticmethod
    def _css(v: int) -> str:
        colour = ("#c0392b" if v < 0 else "#2e7a40" if v > 0
                  else "var(--text-subtle,#888)")
        return f"color:{colour};font-weight:600;font-size:11px"

    def _on_change(self, v: int) -> None:
        self._val.setText(self._fmt(v))
        self._val.setStyleSheet(self._css(v))
        self.valueChanged.emit(v)

    def value(self) -> int:  return self._slider.value()
    def setValue(self, v: int) -> None: self._slider.setValue(v)


_PROFILE_KEYS = ("timer", "end_conditions", "fatigue", "toolbar", "sound")


# ── Sensitivity widget — native Qt ticks, no custom painting ─────────────────

class SensitivityWidget(QWidget):
    """Named 5-level preset for fatigue detection speed, replacing the old
    1-20 slider. A fine-grained numeric slider forces users to guess what
    a given number means; five plain-language levels are easier to pick
    confidently. Still stores/reads a plain int (1-20) under the hood so
    existing configs and the sensitivity-migration logic keep working."""
    valueChanged = pyqtSignal(int)

    _PRESETS = [
        ("Very Low",  3),
        ("Low",       7),
        ("Balanced", 10),
        ("High",     14),
        ("Very High", 18),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._combo = QComboBox(self)
        for label, val in self._PRESETS:
            self._combo.addItem(label, val)
        self._combo.setCurrentIndex(2)  # Balanced
        self._combo.currentIndexChanged.connect(self._on_change)
        layout.addWidget(self._combo, 1)

    def value(self) -> int:
        return int(self._combo.currentData())

    def setValue(self, v: int) -> None:
        # Snap to the closest preset rather than requiring an exact match,
        # so any int 1-20 saved by an older version still lands somewhere
        # sensible instead of silently failing to select anything.
        idx = min(range(len(self._PRESETS)),
                  key=lambda i: abs(self._PRESETS[i][1] - int(v)))
        self._combo.blockSignals(True)
        self._combo.setCurrentIndex(idx)
        self._combo.blockSignals(False)

    def _on_change(self, _index: int) -> None:
        self.valueChanged.emit(self.value())


# ── Fatigue weights widget ────────────────────────────────────────────────────


_GUIDE_SECTIONS: list[tuple[str, str]] = [
    ("Timer Toolbar", """
<p><b>What it does:</b> A small floating bar shows a colored dot, your timer, and how many
cards you have left &mdash; so you always know where you stand without opening anything.</p>
<p><b>How to use it:</b></p>
<ul>
<li>Click the timer once to start or pause it.</li>
<li>Press and hold the timer for about a second to reset it back to the start.</li>
<li>The dot changes color as you study: <b>green</b> means you're focused, <b>yellow</b> means
your focus is starting to drift, and <b>red</b> means your answers are dropping in quality.</li>
</ul>
<p><b>Choosing what the toolbar shows:</b> Toolbar &amp; Sound &rarr; Toolbar &rarr;
<b>Goal display</b> lets you pick how your due cards are shown:</p>
<ul>
<li><b>Remaining count</b> &mdash; just the number, e.g. <code>145 due</code></li>
<li><b>Progress bar</b> &mdash; a bar that fills up as you finish today's goal, e.g.
<code>&#x2588;&#x2588;&#x2588;&#x2588;&#x2588;&#x2588;&#x2591;&#x2591; 80%</code></li>
<li><b>Done / Remaining</b> &mdash; how many you've finished out of today's total, e.g.
<code>12 / 20 due</code></li>
</ul>
<p>This only works when exactly one Goal is turned on in the Timer tab. With two or more Goals
active at once, the toolbar always falls back to a plain list of remaining counts, since only
one of these can represent a single goal at a time.</p>
<p><b>Hiding it when you don't need it:</b> Toolbar &amp; Sound &rarr; Toolbar &rarr;
<b>HUD visibility</b> controls how visible that due-count area is: <b>Always visible</b> shows
it all the time, <b>Reveal on hover</b> keeps it faded out until you move your mouse over the
toolbar, and <b>Hidden</b> never shows it at all. The timer and the colored dot are not
affected by this setting either way.</p>"""),

    ("Heatmap", """
<p><b>What it does:</b> Each square on the grid is one day. The darker the square, the more
(or better) you studied that day &mdash; so you can see your study pattern at a glance.</p>
<p><b>How to use it:</b></p>
<ul>
<li>Click a day to see that day's stats in the panel below.</li>
<li>Click a week number to highlight that whole week and see its totals.</li>
<li>Click a month name to highlight that whole month.</li>
<li>Use the <b>&#x25C4; / &#x25BA;</b> arrows to move between years.</li>
<li>Right-click a day to add a short note or reminder for that date. If a note already
exists, it opens for reading first, with an <b>Edit</b> button, so a stray click can't change
it by accident. You can choose to be reminded on the day itself, or 1 day, 1 week, or 1 month
before &mdash; FocusFlow will show a small popup when Anki starts if a reminder is due.</li>
</ul>
<p>Before you click anything, the stat boxes below show a default view, which you can set in
Heatmap &rarr; Appearance &rarr; <b>Default Statistics View</b>: Today, Last 7 Days, Last 30
Days, or Remember Last Selection.</p>
<p>Light blue squares show cards you already have scheduled for future dates. You can turn
these off in Display &rarr; Heatmap Cells &rarr; <b>Future cells</b>.</p>
<p>To keep things fast, the heatmap only loads your last 3 years by default. If you want the
year arrows to reach all the way back to your very first review, turn on Heatmap &rarr;
Performance &rarr; <b>Show full history in heatmap navigation</b>. Nothing is ever lost by
leaving this off &mdash; it only limits how far back you can browse, not what's recorded.</p>"""),

    ("Customizing the Look", """
<p><b>What it does:</b> Heatmap &rarr; Appearance has a few purely visual options. None of
them change how your study data is measured or calculated &mdash; they only change how it
looks.</p>
<ul>
<li><b>Cell shape</b> &mdash; square, soft square (default), circle, diamond, or star.</li>
<li><b>Note marker</b> &mdash; the small icon shown on days with a note attached. You can
change its shape and which corner it sits in, and pick its color.</li>
<li><b>Label color</b> &mdash; the color used for weekday letters, month names, and week
numbers. Right-click the color swatch to go back to the default, which automatically adjusts
for light or dark mode on its own.</li>
<li><b>Color scheme</b> &mdash; six ready-made themes (Forest, Ocean, Ember, Rose, Mono,
Violet), or click the swatch next to the dropdown to build your own from any color.</li>
</ul>
<p>The glowing ring around today's cell automatically matches whichever color scheme you
choose &mdash; there's nothing extra to set for that.</p>"""),

    ("Rearranging the Panel", """
<p><b>What it does:</b> Lets you put the heatmap, Daily status, stat cards, and streak row in
whichever order you prefer.</p>
<p><b>How to use it:</b> Turn on Display &rarr; Layout &rarr; <b>Enable drag-to-reorder
layout</b>. A small &#x2630; grip appears on each of those four sections &mdash; drag one onto
another to swap their places. Your new order is saved automatically. Click <b>Reset layout
order</b> at any time to put everything back the way it started. If you turn the toggle back
off, the grips disappear but your last saved order is kept.</p>"""),

    ("Stats Panel", """
<p><b>What it does:</b> Shows a handful of numbers for whichever day, week, or month you've
selected on the heatmap.</p>
<table border="0" cellpadding="4">
<tr><td><b>Effective time</b></td><td>Your focused study time, adjusted for how tired you
were</td></tr>
<tr><td><b>Study Quality</b></td><td>How well you performed &mdash; see "Study Quality"
below</td></tr>
<tr><td><b>New cards</b></td><td>New cards you saw for the first time in this period</td></tr>
<tr><td><b>Reviews</b></td><td>How many times you answered a card in this period &mdash; a
card you saw twice counts twice</td></tr>
<tr><td><b>Cards Reviewed</b></td><td>How many different cards that covers &mdash; the same
card seen twice only counts once here</td></tr>
<tr><td><b>Review time</b></td><td>Total time spent reviewing, unadjusted</td></tr>
</table>
<p>The <b>streak row</b> shows your best streak, your current streak, and how many cards are
due today. A small tree icon grows as your current streak gets longer. You can show or hide
each part of this row separately under Display &rarr; Streak Row, instead of only being able
to turn the whole row on or off at once.</p>
<p>If Display &rarr; Heatmap Cells &rarr; <b>Show detail as pop-up</b> is turned on, clicking a
day opens a small floating card with that day's numbers instead of updating the panel below.
You can set how long it stays open right next to it, in <b>Auto-close pop-up</b> &mdash;
"Never" keeps it open until you close it yourself or click another day.</p>"""),

    ("Fatigue Tracking", """
<p><b>What it does:</b> FocusFlow pays attention to <i>how</i> you're answering, not just how
many cards you get through. It watches for a few signs that you might be getting tired:</p>
<ul>
<li>You're answering more slowly than usual.</li>
<li>Your answer speed is bouncing around &mdash; sometimes fast, sometimes very slow.</li>
<li>You're pressing Again more often than usual.</li>
</ul>
<p>These three signs are combined into a single score. No one sign decides it alone, and
nothing is hidden from you &mdash; you can see and turn off any of the three under Fatigue
&rarr; <b>What to watch for</b> (visible in Expert mode). Turning one off simply stops it from
affecting the score.</p>
<p>When FocusFlow notices these signs, the toolbar's colored dot changes and, if you'd like, it
can suggest a short break. If breaks get suggested too often (or not often enough), adjust
Fatigue &rarr; <b>Detection speed</b>.</p>"""),

    ("Study Quality", """
<p><b>What it does:</b> Study Quality is a single score meant to capture how well a study
session actually went, not just how many cards you got through. You can choose how it's
calculated in Measurement &rarr; Step 1:</p>
<ul>
<li><b>Focus score</b> (Recommended) &mdash; based on how alert and steady you were. A short,
focused session can score higher than a long, distracted one. This is the same combination of
signals described in Fatigue Tracking above.</li>
<li><b>Answer buttons</b> &mdash; a weighted average of how often you pressed Again, Hard,
Good, and Easy. You can adjust the weights in Step 2, right below it.</li>
<li><b>Retention rate</b> &mdash; simply how many cards you passed vs. failed. The simplest and
most direct option.</li>
</ul>
<p>Focus score needs no setup and works well for most people, but all three are equally valid
&mdash; pick whichever one matches how you think about your own studying.</p>"""),

    ("All Settings", """
<table border="0" cellpadding="4">
<tr><td><b>Timer</b></td><td>Study/break lengths and your daily goals</td></tr>
<tr><td><b>Fatigue</b></td><td>How quickly tiredness is detected, and which signs to watch
for</td></tr>
<tr><td><b>Heatmap</b></td><td>Cell shape and color, note markers, and heatmap
performance</td></tr>
<tr><td><b>Display</b></td><td>Which heatmap, stat-card, and streak-row elements are shown,
plus fonts and layout order</td></tr>
<tr><td><b>Toolbar &amp; Sound</b></td><td>Toolbar position, what the due-count area shows,
HUD visibility, and notification sounds</td></tr>
<tr><td><b>Measurement</b></td><td>How Study Quality is calculated</td></tr>
</table>
<p>Click <b>Save</b> to apply your changes, or <b>Cancel</b> to discard them.</p>"""),

    ("Common Questions", """
<p><b>Break suggestions come too often.</b> Lower Detection speed in the Fatigue tab, or turn
off the "Speed consistency" signal.</p>
<p><b>The Study Quality score seems off.</b> Open Measurement &rarr; Step 1 and try switching
to Retention rate for a simpler, more direct number.</p>
<p><b>The due-today count doesn't match Anki's own numbers.</b> FocusFlow adds up new, learning,
and review cards together, the same three columns Anki itself shows.</p>
<p><b>The deck browser feels slow.</b> Check Heatmap &rarr; Performance. If "Show full history
in heatmap navigation" is turned on, the heatmap re-renders every year you've ever studied,
every time it refreshes. Turning it off (the default) keeps it to the last 3 years, which is
the fastest setting if you have several years of history.</p>
<p><b>The heatmap isn't showing up at all.</b> Try a clean reinstall: hold Shift while opening
Anki, remove FocusFlow, close Anki, reopen it normally, then reinstall FocusFlow.</p>"""),
]


class _GuideSection(QWidget):
    """A single collapsible section for the Help tab: a flat header row with
    a disclosure arrow, revealing a rich-text body underneath. Styled to sit
    quietly inside Anki's own look — semantic palette roles so it follows
    the user's light/dark theme, a restrained left-border accent for the
    active state instead of a solid color fill, and a thin native QFrame
    divider between sections (see GuideDialog) rather than heavy spacing."""

    _ACCENT = "#5b84c4"   # the one modest blue used for any active/interactive cue

    def __init__(self, title: str, body_html: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)

        self._header = QPushButton(f"\u25B6  {title}", self)
        self._header.setCheckable(True)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(self._header_style(False))
        self._header.toggled.connect(self._on_toggled)
        lay.addWidget(self._header)

        self._body = QLabel(
            f'<div style="line-height:150%;">{body_html}</div>', self)
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.TextFormat.RichText)
        self._body.setOpenExternalLinks(True)
        self._body.setContentsMargins(17, 8, 12, 14)
        self._body.setStyleSheet("background:transparent;")
        self._body.setVisible(False)
        lay.addWidget(self._body)

        self._title = title

    def _on_toggled(self, checked: bool) -> None:
        arrow = "\u25BC" if checked else "\u25B6"
        self._header.setText(f"{arrow}  {self._title}")
        self._header.setStyleSheet(self._header_style(checked))
        self._body.setVisible(checked)

    @classmethod
    def _header_style(cls, active: bool) -> str:
        if active:
            return (
                "QPushButton{text-align:left;padding:7px 10px 7px 8px;"
                f"border:none;border-left:3px solid {cls._ACCENT};"
                "background:palette(alternate-base);color:palette(text);"
                "font-weight:600;font-size:11px;}"
            )
        return (
            "QPushButton{text-align:left;padding:7px 10px 7px 11px;"
            "border:none;background:transparent;color:palette(text);"
            "font-weight:600;font-size:11px;}"
            "QPushButton:hover{background:palette(alternate-base);}"
        )


class GuideDialog(QDialog):
    """Standalone Guide window — a separate floating dialog (its own
    titlebar, own Open/Close lifecycle) rather than a tab buried inside
    Settings, mirroring the "Aura - Guide" popup pattern: header, "Click
    any section to expand" accordion, Open Settings + Got it buttons,
    version/author footer."""

    open_settings_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FocusFlow - Guide")
        self.setMinimumSize(460, 600)
        self.resize(460, 620)

        root = QVBoxLayout(self)

        heading = QLabel("<b>FocusFlow: Focus Timer &amp; Heatmap</b>", self)
        heading.setStyleSheet("font-size:13px;")
        root.addWidget(heading)

        sub = QLabel("Click any section to expand.", self)
        sub.setStyleSheet("color:#9a9a9a;font-size:10px;margin-bottom:4px;")
        root.addWidget(sub)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget(scroll)
        inner_l = QVBoxLayout(inner)
        inner_l.setContentsMargins(0, 0, 0, 0); inner_l.setSpacing(0)
        for i, (_title, _body) in enumerate(_GUIDE_SECTIONS):
            if i > 0:
                inner_l.addWidget(_hline())
            inner_l.addWidget(_GuideSection(_title, _body, inner))
        inner_l.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)   # stretch=1: the scroll area absorbs
                                    # extra space, NOT the footer below it —
                                    # without this the footer/lines could
                                    # get pushed past the visible bottom
                                    # edge on a short window.

        root.addWidget(_hline())
        btn_row = QHBoxLayout()
        open_settings_btn = QPushButton("Open Settings", self)
        open_settings_btn.clicked.connect(self._open_settings)
        btn_row.addWidget(open_settings_btn)
        btn_row.addStretch()
        got_it_btn = QPushButton("Got it  \u2713", self)
        got_it_btn.setDefault(True)
        got_it_btn.setStyleSheet(
            "QPushButton{background:#5b84c4;color:white;font-weight:600;padding:5px 14px;}"
            "QPushButton:hover{background:#4a70ac;}")
        got_it_btn.clicked.connect(self.accept)
        btn_row.addWidget(got_it_btn)
        root.addLayout(btn_row)
        root.addWidget(_hline())

        footer_row = QHBoxLayout()
        footer_left = QLabel(f"FocusFlow v{_addon_version()} \u2014 Created by Adel", self)
        footer_left.setStyleSheet("color:#9a9a9a;font-size:9px;margin-top:2px;")
        footer_left.setMinimumHeight(16)
        footer_row.addWidget(footer_left)
        footer_row.addStretch()
        footer_right = QLabel("Since 2026", self)
        footer_right.setStyleSheet("color:#9a9a9a;font-size:9px;margin-top:2px;")
        footer_row.addWidget(footer_right)
        root.addLayout(footer_row)

        # BUG FIX: on some systems the window was opening shorter than the
        # button row + lines + footer needed, silently clipping them off
        # the bottom edge. SetMinimumSize ties the dialog's minimum size to
        # the layout's actual computed minimum (all children included), so
        # it can never be resized — by us or the user — below what's needed
        # to show every widget, footer included.
        root.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)

    def _open_settings(self) -> None:
        self.open_settings_requested.emit()
        self.accept()


# ── Main settings dialog ──────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    changed  = pyqtSignal(object)
    saved    = pyqtSignal(object)
    test_sound_requested     = pyqtSignal()
    manual_session_requested = pyqtSignal()

    _TIER_RANK = {"beginner": 0, "basic": 1, "intermediate": 2, "advanced": 3, "expert": 4}
    _TIER_LABELS = ["Beginner", "Basic", "Intermediate", "Advanced", "Expert"]

    def __init__(
        self,
        config: dict[str, Any],
        heatmap_service: HeatmapService | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("FocusFlow Settings")
        self.setMinimumWidth(520)
        # heatmap_service may be None when the dialog is opened before the
        # first Anki profile loads (e.g. from the Tools menu at startup).
        # NOTE: nothing in this class currently reads self._heatmap_service —
        # it's accepted and stored for a future heatmap-aware control (e.g.
        # validating date ranges or previewing live stats against real data)
        # but no such control exists yet. If one is added, guard every access
        # with `if self._heatmap_service is not None` since this can be None.
        self._heatmap_service = heatmap_service
        self._config: dict[str, Any] = {}
        self._config_backup: dict[str, Any] = {}  # snapshot for Cancel revert
        self._loading = False
        self._layout_block_order: list[str] = ["heatmap", "progress", "cards", "streak"]

        # No custom stylesheet — Anki's platform theme handles everything.
        # GroupBox border only, using palette() for light/dark compatibility.
        self.setStyleSheet(
            "QGroupBox{font-weight:600;border:1px solid palette(mid);"
            "border-radius:4px;margin-top:8px;padding-top:10px}"
            "QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px}"
        )
        self._build()
        self.load_values(config)

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)

        # ── Experience mode ──────────────────────────────────────────────────
        # Tracks which tabs/groups require which minimum tier to be visible.
        # Each level shows everything the previous level shows, plus more:
        #   Beginner     — Timer, Heatmap, Streak, Theme (essentials only)
        #   Basic        — + Goals, basic display options
        #   Intermediate — + Fatigue detection, Measurement method
        #   Advanced     — + Heatmap customization, detailed statistics/display
        #   Expert       — + detection signals, weight tuning, hidden/debug options
        self._tier_tabs: dict[int, str] = {}                 # tab index -> min tier
        self._tier_widgets: dict[str, list[QWidget]] = {
            tier: [] for tier in self._TIER_RANK if tier != "beginner"
        }  # min tier -> widgets needing at least that tier (beginner = always visible)
        self._TIER_BLURBS = {
            "beginner":     "Just the essentials: Timer, Heatmap, Streak, Theme.",
            "basic":        "Adds Goals (end conditions) and basic display options.",
            "intermediate": "Adds Fatigue detection and the Measurement method.",
            "advanced":     "Adds Heatmap customization and detailed statistics.",
            "expert":       "Adds detection signals, weight tuning, and hidden/debug options.",
        }

        exp_card = QFrame(self)
        exp_card.setObjectName("ffExperienceCard")
        exp_card.setStyleSheet(
            "#ffExperienceCard{background:palette(alternate-base);"
            "border:1px solid palette(mid);border-radius:8px;padding:2px}")
        exp_card_l = QVBoxLayout(exp_card)
        exp_card_l.setContentsMargins(10, 8, 10, 8)
        exp_card_l.setSpacing(2)
        exp_row = QHBoxLayout()
        exp_title = QLabel("Experience level", exp_card)
        exp_title.setStyleSheet("font-weight:600")
        exp_row.addWidget(exp_title)
        self.experience_combo = QComboBox(self)
        for _label in self._TIER_LABELS:
            _text = f"{_label} (Recommended)" if _label == "Beginner" else _label
            self.experience_combo.addItem(_text, _label.lower())
        self.experience_combo.setToolTip(
            "Beginner — Timer, Heatmap, Streak, Theme. Just the essentials.\n"
            "Basic — adds Goals and basic display options.\n"
            "Intermediate — adds Fatigue detection and Measurement method.\n"
            "Advanced — adds Heatmap customization and detailed statistics.\n"
            "Expert — adds detection signals, weight tuning, and hidden/debug options.")
        self.experience_combo.currentIndexChanged.connect(self._on_experience_changed)
        exp_row.addWidget(self.experience_combo, 1)
        exp_card_l.addLayout(exp_row)
        self.experience_blurb = QLabel("", exp_card)
        self.experience_blurb.setWordWrap(True)
        self.experience_blurb.setStyleSheet("font-size:10px;color:palette(placeholder-text)")
        exp_card_l.addWidget(self.experience_blurb)
        root.addWidget(exp_card)

        tabs = QTabWidget(self)

        # ── Tab 1: Timer & Profile ────────────────────────────────────────────
        t1 = QWidget(); t1l = QVBoxLayout(t1); t1l.setSpacing(8)

        pg = QGroupBox("Profile", t1); proot = QVBoxLayout(pg)
        prow = QHBoxLayout()
        self.profile_combo = QComboBox(pg)
        self.profile_combo.currentTextChanged.connect(self._profile_selected)
        self.btn_new    = QPushButton("New",       pg)
        self.btn_rename = QPushButton("Rename",    pg)
        self.btn_dupe   = QPushButton("Duplicate", pg)
        self.btn_delete = QPushButton("Delete",    pg)
        self.btn_new.clicked.connect(self._create_profile)
        self.btn_rename.clicked.connect(self._rename_profile)
        self.btn_dupe.clicked.connect(self._duplicate_profile)
        self.btn_delete.clicked.connect(self._delete_profile)
        prow.addWidget(self.profile_combo, 1)
        for b in (self.btn_new, self.btn_rename, self.btn_dupe, self.btn_delete):
            prow.addWidget(b)
        proot.addLayout(prow); t1l.addWidget(pg)
        # Removed from the visible UI — multiple timer profiles (Vocabulary,
        # Reading, Kanji, ...) added complexity most people never touch.
        # Widgets are still built and wired above so the rest of this file
        # (load/save/switch logic) keeps working unchanged against a single
        # silent "Default" profile — only the group's visibility changes.
        pg.setVisible(False)

        tg = QGroupBox("Timer", t1); tf = QFormLayout(tg)
        self.focus_minutes      = QDoubleSpinBox(tg); self.focus_minutes.setRange(1,240); self.focus_minutes.setDecimals(1); self.focus_minutes.setSuffix(" min")
        self.break_minutes      = QDoubleSpinBox(tg); self.break_minutes.setRange(1,90);  self.break_minutes.setDecimals(1); self.break_minutes.setSuffix(" min")
        self.long_break_minutes = QDoubleSpinBox(tg); self.long_break_minutes.setRange(1,120); self.long_break_minutes.setDecimals(1); self.long_break_minutes.setSuffix(" min")
        self.sessions_before_long = QSpinBox(tg); self.sessions_before_long.setRange(1,12)
        self.auto_resume = QCheckBox("Resume when returning from card editor", tg)
        self.break_stay_on_top = QComboBox(tg)
        self.break_stay_on_top.addItem("Always on top", True)
        self.break_stay_on_top.addItem("Normal window", False)
        self.break_stay_on_top.setToolTip(
            "Whether the break timer popup stays above other windows while a "
            "break is running, or behaves like a normal window instead.")
        tf.addRow("Study duration", self.focus_minutes)
        tf.addRow("Short break",    self.break_minutes)
        tf.addRow("Long break",     self.long_break_minutes)
        tf.addRow("Sessions before long break", self.sessions_before_long)
        tf.addRow("", self.auto_resume)
        tf.addRow("Break popup",    self.break_stay_on_top); t1l.addWidget(tg)

        cg = QGroupBox("End Conditions", t1); cf = QFormLayout(cg)
        self.cards_enabled = QCheckBox("Cards target",         cg); self.cards_target  = QSpinBox(cg);          self.cards_target.setRange(1,9999)
        self.sessions_en   = QCheckBox("Session count",        cg); self.sessions_tgt  = QSpinBox(cg);          self.sessions_tgt.setRange(1,99)
        self.max_time_en   = QCheckBox("Max study time",       cg); self.max_time_mins = QDoubleSpinBox(cg);     self.max_time_mins.setRange(1,240); self.max_time_mins.setDecimals(1); self.max_time_mins.setSuffix(" min")
        self.all_due_en    = QCheckBox("All due cards finished",cg)
        cf.addRow(self.cards_enabled, self.cards_target)
        cf.addRow(self.sessions_en,   self.sessions_tgt)
        cf.addRow(self.max_time_en,   self.max_time_mins)
        cf.addRow("", self.all_due_en); t1l.addWidget(cg)
        self._tier_widgets["basic"].append(cg)
        t1l.addStretch(); tabs.addTab(t1, "Timer")

        # ── Tab 2: Fatigue ────────────────────────────────────────────────────
        t2 = QWidget()
        t2_scroll = QScrollArea(); t2_scroll.setWidgetResizable(True)
        t2_scroll.setFrameShape(t2_scroll.Shape.NoFrame)
        t2_inner = QWidget(); t2l = QVBoxLayout(t2_inner); t2l.setSpacing(10)
        t2_scroll.setWidget(t2_inner)
        t2_outer = QVBoxLayout(t2); t2_outer.setContentsMargins(0,0,0,0)
        t2_outer.addWidget(t2_scroll)

        # ── Plain-language intro banner ───────────────────────────────────────
        _fat_intro = QLabel(
            "<b>What is Focus Tracking?</b><br>"
            "FocusFlow watches how you answer cards as you study. "
            "When your speed slows down or you start pressing Again more often, "
            "it means your brain is getting tired — even if you don't feel it yet. "
            "FocusFlow will gently suggest a break at the right moment."
        )
        _fat_intro.setWordWrap(True)
        _fat_intro.setStyleSheet(
            "background:rgba(80,120,220,0.08);border-radius:6px;"
            "padding:8px 10px;font-size:11px;color:palette(text)")
        t2l.addWidget(_fat_intro)

        # ── How fast to detect ────────────────────────────────────────────────
        fg = QGroupBox("How quickly to detect tiredness", t2_inner)
        ff = QFormLayout(fg)
        self.sensitivity = SensitivityWidget(t2_inner)
        self.sensitivity.setToolTip(
            "Very Low / Low: Only suggest a break when you are clearly exhausted\n"
            "Balanced: catches tiredness before it hurts recall (recommended)\n"
            "High / Very High: Very alert — suggests breaks at the first sign of drift\n\n"
            "Tip: Start at Balanced. If breaks come too often, lower it.")
        ff.addRow("Detection speed", self.sensitivity)

        self.min_cards = QSpinBox(fg)
        self.min_cards.setRange(5, 30)
        self.min_cards.setSuffix(" cards")
        self.min_cards.setToolTip(
            "FocusFlow needs a few cards to learn your normal speed before it\n"
            "can detect when you're slowing down. Lower = detects sooner but\n"
            "may be less accurate. Default (10) works well for most people.")
        ff.addRow("Warm-up cards before tracking", self.min_cards)

        self.break_score = QSpinBox(fg)
        self.break_score.setRange(20, 60)
        self.break_score.setSuffix(" %")
        self.break_score.setToolTip(
            "Suggest a break when your focus score drops below this level.\n"
            "Example: 40 % means FocusFlow waits until you've lost 60 % of\n"
            "your peak focus. Lower = fewer interruptions. Higher = earlier breaks.")
        ff.addRow("Suggest break when focus falls below", self.break_score)
        t2l.addWidget(fg)

        # ── What to track ─────────────────────────────────────────────────────
        sg = QGroupBox("What to watch for  (uncheck anything that fires too often)", t2_inner)
        sf = QVBoxLayout(sg); sf.setSpacing(4)
        self.sig_rt    = QCheckBox("Answer speed — am I getting slower?", sg)
        self.sig_rt.setToolTip(
            "Compares your current average answer time to your fresh-session baseline.\n"
            "The most reliable signal for most study decks.")
        self.sig_iiv   = QCheckBox("Speed consistency — are my times getting erratic?", sg)
        self.sig_iiv.setToolTip(
            "Detects when your answer times become uneven (some very fast, some very slow).\n"
            "This is often the first sign of mental fatigue. Disable if it fires too often.")
        self.sig_again = QCheckBox("Mistake rate — am I pressing Again more?", sg)
        self.sig_again.setToolTip(
            "Tracks whether you're failing cards more than usual.\n"
            "Useful for decks where accuracy matters more than speed.")
        self.sig_lapse = QCheckBox("Single slow response — did one card take much longer than usual?", sg)
        self.sig_lapse.setToolTip(
            "Fires when a single answer takes much longer than your normal pace.\n"
            "Can indicate zoning out or losing concentration momentarily.")
        for cb in (self.sig_rt, self.sig_iiv, self.sig_again, self.sig_lapse):
            sf.addWidget(cb)
        t2l.addWidget(sg)
        self._tier_widgets["expert"].append(sg)

        # ── What happens when tired ───────────────────────────────────────────
        bg = QGroupBox("What happens when tiredness is detected", t2_inner)
        bf = QFormLayout(bg)
        self.dot_radio = QRadioButton("Colour dot  (green → yellow → red)", bg)
        self.pct_radio = QRadioButton("Percentage  (100 % = fully focused)", bg)
        self.dot_radio.setChecked(True)
        _ind_row = QHBoxLayout()
        _ind_row.addWidget(self.dot_radio)
        _ind_row.addWidget(self.pct_radio)
        _ind_row.addStretch()
        bf.addRow("Show focus level as", _ind_row)
        self.soft_prompt = QCheckBox("Pop up a break reminder", bg)
        self.soft_prompt.setToolTip("Shows a gentle reminder to take a break when focus drops below the threshold above.")
        self.adaptive_duration = QCheckBox("Automatically shorten the next study session when tired", bg)
        self.adaptive_duration.setToolTip(
            "If you're tired at the end of a Pomodoro, the next one is shorter\n"
            "so you recover without stopping completely.")
        self.show_daily_report = QCheckBox("Show a summary at the end of each study session", bg)
        bf.addRow("", self.soft_prompt)
        bf.addRow("", self.adaptive_duration)
        bf.addRow("", self.show_daily_report)
        t2l.addWidget(bg)
        t2l.addStretch(); _idx2 = tabs.addTab(t2, "Fatigue")
        self._tier_tabs[_idx2] = "intermediate"

        # ── Tab 3: Heatmap ────────────────────────────────────────────────────
        t3 = QWidget(); t3l = QVBoxLayout(t3); t3l.setSpacing(8)

        hmg = QGroupBox("Appearance", t3); hmf = QFormLayout(hmg)
        self.hm_theme = QComboBox(hmg)
        self.hm_theme.addItem("Follow System", "system")
        self.hm_theme.addItem("Light", "light")
        self.hm_theme.addItem("Dark", "dark")
        self.hm_theme.setToolTip(
            "Which colours the heatmap panel uses \u2014 independent of Anki's own "
            "theme setting.\n\u201cFollow System\u201d (default) matches whatever Anki "
            "is currently using. If the panel ever looks wrong for your setup, picking "
            "Light or Dark here forces it regardless.")
        hmf.addRow("Theme", self.hm_theme)
        self.hm_cell_size = QComboBox(hmg)
        self.hm_cell_size.addItems(["Small (9px)", "Medium (12px)", "Large (15px)", "Extra Large (18px)"])
        self.hm_cell_shape = QComboBox(hmg)
        self.hm_cell_shape.addItem("Soft square (default)", "soft")
        self.hm_cell_shape.addItem("Sharp square",          "sharp")
        self.hm_cell_shape.addItem("Star",                  "star")
        self.hm_cell_shape.addItem("Circle",                "circle")
        self.hm_cell_shape.addItem("Diamond",                "diamond")
        self.hm_cell_shape.setToolTip(
            "The silhouette of each heatmap cell \u2014 purely visual, doesn't\n"
            "change how any data is read or coloured.")

        self.hm_note_shape = QComboBox(hmg)
        self.hm_note_shape.addItem("Triangle (default)", "triangle")
        self.hm_note_shape.addItem("Dot",                 "dot")
        self.hm_note_shape.addItem("Square",               "square")
        self.hm_note_shape.addItem("Ring",                 "ring")
        self.hm_note_position = QComboBox(hmg)
        self.hm_note_position.addItem("Bottom right (default)", "bottom_right")
        self.hm_note_position.addItem("Bottom left",             "bottom_left")
        self.hm_note_position.addItem("Top right",               "top_right")
        self.hm_note_position.addItem("Top left",                "top_left")
        self.hm_note_position.addItem("Center",                  "center")
        self.hm_note_position.setToolTip(
            "Only applies to Dot/Square/Ring \u2014 Triangle always sits in a corner,\n"
            "so \u201cCenter\u201d falls back to bottom right for that shape.")
        self._note_color = ""   # "" = default gold
        self.swatch_note = QPushButton("", self)
        self.swatch_note.setFixedSize(18, 18)
        self.swatch_note.setToolTip(
            "Click to choose a custom colour for the note marker.\n"
            "Right-click to reset to the default gold.")
        self.swatch_note.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch_note.clicked.connect(self._pick_note_color)
        self.swatch_note.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.swatch_note.customContextMenuRequested.connect(self._reset_note_color)
        self._style_swatch(self.swatch_note, "#E8B84D")
        _note_row = QHBoxLayout()
        _note_row.setSpacing(6)
        _note_row.addWidget(self.hm_note_shape, 1)
        _note_row.addWidget(self.hm_note_position, 1)
        _note_row.addWidget(self.swatch_note)

        self._label_color = ""   # "" = deterministic theme default
        self.disp_custom_label = QCheckBox("Label colour", self)
        self.disp_custom_label.setToolTip(
            "Off (default): weekday / month / week-number labels automatically use a "
            "readable colour for light or dark mode.\n"
            "On: pick one fixed colour for those labels instead.")
        self.swatch_label = QPushButton("", self)
        self.swatch_label.setFixedSize(18, 18)
        self.swatch_label.setToolTip("Click to choose a custom colour for the calendar labels.")
        self.swatch_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch_label.clicked.connect(self._pick_label_color)
        self._style_swatch(self.swatch_label, "#888888")
        self.swatch_label.setEnabled(False)
        self.disp_custom_label.toggled.connect(self.swatch_label.setEnabled)
        row_label_color = self._checkbox_with_swatch(self.disp_custom_label, self.swatch_label)
        self.hm_color_scheme = QComboBox(hmg)
        for key, lbl in COLOR_SCHEME_LABELS.items():
            self.hm_color_scheme.addItem(lbl, key)
        self.hm_color_scheme.addItem("Custom \u2014 pick any colour", "custom")
        # Full-spectrum picker for the "Custom" scheme — opens QColorDialog
        # with no restriction to the 6 built-in presets. Presets stay as the
        # recommended/best options in the combo above; this just adds a way
        # to use literally any colour instead of being limited to them.
        self._custom_scheme_color = "#5284C8"
        self.swatch_scheme = QPushButton("", hmg)
        self.swatch_scheme.setFixedSize(18, 18)
        self.swatch_scheme.setToolTip(
            "Pick any colour from the full spectrum for a custom heatmap gradient.\n"
            "Selecting a colour here switches the scheme above to \u201cCustom\u201d.")
        self.swatch_scheme.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch_scheme.clicked.connect(self._pick_scheme_color)
        self._style_swatch(self.swatch_scheme, self._custom_scheme_color)
        self.hm_color_scheme.currentIndexChanged.connect(self._sync_scheme_swatch)
        scheme_row = QHBoxLayout()
        scheme_row.setSpacing(6)
        scheme_row.addWidget(self.hm_color_scheme, 1)
        scheme_row.addWidget(self.swatch_scheme)
        self.hm_grouping = QComboBox(hmg)
        self.hm_grouping.addItem("Continuous year",  "year")
        self.hm_grouping.addItem("Monthly blocks",   "monthly")
        self.hm_grouping.addItem("Weekly blocks",    "weekly")
        self.hm_color_metric = QComboBox(hmg)
        for val, lbl in [("time","Review Time"),("reviews","Reviews"),("quality","Study Quality")]:
            self.hm_color_metric.addItem(lbl, val)
        self.hm_default_view = QComboBox(hmg)
        self.hm_default_view.addItem("Today",                   "today")
        self.hm_default_view.addItem("Last 7 Days",              "7days")
        self.hm_default_view.addItem("Last 30 Days",             "30days")
        self.hm_default_view.addItem("Remember Last Selection",  "remember")
        self.hm_default_view.setToolTip(
            "What the 5 stats boxes below the heatmap show before you've\n"
            "clicked a specific day, week, or month this session.\n"
            "\"Remember Last Selection\" restores whatever you last clicked,\n"
            "even after restarting Anki.")
        self.hide_native_stats = QCheckBox("Hide Anki's own \"Studied N cards...\" line", hmg)
        self.hide_native_stats.setToolTip(
            "Hides the default \"Studied N cards in M minutes today (Xs/card)\" text\n"
            "that Anki shows above the heatmap, since FocusFlow's own stats cover it.")
        self.hide_native_stats.toggled.connect(self._auto_save)
        # BUG FIX: how far ahead the blue "due" forecast cells extend used to
        # be hardcoded to 30 days with no way to see or click further out —
        # cards due more than a month from now just showed as plain,
        # uncoloured, unclickable cells even though the data to forecast
        # them was already one query away. Now user-configurable.
        self.hm_due_forecast_days = QSpinBox(hmg)
        self.hm_due_forecast_days.setRange(1, 365)
        self.hm_due_forecast_days.setSuffix(" days")
        self.hm_due_forecast_days.setToolTip(
            "How far past today the heatmap forecasts \"due\" cells (the blue\n"
            "cells after today, showing cards scheduled to come due) and lets\n"
            "you click one to see which cards those are. Cards due further out\n"
            "than this still exist and get reviewed on schedule as normal —\n"
            "this only controls how far ahead the heatmap bothers looking.")
        # Companion to the spinbox above: rather than picking a number,
        # just forecast as far out as Anki's own scheduler ever goes.
        # Still only reaches as far as whichever year's grid is on screen —
        # flip forward with ▶ to see further-out due cards for later years,
        # same as a physical calendar.
        self.hm_due_forecast_unlimited = QCheckBox("Show all future due cards (ignore the limit above)", hmg)
        self.hm_due_forecast_unlimited.setToolTip(
            "Forecasts due cells as far ahead as Anki's scheduler ever reaches,\n"
            "instead of stopping at the day count set above. Navigating to a\n"
            "later year (▶) will also show its due cards fully, not just the\n"
            "current one.")
        self.hm_due_forecast_unlimited.toggled.connect(self.hm_due_forecast_days.setDisabled)
        self.hm_due_forecast_unlimited.toggled.connect(self._auto_save)
        hmf.addRow("Cell size",     self.hm_cell_size)
        hmf.addRow("Cell shape",    self.hm_cell_shape)
        hmf.addRow("Note marker",   _note_row)
        hmf.addRow("", row_label_color)
        hmf.addRow("Colour scheme", scheme_row)
        hmf.addRow("Grouping",      self.hm_grouping)
        hmf.addRow("Colour metric", self.hm_color_metric)
        hmf.addRow("Default Statistics View", self.hm_default_view)
        hmf.addRow("Forecast ahead", self.hm_due_forecast_days)
        hmf.addRow("", self.hm_due_forecast_unlimited)
        hmf.addRow("", self.hide_native_stats)
        t3l.addWidget(hmg)

        thg = QGroupBox("Workload thresholds", t3); thf = QFormLayout(thg)
        self.heavy_cards = QSpinBox(thg); self.heavy_cards.setRange(1,9999); self.heavy_cards.setSuffix(" cards")
        self.light_cards = QSpinBox(thg); self.light_cards.setRange(1,9999); self.light_cards.setSuffix(" cards")
        thf.addRow("Heavy day ≥", self.heavy_cards)
        thf.addRow("Light day ≤", self.light_cards)
        note2 = QLabel("Days between thresholds are shown as Moderate.")
        note2.setStyleSheet("font-size:10px;font-style:italic")
        thf.addRow("", note2); t3l.addWidget(thg)
        self._tier_widgets["advanced"].append(thg)

        # ── Performance ──────────────────────────────────────────────────────
        # Deliberately NOT tier-gated (unlike most of this tab's other
        # groups) — heaviness can affect anyone regardless of experience
        # level, so this stays visible and discoverable at every tier.
        pg = QGroupBox("Performance", t3); pgl = QVBoxLayout(pg)
        self.hm_full_history = QCheckBox("Show full history in heatmap navigation", pg)
        self.hm_full_history.setToolTip(
            "Off (default): the heatmap only pre-renders the last 3 years, which\n"
            "is what makes the deck browser panel fast to refresh. On: every year\n"
            "since your first-ever review renders too, so \u25c0/\u25b6 can browse your\n"
            "entire history \u2014 heavier, and gets slower the longer you've used Anki.")
        pgl.addWidget(self.hm_full_history)
        _perf_hint = QLabel(
            "Off keeps the panel light by only pre-rendering the last 3 years. "
            "Turn this on if you want \u25c0/\u25b6 to reach all the way back to your "
            "first-ever review \u2014 at the cost of a heavier, slower-to-refresh panel.")
        _perf_hint.setWordWrap(True)
        _perf_hint.setStyleSheet("font-size:10px;color:palette(placeholder-text)")
        pgl.addWidget(_perf_hint)
        t3l.addWidget(pg)

        t3l.addStretch(); tabs.addTab(t3, "Heatmap")

        # ── Display tab (split out from Heatmap — was one long crowded tab) ────
        # Two-column layout: the four content sections (Heatmap Cells, Stats
        # Cards, Streak Row, Appearance) sit side by side in balanced columns
        # instead of one long stacked column, with Layout as a compact
        # full-width strip at the bottom. Rows that combined a checkbox with
        # a second inline control (auto-close timer, progress-bar height/
        # width, font family+size) are split into an indented sub-row under
        # their parent — that's what was forcing the old single-column width
        # up past what two side-by-side columns can fit. Still wrapped in a
        # QScrollArea (same pattern as the Fatigue tab) as a safety net for
        # small windows, though two columns need far less height than one.
        t3b = QWidget()
        t3b_scroll = QScrollArea(); t3b_scroll.setWidgetResizable(True)
        t3b_scroll.setFrameShape(t3b_scroll.Shape.NoFrame)
        t3b_inner = QWidget(); t3bl = QVBoxLayout(t3b_inner); t3bl.setSpacing(6)
        t3bl.setContentsMargins(8, 6, 8, 6)
        t3b_scroll.setWidget(t3b_inner)
        t3b_outer = QVBoxLayout(t3b); t3b_outer.setContentsMargins(0, 0, 0, 0)
        t3b_outer.addWidget(t3b_scroll)

        # ── Heatmap Cells ────────────────────────────────────────────────────
        hcg = QGroupBox("Heatmap Cells", t3b_inner)
        hcgv = QVBoxLayout(hcg); hcgv.setSpacing(2)
        hcgv.setContentsMargins(9, 6, 9, 6)

        self.disp_streak_lines = QCheckBox("Streak lines")
        self.disp_streak_lines.setToolTip(
            "Draw a vertical line through cells that form a consecutive study streak")
        self._streak_color = ""   # "" = theme-adaptive default (no override)
        self.swatch_streak = QPushButton("", self)
        self.swatch_streak.setFixedSize(18, 18)
        self.swatch_streak.setToolTip(
            "Click to choose a custom colour for the streak lines.\n"
            "Right-click to reset to the theme-adaptive default.")
        self.swatch_streak.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch_streak.clicked.connect(self._pick_streak_color)
        self.swatch_streak.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.swatch_streak.customContextMenuRequested.connect(self._reset_streak_color)
        self._style_swatch(self.swatch_streak, "#8C8C8C")
        self.hm_streak_thickness = QComboBox(self)
        self.hm_streak_thickness.addItem("Thin",   "thin")
        self.hm_streak_thickness.addItem("Normal", "normal")
        self.hm_streak_thickness.addItem("Thick",  "thick")
        self.hm_streak_thickness.setToolTip("How thick the streak lines are drawn.")
        row_streak_lines = QWidget(self)
        _rsl = QHBoxLayout(row_streak_lines)
        _rsl.setContentsMargins(0, 0, 0, 0); _rsl.setSpacing(6)
        _rsl.addWidget(self.disp_streak_lines)
        _rsl.addWidget(self.swatch_streak)
        _rsl.addWidget(self.hm_streak_thickness)
        _rsl.addStretch()
        hcgv.addWidget(row_streak_lines)

        self._future_color = ""   # "" = default blue
        self.swatch_future = QPushButton("", self)
        self.swatch_future.setFixedSize(18, 18)
        self.swatch_future.setToolTip(
            "Click to choose a custom colour for future (scheduled) cells.\n"
            "Right-click to reset to the default blue.")
        self.swatch_future.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch_future.clicked.connect(self._pick_future_color)
        self.swatch_future.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.swatch_future.customContextMenuRequested.connect(self._reset_future_color)
        self._style_swatch(self.swatch_future, "#5284C8")
        self.disp_future_cells = QCheckBox("Future cells")
        self.disp_future_cells.setToolTip(
            "Show the forecast strip of scheduled/upcoming cells past today.\n"
            "Uncheck to hide them and show only past + today's cells.")
        row_future = self._checkbox_with_swatch(self.disp_future_cells, self.swatch_future)
        hcgv.addWidget(row_future)

        self.disp_legend = QCheckBox("Colour legend")
        self.disp_legend.setToolTip("Show the colour legend below the heatmap")
        hcgv.addWidget(self.disp_legend)
        self.disp_week_nums = QCheckBox("Week numbers")
        self.disp_week_nums.setToolTip("Show week-number labels below the grid")
        hcgv.addWidget(self.disp_week_nums)
        self.disp_month_names = QCheckBox("Month names")
        self.disp_month_names.setToolTip(
            "Show month name labels on the heatmap. Works in all grouping modes.")
        hcgv.addWidget(self.disp_month_names)

        self.hm_year_visibility = QComboBox(hcg)
        self.hm_year_visibility.addItem("Always visible", "always")
        self.hm_year_visibility.addItem("Reveal on hover", "hover")
        self.hm_year_visibility.addItem("Hidden", "hidden")
        self.hm_year_visibility.setToolTip(
            "How the year text next to the \u25c4 / \u25ba arrows behaves.\n"
            "Always visible: shown normally, as today.\n"
            "Reveal on hover: stays faded out until you hover over the year controls.\n"
            "Hidden: never shown. The arrows themselves are unaffected either way.")
        row_year_visibility = QWidget(self)
        _ryv = QHBoxLayout(row_year_visibility)
        _ryv.setContentsMargins(0, 0, 0, 0); _ryv.setSpacing(6)
        _ryv.addWidget(QLabel("\u2003Year visibility:", row_year_visibility))
        _ryv.addWidget(self.hm_year_visibility)
        _ryv.addStretch()
        hcgv.addWidget(row_year_visibility)

        # "Show detail as pop-up" on its own line, its auto-close timer on an
        # indented line directly below — was one wide combined row, split so
        # it fits a half-width column (still visually linked by the indent,
        # per the same parent/child convention used for the streak sub-toggles
        # below).
        self.disp_stats_popup = QCheckBox("Show detail as pop-up")
        self.disp_stats_popup.setToolTip(
            "Click a day/week/month: show stats in a floating pop-up "
            "instead of updating the panel below the heatmap.")
        hcgv.addWidget(self.disp_stats_popup)
        self.hm_popup_autoclose = QSpinBox(hcg)
        self.hm_popup_autoclose.setRange(0, 60)
        self.hm_popup_autoclose.setSpecialValueText("Never")
        self.hm_popup_autoclose.setSuffix(" s")
        self.hm_popup_autoclose.setToolTip(
            "Only applies when \u201cShow detail as pop-up\u201d is on. "
            "\u201cNever\u201d leaves it open until you close it or click another cell.")
        self.hm_popup_autoclose.setEnabled(False)
        self.disp_stats_popup.toggled.connect(self.hm_popup_autoclose.setEnabled)
        row_popup_autoclose = QWidget(self)
        _rpa = QHBoxLayout(row_popup_autoclose)
        _rpa.setContentsMargins(0, 0, 0, 0); _rpa.setSpacing(6)
        _rpa.addWidget(QLabel("\u2003Auto-close:", row_popup_autoclose))
        _rpa.addWidget(self.hm_popup_autoclose)
        _rpa.addStretch()
        hcgv.addWidget(row_popup_autoclose)

        self._tier_widgets["advanced"].append(hcg)

        # ── Stats Cards ──────────────────────────────────────────────────────
        scg = QGroupBox("Stats Cards", t3b_inner)
        scgv = QVBoxLayout(scg); scgv.setSpacing(2)
        scgv.setContentsMargins(9, 6, 9, 6)

        # Checkboxes constructed here as before; each row (checkbox + its
        # colour swatch) is added to scgv further below, once the swatches
        # exist (see "Appearance" section) — consolidated from two separate
        # rows (a visibility checkbox here, a colour-swatch label in
        # Appearance) into one, via the existing _checkbox_with_swatch()
        # helper, so each stat name appears only once in this dialog. No
        # config keys, defaults, or the swatches' own behaviour changed —
        # this is a widget-layout consolidation only.
        self.disp_eff_time    = QCheckBox("Effective time")
        self.disp_quality     = QCheckBox("Study Quality")
        # Kept directly under Study Quality — it decorates that card, not the
        # heatmap grid.
        self.disp_trend = QCheckBox("Study Quality trend arrow")
        self.disp_trend.setToolTip(
            "Show a trend arrow inside the Study Quality card compared to the previous period")
        self.disp_new_cards   = QCheckBox("New cards")
        self.disp_reviews     = QCheckBox("Reviews")
        self.disp_reviews.setToolTip(
            "Total review events (mature reviews + filtered/cram study) in the "
            "selected period. A card answered more than once counts each time —\n"
            "see \u201cCards Reviewed\u201d below for the distinct-card count.")
        self.disp_cards_reviewed = QCheckBox("Cards Reviewed")
        self.disp_cards_reviewed.setToolTip(
            "Distinct cards behind the Reviews count — a card answered several "
            "times (relearning, repeated cram-deck study) only counts once here.")
        self.disp_rev_time    = QCheckBox("Review time")

        # Today's Daily Status line — replaced the old progress bar (with its
        # colour swatch and height/width controls) with a minimal unboxed
        # "N studied \u00b7 M remaining" text line. No colour, height, or width
        # to configure any more, so this is now a plain checkbox.
        self.disp_progress_bar = QCheckBox("Daily status")
        self.disp_progress_bar.setToolTip(
            "A minimal line under the heatmap showing today's cards studied "
            "and cards remaining (e.g. 14 studied \u00b7 6 remaining).")
        scgv.addWidget(self.disp_progress_bar)

        self.disp_empty_msg = QCheckBox("\"No reviews yet\" message")
        self.disp_empty_msg.setToolTip(
            "Show the friendly \u201cNo reviews yet \u2014 ready when you are!\u201d line "
            "under the stat cards when the selected period is empty.")
        scgv.addWidget(self.disp_empty_msg)

        self._tier_widgets["advanced"].append(scg)

        # ── Streak Row ───────────────────────────────────────────────────────
        strg = QGroupBox("Streak Row", t3b_inner)
        strgv = QVBoxLayout(strg); strgv.setSpacing(2)
        strgv.setContentsMargins(9, 6, 9, 6)

        self.disp_streak_info = QCheckBox("Streak info row")
        self.disp_streak_info.setToolTip(
            "Show the best streak / current streak / due-today row below the stats cards")
        strgv.addWidget(self.disp_streak_info)

        # Granular sub-toggles for the individual pieces of that row — each
        # can be hidden independently instead of only the whole row at once.
        self.disp_best_streak = QCheckBox("\u2003Best streak")
        self.disp_best_streak.setToolTip("Show the \u201cN Day Best Streak\u201d segment")
        strgv.addWidget(self.disp_best_streak)
        self.disp_current_streak = QCheckBox("\u2003Current streak")
        self.disp_current_streak.setToolTip("Show the \u201cCurrent: N days\u201d segment")
        strgv.addWidget(self.disp_current_streak)
        self.disp_due_today = QCheckBox("\u2003Due today count")
        self.disp_due_today.setToolTip("Show the \u201cN cards due today\u201d segment")
        strgv.addWidget(self.disp_due_today)

        self.disp_attention = QCheckBox("Again-rate attention strip")
        self.disp_attention.setToolTip(
            "Show a warning in the streak row when today's again rate is "
            "notably higher than this week's average")
        strgv.addWidget(self.disp_attention)

        self.disp_total_revs = QCheckBox("Total reviews counter")
        self.disp_total_revs.setToolTip(
            "Show your all-time total review count in the streak row "
            "(e.g. \u201c13,343 total reviews done\u201d)")
        strgv.addWidget(self.disp_total_revs)

        # RENAMED from "Streak row" — that label was one word away from
        # "Streak info row" above (a completely different, visibility
        # toggle) despite this one only controlling a custom text colour.
        self.disp_custom_streak_color = QCheckBox("Custom text color")
        self.disp_custom_streak_color.setToolTip(
            "Off (default): the streak row's text automatically uses a readable "
            "colour for light or dark mode.\nOn: pick one fixed colour for it instead.")
        self.swatch_streak_text = QPushButton("", self)
        self.swatch_streak_text.setFixedSize(18, 18)
        self.swatch_streak_text.setToolTip("Click to choose a custom colour for the streak row text.")
        self.swatch_streak_text.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch_streak_text.clicked.connect(self._pick_streak_text_color)
        self._streak_text_color = ""
        self._style_swatch(self.swatch_streak_text, "#888888")
        self.swatch_streak_text.setEnabled(False)
        self.disp_custom_streak_color.toggled.connect(self.swatch_streak_text.setEnabled)
        row_streak_text_color = self._checkbox_with_swatch(self.disp_custom_streak_color, self.swatch_streak_text)
        strgv.addWidget(row_streak_text_color)

        self._tier_widgets["advanced"].append(strg)

        # ── Appearance ───────────────────────────────────────────────────────
        apg = QGroupBox("Appearance", t3b_inner)
        apgv = QVBoxLayout(apg); apgv.setSpacing(2)
        apgv.setContentsMargins(9, 6, 9, 6)

        # Font family on its own line (width-capped so it doesn't dominate a
        # half-width column — the dropdown itself still lists every font in
        # full), Size on an indented line below (was one combined row).
        self.hm_font_family = QFontComboBox(apg)
        self.hm_font_family.setToolTip(
            "Font used everywhere in the heatmap panel — stats cards, month/week\n"
            "labels, streak row, Daily status. Leave untouched to match Anki's own UI font.")
        self.hm_font_family.setMaximumWidth(130)
        self._font_family_touched = False   # BUG FIX below explains why this matters
        self.hm_font_family.currentFontChanged.connect(self._on_font_family_changed)
        row_font = QHBoxLayout()
        row_font.addWidget(QLabel("Font:", apg))
        row_font.addWidget(self.hm_font_family)
        row_font.addStretch()
        apgv.addLayout(row_font)

        self.hm_font_size = QSpinBox(apg)
        self.hm_font_size.setRange(0, 60)
        self.hm_font_size.setSpecialValueText("Default")
        self.hm_font_size.setSuffix(" px")
        self.hm_font_size.setToolTip(
            "Overrides the stats-card number size. \"Default\" follows the\n"
            "Larger card text toggle below (32px on, 18px off).")
        row_font_size = QHBoxLayout()
        row_font_size.addWidget(QLabel("\u2003Size:", apg))
        row_font_size.addWidget(self.hm_font_size)
        row_font_size.addStretch()
        apgv.addLayout(row_font_size)

        # RENAMED from "Card depth" — verified against heatmap_widget.py: this
        # toggle changes corner rounding, background elevation, border, AND
        # adds a hover lift, not just rounding. "Raised card style" (vs. the
        # tooltip's own "flat rectangles" contrast) covers the whole effect
        # without any one visible consequence standing in for all of it.
        self.disp_card_depth = QCheckBox("Raised card style")
        self.disp_card_depth.setToolTip(
            "Rounded corners, a subtle border, and a small hover lift on the "
            "stats cards, instead of flat rectangles.")
        apgv.addWidget(self.disp_card_depth)

        # RENAMED from "Modern typography" — verified against
        # heatmap_widget.py: this toggle changes ONLY the label and number
        # font sizes (10px/18px off, 13px/32px on) — nothing else. The old
        # tooltip claimed labels get "smaller" when this is turned on, which
        # was backwards (13px is larger than the 10px off-state); corrected
        # below.
        self.disp_modern_typo = QCheckBox("Larger card text")
        self.disp_modern_typo.setToolTip(
            "Larger stat numbers (32px) and labels (13px), for a bolder look. "
            "Off uses smaller 18px numbers and 10px labels.")
        apgv.addWidget(self.disp_modern_typo)

        self.disp_comfy_spacing = QCheckBox("Comfortable spacing")
        self.disp_comfy_spacing.setToolTip(
            "More breathing room between the heatmap, stats cards, and streak row.\n"
            "Uncheck for the original tighter layout.")
        apgv.addWidget(self.disp_comfy_spacing)

        self.disp_colored_nums = QCheckBox("Colored stat numbers")
        self.disp_colored_nums.setToolTip(
            "Give each metric card's number its own accent colour "
            "(blue / orange / cyan / purple / green) instead of plain text.")
        apgv.addWidget(self.disp_colored_nums)

        # Per-metric colour swatches — click to choose the accent colour used
        # for that card's number when "Colored stat numbers" is on.
        # Consolidated into the Stats Cards rows below (each checkbox +
        # its swatch on one line, via the existing _checkbox_with_swatch()
        # helper) instead of a separate labelled row here, so each stat
        # name appears only once in this dialog. Swatch creation, defaults,
        # config mapping, and the enable/disable wiring below are all
        # unchanged — only where the resulting widgets are laid out changed.
        self._num_color_defaults = {
            "eff": "#5AA9FF", "quality": "#FFA94D", "new": "#4DD9E8",
            "cards": "#B48CFF", "revtime": "#5FD97A", "cards_reviewed": "#F08FB0",
        }
        self._num_colors: dict[str, str] = dict(self._num_color_defaults)
        self.swatch_eff     = self._make_swatch("eff")
        self.swatch_quality  = self._make_swatch("quality")
        self.swatch_new      = self._make_swatch("new")
        self.swatch_cards    = self._make_swatch("cards")
        self.swatch_cards_reviewed = self._make_swatch("cards_reviewed")
        self.swatch_revtime  = self._make_swatch("revtime")

        for _sw in (self.swatch_eff, self.swatch_quality, self.swatch_new,
                    self.swatch_cards, self.swatch_cards_reviewed, self.swatch_revtime):
            _sw.setEnabled(False)
        self.disp_colored_nums.toggled.connect(self._set_num_swatches_enabled)

        # Stats Cards rows: each visibility checkbox (constructed earlier,
        # in the "Stats Cards" section above) combined with its colour
        # swatch (just constructed above) on one line. Order matches the
        # original Stats Cards section exactly, including "Study Quality
        # trend arrow" directly under Study Quality.
        scgv.addWidget(self._checkbox_with_swatch(self.disp_eff_time, self.swatch_eff))
        scgv.addWidget(self._checkbox_with_swatch(self.disp_quality, self.swatch_quality))
        scgv.addWidget(self.disp_trend)
        scgv.addWidget(self._checkbox_with_swatch(self.disp_new_cards, self.swatch_new))
        scgv.addWidget(self._checkbox_with_swatch(self.disp_reviews, self.swatch_cards))
        scgv.addWidget(self._checkbox_with_swatch(self.disp_cards_reviewed, self.swatch_cards_reviewed))
        scgv.addWidget(self._checkbox_with_swatch(self.disp_rev_time, self.swatch_revtime))

        # Opt-in custom colour for the small "Effective time / Study Quality /
        # New cards / Reviews / Cards Reviewed / Review time" label text
        # specifically. Unchecked (default) is intentionally the ONLY thing
        # most people ever need — it auto-adapts to a readable colour for
        # light or dark mode on its own. This exists for the minority who
        # want a specific colour instead, without making the default state
        # ask anything of everyone else.
        # RENAMED from "Metric label" — that name read as a visibility
        # toggle for the label text itself; it's actually a colour override.
        self.disp_custom_mlbl = QCheckBox("Custom label color")
        self.disp_custom_mlbl.setToolTip(
            "Off (default): the \u201cEffective time / Study Quality / \u2026\u201d label "
            "text automatically uses a readable colour for light or dark mode.\n"
            "On: pick one fixed colour for all labels instead.")
        self.swatch_mlbl = QPushButton("", self)
        self.swatch_mlbl.setFixedSize(18, 18)
        self.swatch_mlbl.setToolTip("Click to choose a custom colour for the metric card labels.")
        self.swatch_mlbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch_mlbl.clicked.connect(self._pick_mlbl_color)
        self._mlbl_color = ""
        self._style_swatch(self.swatch_mlbl, "#888888")
        self.swatch_mlbl.setEnabled(False)
        self.disp_custom_mlbl.toggled.connect(self.swatch_mlbl.setEnabled)
        row_custom_mlbl = self._checkbox_with_swatch(self.disp_custom_mlbl, self.swatch_mlbl)
        apgv.addWidget(row_custom_mlbl)

        self._tier_widgets["advanced"].append(apg)

        # ── Two-column arrangement ──────────────────────────────────────────
        # Heatmap Cells + Appearance on the left, Stats Cards + Streak Row on
        # the right — paired for roughly balanced column height rather than
        # strict top-to-bottom reading order.
        columns_row = QHBoxLayout(); columns_row.setSpacing(12)
        left_col = QVBoxLayout(); left_col.setSpacing(6)
        left_col.addWidget(hcg); left_col.addWidget(apg); left_col.addStretch()
        right_col = QVBoxLayout(); right_col.setSpacing(6)
        right_col.addWidget(scg); right_col.addWidget(strg); right_col.addStretch()
        columns_row.addLayout(left_col, 1)
        columns_row.addLayout(right_col, 1)
        t3bl.addLayout(columns_row)

        # ── Layout customization — compact full-width strip at the bottom ──
        lg = QGroupBox("Layout", t3b_inner); lgl = QVBoxLayout(lg); lgl.setSpacing(3)
        lgl.setContentsMargins(9, 6, 9, 6)
        self.disp_layout_customize = QCheckBox(
            "Enable drag-to-reorder layout", lg)
        self.disp_layout_customize.setToolTip(
            "Shows a small \u2630 grip on the heatmap, Daily status, stat cards,\n"
            "and streak row on the deck browser page. Click and drag one onto\n"
            "another to swap their order \u2014 the new order is saved automatically.")
        self.btn_reset_layout = QPushButton("Reset layout order", lg)
        self.btn_reset_layout.setToolTip(
            "Puts heatmap / Daily status / stat cards / streak row back in "
            "their original top-to-bottom order.")
        self.btn_reset_layout.clicked.connect(self._reset_layout_order)
        row_layout_controls = QHBoxLayout()
        row_layout_controls.addWidget(self.disp_layout_customize)
        row_layout_controls.addWidget(self.btn_reset_layout)
        row_layout_controls.addStretch()
        lgl.addLayout(row_layout_controls)
        _layout_hint = QLabel(
            "Drag a section's grip handle to reorder it on the deck browser page.")
        _layout_hint.setWordWrap(True)
        _layout_hint.setStyleSheet("font-size:10px;color:palette(placeholder-text)")
        lgl.addWidget(_layout_hint)
        t3bl.addWidget(lg)
        self._tier_widgets["advanced"].append(lg)

        t3bl.addStretch(); _idx3b = tabs.addTab(t3b, "Display")
        # BUG FIX: this tab's groups (Heatmap Cells, Stats Cards, Streak Row,
        # Appearance, Layout) are all gated to "advanced" — below that tier
        # the tab rendered completely blank instead of being hidden, since
        # only individual widgets were tier-gated, not the tab itself. Hide
        # the whole tab instead, same as Fatigue/Measurement.
        self._tier_tabs[_idx3b] = "advanced"

        # ── Tab 4: Toolbar & Sound ────────────────────────────────────────────
        t4 = QWidget(); t4l = QVBoxLayout(t4); t4l.setSpacing(8)

        tbg = QGroupBox("Toolbar", t4); tbf = QFormLayout(tbg)
        self.collapsed     = QCheckBox("Start collapsed",                  tbg)
        self.remember_pos  = QCheckBox("Remember position",                tbg)
        self.auto_collapse = QCheckBox("Auto-collapse when review starts", tbg)
        self.goal_display  = QComboBox(tbg)
        self.goal_display.addItem("Remaining count  (e.g. 145 due)", "count")
        self.goal_display.addItem("Progress bar  (e.g. \u2588\u2588\u2588\u2588\u2588\u2588\u2591\u2591 80%)", "bar")
        self.goal_display.addItem("Done / Remaining  (e.g. 12 done \u00b7 8 left)", "done_remaining")
        self.goal_display.setToolTip(
            "Only applies when exactly one Goal (End Condition) is enabled.\n"
            "With two or more Goals active, the remaining-count list is always used.")
        self.hud_visibility = QComboBox(tbg)
        self.hud_visibility.addItem("Always visible", "always")
        self.hud_visibility.addItem("Reveal on hover", "hover")
        self.hud_visibility.addItem("Hidden", "hidden")
        self.hud_visibility.setToolTip(
            "How the due-count/goal text (\u201c145 due\u201d etc.) behaves.\n"
            "Always visible: shown normally, as today.\n"
            "Reveal on hover: stays faded out until you hover over the toolbar.\n"
            "Hidden: never shown. The timer and indicator are unaffected either way.")
        tbf.addRow("", self.collapsed)
        tbf.addRow("", self.remember_pos)
        tbf.addRow("", self.auto_collapse)
        tbf.addRow("Goal display", self.goal_display)
        tbf.addRow("HUD visibility", self.hud_visibility)
        t4l.addWidget(tbg)

        # Each of these is fully optional and off by default — the toolbar's
        # baseline (indicator dot + timer/due-count, depending on display
        # state) already covers pause/resume via clicking the timer digits
        # themselves, so none of these are required for the toolbar to work;
        # they just add a dedicated, always-visible button for people who'd
        # rather not click the timer text itself.
        btng = QGroupBox("Toolbar Buttons", t4); btnf = QFormLayout(btng)
        self.show_play_btn    = QCheckBox("Play/Pause button", btng)
        self.show_skip_btn    = QCheckBox("Skip (break) button", btng)
        self.show_profile_btn = QCheckBox("Profile switcher", btng)
        self.show_mode_lbl    = QCheckBox("Mode label (Studying / Learning / Creating)", btng)
        self.show_profile_btn.setToolTip(
            "A small \u25be dropdown button for switching between study profiles\n"
            "(Settings profiles you've created, not Anki profiles) without opening\n"
            "Settings. If you only use one profile, this button has nothing to do.")
        btnf.addRow("", self.show_play_btn)
        btnf.addRow("", self.show_skip_btn)
        btnf.addRow("", self.show_profile_btn)
        btnf.addRow("", self.show_mode_lbl)
        t4l.addWidget(btng)

        sndg = QGroupBox("Sound", t4); sndf = QFormLayout(sndg)
        self.sound_enabled = QCheckBox("Enable ding", sndg)
        self.volume = QSlider(Qt.Orientation.Horizontal, sndg); self.volume.setRange(0,100)
        self.test_sound_btn = QPushButton("Test", sndg)
        self.test_sound_btn.clicked.connect(self._test_sound)
        srow = QHBoxLayout(); srow.addWidget(self.volume); srow.addWidget(self.test_sound_btn)
        sndf.addRow("",       self.sound_enabled)
        sndf.addRow("Volume", srow)
        # Swap the bundled ding for any .wav of the user's own. Read-only
        # so the only way in is the file picker — avoids someone typing a
        # path by hand and it silently not existing.
        self.custom_sound_path = QLineEdit(sndg)
        self.custom_sound_path.setReadOnly(True)
        self.custom_sound_path.setPlaceholderText("Default ding")
        self.custom_sound_path.setToolTip(
            "Use your own .wav file instead of the bundled ding.\n"
            "Leave empty to use the default.")
        self.browse_sound_btn = QPushButton("Browse…", sndg)
        self.browse_sound_btn.clicked.connect(self._browse_custom_sound)
        self.clear_sound_btn = QPushButton("Reset", sndg)
        self.clear_sound_btn.clicked.connect(self._clear_custom_sound)
        crow = QHBoxLayout()
        crow.addWidget(self.custom_sound_path)
        crow.addWidget(self.browse_sound_btn)
        crow.addWidget(self.clear_sound_btn)
        sndf.addRow("Custom sound", crow)
        t4l.addWidget(sndg)

        dg = QGroupBox("Data", t4); df = QFormLayout(dg)
        self.log_session_btn = QPushButton("Log past session…", t4)
        self.log_session_btn.clicked.connect(self.manual_session_requested.emit)
        df.addRow("", self.log_session_btn)
        note3 = QLabel("Use this if you studied without FocusFlow running.")
        note3.setStyleSheet("font-size:10px;font-style:italic")
        df.addRow("", note3); t4l.addWidget(dg)
        self._tier_widgets["expert"].append(dg)
        t4l.addStretch(); tabs.addTab(t4, "Toolbar && Sound")

        # ── Tab 5: Measurement ─────────────────────────────────────────────
        t5 = QWidget(); t5l = QVBoxLayout(t5); t5l.setSpacing(10)

        # ── Plain-language intro banner ───────────────────────────────────────
        _meas_intro = QLabel(
            "<b>What is Measurement?</b><br>"
            "The 'Study Quality' score you see in the heatmap and stats panel needs "
            "a way to be calculated. This tab lets you choose the method that "
            "best matches how <i>you</i> think about study quality — "
            "and fine-tune how the numbers are weighted."
        )
        _meas_intro.setWordWrap(True)
        _meas_intro.setStyleSheet(
            "background:rgba(80,180,100,0.08);border-radius:6px;"
            "padding:8px 10px;font-size:11px;color:palette(text)")
        t5l.addWidget(_meas_intro)

        # ── Step 1: Choose the method ─────────────────────────────────────────
        qsg = QGroupBox("Step 1 — How should quality be measured?", t5)
        qsf = QVBoxLayout(qsg); qsf.setSpacing(2)
        self.qs_focus = QRadioButton(
            "Focus score  —  based on how alert you are while studying")
        self.qs_focus.setToolTip(
            "Uses your answer speed and consistency to estimate mental sharpness.\n"
            "A long, distracted session scores lower than a short, focused one.\n"
            "Best choice if you want to reward deep focus, not just volume.")
        _focus_badge = QLabel("RECOMMENDED")
        _focus_badge.setStyleSheet(
            "background:#2e7a40;color:white;font-size:9px;font-weight:700;"
            "border-radius:7px;padding:1px 7px;")
        _focus_row = QHBoxLayout(); _focus_row.setSpacing(8)
        _focus_row.addWidget(self.qs_focus)
        _focus_row.addWidget(_focus_badge)
        _focus_row.addStretch()
        self.qs_answers = QRadioButton(
            "Answer buttons  —  based on how you rate each card")
        self.qs_answers.setToolTip(
            "Calculates quality from the Again / Hard / Good / Easy buttons you press.\n"
            "Customise the weights in Step 2 below.\n"
            "Best choice if you want to track how well you actually know the material.")
        self.qs_retention = QRadioButton(
            "Retention rate  —  based on how many cards you pass vs fail")
        self.qs_retention.setToolTip(
            "Quality = 1 − (Again count ÷ total reviews).\n"
            "Simple and objective. Best choice if Again rate is your main metric.")
        self.qs_focus.setChecked(True)
        qsf.addLayout(_focus_row)
        for w in (self.qs_answers, self.qs_retention):
            qsf.addWidget(w)
        t5l.addWidget(qsg)

        # ── Step 2: Tune button weights (only for Answer buttons mode) ────────
        awg = QGroupBox(
            "Step 2 — Tune the answer button weights  "
            "(only used when 'Answer buttons' is selected above)", t5)
        awf = QVBoxLayout(awg); awf.setSpacing(4)
        _aw_note = QLabel(
            "Drag each slider to set how much each button helps or hurts your quality score. "
            "Negative = pulls quality down.  Positive = pushes quality up. "
            "Example: if Hard cards mean you still know the material, give Hard a higher score.")
        _aw_note.setStyleSheet("font-size:10px;font-style:italic;color:palette(placeholder-text)")
        _aw_note.setWordWrap(True)
        awf.addWidget(_aw_note)
        self.w_again = _WeightSliderRow("Again  (failed card)", -5, 0, -2, awg)
        self.w_hard  = _WeightSliderRow("Hard   (struggled)", -5, 0, -1, awg)
        self.w_good  = _WeightSliderRow("Good   (knew it)", 0, 5, 1, awg)
        self.w_easy  = _WeightSliderRow("Easy   (too easy)", 0, 5, 2, awg)
        self.w_reset = QPushButton("Reset to defaults", awg)
        self.w_reset.setFixedHeight(28)
        self.w_reset.clicked.connect(self._reset_weights)
        for w in (self.w_again, self.w_hard, self.w_good, self.w_easy):
            awf.addWidget(w)
        awf.addWidget(self.w_reset)
        t5l.addWidget(awg)
        self._tier_widgets["expert"].append(awg)

        # ── Step 3: Effective time cap ────────────────────────────────────────
        etg = QGroupBox(
            "Step 3 — Stop counting time when you're clearly distracted", t5)
        etf = QFormLayout(etg)
        _et_note = QLabel(
            "If one card takes 3 minutes because you got distracted, that inflates your "
            "effective-time stats. Set a cap so those outliers don't skew your heatmap.")
        _et_note.setStyleSheet("font-size:10px;font-style:italic;color:palette(placeholder-text)")
        _et_note.setWordWrap(True)
        etf.addRow("", _et_note)
        self.eff_cap = QSpinBox(etg)
        self.eff_cap.setRange(0, 600); self.eff_cap.setSingleStep(10)
        self.eff_cap.setSuffix(" sec"); self.eff_cap.setSpecialValueText("No limit")
        self.eff_cap.setToolTip(
            "Any card taking longer than this is capped at this duration.\n"
            "Recommended: 60–120 sec. Set to 0 to disable the cap.")
        self.eff_ignore_easy = QCheckBox(
            "Exclude instant Easy answers from the time cap calculation")
        self.eff_ignore_easy.setToolTip(
            "Pressing Easy in under a second pulls the average time way down,\n"
            "making the cap less effective. Enable this to ignore those fast Easy presses.")
        etf.addRow("Cap per-card time at", self.eff_cap)
        etf.addRow("", self.eff_ignore_easy)
        t5l.addWidget(etg)
        self._tier_widgets["expert"].append(etg)

        t5l.addStretch(); _idx5 = tabs.addTab(t5, "Measurement")
        self._tier_tabs[_idx5] = "intermediate"
        # ── Tab 6: Help ──────────────────────────────────────────────────────
        t6 = QWidget(); t6l = QVBoxLayout(t6); t6l.setContentsMargins(4, 4, 4, 4)

        res_g = QGroupBox("Resources", t6); res_l = QVBoxLayout(res_g)
        self.btn_open_guide = QPushButton("Open Help Guide", res_g)
        self.btn_open_guide.clicked.connect(self._open_guide)
        self.btn_report_issue = QPushButton("Report an Issue", res_g)
        self.btn_report_issue.setToolTip(
            "Opens github.com/Doummar/FocusFlow/issues in your browser.")
        self.btn_report_issue.clicked.connect(self._report_issue)
        res_l.addWidget(self.btn_open_guide)
        res_l.addWidget(self.btn_report_issue)
        t6l.addWidget(res_g)

        reset_g = QGroupBox("Reset", t6); reset_l = QVBoxLayout(reset_g)
        self.btn_reset_defaults = QPushButton("Reset to Default", reset_g)
        self.btn_reset_defaults.setToolTip(
            "Resets Timer, Fatigue, Toolbar, Sound, and Measurement settings\n"
            "for the current profile back to their defaults.\n"
            "Heatmap appearance and Experience mode are not affected.")
        self.btn_reset_defaults.clicked.connect(self._reset_to_defaults)
        reset_l.addWidget(self.btn_reset_defaults)
        t6l.addWidget(reset_g)

        tips_g = QGroupBox("Tips", t6); tips_l = QVBoxLayout(tips_g)
        # BUG FIX: this was a single hardcoded dark green (#3f7a3f), which
        # reads fine on a light background but has poor contrast (~2.7:1,
        # under the 4.5:1 readability threshold) against Anki's dark theme.
        # Pick a shade appropriate to the active theme instead.
        try:
            from aqt.theme import theme_manager as _tm  # type: ignore[import]
            _tips_color = "#5fbf5f" if bool(_tm.night_mode) else "#3f7a3f"
        except Exception:
            _tips_color = "#3f7a3f"
        for _tip in (
            "Click the toolbar's time text to pause or resume the timer.",
            "Hold the time display for ~0.7 s to reset the timer.",
            "Click a heatmap day, week number, or month name to see its stats.",
        ):
            _tl = QLabel(_tip, tips_g)
            _tl.setStyleSheet(f"color:{_tips_color};")
            _tl.setWordWrap(True)
            tips_l.addWidget(_tl)
        t6l.addWidget(tips_g)

        t6l.addStretch()
        tabs.addTab(t6, "Help")

        self.tabs = tabs
        root.addWidget(tabs)
        self.buttons = QDialogButtonBox(self)
        self._btn_save = self.buttons.addButton(
            "Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_save.setDefault(True)
        self._btn_save.setStyleSheet(
            "QPushButton{background:#2563eb;color:#fff;border-radius:5px;"
            "padding:4px 18px;font-weight:600}"
            "QPushButton:hover{background:#1d4ed8}"
            "QPushButton:pressed{background:#1e40af}")
        self._btn_cancel = self.buttons.addButton(
            "Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.accepted.connect(self._save_and_close)
        self.buttons.rejected.connect(self._cancel)
        root.addWidget(self.buttons)

        # Wire auto-save
        for w in self._profile_widgets():
            if isinstance(w, QCheckBox):    w.toggled.connect(self._auto_save)
            elif isinstance(w, (QDoubleSpinBox, QSpinBox)): w.valueChanged.connect(self._auto_save)
            elif isinstance(w, QSlider):    w.valueChanged.connect(self._auto_save)
        for w in (self.hm_cell_size, self.hm_cell_shape, self.hm_note_shape, self.hm_note_position,
                  self.hm_color_scheme, self.hm_grouping, self.hm_streak_thickness, self.hm_theme,
                  self.hm_color_metric, self.goal_display, self.hud_visibility,
                  self.hm_year_visibility, self.hm_default_view, self.break_stay_on_top):
            w.currentIndexChanged.connect(self._auto_save)
        for w in (self.heavy_cards, self.light_cards, self.hm_font_size, self.hm_popup_autoclose,
                  self.hm_due_forecast_days):
            w.valueChanged.connect(self._auto_save)
        for w in (self.disp_streak_lines, self.disp_legend, self.disp_future_cells,
                  self.disp_week_nums, self.disp_month_names, self.disp_trend, self.disp_eff_time,
                  self.disp_quality, self.disp_new_cards, self.disp_reviews,
                  self.disp_rev_time, self.disp_streak_info, self.disp_best_streak,
                  self.disp_current_streak, self.disp_due_today, self.disp_attention,
                  self.disp_total_revs, self.disp_stats_popup, self.disp_empty_msg, self.disp_colored_nums,
                  self.disp_comfy_spacing, self.disp_card_depth, self.disp_modern_typo,
                  self.disp_progress_bar, self.disp_layout_customize, self.hm_full_history,
                  self.disp_custom_mlbl, self.disp_custom_label, self.disp_custom_streak_color):
            w.toggled.connect(self._auto_save)
        # Measurement tab auto-save
        for w in (self.w_again, self.w_hard, self.w_good, self.w_easy):
            w.valueChanged.connect(self._auto_save)
        for rb in (self.qs_focus, self.qs_answers, self.qs_retention):
            rb.toggled.connect(self._auto_save)
        self.eff_cap.valueChanged.connect(self._auto_save)
        self.eff_ignore_easy.toggled.connect(self._auto_save)
        self.sensitivity.valueChanged.connect(self._auto_save)
        self.dot_radio.toggled.connect(self._auto_save)
        self.pct_radio.toggled.connect(self._auto_save)
        for w in (self.sig_rt, self.sig_iiv, self.sig_again, self.sig_lapse,
                  self.adaptive_duration):
            w.toggled.connect(self._auto_save)
        for w in (self.min_cards, self.break_score):
            w.valueChanged.connect(self._auto_save)

    # ── public ────────────────────────────────────────────────────────────────

    def load_values(self, config: dict[str, Any]) -> None:
        self._loading = True
        self._config = self._normalize(copy.deepcopy(config))
        active = self._active()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(list(self._config["profiles"].keys()))
        self.profile_combo.setCurrentText(active)
        self.profile_combo.blockSignals(False)
        self._load_profile(active)
        # BUG FIX: _load_measurement() used to only run from
        # _profile_selected() (switching profiles via the Profile dropdown).
        # Now that the Profile UI is permanently hidden, that handler never
        # fires, so the entire Measurement tab (quality method, answer
        # weights, and this time-cap) was never populated from saved config
        # on open — it silently sat at whatever the widgets' last in-memory
        # state was, making changes look like they "didn't save" even
        # though _write_measurement() was persisting them correctly.
        self._load_measurement(self._config["profiles"][active].get("measurement", {}))
        self._load_heatmap(self._config.get("heatmap", {}))
        mode = self._config.get("experience_mode", "beginner")
        self.experience_combo.blockSignals(True)
        _idx = self.experience_combo.findData(mode)
        self.experience_combo.setCurrentIndex(_idx if _idx >= 0 else 0)
        self.experience_combo.blockSignals(False)
        self._apply_experience_mode(mode)
        self._refresh_btns()
        self._loading = False
        # Refresh backup so Cancel always reverts to the last saved state.
        self._config_backup = copy.deepcopy(self._config)

    # ── profile management ────────────────────────────────────────────────────

    def _profile_selected(self, name: str) -> None:
        if self._loading or not name: return
        old = self._active()
        if name == old: return
        self._write_profile(old)
        self._config["active_profile"] = name
        self._loading = True
        self._load_profile(name)
        _pm = self._config.get("profiles", {}).get(name, {}).get("measurement", {})
        self._load_measurement(_pm)
        self._refresh_btns()
        self._loading = False
        self._emit_changed()

    def _create_profile(self) -> None:
        self._write_profile(self._active())
        name = self._ask_name("New Profile", "Profile")
        if not name: return
        from ..utils.config_manager import _PROFILE_DEFAULTS as PD
        self._config["profiles"][name] = copy.deepcopy(PD)
        self._config["active_profile"] = name
        self.load_values(self._config); self._emit_changed()

    def _rename_profile(self) -> None:
        old = self._active()
        name = self._ask_name("Rename Profile", old, old_name=old)
        if not name or name == old: return
        self._write_profile(old)
        profiles = self._config["profiles"]; items = list(profiles.items()); profiles.clear()
        for k, v in items: profiles[name if k == old else k] = v
        self._config["active_profile"] = name
        self.load_values(self._config); self._emit_changed()

    def _duplicate_profile(self) -> None:
        src = self._active(); self._write_profile(src)
        base = f"{src} Copy"; candidate = base; i = 2
        while candidate in self._config["profiles"]: candidate = f"{base} {i}"; i += 1
        name = self._ask_name("Duplicate Profile", candidate)
        if not name: return
        self._config["profiles"][name] = copy.deepcopy(self._config["profiles"][src])
        self._config["active_profile"] = name
        self.load_values(self._config); self._emit_changed()

    def _delete_profile(self) -> None:
        name = self._active()
        if len(self._config["profiles"]) <= 1:
            QMessageBox.warning(self, "FocusFlow", "At least one profile is required."); return
        ans = QMessageBox.question(
            self, "FocusFlow", f"Delete profile '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes: return
        del self._config["profiles"][name]
        self._config["active_profile"] = next(iter(self._config["profiles"]))
        self.load_values(self._config); self._emit_changed()

    # ── load / write profile ──────────────────────────────────────────────────

    def _load_profile(self, name: str) -> None:
        p = self._config["profiles"][name]
        t = p["timer"]
        self.focus_minutes.setValue(float(t.get("study_minutes", 25)))
        self.break_minutes.setValue(float(t.get("break_minutes", 5)))
        self.long_break_minutes.setValue(float(t.get("long_break_minutes", 15)))
        self.sessions_before_long.setValue(int(t.get("sessions_before_long_break", 4)))
        self.auto_resume.setChecked(bool(t.get("auto_resume_after_editor", True)))
        _stay_on_top = bool(t.get("break_popup_stay_on_top", True))
        for i in range(self.break_stay_on_top.count()):
            if self.break_stay_on_top.itemData(i) == _stay_on_top:
                self.break_stay_on_top.setCurrentIndex(i); break
        ec = p["end_conditions"]
        self.cards_enabled.setChecked(bool(ec.get("cards_enabled", True)))
        self.cards_target.setValue(int(ec.get("cards_target", 25)))
        self.sessions_en.setChecked(bool(ec.get("sessions_enabled", False)))
        self.sessions_tgt.setValue(int(ec.get("sessions_target", 4)))
        self.max_time_en.setChecked(bool(ec.get("max_time_enabled", False)))
        self.max_time_mins.setValue(float(ec.get("max_time_minutes", 25)))
        self.all_due_en.setChecked(bool(ec.get("all_due_enabled", False)))
        fat = p["fatigue"]
        self.sensitivity.setValue(int(fat.get("sensitivity", 10)))
        self.min_cards.setValue(int(fat.get("min_cards", 10)))
        self.break_score.setValue(int(round(float(fat.get("break_score", 0.35)) * 100)))
        sigs = fat.get("signals", {})
        self.sig_rt.setChecked(bool(sigs.get("rt",    True)))
        self.sig_iiv.setChecked(bool(sigs.get("iiv",   True)))
        self.sig_again.setChecked(bool(sigs.get("again", True)))
        self.sig_lapse.setChecked(bool(sigs.get("lapse", True)))
        self.soft_prompt.setChecked(bool(fat.get("soft_break_prompt", True)))
        self.adaptive_duration.setChecked(bool(fat.get("adaptive_duration", True)))
        self.show_daily_report.setChecked(bool(fat.get("show_daily_report", True)))
        self.pct_radio.setChecked(fat.get("indicator", "dot") == "percentage")
        self.dot_radio.setChecked(fat.get("indicator", "dot") != "percentage")
        tb = p["toolbar"]
        self.collapsed.setChecked(bool(tb.get("collapsed", False)))
        self.remember_pos.setChecked(bool(tb.get("remember_position", True)))
        self.auto_collapse.setChecked(bool(tb.get("auto_collapse_on_start", False)))
        self.show_play_btn.setChecked(bool(tb.get("show_play_pause_button", False)))
        self.show_skip_btn.setChecked(bool(tb.get("show_skip_button", False)))
        self.show_profile_btn.setChecked(bool(tb.get("show_profile_button", False)))
        self.show_mode_lbl.setChecked(bool(tb.get("show_mode_label", False)))
        _gd_idx = self.goal_display.findData(tb.get("goal_display", "count"))
        self.goal_display.setCurrentIndex(_gd_idx if _gd_idx >= 0 else 0)
        _hv_idx = self.hud_visibility.findData(tb.get("hud_visibility", "always"))
        self.hud_visibility.setCurrentIndex(_hv_idx if _hv_idx >= 0 else 0)
        snd = p["sound"]
        self.sound_enabled.setChecked(bool(snd.get("enabled", True)))
        self.volume.setValue(int(float(snd.get("volume", 0.55)) * 100))
        self.custom_sound_path.setText(str(snd.get("custom_path", "") or ""))

    def _write_profile(self, name: str) -> None:
        if name not in self._config["profiles"]: return
        p = copy.deepcopy(self._config["profiles"][name])
        p["timer"].pop("focus_minutes", None)
        p["timer"]["study_minutes"]              = self.focus_minutes.value()
        p["timer"]["break_minutes"]              = self.break_minutes.value()
        p["timer"]["long_break_minutes"]         = self.long_break_minutes.value()
        p["timer"]["sessions_before_long_break"] = self.sessions_before_long.value()
        p["timer"]["auto_resume_after_editor"]   = self.auto_resume.isChecked()
        p["timer"]["break_popup_stay_on_top"]    = bool(self.break_stay_on_top.currentData())
        p["end_conditions"]["cards_enabled"]    = self.cards_enabled.isChecked()
        p["end_conditions"]["cards_target"]     = self.cards_target.value()
        p["end_conditions"]["sessions_enabled"] = self.sessions_en.isChecked()
        p["end_conditions"]["sessions_target"]  = self.sessions_tgt.value()
        p["end_conditions"]["max_time_enabled"] = self.max_time_en.isChecked()
        p["end_conditions"]["max_time_minutes"] = self.max_time_mins.value()
        p["end_conditions"]["all_due_enabled"]  = self.all_due_en.isChecked()
        p["fatigue"]["sensitivity"]       = self.sensitivity.value()
        p["fatigue"]["min_cards"]         = self.min_cards.value()
        p["fatigue"]["break_score"]       = round(self.break_score.value() / 100.0, 2)
        p["fatigue"]["signals"] = {
            "rt":    self.sig_rt.isChecked(),
            "iiv":   self.sig_iiv.isChecked(),
            "again": self.sig_again.isChecked(),
            "lapse": self.sig_lapse.isChecked(),
        }
        p["fatigue"]["soft_break_prompt"] = self.soft_prompt.isChecked()
        p["fatigue"]["adaptive_duration"] = self.adaptive_duration.isChecked()
        p["fatigue"]["show_daily_report"] = self.show_daily_report.isChecked()
        p["fatigue"]["indicator"]         = "percentage" if self.pct_radio.isChecked() else "dot"
        p["toolbar"]["collapsed"]              = self.collapsed.isChecked()
        p["toolbar"]["remember_position"]      = self.remember_pos.isChecked()
        p["toolbar"]["auto_collapse_on_start"] = self.auto_collapse.isChecked()
        p["toolbar"]["goal_display"]           = self.goal_display.currentData() or "count"
        p["toolbar"]["hud_visibility"]         = self.hud_visibility.currentData() or "always"
        p["toolbar"]["show_play_pause_button"] = self.show_play_btn.isChecked()
        p["toolbar"]["show_skip_button"]       = self.show_skip_btn.isChecked()
        p["toolbar"]["show_profile_button"]    = self.show_profile_btn.isChecked()
        p["toolbar"]["show_mode_label"]        = self.show_mode_lbl.isChecked()
        p["sound"]["enabled"] = self.sound_enabled.isChecked()
        p["sound"]["volume"]  = self.volume.value() / 100.0
        p["sound"]["custom_path"] = self.custom_sound_path.text().strip()
        self._config["profiles"][name] = p

    # ── heatmap load / write ──────────────────────────────────────────────────

    def _load_heatmap(self, hm: dict) -> None:
        size_map = {9: 0, 12: 1, 15: 2, 18: 3}
        self.hm_cell_size.setCurrentIndex(size_map.get(int(hm.get("cell_size", 12)), 1))
        _shape = hm.get("cell_shape", "soft")
        for i in range(self.hm_cell_shape.count()):
            if self.hm_cell_shape.itemData(i) == _shape:
                self.hm_cell_shape.setCurrentIndex(i); break
        _note_shape = hm.get("note_marker_shape", "triangle")
        for i in range(self.hm_note_shape.count()):
            if self.hm_note_shape.itemData(i) == _note_shape:
                self.hm_note_shape.setCurrentIndex(i); break
        _note_pos = hm.get("note_marker_position", "bottom_right")
        for i in range(self.hm_note_position.count()):
            if self.hm_note_position.itemData(i) == _note_pos:
                self.hm_note_position.setCurrentIndex(i); break
        _theme = hm.get("theme", "system")
        for i in range(self.hm_theme.count()):
            if self.hm_theme.itemData(i) == _theme:
                self.hm_theme.setCurrentIndex(i); break
        scheme = hm.get("color_scheme", "forest")
        for i in range(self.hm_color_scheme.count()):
            if self.hm_color_scheme.itemData(i) == scheme:
                self.hm_color_scheme.setCurrentIndex(i); break
        self._custom_scheme_color = str(hm.get("custom_scheme_color", "") or "#5284C8")
        self._sync_scheme_swatch()
        self.hm_full_history.setChecked(bool(hm.get("heatmap_full_history", False)))
        self.hm_due_forecast_days.setValue(int(hm.get("due_forecast_days", 30) or 30))
        self.hm_due_forecast_unlimited.setChecked(bool(hm.get("due_forecast_unlimited", False)))
        self.hm_due_forecast_days.setDisabled(self.hm_due_forecast_unlimited.isChecked())
        self._label_color = str(hm.get("label_color", "") or "")
        self.disp_custom_label.setChecked(bool(self._label_color))
        self._style_swatch(self.swatch_label, self._label_color or "#888888")
        self.swatch_label.setEnabled(bool(self._label_color))
        self._style_swatch(self.swatch_label, self._label_color or "#888888")
        _saved_order = hm.get("layout_block_order")
        _valid_ids = {"heatmap", "progress", "cards", "streak"}
        if isinstance(_saved_order, list) and set(_saved_order) <= _valid_ids and _saved_order:
            self._layout_block_order = list(_saved_order)
            for _bid in ("heatmap", "progress", "cards", "streak"):
                if _bid not in self._layout_block_order:
                    self._layout_block_order.append(_bid)
        else:
            self._layout_block_order = ["heatmap", "progress", "cards", "streak"]
        grouping = hm.get("grouping", "monthly")
        for i in range(self.hm_grouping.count()):
            if self.hm_grouping.itemData(i) == grouping:
                self.hm_grouping.setCurrentIndex(i); break
        self.heavy_cards.setValue(int(hm.get("heavy_cards", 50)))
        self.light_cards.setValue(int(hm.get("light_cards", 15)))
        _cm = hm.get("color_mode", "time")
        for i in range(self.hm_color_metric.count()):
            if self.hm_color_metric.itemData(i) == _cm:
                self.hm_color_metric.setCurrentIndex(i); break
        _dv = hm.get("default_stats_view", "today")
        for i in range(self.hm_default_view.count()):
            if self.hm_default_view.itemData(i) == _dv:
                self.hm_default_view.setCurrentIndex(i); break
        self.hide_native_stats.setChecked(bool(hm.get("hide_native_stats_line", False)))
        d = hm.get("display") or {}
        self.disp_streak_lines.setChecked(bool(d.get("show_streak_lines", True)))
        self.disp_legend.setChecked(      bool(d.get("show_legend",       True)))
        self.disp_future_cells.setChecked(bool(d.get("show_future_cells", True)))
        self.disp_layout_customize.setChecked(bool(d.get("layout_customize_enabled", False)))
        self.disp_week_nums.setChecked(   bool(d.get("show_week_nums",    True)))
        self.disp_trend.setChecked(       bool(d.get("show_trend",        True)))
        self.disp_month_names.setChecked( bool(d.get("show_month_names",  True)))
        _yv_idx = self.hm_year_visibility.findData(d.get("year_visibility", "always"))
        self.hm_year_visibility.setCurrentIndex(_yv_idx if _yv_idx >= 0 else 0)
        self.disp_stats_popup.setChecked(  bool(d.get("stats_popup",       False)))
        self.hm_popup_autoclose.setEnabled(self.disp_stats_popup.isChecked())
        self.disp_empty_msg.setChecked(bool(d.get("show_empty_state_message", True)))
        self.hm_popup_autoclose.setValue(int(d.get("popup_auto_close_secs", 0) or 0))
        self.disp_eff_time.setChecked(    bool(d.get("show_eff_time",     True)))
        self.disp_quality.setChecked(     bool(d.get("show_quality",      True)))
        self.disp_new_cards.setChecked(   bool(d.get("show_new_cards",    True)))
        self.disp_reviews.setChecked(     bool(d.get("show_reviews",      True)))
        self.disp_cards_reviewed.setChecked(bool(d.get("show_cards_reviewed", True)))
        self.disp_rev_time.setChecked(    bool(d.get("show_rev_time",     True)))
        self.disp_streak_info.setChecked( bool(d.get("show_streak_info",  True)))
        self.disp_best_streak.setChecked(   bool(d.get("show_best_streak",    True)))
        self.disp_current_streak.setChecked(bool(d.get("show_current_streak", True)))
        self.disp_due_today.setChecked(     bool(d.get("show_due_today",      True)))
        self.disp_attention.setChecked(   bool(d.get("show_attention",    True)))
        self.disp_total_revs.setChecked(  bool(d.get("show_total_reviews", True)))
        self.disp_colored_nums.setChecked( bool(d.get("colored_numbers",      True)))
        self._set_num_swatches_enabled(self.disp_colored_nums.isChecked())
        _mlbl = str(d.get("metric_label_color", "") or "")
        self.disp_custom_mlbl.setChecked(bool(_mlbl))
        self._mlbl_color = _mlbl
        self._style_swatch(self.swatch_mlbl, _mlbl or "#888888")
        self.swatch_mlbl.setEnabled(bool(_mlbl))
        _streak_txt = str(d.get("streak_text_color", "") or "")
        self.disp_custom_streak_color.setChecked(bool(_streak_txt))
        self._streak_text_color = _streak_txt
        self._style_swatch(self.swatch_streak_text, _streak_txt or "#888888")
        self.swatch_streak_text.setEnabled(bool(_streak_txt))
        self.disp_comfy_spacing.setChecked(bool(d.get("comfortable_spacing",  True)))
        self.disp_card_depth.setChecked(   bool(d.get("card_depth",           True)))
        self.disp_modern_typo.setChecked(  bool(d.get("modern_typography",    True)))
        self.disp_progress_bar.setChecked( bool(d.get("show_progress_bar",    True)))
        _saved_colors = d.get("number_colors") or {}
        for _key, _swatch in (("eff", self.swatch_eff), ("quality", self.swatch_quality),
                               ("new", self.swatch_new), ("cards", self.swatch_cards),
                               ("cards_reviewed", self.swatch_cards_reviewed),
                               ("revtime", self.swatch_revtime)):
            _hexcolor = str(_saved_colors.get(_key, self._num_color_defaults[_key]))
            self._num_colors[_key] = _hexcolor
            self._style_swatch(_swatch, _hexcolor)
        self._streak_color = str(d.get("streak_line_color", "") or "")
        _thick = str(d.get("streak_line_thickness", "normal") or "normal")
        for i in range(self.hm_streak_thickness.count()):
            if self.hm_streak_thickness.itemData(i) == _thick:
                self.hm_streak_thickness.setCurrentIndex(i); break
        self._style_swatch(self.swatch_streak, self._streak_color or "#8C8C8C")
        self._future_color = str(d.get("future_cell_color", "") or "")
        self._style_swatch(self.swatch_future, self._future_color or "#5284C8")
        self._note_color = str(d.get("note_marker_color", "") or "")
        self._style_swatch(self.swatch_note, self._note_color or "#E8B84D")
        _font_family = str(d.get("card_font_family", "") or "")
        self._font_family_touched = bool(_font_family)
        if _font_family:
            from aqt.qt import QFont
            self.hm_font_family.setCurrentFont(QFont(_font_family))
        self.hm_font_size.setValue(int(d.get("card_font_size", 0) or 0))

    def _write_heatmap(self) -> None:
        size_values = [9, 12, 15, 18]
        # "existing" carries forward config keys this dialog doesn't have a
        # widget for. BUG FIX: last_selected_date, day_notes, and
        # dismissed_reminders are all written independently of this dialog
        # (clicking a day / a reminder popup) — day_notes and
        # dismissed_reminders were previously missing from this list, so
        # saving Settings silently reset every note/reminder the user had
        # added back to empty. Preserve all three here.
        existing = self._config.get("heatmap", {})
        # Carries forward "display" keys this dialog no longer has widgets
        # for (progress_bar_color/height/width — see the "display" dict
        # below), same reasoning and same pattern as "existing" above: keep
        # existing users' stored values intact rather than silently wiping
        # them on the next auto-save, even though nothing currently reads
        # them anymore.
        existing_display = existing.get("display", {})
        self._config["heatmap"] = {
            "cell_size":    size_values[self.hm_cell_size.currentIndex()],
            "cell_shape":   self.hm_cell_shape.currentData() or "soft",
            "theme":        self.hm_theme.currentData() or "system",
            "note_marker_shape":    self.hm_note_shape.currentData() or "triangle",
            "note_marker_position": self.hm_note_position.currentData() or "bottom_right",
            "color_scheme": self.hm_color_scheme.currentData() or "forest",
            "custom_scheme_color": self._custom_scheme_color,
            "layout_block_order": list(self._layout_block_order),
            "heatmap_full_history": self.hm_full_history.isChecked(),
            "due_forecast_days": self.hm_due_forecast_days.value(),
            "due_forecast_unlimited": self.hm_due_forecast_unlimited.isChecked(),
            "label_color": self._label_color if self.disp_custom_label.isChecked() else "",
            "grouping":     self.hm_grouping.currentData() or "monthly",
            "heavy_cards":  self.heavy_cards.value(),
            "light_cards":  self.light_cards.value(),
            "color_mode":   self.hm_color_metric.currentData() or "time",
            "default_stats_view": self.hm_default_view.currentData() or "today",
            "hide_native_stats_line": self.hide_native_stats.isChecked(),
            "last_selected_date": existing.get("last_selected_date", ""),
            "day_notes": existing.get("day_notes", {}),
            "dismissed_reminders": existing.get(
                "dismissed_reminders", {"date": "", "events": []}),
            "display": {
                "show_streak_lines": self.disp_streak_lines.isChecked(),
                "show_goal_dot":     False,   # removed from UI per user request
                "show_legend":       self.disp_legend.isChecked(),
                "show_future_cells": self.disp_future_cells.isChecked(),
                "layout_customize_enabled": self.disp_layout_customize.isChecked(),
                "show_week_nums":    self.disp_week_nums.isChecked(),
                "show_trend":        self.disp_trend.isChecked(),
                "show_month_names":  self.disp_month_names.isChecked(),
                "year_visibility":   self.hm_year_visibility.currentData() or "always",
                "stats_popup":       self.disp_stats_popup.isChecked(),
                "show_empty_state_message": self.disp_empty_msg.isChecked(),
                "popup_auto_close_secs": self.hm_popup_autoclose.value(),
                "show_eff_time":     self.disp_eff_time.isChecked(),
                "show_quality":      self.disp_quality.isChecked(),
                "show_new_cards":    self.disp_new_cards.isChecked(),
                "show_reviews":      self.disp_reviews.isChecked(),
                "show_cards_reviewed": self.disp_cards_reviewed.isChecked(),
                "show_rev_time":     self.disp_rev_time.isChecked(),
                "show_streak_info":  self.disp_streak_info.isChecked(),
                "show_best_streak":    self.disp_best_streak.isChecked(),
                "show_current_streak": self.disp_current_streak.isChecked(),
                "show_due_today":      self.disp_due_today.isChecked(),
                "show_attention":    self.disp_attention.isChecked(),
                "show_total_reviews": self.disp_total_revs.isChecked(),
                "colored_numbers":    self.disp_colored_nums.isChecked(),
                "metric_label_color": self._mlbl_color if self.disp_custom_mlbl.isChecked() else "",
                "streak_text_color": self._streak_text_color if self.disp_custom_streak_color.isChecked() else "",
                "comfortable_spacing": self.disp_comfy_spacing.isChecked(),
                "card_depth":         self.disp_card_depth.isChecked(),
                "modern_typography":  self.disp_modern_typo.isChecked(),
                "show_progress_bar":  self.disp_progress_bar.isChecked(),
                "number_colors":      dict(self._num_colors),
                "streak_line_color":  self._streak_color,
                "streak_line_thickness": self.hm_streak_thickness.currentData() or "normal",
                "future_cell_color":  self._future_color,
                "note_marker_color":  self._note_color,
                "card_font_family":   (self.hm_font_family.currentFont().family()
                                        if self._font_family_touched else ""),
                "card_font_size":     self.hm_font_size.value(),
                # No UI controls for these three any more (the old bar's
                # colour swatch and height/width spinboxes were removed along
                # with the bar itself) — preserve whatever an existing user
                # had stored rather than silently wiping it on next save.
                "progress_bar_color":  existing_display.get("progress_bar_color", ""),
                "progress_bar_height": existing_display.get("progress_bar_height", 0),
                "progress_bar_width":  existing_display.get("progress_bar_width", 0),
            },
        }


    # ── helpers ───────────────────────────────────────────────────────────────

    def _load_measurement(self, meas: dict) -> None:
        """Populate the Measurement tab from a measurement config dict."""
        # Save/restore rather than hardcoding False: this can run inside an
        # outer load that's already set _loading = True (load_values()) —
        # unconditionally clearing it here would re-enable auto-save
        # partway through that outer load, firing premature writes for
        # every widget still left to populate after this call returns.
        _prev_loading = self._loading
        self._loading = True
        try:
            aw = meas.get("ans_weights", {})
            self.w_again.setValue(int(aw.get("again", -2)))
            self.w_hard.setValue( int(aw.get("hard",  -1)))
            self.w_good.setValue( int(aw.get("good",   1)))
            self.w_easy.setValue( int(aw.get("easy",   2)))
            src = meas.get("quality_source", "focus")
            self.qs_focus.setChecked(src == "focus")
            self.qs_answers.setChecked(src == "answers")
            self.qs_retention.setChecked(src == "retention")
            self.eff_cap.setValue(int(meas.get("eff_time_cap_secs", 90)))
            self.eff_ignore_easy.setChecked(bool(meas.get("ignore_easy_time", False)))
        finally:
            self._loading = _prev_loading

    def _write_measurement(self) -> None:
        """Persist current Measurement tab values into the active profile."""
        name = self._active()
        prof = self._config.setdefault("profiles", {}).setdefault(
            name, {})
        src = ("answers"   if self.qs_answers.isChecked() else
               "retention" if self.qs_retention.isChecked() else "focus")
        prof["measurement"] = {
            "quality_source":    src,
            "ans_weights": {
                "again": self.w_again.value(),
                "hard":  self.w_hard.value(),
                "good":  self.w_good.value(),
                "easy":  self.w_easy.value(),
            },
            "eff_time_cap_secs": self.eff_cap.value(),
            "ignore_easy_time":  self.eff_ignore_easy.isChecked(),
        }

    def _reset_weights(self) -> None:
        """Restore Again/Hard/Good/Easy sliders to factory defaults."""
        defaults = {"again": -2, "hard": -1, "good": 1, "easy": 2}
        for attr, val in defaults.items():
            getattr(self, f"w_{attr}").setValue(val)
        # auto-save is triggered by the valueChanged signals above

    def _auto_save(self, *_: Any) -> None:
        if self._loading: return
        self._write_profile(self._active())
        self._write_heatmap()
        self._write_measurement()
        self._config["experience_mode"] = self.experience_combo.currentData() or "beginner"
        self._emit_changed()

    def _test_sound(self) -> None:
        self._auto_save(); self.test_sound_requested.emit()

    def _browse_custom_sound(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a notification sound", "", "WAV files (*.wav)")
        if path:
            self.custom_sound_path.setText(path)
            self._auto_save()

    def _clear_custom_sound(self) -> None:
        self.custom_sound_path.setText("")
        self._auto_save()

    # ── Help tab actions ─────────────────────────────────────────────────────

    def _open_guide(self) -> None:
        dlg = GuideDialog(self)
        dlg.open_settings_requested.connect(lambda: None)  # already on Settings; no-op
        dlg.exec()

    _ISSUES_URL = "https://github.com/Doummar/FocusFlow/issues"

    def _report_issue(self) -> None:
        """Open the GitHub issues page. The log file (focusflow.log, in the
        addon folder) is worth attaching to a report if it's a crash/bug."""
        try:
            from aqt.qt import QDesktopServices, QUrl
            QDesktopServices.openUrl(QUrl(self._ISSUES_URL))
        except Exception:
            QMessageBox.information(
                self, "Report an Issue",
                f"Could not open the browser automatically. Please visit:\n{self._ISSUES_URL}")

    def _reset_to_defaults(self) -> None:
        reply = QMessageBox.question(
            self, "Reset to Default",
            "Reset Timer, Fatigue, Toolbar, Sound, and Measurement settings for "
            f"the \u201c{self._active()}\u201d profile back to their defaults?\n\n"
            "Heatmap appearance and Experience mode are not affected. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        name = self._active()
        self._config["profiles"][name] = copy.deepcopy(_PROFILE_DEFAULTS)
        self.load_values(self._config)
        self._auto_save()

    def _save_and_close(self) -> None:
        """Write all settings to disk and close."""
        self._auto_save()
        self._config_backup = copy.deepcopy(self._config)
        self.saved.emit(copy.deepcopy(self._config))
        self.accept()

    def _restore_backup(self) -> None:
        """Restore the saved configuration after abandoning live previews."""
        if self._config != self._config_backup:
            self._config = copy.deepcopy(self._config_backup)
            self._emit_changed()   # revert heatmap / toolbar live preview

    def _cancel(self) -> None:
        """Revert live-preview changes and close without saving."""
        self._restore_backup()
        self.reject()

    def reject(self) -> None:
        """Restore previews for Cancel, Escape, and window-close alike."""
        self._restore_backup()
        super().reject()

    def _close(self) -> None:   # backward-compat shim
        self._save_and_close()

    # ── per-metric colour swatches ────────────────────────────────────────────

    def _make_swatch(self, key: str) -> QPushButton:
        btn = QPushButton("", self)
        btn.setFixedSize(18, 18)
        btn.setToolTip("Click to choose a colour for this card's number")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self._pick_color(key))
        self._style_swatch(btn, self._num_color_defaults[key])
        return btn

    @staticmethod
    def _style_swatch(btn: QPushButton, hexcolor: str) -> None:
        btn.setStyleSheet(
            f"QPushButton{{background:{hexcolor};border:1px solid palette(mid);"
            f"border-radius:4px;}}")

    def _checkbox_with_swatch(self, checkbox: QCheckBox, swatch: QPushButton) -> QWidget:
        row = QWidget(self)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(6)
        lay.addWidget(checkbox)
        lay.addWidget(swatch)
        lay.addStretch()
        return row

    def _label_with_swatch(self, text: str, swatch: QPushButton) -> QWidget:
        row = QWidget(self)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(6)
        lbl = QLabel(text, row)
        lay.addWidget(lbl)
        lay.addWidget(swatch)
        lay.addStretch()
        return row

    def _set_num_swatches_enabled(self, enabled: bool) -> None:
        """Enable/disable the six per-metric colour swatches together —
        they only take visual effect while "Colored stat numbers" is on."""
        for sw in (self.swatch_eff, self.swatch_quality, self.swatch_new,
                   self.swatch_cards, self.swatch_cards_reviewed, self.swatch_revtime):
            sw.setEnabled(enabled)

    def _sync_scheme_swatch(self) -> None:
        """Keep the swatch showing the colour that's actually active — the
        custom pick when "Custom" is selected, otherwise the real colour of
        whichever built-in preset is selected. Previously the swatch only
        ever reflected the custom pick, so it looked blank/disconnected
        whenever a preset (e.g. "Rose") was selected instead."""
        key = self.hm_color_scheme.currentData()
        if key == "custom":
            self._style_swatch(self.swatch_scheme, self._custom_scheme_color)
            return
        _lo, hi = COLOR_SCHEMES.get(key, COLOR_SCHEMES.get("forest", ((0, 0, 0), (0, 0, 0))))
        self._style_swatch(self.swatch_scheme, "#%02x%02x%02x" % hi)

    def _pick_scheme_color(self) -> None:
        from aqt.qt import QColorDialog, QColor
        current = QColor(self._custom_scheme_color or "#5284C8")
        chosen = QColorDialog.getColor(current, self, "Choose a heatmap colour")
        if not chosen.isValid():
            return
        self._custom_scheme_color = chosen.name()
        self._style_swatch(self.swatch_scheme, self._custom_scheme_color)
        # Picking a colour here always means "use this instead of a preset".
        for i in range(self.hm_color_scheme.count()):
            if self.hm_color_scheme.itemData(i) == "custom":
                self.hm_color_scheme.setCurrentIndex(i)
                break
        self._auto_save()

    def _pick_color(self, key: str) -> None:
        from aqt.qt import QColorDialog, QColor
        swatch = {
            "eff": self.swatch_eff, "quality": self.swatch_quality,
            "new": self.swatch_new, "cards": self.swatch_cards,
            "revtime": self.swatch_revtime,
        }[key]
        current = QColor(self._num_colors.get(key, self._num_color_defaults[key]))
        chosen = QColorDialog.getColor(current, self, "Choose a colour")
        if not chosen.isValid():
            return
        hexcolor = chosen.name()
        self._num_colors[key] = hexcolor
        self._style_swatch(swatch, hexcolor)
        self._auto_save()

    def _pick_streak_color(self) -> None:
        from aqt.qt import QColorDialog, QColor
        current = QColor(self._streak_color or "#8C8C8C")
        chosen = QColorDialog.getColor(current, self, "Choose a colour")
        if not chosen.isValid():
            return
        self._streak_color = chosen.name()
        self._style_swatch(self.swatch_streak, self._streak_color)
        self._auto_save()

    def _reset_streak_color(self, _pos=None) -> None:
        """Right-click the swatch: back to the theme-adaptive default."""
        self._streak_color = ""
        self._style_swatch(self.swatch_streak, "#8C8C8C")
        self._auto_save()

    def _on_font_family_changed(self, _font) -> None:
        # BUG FIX: QFontComboBox always reports *some* font as "current" —
        # there's no genuine empty state — so blindly writing
        # currentFont().family() on every auto-save meant even users who
        # never touched this control got an arbitrary system font baked
        # into their config the moment they toggled anything else, visibly
        # mismatching the rest of the panel. Only mark it touched (and
        # therefore only write it) once the user actually changes it here.
        if self._loading:
            return
        self._font_family_touched = True
        self._auto_save()

    def _pick_label_color(self) -> None:
        from aqt.qt import QColorDialog, QColor
        current = QColor(self._label_color or "#888888")
        chosen = QColorDialog.getColor(current, self, "Choose a colour")
        if not chosen.isValid():
            return
        self._label_color = chosen.name()
        self._style_swatch(self.swatch_label, self._label_color)
        self._auto_save()

    def _pick_note_color(self) -> None:
        from aqt.qt import QColorDialog, QColor
        current = QColor(self._note_color or "#E8B84D")
        chosen = QColorDialog.getColor(current, self, "Choose a colour")
        if not chosen.isValid():
            return
        self._note_color = chosen.name()
        self._style_swatch(self.swatch_note, self._note_color)
        self._auto_save()

    def _reset_note_color(self, _pos=None) -> None:
        self._note_color = ""
        self._style_swatch(self.swatch_note, "#E8B84D")
        self._auto_save()

    def _pick_streak_text_color(self) -> None:
        from aqt.qt import QColorDialog, QColor
        current = QColor(self._streak_text_color or "#888888")
        chosen = QColorDialog.getColor(current, self, "Choose a colour")
        if not chosen.isValid():
            return
        self._streak_text_color = chosen.name()
        self._style_swatch(self.swatch_streak_text, self._streak_text_color)
        self._auto_save()

    def _pick_mlbl_color(self) -> None:
        from aqt.qt import QColorDialog, QColor
        current = QColor(self._mlbl_color or "#888888")
        chosen = QColorDialog.getColor(current, self, "Choose a colour")
        if not chosen.isValid():
            return
        self._mlbl_color = chosen.name()
        self._style_swatch(self.swatch_mlbl, self._mlbl_color)
        self._auto_save()

    def _pick_future_color(self) -> None:
        from aqt.qt import QColorDialog, QColor
        current = QColor(self._future_color or "#5284C8")
        chosen = QColorDialog.getColor(current, self, "Choose a colour")
        if not chosen.isValid():
            return
        self._future_color = chosen.name()
        self._style_swatch(self.swatch_future, self._future_color)
        self._auto_save()

    def _reset_future_color(self, _pos=None) -> None:
        self._future_color = ""
        self._style_swatch(self.swatch_future, "#5284C8")
        self._auto_save()

    def _reset_layout_order(self) -> None:
        self._layout_block_order = ["heatmap", "progress", "cards", "streak"]
        self._auto_save()
        QMessageBox.information(
            self, "Layout reset",
            "Heatmap, Daily status, stat cards, and streak row are back in "
            "their original order.")

    def _emit_changed(self) -> None:
        self.changed.emit(copy.deepcopy(self._config))

    def _refresh_btns(self) -> None:
        self.btn_delete.setEnabled(len(self._config["profiles"]) > 1)

    def _active(self) -> str:
        return str(self._config.get("active_profile", "Default"))

    # ── experience mode ──────────────────────────────────────────────────────

    def _on_experience_changed(self, _index: int) -> None:
        mode = self.experience_combo.currentData() or "beginner"
        self._apply_experience_mode(mode)
        if not self._loading:
            self._config["experience_mode"] = mode
            self._auto_save()

    def _apply_experience_mode(self, mode: str) -> None:
        rank = self._TIER_RANK.get(mode, 0)
        for tab_index, required in self._tier_tabs.items():
            self.tabs.setTabVisible(tab_index, rank >= self._TIER_RANK[required])
        for tier, widgets in self._tier_widgets.items():
            visible = rank >= self._TIER_RANK[tier]
            for w in widgets:
                w.setVisible(visible)
        self.experience_blurb.setText(self._TIER_BLURBS.get(mode, ""))

    def _ask_name(self, title: str, default: str, old_name: str | None = None) -> str | None:
        text, ok = QInputDialog.getText(self, title, "Name:", QLineEdit.EchoMode.Normal, default)
        if not ok: return None
        name = " ".join(text.strip().split())
        if not name:
            QMessageBox.warning(self, "FocusFlow", "Profile name cannot be empty."); return None
        if name != old_name and name in self._config["profiles"]:
            QMessageBox.warning(self, "FocusFlow", "A profile with that name already exists."); return None
        return name

    def _profile_widgets(self) -> tuple:
        return (
            self.focus_minutes, self.break_minutes, self.long_break_minutes,
            self.sessions_before_long, self.auto_resume,
            self.cards_enabled, self.cards_target, self.sessions_en, self.sessions_tgt,
            self.max_time_en, self.max_time_mins, self.all_due_en,
            self.min_cards, self.break_score,
            self.sig_rt, self.sig_iiv, self.sig_again, self.sig_lapse,
            self.soft_prompt, self.adaptive_duration, self.show_daily_report,
            self.dot_radio, self.pct_radio,
            self.collapsed, self.remember_pos, self.auto_collapse,
            self.sound_enabled, self.volume,
        )

    def _normalize(self, config: dict[str, Any]) -> dict[str, Any]:
        from ..utils.config_manager import _HEATMAP_DEFAULTS as HMD, _PROFILE_DEFAULTS as PD
        if "profiles" not in config:
            config = {"profiles": {"Default": copy.deepcopy(PD)}, "active_profile": "Default"}
        if not config.get("profiles"):
            config["profiles"] = {"Default": copy.deepcopy(PD)}
        if config.get("active_profile") not in config["profiles"]:
            config["active_profile"] = next(iter(config["profiles"]))
        hm = copy.deepcopy(HMD)
        raw_hm = config.get("heatmap") or {}
        for k, v in raw_hm.items():
            if k not in HMD:
                continue
            if k == "display" and isinstance(v, dict):
                hm["display"].update(v)
            else:
                hm[k] = v
        config["heatmap"] = hm
        config.setdefault("experience_mode", "beginner")
        # Ensure every profile has a full measurement block
        from ..utils.config_manager import _PROFILE_DEFAULTS as _PD2
        _meas_def = _PD2.get("measurement", {})
        for _pdata in config.get("profiles", {}).values():
            if "measurement" not in _pdata:
                _pdata["measurement"] = copy.deepcopy(_meas_def)
            else:
                for _k, _v in _meas_def.items():
                    if _k not in _pdata["measurement"]:
                        _pdata["measurement"][_k] = copy.deepcopy(_v)
                _aw = _pdata["measurement"].setdefault("ans_weights", {})
                for _k, _v in _meas_def.get("ans_weights", {}).items():
                    _aw.setdefault(_k, _v)
        return config
