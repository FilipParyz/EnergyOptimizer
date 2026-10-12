"""Diagnostics for Energy Optimizer.

Settings -> Devices & Services -> Energy Optimizer -> (appliance) -> "..." ->
Download diagnostics. Produces a JSON snapshot with the configuration, the
detector's live state, the stored runs and suggestions, the ML model and --
most usefully -- every intermediate value from the most recent scheduling
run (runtime.last_trace) and the most recent finished cycle.

The trace lives in memory only: after a restart it is empty until the next
20:00 job or a manual energy_optimizer.force_recompute.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, DATA_RUNTIME, MAX_LOOKAHEAD_DAYS
from .coordinator import _forecast_curve
from .debug import json_safe, rounded

RECENT_RUNS = 30
RECENT_SUGGESTIONS = 30


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    runtime = hass.data.get(DOMAIN, {}).get(DATA_RUNTIME, {}).get(entry.entry_id)
    if runtime is None:
        return {"error": "config entry is not loaded"}

    cfg = runtime.config
    store = runtime.store
    det = runtime.detector
    model = store.ml_model

    return json_safe({
        "config": asdict(cfg),
        "detector": {
            "state": det.state,
            "state_since": det._state_since,
            "cycle_start": det._cycle_start,
            "energy_wh_so_far": round(det._energy_wh, 1),
            "last_reading": det._last_reading,
        },
        "store": {
            "backfill_done": store.backfill_done,
            "run_count": len(store.runs),
            "runs_by_source": dict(Counter(r.source for r in store.runs)),
            "suggestion_count": len(store.suggestions),
            "suggestions_by_status": dict(Counter(s.status for s in store.suggestions)),
            "recent_runs": [asdict(r) for r in store.runs[-RECENT_RUNS:]],
            "recent_suggestions": [asdict(s) for s in store.suggestions[-RECENT_SUGGESTIONS:]],
        },
        "ml_model": None if not model else {
            "promoted": model.get("promoted", False),
            "n_samples": model.get("n_samples"),
            "auc": model.get("auc"),
            "baseline_auc": model.get("baseline_auc"),
            "weights": model.get("weights"),
        },
        "latest_recommendation": runtime.latest_recommendation,
        "solar_forecast_now": {
            f"D+{offset}": rounded(_forecast_curve(hass, cfg.solar_forecast_entity, offset), 1)
            for offset in range(1, MAX_LOOKAHEAD_DAYS + 1)
        },
        "last_scheduling_trace": runtime.last_trace,
        "last_cycle_trace": runtime.last_cycle_trace,
    })
