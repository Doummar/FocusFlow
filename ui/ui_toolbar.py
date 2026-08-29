from __future__ import annotations

from aqt.qt import (
    QAction,
    QColor,
    QFrame,
    QFontDatabase,
    QFontMetrics,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMouseEvent,
    QPainter,
    QPropertyAnimation,
    QPushButton,
    QRectF,
    QSizePolicy,
    QTimer,
    Qt,
    pyqtSignal,
)

from ..core.timer_manager import TimerMode, TimerSnapshot
from ..utils.logger import log


class FatigueDotButton(QPushButton):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._color = QColor("#3b8b4d")
        self._hovered = False
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.setStyleSheet("background: transparent; border: 0; padding: 0;")
        self.setToolTip(
            "Shows how focused you are \u2014 green is good, yellow means "
            "you're drifting, red means quality is dropping. Click to "
            "cycle what the toolbar shows.")

    def set_dot_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._hovered:
            painter.setBrush(QColor(30, 30, 30, 18))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(2, 2, 24, 24))
        painter.setBrush(self._color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(7, 7, 14, 14))


class HoldTimerButton(QPushButton):
    """Timer label that distinguishes a single click from a hold (≥700 ms).

    - Single click  → emits ``clicked``  (inherited) → pause / resume
    - Hold ≥700 ms  → emits ``held``                 → reset timer to start
    """
    held = pyqtSignal()
    _HOLD_MS = 700

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(self._HOLD_MS)
        self._hold_timer.timeout.connect(self._fire_hold)
        self._held_triggered = False

    def _fire_hold(self) -> None:
        self._held_triggered = True
        self.setDown(False)
        self.held.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._held_triggered = False
            self._hold_timer.start()
        elif event.button() == Qt.MouseButton.RightButton:
            parent = self.parent()
            if parent is not None and hasattr(parent, "_show_break_menu"):
                parent._show_break_menu(event.globalPosition().toPoint())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._hold_timer.stop()
            if self._held_triggered:
                # Hold already fired — suppress the click that would follow
                self._held_triggered = False
                self.setDown(False)
                event.accept()
                return
        super().mouseReleaseEvent(event)


