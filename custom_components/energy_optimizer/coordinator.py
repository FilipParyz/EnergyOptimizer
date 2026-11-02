"""Runtime orchestration: one ApplianceRuntime per configured appliance,
plus the single shared daily job that scores every appliance and then
jointly assigns them against a shared solar budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
import homeassistant.util.dt as dt_util

from . import scheduler, ml, backfill
from .const import (
    DOMAIN, CONF_NAME, CONF_APPLIANCE_KIND, CONF_POWER_ENTITY, CONF_ENERGY_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY, CONF_START_THRESHOLD_W, CONF_START_DURATION_S,
    CONF_END_THRESHOLD_W, CONF_END_DURATION_S, CONF_ALLOWED_START, CONF_ALLOWED_END,
    CONF_QUIET_START, CONF_QUIET_END, CONF_PREF_WEIGHT, CONF_NOTIFY_TARGETS,
    CONF_NOTIFY_ACTION, CONF_FIRE_EVENTS, CONF_BINARY_ENTITY,
    DEFAULT_START_THRESHOLD_W, DEFAULT_START_DURATION_S, DEFAULT_END_THRESHOLD_W,
    DEFAULT_END_DURATION_S, DEFAULT_ALLOWED_START, DEFAULT_ALLOWED_END,
    DEFAULT_QUIET_START, DEFAULT_QUIET_END, DEFAULT_PREF_WEIGHT,
    EVENT_CYCLE_ENDED, EVENT_SUGGESTION_READY, SIGNAL_RECOMMENDATION_UPDATED,
    APPLIANCE_KINDS, MAX_LOOKAHEAD_DAYS, DATA_RUNTIME, MIN_RUN_DURATION_MIN, RUNNING_REFRESH_S,
)
from .debug import fmt_hours, rounded
from .detector import CycleDetector, DetectorState
from .store import ApplianceStore, SAME_DAY_RECONCILABLE
from .notify_rules import async_notify

_LOGGER = logging.getLogger(__name__)


@dataclass
class ApplianceConfig:
    entry_id: str
    name: str
    kind: str
    power_entity: str
    energy_entity: str | None
    solar_forecast_entity: str | None
    start_threshold_w: float
    start_duration_s: int
    end_threshold_w: float
    end_duration_s: int
    allowed_start: str
    allowed_end: str
    quiet_start: str
    quiet_end: str
    pref_weight: float
    notify_targets: list
    notify_action: list
    fire_events: bool
    binary_entity: str | None = None


class ApplianceRuntime:
    """Everything tracked at runtime for one configured appliance."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.config = _config_from_entry(entry)
        self.store = ApplianceStore(hass, entry.entry_id)
        self.detector = CycleDetector(
            start_threshold_w=self.config.start_threshold_w,
            start_duration_s=self.config.start_duration_s,
            end_threshold_w=self.config.end_threshold_w,
            end_duration_s=self.config.end_duration_s,
            name=self.config.name,
        )
        self.latest_recommendation: dict | None = None
        self.last_trace: dict | None = None
        self.last_cycle_trace: dict | None = None
        self._unsub_state = None
        self._unsub_tick = None
        self._last_power_w: float | None = None

    async def async_setup(self) -> None:
        await self.store.async_load()
        _LOGGER.debug(
            "[%s] Loaded store: %d run(s), %d suggestion(s), backfill_done=%s, ml_model=%s",
            self.config.name, len(self.store.runs), len(self.store.suggestions),
            self.store.backfill_done,
            "promoted" if self.store.ml_model and self.store.ml_model.get("promoted") else "none",
        )
        if self.store.suggestions:
            self.latest_recommendation = _recommendation_from_suggestion(self.store.suggestions[-1])
        self._unsub_state = async_track_state_change_event(
            self.hass, [self.config.power_entity], self._handle_power_change
        )
        current = self.hass.states.get(self.config.power_entity)
        power_w = _parse_power(current.state if current else None)
        if power_w is not None:
            _LOGGER.debug("[%s] Initial power reading: %.0f W", self.config.name, power_w)
            self._feed_reading(dt_util.utcnow(), power_w)
        if not self.store.backfill_done:
            self.hass.async_create_task(self._async_backfill())

    @callback
    def notify_updated(self) -> None:
        async_dispatcher_send(self.hass, f"{SIGNAL_RECOMMENDATION_UPDATED}_{self.entry.entry_id}")

    def status_snapshot(self) -> dict:
        snap: dict = {
            "appliance_state": "running" if self.detector.is_running else "idle",
            "running_since": None,
            "current_run_fit": None,
            "suggestion_status": self.store.suggestions[-1].status if self.store.suggestions else None,
            "last_run": None,
        }
        start = self.detector.cycle_start
        if self.detector.is_running and start is not None:
            local_start = dt_util.as_local(start)
            snap["running_since"] = local_start.isoformat()
            snap["current_run_fit"] = self._run_fit(local_start)

        parsed = [(r, dt_util.parse_datetime(r.start_time)) for r in self.store.runs]
        parsed = [(r, dt) for r, dt in parsed if dt is not None]
        if parsed:
            run, run_start = max(parsed, key=lambda p: p[1])
            local_start = dt_util.as_local(run_start)
            snap["last_run"] = {
                "start": local_start.isoformat(),
                "end": run.end_time,
                "duration_min": round(run.duration_s / 60),
                "energy_wh": round(run.energy_wh),
                "source": run.source,
                **self._run_fit(local_start),
            }
        return snap

    def _run_fit(self, local_start: datetime) -> dict:
        suggestion = self.store.same_day_suggestion(local_start)
        if suggestion is None:
            return {"fit": "no_suggestion", "suggested_hour": None}
        in_window = abs(local_start.hour - suggestion.suggested_hour) <= 1
        return {
            "fit": "in_window" if in_window else "outside_window",
            "suggested_hour": suggestion.suggested_hour,
        }

    async def async_unload(self) -> None:
        if self._unsub_state:
            self._unsub_state()
        self._cancel_tick()

    async def _async_backfill(self) -> None:
        """First-setup backfill: automatic source, replaces nothing live."""
        try:
            summary = await self.async_rerun_backfill(source=backfill.SOURCE_AUTO, days=None, mode="replace")
            _LOGGER.info("%s: backfilled %d historical run(s) from %s",
                         self.config.name, summary["added"], summary["source"])
        except Exception:
            _LOGGER.exception(
                "%s: historical backfill failed; continuing with live tracking only", self.config.name
            )

    async def async_rerun_backfill(self, source: str = "auto", days: int | None = None,
                                     mode: str = "replace") -> dict:
        cfg = self.config
        raw_days = days or 9
        stats_days = days or 180
        used_source, runs, window_start = await backfill.backfill_runs_with_source(
            self.hass, cfg.power_entity, cfg.energy_entity,
            detector_config={
                "start_threshold_w": cfg.start_threshold_w,
                "start_duration_s": cfg.start_duration_s,
                "end_threshold_w": cfg.end_threshold_w,
                "end_duration_s": cfg.end_duration_s,
            },
            binary_entity=cfg.binary_entity, source=source,
            lookback_days_stats=stats_days, lookback_days_raw=raw_days,
        )

        removed = 0
        if mode == "replace":
            kept = []
            for r in self.store.runs:
                start = dt_util.parse_datetime(r.start_time)
                if r.source == "backfill" and start is not None and start >= window_start:
                    removed += 1
                    continue
                kept.append(r)
            self.store.runs = kept

        spans = _run_spans(self.store.runs)
        added = skipped_overlap = skipped_short = 0
        for run in runs:
            if run.duration_s < MIN_RUN_DURATION_MIN * 60:
                skipped_short += 1
                continue
            if _overlaps(spans, run.start_time, run.end_time):
                skipped_overlap += 1
                continue
            self.store.add_run(run.start_time, run.end_time, run.duration_s, run.energy_wh, source="backfill")
            spans.append((run.start_time, run.end_time))
            added += 1
            _LOGGER.debug(
                "[%s] backfill run: %s -> %s, %.0f min, %.0f Wh", cfg.name,
                dt_util.as_local(run.start_time).isoformat(), dt_util.as_local(run.end_time).isoformat(),
                run.duration_s / 60, run.energy_wh,
            )
        self.store.runs.sort(key=lambda r: r.start_time)
        self.store.backfill_done = True
        await self.store.async_save()

        summary = {
            "name": cfg.name, "source": used_source, "mode": mode,
            "window_start": dt_util.as_local(window_start).isoformat(),
            "found": len(runs), "removed": removed, "added": added,
            "skipped_overlap": skipped_overlap, "skipped_short": skipped_short,
            "total_runs": len(self.store.runs),
        }
        _LOGGER.debug("[%s] Backfill summary: %s", cfg.name, summary)
        self.notify_updated()
        return summary

    @callback
    def _handle_power_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        power_w = _parse_power(new_state.state if new_state else None)
        if power_w is None:
            return
        self._feed_reading(dt_util.utcnow(), power_w)

    @callback
    def _feed_reading(self, timestamp: datetime, power_w: float) -> None:
        self._last_power_w = power_w
        was_running = self.detector.is_running
        result = self.detector.process_reading(timestamp, power_w)
        self._schedule_tick()
        if self.detector.is_running != was_running:
            self.notify_updated()
        if result is not None:
            self.hass.async_create_task(self._async_handle_cycle_end(result))

    @callback
    def _cancel_tick(self) -> None:
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None

    @callback
    def _schedule_tick(self) -> None:
        self._cancel_tick()
        deadline = self.detector.next_deadline()
        if deadline is None and self.detector.state == DetectorState.RUNNING:
            deadline = dt_util.utcnow() + timedelta(seconds=RUNNING_REFRESH_S)
        if deadline is None:
            return
        self._unsub_tick = async_track_point_in_utc_time(
            self.hass, self._handle_tick, deadline + timedelta(seconds=1)
        )

    @callback
    def _handle_tick(self, now: datetime) -> None:
        self._unsub_tick = None
        if self._last_power_w is None:
            return
        if self.detector.state != DetectorState.RUNNING:  # periodic refreshes stay quiet
            _LOGGER.debug(
                "[%s] No new reading since the detector entered %s -- re-evaluating with the last value (%.0f W)",
                self.config.name, self.detector.state.value, self._last_power_w,
            )
        self._feed_reading(now, self._last_power_w)

    async def _async_handle_cycle_end(self, result) -> None:
        local_start = dt_util.as_local(result.start_time)
        if result.duration_s < MIN_RUN_DURATION_MIN * 60:
            _LOGGER.debug(
                "[%s] Ignoring a %.1f min cycle starting %s: shorter than %d min, treated as noise",
                self.config.name, result.duration_s / 60, local_start.isoformat(), MIN_RUN_DURATION_MIN,
            )
            self.last_cycle_trace = {
                "start": local_start.isoformat(), "duration_min": round(result.duration_s / 60, 1),
                "ignored": f"shorter than {MIN_RUN_DURATION_MIN} min",
            }
            return
        _LOGGER.debug(
            "[%s] Cycle finished: start %s, end %s, %.0f min, %.0f Wh (avg %.0f W)",
            self.config.name, local_start.isoformat(), dt_util.as_local(result.end_time).isoformat(),
            result.duration_s / 60, result.energy_wh,
            result.energy_wh / (result.duration_s / 3600) if result.duration_s > 0 else 0.0,
        )
        self.store.add_run(result.start_time, result.end_time, result.duration_s, result.energy_wh, source="live")
        reconcile = self._reconcile_suggestion(result)
        self.last_cycle_trace = {
            "start": local_start.isoformat(),
            "end": dt_util.as_local(result.end_time).isoformat(),
            "duration_min": round(result.duration_s / 60, 1),
            "energy_wh": round(result.energy_wh, 1),
            "reconcile": reconcile,
        }
        await self.store.async_save()
        self.notify_updated()
        if self.config.fire_events:
            self.hass.bus.async_fire(EVENT_CYCLE_ENDED, {
                "entry_id": self.entry.entry_id,
                "name": self.config.name,
                "start_time": local_start.isoformat(),
                "duration_s": result.duration_s,
                "energy_wh": result.energy_wh,
            })

    def _reconcile_suggestion(self, result) -> dict:
        name = self.config.name
        local_start = dt_util.as_local(result.start_time)
        hour = local_start.hour
        run_date = local_start.date().isoformat()
        info: dict = {"run_date": run_date, "run_hour": hour, "same_day": None, "early_run_for": None}

        same_day = self.store.same_day_suggestion(local_start)
        if same_day is None:
            _LOGGER.debug("[%s] Reconcile: run %s %02d:00 -- no suggestion for this day", name, run_date, hour)
        else:
            previous = same_day.status
            if previous in SAME_DAY_RECONCILABLE:
                if abs(hour - same_day.suggested_hour) <= 1:
                    same_day.status = "matched"
                    same_day.actual_start_hour = hour
                    same_day.resolved_at = dt_util.now().isoformat()
                elif previous != "missed":
                    same_day.status = "missed"
                    same_day.actual_start_hour = hour
                    same_day.resolved_at = dt_util.now().isoformat()
                # else: already missed by an earlier off-target run today --
                # keep that first hour; a later on-time run can still upgrade.
            _LOGGER.debug(
                "[%s] Reconcile: run %s %02d:00 vs today's suggestion %02d:00: %s -> %s",
                name, run_date, hour, same_day.suggested_hour, previous, same_day.status,
            )
            info["same_day"] = {
                "suggestion_target_date": same_day.target_date, "suggested_hour": same_day.suggested_hour,
                "previous_status": previous, "new_status": same_day.status,
            }

        upcoming = self.store.upcoming_suggestion(local_start)
        if upcoming is not None:
            upcoming.early_runs.append(local_start.isoformat())
            _LOGGER.debug(
                "[%s] Reconcile: run %s %02d:00 is before suggestion %s %02d:00 -- recorded as early run "
                "#%d, suggestion stays %s for its day",
                name, run_date, hour, upcoming.target_date, upcoming.suggested_hour,
                len(upcoming.early_runs), upcoming.status,
            )
            info["early_run_for"] = {
                "suggestion_target_date": upcoming.target_date, "suggested_hour": upcoming.suggested_hour,
                "status": upcoming.status, "early_runs": list(upcoming.early_runs),
            }
        return info


