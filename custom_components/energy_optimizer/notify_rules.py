"""Three-tier notification dispatch.

Tier 1 uses configured notify entities. For Home Assistant Companion App notify
entities it resolves the corresponding legacy ``notify.mobile_app_*`` action so
that actionable-notification payloads (Accept / Pick different time) can be
sent. The generic entity-based ``notify.send_message`` action currently accepts
only ``message`` and optional ``title``, so non-Mobile-App notify entities fall
back to a basic notification without buttons.
Tier 2 executes a free-form custom action sequence (full automation-editor
flexibility).
Tier 3 bus events are fired by the caller in coordinator.py, not
here.
If neither Tier 1 nor Tier 2 is configured, a persistent notification is
created so a suggestion is never silently lost.
"""
from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.script import Script
from homeassistant.util import slugify

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _mobile_app_notify_service(hass: HomeAssistant, entity_id: str) -> str | None:
    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)
    if (
        entity_entry is None
        or entity_entry.platform != "mobile_app"
        or not entity_entry.config_entry_id
    ):
        return None

    config_entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)
    if config_entry is None:
        return None

    device_name = config_entry.data.get("device_name")
    if not device_name:
        return None

    service = slugify(f"mobile_app_{device_name}")
    if not hass.services.has_service("notify", service):
        _LOGGER.debug(
            "Mobile App notify service notify.%s for %s is not registered; "
            "falling back to notify.send_message without action buttons",
            service,
            entity_id,
        )
        return None

    return service


async def async_notify(hass: HomeAssistant, runtime, suggestion) -> None:
    cfg = runtime.config
    title = f"{cfg.name}: best time"
    message = (
        f"Suggested start {suggestion.suggested_hour:02d}:00 on {suggestion.target_date} "
        f"(~{suggestion.expected_coverage_pct}% solar coverage). {suggestion.basis}"
    )
    tag = f"{DOMAIN}_{runtime.entry.entry_id}_suggestion"

    accept_action = f"{DOMAIN}_accept_{runtime.entry.entry_id}"
    change_action = f"{DOMAIN}_change_{runtime.entry.entry_id}"
    mobile_actions = [
        {"action": accept_action, "title": "Accept"},
        {"action": change_action, "title": "Reject"},
    ]

    sent_something = False

    for target_entity in cfg.notify_targets:
        try:
            if mobile_service := _mobile_app_notify_service(hass, target_entity):
                await hass.services.async_call(
                    "notify",
                    mobile_service,
                    {
                        "title": title,
                        "message": message,
                        "data": {
                            "tag": tag,
                            "actions": mobile_actions,
                        },
                    },
                    blocking=False,
                )
            else:
                await hass.services.async_call(
                    "notify",
                    "send_message",
                    {
                        "entity_id": target_entity,
                        "title": title,
                        "message": message,
                    },
                    blocking=False,
                )
            sent_something = True
        except Exception:
            _LOGGER.warning("Failed to notify via %s", target_entity, exc_info=True)

    if cfg.notify_action:
        script_obj = Script(hass, cfg.notify_action, f"{DOMAIN}_notify_action", DOMAIN)
        await script_obj.async_run(
            run_variables={
                "entry_id": runtime.entry.entry_id,
                "name": cfg.name,
                "suggested_hour": suggestion.suggested_hour,
                "target_date": suggestion.target_date,
                "expected_coverage_pct": suggestion.expected_coverage_pct,
                "confidence": suggestion.confidence,
                "basis": suggestion.basis,
                "accept_action": accept_action,
                "change_action": change_action,
                "notification_tag": tag,
            },
            context=Context(),
        )
        sent_something = True

    if not sent_something:
        persistent_notification.async_create(hass, message, title=title, notification_id=tag)
