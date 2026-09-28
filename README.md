# Energy Optimizer

A Home Assistant custom integration (install via HACS as a custom
repository) that learns when you usually run the dishwasher, washing
machine, or dryer, and recommends the best time to start it tomorrow --
weighing your habits against the solar production forecast, how overdue the
load is, and what your other appliances are already doing.

## What it does

- Detects appliance start/end using a simple power-threshold detector (no
  program identification -- it only needs to know *when*, not *what*).
- Backfills historical data on first setup, preferring long-term recorder
  statistics (deep, hourly resolution) over raw state history (shallow,
  precise) -- see `backfill.py`.
- Builds a recency-weighted habit model of which hour you usually start each
  appliance, blended with a solar-alignment score computed from your solar
  forecast entity.
- Tracks how overdue each appliance is relative to its typical usage
  interval, and will recommend waiting a day if the forecast improves
  meaningfully *and* it's not overdue.
- Jointly schedules multiple appliances so they don't all pile onto the same
  sunny hour -- a greedy, urgency-first assignment against a shared hourly
  solar budget.
- Optionally trains a small NumPy logistic-regression "personalization"
  layer on your accept/override history, but only ever activates it if a
  held-out validation check shows it beats the plain heuristic. Until then
  (and for most households, that may be a while -- appliance usage doesn't
  generate much data per week), you're running on the heuristic alone, and
  the entity attributes will say so (`confidence: baseline`).
- Sends notifications three ways -- explicit `notify.*` targets, a free-form
  custom action sequence, or bus events for your own automations -- falling
  back to a persistent notification if nothing is configured.
- No auto-start in this version: notify-only, by design.

## Installation

1. HACS -> the "..." menu (top right) -> **Custom repositories** -> add this
   repository URL, category **Integration**.
2. Install "Energy Optimizer" from HACS, restart Home Assistant.
3. Settings -> Devices & Services -> **Add Integration** -> "Energy
   Optimizer".
4. Repeat step 3 once per physical appliance (dishwasher, washing machine,
   dryer, ...).

## Configuration

Per appliance, at setup:

| Field | Notes |
|---|---|
| Power sensor (W) | Required. Used for live start/end detection. |
| Energy sensor (kWh) | Optional but strongly recommended -- gives a much deeper historical backfill via long-term statistics instead of the ~10-day raw history window. |
| Solar forecast entity | Optional. Works with Forecast.Solar, Solcast, or similar -- see the note below on forecast parsing. |

Editable afterward via the entry's **Configure** button: start/end detection
thresholds, allowed start window, quiet hours (default 22:00-06:00), the
habit-vs-solar weighting slider, notify targets, a custom notification
action, and whether to fire bus events.

## Dashboard card

The integration ships its own Lovelace card and loads it automatically --
no need to add a dashboard resource by hand. After installing and
restarting Home Assistant (refresh the browser once if the card doesn't
show up), add it to a dashboard:

```yaml
type: custom:energy-optimizer-card
entity: sensor.dishwasher_recommended_start
```

The card shows the recommended start time, expected solar coverage and
confidence, plus **Accept** / **Pick different time** buttons that call the
services below.

## Entities & services

- `sensor.<appliance>_recommended_start` -- a timestamp, with
  `expected_solar_coverage_pct`, `confidence` (`baseline`/`personalized`),
  `basis`, and `sample_count` attributes.
- `energy_optimizer.accept_suggestion` / `energy_optimizer.pick_different_time`
  -- called by the dashboard card's buttons or a mobile notification action;
  resolve the current pending suggestion and feed the personalization model.
- `energy_optimizer.force_recompute` -- manually re-run the daily scheduling
  job instead of waiting for the scheduled 20:00 run. Useful while testing.
- `energy_optimizer.run_backtest` -- manually run the backtest to evaluate
  historic runs. Results are posted as persistent notification. Does not 
  affect live suggestions or stored data.
- Bus events (if enabled): `energy_optimizer_cycle_ended`,
  `energy_optimizer_suggestion_ready`, `energy_optimizer_suggestion_accepted`,
  `energy_optimizer_suggestion_overridden`.