def _recommendation_from_suggestion(suggestion) -> dict | None:
    try:
        return {
            "target_date": date.fromisoformat(suggestion.target_date),
            "hour": suggestion.suggested_hour,
            "expected_coverage_pct": suggestion.expected_coverage_pct,
            "confidence": suggestion.confidence,
            "basis": suggestion.basis,
        }
    except (TypeError, ValueError):
        return None


def _entity_from_entry(entry: ConfigEntry, key: str) -> str | None:
    if key in entry.options:
        return entry.options[key] or None
    return entry.data.get(key) or None


def _config_from_entry(entry: ConfigEntry) -> ApplianceConfig:
    data, opts = entry.data, entry.options
    return ApplianceConfig(
        entry_id=entry.entry_id,
        name=data.get(CONF_NAME) or entry.title,
        kind=data.get(CONF_APPLIANCE_KIND, "other"),
        power_entity=_entity_from_entry(entry, CONF_POWER_ENTITY),
        energy_entity=_entity_from_entry(entry, CONF_ENERGY_ENTITY),
        solar_forecast_entity=_entity_from_entry(entry, CONF_SOLAR_FORECAST_ENTITY),
        start_threshold_w=opts.get(CONF_START_THRESHOLD_W, DEFAULT_START_THRESHOLD_W),
        start_duration_s=opts.get(CONF_START_DURATION_S, DEFAULT_START_DURATION_S),
        end_threshold_w=opts.get(CONF_END_THRESHOLD_W, DEFAULT_END_THRESHOLD_W),
        end_duration_s=opts.get(CONF_END_DURATION_S, DEFAULT_END_DURATION_S),
        allowed_start=opts.get(CONF_ALLOWED_START, DEFAULT_ALLOWED_START),
        allowed_end=opts.get(CONF_ALLOWED_END, DEFAULT_ALLOWED_END),
        quiet_start=opts.get(CONF_QUIET_START, DEFAULT_QUIET_START),
        quiet_end=opts.get(CONF_QUIET_END, DEFAULT_QUIET_END),
        pref_weight=opts.get(CONF_PREF_WEIGHT, DEFAULT_PREF_WEIGHT),
        notify_targets=opts.get(CONF_NOTIFY_TARGETS, []),
        notify_action=opts.get(CONF_NOTIFY_ACTION, []),
        fire_events=opts.get(CONF_FIRE_EVENTS, True),
        binary_entity=_entity_from_entry(entry, CONF_BINARY_ENTITY),
    )


