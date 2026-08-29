from __future__ import annotations
import copy
from typing import Any


def strip_lone_surrogates(s: str) -> str:
    """Remove unpaired UTF-16 surrogate codepoints (U+D800–U+DFFF).

    These are invalid in UTF-8 and crash deep inside Qt/Anki's own encoding
    calls with "'utf-8' codec can't encode characters ...: surrogates not
    allowed" — taking down the entire deck browser render, not just this
    addon's panel. They typically end up in free-text fields (like a day
    note) from a clipboard paste of an emoji or other multi-byte character
    that got truncated mid-codepoint by whatever copied it. Applied both
    when text is saved (so it can't happen going forward) and when it's
    read back for rendering (so data already saved with a stray surrogate
    in it — from before this fix existed — stops crashing on every render).
    """
    if not s:
        return s
    return "".join(c for c in s if not (0xD800 <= ord(c) <= 0xDFFF))


_PROFILE_KEYS = ("timer", "end_conditions", "fatigue", "toolbar", "sound", "measurement")

_PROFILE_DEFAULTS: dict[str, Any] = {
    "timer": {
        "study_minutes": 25, "break_minutes": 5, "long_break_minutes": 15,
        "sessions_before_long_break": 4, "auto_resume_after_editor": True,
    },
    "end_conditions": {
        "cards_enabled": False, "cards_target": 25, "sessions_enabled": False,
        "sessions_target": 4, "max_time_enabled": False, "max_time_minutes": 25,
        "all_due_enabled": True,
    },
    "fatigue": {
        "sensitivity": 10,
        "soft_break_prompt": False,
        "show_daily_report": True,
        "adaptive_duration": False,
        "indicator": "dot",
        # ── new in v42 ────────────────────────────────────────────────────────
        # Minimum answered cards before fatigue scoring begins.  A value of 10
        # prevents early-session Again presses on new cards from triggering an
        # immediate break suggestion.
        "min_cards": 10,
        # Score threshold (0.0–1.0) below which the soft-break popup appears.
        # Kept separate from the display-state thresholds (0.35 / 0.60) so
        # users can tune the suggestion without affecting the indicator colour
        # or adaptive timer behaviour.
        "break_score": 0.35,
        # Per-signal toggles.  Set any signal to false to exclude it from the
        # combined score.  Useful when a specific signal fires too often for a
        # particular deck type (e.g. "iiv": false for chaotic mixed-interval decks).
        "signals": {
            "rt":    True,   # answer-time mean drift
            "iiv":   True,   # answer-time variability drift
            "again": True,   # weighted again-rate drift
            "lapse": True,   # individual lapse detection
        },
    },
    "toolbar": {
        "collapsed": False, "remember_position": True, "position": [80, 80],
        "auto_collapse_on_start": False, "display_state": 1,
        # "bar"   → progress bar + % when exactly one Goal is enabled (default)
        # "count" → always show the plain remaining count (old behaviour)
        "goal_display": "count",
        # "always" (default) → HUD area behaves exactly as before.
        # "hover"  → HUD area stays faded out until the mouse is over the
        #            toolbar, then fades in — the timer and indicator are
        #            unaffected either way.
        # "hidden" → HUD area never shows, regardless of display state.
        "hud_visibility": "always",
        # Independent show/hide toggles for the toolbar's optional controls —
        # each defaults OFF for a minimal toolbar (just the indicator dot and,
        # depending on display state, the timer/due-count). See Settings ->
        # Toolbar & Sound. Overridden to always-off while the toolbar is in
        # its fully-collapsed display state regardless of these values.
        "show_play_pause_button": False,
        "show_skip_button": False,
        "show_profile_button": False,
        "show_mode_label": False,
    },
    # "custom_path": "" uses the bundled default ding (assets/ding.wav);
    # set via Settings -> Toolbar & Sound -> "Custom sound" to point at any
    # .wav file of the user's own instead. Empty/missing/unreadable path
    # falls back to the bundled default (see SoundPlayer.effective_path()).
    "sound": {"enabled": True, "volume": 0.55, "custom_path": ""},
    # ── Measurement — how quality and effective time are computed ──────────
    "measurement": {
        # "focus"     → session fatigue score (default, existing behaviour)
        # "answers"   → weighted average of Again/Hard/Good/Easy buttons
        # "retention" → 1 − (again / total_reviews)
        "quality_source": "focus",
        "ans_weights": {"again": -2, "hard": -1, "good": 1, "easy": 2},
        # Cap on seconds counted per card; keeps outlier-heavy sessions honest.
        "eff_time_cap_secs": 90,
        # When True, Easy presses are excluded from the per-card time cap.
        "ignore_easy_time": True,
    },
}

