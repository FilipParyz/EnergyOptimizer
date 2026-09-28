"""Offline backtest: replays historical appliance runs against the same
scheduling logic used in production, without requiring further live user
interaction. Produces two evaluation angles:
  1. behavioural alignment -- how often the recommendation the algorithm
     WOULD have made lines up with what the resident actually did
  2. solar-efficiency regret -- how much solar coverage was left on the
     table versus the best hour available in hindsight, using the day's
     ACTUAL PV production as a stand-in for the forecast that would have
     been available (see fetch_actual_solar_production below: this is
     optimistic, since a real forecast is never perfectly accurate, and
     forecasts aren't archived by this integration).
Intended to be run manually/offline (see __init__.py's run_backtest
service), not as part of the live daily scheduling job.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import list_statistic_ids, statistics_during_period
import homeassistant.util.dt as dt_util

from . import scheduler
from .backfill import _to_local_dt
from .store import RunRecord


@dataclass
class BacktestResult:
    eval_date: date
    recommended_hour: int
    actual_hour: int
    matched: bool
    recommended_coverage_pct: float
    best_possible_coverage_pct: float
    regret_pct: float


def backtest_appliance(
    runs: list[RunRecord],
    actual_solar_by_date: dict[date, dict[int, float]],
    duration_hours: float,
    avg_power_w: float,
    hours: list[int],
    pref_weight: float,
    min_history_days: int = 14,
) -> list[BacktestResult]:
    parsed = [(r, dt_util.parse_datetime(r.start_time)) for r in runs]
    parsed = [(r, dt) for r, dt in parsed if dt is not None]
    all_dates = sorted({dt.date() for _, dt in parsed})

    if len(all_dates) <= min_history_days:
        return []

    results: list[BacktestResult] = []
    for eval_date in all_dates[min_history_days:]:
        actual_dt = next((dt for _, dt in parsed if dt.date() == eval_date), None)
        if actual_dt is None or eval_date not in actual_solar_by_date:
            continue

        history_so_far = [r for r, dt in parsed if dt.date() < eval_date]
        solar_today = actual_solar_by_date[eval_date]

        habit = scheduler.habit_scores(history_so_far, eval_date, hours)
        solar = scheduler.solar_alignment_scores(solar_today, duration_hours, hours)
        combined = scheduler.combine_scores(habit, solar, pref_weight)
        rec_hour, _ = scheduler.best_hour(combined)

        rec_coverage = scheduler.expected_solar_coverage_pct(solar_today, rec_hour, duration_hours, avg_power_w)
        best_coverage = max(
            scheduler.expected_solar_coverage_pct(solar_today, h, duration_hours, avg_power_w) for h in hours
        )

        results.append(BacktestResult(
            eval_date=eval_date, recommended_hour=rec_hour, actual_hour=actual_dt.hour,
            matched=abs(rec_hour - actual_dt.hour) <= 1,
            recommended_coverage_pct=rec_coverage, best_possible_coverage_pct=best_coverage,
            regret_pct=max(0.0, best_coverage - rec_coverage),
        ))
    return results


def summarize(results: list[BacktestResult]) -> dict:
    if not results:
        return {}
    n = len(results)
    return {
        "n_days": n,
        "match_rate": sum(r.matched for r in results) / n,
        "mean_recommended_coverage_pct": sum(r.recommended_coverage_pct for r in results) / n,
        "mean_best_possible_coverage_pct": sum(r.best_possible_coverage_pct for r in results) / n,
        "mean_regret_pct": sum(r.regret_pct for r in results) / n,
    }


async def fetch_actual_solar_production(
    hass, solar_energy_entity: str, days: int = 90,
) -> dict[date, dict[int, float]]:
    meta_list = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, {solar_energy_entity}
    )
    if not meta_list:
        return {}

    meta = meta_list[0]
    unit = meta.get("unit_of_measurement") if isinstance(meta, dict) else getattr(meta, "unit_of_measurement", None)
    to_wh = 1000.0 if unit == "kWh" else 1.0

    end = dt_util.utcnow()
    start = end - timedelta(days=days)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {solar_energy_entity}, "hour", None, {"sum"},
    )
    rows = stats.get(solar_energy_entity, [])

    by_date: dict[date, dict[int, float]] = {}
    prev_sum = None
    for row in rows:
        cur_sum = row.get("sum")
        row_start = _to_local_dt(row["start"])
        if cur_sum is None or prev_sum is None:
            prev_sum = cur_sum
            continue
        wh = max(0.0, (cur_sum - prev_sum) * to_wh)
        prev_sum = cur_sum
        by_date.setdefault(row_start.date(), {})[row_start.hour] = wh
    return by_date