_FORECAST_VALUE_KEYS: tuple[tuple[str, str, float], ...] = (
    ("pv_estimate", "power", 1000.0),
    ("wh", "energy", 1.0),
    ("value", "energy", 1.0),
)

_FORECAST_ATTRS = ("detailedHourly", "wh_hours", "detailedForecast", "hourly_forecast", "forecast")


def _forecast_curve(hass: HomeAssistant, forecast_entity: str | None, day_offset: int) -> dict[int, float]:
    """Best-effort read of an hourly forecast curve {local hour: Wh in that
    hour} for the day `day_offset` days from today, from a forecast entity's
    attributes. Different solar forecast integrations expose this
    differently -- this tries a few common attribute shapes and degrades to
    an empty curve (pure-habit scheduling) if none match.

    Sub-hourly data (e.g. Solcast's 30-minute `detailedForecast`) is
    aggregated per hour instead of letting later items overwrite earlier
    ones, and values are normalised to Wh so that coverage percentages and
    the joint scheduler's capacity check compare like with like against
    appliance power in W."""
    if not forecast_entity:
        _LOGGER.debug("Forecast D+%d: no solar forecast entity configured", day_offset)
        return {}
    state = hass.states.get(forecast_entity)
    if not state:
        _LOGGER.debug("Forecast D+%d: entity %s not found", day_offset, forecast_entity)
        return {}
    today = dt_util.now().date()

    def _local_hour_for_day(ts) -> int | None:
        parsed = dt_util.parse_datetime(str(ts)) if ts is not None else None
        if parsed is None:
            return None
        local = dt_util.as_local(parsed)
        if (local.date() - today).days != day_offset:
            return None
        return local.hour

    for attr_name in _FORECAST_ATTRS:
        raw = state.attributes.get(attr_name)

        if isinstance(raw, dict):
            curve: dict[int, float] = {}
            for key, val in raw.items():
                hour = _local_hour_for_day(key)
                if hour is None or val is None:
                    continue
                try:
                    curve[hour] = curve.get(hour, 0.0) + float(val)
                except (TypeError, ValueError):
                    continue
            if curve:
                _LOGGER.debug(
                    "Forecast D+%d: read %d hour(s) from %s.%s (dict), total %.0f Wh",
                    day_offset, len(curve), forecast_entity, attr_name, sum(curve.values()),
                )
                return curve

        if isinstance(raw, list):
            energy: dict[int, float] = {}
            power_sum: dict[int, float] = {}
            power_count: dict[int, int] = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                hour = _local_hour_for_day(
                    item.get("period_start") or item.get("datetime") or item.get("time")
                )
                if hour is None:
                    continue
                # `is not None`, not truthiness: a 0 kW night-time value is a
                # real reading, not a missing one.
                found = next(
                    ((item[k], kind, mult) for k, kind, mult in _FORECAST_VALUE_KEYS if item.get(k) is not None),
                    None,
                )
                if found is None:
                    continue
                raw_val, kind, mult = found
                try:
                    val = float(raw_val) * mult
                except (TypeError, ValueError):
                    continue
                if kind == "power":
                    power_sum[hour] = power_sum.get(hour, 0.0) + val
                    power_count[hour] = power_count.get(hour, 0) + 1
                else:
                    energy[hour] = energy.get(hour, 0.0) + val

            curve = dict(energy)
            for hour, total in power_sum.items():
                curve[hour] = curve.get(hour, 0.0) + total / power_count[hour]
            if curve:
                _LOGGER.debug(
                    "Forecast D+%d: read %d hour(s) from %s.%s (list), total %.0f Wh",
                    day_offset, len(curve), forecast_entity, attr_name, sum(curve.values()),
                )
                return curve

    _LOGGER.debug(
        "Forecast D+%d: no usable data in %s (checked attributes: %s)",
        day_offset, forecast_entity, ", ".join(_FORECAST_ATTRS),
    )
    return {}


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _run_stats(runs, kind: str) -> dict:
    spec = APPLIANCE_KINDS.get(kind, APPLIANCE_KINDS["other"])
    min_s, max_s = MIN_RUN_DURATION_MIN * 60, spec["max_duration_min"] * 60
    plausible = [r for r in runs if min_s <= r.duration_s <= max_s]
    excluded = len(runs) - len(plausible)

    if plausible:
        duration_hours = _median([r.duration_s / 3600 for r in plausible])
        duration_source = f"median of {len(plausible)} plausible run(s)"
    else:
        duration_hours = spec["default_duration_min"] / 60
        duration_source = f"default for {kind}"

    powers = [r.energy_wh / (r.duration_s / 3600) for r in plausible if r.energy_wh > 0]
    if powers:
        avg_power_w = _median(powers)
        power_source = f"median of {len(powers)} run(s) with measured energy"
    else:
        avg_power_w = float(spec["default_power_w"])
        power_source = f"default for {kind} (no run has measured energy)"

    return {
        "duration_hours": duration_hours, "duration_source": duration_source,
        "avg_power_w": avg_power_w, "power_source": power_source,
        "runs_used": len(plausible), "runs_excluded": excluded,
        "plausible_range_min": [MIN_RUN_DURATION_MIN, spec["max_duration_min"]],
    }


