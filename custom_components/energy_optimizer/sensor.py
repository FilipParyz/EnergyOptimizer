"""Sensor platform: one recommended-start entity per configured appliance."""
from __future__ import annotations

from datetime import datetime, time

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.util.dt as dt_util

from .const import DOMAIN, APPLIANCE_KINDS, DATA_RUNTIME, SIGNAL_RECOMMENDATION_UPDATED


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                              async_add_entities: AddEntitiesCallback) -> None:
    runtime = hass.data[DOMAIN][DATA_RUNTIME][entry.entry_id]
    async_add_entities([RecommendedStartSensor(runtime)])


class RecommendedStartSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Recommended start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_should_poll = False

    def __init__(self, runtime):
        self._runtime = runtime
        cfg = runtime.config
        self._attr_unique_id = f"{cfg.entry_id}_recommended_start"
        self._attr_icon = APPLIANCE_KINDS.get(cfg.kind, APPLIANCE_KINDS["other"])["icon"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cfg.entry_id)},
            name=cfg.name,
            manufacturer="Energy Optimizer",
            model=cfg.kind.replace("_", " ").title(),
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_RECOMMENDATION_UPDATED}_{self._runtime.entry.entry_id}",
                self._handle_recommendation_updated,
            )
        )

    @callback
    def _handle_recommendation_updated(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> datetime | None:
        rec = self._runtime.latest_recommendation
        if not rec:
            return None
        tz = dt_util.now().tzinfo
        local_dt = datetime.combine(rec["target_date"], time(hour=rec["hour"]), tzinfo=tz)
        return dt_util.as_utc(local_dt)

    @property
    def extra_state_attributes(self) -> dict:
        rec = self._runtime.latest_recommendation
        if not rec:
            return {"sample_count": len(self._runtime.store.runs)}
        return {
            "expected_solar_coverage_pct": rec["expected_coverage_pct"],
            "confidence": rec["confidence"],
            "basis": rec["basis"],
            "sample_count": len(self._runtime.store.runs),
        }
