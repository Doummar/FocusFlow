from __future__ import annotations

import json as _json
import math as _math
import re as _re
from datetime import date, timedelta

from aqt.qt import (
    QColor, QDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QPainter, QPointF, QPolygonF, QPushButton, QRectF,
    QSize, Qt, QVBoxLayout, QWidget,
)

from ..services.heatmap_service import HeatmapDay, HeatmapService, HeatmapStats, activity_total, raw_study_events, scheduler_today, _get_day_cutoff
from ..utils.config_manager import strip_lone_surrogates
from ..utils.logger import log


# ── colour schemes ────────────────────────────────────────────────────────────

def _scheme_from_hex(hexcolor: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Build a (lo, hi) heatmap gradient pair from a single user-picked colour.

    Mirrors how the built-in presets work: lo is a pale/desaturated tint for
    light-activity days, hi is the rich, saturated colour for heavy-activity
    days. Rather than ask the user to pick two colours, we derive both ends
    from the one colour they chose — hi is the colour itself (darkened
    slightly if it's very light so heavy days still read as "intense"), lo
    is a lightened tint of the same hue so the gradient stays coherent.
    """
    r, g, b = _hex_to_rgb(hexcolor, (82, 132, 200))
    # hi: darken a touch so pastel picks still look "heavy" at the top end
    hi = (max(0, int(r * 0.75)), max(0, int(g * 0.75)), max(0, int(b * 0.75)))
    # lo: lighten toward white for a visible-but-pale low end
    lo = (
        min(255, int(r + (255 - r) * 0.65)),
        min(255, int(g + (255 - g) * 0.65)),
        min(255, int(b + (255 - b) * 0.65)),
    )
    return lo, hi


COLOR_SCHEMES = {
    # Each entry: (lo_rgb, hi_rgb)
    # lo = pale/desaturated tint — days with few reviews (still clearly visible)
    # hi = rich, saturated colour — days with heavy activity
    # Quality (0→1) interpolates between lo and hi; alpha scales with volume.
    "forest": ((155, 220, 155),  ( 8,  88,  28)),   # vivid mint  → rich forest
    "ocean":  ((130, 195, 238),  ( 6,  40, 170)),   # vivid sky   → deep navy
    "ember":  ((252, 185, 105),  (172, 18,   6)),   # vivid amber → deep crimson
    "rose":   ((245, 168, 188),  (150,  8,  58)),   # vivid blush → deep raspberry
    "mono":   ((185, 185, 195),  ( 24,  24,  38)),  # cool grey   → near-black
    "violet": ((210, 168, 248),  ( 54,  6,  158))   # vivid lilac → deep violet
}

COLOR_SCHEME_LABELS = {
    "forest": "Forest  — vivid mint → rich forest",
    "ocean":  "Ocean   — vivid sky → deep navy",
    "ember":  "Ember   — vivid amber → deep crimson",
    "rose":   "Rose    — vivid blush → deep raspberry",
    "mono":   "Mono    — cool grey → near-black",
    "violet": "Violet  — vivid lilac → deep violet",
}

_DOW  = ["M", "T", "W", "T", "F", "S", "S"]
_CELL = 12
_GAP  = 2

# Locale-independent weekday/month abbreviations.
#
# BUG FIX: tooltips, month labels, and axis captions throughout the heatmap
# used to call date.strftime("%a"/"%b") directly. Those directives are
# locale-dependent — on a system whose OS display language isn't English,
# Python picks up that locale for time formatting, so e.g. "Thu, 16 Jul
# 2026" silently became "to, 16 jul 2026" (Danish "torsdag"/"juli", both
# lower-case, different abbreviation length). FocusFlow's UI is English-only
# by design, so every date label is built from these fixed tables instead of
# trusting the OS locale.
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _fmt_weekday_date(d: date) -> str:
    """Locale-independent 'Thu, 16 Jul 2026' style label."""
    return f"{_WEEKDAY_ABBR[d.weekday()]}, {d.day} {_MONTH_ABBR[d.month - 1]} {d.year}"


def _fmt_month(d: date) -> str:
    """Locale-independent 3-letter month abbreviation, e.g. 'Jul'."""
    return _MONTH_ABBR[d.month - 1]


def _fmt_month_year(d: date) -> str:
    """Locale-independent 'Jul 2026' style label."""
    return f"{_MONTH_ABBR[d.month - 1]} {d.year}"


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_mins(minutes: float) -> str:
    total = int(round(minutes))
    if total < 60:
        return f"{total}m"
    h, m = divmod(total, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _fmt_secs(seconds: int) -> str:
    return _fmt_mins(seconds / 60.0)


def _past_fill(d: HeatmapDay, max_eff: float, lo: tuple, hi: tuple) -> str:
    """Two-channel fill: hue = quality ratio, brightness = activity volume.

    Alpha uses a power curve so low-activity days are more readable than a
    linear scale would produce.  Low-intensity cells fade toward white (not
    grey) to avoid a muddy appearance.
    """
    intensity = 0.0 if max_eff <= 0 else min(1.0, d.effective_minutes / max_eff)
    if intensity <= 0:
        return "var(--canvas-subtle,rgba(195,191,186,0.30))"
    q  = max(0.0, min(1.0, d.quality))
    r_ = int(lo[0] + (hi[0] - lo[0]) * q)
    g_ = int(lo[1] + (hi[1] - lo[1]) * q)
    b_ = int(lo[2] + (hi[2] - lo[2]) * q)
    # Power curve: makes low-activity days noticeably visible without
    # swamping high-activity days
    alpha = 0.38 + 0.59 * (intensity ** 0.50)
    # Fade toward white for very low activity (cleaner than fading to grey)
    if intensity < 0.40:
        fade = (0.40 - intensity) / 0.40
        r_ = int(r_ + (245 - r_) * fade * 0.45)
        g_ = int(g_ + (245 - g_) * fade * 0.45)
        b_ = int(b_ + (242 - b_) * fade * 0.45)
    return f"rgba({r_},{g_},{b_},{alpha:.2f})"


def _quality_fill(d: HeatmapDay) -> str:
    """Colour driven purely by quality ratio.

    Uses a three-point lerp:
        q = 0.0  →  deep red     (180, 58, 48)
        q = 0.5  →  warm amber   (192, 152, 38)
        q = 1.0  →  forest green ( 48, 128, 65)

    Intensity scales with total card count so days with very few cards appear
    faint.  Zero-card days return the empty-cell colour.
    """
    total = d.reviews_count + d.new_cards + d.relearned
    if total == 0:
        return "var(--canvas-subtle,rgba(195,191,186,0.30))"
    q = max(0.0, min(1.0, d.quality))
    if q >= 0.5:
        t = (q - 0.5) / 0.5
        r_ = int(215 + ( 18 - 215) * t)   # vivid amber → vivid green
        g_ = int(135 + (148 - 135) * t)
        b_ = int( 15 + ( 36 -  15) * t)
    else:
        t = q / 0.5
        r_ = int(200 + (215 - 200) * t)   # vivid red → amber
        g_ = int( 30 + (135 -  30) * t)
        b_ = int( 28 + ( 15 -  28) * t)
    alpha = min(0.97, 0.44 + 0.53 * min(1.0, total / 22.0) ** 0.52)
    return f"rgba({r_},{g_},{b_},{alpha:.2f})"


def _time_fill(d: HeatmapDay, max_time: float, lo: tuple, hi: tuple) -> str:
    """Colour driven by total review time — same hue channel as reviews mode.

    Uses the same power-curve alpha and white-blend as _past_fill.
    Zero review-time days return the empty-cell colour.
    """
    if d.review_time_seconds <= 0:
        return "var(--canvas-subtle,rgba(195,191,186,0.30))"
    intensity = min(1.0, d.review_time_seconds / max(max_time, 1.0))
    q  = max(0.0, min(1.0, d.quality))
    r_ = int(lo[0] + (hi[0] - lo[0]) * q)
    g_ = int(lo[1] + (hi[1] - lo[1]) * q)
    b_ = int(lo[2] + (hi[2] - lo[2]) * q)
    alpha = 0.28 + 0.64 * (intensity ** 0.65)
    if intensity < 0.40:
        fade = (0.40 - intensity) / 0.40
        r_ = int(r_ + (245 - r_) * fade * 0.45)
        g_ = int(g_ + (245 - g_) * fade * 0.45)
        b_ = int(b_ + (242 - b_) * fade * 0.45)
    return f"rgba({r_},{g_},{b_},{alpha:.2f})"


def _hex_to_rgb(hexcolor: str, fallback: tuple[int, int, int] = (82, 132, 200)) -> tuple[int, int, int]:
    h = hexcolor.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return fallback
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return fallback


def _future_fill(d: HeatmapDay, max_due: float, base_rgb: tuple[int, int, int] = (82, 132, 200)) -> str:
    """Future cells: cool tint scaled by due-card load (blue by default,
    customizable via Settings -> Display -> Heatmap Cells -> the swatch next
    to "Future cells").

    Days with no due cards show a very subtle neutral fill (not hatch) so
    users can tell the cell exists without wondering what the lines mean.
    Days with due cards show an increasingly saturated tint.
    """
    if d.due_cards <= 0:
        return "rgba(160,162,168,0.10)"
    intensity = min(1.0, d.due_cards / max(max_due, 1))
    alpha = 0.16 + 0.48 * (intensity ** 0.70)
    r, g, b = base_rgb
    return f"rgba({r},{g},{b},{alpha:.2f})"


def _note_marker_svg(x: int, y: int, cell: int, shape: str, position: str,
                     color: str = "#E8B84D") -> str:
    """Small marker shown on cells with a right-click note/reminder attached.

    shape:    triangle (corner notch, default) | dot | square | ring
    position: top_left | top_right | bottom_left | bottom_right | center
    color:    hex colour, defaults to the original gold (#E8B84D)
    """
    color = color or "#E8B84D"
    if shape == "triangle":
        _nm = max(3, int(cell * 0.32))
        # A corner-notch triangle only makes sense pinned to an actual
        # corner — "center" falls back to the marker's original spot.
        corner = position if position != "center" else "bottom_right"
        if corner == "bottom_left":
            pts = f"{x},{y + cell} {x + _nm},{y + cell} {x},{y + cell - _nm}"
        elif corner == "top_right":
            pts = f"{x + cell - _nm},{y} {x + cell},{y} {x + cell},{y + _nm}"
        elif corner == "top_left":
            pts = f"{x},{y} {x + _nm},{y} {x},{y + _nm}"
        else:  # bottom_right (default)
            pts = f"{x + cell - _nm},{y + cell} {x + cell},{y + cell} {x + cell},{y + cell - _nm}"
        return f'<polygon points="{pts}" fill="{color}" pointer-events="none"/>'

    # dot / square / ring: a small shape near the chosen anchor, inset from
    # the cell edge so it doesn't get visually clipped by neighbouring cells.
    _r = max(1.3, cell * 0.14)
    _inset = _r * 1.4
    anchors = {
        "top_left":     (x + _inset,        y + _inset),
        "top_right":    (x + cell - _inset, y + _inset),
        "bottom_left":  (x + _inset,        y + cell - _inset),
        "bottom_right": (x + cell - _inset, y + cell - _inset),
        "center":       (x + cell / 2,      y + cell / 2),
    }
    cx, cy = anchors.get(position, anchors["bottom_right"])
    if shape == "dot":
        return f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{_r:.1f}" fill="{color}" pointer-events="none"/>'
    if shape == "ring":
        return (
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{_r:.1f}" fill="none" '
            f'stroke="{color}" stroke-width="{max(1.0, _r * 0.4):.1f}" pointer-events="none"/>'
        )
    # "square"
    _s = _r * 1.7
    return (
        f'<rect x="{cx - _s / 2:.1f}" y="{cy - _s / 2:.1f}" width="{_s:.1f}" height="{_s:.1f}" '
        f'rx="{_s * 0.2:.1f}" fill="{color}" pointer-events="none"/>'
    )


def _star_points(cx: float, cy: float, r_outer: float, r_inner: float) -> str:
    """5-pointed star polygon points (tip pointing up), for the 'star' cell shape."""
    pts = []
    for i in range(10):
        ang = -_math.pi / 2 + i * _math.pi / 5   # start pointing up
        r = r_outer if i % 2 == 0 else r_inner   # alternate outer/inner vertices
        pts.append(f"{cx + r * _math.cos(ang):.2f},{cy + r * _math.sin(ang):.2f}")
    return " ".join(pts)


def _cell_rect(d: HeatmapDay, x: int, y: int, cell: int, fill: str,
               detail: str, is_today: bool,
               fill_quality: str = "", fill_time: str = "",
               show_goal_dot: bool = True, has_note: bool = False,
               shape: str = "soft",
               note_marker_shape: str = "triangle",
               note_marker_position: str = "bottom_right",
               note_marker_color: str = "") -> str:
    """Render one heatmap cell.

    Wrapped in a <g> with a <title> child so the date label appears as a
    native SVG tooltip on mouse-over — visible only while hovering, no
    permanent screen clutter.

    `shape` picks the cell's silhouette (Settings → Heatmap → Cell shape):
      sharp   — square, hard corners (rx=0)
      soft    — square with a light rounding (default, ~22% of the side)
      rounded — square with a heavier rounding (~40% of the side)
      circle  — fully round (pill/dot), rx = half the side
      diamond — square rotated 45°, scaled to fit within the cell's footprint
    """
    esc       = detail.replace('"', "&quot;")
    tc        = " ff-today" if is_today else ""
    mode_attrs = f'data-fill-reviews="{fill}"'
    if fill_quality:
        mode_attrs += f' data-fill-quality="{fill_quality}"'
    if fill_time:
        mode_attrs += f' data-fill-time="{fill_time}"'

    # Week/month membership keys — let a week-number or month-label click
    # find and highlight every cell in that period, the same way a day
    # click highlights itself. ISO year+week (not just week number) avoids
    # collisions at year boundaries (e.g. late-Dec dates in ISO week 1).
    _iso_year, _iso_week, _ = d.day.isocalendar()
    week_key  = f"{_iso_year}-{_iso_week:02d}"
    month_key = f"{d.day.year}-{d.day.month:02d}"

    # Hover tooltip: show due count for future cells and today's remaining
    tip_date = _fmt_weekday_date(d.day)
    if d.is_future:
        tip = (f"{tip_date}  —  {d.due_cards} cards due"
               if d.due_cards > 0 else f"{tip_date}  —  no cards due")
    elif is_today and d.due_cards > 0:
        tip = f"{tip_date}  —  {d.due_cards} still due today"
    else:
        tip = tip_date
    if has_note:
        tip += "  \U0001F4CC has a note (right-click to view/edit)"

    # Corner radius / geometry depends on the chosen shape. Diamond and star
    # are special cases:
    #  - diamond: a square rotated 45° would poke into neighbouring cells at
    #    full size, so its side is shrunk to cell/√2 and centred before the
    #    rotation is applied, keeping its bounding box inside the cell's own
    #    footprint.
    #  - star: needs a <polygon>, not a <rect> — "Rounded square" and
    #    "Circle" looked too similar at small cell sizes to tell apart, so
    #    this replaced "rounded" as a visually distinct option.
    _is_diamond = (shape == "diamond")
    _is_star = (shape == "star")
    _tag = "polygon" if _is_star else "rect"
    if _is_diamond:
        _side = round(cell / 1.41421356, 2)
        _rx   = max(0.8, round(cell * 0.08, 1))
        _rx_x, _rx_y = x + (cell - _side) / 2, y + (cell - _side) / 2
        _cx, _cy = x + cell / 2, y + cell / 2
        _shape_attrs = (
            f'x="{_rx_x:.2f}" y="{_rx_y:.2f}" width="{_side}" height="{_side}" '
            f'rx="{_rx}" transform="rotate(45 {_cx:.1f} {_cy:.1f})"'
        )
    elif _is_star:
        _cx, _cy = x + cell / 2, y + cell / 2
        _r_outer, _r_inner = cell * 0.52, cell * 0.21
        _shape_attrs = (
            f'points="{_star_points(_cx, _cy, _r_outer, _r_inner)}" '
            f'data-star="1" data-cx="{_cx:.2f}" data-cy="{_cy:.2f}" '
            f'data-ro="{_r_outer:.2f}" data-ri="{_r_inner:.2f}"'
        )
    else:
        if shape == "sharp":
            _rx = 0.0
        elif shape == "circle":
            _rx = round(cell / 2, 1)
        else:  # "soft" (default) — light rounding, works at every cell size
            _rx = max(1.4, round(cell * 0.22, 1))
        _shape_attrs = f'x="{x}" y="{y}" width="{cell}" height="{cell}" rx="{_rx}"'

    rect = (
        f'<g class="ff-cell-wrap">'
        f'<{_tag} class="ff-cell{tc}" {_shape_attrs} fill="{fill}" '
        f'{mode_attrs} data-json="{esc}" '
        f'data-week="{week_key}" data-month="{month_key}" '
        f'style="cursor:pointer;stroke:rgba(120,114,107,0.32);stroke-width:0.6"/>'
    )
    # Subtle glossy sheen: a light-to-dark diagonal overlay reusing one
    # shared gradient (ff-cell-sheen, defined once in _DEFS) so every cell
    # gets a soft "raised card" highlight without needing a per-colour
    # gradient definition. Non-interactive so it never intercepts clicks.
    # Grouped with the base cell in <g class="ff-cell-wrap"> so hover's
    # scale/lift animates both together — otherwise the sheen stayed put
    # while the coloured cell scaled up, leaving it visibly misaligned.
    rect += (
        f'<{_tag} {_shape_attrs} '
        f'fill="url(#ff-cell-sheen)" pointer-events="none"/></g>'
    )
    inner = f'<title>{tip}</title>' + rect

    if is_today:
        # Animated ring (pulsing) so today stands out clearly at a glance.
        # For diamond/star cells, the ring needs to match that geometry too
        # — otherwise it'd show as a plain square outline around a rotated
        # diamond or a star, visually mismatched.
        if _is_diamond:
            _ring_tag = "rect"
            _ring_attrs = (
                f'x="{_rx_x + 0.5:.2f}" y="{_rx_y + 0.5:.2f}" '
                f'width="{_side - 1:.2f}" height="{_side - 1:.2f}" rx="{_rx}" '
                f'transform="rotate(45 {_cx:.1f} {_cy:.1f})"'
            )
        elif _is_star:
            _ring_tag = "polygon"
            _ring_attrs = f'points="{_star_points(_cx, _cy, _r_outer + 1.2, _r_inner + 0.6)}"'
        else:
            _ring_tag = "rect"
            _ring_attrs = (
                f'x="{x + 0.5:.1f}" y="{y + 0.5:.1f}" '
                f'width="{cell - 1}" height="{cell - 1}" rx="{_rx}"'
            )
        inner += (
            f'<{_ring_tag} id="ff-today-ring" class="ff-today-ring" {_ring_attrs} fill="none" '
            f'stroke="var(--text-fg,rgba(40,40,40,0.9))" '
            f'stroke-width="2.0" pointer-events="none"/>'
        )
    if show_goal_dot and d.is_goal_reached and not d.is_future and d.sessions > 0:
        # Corner triangle — a right-angle notch in the top-right of the cell.
        # Much cleaner than a floating circle: no visual noise, scales with cell.
        _tri = max(3, int(cell * 0.35))  # ~4 px at 12 px cell, ~5 px at 15 px
        inner += (
            f'<polygon '
            f'points="{x + cell - _tri},{y} {x + cell},{y} {x + cell},{y + _tri}" '
            f'fill="rgba(255,255,255,0.82)" pointer-events="none"/>'
        )
    if has_note:
        # Marker for days with a right-click note/reminder attached — shape
        # and position are user-configurable (Settings → Heatmap → Note
        # marker), default matches the original bottom-right triangle notch.
        inner += _note_marker_svg(x, y, cell, note_marker_shape, note_marker_position, note_marker_color)
    return f'<g>{inner}</g>'


def _day_json(d: HeatmapDay, heavy_cards: int, light_cards: int,
              notes: dict | None = None) -> str:
    """Return a compact JSON string for the cell's data-json attribute.

    The JS reads this to update the metric cards in place — no dropdown,
    no extra HTML card.  Keys match the metric card element IDs:
      eff      → Effective time (formatted string)
      q_raw    → Quality as float 0-1
      new      → New cards count
      cards    → Reviews count (review events — see cards_reviewed below
                 for the distinct-card count)
      cards_reviewed → Distinct cards behind `cards` (same event types,
                 deduplicated by card id — a card reviewed several times
                 this day, e.g. relearning or repeated cram-deck study,
                 still only counts once here)
      revtime  → Review time (formatted string)
      ctx      → Human-readable context label (e.g. "Mon, 2 Jun 2026")
      sessions → Session count (shown in context line)
      state    → "future" | "no_data" | "no_session" | "session"
      due      → Due cards count (future days)
      load     → "heavy" | "moderate" | "light" | "" (future days)
      note     → User-written note/reminder for this day (right-click to add)
    """
    import json as _j

    _note_raw = (notes or {}).get(d.day.isoformat(), "")
    _note = _note_raw.get("text", "") if isinstance(_note_raw, dict) else str(_note_raw or "")
    # BUG FIX: a lone UTF-16 surrogate in saved note text (from a broken
    # clipboard paste of an emoji, typically) crashes the ENTIRE deck
    # browser render with UnicodeEncodeError deep inside Qt/Anki's own
    # UTF-8 encoding — not just this addon's panel. Strip it here so
    # already-corrupted data (saved before this fix existed) stops
    # crashing on every single render, not just newly-saved notes.
    _note = strip_lone_surrogates(_note)

    # Use cross-platform date formatting (%-d fails on Windows)
    ctx = _fmt_weekday_date(d.day)

    if d.is_future:
        load = ("heavy"    if d.due_cards >= heavy_cards else
                "moderate" if d.due_cards >= light_cards else
                "light")
        return _j.dumps({
            "state":   "future",
            "ctx":     ctx,
            "date":    d.day.isoformat(),
            "start":   d.day.isoformat(),
            "end":     d.day.isoformat(),
            "note":    _note,
            "due":     d.due_cards,
            "rev_due": d.rev_due,
            "new_due": d.new_due,
            "lrn_due": 0,
            "load":    load,
            "is_today": False,
            "eff":     "—", "q_raw": 0, "new": 0, "cards": 0,
            "cards_reviewed": 0, "revtime": "—",
            "sessions": 0,
        })

    total = d.reviews_count + d.new_cards + d.relearned

    if d.sessions == 0 and total == 0:
        _is_today = (d.day == date.today())
        return _j.dumps({
            "state": "no_data", "ctx": ctx,
            "date":  d.day.isoformat(),
            "start": d.day.isoformat(),
            "end":   d.day.isoformat(),
            "note":  _note,
            "eff": "—", "q_raw": 0, "new": 0, "cards": 0,
            "cards_reviewed": 0, "revtime": "—",
            "sessions": 0,
            "due":      d.due_cards,
            "new_due":  d.new_due,
            "lrn_due":  d.lrn_due,
            "rev_due":  d.rev_due,
            "is_today": _is_today,
            "load": "",
        })

    rev_mins = round(d.review_time_seconds / 60, 1) if d.review_time_seconds else 0
    _is_today = (d.day == date.today())
    return _j.dumps({
        "state":    "no_session" if d.sessions == 0 else "session",
        "ctx":      ctx,
        "date":     d.day.isoformat(),
        "start":    d.day.isoformat(),
        "end":      d.day.isoformat(),
        "note":     _note,
        "eff":      _fmt_mins(d.effective_minutes),
        "q_raw":    round(d.quality, 4),
        "new":      d.new_cards,
        # Reviews total = RAW (undeduplicated) types 0+1+2+3 (Learning+Review+
        # Relearning+Filtered), matching Anki's own LIVE studied_today()
        # semantics via raw_study_events() — the same canonical figure every
        # tab stat, the week/month popup, and _day_as_stat all use, so the
        # day cell, tabs, and popups always agree for the same day.
        "cards":    raw_study_events(d),
        "cards_reviewed": d.cards_reviewed,
        "revtime":  _fmt_mins(rev_mins),
        "sessions": d.sessions,
        # due_cards is non-zero only for today (populated from sched.counts())
        "due":      d.due_cards,
        "new_due":  d.new_due,
        "lrn_due":  d.lrn_due,
        "rev_due":  d.rev_due,
        "is_today": _is_today,
        "load": "",
    })
def _compute_streaks(days: list[HeatmapDay]) -> tuple[int, int]:
    """Count consecutive days where the configured goal was reached.

    is_goal_reached is True for every study day when no goal is set,
    and True only for explicitly goal-met days when a goal is active.
    """
    today        = scheduler_today(_get_day_cutoff())
    study_days   = {d.day for d in days if not d.is_future and d.is_goal_reached}
    longest = current = 0
    run = 0
    # BUG FIX: this used to always start the backward walk at `today` — so
    # whenever today's goal isn't finished YET (still midday, cards left),
    # `today in study_days` fails immediately and the streak shows as 0,
    # discarding any number of genuinely-completed consecutive prior days.
    # The user has until midnight to still finish today, so today shouldn't
    # get to break the display before the day is even over. See the
    # matching fix in HeatmapService.goal_streak(), which this function's
    # result gets max()'d against in __init__.py — both needed the same
    # fix, since either one alone still being wrong would drag max() down.
    d = today if today in study_days else today - timedelta(days=1)
    while d in study_days:
        run += 1
        d -= timedelta(days=1)
    current = run
    # Walk all days for longest
    sorted_days = sorted(study_days)
    if sorted_days:
        run = 1
        for i in range(1, len(sorted_days)):
            if (sorted_days[i] - sorted_days[i - 1]).days == 1:
                run += 1
                longest = max(longest, run)
            else:
                run = 1
        longest = max(longest, run)
    return current, longest


def _streak_lines(days: list[HeatmapDay],
                  positions: dict[date, tuple[int, int]],
                  cell: int,
                  show: bool = True,
                  line_color: str = "",
                  thickness: float = 1.0) -> str:
    """Draw streak indicators along consecutive study day runs.

    Within each week-column, a vertical bar runs from the first to the last
    day of the streak in that column.  Adjacent column segments are joined by
    a diagonal connector so multi-week streaks form a continuous path rather
    than a misleading flat horizontal line.

    `line_color`: empty string (default) keeps the theme-adaptive neutral
    colour; a hex value overrides it with a user-chosen colour (Settings ->
    Display -> Heatmap Cells -> the swatch next to "Streak lines").
    `thickness`: multiplier on the auto-computed stroke width (Settings ->
    Display -> Heatmap Cells -> the dropdown next to "Streak lines"). 1.0 = default.
    """
    if not show:
        return ""
    study_days  = {d.day for d in days if not d.is_future and d.is_goal_reached}
    sorted_days = sorted(study_days)
    if not sorted_days:
        return ""

    half  = cell // 2
    inset = max(2, cell // 5)
    sw    = max(1.5, cell / 8) * max(0.3, thickness)
    if line_color:
        color = line_color
        conn_color = line_color
        conn_opacity = ' opacity="0.55"'
    else:
        color = "var(--text-fg,rgba(60,60,60,0.22))"
        conn_color = "var(--text-fg,rgba(60,60,60,0.14))"
        conn_opacity = ""
    pieces: list[str] = []

    i = 0
    while i < len(sorted_days):
        # Collect the next consecutive run
        j = i + 1
        while j < len(sorted_days) and (sorted_days[j] - sorted_days[j - 1]).days == 1:
            j += 1
        run = sorted_days[i:j]
        i   = j

        if len(run) < 2:
            continue

        # Group run days by their x-column (each week = one column)
        by_col: dict[int, list[int]] = {}   # col_x → sorted list of y values
        for d in run:
            if d not in positions:
                continue
            cx, cy = positions[d]
            by_col.setdefault(cx, []).append(cy)

        sorted_cols = sorted(by_col)
        for ci, col_x in enumerate(sorted_cols):
            ys = sorted(by_col[col_x])
            mx = col_x + half

            # Vertical bar within this column segment
            y_top = ys[0]  + inset
            y_bot = ys[-1] + cell - inset
            if y_top < y_bot:
                pieces.append(
                    f'<line x1="{mx}" y1="{y_top}" x2="{mx}" y2="{y_bot}" '
                    f'stroke="{color}" stroke-width="{sw:.1f}" '
                    f'stroke-linecap="round" pointer-events="none"/>'
                )

            # Diagonal connector to the top of the next column segment
            if ci + 1 < len(sorted_cols):
                nx      = sorted_cols[ci + 1]
                ny_list = sorted(by_col[nx])
                mx2     = nx + half
                conn_y1 = ys[-1]      + cell - inset
                conn_y2 = ny_list[0]  + inset
                pieces.append(
                    f'<line x1="{mx}" y1="{conn_y1}" '
                    f'x2="{mx2}" y2="{conn_y2}" '
                    f'stroke="{conn_color}" stroke-width="{sw * 0.65:.1f}"{conn_opacity} '
                    f'stroke-linecap="round" pointer-events="none"/>'
                )

    return "".join(pieces)


# ── three layout engines ──────────────────────────────────────────────────────

def _period_json(days: list["HeatmapDay"], ctx_label: str) -> str:
    """Aggregate a list of HeatmapDay objects into a data-json string.

    Used by week-number and month-label SVG elements so clicking them
    updates the metric cards with that period's totals.
    """
    import json as _j
    past = [d for d in days if not d.is_future and d.sessions > 0]
    all_past = [d for d in days if not d.is_future]
    if not all_past:
        return _j.dumps({"state": "no_data", "ctx": ctx_label,
                          "eff": "—", "q_raw": 0, "new": 0,
                          "cards": 0, "cards_reviewed": 0, "revtime": "—",
                          "sessions": 0, "due": 0, "load": ""})
    eff   = sum(d.effective_minutes   for d in past)
    q     = (sum(d.quality * max(d.reviews_count,1) for d in past)
             / max(sum(max(d.reviews_count,1) for d in past), 1)) if past else 0.0
    new   = sum(d.new_cards           for d in all_past)
    # Reviews total = RAW types 0+1+2+3, matching the day-popup and
    # tab-level "Reviews" figures via raw_study_events().
    cards = sum(raw_study_events(d) for d in all_past)
    # NOTE: summed from each day's already-deduplicated count, so a card
    # reviewed on more than one day within this week/month is counted once
    # per day it was touched, not once for the whole period — getting a
    # true period-wide distinct count would mean a fresh COUNT(DISTINCT
    # cid) query scoped to this exact week/month (see
    # HeatmapService._distinct_cards_reviewed, used for the Today/Week/
    # Month/... tabs), which this function doesn't have DB access to run.
    # Close enough for a week/month label popup; exact for single days and
    # for the period tabs, which is where it matters most.
    cards_reviewed = sum(d.cards_reviewed for d in all_past)
    rt    = sum(d.review_time_seconds for d in all_past) / 60.0
    # start/end read directly from the days list the caller already sliced
    # to this week/month — not a new date computation, just the boundary of
    # data we already have. Powers the browse-click feature the same way
    # tab_data's start/end (from HeatmapStats, above) does.
    start_iso = min(d.day for d in all_past).isoformat()
    end_iso   = max(d.day for d in all_past).isoformat()
    return _j.dumps({
        "state":    "session" if past else "no_session",
        "ctx":      ctx_label,
        "eff":      _fmt_mins(eff),
        "q_raw":    round(q, 4),
        "new":      new,
        "cards":    cards,
        "cards_reviewed": cards_reviewed,
        "start":    start_iso,
        "end":      end_iso,
        "revtime":  _fmt_mins(rt),
        "sessions": len(past),
        "due":      0, "load": "",
    })


def _near_future_count_labels(
        days: list[HeatmapDay],
        positions: dict,
        cell: int,
        today) -> str:
    """Tiny count label centred inside each of the next 3 future cells (cell>=12 only)."""
    # Inline cell labels removed — numbers in small cells were too cramped.
    return ""


def _layout_monthly(days: list[HeatmapDay], cell: int, lo: tuple, hi: tuple,
                    heavy_cards: int, light_cards: int,
                    display: dict | None = None
                    ) -> tuple[str, int, int, dict[date, tuple[int, int]]]:
    """Month-separated blocks. Returns (svg_inner, width, height, positions)."""
    _dsp = display or {}
    _show_goal_dot     = False  # removed per user request; dot never renders
    _show_streak_lines = _dsp.get("show_streak_lines", True)
    _streak_color_raw = str(_dsp.get("streak_line_color", "") or "")
    _streak_line_color = _streak_color_raw if _re.match(r"^#[0-9a-fA-F]{3,8}$", _streak_color_raw) else ""
    _streak_line_thickness = {"thin": 0.6, "normal": 1.0, "thick": 1.7}.get(
        _dsp.get("streak_line_thickness", "normal"), 1.0)
    _future_color_raw = str(_dsp.get("future_cell_color", "") or "")
    _future_rgb = _hex_to_rgb(_future_color_raw) if _re.match(r"^#[0-9a-fA-F]{3,8}$", _future_color_raw) else (82, 132, 200)
    _show_month_names  = _dsp.get("show_month_names",  True)
    _show_week_nums    = _dsp.get("show_week_nums",    True)
    gap        = _GAP
    step       = cell + gap
    month_gap  = max(8, cell)
    dow_w      = 16
    x0         = dow_w + 6
    # Reserve space above the grid when month names are shown at the top
    y0         = 16 if _show_month_names else 4
    label_h    = 14
    grid_h     = 7 * step - gap
    today      = date.today()
    max_eff    = max((d.effective_minutes  for d in days if not d.is_future), default=0.0)
    max_due    = max((d.due_cards          for d in days if d.is_future),     default=1)
    max_time   = max((d.review_time_seconds for d in days if not d.is_future), default=1.0)

    month_map: dict[tuple, list[HeatmapDay]] = {}
    for d in days:
        month_map.setdefault((d.day.year, d.day.month), []).append(d)

    rects: list[str] = []
    labels: list[str] = []
    positions: dict[date, tuple[int, int]] = {}
    month_start_x:  dict[tuple, int]        = {}   # mk → x at start of block
    month_week_cols: dict[tuple, dict[int, list]] = {}  # mk → col → [HeatmapDay]

    # Group ALL days by ISO week so cross-month week labels aggregate every
    # day in that calendar week — not just the days in one month block.
    iso_week_all: dict[int, list] = {}
    for _d in days:
        _iw = _d.day.isocalendar()[1]
        iso_week_all.setdefault(_iw, []).append(_d)

    x_cursor = x0

    for mk in sorted(month_map.keys()):
        month_start_x[mk] = x_cursor
        mdays    = month_map[mk]
        first_wd = date(mk[0], mk[1], 1).weekday()
        last_col = 0
        for d in mdays:
            idx  = first_wd + d.day.day - 1
            col  = idx // 7
            row  = idx % 7
            x    = x_cursor + col * step
            y    = y0 + row * step
            last_col = max(last_col, col)
            positions[d.day] = (x, y)
            month_week_cols.setdefault(mk, {}).setdefault(col, []).append(d)
            fill   = _past_fill(d, max_eff, lo, hi) if not d.is_future else _future_fill(d, max_due, _future_rgb)
            fq     = "" if d.is_future else _quality_fill(d)
            ft     = "" if d.is_future else _time_fill(d, max_time, lo, hi)
            detail = _day_json(d, heavy_cards, light_cards, _dsp.get("_day_notes", {}))
            rects.append(_cell_rect(d, x, y, cell, fill, detail, d.day == today, fq, ft, _show_goal_dot, d.day.isoformat() in _dsp.get("_day_notes", {}), _dsp.get("cell_shape", "soft"), _dsp.get("note_marker_shape", "triangle"), _dsp.get("note_marker_position", "bottom_right"), _dsp.get("note_marker_color", "")))
        mw  = (last_col + 1) * step - gap
        cx  = x_cursor + mw // 2
        lbl = _fmt_month(date(mk[0], mk[1], 1))
        fw  = "font-weight='600'" if mk[1] == 1 else ""
        mo_ctx  = _fmt_month_year(date(mk[0], mk[1], 1))
        mo_json = _period_json(mdays, mo_ctx).replace('"', "&quot;")
        if _show_month_names:
            labels.append(
                f'<text x="{cx}" y="{y0 - 3}" font-size="9" opacity="0.8" '
                f'fill="var(--cal-label-color,var(--text-subtle,#aaa))" text-anchor="middle" '
                f'class="ff-month" style="cursor:pointer" '
                f'{fw} data-month="{mk[0]}-{mk[1]:02d}" data-json="{mo_json}">{lbl}</text>'
            )
        x_cursor += mw + month_gap

    # Week number labels — same style as year/weekly views.
    # In monthly mode each month block has week-within-month columns;
    # we label each column with its ISO week number.
    week_labels: list[str] = []
    if _show_week_nums:
        wy = y0 + grid_h + 12   # always just below the cell grid
        for mk, wcols in month_week_cols.items():
            mx = month_start_x[mk]
            for col, col_days in sorted(wcols.items()):
                wx    = mx + col * step
                iso_w = col_days[0].day.isocalendar()[1]
                w_ctx = f"Week {iso_w}"
                # Use ALL days for this ISO week (across months) so clicking
                # a week number that spans two months shows complete data.
                all_wdays = iso_week_all.get(iso_w, col_days)
                wj    = _period_json(all_wdays, w_ctx).replace('"', "&quot;")
                week_labels.append(
                    f'<text x="{wx + cell // 2}" y="{wy}" font-size="8" '
                    f'fill="var(--cal-label-color,var(--text-subtle,#aaa))" text-anchor="middle" '
                    f'class="ff-week" style="cursor:pointer"'
                    f' data-week="{col_days[0].day.isocalendar()[0]}-{iso_w:02d}"'
                    f' data-json="{wj}">{iso_w}</text>'
                )

    dow_svg = "".join(
        f'<text x="0" y="{y0 + i * step + cell // 2 + 3}" font-size="10" '
        f'fill="var(--cal-label-color,var(--text-subtle,#aaa))" dominant-baseline="middle">{lbl}</text>'
        for i, lbl in enumerate(_DOW)
    )
    w = x_cursor - month_gap + 12
    # Height: y0 already carries the month label overhead.
    # Only add bottom padding for week numbers when shown.
    _extra = 6 + (label_h if _show_week_nums else 0)
    h = y0 + grid_h + _extra
    streak_svg  = _streak_lines(days, positions, cell, _show_streak_lines, _streak_line_color, _streak_line_thickness)
    near_labels  = _near_future_count_labels(days, positions, cell, today)
    return (dow_svg + "".join(rects) + near_labels + streak_svg
            + "".join(labels) + "".join(week_labels)), w, h, positions


def _layout_year(days: list[HeatmapDay], cell: int, lo: tuple, hi: tuple,
                 heavy_cards: int, light_cards: int,
                 display: dict | None = None
                 ) -> tuple[str, int, int, dict[date, tuple[int, int]]]:
    """Continuous GitHub-style layout."""
    _dsp = display or {}
    _show_goal_dot     = False  # removed per user request; dot never renders
    _show_streak_lines = _dsp.get("show_streak_lines", True)
    _streak_color_raw = str(_dsp.get("streak_line_color", "") or "")
    _streak_line_color = _streak_color_raw if _re.match(r"^#[0-9a-fA-F]{3,8}$", _streak_color_raw) else ""
    _streak_line_thickness = {"thin": 0.6, "normal": 1.0, "thick": 1.7}.get(
        _dsp.get("streak_line_thickness", "normal"), 1.0)
    _future_color_raw = str(_dsp.get("future_cell_color", "") or "")
    _future_rgb = _hex_to_rgb(_future_color_raw) if _re.match(r"^#[0-9a-fA-F]{3,8}$", _future_color_raw) else (82, 132, 200)
    _show_week_nums    = _dsp.get("show_week_nums",    True)
    _show_month_names  = _dsp.get("show_month_names",  True)
    gap     = _GAP
    step    = cell + gap
    dow_w   = 16
    x0      = dow_w + 6
    y0      = 16
    grid_h  = 7 * step - gap
    today   = date.today()
    max_eff  = max((d.effective_minutes   for d in days if not d.is_future), default=0.0)
    max_due  = max((d.due_cards           for d in days if d.is_future),     default=1)
    max_time = max((d.review_time_seconds for d in days if not d.is_future), default=1.0)

    if not days:
        return "", 0, 0, {}

    first     = days[0].day
    first_mon = first - timedelta(days=first.weekday())

    rects:  list[str] = []
    labels: list[str] = []
    positions: dict[date, tuple[int, int]] = {}
    prev_month = None
    sep_drawn  = False

    # Group days by ISO week for week-number labels
    week_days: dict[int, list] = {}   # col → [HeatmapDay]
    week_col_x: dict[int, int] = {}   # col → x

    for d in days:
        offset = (d.day - first_mon).days
        col    = offset // 7
        row    = d.day.weekday()
        x      = x0 + col * step
        y      = y0 + row * step
        positions[d.day] = (x, y)
        week_days.setdefault(col, []).append(d)
        week_col_x[col] = x

        if d.day.month != prev_month:
            prev_month = d.day.month
            mo_lbl = f"{_fmt_month(d.day)} '{d.day.year % 100:02d}" if d.day.month == 1 else _fmt_month(d.day)
            month_ds = [dd for dd in days
                        if dd.day.year == d.day.year and dd.day.month == d.day.month]
            mo_ctx   = _fmt_month_year(d.day)
            mo_json  = _period_json(month_ds, mo_ctx).replace('"', "&quot;")
            if _show_month_names:
                labels.append(
                    f'<text x="{x}" y="{y0 - 3}" font-size="9" opacity="0.8" '
                    f'fill="var(--cal-label-color,var(--text-subtle,#aaa))" class="ff-month" '
                    f'style="cursor:pointer" data-month="{d.day.year}-{d.day.month:02d}" '
                    f'data-json="{mo_json}">{mo_lbl}</text>'
                )
        if d.is_future and not sep_drawn:
            sep_drawn = True
            sx = x - gap // 2 - 1
            labels.append(
                f'<line x1="{sx}" y1="{y0}" x2="{sx}" y2="{y0 + grid_h}" '
                f'stroke="var(--text-subtle,rgba(100,100,100,0.55))" stroke-width="1.5" '
                f'stroke-dasharray="3,2" pointer-events="none"/>'
            )
        fill   = _past_fill(d, max_eff, lo, hi) if not d.is_future else _future_fill(d, max_due, _future_rgb)
        fq     = "" if d.is_future else _quality_fill(d)
        ft     = "" if d.is_future else _time_fill(d, max_time, lo, hi)
        detail = _day_json(d, heavy_cards, light_cards, _dsp.get("_day_notes", {}))
        rects.append(_cell_rect(d, x, y, cell, fill, detail, d.day == today, fq, ft, _show_goal_dot, d.day.isoformat() in _dsp.get("_day_notes", {}), _dsp.get("cell_shape", "soft"), _dsp.get("note_marker_shape", "triangle"), _dsp.get("note_marker_position", "bottom_right"), _dsp.get("note_marker_color", "")))

    # Week number row below the grid (conditional on display setting)
    week_labels: list[str] = []
    if _show_week_nums:
        wy = y0 + grid_h + 12
        for col, wdays in sorted(week_days.items()):
            wx    = week_col_x[col]
            iso_w = wdays[0].day.isocalendar()[1]
            w_ctx = f"Week {iso_w}"
            wj    = _period_json(wdays, w_ctx).replace('"', "&quot;")
            week_labels.append(
                f'<text x="{wx + cell//2}" y="{wy}" font-size="8" '
                f'fill="var(--cal-label-color,var(--text-subtle,#aaa))" text-anchor="middle" '
                f'class="ff-week" style="cursor:pointer" '
                f'data-week="{wdays[0].day.isocalendar()[0]}-{iso_w:02d}" '
                f'data-json="{wj}">{iso_w}</text>'
            )

    total_cols = ((days[-1].day - first_mon).days) // 7 + 1
    w = x0 + total_cols * step + 8
    h = y0 + grid_h + 20   # extra height for week number row

    dow_svg = "".join(
        f'<text x="0" y="{y0 + i * step + cell // 2 + 3}" font-size="10" '
        f'fill="var(--cal-label-color,var(--text-subtle,#aaa))" dominant-baseline="middle">{lbl}</text>'
        for i, lbl in enumerate(_DOW)
    )
    streak_svg  = _streak_lines(days, positions, cell, _show_streak_lines, _streak_line_color, _streak_line_thickness)
    near_labels = _near_future_count_labels(days, positions, cell, today)
    return (dow_svg + "".join(labels) + "".join(rects)
            + near_labels + streak_svg + "".join(week_labels)), w, h, positions


def _layout_weekly(days: list[HeatmapDay], cell: int, lo: tuple, hi: tuple,
                   heavy_cards: int, light_cards: int,
                   display: dict | None = None
                   ) -> tuple[str, int, int, dict[date, tuple[int, int]]]:
    """Weekly columns with small gaps between weeks."""
    _dsp = display or {}
    _show_goal_dot     = False  # removed per user request; dot never renders
    _show_streak_lines = _dsp.get("show_streak_lines", True)
    _streak_color_raw = str(_dsp.get("streak_line_color", "") or "")
    _streak_line_color = _streak_color_raw if _re.match(r"^#[0-9a-fA-F]{3,8}$", _streak_color_raw) else ""
    _streak_line_thickness = {"thin": 0.6, "normal": 1.0, "thick": 1.7}.get(
        _dsp.get("streak_line_thickness", "normal"), 1.0)
    _future_color_raw = str(_dsp.get("future_cell_color", "") or "")
    _future_rgb = _hex_to_rgb(_future_color_raw) if _re.match(r"^#[0-9a-fA-F]{3,8}$", _future_color_raw) else (82, 132, 200)
    _show_week_nums    = _dsp.get("show_week_nums",    True)
    _show_month_names  = _dsp.get("show_month_names",  True)
    gap       = _GAP
    week_gap  = 4
    step      = cell + gap
    dow_w     = 16
    x0        = dow_w + 6
    y0        = 16
    grid_h    = 7 * step - gap
    today     = date.today()
    max_eff   = max((d.effective_minutes   for d in days if not d.is_future), default=0.0)
    max_due   = max((d.due_cards           for d in days if d.is_future),     default=1)
    max_time  = max((d.review_time_seconds for d in days if not d.is_future), default=1.0)

    if not days:
        return "", 0, 0, {}

    first     = days[0].day
    first_mon = first - timedelta(days=first.weekday())

    rects:    list[str] = []
    labels:   list[str] = []
    positions: dict[date, tuple[int, int]] = {}
    week_x:   dict[int, int] = {}
    week_days_map: dict[int, list] = {}   # week_num → [HeatmapDay]
    x_cursor  = x0
    sep_drawn = False
    prev_month = None

    for d in days:
        offset   = (d.day - first_mon).days
        week_num = offset // 7
        row      = d.day.weekday()

        if week_num not in week_x:
            week_x[week_num] = x_cursor
            x_cursor += step + week_gap
        week_days_map.setdefault(week_num, []).append(d)

        x = week_x[week_num]
        y = y0 + row * step
        positions[d.day] = (x, y)

        if d.day.month != prev_month and row == 0:
            prev_month = d.day.month
            mo_lbl = f"{_fmt_month(d.day)} '{d.day.year % 100:02d}" if d.day.month == 1 else _fmt_month(d.day)
            month_ds = [dd for dd in days
                        if dd.day.year == d.day.year and dd.day.month == d.day.month]
            mo_ctx  = _fmt_month_year(d.day)
            mo_json = _period_json(month_ds, mo_ctx).replace('"', "&quot;")
            if _show_month_names:
                labels.append(
                    f'<text x="{x}" y="{y0 - 3}" font-size="9" opacity="0.8" '
                    f'fill="var(--cal-label-color,var(--text-subtle,#aaa))" class="ff-month" '
                    f'style="cursor:pointer" data-month="{d.day.year}-{d.day.month:02d}" '
                    f'data-json="{mo_json}">{mo_lbl}</text>'
                )
        if d.is_future and not sep_drawn:
            sep_drawn = True
            sx = x - week_gap // 2 - 1
            labels.append(
                f'<line x1="{sx}" y1="{y0}" x2="{sx}" y2="{y0 + grid_h}" '
                f'stroke="var(--text-subtle,rgba(100,100,100,0.55))" stroke-width="1.5" '
                f'stroke-dasharray="3,2" pointer-events="none"/>'
            )
        fill   = _past_fill(d, max_eff, lo, hi) if not d.is_future else _future_fill(d, max_due, _future_rgb)
        fq     = "" if d.is_future else _quality_fill(d)
        ft     = "" if d.is_future else _time_fill(d, max_time, lo, hi)
        detail = _day_json(d, heavy_cards, light_cards, _dsp.get("_day_notes", {}))
        rects.append(_cell_rect(d, x, y, cell, fill, detail, d.day == today, fq, ft, _show_goal_dot, d.day.isoformat() in _dsp.get("_day_notes", {}), _dsp.get("cell_shape", "soft"), _dsp.get("note_marker_shape", "triangle"), _dsp.get("note_marker_position", "bottom_right"), _dsp.get("note_marker_color", "")))

    # Week number row below the grid (conditional on display setting)
    week_labels: list[str] = []
    if _show_week_nums:
        wy = y0 + grid_h + 12
        for wn, wdays in sorted(week_days_map.items()):
            wx    = week_x[wn]
            iso_w = wdays[0].day.isocalendar()[1]
            w_ctx = f"Week {iso_w}"
            wj    = _period_json(wdays, w_ctx).replace('"', "&quot;")
            week_labels.append(
                f'<text x="{wx + cell//2}" y="{wy}" font-size="8" '
                f'fill="var(--cal-label-color,var(--text-subtle,#aaa))" text-anchor="middle" '
                f'class="ff-week" style="cursor:pointer" '
                f'data-week="{wdays[0].day.isocalendar()[0]}-{iso_w:02d}" '
                f'data-json="{wj}">{iso_w}</text>'
            )

    dow_svg = "".join(
        f'<text x="0" y="{y0 + i * step + cell // 2 + 3}" font-size="10" '
        f'fill="var(--cal-label-color,var(--text-subtle,#aaa))" dominant-baseline="middle">{lbl}</text>'
        for i, lbl in enumerate(_DOW)
    )
    w = x_cursor - week_gap + 10
    # Height: reserve space for week numbers and/or month labels above.
    # y0=16 already reserves space above; only count bottom rows.
    h = y0 + grid_h + (22 if _show_week_nums else 4)
    streak_svg  = _streak_lines(days, positions, cell, _show_streak_lines, _streak_line_color, _streak_line_thickness)
    near_labels = _near_future_count_labels(days, positions, cell, today)
    return (dow_svg + "".join(labels) + "".join(rects)
            + near_labels + streak_svg + "".join(week_labels)), w, h, positions


def _render_year(days: list[HeatmapDay], cell: int, lo: tuple, hi: tuple,
                 heavy_cards: int, light_cards: int,
                 grouping: str,
                 display: dict | None = None) -> tuple[str, int, int]:
    """Dispatch to correct layout engine."""
    _dsp = display or {}
    if not _dsp.get("show_future_cells", True):
        # User opted to hide the forecast/future preview cells entirely —
        # same toggle pattern as every other heatmap-cells option below.
        days = [d for d in days if not d.is_future]
    if grouping == "year":
        inner, w, h, _ = _layout_year(days, cell, lo, hi, heavy_cards, light_cards, display)
    elif grouping == "weekly":
        inner, w, h, _ = _layout_weekly(days, cell, lo, hi, heavy_cards, light_cards, display)
    else:  # "monthly" is default
        inner, w, h, _ = _layout_monthly(days, cell, lo, hi, heavy_cards, light_cards, display)
    return inner, w, h


# ── shared CSS / JS / defs ────────────────────────────────────────────────────

_CSS = """<style>
#ff-heatmap{margin:8px auto 6px;text-align:center;
  font-family:var(--font-family,system-ui,sans-serif);
  font-size:var(--base-font-size,12px);color:var(--text-fg,#222);
  /* BUG FIX: these custom properties used to only be declared under
     .ff-dark (dark mode) — in light mode they were never declared at all,
     so every var(--x, fallback) silently depended on whether Anki's own
     page CSS happened to define a variable with the same name, rather than
     our own fallback. That's how the same light-mode render could come out
     with different label/text colours at different times — it was
     inheriting whatever (if anything) was in scope, not a fixed default.
     Declaring them explicitly here makes light mode deterministic, same
     as .ff-dark already is for dark mode. */
  --text-fg:      #222222;
  --text-subtle:  #3d3d3d;
  --canvas:       #fefefe;
  --border:       rgba(0,0,0,0.15);
  --button-bg:    rgba(40,40,40,0.08);
  --canvas-subtle:rgba(0,0,0,0.025);
}
#ff-nav{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:6px;transition:opacity .15s ease}
#ff-nav button{background:var(--canvas-subtle,rgba(0,0,0,0.05));
  border:1px solid var(--border,rgba(40,40,40,0.2));
  border-radius:4px;padding:2px 12px;cursor:pointer;
  font-size:12px;font-weight:600;color:var(--text-fg,#222);font-family:inherit}
#ff-nav button:hover:not(:disabled){background:var(--button-bg,rgba(40,40,40,0.08))}
#ff-nav button:disabled{opacity:0.6;cursor:default}
#ff-year-lbl{font-size:13px;font-weight:600;color:var(--cal-label-color,var(--text-subtle,#aaa));min-width:42px}
#ff-nav.ff-year-hover{opacity:0}
#ff-nav.ff-year-hover:hover{opacity:1}
#ff-nav.ff-year-hidden{display:none}
#ff-color-mode{font-size:11px;background:transparent;
  border:1px solid var(--border,rgba(40,40,40,0.2));
  border-radius:4px;padding:1px 5px;cursor:pointer;
  color:var(--text-fg,#222);font-family:inherit}
.ff-cell-wrap{transition:transform .12s ease;transform-box:fill-box;transform-origin:center}
.ff-cell-wrap:hover{transform:scale(1.12)}
.ff-cell-wrap:hover .ff-cell{filter:brightness(1.12) drop-shadow(0 1px 2px rgba(0,0,0,0.35))}
.ff-week{transition:opacity .1s;cursor:pointer}
.ff-week:hover{opacity:.5}
.ff-month{transition:opacity .1s;cursor:pointer}
.ff-month:hover{opacity:.5}
.ff-today-ring{animation:ff-today-pulse 2.4s ease-in-out infinite;
  filter:drop-shadow(0 0 3px var(--accent-glow, rgba(90,169,255,.85)))}
@keyframes ff-today-pulse{0%,100%{opacity:1}50%{opacity:0.45}}
#ff-detail{display:none;margin:8px auto 4px;max-width:700px;
  font-family:var(--font-family,system-ui,sans-serif);
  font-size:var(--base-font-size,12px)}
/* Cards use spacing and subtle background only — no heavy border box */
.ff-day-card{background:var(--canvas-subtle,rgba(0,0,0,0.025));
  border-radius:8px;padding:12px 16px;
  text-align:left;line-height:1.5;color:var(--text-fg,#222)}
.ff-lbl{display:inline-block;width:110px;color:var(--text-subtle,#888);font-size:11px}
.ff-val{font-weight:600}
.ff-tag{display:inline-block;font-size:11px;border-radius:99px;
  padding:2px 10px;margin-top:2px;margin-bottom:4px;
  font-weight:600;letter-spacing:.01em}
.ff-tag-good{background:rgba(52,130,68,.14);color:#2e7a40}
.ff-tag-warn{background:rgba(184,157,40,.14);color:#8a7200}
.ff-tag-poor{background:rgba(180,60,50,.12);color:#a03030}
.ff-tag-future{background:rgba(80,80,160,.12);color:#4040a0}
.ff-tag-empty{background:rgba(160,160,160,.13);color:#777}
#ff-popup{display:none;position:fixed;background:var(--canvas,#fefefe);border:1px solid var(--border,rgba(0,0,0,0.13));border-radius:10px;padding:10px 14px 12px;box-shadow:0 6px 22px rgba(0,0,0,0.14);min-width:196px;max-width:290px;z-index:9999;font-size:12px;color:var(--text-fg,#222)}#ff-popup-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:7px;padding-bottom:5px;border-bottom:1px solid var(--border,rgba(0,0,0,0.08))}#ff-popup-title{font-weight:600;font-size:11px}#ff-popup-close{background:none;border:none;cursor:pointer;font-size:14px;color:var(--text-subtle,#aaa);padding:0 1px;line-height:1}.ff-popup-row{display:flex;justify-content:space-between;align-items:baseline;padding:2px 0}.ff-popup-row-click{cursor:pointer;border-radius:4px;margin:0 -4px;padding:2px 4px}.ff-popup-row-click:hover{background:var(--border,rgba(0,0,0,0.06))}.ff-popup-row-click:hover .ff-popup-val{text-decoration:underline}.ff-popup-lbl{color:var(--text-subtle,#888);font-size:10px}.ff-popup-val{font-weight:600;font-size:12px}.ff-popup-note{display:flex;gap:5px;align-items:flex-start;margin-top:7px;padding-top:6px;border-top:1px solid var(--border,rgba(0,0,0,0.08));font-size:11px;color:var(--text-fg,#222)}.ff-popup-note-icon{flex-shrink:0}.ff-popup-note-text{white-space:pre-wrap;word-break:break-word}
#ff-legend{font-size:10px;color:var(--text-subtle,#bbb);
  letter-spacing:.02em;margin-top:4px;margin-bottom:2px}
#ff-streaks{font-size:11px;color:var(--text-subtle,#888);
  margin:4px auto 2px;text-align:center}
#ff-stats-wrap{margin:8px auto 4px;max-width:700px;
  font-family:var(--font-family,system-ui,sans-serif);
  font-size:var(--base-font-size,12px);color:var(--text-fg,#222)}
#ff-stats-wrap table{width:100%;border-collapse:collapse;text-align:left}
#ff-stats-wrap th{color:var(--text-subtle,#888);font-size:11px;
  font-weight:600;padding:3px 6px}
#ff-stats-wrap td{padding:3px 6px}
#ff-stats-wrap tr:hover td{background:var(--canvas-subtle,rgba(0,0,0,0.03))}
.ff-yr-row td{font-weight:600}
</style>"""

_CSS_DARK = """<style>
/* Dark-mode overrides — applied when Anki is in night mode.
   All colours reference the same var() tokens used in _CSS; we simply
   redefine those tokens on the .ff-dark parent so every child element
   picks up the correct dark values automatically. */
.ff-dark{
  --text-fg:     #dcdcdc;
  --text-subtle: #d8d8d8;
  --canvas:      rgba(44,44,46,0.97);
  --border:      rgba(255,255,255,0.14);
  --button-bg:   rgba(255,255,255,0.08);
  --canvas-subtle:rgba(255,255,255,0.04);
}
/* Tag pill colours — need brighter hues on a dark background */
.ff-dark .ff-tag-good  {background:rgba(52,130,68,.30);color:#72c97f}
.ff-dark .ff-tag-warn  {background:rgba(184,157,40,.28);color:#cdb240}
.ff-dark .ff-tag-poor  {background:rgba(180,60,50,.28);color:#e07272}
.ff-dark .ff-tag-future{background:rgba(80,80,160,.28);color:#8898e0}
.ff-dark .ff-tag-empty {background:rgba(160,160,160,.20);color:#aaaaaa}
/* Goal-reached dot — keep white dot visible on dark cells */
.ff-dark .ff-goal-dot  {background:rgba(255,255,255,0.90)!important}
/* Nav controls */
.ff-dark #ff-color-mode{background:rgba(255,255,255,0.06);color:#dcdcdc}
.ff-dark #ff-color-mode option{background:#2c2c2e;color:#dcdcdc}
</style>"""

_DEFS = """<defs>
  <pattern id="ff-hatch" patternUnits="userSpaceOnUse" width="5" height="5">
    <path d="M-1,1 l2,-2 M0,5 l5,-5 M4,6 l2,-2"
      stroke="rgba(100,125,175,0.22)" stroke-width="0.9"/>
  </pattern>
  <linearGradient id="ff-cell-sheen" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="#ffffff" stop-opacity="0.20"/>
    <stop offset="40%" stop-color="#ffffff" stop-opacity="0.05"/>
    <stop offset="100%" stop-color="#000000" stop-opacity="0.10"/>
  </linearGradient>
</defs>"""

_LEGEND = (
    '<div id="ff-legend-wrap" style="margin:4px 2px 0">'
    '<div id="ff-legend" style="font-size:10px;'
    'color:var(--text-subtle,#bbb);padding:5px 4px 2px;line-height:2">'
    '<span style="color:rgba(38,122,60,0.90)">&#9632;</span> high quality&nbsp;&nbsp;<span style="color:rgba(188,152,42,0.90)">&#9632;</span> low quality&nbsp;&nbsp;<span style="color:rgba(192,188,183,0.80);border:1px solid rgba(160,155,150,0.4)">&#9632;</span> no data&nbsp;&nbsp;<span style="color:rgba(82,132,200,0.70)">&#9632;</span> due (upcoming)&nbsp;&nbsp;<span style="display:inline-block;width:9px;height:9px;background:rgba(120,120,120,0.25);border-radius:2px;position:relative;vertical-align:middle;overflow:hidden"><span style="position:absolute;top:0;right:0;width:0;height:0;border-top:4px solid rgba(255,255,255,0.82);border-left:4px solid transparent"></span></span>&nbsp;goal reached'
    '</div></div>'
)
_DETAIL_PANEL = ""   # No dropdown — cell data updates the existing metric cards in place.

_JS_INTERACT = """<script>
(function(){
  // ── helpers ──────────────────────────────────────────────────────────────
  function _starPoints(cx, cy, rOuter, rInner){
    // Mirrors Python's _star_points() — 5-pointed star, tip pointing up.
    var pts = [];
    for(var i=0;i<10;i++){
      var ang = -Math.PI/2 + i*Math.PI/5;
      var r = (i % 2 === 0) ? rOuter : rInner;
      pts.push((cx + r*Math.cos(ang)).toFixed(2) + ',' + (cy + r*Math.sin(ang)).toFixed(2));
    }
    return pts.join(' ');
  }
  function _badge(q){
    var bg,fg;
    if(q>=0.75){bg="rgba(52,130,68,.14)";fg="#2e7a40";}
    else if(q>=0.50){bg="rgba(184,157,40,.14)";fg="#8a7200";}
    else{bg="rgba(160,160,160,.12)";fg="#6a6a6a";}
    return '<span style="display:inline-block;padding:1px 8px;border-radius:99px;'+
      'font-size:11px;font-weight:600;background:'+bg+';color:'+fg+'">'+
      Math.round(q*100)+'%</span>';
  }

  var _activeTab = "default";
  var sel = null;

  // The pulsing "today" ring is a single movable overlay rect — clicking a
  // day now slides this same shining ring onto that cell instead of a flat
  // static outline, then slides it back "home" to today on deselect.
  var _todayRingEl = document.getElementById('ff-today-ring');
  var _ringHome = _todayRingEl ? {
    x: _todayRingEl.getAttribute('x'), y: _todayRingEl.getAttribute('y'),
    w: _todayRingEl.getAttribute('width'), h: _todayRingEl.getAttribute('height'),
    stroke: _todayRingEl.getAttribute('stroke'),
    transform: _todayRingEl.getAttribute('transform'),
    points: _todayRingEl.getAttribute('points')
  } : null;

  // Update the 5 metric cards + context line from a JSON data object
  // Shared helper: format today's remaining due as "N review + M new" etc.
  window._buildDueStr = function(d){
    if(d && (d.rev_due > 0 || d.lrn_due > 0 || d.new_due > 0)){
      var parts = [];
      if(d.rev_due  > 0) parts.push(d.rev_due  + ' review');
      if(d.lrn_due  > 0) parts.push(d.lrn_due  + ' learning');
      if(d.new_due  > 0) parts.push(d.new_due  + ' new');
      return parts.join(' + ');
    }
    return (d ? d.due : 0) + ' cards';
  };

  // Generic registry of clickable metric-card stats. Each entry just needs
  // an element id and a "kind" (matching a key in __init__.py's
  // _BROWSE_TERMS registry, which maps kind -> Anki search term). To make a
  // new stat clickable later: add one entry here and one entry to
  // _BROWSE_TERMS — nothing else in this file, _applyData(), or ffTab()
  // needs to change, since all of them already drive whichever stats are
  // registered here through the same ffWireCardClick() call below.
  window._ffClickableStats = [
    {id: "ff-m-new",            kind: "new"},
    {id: "ff-m-cards",          kind: "reviewed"},
    {id: "ff-m-cards-reviewed", kind: "cards_reviewed"},
  ];

  // Wires (or clears) click-to-browse on every registered stat card above.
  // Shared between _applyData() (day/week/month cell clicks) and ffTab()
  // (Today/Week/Month/All-time tab clicks) below — both paths update the
  // same metric-card elements for whatever period is now current, so both
  // call this with that period's [startISO, endISO] (inclusive). A single
  // day is just startISO === endISO. null/null (a genuine "no well-defined
  // range" case, currently never emitted but supported defensively) clears
  // click-ability instead of wiring a broken link.
  window.ffWireCardClick = function(startISO, endISO){
    window._ffClickableStats.forEach(function(stat){
      var val = document.getElementById(stat.id);
      if(!val) return;
      var card = val.closest(".ff-mcard");
      var target = card || val;
      if(startISO && endISO){
        target.classList.add("ff-mcard-click");
        target.setAttribute("title", "Open in Browser");
        target.onclick = function(e){
          e.stopPropagation();
          window.ffBrowseStat(stat.kind, startISO, endISO);
        };
      } else {
        target.classList.remove("ff-mcard-click");
        target.removeAttribute("title");
        target.onclick = null;
      }
    });
  };

  function _applyData(d){
    var e = function(id){ return document.getElementById(id); };
    if(e('ff-m-eff'))     e('ff-m-eff').textContent     = d.eff     || "—";
    if(e('ff-m-q'))       e('ff-m-q').innerHTML         = d.q_raw   ? _badge(d.q_raw) : '<span style="color:var(--text-subtle,#888)">—</span>';
    if(e('ff-m-new'))     e('ff-m-new').textContent     = d.new     != null ? d.new    : "—";
    if(e('ff-m-cards'))   e('ff-m-cards').textContent   = d.cards   != null ? d.cards  : "—";
    if(e('ff-m-cards-reviewed')) e('ff-m-cards-reviewed').textContent = d.cards_reviewed != null ? d.cards_reviewed : "—";
    if(e('ff-m-revtime')) e('ff-m-revtime').textContent = d.revtime || "—";
    if(window.ffWireCardClick) window.ffWireCardClick(d.start || null, d.end || null);
    // Trend + due card: content-only updates. ZERO layout-property changes.
    // Hiding either element changes card height or width, causing the
    // size-jump bug. We only update textContent and color instead.
    var tEl = e('ff-m-trend');
    if(tEl){ tEl.textContent='\u2014'; tEl.style.color='var(--text-subtle,#888)'; }
    // Due count shown in the context line + streak row only (no card box)

    // Context line — date label + due count for future and today
    var ctx = e('ff-ctx');
    if(!ctx) return;
    if(d.state === "future"){
      var loadCol = d.load==="heavy"?"#8a7200":d.load==="moderate"?"#6a6a6a":"#2e7a40";
      if(d.due > 0){
        // BUG FIX: this text wasn't clickable at all — the only way to see
        // WHICH cards make up a future day's due count was the popup's
        // "Due" row (see the matching fix in _ffShowPopup above), so
        // anyone using the inline stats-card display (stats_popup off)
        // had no way to drill into a forecast day whatsoever. Same
        // ffBrowseStat("due", ...) plumbing, just wired here too.
        ctx.innerHTML = '<span style="font-weight:600">' + d.ctx + '</span>'
          + ' &nbsp;&middot;&nbsp; <span style="color:'+loadCol+';cursor:pointer;text-decoration:underline dotted" '
          + 'title="Open in Browser" onclick="window.ffBrowseStat(\\'due\\',\\''+d.start+'\\',\\''+d.end+'\\')">'
          + d.due + ' cards due</span>';
      } else {
        ctx.innerHTML = '<span style="font-weight:600">' + d.ctx + '</span>'
          + ' &nbsp;&middot;&nbsp; <span style="color:var(--text-subtle,#888)">no cards due</span>';
      }
    } else if(d.state === "no_data"){
      if(d.is_today && d.due > 0){
        var ds = window._buildDueStr(d);
        ctx.innerHTML = '<span style="color:var(--text-subtle,#888)">' + d.ctx + '</span>'
          + ' &nbsp;&middot;&nbsp; <span style="color:#8a7200;font-weight:600">'
          + ds + ' still due</span>';
      } else {
        ctx.innerHTML = '<span style="color:var(--text-subtle,#888)">' + d.ctx + ' &mdash; no activity</span>';
      }
    } else {
      var ctxHtml = '<span style="font-weight:600;color:var(--text-fg,#222)">' + d.ctx + '</span>';
      if(d.is_today && d.due > 0){
        var ds = window._buildDueStr(d);
        ctxHtml += ' &nbsp;&middot;&nbsp; <span style="color:#8a7200;font-weight:600">'
          + ds + ' still due</span>';
      }
      ctx.innerHTML = ctxHtml;
    }
    ctx.style.display = "";
    var _sep = document.getElementById('ff-ctx-sep');
    if(_sep) _sep.style.display = "";
  }

  function _restoreCtx(){
    if(window._ffHidePopup) window._ffHidePopup();
    var tEl = document.getElementById('ff-m-trend');
    if(tEl){
      tEl.textContent = tEl.getAttribute('data-orig') || tEl.textContent;
      tEl.style.color = tEl.getAttribute('data-orig-color') || '';
    }
    var ctx = document.getElementById('ff-ctx');
    if(ctx){ ctx.textContent = ""; ctx.style.display = "none"; }
    var _sep2 = document.getElementById('ff-ctx-sep');
    if(_sep2) _sep2.style.display = "none";
    // Re-trigger the current tab to repaint the metric values
    if(window.ffTab) window.ffTab(_activeTab);
  }

  // Hook into ffTab AFTER it is defined by the stats JS block.
  // setTimeout 0 defers until all synchronous script blocks have run.
  setTimeout(function(){
    var _origTab = window.ffTab;
    window.ffTab = function(key){
      _activeTab = key;
      if(sel){
        _deselStyle(sel);
        sel = null;
        var ctx = document.getElementById('ff-ctx');
        if(ctx){ ctx.textContent=""; ctx.style.display="none"; }
        var _sep3 = document.getElementById('ff-ctx-sep');
        if(_sep3) _sep3.style.display="none";
        var tEl = document.getElementById('ff-m-trend');
        if(tEl){
          tEl.textContent = tEl.getAttribute('data-orig') || tEl.textContent;
          tEl.style.color = tEl.getAttribute('data-orig-color') || '';
        }
      }
      if(_origTab) _origTab(key);
    };
  }, 0);

  // ── unified selection handler (day cells, week labels, month labels) ──────
  function _selectEl(el){
    if(sel && sel !== el){
      _deselStyle(sel);
    }
    if(sel === el){
      _deselStyle(el);
      sel = null;
      _restoreCtx();
      return;
    }
    sel = el;
    _selStyle(el);
    try{
      var d = JSON.parse(el.getAttribute('data-json') || '{}');
      if(window._ffStatsMode === 'popup'){
        if(window._ffShowPopup) window._ffShowPopup(d, _lastCX, _lastCY);
      } else {
        _applyData(d);
      }
      // Persist the clicked date so it can be restored on next Anki start
      // when "Default Statistics View" is set to "Remember Last Selection".
      // Harmless no-op server-side if that setting isn't active.
      if(el.classList.contains('ff-cell') && d.date && typeof pycmd !== 'undefined'){
        pycmd('ff_select_date:' + d.date);
      }
    } catch(err){ /* ignore */ }
  }

  function _selStyle(el){
    if(el.classList.contains('ff-cell')){
      // Slide the same pulsing "today" ring onto the clicked cell so the
      // selection uses that nice shining animated box, instead of a flat
      // static outline drawn separately on the cell itself.
      if(_todayRingEl){
        if(el.hasAttribute('data-star')){
          // BUG FIX: the star shape's cells (and its ring) are <polygon>
          // elements, which have no x/y/width/height for the generic path
          // below to read — without this branch, clicking a different star
          // cell would silently no-op every setAttribute call and the ring
          // would just stay put on today's star instead of moving.
          var scx = parseFloat(el.getAttribute('data-cx'));
          var scy = parseFloat(el.getAttribute('data-cy'));
          var sro = parseFloat(el.getAttribute('data-ro'));
          var sri = parseFloat(el.getAttribute('data-ri'));
          _todayRingEl.setAttribute('points', _starPoints(scx, scy, sro + 1.2, sri + 0.6));
          _todayRingEl.removeAttribute('transform');
        } else {
        var cx = parseFloat(el.getAttribute('x'));
        var cy = parseFloat(el.getAttribute('y'));
        var cw = parseFloat(el.getAttribute('width'));
        var ch = parseFloat(el.getAttribute('height'));
        _todayRingEl.setAttribute('x', (cx + 0.5).toFixed(1));
        _todayRingEl.setAttribute('y', (cy + 0.5).toFixed(1));
        _todayRingEl.setAttribute('width',  Math.max(0, cw - 1));
        _todayRingEl.setAttribute('height', Math.max(0, ch - 1));
        // BUG FIX: for diamond-shaped cells the target has its own
        // rotate(45 ...) transform around ITS OWN center — without copying
        // it here, the ring keeps whatever transform it last had (e.g.
        // today's rotation pivot), so it'd render rotated around the wrong
        // point entirely instead of framing the clicked cell.
        var _ct = el.getAttribute('transform');
        if(_ct){ _todayRingEl.setAttribute('transform', _ct); }
        else{ _todayRingEl.removeAttribute('transform'); }
        }
        // Today itself keeps its neutral colour; any other selected day
        // gets the same blue accent used everywhere else in the heatmap.
        var isToday = el.classList.contains('ff-today');
        _todayRingEl.setAttribute('stroke',
          isToday && _ringHome ? _ringHome.stroke : 'rgba(40,100,220,0.95)');
        // BUG FIX (kept from before): a running CSS @keyframes animation
        // overrides inline style.opacity on that same property every
        // frame, so make sure the animation is actually enabled (not
        // left disabled from a prior week/month selection) rather than
        // just touching opacity.
        _todayRingEl.style.animation = '';
        _todayRingEl.style.opacity   = '';
      } else {
        // Today isn't in the currently-viewed year, so there's no ring to
        // move — fall back to a plain static outline on the cell itself.
        el.setAttribute('stroke','rgba(40,100,220,0.95)');
        el.setAttribute('stroke-width','2.8');
      }
    } else {
      // Bold + underline + accent-blue fill for week/month text labels
      el.setAttribute('fill','rgba(40,100,220,0.95)');
      el.setAttribute('font-weight','800');
      el.setAttribute('text-decoration','underline');
      // Also highlight every cell belonging to that week/month, the same
      // way a day click highlights itself — previously only the small
      // label text changed colour, so the actual boxes never showed which
      // days were included in the selection.
      var attr = el.classList.contains('ff-week') ? 'data-week' : 'data-month';
      var key  = el.getAttribute(attr);
      if(key){
        var cells = document.querySelectorAll(
          '.ff-cell[' + attr + '="' + key + '"]');
        for(var i=0;i<cells.length;i++){
          cells[i].setAttribute('stroke','rgba(40,100,220,0.95)');
          cells[i].setAttribute('stroke-width','1.8');
        }
      }
      // A week/month selection covers many cells at once, which the single
      // ring can't represent — send it home and hide it instead. Same
      // animation-vs-inline-style fix as above (kill the animation itself,
      // not just opacity, or the pulse keeps winning the cascade fight).
      if(_todayRingEl){
        _todayRingEl.style.animation = 'none';
        _todayRingEl.style.opacity   = '0';
      }
    }
  }
  function _deselStyle(el){
    if(el.classList.contains('ff-cell')){
      // Slide the ring back home to today and restore its neutral colour.
      if(_todayRingEl && _ringHome){
        if(_ringHome.points){
          // Star shape: x/y/width/height are meaningless on a <polygon>,
          // restore its actual home shape instead.
          _todayRingEl.setAttribute('points', _ringHome.points);
          _todayRingEl.removeAttribute('transform');
        } else {
        _todayRingEl.setAttribute('x', _ringHome.x);
        _todayRingEl.setAttribute('y', _ringHome.y);
        _todayRingEl.setAttribute('width',  _ringHome.w);
        _todayRingEl.setAttribute('height', _ringHome.h);
        if(_ringHome.transform){ _todayRingEl.setAttribute('transform', _ringHome.transform); }
        else{ _todayRingEl.removeAttribute('transform'); }
        }
        _todayRingEl.setAttribute('stroke', _ringHome.stroke);
      } else {
        // Fallback-path cell (see _selStyle) — clear its static outline.
        el.setAttribute('stroke','rgba(120,114,107,0.32)');
        el.setAttribute('stroke-width','0.6');
      }
    } else {
      el.setAttribute('fill','var(--text-subtle,#aaa)');
      el.setAttribute('font-weight','normal');
      el.setAttribute('text-decoration','none');
      var attr = el.classList.contains('ff-week') ? 'data-week' : 'data-month';
      var key  = el.getAttribute(attr);
      if(key){
        var cells = document.querySelectorAll(
          '.ff-cell[' + attr + '="' + key + '"]');
        for(var i=0;i<cells.length;i++){
          cells[i].setAttribute('stroke','rgba(120,114,107,0.32)');
          cells[i].setAttribute('stroke-width','0.6');
        }
      }
    }
    // Always restore the ring's shine when any element is deselected
    if(_todayRingEl){
      _todayRingEl.style.animation = '';
      _todayRingEl.style.opacity   = '';
    }
  }

  var _lastCX = 0, _lastCY = 0;
  document.addEventListener('click', function(e){
    _lastCX = e.clientX; _lastCY = e.clientY;
    var el = e.target;
    if(el.classList && (
        el.classList.contains('ff-cell') ||
        el.classList.contains('ff-week') ||
        el.classList.contains('ff-month'))){
      _selectEl(el);
      return;
    }
    // Click outside heatmap — deselect and close popup
    if(window._ffHidePopup) window._ffHidePopup();
    if(sel && !el.closest('#ff-heatmap')){
      _deselStyle(sel);
      sel = null;
      _restoreCtx();
    }
  });

  // Right-click a day cell: opens a proper native dialog in Python (not a
  // browser prompt() — cleaner, and lets us offer a "remind me: same day /
  // day before / week before / month before" choice that a plain text
  // prompt can't). Python already has the day's existing note/offset in
  // config, so we only need to send the date — no need to round-trip the
  // current text through JS at all.
  document.addEventListener('contextmenu', function(e){
    var el = e.target;
    if(!(el.classList && el.classList.contains('ff-cell'))) return;
    e.preventDefault();
    var d;
    try{ d = JSON.parse(el.getAttribute('data-json') || '{}'); }
    catch(err){ return; }
    if(!d.date) return;
    if(typeof pycmd === 'undefined') return;
    pycmd('ff_edit_note:' + d.date);
  });

  // BUG FIX: the popup's auto-close timer (a separate <script> block) needs
  // to clear the selected-cell highlight when it fires, but sel/_deselStyle
  // are private to this closure. Expose a small global hook instead of
  // reaching into these locals from outside — without this, the timer was
  // silently a no-op (typeof-guarded so it didn't error, it just never ran)
  // and the highlighted cell stayed highlighted after the popup closed.
  window._ffDeselectCell = function(){
    if(sel){ _deselStyle(sel); sel = null; }
  };
})();
</script>"""


# ── stats table helpers ───────────────────────────────────────────────────────

def _build_stats_html(
    period_stats: list[HeatmapStats],
    year_stats_by_year: dict[int, dict],
    current_year: int,
    current_streak: int,
    longest_streak: int,
    today_day: HeatmapDay | None = None,
    future_days: list[HeatmapDay] | None = None,
    due_today_override: int | None = None,
    display: dict | None = None,
    default_view: str = "today",
    remembered_day: HeatmapDay | None = None,
    night_mode: bool = False,
) -> dict[str, str]:
    """Redesigned stats panel — tabbed metric cards + focused session detail.

    Layout (top → bottom):
      1. Period tabs: Today / This week / Month / All time
      2. Four metric cards (Effective time, Quality, New cards, Cards)
         that update live when the user switches tabs.
      3. Streak line.
      4. Session detail card for today with a two-column layout and an
         attention strip when the again rate is notably above the weekly avg.

    `default_view` / `remembered_day` control what the 5 metric cards show
    before the user has clicked anything this session — the "Default
    Statistics View" setting (Today / Last 7 Days / Last 30 Days / Remember
    Last Selection).
    """
    week_stat    = next((s for s in period_stats if s.label == "Weekly"),  None)
    today_stat   = next((s for s in period_stats if s.label == "Daily"),   None)
    month_stat   = next((s for s in period_stats if s.label == "Monthly"), None)
    last7_stat   = next((s for s in period_stats if s.label == "Last7"),   None)
    last30_stat  = next((s for s in period_stats if s.label == "Last30"),  None)
    alltime_stat = next((s for s in period_stats if s.label == "All-time"), None)

    def _eff(s):   return s.effective_minutes if s else 0.0
    def _q(s):     return s.quality           if s else 0.0
    def _new(s):   return s.new_cards         if s else 0
    # s.reviews_count on a HeatmapStats object is the RAW (undeduplicated)
    # types 0+1+2+3 total — Learning+Review+Relearning+Filtered combined via
    # raw_study_events() inside _nc_rc_rt() in heatmap_service.py, matching
    # Anki's own LIVE studied_today() semantics (a plain COUNT(), no per-card
    # dedup). This is the same canonical figure the day-popup (_day_json),
    # the week/month-label popup, _day_as_stat, and HeatmapDialog's stats
    # table all use — read directly, with no further addition here. Adding
    # s.new_cards on top (as this used to) would double-count new cards: once
    # on their own "New cards" card, and again inside this already-combined
    # total. (For the OLD activity_total()-based figure — distinct new cards
    # instead of raw type=0 events — see s.activity_total instead.)
    def _cards(s): return s.reviews_count if s else 0
    def _dc(s):    return s.cards_reviewed    if s else 0
    def _range(s):
        """(start_iso, end_iso) for a period's browse-click, or (None, None)
        for a period with no well-defined date range attached."""
        if s is None or s.start_date is None or s.end_date is None:
            return None, None
        return s.start_date.isoformat(), s.end_date.isoformat()

    def _day_as_stat(d: HeatmapDay | None) -> HeatmapStats | None:
        """Adapt a single HeatmapDay (from a remembered click) into the same
        shape as a HeatmapStats period so the card-rendering code below
        doesn't need a separate code path for it."""
        if d is None:
            return None
        return HeatmapStats(
            label="Remembered", minutes=d.minutes, effective_minutes=d.effective_minutes,
            quality=d.quality, productivity=0.0, sessions=d.sessions,
            new_cards=d.new_cards,
            # Reviews total = RAW types 0+1+2+3 (Learning+Review+Relearning+
            # Filtered), matching Anki's own LIVE studied_today() semantics
            # via raw_study_events() — the same canonical figure every other
            # tab stat uses, so "Remember last selection" shows the same
            # number Today/Week/Month/etc would for that day.
            reviews_count=raw_study_events(d),
            review_time_minutes=d.review_time_seconds / 60.0,
            cards_reviewed=d.cards_reviewed,
            start_date=d.day, end_date=d.day,
        )

    # Resolve which stat drives the 5 metric cards' initial (pre-click) values.
    if default_view == "7days":
        default_stat = last7_stat or today_stat
    elif default_view == "30days":
        default_stat = last30_stat or today_stat
    elif default_view == "remember" and remembered_day is not None:
        default_stat = _day_as_stat(remembered_day)
    else:
        default_stat = today_stat

    # Quality trend vs all-time average
    delta_q   = _q(week_stat) - _q(alltime_stat)
    trend_lbl = "\u2191 improving" if delta_q >  0.04 else "\u2193 declining" if delta_q < -0.04 else "\u2192 stable"
    trend_col = "#2e7a40"           if delta_q >  0.04 else "#8a7200"           if delta_q < -0.04 else "var(--text-subtle,#888)"
    trend_tip = f"This week: {_q(week_stat):.0%} vs all-time: {_q(alltime_stat):.0%}"

    # Again-rate attention signal — use today_day (HeatmapDay, same source as
    # the detail card) so the Today card and the day-detail card always agree.
    td              = today_day  # shorthand
    today_again     = td.again_count if td else 0
    today_total     = activity_total(td) if td else 0
    today_again_rt  = today_again / today_total if today_total > 0 else 0.0
    week_again      = getattr(week_stat, "again_count", 0) or 0
    # week_stat.activity_total holds the OLD activity_total()-based figure,
    # kept separate from week_stat.reviews_count (which now holds the raw,
    # Anki-matching Reviews total used for display). Using .activity_total
    # here keeps this denominator on the exact same scope as today_total
    # above (also activity_total()-based), so today_again_rt and
    # week_again_rt remain directly comparable -- unaffected by the Reviews
    # display fix.
    week_total      = week_stat.activity_total if week_stat else 0
    week_again_rt   = week_again / week_total if week_total > 0 else 0.0
    show_attention  = today_again > 0 and (today_again_rt - week_again_rt) > 0.05

    # ── helpers ───────────────────────────────────────────────────────────────
    def _q_badge(q: float) -> str:
        if q >= 0.75:
            bg, fg = "rgba(52,130,68,.14)", "#2e7a40"
        elif q >= 0.50:
            bg, fg = "rgba(184,157,40,.14)", "#8a7200"
        else:
            bg, fg = "rgba(180,60,50,.12)", "#a03030"
        return (
            f'<span style="display:inline-block;padding:1px 8px;border-radius:99px;'
            f'font-size:11px;font-weight:600;background:{bg};color:{fg}">'
            f'{q:.0%}</span>'
        )

    def _streak_icon(days: int) -> tuple[str, str]:
        """Return (inline_svg, tooltip) for a streak length.

        8-stage seed → tree growth progression (based on longest_streak) —
        hand-drawn flat-vector SVGs in one consistent style/palette across
        all 8 stages, so it reads as a single thing growing over time
        rather than a grab-bag of unrelated symbols. (Not sourced from any
        third-party icon site — original inline artwork, so there's nothing
        to license and it renders identically on every platform, unlike
        emoji which look different on Windows/Mac/Linux.)

          1   d  seed planted
          3   d  sprouting
          7   d  seedling
          14  d  young plant
          30  d  small tree
          60  d  tree
          100 d  fruit tree
          365 d  ancient tree — legendary (gold accents)
        """
        _SOIL   = '<ellipse cx="12" cy="20" rx="7" ry="2.2" fill="#8B5A2B"/>'
        _SOIL_S = '<ellipse cx="12" cy="20" rx="4.5" ry="1.6" fill="#8B5A2B"/>'

        def _svg(inner: str) -> str:
            return (f'<svg viewBox="0 0 24 24" width="16" height="16" '
                    f'style="vertical-align:-3px">{inner}</svg>')

        if days >= 365:
            body = (
                _SOIL +
                '<path d="M12 20 L12 8" stroke="#5D4037" stroke-width="2.4" stroke-linecap="round"/>'
                '<circle cx="8.5" cy="9" r="4.6" fill="#2E7D32"/>'
                '<circle cx="15.5" cy="9" r="4.6" fill="#388E3C"/>'
                '<circle cx="12" cy="5.5" r="5" fill="#43A047"/>'
                '<path d="M4 6l.8 1.6L6.5 8l-1.7.7L4 10.3l-.8-1.6L1.5 8l1.7-.7z" fill="#FFD54F"/>'
                '<path d="M20 4l.6 1.2 1.3.6-1.3.6-.6 1.2-.6-1.2L18 5.8l1.3-.6z" fill="#FFD54F"/>'
            )
            return _svg(body), "Ancient tree — legendary (365+ days)"
        if days >= 100:
            body = (
                _SOIL +
                '<path d="M12 20 L12 9" stroke="#6B4423" stroke-width="2.2" stroke-linecap="round"/>'
                '<circle cx="9" cy="9.5" r="4.3" fill="#388E3C"/>'
                '<circle cx="15" cy="9.5" r="4.3" fill="#43A047"/>'
                '<circle cx="12" cy="6.5" r="4.4" fill="#4CAF50"/>'
                '<circle cx="8.5" cy="10" r="1.1" fill="#E53935"/>'
                '<circle cx="15" cy="11.5" r="1.1" fill="#FB8C00"/>'
                '<circle cx="12.5" cy="8" r="1.1" fill="#E53935"/>'
            )
            return _svg(body), "Fruit tree — 100+ days"
        if days >= 60:
            body = (
                _SOIL +
                '<path d="M12 20 L12 10" stroke="#6B4423" stroke-width="2" stroke-linecap="round"/>'
                '<circle cx="9.5" cy="10" r="3.8" fill="#4CAF50"/>'
                '<circle cx="14.5" cy="10" r="3.8" fill="#4CAF50"/>'
                '<circle cx="12" cy="7.3" r="3.9" fill="#66BB6A"/>'
            )
            return _svg(body), "Tree — 60+ days"
        if days >= 30:
            body = (
                _SOIL +
                '<path d="M12 20 L12 12" stroke="#795548" stroke-width="1.8" stroke-linecap="round"/>'
                '<circle cx="12" cy="9.5" r="4.2" fill="#66BB6A"/>'
            )
            return _svg(body), "Small tree — 30+ days"
        if days >= 14:
            body = (
                _SOIL_S +
                '<path d="M12 20 L12 10" stroke="#4CAF50" stroke-width="1.6" stroke-linecap="round"/>'
                '<ellipse cx="8.7" cy="12" rx="2.6" ry="1.4" fill="#7CB342" transform="rotate(-25 8.7 12)"/>'
                '<ellipse cx="15.3" cy="12" rx="2.6" ry="1.4" fill="#7CB342" transform="rotate(25 15.3 12)"/>'
                '<ellipse cx="9" cy="8.3" rx="2.4" ry="1.3" fill="#8BC34A" transform="rotate(-20 9 8.3)"/>'
                '<ellipse cx="15" cy="8.3" rx="2.4" ry="1.3" fill="#8BC34A" transform="rotate(20 15 8.3)"/>'
            )
            return _svg(body), "Young plant — 14+ days"
        if days >= 7:
            body = (
                _SOIL_S +
                '<path d="M12 20 L12 11.5" stroke="#4CAF50" stroke-width="1.5" stroke-linecap="round"/>'
                '<ellipse cx="9.3" cy="13" rx="2.2" ry="1.2" fill="#8BC34A" transform="rotate(-25 9.3 13)"/>'
                '<ellipse cx="14.7" cy="13" rx="2.2" ry="1.2" fill="#8BC34A" transform="rotate(25 14.7 13)"/>'
                '<ellipse cx="12" cy="10" rx="2" ry="2.6" fill="#9CCC65"/>'
            )
            return _svg(body), "Seedling — 7+ days"
        if days >= 3:
            body = (
                _SOIL_S +
                '<path d="M12 20 L12 13.5" stroke="#66BB6A" stroke-width="1.4" stroke-linecap="round"/>'
                '<ellipse cx="10.2" cy="14" rx="1.9" ry="1" fill="#9CCC65" transform="rotate(-30 10.2 14)"/>'
                '<ellipse cx="13.8" cy="14" rx="1.9" ry="1" fill="#9CCC65" transform="rotate(30 13.8 14)"/>'
            )
            return _svg(body), "Sprouting — 3+ days"
        body = (
            _SOIL_S +
            '<ellipse cx="12" cy="18.5" rx="2.1" ry="1.6" fill="#6D4C29"/>'
        )
        return _svg(body), "Seed planted — just starting"

    # BUG FIX: this used to be _streak_icon(longest_streak), and the icon sat
    # next to "Day Best Streak" instead of "Current". The seed -> sprout ->
    # tree growth visual is meant to reflect how your ACTIVE streak is
    # growing right now, not the all-time record - a 0-day current streak
    # showing a full-grown tree next to "Best Streak" doesn't tell you
    # anything about today; a seed next to "Current: 0 days" does.
    _sicon, _stip = _streak_icon(current_streak)

    # ── Today remaining due ───────────────────────────────────────────────────
    # due_today_override is computed via fresh SQL in build_heatmap_html and
    # takes precedence over the cached HeatmapDay value, which can be 0 when
    # sched.counts() returns nothing outside a review session.
    td_due = (due_today_override
              if due_today_override is not None
              else (today_day.due_cards if today_day else 0))
    if today_day and today_day.due_cards > 0:
        _due_parts = []
        if today_day.rev_due > 0: _due_parts.append(f"{today_day.rev_due} review")
        if today_day.lrn_due > 0: _due_parts.append(f"{today_day.lrn_due} learning")
        if today_day.new_due > 0: _due_parts.append(f"{today_day.new_due} new")
        td_due_str = " + ".join(_due_parts) if _due_parts else f"{td_due} cards"
    else:
        td_due_str = ""

    # ── Today's remaining due count — shown inline on the streak row ────────────
    # Always show "due today" — colour signals urgency, green when nothing left.
    _today_col = ("#b05c00" if td_due >= 50 else
                  "#5a6e8a" if td_due >= 15 else "#2e7a40")
    tomorrow_inline = (
        f'<span style="font-weight:600;color:{_today_col}">{td_due}</span>'
        f'&#x202F;<span>cards due today</span>'
    )

    # Again-rate attention strip — rendered inline on the streak row when
    # today's again rate is notably above this week's average (see
    # `show_attention` computed above). Previously computed but never
    # rendered, so the indicator silently never appeared.
    attention_inline = (
        f'<span style="font-weight:600;color:#a03030" '
        f'title="Today\'s again rate ({today_again_rt:.0%}) is higher than '
        f'this week\'s average ({week_again_rt:.0%})">'
        f'&#x26A0;&#xFE0F;&#x202F;elevated again rate</span>'
        if show_attention else ""
    )

    def _det_row(label: str, value: str, warn: bool = False) -> str:
        vc = "color:#8a7200;font-weight:600" if warn else "font-weight:600"
        return (
            f'<tr>'
            f'<td style="color:var(--text-subtle,#888);font-size:11px;padding:5px 0;'
            f'border-bottom:0.5px solid var(--border,rgba(40,40,40,0.09));width:48%">{label}</td>'
            f'<td style="{vc};font-size:11px;padding:5px 0 5px 8px;'
            f'border-bottom:0.5px solid var(--border,rgba(40,40,40,0.09))">{value}</td>'
            f'</tr>'
        )

    def _rev_time(s): return getattr(s, "review_time_minutes", 0.0) if s else 0.0

    # ── JS tab data ───────────────────────────────────────────────────────────
    tab_data = {
        "today":   {"eff": _fmt_mins(_eff(today_stat)),   "q": f"{_q(today_stat):.0%}",
                    "q_raw": _q(today_stat),   "new": _new(today_stat),
                    "cards": _cards(today_stat), "cards_reviewed": _dc(today_stat),
                    "revtime": _fmt_mins(_rev_time(today_stat)),
                    "due": td_due, "due_str": td_due_str,
                    "date": date.today().isoformat(),
                    "start": _range(today_stat)[0], "end": _range(today_stat)[1]},
        "week":    {"eff": _fmt_mins(_eff(week_stat)),    "q": f"{_q(week_stat):.0%}",
                    "q_raw": _q(week_stat),    "new": _new(week_stat),
                    "cards": _cards(week_stat), "cards_reviewed": _dc(week_stat),
                    "revtime": _fmt_mins(_rev_time(week_stat)),
                    "start": _range(week_stat)[0], "end": _range(week_stat)[1]},
        "month":   {"eff": _fmt_mins(_eff(month_stat)),   "q": f"{_q(month_stat):.0%}",
                    "q_raw": _q(month_stat),   "new": _new(month_stat),
                    "cards": _cards(month_stat), "cards_reviewed": _dc(month_stat),
                    "revtime": _fmt_mins(_rev_time(month_stat)),
                    "start": _range(month_stat)[0], "end": _range(month_stat)[1]},
        "alltime": {"eff": _fmt_mins(_eff(alltime_stat)), "q": f"{_q(alltime_stat):.0%}",
                    "q_raw": _q(alltime_stat), "new": _new(alltime_stat),
                    "cards": _cards(alltime_stat), "cards_reviewed": _dc(alltime_stat),
                    "revtime": _fmt_mins(_rev_time(alltime_stat)),
                    "start": _range(alltime_stat)[0], "end": _range(alltime_stat)[1]},
        # BUG FIX: clicking any blank area (deselecting) called _restoreCtx(),
        # which called window.ffTab(_activeTab) — and _activeTab was
        # hardcoded to "week" client-side, completely bypassing the
        # server-computed "Default Statistics View" setting the moment the
        # user interacted with the page at all. This "default" entry mirrors
        # whatever default_stat resolved to above (Today / Last 7 / Last 30 /
        # Remember), and _activeTab now starts on "default" instead of the
        # hardcoded "week" — see near the top of the script block below.
        "default": {"eff": _fmt_mins(_eff(default_stat)),   "q": f"{_q(default_stat):.0%}",
                    "q_raw": _q(default_stat),   "new": _new(default_stat),
                    "cards": _cards(default_stat), "cards_reviewed": _dc(default_stat),
                    "revtime": _fmt_mins(_rev_time(default_stat)),
                    "due": td_due, "due_str": td_due_str,
                    "date": _range(default_stat)[1],
                    "start": _range(default_stat)[0], "end": _range(default_stat)[1]},
    }
    tab_json = _json.dumps(tab_data)

    # ── final HTML ────────────────────────────────────────────────────────────
    # Main screen: tabs + 5 metric cards + streak ONLY.
    # Day detail appears below when the user clicks a heatmap cell.
    # The Today pulse is no longer always-visible — click today's cell instead.

    # Due-card box removed — count still appears in the streak info row
    # and in the context line on cell click. No separate card is rendered.
    # ── Display flags ────────────────────────────────────────────────
    _dsp = display or {}
    show_eff_time    = _dsp.get("show_eff_time",    True)
    show_quality     = _dsp.get("show_quality",     True)
    show_new_cards   = _dsp.get("show_new_cards",   True)
    show_reviews     = _dsp.get("show_reviews",     True)
    show_cards_reviewed = _dsp.get("show_cards_reviewed", True)
    show_rev_time    = _dsp.get("show_rev_time",    True)
    show_streak_info = _dsp.get("show_streak_info", True)
    show_trend       = _dsp.get("show_trend",       True)
    show_attn_pref   = _dsp.get("show_attention",   True)
    show_total_revs  = _dsp.get("show_total_reviews", True)
    colored_numbers  = _dsp.get("colored_numbers",  True)
    comfortable_sp   = _dsp.get("comfortable_spacing", True)
    card_depth       = _dsp.get("card_depth",       True)
    modern_typo      = _dsp.get("modern_typography", True)
    show_progress    = _dsp.get("show_progress_bar", True)

    # BUG FIX: this CSS block reads colored_numbers/comfortable_sp/card_depth/
    # modern_typo, which must be defined first — it used to sit above the
    # "Display flags" block that defines them (NameError waiting to happen
    # the moment any of these toggles were introduced).
    # BUG FIX: card_depth's background/border were hardcoded dark-theme
    # colors (#2f2f2f background, white-alpha border) that don't adapt when
    # Anki is in day mode — unlike every other color in this file, which
    # uses Anki's own theme CSS variables (--canvas-subtle, --border, etc.)
    # and switches automatically. Reusing those same variables here instead
    # of hardcoding hex values fixes it for both themes, consistently with
    # how the rest of the panel already behaves.
    _card_bg     = ("border-radius:12px;"
                     "background:var(--canvas-elevated,var(--canvas-subtle,rgba(0,0,0,0.06)));"
                     "border:1px solid var(--border,rgba(0,0,0,0.08));"
                     "transition:transform .15s ease;padding:10px 12px"
                     if card_depth else
                     "background:var(--canvas-subtle,rgba(0,0,0,0.04));"
                     "border-radius:6px;padding:10px 12px")
    _card_hover  = ".ff-mcard:hover{transform:translateY(-2px)}\n" if card_depth else ""
    _lbl_size, _val_size = ("13px", "32px") if modern_typo else ("10px", "18px")
    _custom_font_size = int(_dsp.get("card_font_size", 0) or 0)
    if _custom_font_size > 0:
        _val_size = f"{_custom_font_size}px"
    _custom_font_family = str(_dsp.get("card_font_family", "") or "")
    _font_css = (
        f"font-family:{_json.dumps(_custom_font_family)},var(--font-family,system-ui,sans-serif);"
        if _custom_font_family else ""
    )
    # Applying the font override sitewide (heatmap month/week labels, streak
    # row, Daily status — not just the 5 stats cards): every element in this
    # panel already reads its font via var(--font-family, ...), so redefining
    # that custom property on the shared #ff-heatmap wrapper cascades the
    # chosen font everywhere at once instead of needing a separate rule per
    # element type.
    _global_font_css = (
        f'#ff-heatmap{{--font-family:{_json.dumps(_custom_font_family)}, system-ui, sans-serif;}}'
        f'#ff-heatmap svg text{{{_font_css}}}'
        if _custom_font_family else ""
    )
    _stats_gap   = "margin-top:24px" if comfortable_sp else ""
    _streak_gap  = "margin-top:18px" if comfortable_sp else ""
    _default_num_colors = {
        "eff": "#5AA9FF", "quality": "#FFA94D", "new": "#4DD9E8",
        "cards": "#B48CFF", "revtime": "#5FD97A", "cards_reviewed": "#F08FB0",
    }
    _user_num_colors = _dsp.get("number_colors") or {}
    _hex_re = _re.compile(r"^#[0-9a-fA-F]{3,8}$")

    def _safe_color(key: str) -> str:
        val = str(_user_num_colors.get(key, _default_num_colors[key]))
        return val if _hex_re.match(val) else _default_num_colors[key]

    _num_colors = (
        f"#ff-m-eff{{color:{_safe_color('eff')}}}"
        f"#ff-m-q{{color:{_safe_color('quality')}}}"
        f"#ff-m-new{{color:{_safe_color('new')}}}"
        f"#ff-m-cards{{color:{_safe_color('cards')}}}"
        f"#ff-m-cards-reviewed{{color:{_safe_color('cards_reviewed')}}}"
        f"#ff-m-revtime{{color:{_safe_color('revtime')}}}"
        if colored_numbers else ""
    )
    # BUG FIX / defense-in-depth: these text colours previously depended
    # entirely on --text-subtle cascading down correctly from the .ff-dark
    # class on the outer #ff-heatmap wrapper. That's the right mechanism in
    # principle, but if it ever fails to reach a specific subtree for any
    # reason, there was no fallback — the element would silently render
    # with light-mode's dark-on-dark colour instead. Resolve the correct
    # colour directly from the night_mode flag we already know here, and
    # use that as the primary value; var(--text-subtle, ...) is now only a
    # backup if this literal value were ever somehow overridden.
    _resolved_subtle = "#d8d8d8" if night_mode else "#3d3d3d"
    _resolved_fg     = "#dcdcdc" if night_mode else "#222222"
    _metric_label_color = str(_dsp.get("metric_label_color", "") or "")
    _mlbl_color_css = (
        f"color:{_metric_label_color}" if _metric_label_color
        else f"color:{_resolved_subtle}"
    )
    css = f"""<style>
{_global_font_css}
.ff-mcard{{{_card_bg}}}
.ff-mcard-click{{cursor:pointer}}
.ff-mcard-click:hover{{outline:1px solid var(--accent-glow,rgba(90,169,255,.6))}}
.ff-mcard-click:hover .ff-mval{{text-decoration:underline}}
{_card_hover}.ff-mlbl{{font-size:{_lbl_size};{_mlbl_color_css};margin-bottom:5px;{_font_css}}}
.ff-mval{{font-size:{_val_size};font-weight:600;line-height:1;{_font_css}}}
{_num_colors}
#ff-mcards{{{_stats_gap}}}
.ff-streak-row{{{_streak_gap}}}
</style>"""

    # All-time total reviews — shown inline on the streak row, similar to
    # "due today". Off by default toggle name is show_total_reviews.
    _total_revs = alltime_stat.reviews_count if alltime_stat else 0
    total_reviews_inline = (
        f'<span style="font-weight:600">{_total_revs:,}</span>'
        f'&#x202F;<span>reviews completed</span>'
        if show_total_revs else ""
    )
    n_base = max(1, sum([show_eff_time, show_quality, show_new_cards,
                         show_reviews, show_cards_reviewed, show_rev_time]))
    _init_cols = f"repeat({n_base},minmax(0,1fr))"
    _due_grid_open_with_data = (
        f'<div style="display:grid;grid-template-columns:{_init_cols};' +
        'gap:8px;margin-bottom:10px" id="ff-mcards">'  # no due card slot needed
    )

    # ── Today's Daily Status ────────────────────────────────────────────────
    # Minimal "N studied · M remaining" line for TODAY specifically (not the
    # chosen default view) — same reasoning the old bar used for reading
    # today_stat rather than default_stat: "today's status" only makes sense
    # as today's own number. Replaces the old gradient progress bar entirely:
    # no percentage, no fill, no gradient. "studied" comes from the new
    # all-inclusive cards_studied stat (revlog types 0-3) — deliberately NOT
    # reviews_count (raw events; a card answered several times today via
    # relearning would inflate this) and NOT cards_reviewed (excludes new
    # cards, would undercount on any day involving new-card study). See
    # HeatmapStats.cards_studied's docstring in heatmap_service.py.
    # "remaining" is td_due, already computed above from the live scheduler
    # queue — unchanged. These are presented as two independent facts, not a
    # ratio: nothing here implies studied + remaining add up to a fixed
    # daily total (a card can legitimately be counted in both — e.g. failed
    # today and still queued for a later relearning step).
    _td_studied = today_stat.cards_studied if today_stat else 0
    if show_progress and (_td_studied > 0 or td_due > 0):
        if td_due > 0:
            _status_line = (
                f'<span style="font-weight:600;color:{_resolved_fg}">{_td_studied}</span>'
                f'&#x202F;<span>studied</span>'
                f'&#x2009;&middot;&#x2009;'
                f'<span style="font-weight:600;color:{_today_col}">{td_due}</span>'
                f'&#x202F;<span>remaining</span>'
            )
        else:
            # remaining == 0 and studied > 0 (the show_progress and (...)
            # gate above guarantees at least one is nonzero, so this branch
            # only runs when studied > 0) — _today_col already evaluates to
            # its "green" tier whenever td_due < 15, which td_due == 0
            # always satisfies, so reusing it here keeps this state visually
            # part of the same colour language as the numeric case above
            # rather than introducing a separate hardcoded colour.
            _status_line = (
                f'<span style="font-weight:600;color:{_resolved_fg}">{_td_studied}</span>'
                f'&#x202F;<span>studied</span>'
                f'&#x2009;&middot;&#x2009;'
                f'<span style="font-weight:600;color:{_today_col}">All caught up</span>'
            )
        progress_html = (
            f'<div style="margin:6px auto {"24px" if comfortable_sp else "10px"};max-width:700px;'
            f'font-family:{_json.dumps(_custom_font_family) if _custom_font_family else "var(--font-family,system-ui,sans-serif)"};'
            f'font-size:11px;color:{_resolved_subtle}">'
            f'<div style="margin-bottom:2px">Today</div>'
            f'<div>{_status_line}</div>'
            f'</div>'
        )
    else:
        progress_html = ""

    # ── Friendly empty state ────────────────────────────────────────────────
    # Replaces the "everything reads 0" dead feeling with a short encouraging
    # line, shown alongside (not instead of) the numeric cards — the numbers
    # stay for consistency, this just adds warmth when there's truly nothing
    # in the selected period yet.
    _is_empty = _eff(default_stat) == 0 and _cards(default_stat) == 0 and _new(default_stat) == 0
    empty_state_html = (
        '<div style="text-align:center;margin:2px auto 10px;max-width:700px;'
        'font-family:var(--font-family,system-ui,sans-serif);font-size:12px;'
        'color:var(--text-subtle,#888);font-style:italic">'
        'No reviews yet &mdash; ready when you are!</div>'
        if _is_empty and _dsp.get("show_empty_state_message", True) else ""
    )

    # ── Pre-build conditional HTML blocks ─────────────────────────────
    _c: list[str] = []
    if show_eff_time:
        _c.append(
            '    <div class="ff-mcard"><div class="ff-mlbl">Effective time</div>'
            f'<div class="ff-mval" id="ff-m-eff">{_fmt_mins(_eff(default_stat))}</div></div>\n'
        )
    if show_quality:
        _trend_extra = (
            f'<div style="font-size:10px;color:{trend_col};margin-top:3px"'
            f' title="{trend_tip}" id="ff-m-trend"'
            f' data-orig="{trend_lbl}" data-orig-color="{trend_col}">'
            f'{trend_lbl}</div>'
            if show_trend else ""
        )
        _c.append(
            '    <div class="ff-mcard"><div class="ff-mlbl">Study Quality</div>'
            f'<div class="ff-mval" id="ff-m-q">{_q_badge(_q(default_stat))}</div>'
            f'{_trend_extra}</div>\n'
        )
    if show_new_cards:
        _c.append(
            '    <div class="ff-mcard"><div class="ff-mlbl">New cards</div>'
            f'<div class="ff-mval" id="ff-m-new">{_new(default_stat)}</div></div>\n'
        )
    if show_reviews:
        _c.append(
            '    <div class="ff-mcard"><div class="ff-mlbl">Reviews</div>'
            f'<div class="ff-mval" id="ff-m-cards">{_cards(default_stat)}</div></div>\n'
        )
    if show_cards_reviewed:
        _c.append(
            '    <div class="ff-mcard"><div class="ff-mlbl">Cards Reviewed</div>'
            f'<div class="ff-mval" id="ff-m-cards-reviewed">{_dc(default_stat)}</div></div>\n'
        )
    if show_rev_time:
        _c.append(
            '    <div class="ff-mcard"><div class="ff-mlbl">Review time</div>'
            f'<div class="ff-mval" id="ff-m-revtime">{_fmt_mins(_rev_time(default_stat))}</div></div>\n'
        )
    _cards_html = "".join(_c)
    _streak_text_color = str(_dsp.get("streak_text_color", "") or "")
    _streak_color_css = (
        f"color:{_streak_text_color}" if _streak_text_color
        else f"color:{_resolved_subtle}"
    )
    _streak_row_open  = (
        '  <div class="ff-streak-row" style="display:flex;align-items:center;gap:6px;margin-bottom:4px;\n'
        f'    font-size:11px;{_streak_color_css};flex-wrap:wrap">\n'
        if show_streak_info else ""
    )
    _streak_row_close = ("  </div>\n" if show_streak_info else "")

    _stats_shared_style = (
        "font-family:var(--font-family,system-ui,sans-serif);"
        "font-size:var(--base-font-size,12px);color:var(--text-fg,#222)"
    )

    cards_block_html = f"""<div id="ff-stats-redesign" style="margin:8px auto 4px;max-width:700px;{_stats_shared_style}">
{_due_grid_open_with_data}
{_cards_html}  </div>
{empty_state_html}
</div>"""

    _best_streak_inline = (
        f'<span style="font-weight:600;color:var(--text-fg,#222)">{longest_streak}</span>&#x202F;'
        f'<span>Day Best Streak</span>'
        if _dsp.get("show_best_streak", True) else ""
    )
    _current_streak_inline = (
        f'<span style="font-size:14px" title="{_stip}">{_sicon}</span>&#x202F;'
        f'<span>Current:</span>&#x202F;'
        f'<span style="font-weight:600;color:var(--text-fg,#222)">{current_streak} days</span>'
        if _dsp.get("show_current_streak", True) else ""
    )
    # Join every optional streak-row segment with a separating dot, skipping
    # whichever ones are toggled off, so hiding any combination of them never
    # leaves a dangling leading "·" (each toggle used to hardcode its own
    # leading separator, which broke as soon as an earlier segment was off).
    #
    # BUG FIX (1.0.28): show_streak_info previously only gated the wrapper
    # div (_streak_row_open/_streak_row_close below) — it did NOT gate this
    # list itself, so an individually-enabled segment (e.g. show_due_today)
    # still rendered its text into the always-present outer container even
    # with show_streak_info=False, just without the wrapper's flex/font/
    # colour styling. The trailing "if show_streak_info else []" is the
    # entire fix: it forces every segment to collapse to nothing whenever
    # the master toggle is off, regardless of any individual setting, while
    # leaving every individual setting's stored value and its own on/on
    # behavior when the master IS on completely unchanged.
    _streak_segments = [
        s for s in (
            _best_streak_inline,
            _current_streak_inline,
            tomorrow_inline if _dsp.get("show_due_today", True) else "",
            attention_inline if show_attn_pref else "",
            total_reviews_inline,
        ) if s
    ] if show_streak_info else []
    _streak_joined = "&nbsp;&#xB7;&nbsp;".join(_streak_segments)

    streak_block_html = f"""<div style="margin:4px auto;max-width:700px;{_stats_shared_style}">
  {_streak_row_open}    {_streak_joined}
    <span id="ff-ctx-sep" style="display:none">&nbsp;&#xB7;&nbsp;</span>
    <span id="ff-ctx" style="display:none"></span>
  {_streak_row_close}
</div>"""

    js = f"""<script>
(function(){{
  var _d={tab_json};
  function _badge(q){{
    var bg,fg;
    if(q>=0.75){{bg="rgba(52,130,68,.14)";fg="#2e7a40";}}
    else if(q>=0.50){{bg="rgba(184,157,40,.14)";fg="#8a7200";}}
    else{{bg="rgba(160,160,160,.12)";fg="#6a6a6a";}}
    return '<span style="display:inline-block;padding:1px 8px;border-radius:99px;'+
      'font-size:11px;font-weight:600;background:'+bg+';color:'+fg+'">'+
      Math.round(q*100)+'%</span>';
  }}
  var _baseQ={_q(alltime_stat)};
  window.ffTab=function(key){{
    var t=_d[key];if(!t)return;
    var _ge=function(id){{return document.getElementById(id);}};
    if(_ge('ff-m-eff'))     _ge('ff-m-eff').textContent    =t.eff;
    if(_ge('ff-m-q'))       _ge('ff-m-q').innerHTML        =_badge(t.q_raw);
    if(_ge('ff-m-new'))     _ge('ff-m-new').textContent    =t.new;
    if(_ge('ff-m-cards'))   _ge('ff-m-cards').textContent  =t.cards;
    if(_ge('ff-m-cards-reviewed')) _ge('ff-m-cards-reviewed').textContent=t.cards_reviewed;
    if(_ge('ff-m-revtime')) _ge('ff-m-revtime').textContent=t.revtime;
    if(window.ffWireCardClick) window.ffWireCardClick(t.start || null, t.end || null);
    var delta=t.q_raw-_baseQ;
    var tEl=document.getElementById('ff-m-trend');
    if(tEl){{
      var lbl=delta>0.04?"\u2191 improving":delta<-0.04?"\u2193 declining":"\u2192 stable";
      var col=delta>0.04?"#2e7a40":delta<-0.04?"#8a7200":"var(--text-subtle,#888)";
      tEl.textContent=lbl;tEl.style.color=col;
    }}
  }};
  // Wire up click-to-browse for the initial (pre-click) card view too —
  // deferred with the same setTimeout(0) trick _JS_INTERACT itself uses to
  // hook window.ffTab, since script blocks in this panel run in document
  // order and this one shouldn't have to assume it runs after (or before)
  // ffWireCardClick's own definition.
  setTimeout(function(){{ if(window.ffTab) window.ffTab("default"); }}, 0);
}})();
</script>"""

    return {
        "css": css,
        "progress": progress_html,
        "cards": cards_block_html,
        "streak": streak_block_html,
        "js": js,
    }


class HeatmapCanvas(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._days:   list[HeatmapDay] = []
        self._scheme  = "forest"
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def set_days(self, days: list[HeatmapDay], scheme: str = "forest") -> None:
        self._days = days; self._scheme = scheme
        if days:
            cols = (len(days) + 6) // 7
            step = _CELL + _GAP
            self.setFixedSize(max(16 + cols * step, 200), 22 + 7 * step + 8)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(760, 120)

    def paintEvent(self, _event) -> None:
        if not self._days: return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        step = _CELL + _GAP; x0 = 8; y0 = 22; today = date.today()
        lo, hi  = COLOR_SCHEMES.get(self._scheme, COLOR_SCHEMES["forest"])
        max_eff = max((d.effective_minutes for d in self._days if not d.is_future), default=0.0)
        max_due = max((d.due_cards        for d in self._days if d.is_future),     default=1)
        prev_month = None
        for idx, d in enumerate(self._days):
            col = idx // 7; row = idx % 7
            x = x0 + col * step; y = y0 + row * step
            if row == 0 and d.day.month != prev_month:
                prev_month = d.day.month
                painter.setPen(QColor(140, 138, 134))
                painter.drawText(int(x), y0 - 5, _fmt_month(d.day))
            if d.is_future:
                if d.due_cards <= 0:
                    color = QColor(195, 191, 186, 48)
                else:
                    a = int((0.16 + 0.48 * min(1.0, d.due_cards / max(max_due, 1)) ** 0.70) * 255)
                    color = QColor(82, 132, 200, a)
            else:
                intensity = 0.0 if max_eff <= 0 else min(1.0, d.effective_minutes / max_eff)
                if intensity <= 0:
                    color = QColor(195, 191, 186, 76)
                else:
                    q  = max(0.0, min(1.0, d.quality))
                    r_ = int(lo[0] + (hi[0] - lo[0]) * q)
                    g_ = int(lo[1] + (hi[1] - lo[1]) * q)
                    b_ = int(lo[2] + (hi[2] - lo[2]) * q)
                    if intensity < 0.40:
                        fade = (0.40 - intensity) / 0.40
                        r_ = int(r_ + (245 - r_) * fade * 0.45)
                        g_ = int(g_ + (245 - g_) * fade * 0.45)
                        b_ = int(b_ + (242 - b_) * fade * 0.45)
                    a = int((0.28 + 0.64 * (intensity ** 0.65)) * 255)
                    color = QColor(r_, g_, b_, a)
            painter.setBrush(color)
            painter.setPen(QColor(168, 162, 155, 64))
            painter.drawRoundedRect(QRectF(x, y, _CELL, _CELL), 2.0, 2.0)
            if d.day == today:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QColor(50, 50, 50, 200))
                painter.drawRoundedRect(QRectF(x + 0.5, y + 0.5, _CELL - 1, _CELL - 1), 2.0, 2.0)
            # Goal-reached corner triangle (matches SVG version)
            if d.is_goal_reached and not d.is_future and d.sessions > 0:
                _qt_tri = max(3, int(_CELL * 0.35))
                painter.setBrush(QColor(255, 255, 255, 210))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawPolygon(QPolygonF([
                    QPointF(x + _CELL - _qt_tri, y),
                    QPointF(x + _CELL, y),
                    QPointF(x + _CELL, y + _qt_tri),
                ]))


# ── HeatmapDialog (Tools menu) ────────────────────────────────────────────────

class HeatmapDialog(QDialog):
    def __init__(self, service: HeatmapService, hm_config: dict, parent=None) -> None:
        super().__init__(parent)
        self.service    = service
        self.hm_config  = dict(hm_config)
        self._year      = date.today().year
        self.setWindowTitle("FocusFlow — Study Stats")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self); layout.setSpacing(10)

        nav = QHBoxLayout()
        self.btn_prev = QPushButton("◀"); self.btn_prev.setFixedWidth(36)
        self.btn_next = QPushButton("▶"); self.btn_next.setFixedWidth(36)
        self.btn_prev.clicked.connect(self._prev_year)
        self.btn_next.clicked.connect(self._next_year)
        self.year_lbl = QLabel(str(self._year))
        self.year_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self.year_lbl.font(); f.setBold(True); f.setPointSize(12)
        self.year_lbl.setFont(f)
        nav.addWidget(self.btn_prev); nav.addStretch()
        nav.addWidget(self.year_lbl); nav.addStretch()
        nav.addWidget(self.btn_next); layout.addLayout(nav)

        sg = QGroupBox("Stats", self)
        self.stats_grid = QGridLayout(sg)
        self._stat_labels: dict[tuple, QLabel] = {}
        headers = ("Period", "Study time", "Effective", "Study Quality", "Sessions", "New", "Reviews", "Cards Reviewed")
        for col, h in enumerate(headers):
            lbl = QLabel(h); lbl.setStyleSheet("font-weight:600;")
            self.stats_grid.addWidget(lbl, 0, col)
        for row in range(1, 6):
            for col in range(len(headers)):
                lbl = QLabel(""); self._stat_labels[(row, col)] = lbl
                self.stats_grid.addWidget(lbl, row, col)
        layout.addWidget(sg)
        self._refresh(); self._refresh_nav()

    def _prev_year(self) -> None:
        self._year -= 1; self.year_lbl.setText(str(self._year))
        self._refresh(); self._refresh_nav()

    def _next_year(self) -> None:
        self._year += 1; self.year_lbl.setText(str(self._year))
        self._refresh(); self._refresh_nav()

    def _refresh_nav(self) -> None:
        today = date.today()
        self.btn_next.setEnabled(self._year < today.year + 1)
        self.btn_prev.setEnabled(self._year > 2020)

    def _refresh(self) -> None:
        try:
            stats = self.service.stats()
            for row_idx, stat in enumerate(stats, start=1):
                # Match "2026", "2025", etc. — the service now emits the actual
                # year string instead of the literal "Yearly" sentinel.  The
                # regex also handles any legacy "Yearly" value from older caches.
                is_year_row = bool(_re.fullmatch(r"\d{4}", stat.label)) or stat.label == "Yearly"
                label = str(self._year) if is_year_row else stat.label
                # For the year row, query the year the user has navigated to
                # (self._year), not necessarily the current calendar year.
                if is_year_row:
                    yr_s = self.service.stats_for_year(self._year)
                    values = (
                        label,
                        _fmt_mins(yr_s.get("minutes", 0)),
                        _fmt_mins(yr_s.get("effective", 0)),
                        f"{yr_s.get('quality', 0):.0%}",
                        str(yr_s.get("sessions", 0)),
                        str(yr_s.get("new_cards", 0)),
                        str(yr_s.get("reviews", 0)),
                        str(yr_s.get("cards_reviewed", 0)),
                    )
                else:
                    values = (
                        label,
                        _fmt_mins(stat.minutes),
                        _fmt_mins(stat.effective_minutes),
                        f"{stat.quality:.0%}",
                        str(stat.sessions),
                        str(stat.new_cards),
                        str(stat.reviews_count),
                        str(stat.cards_reviewed),
                    )
                for col, val in enumerate(values):
                    if (row_idx, col) in self._stat_labels:
                        self._stat_labels[(row_idx, col)].setText(val)
        except Exception as exc:
            log.warning("HeatmapDialog._refresh failed: %s", exc)


# ── build_heatmap_html  (deck browser) ───────────────────────────────────────

def _derive_render_cfg(hm_config: dict) -> tuple[int, int, int, tuple, tuple, str, dict]:
    """Shared config extraction for both the eager build_heatmap_html() path
    and the on-demand render_single_year_svg() path (see PERFORMANCE FIX
    below) — kept in one place so the two can never drift out of sync."""
    cell        = int(hm_config.get("cell_size", _CELL))
    heavy_cards = int(hm_config.get("heavy_cards", 50))
    light_cards = int(hm_config.get("light_cards", 15))
    scheme_key  = hm_config.get("color_scheme", "forest")
    grouping    = hm_config.get("grouping", "monthly")
    if scheme_key == "custom":
        lo, hi = _scheme_from_hex(str(hm_config.get("custom_scheme_color", "") or "#5284C8"))
    else:
        lo, hi = COLOR_SCHEMES.get(scheme_key, COLOR_SCHEMES["forest"])
    _raw_dsp = hm_config.get("display")
    disp_cfg = dict(_raw_dsp) if isinstance(_raw_dsp, dict) else {}
    _raw_notes = hm_config.get("day_notes")
    disp_cfg["_day_notes"] = dict(_raw_notes) if isinstance(_raw_notes, dict) else {}
    disp_cfg["cell_shape"] = hm_config.get("cell_shape", "soft")
    disp_cfg["note_marker_shape"] = hm_config.get("note_marker_shape", "triangle")
    disp_cfg["note_marker_position"] = hm_config.get("note_marker_position", "bottom_right")
    return cell, heavy_cards, light_cards, lo, hi, grouping, disp_cfg


def _year_svg_block(yr: int, days: list[HeatmapDay], cell: int, lo: tuple, hi: tuple,
                     heavy_cards: int, light_cards: int, grouping: str, disp_cfg: dict,
                     width: int | None, height: int | None, display: str) -> tuple[str, int, int]:
    """Render one year's complete <svg id="ff-svg-{yr}"> block, standalone
    enough that it can be injected into the DOM independently — see
    render_single_year_svg(). Does NOT bundle its own copy of _DEFS: that
    lives once, permanently, in build_heatmap_html()'s nav_html (see the
    BUG FIX note there) so every year's cells — eager or lazily fetched —
    share the exact same ff-cell-sheen definition instead of the DOM
    accumulating one duplicate id="ff-cell-sheen" per year visited."""
    inner, w, h = _render_year(days, cell, lo, hi, heavy_cards, light_cards, grouping, disp_cfg)
    w = width if width is not None else w
    h = height if height is not None else h
    html = (
        f'<svg id="ff-svg-{yr}" class="ff-yr-svg" data-year="{yr}" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'width="{w}" height="{h}" '
        f'style="display:{display};margin:4px auto 2px;overflow:visible">'
        + inner + '</svg>\n'
    )
    return html, w, h


def render_single_year_svg(year: int, days: list[HeatmapDay], hm_config: dict) -> str:
    """Render one year's <svg> block on its own, for lazy on-demand loading.

    PERFORMANCE FIX: build_heatmap_html() used to render a full day-grid SVG
    (with every cell's colours, note markers, and tooltip data) for every
    year in the navigable range — 3 years by default, more with "Show full
    history" on — on *every single* deck-browser render, even though a user
    looking at the panel is looking at exactly one year at a time. The other
    years' markup was pure waste 99% of the time. Now only the current year
    is rendered eagerly (see _build_days_by_year in __init__.py, which also
    skips the underlying day-list computation for the other years); this
    function is called instead, on demand, the first time the user actually
    clicks ◀/▶ to a year that isn't in the DOM yet — wired through Anki's
    webview_did_receive_js_message bridge (__init__.py's "ff_get_year:"
    handler) and the updated ffNav() JS below.
    """
    if not days:
        return ""
    cell, heavy_cards, light_cards, lo, hi, grouping, disp_cfg = _derive_render_cfg(hm_config)
    html, _, _ = _year_svg_block(year, days, cell, lo, hi, heavy_cards, light_cards,
                                  grouping, disp_cfg, width=None, height=None, display="block")
    return html


def build_heatmap_html(
    days_by_year: dict[int, list[HeatmapDay]],
    hm_config: dict,
    period_stats: list[HeatmapStats] | None = None,
    year_stats: dict | None = None,
    current_streak: int = 0,
    longest_streak: int = 0,
    color_mode: str = "reviews",
    night_mode: bool = False,
) -> str:
    if not days_by_year:
        return ""

    cell, heavy_cards, light_cards, lo, hi, grouping, disp_cfg = _derive_render_cfg(hm_config)

    today      = date.today()
    cur_year   = today.year
    years      = sorted(days_by_year.keys())
    first_year = years[0]
    last_year  = years[-1]

    svg_blocks: dict[int, str] = {}
    max_w = 0
    svg_h = 100

    for yr, days in days_by_year.items():
        # Years with an empty day-list are ones _build_days_by_year() left
        # unrendered on purpose (see render_single_year_svg() above) — they
        # get fetched lazily if/when the user navigates to them, not here.
        if not days:
            continue
        inner, w, h = _render_year(days, cell, lo, hi, heavy_cards, light_cards, grouping, disp_cfg)
        svg_blocks[yr] = inner
        max_w = max(max_w, w)
        svg_h = max(svg_h, h)   # take the tallest year so week row is never clipped

    if not svg_blocks:
        return ""

    svgs_html = ""
    for yr, inner in sorted(svg_blocks.items()):
        display = "block" if yr == cur_year else "none"
        svgs_html += (
            f'<svg id="ff-svg-{yr}" class="ff-yr-svg" data-year="{yr}" '
            f'xmlns="http://www.w3.org/2000/svg" '
            f'width="{max_w}" height="{svg_h}" '
            f'style="display:{display};margin:4px auto 2px;overflow:visible">'
            + inner + '</svg>\n'
        )

    # BUG FIX: _DEFS (the ff-cell-sheen gradient every cell's gloss overlay
    # references via url(#ff-cell-sheen)) used to be embedded once per
    # per-year <svg> block above, and again in _year_svg_block() for every
    # lazily-fetched year (see render_single_year_svg()). id attributes are
    # unique per *document*, not per <svg> root, so the moment a second year
    # got fetched, the DOM held two elements with id="ff-cell-sheen" — every
    # existing url(#ff-cell-sheen) reference (including the already-visible
    # current year's, since ffApplyColorMode's querySelectorAll runs across
    # the whole document) then resolves against whichever one the engine
    # picks, which is order-dependent and shifts every time a year is
    # fetched or the DOM is reshuffled — exactly the "looks different after
    # cycling years, fixed by a full refresh" symptom, since a full rebuild
    # (e.g. on sync) starts over with a single, unambiguous copy.
    # One copy, living inside `nav_html` (which is always present and never
    # replaced or removed by the lazy-fetch path — see ffNav() below), is
    # now the single source every year's cells reference, regardless of how
    # many years get fetched over the session.
    defs_html = f'<svg width="0" height="0" style="position:absolute" aria-hidden="true">{_DEFS}</svg>'

    # Year visibility — opacity-only (see #ff-year-lbl CSS), so the label's
    # reserved space never changes and the ◀/▶ arrows never move, in any
    # mode. Independent of the toolbar's own HUD visibility setting.
    _year_vis = disp_cfg.get("year_visibility", "always")
    _nav_class = ' class="ff-year-hover"' if _year_vis == "hover" else (
        ' class="ff-year-hidden"' if _year_vis == "hidden" else '')

    nav_html = (
        defs_html
        + f'<div id="ff-nav"{_nav_class}>'
        f'<button id="ff-prev" onclick="ffNav(-1)">◀</button>'
        f'<span id="ff-year-lbl">{cur_year}</span>'
        f'<button id="ff-next" onclick="ffNav(1)">▶</button>'
        f'</div>'
    )
    js_colormode = f"""<script>
(function(){{
  // BUG FIX: this used to be a plain `var savedMode` local to this IIFE,
  // and js_nav (below) declared its own SEPARATE `var savedMode` local to
  // ITS IIFE — two independent copies, both seeded from the same initial
  // {{color_mode}} value but never kept in sync. Changing the dropdown only
  // ever updated this IIFE's copy (and the cells already in the DOM); the
  // moment the user then navigated ◀/▶ to a year that hadn't been fetched
  // yet, js_nav's still-stale copy got handed to ffApplyColorMode(), so the
  // freshly-injected year rendered in the OLD colour mode while the
  // already-visible year showed the new one — the exact "looks different
  // after cycling years" symptom. Using a single window.ffColorMode as the
  // one shared source of truth means every reader always sees the same,
  // current value, however it was last changed.
  window.ffColorMode = "{color_mode}";
  function applyMode(mode){{
    document.querySelectorAll('.ff-cell').forEach(function(el){{
      var f = el.getAttribute('data-fill-'+mode);
      if(f) el.setAttribute('fill', f);
    }});
  }}
  // Exposed globally so js_nav's lazy year-fetch path (see render_single_year_svg
  // in heatmap_widget.py) can re-apply the currently-selected colour mode to
  // cells that get injected into the DOM later, well after this IIFE runs —
  // those cells are rendered server-side with the default "reviews" fill.
  window.ffApplyColorMode = applyMode;
  // Apply saved mode on load (cells are rendered with "reviews" fill by default)
  if(window.ffColorMode !== "reviews") applyMode(window.ffColorMode);
  // Save mode back to Python whenever the user changes the dropdown
  var modeEl = document.getElementById('ff-color-mode');
  if(modeEl) {{
    modeEl.addEventListener('change', function(){{
      window.ffColorMode = this.value;
      applyMode(this.value);
      if(typeof pycmd !== 'undefined') pycmd('ff_color_mode:' + this.value);
    }});
  }}
}})();
</script>"""

    js_nav = f"""<script>
(function(){{
  var curYear   = {cur_year};
  var firstYear = {first_year};
  var lastYear  = {last_year};
  // BUG FIX: no longer keeps its own local `savedMode` copy — see the
  // matching comment in js_colormode above. Reads window.ffColorMode at
  // call time instead, so it always reflects whatever mode is currently
  // active, even if the user changed it after this script first ran.
  function showYear(ny){{
    document.querySelectorAll('.ff-yr-svg').forEach(function(s){{
      s.style.display=(parseInt(s.getAttribute('data-year'))===ny)?'block':'none';
    }});
    document.getElementById('ff-year-lbl').textContent = ny;
    document.getElementById('ff-prev').disabled = (ny<=firstYear);
    document.getElementById('ff-next').disabled = (ny>=lastYear);
    if(typeof _ffUpdateStats === 'function') _ffUpdateStats(ny);
  }}
  window.ffNav = function(dir){{
    var ny = curYear + dir;
    if(ny < firstYear || ny > lastYear) return;
    curYear = ny;
    // PERFORMANCE FIX: only the current year's grid is pre-rendered in the
    // initial panel HTML now (see render_single_year_svg() in
    // heatmap_widget.py) — other years used to always be in the DOM
    // already, hidden with display:none, but that meant building a full
    // SVG grid for years nobody was looking at on every single deck-browser
    // refresh. If the target year isn't in the DOM yet, fetch just that one
    // on demand and cache it in place (as a real, still-hidden .ff-yr-svg
    // element) so navigating back to it later is instant, exactly like the
    // pre-rendered years used to be.
    var existing = document.getElementById('ff-svg-' + ny);
    if(existing){{
      showYear(ny);
      return;
    }}
    if(typeof pycmd === 'undefined'){{
      showYear(ny);   // no bridge available — nothing more we can do
      return;
    }}
    var nav = document.getElementById('ff-nav');
    pycmd('ff_get_year:' + ny, function(html){{
      if(html){{
        var holder = document.createElement('div');
        holder.innerHTML = html;
        var svgEl = holder.firstElementChild;
        // Insert right AFTER nav — matching the "nav_html + svgs_html"
        // ordering in build_heatmap_html() above, so every year's grid
        // lands in the same group (below nav), whether it was the eager
        // current year or fetched here later. Inserting before nav instead
        // would put freshly-fetched years above nav while the original
        // eager year stayed below it — an inconsistent position depending
        // on which year happened to still be in its original DOM spot.
        if(svgEl && nav && nav.parentNode) nav.parentNode.insertBefore(svgEl, nav.nextSibling);
        // The newly-injected cells carry the fill baked in for whatever
        // colour mode was active on the *server* side; re-apply the
        // client's current selection (window.ffColorMode — always up to
        // date, see the BUG FIX note above) so a mid-session mode change
        // also covers cells that arrive later via this lazy path.
        if(typeof window.ffApplyColorMode === 'function') window.ffApplyColorMode(window.ffColorMode);
      }}
      showYear(ny);
    }});
  }};
  document.getElementById('ff-prev').disabled = (curYear<=firstYear);
  document.getElementById('ff-next').disabled = (curYear>=lastYear);
}})();
</script>"""

    show_stats = (
        period_stats is not None
        and year_stats is not None
    )
    stats_blocks: dict[str, str] = {"css": "", "progress": "", "cards": "", "streak": "", "js": ""}
    if show_stats:
        # Extract today's HeatmapDay from days_by_year so the Today card
        # reads from the exact same revlog source as the day-detail card.
        # This fixes the data mismatch between Today and clicking today's cell.
        today_day: HeatmapDay | None = None
        cur_year_days = days_by_year.get(cur_year, [])
        for d in cur_year_days:
            if d.day == today and not d.is_future:
                today_day = d
                break
        # "Remember Last Selection" — look up the previously-clicked date
        # (persisted across Anki restarts) across every year we have loaded.
        # Falls back to "today" further down if not found (e.g. it's older
        # than the ~2 years of history we keep in days_by_year).
        remembered_day: HeatmapDay | None = None
        _default_view = str(hm_config.get("default_stats_view", "today"))
        if _default_view == "remember":
            _last_sel = str(hm_config.get("last_selected_date", "") or "")
            if _last_sel:
                try:
                    _last_sel_date = date.fromisoformat(_last_sel)
                    for _yr_days in days_by_year.values():
                        for d in _yr_days:
                            if d.day == _last_sel_date and not d.is_future:
                                remembered_day = d
                                break
                        if remembered_day is not None:
                            break
                except ValueError:
                    pass
        # Collect all future days across years for the forecast summary
        future_days: list[HeatmapDay] = sorted(
            (d for days in days_by_year.values() for d in days if d.is_future),
            key=lambda d: d.day,
        )
        # Single SQL query — covers all actionable card states without needing
        # sched.counts() (which returns (0,0,0) in the deck browser in Anki 25.x).
        #   queue=2  review cards        due = ordinal day
        #   queue=3  day-relearn steps   due = ordinal day
        #   queue=1  intraday learning   due = unix timestamp (seconds)
        #
        # BUG (found by user report + screenshot): the raw-SQL count below
        # ignores each deck's daily new/review limits entirely, so once
        # today's limit is used up it kept counting the remaining backlog as
        # "due today" even though Anki's own deck list correctly shows 0/0/0
        # for the day. sched.counts() *does* respect those limits — the
        # (0,0,0) issue mentioned above turned out to be a missing
        # sched.reset() before reading counts, not a fundamental problem
        # with the API — so we now prefer it and only fall back to the raw
        # SQL if the scheduler call itself fails.
        _due_today_count: int = 0
        try:
            from aqt import mw as _mw
            if _mw and _mw.col:
                try:
                    _mw.col.sched.reset()
                    _new_c, _lrn_c, _rev_c = _mw.col.sched.counts()
                    _due_today_count = int(_new_c) + int(_lrn_c) + int(_rev_c)
                except Exception:
                    import time as _t2
                    try:
                        _tod = int(_mw.col.sched.today)
                    except Exception:
                        from datetime import date as _date_fb
                        _tod = (_date_fb.today() - _date_fb(2006, 1, 1)).days
                    _nw = int(_t2.time())
                    _due_today_count = int(_mw.col.db.scalar(
                        "SELECT count() FROM cards "
                        "WHERE (queue = 0) "
                        "   OR (queue = 2 AND due <= ?) "
                        "   OR (queue = 3 AND due <= ?) "
                        "   OR (queue = 1 AND due <= ?)",
                        _tod, _tod, _nw,
                    ) or 0)
        except Exception:
            pass

        try:
            stats_blocks = _build_stats_html(
                period_stats, year_stats, cur_year,
                current_streak, longest_streak,
                today_day=today_day,
                future_days=future_days,
                due_today_override=_due_today_count,
                display=disp_cfg,
                default_view=_default_view,
                remembered_day=remembered_day,
                night_mode=night_mode,
            )
        except Exception:
            import traceback as _tb
            log.error("stats panel error: %s", _tb.format_exc())
            stats_blocks = {"css": "", "progress": "", "cards": "", "streak": "", "js": ""}

    # JS grid-column vars so _applyData can resize the grid correctly
    # regardless of how many metric cards are visible.
    _n_base = max(1, sum([
        disp_cfg.get("show_eff_time",   True),
        disp_cfg.get("show_quality",    True),
        disp_cfg.get("show_new_cards",  True),
        disp_cfg.get("show_reviews",    True),
        disp_cfg.get("show_cards_reviewed", True),
        disp_cfg.get("show_rev_time",   True),
    ]))
    _stats_popup = disp_cfg.get("stats_popup", False)
    _js_grid_vars = (
        "<script>"
        f"window._ffBaseCols='repeat({_n_base},minmax(0,1fr))';"
        f"window._ffStatsMode='{"popup" if _stats_popup else "inline"}'"
        "</script>"
    )
    _popup_js = f"""
<script>
window._ffPopupAutoCloseSecs = {int(disp_cfg.get("popup_auto_close_secs", 0) or 0)};
(function(){{
  var _ffPopupTimer = null;
  window._ffShowPopup = function(d, cx, cy){{
    var popup = document.getElementById("ff-popup");
    if(!popup) return;
    var t = document.getElementById("ff-popup-title");
    // BUG FIX: month/week clicks only ever populate "ctx" (e.g. "Apr 2026",
    // "Week 16") — never "label"/"date" — so the popup title was silently
    // blank for those. ctx is also the nicer formatted string day cells
    // already carry, so prefer it everywhere for a consistent look.
    if(t) t.textContent = d.ctx || d.label || d.date || "";
    var rows = [];
    // BUG FIX: future (forecast) days used to fall straight into the same
    // rows as a real day below — Effective time, Study Quality, New cards,
    // Reviews, Cards Reviewed, Review time — every one of them a
    // meaningless placeholder ("—", 0%, 0, 0, 0, "—") for a day that
    // hasn't happened yet. The one row that WOULD show something useful
    // (the due-card breakdown) only ever appeared for d.is_today, which a
    // future day never satisfies — so clicking any forecast (blue) cell
    // showed a wall of zeros and nothing else, with no way to see which
    // cards were actually due. Future days get their own branch instead:
    // just the due breakdown, wired to open the Browser the same way the
    // other clickable rows already do.
    if(d.state === "future"){{
      var dueStr = (typeof _buildDueStr === "function") ? _buildDueStr(d) : ((d.due||0) + " cards");
      rows.push(["Due", dueStr, d.due > 0 ? "due" : null]);
    }} else {{
    if(d.eff)     rows.push(["Effective time", d.eff]);
    if(typeof d.q_raw === "number")
      rows.push(["Study Quality", (typeof _badge === "function") ? _badge(d.q_raw) : Math.round(d.q_raw*100)+"%"]);
    if(d.new !== undefined && d.new !== null) rows.push(["New cards", d.new, "new"]);
    // BUG FIX: this used a truthy check (if(d.cards)), so a legitimate
    // value of exactly 0 silently hid the whole row — unlike "New cards"
    // just above, which already used the correct explicit check.
    if(d.cards !== undefined && d.cards !== null) rows.push(["Reviews", d.cards, "reviewed"]);
    if(d.cards_reviewed !== undefined && d.cards_reviewed !== null)
      rows.push(["Cards Reviewed", d.cards_reviewed, "cards_reviewed"]);
    if(d.revtime) rows.push(["Review time", d.revtime]);
    if(d.is_today && d.due > 0 && typeof _buildDueStr === "function")
      rows.push(["Still due", _buildDueStr(d)]);
    }}
    var body = document.getElementById("ff-popup-body");
    if(body){{
      var rowsHtml = rows.map(function(r){{
        // "New cards" / "Reviews" / "Cards Reviewed" carry a 3rd element
        // (a "kind" matching a key in __init__.py's _BROWSE_TERMS registry)
        // marking them clickable — opens Anki's own Browser pre-filtered to
        // this period's cards of that kind (see ffBrowseStat() and the
        // "ff_browse_range:" handler in __init__.py). Nothing is computed
        // or fetched here beyond what the popup already had (the date
        // range + which row was clicked), so this stays a thin, stateless
        // passthrough — same registry-driven approach as the metric cards'
        // own ffWireCardClick(), just triggered directly on click instead
        // of pre-wired, since popup rows are rebuilt fresh every time.
        var kind = r[2];
        var cls  = kind ? ' class="ff-popup-row ff-popup-row-click"' : ' class="ff-popup-row"';
        var act  = kind ? " onclick='ffBrowseStat(\\"" + kind + "\\",\\"" + d.start + "\\",\\"" + d.end + "\\")' title=\\"Open in Browser\\"" : "";
        return '<div' + cls + act + '><span class="ff-popup-lbl">'+r[0]+'</span>'
             + '<span class="ff-popup-val">'+r[1]+'</span></div>';
      }}).join("");
      // Show the note at the bottom, one click away, instead of requiring
      // a separate right-click just to find out a note exists.
      if(d.note){{
        var _esc = document.createElement("div"); _esc.textContent = d.note;
        rowsHtml += '<div class="ff-popup-note"><span class="ff-popup-note-icon">\U0001F4CC</span>'
                  + '<span class="ff-popup-note-text">' + _esc.innerHTML + '</span></div>';
      }}
      body.innerHTML = rowsHtml;
    }}
    popup.style.display = "block";
    popup.style.left = "0"; popup.style.top = "0";
    var vw = window.innerWidth, vh = window.innerHeight;
    var pw = popup.offsetWidth || 220, ph = popup.offsetHeight || 180;
    var left = cx + 16, top = cy - 8;
    if(left + pw > vw - 8) left = cx - pw - 12;
    if(left < 8) left = 8;
    if(top + ph > vh - 8) top = vh - ph - 8;
    if(top < 8) top = 8;
    popup.style.left = left + "px"; popup.style.top = top + "px";
    // Auto-close after N seconds (0/off = stays open until manually closed
    // or another cell is clicked). Configurable in Settings.
    if(_ffPopupTimer) {{ clearTimeout(_ffPopupTimer); _ffPopupTimer = null; }}
    if(window._ffPopupAutoCloseSecs > 0){{
      _ffPopupTimer = setTimeout(function(){{
        window._ffHidePopup();
        if(typeof window._ffDeselectCell === "function") window._ffDeselectCell();
      }}, window._ffPopupAutoCloseSecs * 1000);
    }}
  }};
  window._ffHidePopup = function(){{
    var p = document.getElementById("ff-popup");
    if(p) p.style.display = "none";
    if(_ffPopupTimer) {{ clearTimeout(_ffPopupTimer); _ffPopupTimer = null; }}
  }};
  var cl = document.getElementById("ff-popup-close");
  if(cl) cl.onclick = window._ffHidePopup;
  // Generic click-to-browse dispatch — any registered stat (see
  // window._ffClickableStats above) calls this the same way, whether it's
  // a single day (start === end) or a real range (week/month/tab).
  window.ffBrowseStat = function(kind, startISO, endISO){{
    if(typeof pycmd !== 'undefined') pycmd('ff_browse_range:' + kind + ':' + startISO + ':' + endISO);
  }};
  // Backward-compatible single-day alias, kept in case anything still
  // calls the old name directly.
  window.ffBrowseDay = function(kind, dateStr){{
    window.ffBrowseStat(kind, dateStr, dateStr);
  }};
}})();
</script>"""

    _legend_html = _LEGEND if disp_cfg.get("show_legend", True) else ""

    # User-configurable colour for the calendar's own labels (weekday
    # letters, month names, week numbers) — Settings → Heatmap → Label
    # colour. Empty means "use the theme default" (the --cal-label-color
    # fallback chain already handles that; nothing extra to inject).
    _label_color = str(hm_config.get("label_color", "") or "")
    _label_color_css = (
        f'<style>#ff-heatmap{{--cal-label-color:{_label_color};}}</style>'
        if _label_color else ""
    )

    # BUG FIX: the pulsing "today" ring's glow was a hardcoded blue
    # (rgba(90,169,255,.85)) no matter which colour scheme was active, so
    # it visibly clashed with anything that wasn't blue-ish (Rose, Violet,
    # Ember, a custom pick, ...). Derive the glow from the scheme's own
    # "hi" colour instead, so it always reads as part of the same palette.
    _accent_glow_css = f'<style>#ff-heatmap{{--accent-glow: rgba({hi[0]},{hi[1]},{hi[2]},0.85);}}</style>'

    # ── drag-to-reorder layout (heatmap / Daily status / stat cards / streak row) ──
    _customize = bool(disp_cfg.get("layout_customize_enabled", False))
    _block_content = {
        # The ◀ year ▶ control renders ABOVE the grid, by design. Getting
        # this consistent for BOTH the eagerly-rendered current year and any
        # lazily-fetched year (see render_single_year_svg() and ffNav()
        # below) took two matching pieces: nav_html has to come first here,
        # AND ffNav()'s injection point for newly-fetched svgs has to target
        # "right after nav" (nav.nextSibling), not "right before nav" —
        # otherwise a freshly-fetched year lands on the opposite side of nav
        # from the original eager one, and nav ends up above the grid for
        # some years and below it for others depending on which was fetched
        # when. Both pieces need to agree on which side of nav the svg group
        # lives; see the matching comment in ffNav() below.
        "heatmap":  nav_html + svgs_html + _legend_html,
        "progress": stats_blocks.get("progress", ""),
        "cards":    stats_blocks.get("cards", ""),
        "streak":   stats_blocks.get("streak", ""),
    }
    _block_labels = {
        "heatmap": "Heatmap", "progress": "Daily status",
        "cards": "Stat cards", "streak": "Streak row",
    }
    _default_order = ["heatmap", "progress", "cards", "streak"]
    _saved_order = hm_config.get("layout_block_order") or _default_order
    if not isinstance(_saved_order, list):
        _saved_order = _default_order
    # Defensive: drop unknown ids (stale config), then append any block ids
    # missing from a saved order (e.g. a new block added in a later version)
    # so nothing silently disappears from the page.
    _order = [b for b in _saved_order if b in _block_content]
    _order += [b for b in _default_order if b not in _order]

    _blocks_html = ""
    for _bid in _order:
        _content = _block_content.get(_bid, "")
        if not _content:
            continue
        _handle = (
            f'<div class="ff-drag-handle" title="Drag to reorder \u2014 {_block_labels[_bid]}">'
            '&#x2630;</div>' if _customize else ""
        )
        _cls = "ff-drag-block ff-drag-enabled" if _customize else "ff-drag-block"
        _blocks_html += (
            f'<div class="{_cls}" data-block-id="{_bid}">'
            f'{_handle}{_content}</div>'
        )

    _drag_css = ("""<style>
.ff-drag-enabled{outline:1px dashed transparent;border-radius:6px;transition:outline-color .15s}
.ff-drag-enabled:hover{outline-color:rgba(120,120,120,0.35)}
.ff-drag-handle{position:absolute;top:2px;right:2px;width:20px;height:20px;
  display:flex;align-items:center;justify-content:center;cursor:grab;
  border-radius:4px;font-size:13px;color:var(--text-subtle,#888);
  background:var(--canvas-subtle,rgba(0,0,0,0.06));z-index:6;user-select:none;
  touch-action:none}
.ff-drag-handle:hover{color:var(--text-fg,#222);background:var(--canvas-subtle,rgba(0,0,0,0.12))}
.ff-drag-handle:active{cursor:grabbing}
.ff-drag-block{position:relative}
.ff-drag-block.ff-dragging{opacity:0.5;z-index:10}
.ff-drag-block.ff-drag-over{outline:2px dashed var(--accent-color,#5AA9FF)}
</style>""" if _customize else "")

    # BUG FIX: this used to be native HTML5 drag-and-drop (draggable="true" +
    # dragstart/dragover/drop). It visually followed the cursor while
    # dragging but the reorder didn't stick once you let go — HTML5 DnD is
    # known to be unreliable inside embedded Chromium webviews like Anki's.
    # Rebuilt on plain pointer events instead (pointerdown/move/up), which
    # we fully control ourselves, so the drop always finalizes.
    _drag_js = ("""<script>(function(){
  var container = document.getElementById("ff-heatmap");
  if(!container) return;
  var dragEl = null;
  container.querySelectorAll('.ff-drag-handle').forEach(function(handle){
    handle.addEventListener('pointerdown', function(e){
      dragEl = handle.closest('.ff-drag-block');
      if(!dragEl) return;
      e.preventDefault();
      dragEl.classList.add('ff-dragging');
      try { handle.setPointerCapture(e.pointerId); } catch(err) {}

      function overBlock(clientY){
        var blocks = Array.prototype.slice.call(container.querySelectorAll('.ff-drag-block'));
        for(var i=0;i<blocks.length;i++){
          var b = blocks[i];
          if(b === dragEl) continue;
          var r = b.getBoundingClientRect();
          if(clientY >= r.top && clientY <= r.bottom) return b;
        }
        return null;
      }

      function onMove(ev){
        if(!dragEl) return;
        container.querySelectorAll('.ff-drag-over').forEach(function(o){ o.classList.remove('ff-drag-over'); });
        var target = overBlock(ev.clientY);
        if(target) target.classList.add('ff-drag-over');
      }

      function onUp(ev){
        try { handle.releasePointerCapture(ev.pointerId); } catch(err) {}
        document.removeEventListener('pointermove', onMove);
        document.removeEventListener('pointerup', onUp);
        if(!dragEl) return;
        dragEl.classList.remove('ff-dragging');
        container.querySelectorAll('.ff-drag-over').forEach(function(o){ o.classList.remove('ff-drag-over'); });
        var target = overBlock(ev.clientY);
        if(target){
          var r = target.getBoundingClientRect();
          var before = ev.clientY < (r.top + r.height / 2);
          container.insertBefore(dragEl, before ? target : target.nextSibling);
          var order = [];
          container.querySelectorAll('.ff-drag-block').forEach(function(b){
            order.push(b.getAttribute('data-block-id'));
          });
          if(typeof pycmd !== 'undefined') pycmd('ff_layout_order:' + order.join(','));
        }
        dragEl = null;
      }

      document.addEventListener('pointermove', onMove);
      document.addEventListener('pointerup', onUp);
    });
  });
})();</script>""" if _customize else "")

    return (
        _CSS
        + (_CSS_DARK if night_mode else "")
        + _drag_css
        + _label_color_css
        + _accent_glow_css
        + (
            '<div id="ff-heatmap" class="ff-dark" style="'
            '--text-fg:#dcdcdc;--text-subtle:#d8d8d8;--canvas:rgba(44,44,46,0.97);'
            '--border:rgba(255,255,255,0.14);--button-bg:rgba(255,255,255,0.08);'
            '--canvas-subtle:rgba(255,255,255,0.04)">'
            if night_mode else
            '<div id="ff-heatmap" style="'
            '--text-fg:#222222;--text-subtle:#3d3d3d;--canvas:#fefefe;'
            '--border:rgba(0,0,0,0.15);--button-bg:rgba(40,40,40,0.08);'
            '--canvas-subtle:rgba(0,0,0,0.025)">'
        )
        + _blocks_html
        + _DETAIL_PANEL
        + '<div id="ff-popup"><div id="ff-popup-header">'
          '<span id="ff-popup-title"></span>'
          '<button id="ff-popup-close">&#x2715;</button></div>'
          '<div id="ff-popup-body"></div></div>'
        + '</div>'
        + _js_grid_vars
        + _JS_INTERACT
        + _popup_js
        + js_colormode
        + js_nav
        + stats_blocks.get("css", "")
        + stats_blocks.get("js", "")
        + _drag_js
    )
