"""Runtime orchestration: one ApplianceRuntime per configured appliance,
plus the single shared daily job that scores every appliance and then
jointly assigns them against a shared solar budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
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
    APPLIANCE_KINDS, MAX_LOOKAHEAD_DAYS, DATA_RUNTIME,
)
from .detector import CycleDetector
from .store import ApplianceStore
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
        )
        self.latest_recommendation: dict | None = None
        self._unsub_state = None

    async def async_setup(self) -> None:
        await self.store.async_load()
        if self.store.suggestions:
            self.latest_recommendation = _recommendation_from_suggestion(self.store.suggestions[-1])
        self._unsub_state = async_track_state_change_event(
            self.hass, [self.config.power_entity], self._handle_power_change
        )
        if not self.store.backfill_done:
            self.hass.async_create_task(self._async_backfill())

    async def async_unload(self) -> None:
        if self._unsub_state:
            self._unsub_state()

    async def _async_backfill(self) -> None:
        try:
            runs = await backfill.backfill_runs(
                self.hass, self.config.power_entity, self.config.energy_entity,
                detector_config={
                    "start_threshold_w": self.config.start_threshold_w,
                    "start_duration_s": self.config.start_duration_s,
                    "end_threshold_w": self.config.end_threshold_w,
                    "end_duration_s": self.config.end_duration_s,
                },
                binary_entity=self.config.binary_entity,
            )
            self.store.runs = [r for r in self.store.runs if r.source != "backfill"]
            for run in runs:
                self.store.add_run(run.start_time, run.end_time, run.duration_s, run.energy_wh, source="backfill")
            self.store.backfill_done = True
            await self.store.async_save()
            _LOGGER.info("%s: backfilled %d historical run(s)", self.config.name, len(runs))
            async_dispatcher_send(self.hass, f"{SIGNAL_RECOMMENDATION_UPDATED}_{self.entry.entry_id}")
        except Exception:
            _LOGGER.exception(
                "%s: historical backfill failed; continuing with live tracking only", self.config.name
            )

    @callback
    def _handle_power_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        try:
            power_w = float(new_state.state)
        except (TypeError, ValueError):
            return
        result = self.detector.process_reading(dt_util.utcnow(), power_w)
        if result is None:
            return
        self.hass.async_create_task(self._async_handle_cycle_end(result))

    async def _async_handle_cycle_end(self, result) -> None:
        self.store.add_run(result.start_time, result.end_time, result.duration_s, result.energy_wh, source="live")
        self._reconcile_suggestion(result)
        await self.store.async_save()
        async_dispatcher_send(self.hass, f"{SIGNAL_RECOMMENDATION_UPDATED}_{self.entry.entry_id}")
        if self.config.fire_events:
            self.hass.bus.async_fire(EVENT_CYCLE_ENDED, {
                "entry_id": self.entry.entry_id,
                "name": self.config.name,
                "start_time": dt_util.as_local(result.start_time).isoformat(),
                "duration_s": result.duration_s,
                "energy_wh": result.energy_wh,
            })

    def _reconcile_suggestion(self, result) -> None:
        local_start = dt_util.as_local(result.start_time)
        suggestion = self.store.suggestion_for_cycle(local_start.date())
        if suggestion is None:
            return
        target = date.fromisoformat(suggestion.target_date)
        matched = local_start.date() == target and abs(local_start.hour - suggestion.suggested_hour) <= 1
        suggestion.status = "matched" if matched else "missed"
        suggestion.actual_start_hour = local_start.hour
        suggestion.resolved_at = dt_util.now().isoformat()


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


def _config_from_entry(entry: ConfigEntry) -> ApplianceConfig:
    data, opts = entry.data, entry.options
    return ApplianceConfig(
        entry_id=entry.entry_id,
        name=data.get(CONF_NAME) or entry.title,
        kind=data.get(CONF_APPLIANCE_KIND, "other"),
        power_entity=data[CONF_POWER_ENTITY],
        energy_entity=data.get(CONF_ENERGY_ENTITY),
        solar_forecast_entity=data.get(CONF_SOLAR_FORECAST_ENTITY),
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
        binary_entity=opts.get(CONF_BINARY_ENTITY) or None,
    )


def _forecast_curve(hass: HomeAssistant, forecast_entity: str | None, day_offset: int) -> dict[int, float]:
    """Best-effort read of an hourly forecast curve from a forecast entity's
    attributes. Different solar forecast integrations expose this
    differently -- this tries a few common attribute shapes and degrades to
    an empty curve (pure-habit scheduling) if none match."""
    if not forecast_entity:
        return {}
    state = hass.states.get(forecast_entity)
    if not state:
        return {}
    today = dt_util.now().date()

    for attr_name in ("wh_hours", "detailedForecast", "detailedHourly", "hourly_forecast", "forecast"):
        raw = state.attributes.get(attr_name)

        if isinstance(raw, dict):
            curve: dict[int, float] = {}
            for key, val in raw.items():
                parsed = dt_util.parse_datetime(str(key))
                if parsed and (dt_util.as_local(parsed).date() - today).days == day_offset:
                    curve[dt_util.as_local(parsed).hour] = float(val)
            if curve:
                return curve

        if isinstance(raw, list):
            curve = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                ts = item.get("period_start") or item.get("datetime") or item.get("time")
                val = item.get("pv_estimate") or item.get("value") or item.get("wh")
                parsed = dt_util.parse_datetime(str(ts)) if ts else None
                if parsed and val is not None and (dt_util.as_local(parsed).date() - today).days == day_offset:
                    curve[dt_util.as_local(parsed).hour] = float(val)
            if curve:
                return curve

    return {}


def _typical_duration_hours(runs, kind: str) -> float:
    durations = [r.duration_s / 3600 for r in runs if r.duration_s > 0]
    if durations:
        return sum(durations) / len(durations)
    return APPLIANCE_KINDS.get(kind, APPLIANCE_KINDS["other"])["default_duration_min"] / 60


def _typical_power_w(runs, duration_hours: float) -> float:
    powers = [r.energy_wh / (r.duration_s / 3600) for r in runs if r.duration_s > 0 and r.energy_wh > 0]
    if powers:
        return sum(powers) / len(powers)
    return 0.0


async def async_run_daily_scheduling(hass: HomeAssistant) -> None:
    """The one shared job that scores every configured appliance and then
    jointly assigns them against a shared solar budget."""
    runtimes: dict[str, ApplianceRuntime] = hass.data[DOMAIN][DATA_RUNTIME]
    today = dt_util.now().date()

    per_appliance: dict[str, dict] = {}

    for entry_id, runtime in runtimes.items():
        cfg = runtime.config
        runtime.store.expire_stale_suggestions(today)
        hours = scheduler.allowed_hours(cfg.allowed_start, cfg.allowed_end, cfg.quiet_start, cfg.quiet_end)
        if not hours:
            continue
        urgency_info = scheduler.compute_urgency(runtime.store.runs, today)
        duration_hours = _typical_duration_hours(runtime.store.runs, cfg.kind)
        avg_power_w = _typical_power_w(runtime.store.runs, duration_hours)

        day_best: dict[int, tuple[int, float]] = {}
        day_blended: dict[int, dict[int, float]] = {}
        forecasts: dict[int, dict[int, float]] = {}

        for offset in range(1, MAX_LOOKAHEAD_DAYS + 1):
            forecast = _forecast_curve(hass, cfg.solar_forecast_entity, offset)
            if not forecast and offset > 1:
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

        if not day_best:
            continue

        chosen_offset, reason = scheduler.choose_target_day(day_best, urgency_info)
        target_date = today + timedelta(days=chosen_offset)

        per_appliance[entry_id] = {
            "cfg": cfg, "runtime": runtime, "urgency_info": urgency_info,
            "duration_hours": duration_hours, "avg_power_w": avg_power_w,
            "target_date": target_date, "reason": reason,
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
        assignment = scheduler.joint_schedule(candidates, shared_forecast)

        for eid in entry_ids:
            if eid not in assignment:
                continue
            info = per_appliance[eid]
            runtime = info["runtime"]
            hour, _adj_score = assignment[eid]

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

            suggestion = runtime.store.add_suggestion(
                target_date=target_date, suggested_hour=hour,
                expected_coverage_pct=coverage_pct, confidence=info["confidence"], basis=info["reason"],
                features=features, heuristic_score=heuristic_score_here,
            )
            runtime.latest_recommendation = {
                "target_date": target_date, "hour": hour,
                "expected_coverage_pct": coverage_pct, "confidence": info["confidence"], "basis": info["reason"],
            }
            async_dispatcher_send(hass, f"{SIGNAL_RECOMMENDATION_UPDATED}_{eid}")

            examples = _training_examples(runtime.store.suggestions)
            if examples:
                runtime.store.ml_model = ml.maybe_train_and_promote(examples, runtime.store.ml_model)

            await runtime.store.async_save()
            if info["cfg"].fire_events:
                hass.bus.async_fire(EVENT_SUGGESTION_READY, {
                    "entry_id": eid, "name": info["cfg"].name,
                    "target_date": target_date.isoformat(), "hour": hour,
                    "expected_coverage_pct": coverage_pct,
                })
            await async_notify(hass, runtime, suggestion)


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
