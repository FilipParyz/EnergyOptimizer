"""One-time historical backfill, run once per appliance on first setup.

Tries three sources, in order of how much history they can actually give
us and how reliably they reflect real "was the appliance running" events:

1. long-term statistics on the energy sensor (hourly resolution, kept
   indefinitely) -- deepest history, when it exists.
2. a dedicated "is running" binary sensor, if one is configured -- raw
   state history (purged after ~10 days by default, same ceiling as #3),
   but every on/off transition is an unambiguous, complete cycle: no
   threshold tuning involved, and no per-reading detector state that a
   Home Assistant restart could lose mid-cycle.
3. raw power-sensor state history replayed through the same CycleDetector
   used for live tracking -- last resort, same ~10 day ceiling as #2, and
   only as good as the configured power thresholds.

Backfilled and live-tracked runs share the same CycleResult shape either
way, so downstream code never needs to know which path produced them.
"""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
import homeassistant.util.dt as dt_util

from .detector import CycleDetector, CycleResult

_LOGGER = logging.getLogger(__name__)

IDLE_ENERGY_WH = 5.0


def _to_local_dt(value):
    if isinstance(value, (int, float)):
        return dt_util.as_local(dt_util.utc_from_timestamp(value))
    return dt_util.as_local(value)


async def backfill_runs(hass: HomeAssistant, power_entity: str, energy_entity: str | None,
                          detector_config: dict, binary_entity: str | None = None,
                          lookback_days_stats: int = 180, lookback_days_raw: int = 9) -> list[CycleResult]:
    if energy_entity:
        runs = await _backfill_from_statistics(hass, energy_entity, lookback_days_stats)
        if runs:
            return runs
        _LOGGER.info(
            "No long-term statistics found for %s yet; trying the next available source.",
            energy_entity,
        )

    if binary_entity:
        runs = await _backfill_from_binary_sensor(hass, binary_entity, lookback_days_raw)
        if runs:
            return runs
        _LOGGER.info(
            "No on/off history found for %s in the last %d days either; falling back to "
            "raw power-threshold detection.",
            binary_entity, lookback_days_raw,
        )

    if not energy_entity and not binary_entity:
        _LOGGER.info(
            "No energy_entity or binary_entity configured; using raw power-threshold "
            "detection, which only covers the last %d days and depends on the configured "
            "start/end thresholds. Consider adding a Utility Meter/Riemann sum helper on "
            "the power sensor, or a dedicated 'is running' binary sensor, for better "
            "backfills next time.",
            lookback_days_raw,
        )
    return await _backfill_from_raw_states(hass, power_entity, detector_config, lookback_days_raw)


async def _backfill_from_statistics(hass: HomeAssistant, energy_entity: str, days: int) -> list[CycleResult]:
    meta_list = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, {energy_entity}
    )
    if not meta_list:
        return []

    meta = meta_list[0]
    unit = meta.get("unit_of_measurement") if isinstance(meta, dict) else getattr(meta, "unit_of_measurement", None)
    # Hourly bucket values are treated as Wh for that hour; convert kWh sources.
    to_wh = 1000.0 if unit == "kWh" else 1.0

    end = dt_util.utcnow()
    start = end - timedelta(days=days)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {energy_entity}, "hour", None, {"sum"},
    )
    rows = stats.get(energy_entity, [])

    runs: list[CycleResult] = []
    prev_sum = None
    active_start = None
    active_energy = 0.0
    last_row_end = None

    def close_run(end_time):
        nonlocal active_start, active_energy
        if active_start is not None and end_time is not None:
            runs.append(CycleResult(
                start_time=active_start,
                end_time=end_time,
                duration_s=max(0.0, (end_time - active_start).total_seconds()),
                energy_wh=active_energy,
            ))
        active_start = None
        active_energy = 0.0

    for row in rows:
        cur_sum = row.get("sum")
        row_start = _to_local_dt(row["start"])
        row_end = _to_local_dt(row["end"])
        if cur_sum is None or prev_sum is None:
            prev_sum = cur_sum
            continue
        # `sum` is reset-compensated by the recorder itself, so a plain diff
        # between consecutive hourly buckets is safe even across meter resets.
        wh = (cur_sum - prev_sum) * to_wh
        prev_sum = cur_sum
        if wh > IDLE_ENERGY_WH:
            if active_start is None:
                active_start = row_start
            active_energy += wh
            last_row_end = row_end
        else:
            close_run(last_row_end or row_start)
        last_row_end = row_end
    close_run(last_row_end)
    return runs


async def _backfill_from_binary_sensor(hass: HomeAssistant, binary_entity: str, days: int) -> list[CycleResult]:
    end = dt_util.utcnow()
    start = end - timedelta(days=days)
    result = await get_instance(hass).async_add_executor_job(
        history.state_changes_during_period, hass, start, end, binary_entity,
    )
    states = result.get(binary_entity, [])

    runs: list[CycleResult] = []
    active_start = None
    for state in states:
        if state.state == "on" and active_start is None:
            active_start = dt_util.as_utc(state.last_changed)
        elif state.state != "on" and active_start is not None:
            end_time = dt_util.as_utc(state.last_changed)
            runs.append(CycleResult(
                start_time=active_start,
                end_time=end_time,
                duration_s=max(0.0, (end_time - active_start).total_seconds()),
                energy_wh=0.0,
            ))
            active_start = None
    return runs


async def _backfill_from_raw_states(hass: HomeAssistant, power_entity: str,
                                      detector_config: dict, days: int) -> list[CycleResult]:
    end = dt_util.utcnow()
    start = end - timedelta(days=days)
    result = await get_instance(hass).async_add_executor_job(
        history.state_changes_during_period, hass, start, end, power_entity,
    )
    states = result.get(power_entity, [])

    detector = CycleDetector(**detector_config)
    runs: list[CycleResult] = []
    for state in states:
        try:
            power_w = float(state.state)
        except (TypeError, ValueError):
            continue
        res = detector.process_reading(dt_util.as_utc(state.last_changed), power_w)
        if res:
            runs.append(res)
    return runs
