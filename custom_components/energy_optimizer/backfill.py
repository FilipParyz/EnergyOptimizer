"""Historical backfill: run once per appliance on first setup, and again on
demand via the energy_optimizer.rerun_backfill service (e.g. after the
detection thresholds were tuned in the options flow).

Sources, tried in this order by source="auto":

1. long-term statistics on the energy sensor (hourly resolution, kept
   indefinitely) -- deepest history, when it exists.
2. a dedicated "is running" binary sensor, if one is configured -- raw
   state history (purged after ~10 days by default, same ceiling as #3),
   but every on/off transition is an unambiguous, complete cycle. Energy is
   filled in from the power sensor's history over the same window.
3. raw power-sensor state history replayed through the same CycleDetector
   used for live tracking -- same ~10 day ceiling, and the only source the
   configured start/end thresholds actually apply to.

Backfilled and live-tracked runs share the same CycleResult shape either
way, so downstream code never needs to know which path produced them.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
import homeassistant.util.dt as dt_util

from .const import RUNNING_REFRESH_S
from .detector import CycleDetector, CycleResult, DetectorState

_LOGGER = logging.getLogger(__name__)

IDLE_ENERGY_WH = 5.0

SOURCE_AUTO = "auto"
SOURCE_ENERGY = "energy_statistics"
SOURCE_BINARY = "binary_sensor"
SOURCE_POWER = "power_history"
SOURCES = (SOURCE_AUTO, SOURCE_ENERGY, SOURCE_BINARY, SOURCE_POWER)


def _to_local_dt(value):
    if isinstance(value, (int, float)):
        return dt_util.as_local(dt_util.utc_from_timestamp(value))
    return dt_util.as_local(value)


async def backfill_runs(hass: HomeAssistant, power_entity: str, energy_entity: str | None,
                          detector_config: dict, binary_entity: str | None = None,
                          lookback_days_stats: int = 180, lookback_days_raw: int = 9) -> list[CycleResult]:
    _source, runs, _window_start = await backfill_runs_with_source(
        hass, power_entity, energy_entity, detector_config, binary_entity,
        source=SOURCE_AUTO, lookback_days_stats=lookback_days_stats, lookback_days_raw=lookback_days_raw,
    )
    return runs


async def backfill_runs_with_source(
    hass: HomeAssistant, power_entity: str, energy_entity: str | None, detector_config: dict,
    binary_entity: str | None = None, source: str = SOURCE_AUTO,
    lookback_days_stats: int = 180, lookback_days_raw: int = 9,
) -> tuple[str, list[CycleResult], datetime]:
    now = dt_util.utcnow()
    stats_start = now - timedelta(days=lookback_days_stats)
    raw_start = now - timedelta(days=lookback_days_raw)

    if source == SOURCE_ENERGY or (source == SOURCE_AUTO and energy_entity):
        if not energy_entity:
            raise ValueError("source=energy_statistics needs an energy sensor in the appliance config")
        runs = await _backfill_from_statistics(hass, energy_entity, stats_start, now)
        if runs or source == SOURCE_ENERGY:
            return SOURCE_ENERGY, runs, stats_start
        _LOGGER.info(
            "No long-term statistics found for %s yet; trying the next available source.", energy_entity,
        )

    if source == SOURCE_BINARY or (source == SOURCE_AUTO and binary_entity):
        if not binary_entity:
            raise ValueError("source=binary_sensor needs a binary sensor in the appliance options")
        runs = await _backfill_from_binary_sensor(hass, binary_entity, raw_start, now)
        if runs:
            await _attach_energy_from_power(hass, power_entity, runs, raw_start, now)
        if runs or source == SOURCE_BINARY:
            return SOURCE_BINARY, runs, raw_start
        _LOGGER.info(
            "No on/off history found for %s in the last %d days either; falling back to "
            "raw power-threshold detection.", binary_entity, lookback_days_raw,
        )

    if source == SOURCE_AUTO and not energy_entity and not binary_entity:
        _LOGGER.info(
            "No energy_entity or binary_entity configured; using raw power-threshold "
            "detection, which only covers the last %d days and depends on the configured "
            "start/end thresholds.", lookback_days_raw,
        )
    runs = await _backfill_from_raw_states(hass, power_entity, detector_config, raw_start, now)
    return SOURCE_POWER, runs, raw_start


async def _backfill_from_statistics(hass: HomeAssistant, energy_entity: str,
                                      start: datetime, end: datetime) -> list[CycleResult]:
    meta_list = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, {energy_entity}
    )
    if not meta_list:
        return []

    meta = meta_list[0]
    unit = meta.get("unit_of_measurement") if isinstance(meta, dict) else getattr(meta, "unit_of_measurement", None)
    # Hourly bucket values are treated as Wh for that hour; convert kWh sources.
    to_wh = 1000.0 if unit == "kWh" else 1.0

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


async def _backfill_from_binary_sensor(hass: HomeAssistant, binary_entity: str,
                                         start: datetime, end: datetime) -> list[CycleResult]:
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


async def _power_readings(hass: HomeAssistant, power_entity: str,
                            start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    result = await get_instance(hass).async_add_executor_job(
        history.state_changes_during_period, hass, start, end, power_entity,
    )
    readings = []
    for state in result.get(power_entity, []):
        try:
            readings.append((dt_util.as_utc(state.last_changed), float(state.state)))
        except (TypeError, ValueError):
            continue  # unavailable / unknown
    return readings


async def _attach_energy_from_power(hass: HomeAssistant, power_entity: str, runs: list[CycleResult],
                                      start: datetime, end: datetime) -> None:
    readings = await _power_readings(hass, power_entity, start, end)
    if not readings:
        return
    for run in runs:
        energy_wh = 0.0
        for i, (ts, watts) in enumerate(readings):
            seg_end = readings[i + 1][0] if i + 1 < len(readings) else end
            lo, hi = max(ts, run.start_time), min(seg_end, run.end_time)
            if hi > lo:
                energy_wh += watts * (hi - lo).total_seconds() / 3600
        run.energy_wh = energy_wh


def _advance_without_readings(detector: CycleDetector, until: datetime, power_w: float,
                                runs: list[CycleResult]) -> None:
    while True:
        deadline = detector.next_deadline()
        if deadline is None and detector.state == DetectorState.RUNNING and detector._last_reading:
            deadline = detector._last_reading[0] + timedelta(seconds=RUNNING_REFRESH_S)
        if deadline is None or deadline >= until:
            return
        result = detector.process_reading(deadline, power_w)
        if result:
            runs.append(result)


async def _backfill_from_raw_states(hass: HomeAssistant, power_entity: str, detector_config: dict,
                                      start: datetime, end: datetime) -> list[CycleResult]:
    readings = await _power_readings(hass, power_entity, start, end)
    detector = CycleDetector(**detector_config)
    runs: list[CycleResult] = []
    last_w: float | None = None
    for ts, watts in readings:
        if last_w is not None:
            _advance_without_readings(detector, ts, last_w, runs)
        result = detector.process_reading(ts, watts)
        if result:
            runs.append(result)
        last_w = watts
    if last_w is not None:
        _advance_without_readings(detector, end, last_w, runs)
    return runs