class FocusFlowToolbar(QFrame):
    play_pause_requested = pyqtSignal()
    reset_requested = pyqtSignal()
    skip_requested = pyqtSignal()
    profile_switch_requested = pyqtSignal(str)
    collapse_changed = pyqtSignal(int)   # emits display_state (0–3)
    position_changed = pyqtSignal(int, int)
    break_requested = pyqtSignal(bool)   # True = long break, False = short break

    def __init__(self, config: dict) -> None:
        super().__init__(None)
        self._config = config
        self._drag_offset = None
        self._display_state: int = (
            0 if bool(config.get("toolbar", {}).get("collapsed", False)) else 1
        )   # 0=indicator only  1=timer  2=timer+due  3=due only
        self._condition_parts: list[str] = []
        self._profile_names: list[str] = []
        self._active_profile = ""
        self._profile_actions: dict[str, QAction] = {}
        self._break_display = False
        self._indicator_mode: str = "dot"   # "dot" or "percentage"
        self._hud_mode: str = "always"      # "always" | "hover" | "hidden"
        self._hud_hovered = False

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setObjectName("focusFlowToolbar")
        self.setFixedHeight(40)
        self._build_once()
        self.apply_config(config)

    # ── build ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_dark() -> bool:
        """Return True when Anki is running in dark/night mode."""
        try:
            from aqt.theme import theme_manager   # type: ignore[import]
            return bool(theme_manager.night_mode)
        except Exception as exc:
            log.debug("theme_manager unavailable, defaulting to light: %s", exc)
            return False

    def _apply_theme(self) -> None:
        """Build and apply the toolbar stylesheet for the current Anki theme.

        Called once on construction and again via refresh_theme() whenever
        Anki switches between light and dark mode.
        """
        night = self._is_dark()
        if night:
            bg        = "rgba(38, 38, 40, 225)"
            bg_hover  = "rgba(255,255,255,12)"
            text      = "#e0e0e0"
            sep_color = "rgba(255,255,255,55)"
            border    = "rgba(255,255,255,18)"
            menu_bg   = "rgba(46,46,48,248)"
            menu_brd  = "rgba(255,255,255,20)"
        else:
            bg        = "rgba(252, 252, 250, 210)"
            bg_hover  = "rgba(20, 20, 20, 18)"
            text      = "#222222"
            sep_color = "rgba(35,35,35,92)"
            border    = "rgba(35,35,35,38)"
            menu_bg   = "rgba(252,252,250,242)"
            menu_brd  = "rgba(35,35,35,42)"

        self.setStyleSheet(f"""
            QFrame#focusFlowToolbar {{
                background: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            QPushButton {{
                background: transparent;
                border: 0;
                color: {text};
                font-size: 12px;
                min-height: 26px;
                max-height: 26px;
                padding: 0 8px;
            }}
            QPushButton:hover {{
                background: {bg_hover};
                border-radius: 4px;
            }}
            QPushButton::menu-indicator {{ image: none; width: 0; }}
            QLabel {{
                color: {text};
                font-size: 12px;
                padding: 0 1px;
            }}
            QLabel#focusFlowSeparator {{
                color: {sep_color};
                font-size: 12px;
                padding: 0;
            }}
            QMenu {{
                background: {menu_bg};
                border: 1px solid {menu_brd};
                color: {text};
            }}
            QMenu::item {{
                padding: 5px 22px 5px 12px;
                background: transparent;
            }}
            QMenu::item:selected {{ background: {bg_hover}; }}
        """)

    def refresh_theme(self) -> None:
        """Re-apply the theme stylesheet.  Called when Anki switches themes."""
        self._apply_theme()

    def _build_once(self) -> None:
        self._apply_theme()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(7)
        layout.setSizeConstraint(QHBoxLayout.SizeConstraint.SetFixedSize)

        self.play_button = QPushButton("Study", self)
        self.play_button.setFixedWidth(58)
        self.play_button.clicked.connect(self.play_pause_requested.emit)

        self.sep_controls = self._separator()

        self.skip_button = QPushButton("Skip", self)
        self.skip_button.setFixedWidth(48)
        self.skip_button.clicked.connect(self.skip_requested.emit)

        self.sep_profile = self._separator()

        # Profile switcher — shows a dropdown chevron only.
        # The active profile name is visible in the tooltip on hover,
        # keeping the toolbar minimal while preserving full functionality.
        self.profile_button = QPushButton("▾", self)
        self.profile_button.setFixedWidth(22)
        self.profile_button.setToolTip("Profile: Default")
        self.profile_menu = QMenu(self)
        self.profile_button.setMenu(self.profile_menu)

        self.sep_timer = self._separator()

        # Timer doubles as a pause/resume button — click to toggle, hold to reset.
        # Styled with zero padding so the monospace text fills the fixed width.
        self.timer_label = HoldTimerButton("25:00", self)
        timer_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        timer_font.setPointSize(12)
        self.timer_label.setFont(timer_font)
        self.timer_label.setFixedWidth(54)
        self.timer_label.setStyleSheet("padding: 0;")
        self.timer_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.timer_label.setToolTip("Click to pause / resume · Hold to reset")
        self.timer_label.clicked.connect(self.play_pause_requested.emit)
        self.timer_label.held.connect(self.reset_requested.emit)

        self.sep_conditions = self._separator()

        # Conditions label is display-only. WA_TransparentForMouseEvents lets
        # click-and-drag events fall through to the parent frame so the user
        # can drag the toolbar from this area.
        self.conditions_label = QLabel("", self)
        self.conditions_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.conditions_label.setFixedWidth(120)
        self.conditions_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )

        # Opacity-only fade for "Reveal on hover" HUD mode — deliberately NOT
        # setVisible()/hide(), which would collapse this widget out of the
        # layout and shift the separator + everything to its right. This
        # keeps the reserved 120px area and the rest of the toolbar's layout
        # completely static; only the rendered pixels inside that fixed
        # space fade in/out. See _sync_hud_opacity() / enterEvent/leaveEvent.
        self._conditions_opacity = QGraphicsOpacityEffect(self.conditions_label)
        self.conditions_label.setGraphicsEffect(self._conditions_opacity)
        self._conditions_opacity.setOpacity(1.0)
        self._hud_fade = QPropertyAnimation(self._conditions_opacity, b"opacity", self)
        self._hud_fade.setDuration(150)

        self.sep_mode = self._separator()

        self.mode_label = QLabel("Studying", self)
        self.mode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mode_label.setFixedWidth(66)

        self.expanded_dot = FatigueDotButton(self)
        self.expanded_dot.clicked.connect(self.cycle_display_state)

        # Percentage indicator — shown instead of the dot when indicator == "percentage".
        # Clicking it also cycles the display state, matching the dot behaviour.
        self.fatigue_pct_label = QPushButton("--", self)
        self.fatigue_pct_label.setFixedWidth(38)
        self.fatigue_pct_label.setStyleSheet("padding:0;font-weight:600;font-size:12px;")
        self.fatigue_pct_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fatigue_pct_label.setToolTip("Fatigue score — click to cycle view")
        self.fatigue_pct_label.clicked.connect(self.cycle_display_state)

        for widget in (
            self.expanded_dot,
            self.fatigue_pct_label,
            self.play_button,
            self.sep_controls,
            self.skip_button,
            self.sep_profile,
            self.profile_button,
            self.sep_timer,
            self.timer_label,
            self.sep_conditions,
            self.conditions_label,
            self.sep_mode,
            self.mode_label,
        ):
            widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            layout.addWidget(widget)

    def _separator(self) -> QLabel:
        label = QLabel("|", self)
        label.setObjectName("focusFlowSeparator")
        label.setFixedWidth(10)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        return label

    # ── public setters ────────────────────────────────────────────────────────

    def apply_config(self, config: dict) -> None:
        self._config = config
        toolbar = config.get("toolbar", {})
        # Restore the full display state (0–3) if saved; fall back to the legacy
        # collapsed bool for configs written before this feature was added.
        if "display_state" in toolbar:
            self._display_state = max(0, min(3, int(toolbar["display_state"])))
        else:
            collapsed = bool(toolbar.get("collapsed", False))
            self._display_state = 0 if collapsed else 1
        mode = config.get("fatigue", {}).get("indicator", "dot")
        if mode in ("dot", "percentage"):
            self._indicator_mode = mode
        hud_mode = toolbar.get("hud_visibility", "always")
        if hud_mode not in ("always", "hover", "hidden"):
            hud_mode = "always"
        self._hud_mode = hud_mode
        self._sync_hud_opacity()
        position = toolbar.get("position", [80, 80])
        if (toolbar.get("remember_position", True)
                and isinstance(position, (list, tuple)) and len(position) == 2):
            self.move(int(position[0]), int(position[1]))
        self._apply_visibility()

    def _sync_hud_opacity(self, animate: bool = False) -> None:
        """Keep the HUD's rendered opacity in sync with the configured mode
        and current hover state. Opacity-only, as noted where the effect is
        created — the label's own visibility/geometry is never touched here,
        so this can never cause the toolbar to jump or resize. "hidden" mode
        is instead enforced in _apply_visibility(), the same mechanism
        already used for display states that don't show the HUD."""
        target = 1.0 if (self._hud_mode != "hover" or self._hud_hovered) else 0.0
        self._hud_fade.stop()
        if animate:
            self._hud_fade.setStartValue(self._conditions_opacity.opacity())
            self._hud_fade.setEndValue(target)
            self._hud_fade.start()
        else:
            self._conditions_opacity.setOpacity(target)

    def set_profiles(self, names: list[str], active_name: str) -> None:
        self._active_profile = active_name
        if names != self._profile_names:
            self._profile_names = list(names)
            self.profile_menu.clear()
            self._profile_actions.clear()
            for name in self._profile_names:
                action = QAction(name, self.profile_menu)
                action.setCheckable(True)
                action.triggered.connect(
                    lambda _checked=False, n=name: self.profile_switch_requested.emit(n)
                )
                self.profile_menu.addAction(action)
                self._profile_actions[name] = action
        for name, action in self._profile_actions.items():
            action.setChecked(name == active_name)
        self._render_profile()

    def set_timer(self, snapshot: TimerSnapshot) -> None:
        new_break_display = snapshot.mode == TimerMode.BREAK
        mode_changed = new_break_display != self._break_display
        self._break_display = new_break_display

        seconds = snapshot.remaining_seconds if snapshot.has_started else snapshot.total_seconds
        text = f"{seconds // 60:02d}:{seconds % 60:02d}"
        if self.timer_label.text() != text:
            self.timer_label.setText(text)

        # Dim the timer when paused — visual confirmation of the pause state
        paused_style = ("padding: 0; color: rgba(128,128,128,200);" if snapshot.is_paused else "padding: 0;")
        if self.timer_label.styleSheet() != paused_style:
            self.timer_label.setStyleSheet(paused_style)

        # BUG FIX: BREAK used to fall through to the same "Study" label as
        # IDLE, and _cmd_play_pause() in __init__.py only ever handled IDLE
        # and STUDY — so during a break the button read "Study" (implying
        # it would start a new session) but clicking it silently did
        # nothing. Treat BREAK the same as STUDY: it's a running timer that
        # can be paused/resumed just like a study session.
        if snapshot.mode == TimerMode.IDLE:
            label = "Study"
        else:
            label = "Resume" if snapshot.is_paused else "Pause"
        if self.play_button.text() != label:
            self.play_button.setText(label)

        if mode_changed:
            self._apply_visibility()

    def set_idle(self) -> None:
        if self.timer_label.text() != "--:--":
            self.timer_label.setText("--:--")
        if self.play_button.text() != "Study":
            self.play_button.setText("Study")
        was_break = self._break_display
        self._break_display = False
        if was_break:
            self._apply_visibility()

    def set_conditions(self, parts: list[str]) -> None:
        if parts == self._condition_parts:
            return
        self._condition_parts = parts
        self._render_conditions()
        self._apply_visibility()

    def set_conditions_tooltip(self, text: str | None) -> None:
        """Hover text on the due/goal label — used for the pace prediction
        ('At your current pace you'll finish today's reviews in ~17 minutes').
        Kept as a tooltip rather than another toolbar label so the toolbar
        itself stays compact; the extra detail is one hover away."""
        self.conditions_label.setToolTip(text or "")

    def set_mode(self, mode: str) -> None:
        labels = {"Studying": "Studying", "Learning": "Learning", "Creating": "Creating"}
        label = labels.get(mode, "Studying")
        if self.mode_label.text() != label:
            self.mode_label.setText(label)

    def set_fatigue(self, state: str, score: float) -> None:
        if state == "low_quality":
            color, tip_label = "#b6453a", "Low quality"
        elif state == "drifting":
            color, tip_label = "#b89d28", "Drifting"
        else:
            color, tip_label = "#3b8b4d", "Focused"
        self.expanded_dot.set_dot_color(color)
        pct = int(score * 100)
        self.fatigue_pct_label.setText(f"{pct}%")
        self.fatigue_pct_label.setStyleSheet(
            f"padding:0;font-weight:600;font-size:12px;color:{color};"
        )
        self.fatigue_pct_label.setToolTip(
            f"{tip_label} — {pct}% — click to collapse"
        )

    def set_indicator_mode(self, mode: str) -> None:
        """Switch between 'dot' and 'percentage' fatigue indicator."""
        if mode not in ("dot", "percentage"):
            mode = "dot"
        self._indicator_mode = mode
        self._apply_visibility()

    def show_for_reviewer(self) -> None:
        if not self.isVisible():
            self.show()
        self.raise_()

    def cycle_display_state(self) -> None:
        """Cycle: indicator-only → timer → timer+due → due-only → indicator-only → …

        When no due-card conditions are set, only states 0 and 1 are used.
        """
        has_cond = bool(self._condition_parts)
        if has_cond:
            self._display_state = (self._display_state + 1) % 4
        else:
            self._display_state = 0 if self._display_state != 0 else 1
        self._apply_visibility()
        self.collapse_changed.emit(self._display_state)

    def set_collapsed(self, collapsed: bool) -> None:
        """Force into fully-collapsed (state 0) or timer-visible (state 1)."""
        target = 0 if collapsed else max(1, self._display_state)
        if self._display_state == target:
            return
        self._display_state = target
        self._apply_visibility()
        self.collapse_changed.emit(self._display_state)

    # ── private ───────────────────────────────────────────────────────────────

    # Fixed layout order of every widget in the row (content items and the
    # separator that sits after each one, except the last). Kept as a single
    # source of truth so _apply_visibility() never has to hand-list which
    # separator goes with which widgets again — see the BUG FIX note there.
    _LAYOUT = (
        ("indicator", False), ("play", False), ("sep_controls", True),
        ("skip", False), ("sep_profile", True), ("profile", False),
        ("sep_timer", True), ("timer", False), ("sep_conditions", True),
        ("conditions", False), ("sep_mode", True), ("mode", False),
    )

    def _apply_visibility(self) -> None:
        has_cond = bool(self._condition_parts)
        use_pct  = (self._indicator_mode == "percentage")
        indicator = self.fatigue_pct_label if use_pct else self.expanded_dot
        ind_w     = 38 if use_pct else 28

        # When no due-card conditions exist, states 2 and 3 would show an
        # empty label.  Snap DOWN for rendering purposes only — do NOT write
        # back to self._display_state so the saved preference is preserved
        # and restored correctly once conditions become available.
        state = self._display_state

        if not has_cond and state in (2, 3):
            state = 1

        # BUG FIX (again): the previous version of this method hard-coded
        # play_button/skip_button/profile_button/mode_label into a single
        # "expanded cluster" shown in every state but 0. That fixed the
        # original "these buttons never show at all" bug, but it also made
        # them permanently mandatory whenever the toolbar wasn't fully
        # collapsed. They're now each controlled by their own Settings ->
        # Toolbar & Sound checkbox (all off by default, matching a
        # minimalist default toolbar), and are OFF whenever state == 0
        # regardless of their checkbox — clicking the indicator down to
        # "collapsed" should always mean fully minimal, no exceptions.
        tb_cfg = (self._config or {}).get("toolbar", {})
        collapsed = (state == 0)
        content_visible = {
            "indicator":  True,
            "play":       (not collapsed) and bool(tb_cfg.get("show_play_pause_button", False)),
            "skip":       (not collapsed) and bool(tb_cfg.get("show_skip_button", False)),
            "profile":    (not collapsed) and bool(tb_cfg.get("show_profile_button", False)),
            "timer":      (not collapsed) and state in (1, 2),
            "conditions": (not collapsed) and state in (2, 3) and self._hud_mode != "hidden",
            "mode":       (not collapsed) and bool(tb_cfg.get("show_mode_label", False)),
        }

        name_to_widget = {
            "indicator": indicator, "play": self.play_button, "skip": self.skip_button,
            "profile": self.profile_button, "timer": self.timer_label,
            "conditions": self.conditions_label, "mode": self.mode_label,
            "sep_controls": self.sep_controls, "sep_profile": self.sep_profile,
            "sep_timer": self.sep_timer, "sep_conditions": self.sep_conditions,
            "sep_mode": self.sep_mode,
        }
        names = [n for n, _ in self._LAYOUT]
        is_sep = dict(self._LAYOUT)

        # A separator only shows when it actually has visible content on
        # BOTH sides — this is what guarantees "N controls on, rest off"
        # never leaves a dangling or doubled-up "|", for any combination of
        # the 4 independent checkboxes, without hand-listing every case.
        #
        # Single left-to-right pass: carry at most one "pending" separator
        # across any run of hidden content, and only commit it once we reach
        # the next VISIBLE content item. An earlier version instead looked
        # at each separator's structurally-nearest content neighbour and
        # checked THAT item's visibility — which breaks two ways: (1) a
        # separator could be wrongly hidden when its immediate structural
        # neighbour was off even though something visible sat further out
        # past it (e.g. only Play + Timer on: the separator right after Play
        # has Skip as its structural neighbour, which is off, so it was
        # dropped even though Play and Timer are genuinely adjacent once
        # Skip/Profile collapse out); and (2) several separators spanning
        # the same hidden run could all resolve to the same visible pair on
        # both sides and all get shown at once, producing "| |" instead of
        # a single "|". Carrying one pending separator per gap avoids both.
        visible_names: list[str] = []
        pending_sep: str | None = None
        for name in names:
            if is_sep[name]:
                if pending_sep is None:
                    pending_sep = name
                continue
            if content_visible.get(name):
                if visible_names and pending_sep is not None:
                    visible_names.append(pending_sep)
                visible_names.append(name)
                pending_sep = None
            # hidden content: skip it, leaving any pending separator armed
            # for whichever visible item comes next.

        visible = {name_to_widget[n] for n in visible_names}

        # Fixed width of every widget except the indicator (which varies with
        # ind_w above) — used to compute new_width generically below so it
        # can never silently drift out of sync with `visible` the way the
        # old hand-written per-state formulas did.
        _WIDTHS = {
            self.play_button: 58, self.sep_controls: 10, self.skip_button: 48,
            self.sep_profile: 10, self.profile_button: 22,
            self.sep_timer: 10, self.timer_label: 54,
            self.sep_conditions: 10, self.conditions_label: 120,
            self.sep_mode: 10, self.mode_label: 66,
        }
        content_width = sum(_WIDTHS.get(w, ind_w) for w in visible)
        gaps = 7 * max(0, len(visible) - 1)   # QHBoxLayout spacing(7) between each pair
        new_width = 20 + content_width + gaps  # 20 = left + right contentsMargins

        if self.width() != new_width:
            self.setFixedWidth(new_width)

        all_widgets = (
            self.expanded_dot, self.fatigue_pct_label,
            self.play_button, self.sep_controls, self.skip_button,
            self.sep_profile, self.profile_button,
            self.sep_timer, self.timer_label,
            self.sep_conditions, self.conditions_label,
            self.sep_mode, self.mode_label,
        )
        for widget in all_widgets:
            widget.setVisible(widget in visible)

    def _render_profile(self) -> None:
        # Text is intentionally blank — the chevron (▾) is the only visual.
        # Active profile name is surfaced via tooltip on hover.
        self.profile_button.setToolTip(f"Profile: {self._active_profile}")

    def _render_conditions(self) -> None:
        label = " | ".join(self._condition_parts)
        self.conditions_label.setText(self._elided(label, self.conditions_label))

    def _elided(self, text: str, widget) -> str:
        metrics = QFontMetrics(widget.font())
        # Middle-elide (not right-elide): the progress bar's percentage sits
        # at the end of the string and must never be the part that gets cut.
        return metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, max(20, widget.width() - 8))

    # ── drag ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self._show_break_menu(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def _show_break_menu(self, global_pos) -> None:
        menu = QMenu(self)
        short_action = menu.addAction("Short break")
        long_action  = menu.addAction("Long break")
        chosen = menu.exec(global_pos)
        if chosen is short_action:
            self.break_requested.emit(False)
        elif chosen is long_action:
            self.break_requested.emit(True)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            self.position_changed.emit(self.x(), self.y())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:
        self._hud_hovered = True
        if self._hud_mode == "hover":
            self._sync_hud_opacity(animate=True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hud_hovered = False
        if self._hud_mode == "hover":
            self._sync_hud_opacity(animate=True)
        super().leaveEvent(event)
