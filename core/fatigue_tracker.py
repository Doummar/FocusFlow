from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _Answer:
    """Internal record of one answered card."""
    rt_raw:    float   # raw answer time, seconds
    rt_norm:   float   # difficulty-normalised answer time
    ease:      int     # 1=Again 2=Hard 3=Good 4=Easy
    card_type: int     # 0=new 1=learn 2=review 3=relearn
    interval:  int     # card interval in days (0 for new/learning)
    cid:       int = 0     # card id -- 0 for any pre-existing answer that
                            # predates this field (defensive default only;
                            # every _Answer constructed by this module now
                            # always supplies a real cid)
    is_repeat: bool = False  # True if this cid already appeared earlier in
                              # the retained answer history (see
                              # record_answer() -- checked against the
                              # bounded _answers deque, which already
                              # persists across a completed break via
                              # on_break_taken()'s retention, so repeat
                              # recognition survives a break for free with
                              # no new session-boundary wiring needed)


@dataclass(frozen=True)
class FatigueSnapshot:
    score:                float
    state:                str
    answer_time_deviation: float   # normalised RT drift (recent/baseline − 1)
    again_rate:           float   # weighted again rate, recent window
    streak_consistency:   float   # IIV component score (1=consistent, 0=erratic)
    ease_distribution:    float   # lapse component score
    should_suggest_break: bool


