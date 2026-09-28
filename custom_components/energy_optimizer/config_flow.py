"""Config flow for Energy Optimizer. Each config entry is one physical
appliance; add as many as you have."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    APPLIANCE_KINDS,
    CONF_NAME,
    CONF_APPLIANCE_KIND,
    CONF_POWER_ENTITY,
    CONF_ENERGY_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_START_THRESHOLD_W,
    CONF_START_DURATION_S,
    CONF_END_THRESHOLD_W,
    CONF_END_DURATION_S,
    CONF_ALLOWED_START,
    CONF_ALLOWED_END,
    CONF_QUIET_START,
    CONF_QUIET_END,
    CONF_PREF_WEIGHT,
    CONF_NOTIFY_TARGETS,
    CONF_NOTIFY_ACTION,
    CONF_FIRE_EVENTS,
    CONF_BINARY_ENTITY,
    DEFAULT_START_THRESHOLD_W,
    DEFAULT_START_DURATION_S,
    DEFAULT_END_THRESHOLD_W,
    DEFAULT_END_DURATION_S,
    DEFAULT_ALLOWED_START,
    DEFAULT_ALLOWED_END,
    DEFAULT_QUIET_START,
    DEFAULT_QUIET_END,
    DEFAULT_PREF_WEIGHT,
)


class EnergyOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_APPLIANCE_KIND]}_{user_input[CONF_POWER_ENTITY]}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get(CONF_NAME)
                or user_input[CONF_APPLIANCE_KIND].replace("_", " ").title(),
                data=user_input,
            )

        schema = vol.Schema({
            vol.Required(CONF_NAME): str,
            vol.Required(CONF_APPLIANCE_KIND, default="dishwasher"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(APPLIANCE_KINDS.keys()),
                    translation_key=CONF_APPLIANCE_KIND,
                )
            ),
            vol.Required(CONF_POWER_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power")
            ),
            vol.Optional(CONF_ENERGY_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="energy")
            ),
            vol.Optional(CONF_SOLAR_FORECAST_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EnergyOptimizerOptionsFlow()


class EnergyOptimizerOptionsFlow(config_entries.OptionsFlow):

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema({
            vol.Optional(
                CONF_START_THRESHOLD_W, default=opts.get(CONF_START_THRESHOLD_W, DEFAULT_START_THRESHOLD_W)
            ): vol.Coerce(float),
            vol.Optional(
                CONF_START_DURATION_S, default=opts.get(CONF_START_DURATION_S, DEFAULT_START_DURATION_S)
            ): vol.Coerce(int),
            vol.Optional(
                CONF_END_THRESHOLD_W, default=opts.get(CONF_END_THRESHOLD_W, DEFAULT_END_THRESHOLD_W)
            ): vol.Coerce(float),
            vol.Optional(
                CONF_END_DURATION_S, default=opts.get(CONF_END_DURATION_S, DEFAULT_END_DURATION_S)
            ): vol.Coerce(int),
            vol.Optional(
                CONF_BINARY_ENTITY, default=opts.get(CONF_BINARY_ENTITY, None)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
            vol.Optional(
                CONF_ALLOWED_START,
                default=opts.get(
                    CONF_ALLOWED_START,
                    DEFAULT_ALLOWED_START,
                ),
            ): selector.TimeSelector(),
            vol.Optional(
                CONF_ALLOWED_END,
                default=opts.get(
                    CONF_ALLOWED_END,
                    DEFAULT_ALLOWED_END,
                ),
            ): selector.TimeSelector(),
            vol.Optional(
                CONF_QUIET_START,
                default=opts.get(
                    CONF_QUIET_START,
                    DEFAULT_QUIET_START,
                ),
            ): selector.TimeSelector(),
            vol.Optional(
                CONF_QUIET_END,
                default=opts.get(
                    CONF_QUIET_END,
                    DEFAULT_QUIET_END,
                ),
            ): selector.TimeSelector(),
            vol.Optional(
                CONF_PREF_WEIGHT, default=opts.get(CONF_PREF_WEIGHT, DEFAULT_PREF_WEIGHT)
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
            vol.Optional(
                CONF_NOTIFY_TARGETS, default=opts.get(CONF_NOTIFY_TARGETS, [])
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="notify", multiple=True)),
            vol.Optional(
                CONF_NOTIFY_ACTION, default=opts.get(CONF_NOTIFY_ACTION, [])
            ): selector.ActionSelector(),
            vol.Optional(
                CONF_FIRE_EVENTS, default=opts.get(CONF_FIRE_EVENTS, True)
            ): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
