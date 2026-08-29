from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StudySession:
    start_time: int
    end_time: int
    duration: int
    cards_done: int
    effective_score: float


@dataclass(frozen=True)
class ProductivitySummary:
    study_minutes: float
    effective_minutes: float
    average_quality: float
    fatigue_adjusted_productivity: float
    cards_done: int
    sessions: int


def clamp_quality(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def effective_minutes(duration_seconds: int, effective_score: float) -> float:
    return max(0.0, duration_seconds / 60.0 * clamp_quality(effective_score))


def summarize_sessions(sessions: list[StudySession]) -> ProductivitySummary:
    total_seconds = sum(s.duration for s in sessions)
    total_minutes = total_seconds / 60.0
    eff = sum(effective_minutes(s.duration, s.effective_score) for s in sessions)
    quality = eff / total_minutes if total_minutes else 0.0
    cards = sum(s.cards_done for s in sessions)
    productivity = eff / max(1.0, len(sessions))
    return ProductivitySummary(
        study_minutes=total_minutes,
        effective_minutes=eff,
        average_quality=quality,
        fatigue_adjusted_productivity=productivity,
        cards_done=cards,
        sessions=len(sessions),
    )