def _typical_duration_hours(runs, kind: str) -> float:
    return _run_stats(runs, kind)["duration_hours"]


def _typical_power_w(runs, duration_hours: float, kind: str = "other") -> float:
    return _run_stats(runs, kind)["avg_power_w"]


def _parse_power(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _run_spans(runs) -> list[tuple[datetime, datetime]]:
    spans = []
    for r in runs:
        start, end = dt_util.parse_datetime(r.start_time), dt_util.parse_datetime(r.end_time)
        if start is not None and end is not None:
            spans.append((start, end))
    return spans


def _overlaps(spans: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    return any(start < s_end and s_start < end for s_start, s_end in spans)


def _basis(reason: str, day_best_hour: int, final_hour: int) -> str:
    if reason.startswith("Best fit for tomorrow"):
        text = f"Best fit for tomorrow around {final_hour:02d}:00."
    else:
        text = reason
    if final_hour != day_best_hour:
        text += (f" Moved from {day_best_hour:02d}:00 to {final_hour:02d}:00 to share the solar "
                 f"surplus with other appliances.")
    return text


async def async_run_daily_scheduling(hass: HomeAssistant) -> None:
    """The one shared job that scores every configured appliance and then
    jointly assigns them against a shared solar budget.

    Every intermediate value is logged at DEBUG level and kept in
    runtime.last_trace, which "Download diagnostics" exports as JSON."""
    runtimes: dict[str, ApplianceRuntime] = hass.data[DOMAIN][DATA_RUNTIME]
    today = dt_util.now().date()
    debug = _LOGGER.isEnabledFor(logging.DEBUG)
    _LOGGER.debug("=== Daily scheduling started (today=%s, %d appliance(s)) ===", today, len(runtimes))

    per_appliance: dict[str, dict] = {}

    for entry_id, runtime in runtimes.items():
        cfg = runtime.config
        name = cfg.name
        trace: dict = {
            "computed_at": dt_util.now().isoformat(),
            "today": today.isoformat(),
            "runs_available": len(runtime.store.runs),
            "pref_weight": cfg.pref_weight,
        }
        runtime.last_trace = trace

        expired = runtime.store.expire_stale_suggestions(today)
        trace["expired_suggestions"] = expired
        hours = scheduler.allowed_hours(cfg.allowed_start, cfg.allowed_end, cfg.quiet_start, cfg.quiet_end)
        trace["allowed_hours"] = hours
        _LOGGER.debug(
            "[%s] Step 1 -- input: %d run(s), expired %d stale suggestion(s), allowed hours %s "
            "(window %s-%s, quiet %s-%s), pref_weight=%.2f",
            name, len(runtime.store.runs), expired, hours, cfg.allowed_start, cfg.allowed_end,
            cfg.quiet_start, cfg.quiet_end, cfg.pref_weight,
        )
        if not hours:
            trace["skipped"] = "no allowed hours (window fully covered by quiet hours)"
            _LOGGER.debug("[%s] Skipped: %s", name, trace["skipped"])
            continue

        urgency_info = scheduler.compute_urgency(runtime.store.runs, today)
        stats = _run_stats(runtime.store.runs, cfg.kind)
        duration_hours = stats["duration_hours"]
        avg_power_w = stats["avg_power_w"]
        trace["urgency"] = {
            "urgency": round(urgency_info.urgency, 4),
            "typical_interval_days": round(urgency_info.typical_interval_days, 2),
            "days_since_last": urgency_info.days_since_last,
        }
        trace["duration_hours"] = round(duration_hours, 3)
        trace["avg_power_w"] = round(avg_power_w, 1)
        trace["run_stats"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in stats.items()}
        _LOGGER.debug(
            "[%s] Step 2 -- urgency=%.3f (days since last run=%s, typical interval=%.2f d); "
            "typical duration=%.2f h (%s); avg power=%.0f W (%s); %d run(s) outside %d-%d min excluded",
            name, urgency_info.urgency, urgency_info.days_since_last,
            urgency_info.typical_interval_days, duration_hours, stats["duration_source"],
            avg_power_w, stats["power_source"], stats["runs_excluded"], *stats["plausible_range_min"],
        )

        day_best: dict[int, tuple[int, float]] = {}
        day_blended: dict[int, dict[int, float]] = {}
        forecasts: dict[int, dict[int, float]] = {}
        trace["days"] = {}

        for offset in range(1, MAX_LOOKAHEAD_DAYS + 1):
            forecast = _forecast_curve(hass, cfg.solar_forecast_entity, offset)
            if not forecast and offset > 1:
                _LOGGER.debug("[%s] Step 3 -- D+%d skipped: forecast doesn't reach that far", name, offset)
                continue  # can't look further ahead than the forecast actually covers
            forecasts[offset] = forecast
            target_date = today + timedelta(days=offset)
            habit = scheduler.habit_scores(runtime.store.runs, target_date, hours)
            solar = scheduler.solar_alignment_scores(forecast, duration_hours, hours)
            heuristic_only = scheduler.combine_scores(habit, solar, cfg.pref_weight)

            ml_scores = ml.ml_adjusted_scores(
                runtime.store.ml_model, habit, solar, urgency_info.urgency, hours, target_date.weekday()
            )
            blended = heuristic_only
            if ml_scores:
                blended = {h: 0.5 * heuristic_only[h] + 0.5 * ml_scores.get(h, heuristic_only[h]) for h in hours}

            day_blended[offset] = blended
            day_best[offset] = scheduler.best_hour(blended)

            trace["days"][f"D+{offset}"] = {
                "target_date": target_date.isoformat(),
                "forecast_wh": rounded(forecast, 1),
                "habit": rounded(habit),
                "solar": rounded(solar),
                "heuristic": rounded(heuristic_only),
                "ml": rounded(ml_scores),
                "blended": rounded(blended),
                "best_hour": day_best[offset][0],
                "best_score": round(day_best[offset][1], 4),
            }
            if debug:
                _LOGGER.debug("[%s] Step 3 -- D+%d (%s):", name, offset, target_date)
                _LOGGER.debug("[%s]   forecast Wh: %s", name, fmt_hours(forecast, 0))
                _LOGGER.debug("[%s]   habit:       %s", name, fmt_hours(habit))
                _LOGGER.debug("[%s]   solar:       %s", name, fmt_hours(solar))
                _LOGGER.debug("[%s]   heuristic:   %s", name, fmt_hours(heuristic_only))
                _LOGGER.debug("[%s]   ml:          %s", name, fmt_hours(ml_scores))
                _LOGGER.debug("[%s]   blended:     %s", name, fmt_hours(blended))
                _LOGGER.debug("[%s]   best: %02d:00 (score %.3f)", name, day_best[offset][0], day_best[offset][1])

        if not day_best:
            trace["skipped"] = "no day could be scored"
            _LOGGER.debug("[%s] Skipped: %s", name, trace["skipped"])
            continue

        chosen_offset, reason = scheduler.choose_target_day(day_best, urgency_info)
        target_date = today + timedelta(days=chosen_offset)
        trace["chosen_day"] = {"offset": chosen_offset, "target_date": target_date.isoformat(), "reason": reason}
        _LOGGER.debug("[%s] Step 4 -- chosen day D+%d (%s): %s", name, chosen_offset, target_date, reason)

        per_appliance[entry_id] = {
            "cfg": cfg, "runtime": runtime, "urgency_info": urgency_info,
            "duration_hours": duration_hours, "avg_power_w": avg_power_w,
            "target_date": target_date, "reason": reason,
            "day_best_hour": day_best[chosen_offset][0],
            "blended_scores": day_blended[chosen_offset], "hours": hours,
            "forecast": forecasts.get(chosen_offset, {}),
            "confidence": "personalized" if runtime.store.ml_model and runtime.store.ml_model.get("promoted") else "baseline",
        }

    groups: dict[date, list[str]] = {}
    for entry_id, info in per_appliance.items():
        groups.setdefault(info["target_date"], []).append(entry_id)

    for target_date, entry_ids in groups.items():
        candidates = [
            scheduler.ApplianceCandidate(
                entry_id=eid, name=per_appliance[eid]["cfg"].name,
                avg_power_w=per_appliance[eid]["avg_power_w"],
                duration_hours=per_appliance[eid]["duration_hours"],
                urgency=per_appliance[eid]["urgency_info"].urgency,
                typical_interval_days=per_appliance[eid]["urgency_info"].typical_interval_days,
                day_scores={0: per_appliance[eid]["blended_scores"]},
            )
            for eid in entry_ids
        ]
        shared_forecast = next(
            (per_appliance[eid]["forecast"] for eid in entry_ids if per_appliance[eid]["forecast"]), {}
        )
        joint_trace: list[dict] = []
        assignment = scheduler.joint_schedule(candidates, shared_forecast, trace=joint_trace)
        _LOGGER.debug(
            "Step 5 -- joint schedule for %s: %d appliance(s), order by urgency: %s",
            target_date, len(candidates), ", ".join(t["name"] for t in joint_trace) or "-",
        )
        for t in joint_trace:
            _LOGGER.debug(
                "  %s -> %02d:00 (score %.3f, capacity penalty %.3f, adjusted %.3f)",
                t["name"], t["chosen_hour"], t["score"], t["penalty"], t["adjusted_score"],
            )
        joint_by_id = {t["entry_id"]: t for t in joint_trace}

        for eid in entry_ids:
            if eid not in assignment:
                continue
            info = per_appliance[eid]
            runtime = info["runtime"]
            name = info["cfg"].name
            hour, _adj_score = assignment[eid]
            basis = _basis(info["reason"], info["day_best_hour"], hour)

            habit = scheduler.habit_scores(runtime.store.runs, target_date, info["hours"])
            solar = scheduler.solar_alignment_scores(info["forecast"], info["duration_hours"], info["hours"])
            heuristic_score_here = scheduler.combine_scores(habit, solar, info["cfg"].pref_weight).get(hour, 0.0)
            coverage_pct = round(scheduler.expected_solar_coverage_pct(
                info["forecast"], hour, info["duration_hours"], info["avg_power_w"]
            ))
            features = ml.build_features(
                habit.get(hour, 0.0), solar.get(hour, 0.0), info["urgency_info"].urgency,
                hour, target_date.weekday(),
            ).tolist()

            trace = runtime.last_trace if runtime.last_trace is not None else {}
            trace["joint_schedule"] = {
                "competing_appliances": [per_appliance[e]["cfg"].name for e in entry_ids],
                **{k: v for k, v in joint_by_id.get(eid, {}).items() if k not in ("entry_id", "name")},
            }
            trace["result"] = {
                "target_date": target_date.isoformat(),
                "hour": hour,
                "expected_coverage_pct": coverage_pct,
                "heuristic_score": round(heuristic_score_here, 4),
                "confidence": info["confidence"],
                "basis": basis,
                "ml_features": [round(f, 4) for f in features],
            }
            _LOGGER.debug(
                "[%s] Step 6 -- suggestion: %s %02d:00, expected solar coverage %d%%, "
                "heuristic score %.3f, confidence %s, features %s",
                name, target_date, hour, coverage_pct, heuristic_score_here, info["confidence"],
                [round(f, 3) for f in features],
            )

            suggestion = runtime.store.add_suggestion(
                target_date=target_date, suggested_hour=hour,
                expected_coverage_pct=coverage_pct, confidence=info["confidence"], basis=basis,
                features=features, heuristic_score=heuristic_score_here,
            )
            runtime.latest_recommendation = {
                "target_date": target_date, "hour": hour,
                "expected_coverage_pct": coverage_pct, "confidence": info["confidence"], "basis": basis,
            }
            async_dispatcher_send(hass, f"{SIGNAL_RECOMMENDATION_UPDATED}_{eid}")

            examples = _training_examples(runtime.store.suggestions)
            ml_trace: dict = {"examples": 0, "outcome": "skipped: no resolved suggestions yet"}
            if examples:
                runtime.store.ml_model = ml.maybe_train_and_promote(examples, runtime.store.ml_model, trace=ml_trace)
            trace["ml_training"] = ml_trace
            _LOGGER.debug("[%s] Step 7 -- ML training: %s", name, ml_trace)

            await runtime.store.async_save()
            if info["cfg"].fire_events:
                hass.bus.async_fire(EVENT_SUGGESTION_READY, {
                    "entry_id": eid, "name": info["cfg"].name,
                    "target_date": target_date.isoformat(), "hour": hour,
                    "expected_coverage_pct": coverage_pct,
                })
            await async_notify(hass, runtime, suggestion)

    _LOGGER.debug("=== Daily scheduling finished ===")


def _training_examples(suggestions) -> list[dict]:
    examples = []
    for s in suggestions:
        if s.status in ("matched", "accepted"):
            label = 1
        elif s.status in ("missed", "overridden"):
            label = 0
        else:
            continue
        if not s.features:
            continue
        examples.append({"features": s.features, "label": label, "heuristic_score": s.heuristic_score})
    return examples