_HEATMAP_DEFAULTS: dict[str, Any] = {
    "cell_size": 12,
    "cell_shape": "soft",  # sharp | soft | circle | diamond | star
    "theme": "system",      # system | light | dark — overrides Anki's own theme
    # for the heatmap panel specifically. "system" (default) follows
    # aqt.theme.theme_manager.night_mode like before; "light"/"dark" force
    # it regardless, as an escape hatch if that detection is ever wrong.
    # Off by default: the heatmap only pre-renders the last 3 years (this
    # year + 2 back) instead of every year since your first-ever review —
    # for a long-time user that's the difference between a light render and
    # regenerating years of SVG markup on every deck-browser refresh. Turn
    # this on to restore full-history ◀/▶ navigation at the cost of a
    # heavier panel.
    "heatmap_full_history": False,
    # How many days ahead of today to show as forecast/"due" cells (the blue
    # cells past today showing cards scheduled to come due). Previously
    # hardcoded to 30 with no way to see further out — bumped the default to
    # 30 still, but now user-configurable (see the Heatmap settings tab).
    "due_forecast_days": 30,
    # When True, ignores due_forecast_days above and forecasts as far out as
    # Anki's own scheduler ever goes (its own max interval is 36500 days /
    # 100 years — see _due_forecast_window() in __init__.py), so nothing due
    # later than that ever shows as an uncoloured, empty cell within
    # whichever year's grid is on screen. This still only affects the
    # *currently displayed* year: flip to next year to see further-out due
    # cards for that year the same way you'd flip a physical calendar.
    "due_forecast_unlimited": True,
    "note_marker_shape": "triangle",     # triangle | dot | square | ring
    "note_marker_position": "top_left",  # top_left|top_right|bottom_left|bottom_right|center
    # "" = deterministic theme default (fixed light/dark text-subtle colour,
    # see the _CSS bug-fix note in heatmap_widget.py); else a hex colour for
    # the weekday/month/week-number labels specifically.
    "label_color": "",
    "heavy_cards": 100,
    "light_cards": 50,
    "color_scheme": "forest",
    # Hex colour used when color_scheme == "custom" — the user picked this
    # from the full colour spectrum (QColorDialog) instead of a preset.
    "custom_scheme_color": "#5284C8",
    # Order the heatmap grid, progress bar, stat cards, and streak row render
    # in below the deck browser toolbar. Only takes visible effect while
    # "layout_customize_enabled" (below) is on, which reveals drag handles
    # so the user can rearrange them directly on the page; the saved order
    # is still respected either way.
    "layout_block_order": ["heatmap", "progress", "cards", "streak"],
    "grouping": "monthly",
    "color_mode": "time",
    # Which stats the 5 metric cards show when nothing has been clicked yet.
    # "today"    → today's stats (default)
    # "7days"    → trailing 7 days (today inclusive)
    # "30days"   → trailing 30 days (today inclusive)
    # "remember" → whatever day/period was last clicked, restored from
    #              "last_selected_date" below (falls back to "today" if
    #              nothing was ever selected, or that date is out of range)
    "default_stats_view": "today",
    "last_selected_date": "",   # ISO date "YYYY-MM-DD", only used by "remember"
    "day_notes": {},            # {"YYYY-MM-DD": "note text"} — right-click a day to add
    # Reminders the user explicitly dismissed, so they don't nag again on a
    # same-day relaunch. Scoped to "date" (today's ISO date when dismissed)
    # so it naturally resets once the day rolls over — dismissing a reminder
    # only silences it for its current occurrence, not forever.
    "dismissed_reminders": {"date": "", "events": []},
    "hide_native_stats_line": False,  # hide Anki's own "Studied N cards..." text
    # ── per-element visibility toggles (all True = show everything) ───────────
    "display": {
        "show_streak_lines": False,  # vertical lines through consecutive study days
        "show_goal_dot":     False,  # removed from Settings UI per user request
        "show_legend":       False,  # colour legend below the heatmap
        "show_week_nums":    True,   # week-number labels below the grid
        "show_eff_time":     False,  # Effective time metric card
        "show_quality":      False,  # Quality metric card
        "show_new_cards":    False,  # New cards metric card
        "show_reviews":      False,  # Reviews metric card (review events — see cards_reviewed below)
        "show_cards_reviewed": False,  # Cards Reviewed metric card (distinct cards, companion to Reviews)
        "show_rev_time":     False,  # Review time metric card
        # Reverted back to OFF by default (was briefly True — see prior
        # comment history — to match the Experience Wizard's "Timer,
        # Heatmap, Streak, Theme" promise, but that change has been rolled
        # back at the developer's request).
        "show_streak_info":  False,  # streak row (best / current streak + due today)
        "show_best_streak":  False,  # "N Day Best Streak" segment within the row
        "show_current_streak": False,# "Current: N days" segment within the row
        "show_due_today":    False,  # "N cards due today" segment within the row
        "show_attention":    False,  # again-rate attention strip in the streak row
        "show_total_reviews": False, # all-time total reviews counter in the streak row
        "show_trend":        True,   # quality trend arrow inside the Quality card
        "show_month_names":  True,   # month labels on the heatmap
        # "always" (default) → year text next to the ◀/▶ arrows behaves
        # exactly as before. "hover" → faded out until the mouse is over
        # the year-navigation area, then fades in — the arrows themselves
        # are completely unaffected either way. "hidden" → never shown.
        # Independent of toolbar.hud_visibility — a separate setting for a
        # separate part of the UI, not shared state.
        "year_visibility":   "always",
        "stats_popup":       True,   # True = show clicked stats in a pop-up
        "popup_auto_close_secs": 15, # 0 = stays open until closed/reclicked
        "colored_numbers":   False,  # per-card accent colour on each metric value
        "metric_label_color": "",    # "" = theme-adaptive default; else a hex colour
        # for the "Effective time / Study Quality / New cards / Reviews /
        # Cards Reviewed / Review time" label text specifically (not the big numbers below them)
        "streak_text_color": "",     # "" = theme-adaptive default; else a hex colour
        # for the streak row's base text (best/current streak, due today, etc)
        "comfortable_spacing": True, # extra breathing room between heatmap/stats/streak
        "card_depth":        True,   # rounded corners + subtle border + hover lift on cards
        "modern_typography": True,   # bigger metric numbers, smaller labels
        "show_progress_bar": False,  # "Today's Progress" bar under the heatmap
        "number_colors": {           # per-metric accent colour, user-editable
            "eff": "#5AA9FF", "quality": "#FFA94D", "new": "#4DD9E8",
            "cards": "#B48CFF", "revtime": "#5FD97A", "cards_reviewed": "#F08FB0",
        },
        "streak_line_color": "",     # "" = theme-adaptive default; else a hex colour
        "streak_line_thickness": "normal",  # thin | normal | thick
        "future_cell_color": "",     # "" = default blue (82,132,200); else a hex colour
        "note_marker_color": "",     # "" = default gold (#E8B84D); else a hex colour
        # BUG FIX: this used to default to "Arial", but the Settings dialog's
        # own load logic treats the font field as "untouched" only when this
        # is falsy ("" / not set) — see _font_family_touched in
        # settings_dialog.py. A truthy default here made every fresh install
        # look as if the user had explicitly chosen Arial, when the intent
        # was to follow Anki's own UI font (shown as e.g. "Segoe UI" on
        # Windows, purely as the font combo's own reported default — never
        # actually written to config until the user picks a font themselves).
        "card_font_family": "",      # "" = Anki's default UI font
        # BUG FIX: was 24, which silently overrode "Default" sizing (the
        # Settings dialog shows this field as "Default" only at 0 — see
        # hm_font_size.setSpecialValueText("Default")). 0 = follow
        # modern_typography's own sizing (32/18px) as documented below.
        "card_font_size": 0,         # 0 = follow modern_typography (32/18px)
        "progress_bar_color": "",    # "" = default blue #5AA9FF
        # BUG FIX: was 20, for the same reason as card_font_size above — the
        # Settings dialog only shows "Default" at 0
        # (hm_progress_height.setSpecialValueText("Default")).
        "progress_bar_height": 0,    # 0 = default 8px
        "progress_bar_width": 0,     # 0 = default 100% (full panel width)
        "show_future_cells": True,   # forecast/scheduled cells past today
        "show_empty_state_message": False,  # "No reviews yet..." line when a period is empty
        # When on, a small grip handle appears on the heatmap, stat cards,
        # streak row, and progress bar so the user can drag them into a
        # different order directly on the deck browser page (persisted to
        # "layout_block_order" above). Off by default so the panel looks
        # exactly as before until the user opts in.
        "layout_customize_enabled": False,
    },
}


