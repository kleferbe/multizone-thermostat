import voluptuous as vol
import homeassistant.helpers.config_validation as cv
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    PRESET_NONE,
    HVACMode,
)
from homeassistant.const import EVENT_HOMEASSISTANT_START, SERVICE_RELOAD
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.reload import async_reload_integration_platforms
from homeassistant.helpers.service import async_register_admin_service

from . import DOMAIN, PLATFORMS
from .const import *
from .zone_registry import async_get_registry

SUPPORTED_PRESET_MODES = [PRESET_NONE, PRESET_EMERGENCY, PRESET_RESTORE, PRESET_STANDBY]
SUPPORTED_HVAC_MODES = [HVACMode.HEAT, HVACMode.COOL, HVACMode.OFF]
PRESET_CHOICES = list(SUPPORTED_PRESET_MODES)


def remember_presets(custom_presets) -> None:
    """Add presets from the current YAML. The service schema holds this list."""
    for preset in custom_presets:
        if preset not in PRESET_CHOICES:
            PRESET_CHOICES.append(preset)


def clear_custom_presets() -> None:
    """Drop presets from the previous YAML. Built-in modes stay."""
    del PRESET_CHOICES[len(SUPPORTED_PRESET_MODES) :]


def register_services(custom_presets):
    remember_presets(custom_presets)
    platform = entity_platform.current_platform.get()
    assert platform

    platform.async_register_entity_service(  # type: ignore
        "set_preset_mode",
        {
            vol.Required(ATTR_PRESET_MODE): vol.In(PRESET_CHOICES),
            vol.Required(ATTR_HVAC_MODE): vol.In(SUPPORTED_HVAC_MODES),
        },
        "async_set_preset_mode",
    )

    platform.async_register_entity_service(  # type: ignore
        "detailed_output",
        {
            vol.Required(ATTR_HVAC_MODE): vol.In(SUPPORTED_HVAC_MODES),
            vol.Required("new_mode"): cv.boolean,
        },
        "set_detailed_output",
    )

    platform.async_register_entity_service(  # type: ignore
        "pwm_threshold",
        {
            vol.Required(ATTR_HVAC_MODE): vol.In(SUPPORTED_HVAC_MODES),
            vol.Required("new_threshold"): vol.Coerce(float),
        },
        "async_set_pwm_threshold",
    )

    platform.async_register_entity_service(  # type: ignore
        "set_pid",
        {
            vol.Required(ATTR_HVAC_MODE): vol.In(SUPPORTED_HVAC_MODES),
            vol.Optional(ATTR_KP): vol.Coerce(float),
            vol.Optional(ATTR_KI): vol.Coerce(float),
            vol.Optional(ATTR_KD): vol.Coerce(float),
            vol.Optional("update", default=True): vol.Boolean,
        },
        "async_set_pid",
    )

    platform.async_register_entity_service(  # type: ignore
        "set_filter_mode",
        {
            vol.Optional("mode"): vol.Coerce(int),
        },
        "async_set_filter_mode",
    )

    platform.async_register_entity_service(  # type: ignore
        "set_integral",
        {
            vol.Required(ATTR_HVAC_MODE): vol.In(SUPPORTED_HVAC_MODES),
            vol.Required("integral"): vol.Coerce(float),
        },
        "async_set_integral",
    )

    platform.async_register_entity_service(  # type: ignore
        "set_ka_kb",
        {
            vol.Required(ATTR_HVAC_MODE): vol.In(SUPPORTED_HVAC_MODES),
            vol.Optional(ATTR_KA): vol.Coerce(float),
            vol.Optional(ATTR_KB): vol.Coerce(float),
        },
        "async_set_ka_kb",
    )

    platform.async_register_entity_service(  # type: ignore
        "stuck_prevention",
        {
            vol.Optional("force", default=False): cv.boolean,
        },
        "async_run_stuck_prevention",
    )


def track_startup(entity, action) -> None:
    """Run action when HA starts. listen_once already drops its job, so remove must not unsubscribe twice."""
    armed = True

    async def _run(event) -> None:
        nonlocal armed
        armed = False
        await action(event)

    unsub = entity.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _run)

    @callback
    def _cancel() -> None:
        nonlocal armed
        if not armed:
            return
        armed = False
        unsub()

    entity.async_on_remove(_cancel)


def async_setup_domain_reload(hass: HomeAssistant) -> None:
    """Register multizone_thermostat.reload. Both platforms call this."""
    if hass.services.has_service(DOMAIN, SERVICE_RELOAD):
        return

    pending_start = None

    async def _async_start_registered(_event=None) -> None:
        registry = async_get_registry(hass)
        for circuit in list(registry.circuits.values()):
            await circuit.async_startup()
        for climate in list(registry.climates.values()):
            await climate.async_startup()

    async def _reload(_call: ServiceCall) -> None:
        nonlocal pending_start
        registry = async_get_registry(hass)
        registry.reloading = True
        clear_custom_presets()
        try:
            await async_reload_integration_platforms(hass, DOMAIN, PLATFORMS)
        finally:
            registry.reloading = False
        if hass.state == CoreState.running:
            await _async_start_registered()
            return
        if pending_start is not None:
            pending_start()
        pending_start = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_START, _async_start_registered
        )

    async_register_admin_service(hass, DOMAIN, SERVICE_RELOAD, _reload)