class FatigueTracker:
    """
    Four-signal, card-type-aware cognitive fatigue detector.

    ── Signals ────────────────────────────────────────────────────────────────

    1. RT Mean Drift  (25 %)
       Has the difficulty-normalised median answer time risen vs the session
       baseline?

    2. RT Intra-Individual Variability Drift  (30 %)
       Has the standard deviation of answer times grown vs the baseline?
       IIV increases *before* mean RT changes and is the most sensitive
       pure-behavioural marker of cognitive fatigue.

    3. Weighted Again-Rate Drift  (30 %)
       Has the "Again" rate risen vs baseline?  Card types are weighted:
         new / learning  → weight 0.0
         relearning      → weight 0.5
         review (mature) → weight 1.0

    4. Lapse Rate  (15 %)
       A "lapse" is a single response much slower than the user's own session
       baseline (> baseline_median + 2.5 × baseline_SD).  Only review cards
       are counted.

    ── Sensitivity slider ─────────────────────────────────────────────────────

    Slider 1–20 maps to s=0.0–1.0.  The thresholds use a much wider range
    than the previous version so that sensitivity=1 (s=0) requires extreme
    signal drift before anything fires — normal session variation cannot
    trigger a break suggestion at the lowest setting.

    At s=0 (slider=1):
        RT needs a 200 % increase, IIV needs 150 %, again-rate needs a 60 pp
        rise, and 50 % of recent cards must be lapses before full penalties
        fire.  The absolute again-rate ceiling and the IIV ease-compression
        boost are both multiplied by s, so they contribute nothing at minimum
        sensitivity.

    At s=1 (slider=20):
        RT fires at 10 %, IIV at 10 %, again-rate at 5 pp, lapse at 7 % —
        matching the original high-end behaviour.

    ── Per-signal toggles ─────────────────────────────────────────────────────

    Individual signals can be disabled via fatigue.signals.{rt,iiv,again,lapse}.
    A disabled signal contributes its full weight at score=1.0 (no penalty) so
    the remaining weights stay proportional without re-normalisation.

    ── Configurable thresholds ────────────────────────────────────────────────

    fatigue.min_cards   (5–30, default 10)  — minimum answered cards before
        scoring starts.  Prevents a handful of early Again presses on new cards
        from producing a spurious break suggestion at session start.

    fatigue.break_score (0.20–0.60, default 0.35)  — score below which a break
        suggestion is emitted.  The state thresholds used for display and the
        adaptive timer are kept fixed (0.35 / 0.60); this setting only controls
        *when* the soft break popup appears.

    ── Break recovery ─────────────────────────────────────────────────────────

    on_break_taken() retains the most recent BASELINE_N answers and blends their
    RT toward the session median before the fatigue set in, preventing the
    post-break baseline from being contaminated by pre-break fatigue.
    """

    BASELINE_N = 10   # session-start "fresh" window
    RECENT_N   = 8    # "right now" window
    MIN_CARDS  = 10   # kept for backward-compat / tests; runtime uses _min_cards()

    # Number of CONSECUTIVE answered cards that must all report
    # should_suggest_break=True before should_break_now() agrees -- i.e.
    # before the break-suggestion popup is allowed to fire. This does NOT
    # change the meaning of FatigueSnapshot.should_suggest_break itself
    # (still an instantaneous, per-card fact used live by the HUD indicator
    # and apply_fatigue_to_timer()'s adaptive-duration multiplier); it only
    # gates the separate, more conservative decision of whether to actually
    # interrupt the user. See should_break_now() below. Internal constant,
    # not user-configurable, by design (see investigation report).
    _PERSISTENCE_N = 3

    _W_RT    = 0.25   # mean RT drift
    _W_IIV   = 0.30   # variability drift
    _W_AR    = 0.30   # weighted again-rate drift
    _W_LAPSE = 0.15   # lapse rate

    _AGAIN_WEIGHT: dict[int, float] = {0: 0.0, 1: 0.10, 2: 1.0, 3: 0.50}

    # Weight multiplier applied ONLY to a repeated-AND-correct answer's
    # contribution to the correctness/quality signals below (see
    # _weighted_again_rate() and _answer_weight_signal()). A repeated card
    # answered correctly is genuinely easier from familiarity, not from
    # improved fatigue/focus -- this reduces (but does not zero out) its
    # influence so it can't be misread as fresh evidence of recovery. A
    # repeated-and-WRONG answer is explicitly NOT touched by this factor --
    # struggling again despite familiarity is still meaningful evidence.
    # Initial value only, not a derived/proven-optimal constant -- kept as
    # its own named constant specifically so it's easy to retune later
    # without touching the surrounding formulas.
    _REPEAT_CORRECT_WEIGHT = 0.5

    def __init__(self, config: dict) -> None:
        self._config = config
        self._card_started_at: dict[int, tuple[float, int, int]] = {}
        self._answers: deque[_Answer] = deque(maxlen=60)

        # Pause tracking: when the Pomodoro timer is paused we must exclude
        # the idle wall-clock time from RT, otherwise every card answered
        # after a pause appears massively slow and fires spurious fatigue alerts.
        self._paused_at:          float | None = None  # monotonic of current pause start
        self._card_pause_secs:    float        = 0.0   # paused secs accumulated for current card = deque(maxlen=60)
        self.current_score = 1.0
        self.current_state = "focused"
        # Consecutive-card counter backing should_break_now() -- incremented
        # each time record_answer() reports should_suggest_break=True,
        # reset to 0 the instant a card reports False. See _PERSISTENCE_N.
        self._consecutive_low = 0

    def update_config(self, config: dict) -> None:
        self._config = config

    # ── public API ────────────────────────────────────────────────────────────

    # ── timer pause/resume hooks ──────────────────────────────────────────────

    def on_timer_paused(self) -> None:
        """Called when the Pomodoro timer is paused.

        Records the wall-clock instant so that time spent paused is excluded
        from the next card's response-time measurement.
        """
        if self._paused_at is None:
            self._paused_at = time.monotonic()

    def on_timer_resumed(self) -> None:
        """Called when the Pomodoro timer resumes after a pause.

        Accumulates the pause duration into ``_card_pause_secs`` so that
        ``record_answer`` can subtract it from the raw RT.
        """
        if self._paused_at is not None:
            self._card_pause_secs += time.monotonic() - self._paused_at
            self._paused_at = None

    def start_card(self, card: Any) -> None:
        """Record when a card was shown (used to measure answer time)."""
        cid = int(getattr(card, "id", 0) or 0)
        if not cid:
            return
        ctype = int(getattr(card, "type", 2) or 2)
        civl  = max(0, int(getattr(card, "ivl",  0) or 0))
        # Reset the per-card pause accumulator for every new card so pauses
        # from a previous card never leak into this one.
        self._card_pause_secs = 0.0
        if self._paused_at is not None:
            # Timer is currently paused while a new card appeared (unlikely
            # but possible). Start the pause accumulator from now.
            self._paused_at = time.monotonic()
        self._card_started_at[cid] = (time.monotonic(), ctype, civl)

    def record_answer(self, card: Any, ease: int) -> FatigueSnapshot:
        """Record a card answer and return the updated fatigue snapshot."""
        cid   = int(getattr(card, "id", 0) or 0)
        entry = self._card_started_at.pop(cid, None)

        if entry:
            started, ctype, civl = entry
        else:
            ctype   = int(getattr(card, "type", 2) or 2)
            civl    = max(0, int(getattr(card, "ivl", 0) or 0))
            started = None

        # Subtract any time the timer was paused while this card was shown.
        # If the timer is still paused right now, add the in-progress pause
        # duration too so the RT is accurate up to this exact moment.
        _pause_adj = self._card_pause_secs
        if self._paused_at is not None:
            _pause_adj += time.monotonic() - self._paused_at
        rt_raw  = max(0.25, time.monotonic() - started - _pause_adj) if started else 5.0
        rt_norm = rt_raw / self._difficulty_factor(ctype, civl)

        # A repeat is any cid already present in the retained answer
        # history -- checked BEFORE appending this new answer, so a card
        # never counts as a repeat of itself. self._answers already spans
        # a completed break (on_break_taken() retains the tail rather than
        # clearing it), so this recognizes "wrong before break, correct
        # after" without any new session-boundary logic. A fresh
        # FatigueTracker (new Anki profile session) naturally starts with
        # an empty deque, and cids old enough to fall outside the bounded
        # deque naturally stop being recognized as repeats -- both are
        # existing, already-relied-upon behaviors of this deque, not new
        # mechanisms introduced here.
        is_repeat = any(prior.cid == cid for prior in self._answers)

        self._answers.append(_Answer(
            rt_raw    = rt_raw,
            rt_norm   = rt_norm,
            ease      = int(ease),
            card_type = ctype,
            interval  = civl,
            cid       = cid,
            is_repeat = is_repeat,
        ))
        snap = self._compute()
        self.current_score = snap.score
        self.current_state = snap.state
        # Persistence tracking for should_break_now() -- see _PERSISTENCE_N.
        # Deliberately keyed off this card's own instantaneous
        # should_suggest_break, not off should_break_now() itself, so this
        # is a plain consecutive-run counter rather than a
        # read-modify-write against its own gated output.
        if snap.should_suggest_break:
            self._consecutive_low += 1
        else:
            self._consecutive_low = 0
        return snap

    def editor_opened(self) -> None:
        """Discard in-progress timing when the editor is opened."""
        self._card_started_at.clear()

    def on_break_taken(self) -> None:
        """Soft-reset after a break: blend pre-break RTs toward session median.

        Retains the most recent BASELINE_N answers but nudges their RT 40 %
        back toward the global session median, removing most fatigue bias while
        preserving individual differences.  Score resets to 1.0 (focused).
        """
        self._card_started_at.clear()
        recent = list(self._answers)[-self.BASELINE_N:]
        if recent:
            session_median = statistics.median(a.rt_norm for a in self._answers)
            _BLEND = 0.40
            blended = [
                _Answer(
                    rt_raw    = a.rt_raw,
                    rt_norm   = a.rt_norm + _BLEND * (session_median - a.rt_norm),
                    ease      = a.ease,
                    card_type = a.card_type,
                    interval  = a.interval,
                    cid       = a.cid,
                    is_repeat = a.is_repeat,
                )
                for a in recent
            ]
        else:
            blended = []
        self._answers.clear()
        for ans in blended:
            self._answers.append(ans)
        self.current_score = 1.0
        self.current_state = "focused"
        self._consecutive_low = 0

    def session_again_rate(self) -> float:
        """Overall review-card again rate for the current session.

        Public API — used by the settings dialog and the post-session popup.
        Only mature review cards (type 2) are counted.
        """
        review = [a for a in self._answers if a.card_type == 2]
        if not review:
            return 0.0
        return sum(1 for a in review if a.ease == 1) / len(review)

    def reset(self) -> None:
        self.on_break_taken()

    def current_snapshot(self) -> FatigueSnapshot:
        return self._compute()

    def should_break_now(self) -> bool:
        """Persistence-gated decision for the break-suggestion popup only.

        True once should_suggest_break has held for _PERSISTENCE_N
        consecutive answered cards. FatigueSnapshot.should_suggest_break
        itself is unaffected by this -- it remains the live, instantaneous
        per-card fact that the HUD fatigue indicator and
        apply_fatigue_to_timer()'s adaptive-duration multiplier correctly
        want. This method exists specifically so a single noisy card (or a
        brief, non-sustained rough patch -- see the false-positive
        investigation) cannot by itself interrupt the user with a popup;
        only a deterioration that persists across several consecutive
        cards can.
        """
        return self._consecutive_low >= self._PERSISTENCE_N

    # ── config helpers ────────────────────────────────────────────────────────

    def _sensitivity(self) -> float:
        """Map sensitivity slider 1–20 → 0.0–1.0."""
        raw = float(self._config.get("fatigue", {}).get("sensitivity", 10))
        return max(0.0, min(1.0, (raw - 1.0) / 19.0))

    def _min_cards(self) -> int:
        """Minimum answered cards before scoring begins (5–30, default 10).

        A higher value prevents a few early Again presses on new cards from
        generating a premature break suggestion at the start of a session.
        """
        return max(5, min(30, int(self._config.get("fatigue", {}).get("min_cards", 10))))

    def _break_score(self) -> float:
        """Score below which a break suggestion is emitted (0.20–0.60, default 0.35).

        Kept separate from the display-state thresholds (0.35 / 0.60) so users
        can adjust when the soft-break popup appears without affecting the
        adaptive timer or the fatigue indicator colour.
        """
        return max(0.20, min(0.60, float(
            self._config.get("fatigue", {}).get("break_score", 0.35)
        )))

    def _signals_enabled(self) -> dict[str, bool]:
        """Which of the four signals are active.

        A disabled signal contributes its full weight at 1.0 (no penalty),
        keeping the score floor proportional without re-normalising.
        """
        sigs = self._config.get("fatigue", {}).get("signals", {})
        return {
            "rt":    bool(sigs.get("rt",    True)),
            "iiv":   bool(sigs.get("iiv",   True)),
            "again": bool(sigs.get("again", True)),
            "lapse": bool(sigs.get("lapse", True)),
        }

    # ── threshold methods (sensitivity-controlled) ────────────────────────────

    def _rt_threshold(self) -> float:
        """RT increase fraction that earns a full RT penalty.

        Dramatically widened range vs previous version so sensitivity=1
        requires a 200 % RT increase — impossible to hit under normal variation.

        Old range: 0.80 → 0.10  (sensitivity 1 → 20)
        New range: 2.00 → 0.10
        """
        return 2.0 - 1.90 * self._sensitivity()

    def _iiv_threshold(self) -> float:
        """SD increase fraction that earns a full IIV penalty.

        Old range: 0.70 → 0.10
        New range: 1.50 → 0.10
        """
        return 1.50 - 1.40 * self._sensitivity()

    def _ar_threshold(self) -> float:
        """Weighted again-rate rise (pp) that earns a full Again penalty.

        Old range: 0.40 → 0.05
        New range: 0.60 → 0.05
        """
        return 0.60 - 0.55 * self._sensitivity()

    def _lapse_threshold(self) -> float:
        """Fraction of review lapses before full penalty.

        Old range: 0.35 → 0.07
        New range: 0.50 → 0.07
        """
        return 0.50 - 0.43 * self._sensitivity()

    # ── static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _difficulty_factor(card_type: int, ivl: int) -> float:
        """Difficulty factor for normalising raw RT.

        new / learning cards → 1.50 (first exposure, long RT expected)
        review by interval:
            ≤  3 d  → 1.35   recently failed, still shaky
            ≤ 14 d  → 1.15   short-term memory
            ≤ 60 d  → 1.00   medium-term reference
            ≤180 d  → 0.90   well established
             >180 d → 0.80   very mature, near-instant recall
        """
        if card_type in (0, 1):
            return 1.50
        if ivl <= 0:
            return 1.50
        if ivl <= 3:
            return 1.35
        if ivl <= 14:
            return 1.15
        if ivl <= 60:
            return 1.00
        if ivl <= 180:
            return 0.90
        return 0.80

    @staticmethod
    def _weighted_again_rate(window: list[_Answer]) -> float:
        """Card-type-weighted again rate for a window.

        Quick Again presses on review cards (< 1.5 s) get an extra 1.6×
        boost — clicking through without reading = probable disengagement.

        A repeated card (same cid seen earlier in the retained history)
        that was answered CORRECTLY contributes at _REPEAT_CORRECT_WEIGHT
        instead of full weight — see that constant's docstring. A repeated
        card answered WRONG (ease == 1) is deliberately excluded from this
        reduction and contributes exactly as a first-time wrong answer
        would.
        """
        total_w = again_w = 0.0
        for a in window:
            w = FatigueTracker._AGAIN_WEIGHT.get(a.card_type, 1.0)
            if a.is_repeat and a.ease != 1:
                w *= FatigueTracker._REPEAT_CORRECT_WEIGHT
            total_w += w
            if a.ease == 1:
                boost = 1.6 if (a.card_type == 2 and a.rt_raw < 1.5) else 1.0
                again_w += w * boost
        return again_w / total_w if total_w >= 0.5 else 0.0

    @staticmethod
    def _ec_rate(window: list[_Answer]) -> float:
        """(Again + Hard) rate on review cards only."""
        rev = [a for a in window if a.card_type == 2]
        if not rev:
            return 0.0
        return sum(1 for a in rev if a.ease <= 2) / len(rev)

    # ── Aura-inspired answer-weight signal ───────────────────────────────────

    def _answer_weight_signal(
        self, base: list[_Answer], rec: list[_Answer], s: float
    ) -> tuple[float, float]:
        """Quality-drift signal using configurable per-button answer weights.

        Reads the same ``measurement.ans_weights`` the user configured in the
        Measurement settings tab, so the heatmap quality colours and the live
        fatigue score are driven by a *unified* measurement policy.

        Aura's insight: tracking only Again misses the Hard signal.  A user
        grinding Hard on cards they used to get Good is already accumulating
        cognitive load.  Weighting all four buttons gives an earlier, richer
        signal than an again-only rate.

        Returns (ar_score: float 0-1, rec_again_rate: float).
        """
        meas  = self._config.get("measurement", {})
        ww    = meas.get("ans_weights", {})
        wmap  = {
            1: max(-5.0, min(5.0, float(ww.get("again", -2)))),
            2: max(-5.0, min(5.0, float(ww.get("hard",  -1)))),
            3: max(-5.0, min(5.0, float(ww.get("good",   1)))),
            4: max(-5.0, min(5.0, float(ww.get("easy",   2)))),
        }
        w_min = min(wmap.values())
        w_max = max(wmap.values())
        w_rng = max(w_max - w_min, 1e-9)

        def _quality(window: list[_Answer]) -> float:
            """Normalised quality score (0.0 = all Again, 1.0 = all Easy).

            A repeated-and-correct answer contributes at
            _REPEAT_CORRECT_WEIGHT instead of a full 1.0 "vote" -- reduced
            influence on the aggregate, not a different value in
            isolation (a window of only repeated-correct answers still
            correctly averages to the same quality as if unweighted).
            Repeated-and-wrong answers are unaffected, same rationale as
            _weighted_again_rate() above.
            """
            if not window:
                return 0.5
            weighted_sum = 0.0
            weight_total = 0.0
            for a in window:
                wt = FatigueTracker._REPEAT_CORRECT_WEIGHT if (a.is_repeat and a.ease != 1) else 1.0
                weighted_sum += wmap.get(a.ease, 0.0) * wt
                weight_total += wt
            if weight_total <= 0:
                return 0.5
            avg_w = weighted_sum / weight_total
            return max(0.0, min(1.0, (avg_w - w_min) / w_rng))

        base_q = _quality(base)
        rec_q  = _quality(rec)

        # Drift: how much has answer quality dropped from the early-session
        # baseline?  The same ar_threshold controls the scale as in the
        # default mode so sensitivity settings apply consistently.
        drift   = max(0.0, base_q - rec_q)
        penalty = min(1.0, drift / max(self._ar_threshold(), 0.01))

        # Absolute floor: fire even when baseline was already low, scaled by
        # sensitivity (contributes nothing at minimum sensitivity).
        abs_floor = max(0.0, 0.25 - rec_q) * 1.5 * s
        penalty   = min(1.0, penalty + abs_floor)

        # Keep the snapshot again_rate using the original weighted formula
        # for backwards-compatible display (overlay colour, settings dialog).
        real_ar = self._weighted_again_rate(rec)
        return 1.0 - penalty, real_ar

    # ── core compute ──────────────────────────────────────────────────────────

    def _compute(self) -> FatigueSnapshot:
        answers = list(self._answers)
        n       = len(answers)

        if n < self._min_cards():
            return FatigueSnapshot(1.0, "focused", 0.0, 0.0, 1.0, 1.0, False)

        # ── Read measurement config (shared with heatmap quality source) ───────
        meas        = self._config.get("measurement", {})
        use_aw      = meas.get("quality_source", "focus") == "answers"
        ignore_easy = bool(meas.get("ignore_easy_time", False))

        # ── Baseline & recent windows ─────────────────────────────────────────
        # Adaptive baseline: scale from BASELINE_N up to 2×BASELINE_N as the
        # session grows, but cap it so baseline and recent windows never overlap.
        raw_base_n = min(max(self.BASELINE_N, n // 4), self.BASELINE_N * 2, n // 2)
        base_n = min(raw_base_n, max(1, n - self.RECENT_N))
        base   = answers[:base_n]
        rec    = answers[-min(self.RECENT_N, n):]

        base_rts = [a.rt_norm for a in base]
        rec_rts  = [a.rt_norm for a in rec]

        base_med = statistics.median(base_rts)
        rec_med  = statistics.median(rec_rts)
        base_sd  = max(statistics.pstdev(base_rts) if len(base_rts) >= 2 else 0.0,
                       base_med * 0.10)

        s = self._sensitivity()   # 0.0 at slider=1, 1.0 at slider=20

        # ── Signal 1: RT Mean Drift ───────────────────────────────────────────
        rt_drift   = max(0.0, rec_med / max(base_med, 0.5) - 1.0)
        rt_penalty = min(1.0, rt_drift / max(self._rt_threshold(), 0.01))
        rt_score   = 1.0 - rt_penalty

        # ── Signal 2: RT Intra-Individual Variability Drift ───────────────────
        # When measurement.ignore_easy_time is True (mirrors Aura's setting),
        # Easy cards are excluded from IIV.  Instant Easy presses on well-known
        # cards pull variance DOWN, masking genuine fatigue-driven variability.
        if ignore_easy:
            _iiv_base = [a.rt_norm for a in base if a.ease != 4]
            _iiv_rec  = [a.rt_norm for a in rec  if a.ease != 4]
            if len(_iiv_base) >= 2 and len(_iiv_rec) >= 2:
                _iiv_base_med = statistics.median(_iiv_base)
                _iiv_base_sd  = max(
                    statistics.pstdev(_iiv_base), _iiv_base_med * 0.10)
                rec_sd    = statistics.pstdev(_iiv_rec)
                iiv_drift = max(0.0, rec_sd / max(_iiv_base_sd, 0.5) - 1.0)
            else:  # not enough non-Easy cards — fall back to all cards
                rec_sd    = statistics.pstdev(rec_rts) if len(rec_rts) >= 2 else 0.0
                iiv_drift = max(0.0, rec_sd / max(base_sd, 0.5) - 1.0)
        else:
            rec_sd    = statistics.pstdev(rec_rts) if len(rec_rts) >= 2 else 0.0
            iiv_drift = max(0.0, rec_sd / max(base_sd, 0.5) - 1.0)

        # Ease-compression boost scaled by sensitivity so it contributes
        # nothing at minimum sensitivity and up to 0.25 at maximum.
        ec_drift      = max(0.0, self._ec_rate(rec) - self._ec_rate(base))
        iiv_drift_eff = iiv_drift + ec_drift * 0.60 * s

        iiv_penalty = min(1.0, iiv_drift_eff / max(self._iiv_threshold(), 0.01))
        iiv_score   = 1.0 - iiv_penalty

        # ── Signal 3: Quality Drift ───────────────────────────────────────────
        # Two modes selected by measurement.quality_source:
        #
        #  "answers" — Aura-style configurable answer weights (Again/Hard/Good/Easy).
        #     The same weights configured in Settings → Measurement now also drive
        #     the live fatigue score, creating a unified measurement across the
        #     heatmap quality display and the real-time focus indicator.
        #     Hard presses on cards you usually nail are caught early, before
        #     an again-only rate would fire.
        #
        #  "focus" (default) — original weighted again-rate drift; behaviour
        #     is identical to all previous FocusFlow versions.
        if use_aw:
            ar_score, rec_ar = self._answer_weight_signal(base, rec, s)
        else:
            base_ar  = self._weighted_again_rate(base)
            rec_ar   = self._weighted_again_rate(rec)
            ar_drift = max(0.0, rec_ar - base_ar)
            ar_abs     = max(0.0, rec_ar - 0.65) * 1.5 * s
            ar_penalty = min(1.0, ar_drift / max(self._ar_threshold(), 0.01) + ar_abs)
            ar_score   = 1.0 - ar_penalty

        # ── Signal 4: Lapse Rate ──────────────────────────────────────────────
        lapse_boundary = base_med + 2.5 * base_sd
        review_rec     = [a for a in rec if a.card_type == 2]
        if review_rec:
            lapse_rate = sum(1 for a in review_rec
                             if a.rt_norm > lapse_boundary) / len(review_rec)
        else:
            lapse_rate = 0.0
        lapse_penalty = min(1.0, lapse_rate / max(self._lapse_threshold(), 0.01))
        lapse_score   = 1.0 - lapse_penalty

        # ── Combined score with per-signal masking ────────────────────────────
        # A disabled signal contributes its full weight at 1.0 (no penalty),
        # preserving the proportional contribution of the active signals.
        sigs = self._signals_enabled()
        score = max(0.0, min(1.0,
            self._W_RT    * (rt_score    if sigs["rt"]    else 1.0)
            + self._W_IIV   * (iiv_score   if sigs["iiv"]   else 1.0)
            + self._W_AR    * (ar_score    if sigs["again"] else 1.0)
            + self._W_LAPSE * (lapse_score if sigs["lapse"] else 1.0)
        ))

        # ── State thresholds ──────────────────────────────────────────────────
        # Fixed at 0.35 / 0.60 for display and adaptive-timer purposes.
        # The break *suggestion* uses the separate, configurable break_score.
        if score < 0.35:
            state = "low_quality"
        elif score < 0.60:
            state = "drifting"
        else:
            state = "focused"

        return FatigueSnapshot(
            score                = score,
            state                = state,
            answer_time_deviation= rt_drift,
            again_rate           = rec_ar,
            streak_consistency   = iiv_score,
            ease_distribution    = lapse_score,
            should_suggest_break = (score < self._break_score()),
        )
