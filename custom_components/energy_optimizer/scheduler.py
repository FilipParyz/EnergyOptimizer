"""Deterministic scheduling logic. This module deliberately contains no
machine learning -- it's the heuristic that works from day one with zero
training data. ml.py sits on top of this and may nudge its output once
there's enough labeled history, never replace it outright.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import homeassistant.util.dt as dt_util

from .const import RECENCY_HALF_LIFE_DAYS, LOOKAHEAD_IMPROVEMENT_THRESHOLD, CAPACITY_SLACK


def _parse_hour(value: str) -> int:
    return int(str(value).split(":")[0])


def allowed_hours(allowed_start: str, allowed_end: str, quiet_start: str, quiet_end: str) -> list[int]:
    start_h = _parse_hour(allowed_start)
    end_h = _parse_hour(allowed_end)
    q_start = _parse_hour(quiet_start)
    q_end = _parse_hour(quiet_end)

    def in_range(h: int, a: int, b: int) -> bool:
        if a <= b:
            return a <= h < b
        return h >= a or h < b  # wraps past midnight, e.g. 22 -> 6

    return [h for h in range(24) if in_range(h, start_h, end_h) and not in_range(h, q_start, q_end)]


def habit_scores(runs, target_date: date, hours: list[int]) -> dict[int, float]:
    target_dow = target_date.weekday()
    now = dt_util.now()
    weighted = {h: 0.0 for h in hours}
    total_weight = 0.0

    for run in runs:
        start = dt_util.parse_datetime(run.start_time)
        if start is None:
            continue
        age_days = max((now - start).total_seconds() / 86400, 0)
        recency = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
        dow_match = 1.0 if start.weekday() == target_dow else 0.35
        weight = recency * dow_match
        if start.hour in weighted:
            weighted[start.hour] += weight
        total_weight += weight

    if total_weight <= 0:
        return {h: 1.0 / len(hours) for h in hours} if hours else {}
    return {h: v / total_weight for h, v in weighted.items()}


@dataclass
class UrgencyInfo:
    urgency: float
    typical_interval_days: float
    days_since_last: float | None


def compute_urgency(runs, today: date) -> UrgencyInfo:
    if not runs:
        return UrgencyInfo(urgency=0.5, typical_interval_days=7.0, days_since_last=None)

    parsed = [dt_util.parse_datetime(r.start_time) for r in runs]
    dates = sorted({d.date() for d in parsed if d is not None})

    if len(dates) < 2:
        return UrgencyInfo(urgency=0.5, typical_interval_days=7.0, days_since_last=(today - dates[-1]).days)

    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
    if not gaps:
        return UrgencyInfo(urgency=0.5, typical_interval_days=7.0, days_since_last=(today - dates[-1]).days)

    weights = [0.5 ** ((len(gaps) - 1 - i) / 5) for i in range(len(gaps))]
    typical = sum(g * w for g, w in zip(gaps, weights)) / sum(weights)
    days_since_last = (today - dates[-1]).days
    urgency = min(1.2, days_since_last / typical) if typical > 0 else 0.5
    return UrgencyInfo(urgency=urgency, typical_interval_days=typical, days_since_last=days_since_last)


def solar_alignment_scores(forecast_watts: dict[int, float], duration_hours: float,
                             hours: list[int]) -> dict[int, float]:
    if not forecast_watts or sum(forecast_watts.values()) <= 0:
        return {h: 0.0 for h in hours}

    span = max(1, round(duration_hours))
    raw = {}
    for h in hours:
        window = [forecast_watts.get((h + i) % 24, 0.0) for i in range(span)]
        raw[h] = sum(window) / span
    peak = max(raw.values()) or 1.0
    return {h: v / peak for h, v in raw.items()}


def expected_solar_coverage_pct(forecast_watts: dict[int, float], hour: int,
                                  duration_hours: float, avg_power_w: float) -> float:
    if avg_power_w <= 0 or duration_hours <= 0:
        return 0.0
    span = max(1, round(duration_hours))
    available_wh = sum(forecast_watts.get((hour + i) % 24, 0.0) for i in range(span))
    needed_wh = avg_power_w * duration_hours
    if needed_wh <= 0:
        return 0.0
    return max(0.0, min(100.0, available_wh / needed_wh * 100))


def combine_scores(habit: dict[int, float], solar: dict[int, float], pref_weight: float) -> dict[int, float]:
    return {h: pref_weight * habit.get(h, 0.0) + (1 - pref_weight) * solar.get(h, 0.0) for h in habit}


def best_hour(scores: dict[int, float]) -> tuple[int, float]:
    return max(scores.items(), key=lambda kv: kv[1])


def choose_target_day(day_best: dict[int, tuple[int, float]], urgency_info: UrgencyInfo) -> tuple[int, str]:
    if 0 not in day_best:
        return 0, "no forecast available; defaulting to tomorrow"

    base_offset = min(day_best)
    base_hour, base_score = day_best[base_offset]
    best_offset, best_h, best_score = base_offset, base_hour, base_score

    for offset, (h, s) in day_best.items():
        if offset == base_offset:
            continue
        projected_wait = (urgency_info.days_since_last or 0) + offset
        if projected_wait > urgency_info.typical_interval_days * 1.3:
            continue  # would push this appliance too far past its usual interval
        if s > best_score * (1 + LOOKAHEAD_IMPROVEMENT_THRESHOLD):
            best_offset, best_h, best_score = offset, h, s

    if best_offset == base_offset:
        return base_offset, f"Best fit for tomorrow around {base_hour:02d}:00."
    return best_offset, (
        f"Waiting {best_offset - base_offset} day(s) for meaningfully better solar coverage."
        f"Still within your typical usage interval."
    )


@dataclass
class ApplianceCandidate:
    entry_id: str
    name: str
    avg_power_w: float
    duration_hours: float
    urgency: float
    typical_interval_days: float
    day_scores: dict[int, dict[int, float]]  # here, always keyed {0: scores_for_its_chosen_day}


def joint_schedule(candidates: list[ApplianceCandidate], forecast_watts: dict[int, float],
                     capacity_slack: float = CAPACITY_SLACK) -> dict[str, tuple[int, float]]:
    remaining = {h: w * capacity_slack for h, w in forecast_watts.items()}
    ordered = sorted(candidates, key=lambda c: c.urgency, reverse=True)
    assignment: dict[str, tuple[int, float]] = {}

    for cand in ordered:
        scores = cand.day_scores.get(0, {})
        if not scores:
            continue
        span = max(1, round(cand.duration_hours))
        best_h, best_adj = None, float("-inf")
        for h, s in scores.items():
            window = [(h + i) % 24 for i in range(span)]
            overflow = sum(max(0.0, cand.avg_power_w - remaining.get(w, 0.0)) for w in window)
            penalty = overflow / max(cand.avg_power_w, 1.0) * 0.5
            adj = s - penalty
            if adj > best_adj:
                best_adj, best_h = adj, h
        if best_h is not None:
            assignment[cand.entry_id] = (best_h, best_adj)
            for i in range(span):
                w = (best_h + i) % 24
                remaining[w] = max(0.0, remaining.get(w, 0.0) - cand.avg_power_w)

    return assignment
