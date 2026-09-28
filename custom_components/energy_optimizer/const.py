"""Constants for the Energy Optimizer integration."""

DOMAIN = "energy_optimizer"
STORAGE_VERSION = 1

PLATFORMS = ["sensor"]

# --- config keys (set once, in the initial config flow step) ---
CONF_NAME = "name"
CONF_APPLIANCE_KIND = "appliance_kind"
CONF_POWER_ENTITY = "power_entity"
CONF_ENERGY_ENTITY = "energy_entity"
CONF_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"

# --- option keys (editable later via the options flow) ---
CONF_START_THRESHOLD_W = "start_threshold_w"
CONF_START_DURATION_S = "start_duration_s"
CONF_END_THRESHOLD_W = "end_threshold_w"
CONF_END_DURATION_S = "end_duration_s"
CONF_ALLOWED_START = "allowed_window_start"
CONF_ALLOWED_END = "allowed_window_end"
CONF_QUIET_START = "quiet_hours_start"
CONF_QUIET_END = "quiet_hours_end"
CONF_PREF_WEIGHT = "preference_weight"
CONF_NOTIFY_TARGETS = "notify_targets"
CONF_NOTIFY_ACTION = "notify_action"
CONF_FIRE_EVENTS = "fire_events"
CONF_BINARY_ENTITY = "binary_entity"

APPLIANCE_KINDS = {
    "dishwasher": {"icon": "mdi:dishwasher", "default_duration_min": 120},
    "washing_machine": {"icon": "mdi:washing-machine", "default_duration_min": 100},
    "dryer": {"icon": "mdi:tumble-dryer", "default_duration_min": 60},
    "other": {"icon": "mdi:power-plug", "default_duration_min": 90},
}

DEFAULT_START_THRESHOLD_W = 15.0
DEFAULT_START_DURATION_S = 20
DEFAULT_END_THRESHOLD_W = 8.0
DEFAULT_END_DURATION_S = 120
DEFAULT_ALLOWED_START = "06:00"
DEFAULT_ALLOWED_END = "22:00"
DEFAULT_QUIET_START = "22:00"
DEFAULT_QUIET_END = "06:00"
DEFAULT_PREF_WEIGHT = 0.5

MIN_SAMPLES_FOR_ML = 30
ML_AUC_PROMOTION_MARGIN = 0.03

RECENCY_HALF_LIFE_DAYS = 45
MAX_LOOKAHEAD_DAYS = 3
LOOKAHEAD_IMPROVEMENT_THRESHOLD = 0.15
CAPACITY_SLACK = 1.15

SIGNAL_RECOMMENDATION_UPDATED = f"{DOMAIN}_recommendation_updated"

EVENT_CYCLE_STARTED = f"{DOMAIN}_cycle_started"
EVENT_CYCLE_ENDED = f"{DOMAIN}_cycle_ended"
EVENT_SUGGESTION_READY = f"{DOMAIN}_suggestion_ready"
EVENT_SUGGESTION_ACCEPTED = f"{DOMAIN}_suggestion_accepted"
EVENT_SUGGESTION_OVERRIDDEN = f"{DOMAIN}_suggestion_overridden"

SERVICE_ACCEPT_SUGGESTION = "accept_suggestion"
SERVICE_PICK_DIFFERENT_TIME = "pick_different_time"
SERVICE_FORCE_RECOMPUTE = "force_recompute"
SERVICE_RUN_BACKTEST = "run_backtest"

DATA_RUNTIME = "runtime"
DATA_UNSUB_DAILY = "unsub_daily_job"
