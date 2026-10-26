"""The Energy Optimizer integration."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import persistent_notification
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event, ServiceCall
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.event import async_track_time_change
import homeassistant.util.dt as dt_util

from . import backfill, backtest, scheduler
from .const import (
    DOMAIN, PLATFORMS, DATA_RUNTIME, DATA_UNSUB_DAILY,
    SERVICE_ACCEPT_SUGGESTION, SERVICE_PICK_DIFFERENT_TIME, SERVICE_FORCE_RECOMPUTE,
    SERVICE_RUN_BACKTEST, SERVICE_RERUN_BACKFILL,
    EVENT_SUGGESTION_ACCEPTED, EVENT_SUGGESTION_OVERRIDDEN,
)
from .coordinator import ApplianceRuntime, async_run_daily_scheduling, _typical_duration_hours, _typical_power_w

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "energy-optimizer-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"
DATA_CARD_REGISTERED = f"{DOMAIN}_card_registered"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
DATA_UNSUB_MOBILE = "unsub_mobile_action"
BACKFILL_MODES = ("replace", "merge")

async def _async_register_card(hass: HomeAssistant) -> None:
    if hass.data.get(DATA_CARD_REGISTERED):
        return
    hass.data[DATA_CARD_REGISTERED] = True
    card_path = Path(__file__).parent / "frontend" / CARD_FILENAME
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(card_path), cache_headers=False)]
    )
    add_extra_js_url(hass, CARD_URL)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await _async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {DATA_RUNTIME: {}})

    runtime = ApplianceRuntime(hass, entry)
    await runtime.async_setup()
    hass.data[DOMAIN][DATA_RUNTIME][entry.entry_id] = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _async_ensure_shared_jobs(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        runtime = hass.data[DOMAIN][DATA_RUNTIME].pop(entry.entry_id, None)
        if runtime:
            await runtime.async_unload()
    if not hass.data[DOMAIN][DATA_RUNTIME]:
        for key in (DATA_UNSUB_DAILY, DATA_UNSUB_MOBILE):
            unsub = hass.data[DOMAIN].pop(key, None)
            if unsub:
                unsub()
    return ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _async_ensure_shared_jobs(hass: HomeAssistant) -> None:
    if DATA_UNSUB_DAILY in hass.data[DOMAIN]:
        return

    async def _daily(_now) -> None:
        await async_run_daily_scheduling(hass)

    hass.data[DOMAIN][DATA_UNSUB_DAILY] = async_track_time_change(
        hass, _daily, hour=20, minute=0, second=0
    )

    async def _handle_mobile_action(event: Event) -> None:
        action = event.data.get("action", "")
        if action.startswith(f"{DOMAIN}_accept_"):
            await _accept_via_entry_id(hass, action[len(f"{DOMAIN}_accept_"):])
        elif action.startswith(f"{DOMAIN}_change_"):
            await _change_via_entry_id(hass, action[len(f"{DOMAIN}_change_"):])

    hass.data[DOMAIN][DATA_UNSUB_MOBILE] = hass.bus.async_listen(
        "mobile_app_notification_action", _handle_mobile_action
    )

    async def _accept_service(call: ServiceCall) -> None:
        for entry_id in _entry_ids_from_call(hass, call):
            await _accept_via_entry_id(hass, entry_id)

    async def _pick_service(call: ServiceCall) -> None:
        for entry_id in _entry_ids_from_call(hass, call):
            await _change_via_entry_id(hass, entry_id)

    async def _recompute_service(call: ServiceCall) -> None:
        await async_run_daily_scheduling(hass)

    async def _backtest_service(call: ServiceCall) -> None:
        await _run_backtest_service(hass, call)

    async def _rerun_backfill_service(call: ServiceCall) -> None:
        await _run_rerun_backfill_service(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_ACCEPT_SUGGESTION, _accept_service)
    hass.services.async_register(DOMAIN, SERVICE_PICK_DIFFERENT_TIME, _pick_service)
    hass.services.async_register(DOMAIN, SERVICE_FORCE_RECOMPUTE, _recompute_service)
    hass.services.async_register(DOMAIN, SERVICE_RUN_BACKTEST, _backtest_service)
    hass.services.async_register(DOMAIN, SERVICE_RERUN_BACKFILL, _rerun_backfill_service)


def _entry_ids_from_call(hass: HomeAssistant, call: ServiceCall) -> list[str]:
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    registry = er.async_get(hass)
    entry_ids = []
    for entity_id in entity_ids:
        entity_entry = registry.async_get(entity_id)
        if entity_entry and entity_entry.config_entry_id:
            entry_ids.append(entity_entry.config_entry_id)
    return entry_ids


async def _accept_via_entry_id(hass: HomeAssistant, entry_id: str) -> None:
    runtime = hass.data.get(DOMAIN, {}).get(DATA_RUNTIME, {}).get(entry_id)
    if not runtime:
        return
    pending = runtime.store.latest_pending_suggestion()
    if not pending:
        return
    pending.status = "accepted"
    pending.resolved_at = dt_util.now().isoformat()
    await runtime.store.async_save()
    if runtime.config.fire_events:
        hass.bus.async_fire(EVENT_SUGGESTION_ACCEPTED, {"entry_id": entry_id, "name": runtime.config.name})


async def _change_via_entry_id(hass: HomeAssistant, entry_id: str) -> None:
    runtime = hass.data.get(DOMAIN, {}).get(DATA_RUNTIME, {}).get(entry_id)
    if not runtime:
        return
    pending = runtime.store.latest_pending_suggestion()
    if pending:
        pending.status = "overridden"
        pending.resolved_at = dt_util.now().isoformat()
    await runtime.store.async_save()
    if runtime.config.fire_events:
        hass.bus.async_fire(EVENT_SUGGESTION_OVERRIDDEN, {"entry_id": entry_id, "name": runtime.config.name})


async def _run_rerun_backfill_service(hass: HomeAssistant, call: ServiceCall) -> None:
    runtimes: dict[str, ApplianceRuntime] = hass.data.get(DOMAIN, {}).get(DATA_RUNTIME, {})
    entry_ids = _entry_ids_from_call(hass, call) or list(runtimes)
    source = call.data.get("source", backfill.SOURCE_AUTO)
    mode = call.data.get("mode", "replace")
    days = call.data.get("days")
    days = int(days) if days else None

    if source not in backfill.SOURCES or mode not in BACKFILL_MODES:
        persistent_notification.async_create(
            hass,
            f"Invalid arguments: source must be one of {', '.join(backfill.SOURCES)}, "
            f"mode one of {', '.join(BACKFILL_MODES)}.",
            title="Energy Optimizer: backfill not run",
            notification_id=f"{DOMAIN}_backfill_result",
        )
        return

    rows: list[str] = []
    for entry_id in entry_ids:
        runtime = runtimes.get(entry_id)
        if runtime is None:
            continue
        try:
            s = await runtime.async_rerun_backfill(source=source, days=days, mode=mode)
        except ValueError as err:  # e.g. source=binary_sensor with none configured
            rows.append(f"| {runtime.config.name} | {source} | - | - | - | - | - | {err} |")
            continue
        except Exception as err:  # keep going for the other appliances
            _LOGGER.exception("%s: rerun_backfill failed", runtime.config.name)
            rows.append(f"| {runtime.config.name} | {source} | - | - | - | - | - | error: {err} |")
            continue
        rows.append(
            f"| {s['name']} | {s['source']} | {s['window_start'][:10]} | {s['found']} | {s['removed']} | "
            f"{s['added']} | {s['skipped_overlap']} / {s['skipped_short']} | {s['total_runs']} |"
        )

    header = (
        f"Mode: **{mode}**. Run `energy_optimizer.force_recompute` to refresh today's suggestions.\n\n"
        "| Appliance | Source | Window from | Found | Removed | Added | Skipped (overlap / <10 min) | Runs now |\n"
        "|---|---|---|---|---|---|---|---|"
    )
    persistent_notification.async_create(
        hass, header + "\n" + "\n".join(rows or ["| - | - | - | - | - | - | - | no appliances |"]),
        title="Energy Optimizer: backfill results",
        notification_id=f"{DOMAIN}_backfill_result",
    )


def _distinct_run_days(runs) -> int:
    days = set()
    for r in runs:
        parsed = dt_util.parse_datetime(r.start_time)
        if parsed is not None:
            days.add(parsed.date())
    return len(days)


async def _run_backtest_service(hass: HomeAssistant, call: ServiceCall) -> None:
    solar_energy_entity = call.data.get("solar_energy_entity")
    days = call.data.get("days", 90)
    min_history_days = call.data.get("min_history_days", 14)

    if not solar_energy_entity:
        _LOGGER.warning("run_backtest called without solar_energy_entity; aborting")
        persistent_notification.async_create(
            hass,
            "`solar_energy_entity` is required: point it at the sensor that records "
            "your actual (measured, not forecast) PV production -- e.g. your "
            "inverter's total energy sensor.",
            title="Energy Optimizer: backtest not run",
            notification_id=f"{DOMAIN}_backtest_result",
        )
        return

    actual_solar = await backtest.fetch_actual_solar_production(hass, solar_energy_entity, days=days)
    if not actual_solar:
        _LOGGER.warning(
            "run_backtest: no long-term statistics found for %s in the last %d day(s) "
            "-- check the entity id and that it has 'sum' statistics",
            solar_energy_entity, days,
        )
        persistent_notification.async_create(
            hass,
            f"No long-term statistics found for `{solar_energy_entity}` in the last "
            f"{days} day(s). Check the entity id, and that it has 'sum' statistics "
            f"(Developer Tools -> Statistics).",
            title="Energy Optimizer: backtest not run",
            notification_id=f"{DOMAIN}_backtest_result",
        )
        return

    runtimes: dict[str, ApplianceRuntime] = hass.data.get(DOMAIN, {}).get(DATA_RUNTIME, {})
    rows: list[str] = []
    any_results = False

    for runtime in runtimes.values():
        cfg = runtime.config
        hours = scheduler.allowed_hours(cfg.allowed_start, cfg.allowed_end, cfg.quiet_start, cfg.quiet_end)
        if not hours:
            continue

        run_days = _distinct_run_days(runtime.store.runs)
        duration_hours = _typical_duration_hours(runtime.store.runs, cfg.kind)
        avg_power_w = _typical_power_w(runtime.store.runs, duration_hours, cfg.kind)
        sample_day = max(actual_solar)
        _LOGGER.warning(
            "%s: duration_hours=%.2f avg_power_w=%.0f needed_wh=%.0f | solar %s: %s",
            cfg.name, duration_hours, avg_power_w, duration_hours * avg_power_w,
            sample_day, {h: round(v) for h, v in sorted(actual_solar.get(sample_day, {}).items())},
        )

        results = backtest.backtest_appliance(
            runtime.store.runs, actual_solar, duration_hours, avg_power_w, hours,
            cfg.pref_weight, min_history_days=min_history_days,
        )
        summary = backtest.summarize(results)

        if not summary:
            _LOGGER.info(
                "%s: not enough history for a backtest yet -- %d distinct day(s) with a "
                "recorded run out of %d total run(s) stored; need more than %d day(s)",
                cfg.name, run_days, len(runtime.store.runs), min_history_days,
            )
            rows.append(
                f"| {cfg.name} | {run_days} (need > {min_history_days}) | not enough history yet | - | - | - | - | - |"
            )
            continue

        any_results = True
        _LOGGER.info(
            "%s: backtest over %d day(s) -- match rate %.0f%%, mean recommended "
            "coverage %.0f%%, mean best-possible coverage %.0f%%, mean regret %.1fpp, "
            "mean actual coverage %.0f%%, mean gain %+.1fpp",
            cfg.name, summary["n_days"], summary["match_rate"] * 100,
            summary["mean_recommended_coverage_pct"], summary["mean_best_possible_coverage_pct"],
            summary["mean_regret_pct"], summary["mean_actual_coverage_pct"], summary["mean_gain_pct"],
        )
        rows.append(
            f"| {cfg.name} | {summary['n_days']} | {summary['match_rate'] * 100:.0f}% | "
            f"{summary['mean_regret_pct']:.1f} pp | {summary['mean_recommended_coverage_pct']:.0f}% | "
            f"{summary['mean_best_possible_coverage_pct']:.0f}% | "
            f"{summary['mean_actual_coverage_pct']:.0f}% | {summary['mean_gain_pct']:+.1f} pp |"
        )

    if not rows:
        message = "No configured appliances have an allowed scheduling window; nothing to backtest."
    else:
        header = (
            "| Appliance | Days evaluated | Match rate | Mean regret | Recommended coverage "
            "| Best possible coverage | Actual coverage | Mean gain |\n"
            "|---|---|---|---|---|---|---|---|"
        )
        message = header + "\n" + "\n".join(rows)
        if not any_results:
            message += (
                "\n\nNone of these have enough history yet -- each appliance needs more than "
                f"`min_history_days` ({min_history_days}) distinct day(s) of recorded runs before "
                "walk-forward evaluation can start scoring days."
            )

    persistent_notification.async_create(
        hass, message, title="Energy Optimizer: backtest results",
        notification_id=f"{DOMAIN}_backtest_result",
    )