_MISSING = object()  # sentinel for "no default supplied"

_TOP_LEVEL_KEYS = frozenset({
    "profiles", "active_profile", "heatmap", "experience_mode", "wizard_completed",
})

_EXPERIENCE_MODES = ("beginner", "basic", "intermediate", "advanced", "expert")


class ConfigManager:
    def __init__(self, module_name: str, addon_dir) -> None:
        from aqt import mw as _mw
        self._module_name = module_name
        self._addon_package = _mw.addonManager.addonFromModule(module_name)
        stored = _mw.addonManager.getConfig(self._addon_package) or {}
        self._config = self._normalize_config(stored)
        self.save()

    # ── profile API ───────────────────────────────────────────────────────────
    @property
    def data(self) -> dict[str, Any]:
        return self._config["profiles"][self.active_profile_name()]

    def full_config(self) -> dict[str, Any]:
        return copy.deepcopy(self._config)

    def profile_names(self) -> list[str]:
        return list(self._config["profiles"].keys())

    def active_profile_name(self) -> str:
        active = str(self._config.get("active_profile") or "Default")
        if active not in self._config["profiles"]:
            active = next(iter(self._config["profiles"]))
            self._config["active_profile"] = active
        return active

    def get(self, dotted_path: str, default: Any = _MISSING) -> Any:
        val: Any = self.data
        for part in dotted_path.split("."):
            if not isinstance(val, dict) or part not in val:
                if default is _MISSING:
                    raise KeyError(
                        f"[FocusFlow] Config key '{dotted_path}' not found "
                        f"(missing segment '{part}')"
                    )
                return default
            val = val[part]
        return val

    def set(self, dotted_path: str, value: Any) -> None:
        parts  = dotted_path.split(".")
        target = self.data
        for part in parts[:-1]:
            if not isinstance(target, dict):
                raise KeyError(
                    f"[FocusFlow] Config path '{dotted_path}' is not "
                    f"navigable at '{part}'"
                )
            if part not in target:
                target[part] = {}
            target = target[part]
        if not isinstance(target, dict):
            raise KeyError(
                f"[FocusFlow] Config path '{dotted_path}' cannot be set"
            )
        target[parts[-1]] = value

    def switch_profile(self, name: str) -> None:
        if name not in self._config["profiles"]: raise ValueError(f"Unknown profile: {name}")
        self._config["active_profile"] = name; self.save()

    def replace_full_config(self, new_data: dict[str, Any]) -> None:
        self._config = self._normalize_config(new_data)

    def delete_profile(self, name: str) -> None:
        if name not in self._config["profiles"]: raise ValueError(f"Unknown profile: {name}")
        if len(self._config["profiles"]) <= 1: raise ValueError("At least one profile is required")
        del self._config["profiles"][name]
        if self._config.get("active_profile") == name:
            self._config["active_profile"] = next(iter(self._config["profiles"]))
        self.save()

    # ── heatmap API ───────────────────────────────────────────────────────────
    def heatmap_config(self) -> dict[str, Any]:
        return copy.deepcopy(self._config.get("heatmap", _HEATMAP_DEFAULTS))

    def update_heatmap_config(self, data: dict[str, Any]) -> None:
        hm = copy.deepcopy(self._config.get("heatmap", _HEATMAP_DEFAULTS))
        for k, v in data.items():
            if k not in _HEATMAP_DEFAULTS:
                continue
            # Deep-merge the display sub-dict so individual keys can be updated
            # without wiping the rest.
            if k == "display" and isinstance(v, dict) and isinstance(hm.get("display"), dict):
                hm["display"].update(v)
            else:
                hm[k] = v
        self._config["heatmap"] = hm

    def save(self) -> None:
        from aqt import mw as _mw
        _mw.addonManager.writeConfig(self._addon_package, self._config)

    # ── experience mode / first-run wizard API ──────────────────────────────
    def experience_mode(self) -> str:
        return str(self._config.get("experience_mode", "beginner"))

    def set_experience_mode(self, mode: str) -> None:
        if mode not in _EXPERIENCE_MODES:
            raise ValueError(f"Unknown experience mode: {mode!r}")
        self._config["experience_mode"] = mode
        self.save()

    def wizard_completed(self) -> bool:
        return bool(self._config.get("wizard_completed", False))

    def mark_wizard_completed(self, chosen_mode: str = "beginner") -> None:
        self.set_experience_mode(chosen_mode)
        self._config["wizard_completed"] = True
        self.save()

    # ── private ───────────────────────────────────────────────────────────────
    def _normalize_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        from .logger import log as _log
        for key in raw:
            if key not in _TOP_LEVEL_KEYS:
                _log.warning(
                    "config: unexpected top-level key %r — not in schema, will be ignored.",
                    key,
                )
        if "profiles" not in raw:
            profile = {k: copy.deepcopy(raw.get(k, _PROFILE_DEFAULTS[k])) for k in _PROFILE_KEYS if k in raw}
            raw = {"profiles": {"Default": self._normalize_profile(profile)}, "active_profile": "Default"}

        profiles: dict[str, Any] = {}
        _raw_profiles = raw.get("profiles")
        if not isinstance(_raw_profiles, dict):
            _raw_profiles = {}
        for raw_name, raw_profile in _raw_profiles.items():
            name = self._clean(str(raw_name)) or "Default"
            if name in profiles:
                i = 2
                while f"{name} {i}" in profiles: i += 1
                new_name = f"{name} {i}"
                _log.warning(
                    "config: profile name collision — %r renamed to %r on import.",
                    name, new_name,
                )
                name = new_name
            profiles[name] = self._normalize_profile(raw_profile if isinstance(raw_profile, dict) else {})
        if not profiles:
            profiles["Default"] = self._default_profile()

        active = self._clean(str(raw.get("active_profile") or ""))
        if active not in profiles: active = next(iter(profiles))

        hm = copy.deepcopy(_HEATMAP_DEFAULTS)
        raw_hm = raw.get("heatmap")
        if not isinstance(raw_hm, dict):
            raw_hm = {}
        if "heavy_threshold" in raw_hm and "heavy_cards" not in raw_hm:
            raw_hm["heavy_cards"] = 50
        if "light_threshold" in raw_hm and "light_cards" not in raw_hm:
            raw_hm["light_cards"] = 15
        _SCHEME_REMAP = {"amber": "ember", "slate": "mono", "purple": "violet"}
        if raw_hm.get("color_scheme") in _SCHEME_REMAP:
            raw_hm["color_scheme"] = _SCHEME_REMAP[raw_hm["color_scheme"]]
        for k, v in raw_hm.items():
            if k not in _HEATMAP_DEFAULTS:
                continue
            if k == "display" and isinstance(v, dict):
                hm["display"].update(v)
            else:
                hm[k] = v

        # ── experience mode / first-run wizard ──────────────────────────────
        # "wizard_completed" only exists in configs saved by this version or
        # later. If it's missing we can't tell a brand-new install apart from
        # an upgrade from an older version just by that alone, so we also
        # check whether anything has actually been customised yet (more than
        # one profile, or a profile/heatmap that differs from the untouched
        # defaults). Untouched → treat as a genuine first run (wizard shows,
        # starts on Beginner). Anything customised → treat as an existing
        # user (skip the wizard, default to Expert so nothing changes for them).
        if "wizard_completed" in raw:
            wizard_completed = bool(raw["wizard_completed"])
            experience_mode = str(raw.get("experience_mode") or "beginner")
        else:
            looks_untouched = (
                list(profiles.keys()) == ["Default"]
                and profiles["Default"] == self._default_profile()
                and hm == copy.deepcopy(_HEATMAP_DEFAULTS)
            )
            wizard_completed = not looks_untouched
            experience_mode = "beginner" if looks_untouched else "expert"

        if experience_mode not in _EXPERIENCE_MODES:
            experience_mode = "beginner"

        return {
            "profiles": profiles,
            "active_profile": active,
            "heatmap": hm,
            "experience_mode": experience_mode,
            "wizard_completed": wizard_completed,
        }

    def _normalize_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        profile = self._default_profile()
        profile = self._deep_merge(profile, data)
        timer = profile.setdefault("timer", {})
        if "study_minutes" not in timer and "focus_minutes" in timer:
            timer["study_minutes"] = timer.pop("focus_minutes")
        timer.pop("focus_minutes", None)
        for k, v in _PROFILE_DEFAULTS["timer"].items(): timer.setdefault(k, v)
        for section in ("end_conditions", "fatigue", "toolbar", "sound", "measurement"):
            sec = profile.setdefault(section, {})
            for k, v in _PROFILE_DEFAULTS[section].items(): sec.setdefault(k, v)
        # Strip legacy section removed in earlier versions
        profile.pop("fatigue_weights", None)
        # Migrate any removed Goal display choice — "eta" (Estimated time to
        # finish), dropped in 1.0.15 because its underlying pace estimate
        # proved unreliable (see session_coordinator.py's
        # _pace_prediction()/_eta_short_text() docstrings), and "new_learn"
        # (New/Learning breakdown), dropped because a faithful 3-number
        # breakdown with real words never fit the toolbar's 120px HUD area.
        # Any config saved with an old value falls back to the plain count,
        # same as any other unrecognized value would.
        tb = profile["toolbar"]
        if tb.get("goal_display") not in ("bar", "count", "done_remaining"):
            tb["goal_display"] = "count"
        if tb.get("hud_visibility") not in ("always", "hover", "hidden"):
            tb["hud_visibility"] = "always"
        fat = profile["fatigue"]
        # Migrate old float sensitivity (0.0–1.0) to new int range (1–20).
        #
        # BUG FIX: this used to check `float(fat["sensitivity"]) <= 1.0`,
        # which also matched the new format's own valid minimum — int(1),
        # i.e. the "Very Low" preset — since 1 <= 1.0 is True. That meant
        # anyone who had picked the lowest sensitivity got it silently
        # flipped to round(1 * 20) = 20 (maximum sensitivity) on every
        # single Anki restart, exactly inverting their setting.
        #
        # Legacy configs always stored this as a genuine Python float
        # (e.g. 0.45); current configs always write a plain int (see
        # SensitivityWidget.value()). JSON round-tripping preserves that
        # distinction (1.0 loads back as float, 1 loads back as int), so
        # checking the type instead of just the value correctly leaves any
        # new-format int alone — including 1 — and only migrates a true
        # legacy float.
        _raw_sensitivity = fat.get("sensitivity")
        if isinstance(_raw_sensitivity, float) and _raw_sensitivity <= 1.0:
            fat["sensitivity"] = int(round(_raw_sensitivity * 20))
        # Deep-fill the signals sub-dict so individual keys added in future
        # versions appear in existing profiles without resetting user choices.
        sig_defaults = _PROFILE_DEFAULTS["fatigue"].get("signals", {})
        if sig_defaults:
            fat_sigs = fat.setdefault("signals", {})
            for k, v in sig_defaults.items():
                fat_sigs.setdefault(k, v)
        return profile

    def _default_profile(self) -> dict[str, Any]:
        return copy.deepcopy(_PROFILE_DEFAULTS)

    def _deep_merge(self, base: dict, incoming: dict) -> dict:
        for key, value in incoming.items():
            if key in base and isinstance(value, dict) and isinstance(base[key], dict):
                base[key] = self._deep_merge(base[key], value)
            elif key in base:
                base[key] = value
        return base

    def _clean(self, name: str) -> str:
        return " ".join(name.strip().split())
