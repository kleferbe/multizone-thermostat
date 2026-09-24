"""MultiZone thermostat.

Incl support for:
- multizone heating
- UKF filter on sensor
- various controllers:
    - temperature: PID
    - outdoor temperature: weather
    - valve position: PID
For more details about this platform, please read to the README
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
import traceback
from typing import TYPE_CHECKING

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_OFF,
    STATE_ON,
    STATE_OPEN,
    STATE_OPENING,
    STATE_PROBLEM,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    DOMAIN as HA_DOMAIN,
    CoreState,
    HomeAssistant,
    callback,
    Event,
)
from homeassistant.exceptions import ConditionError
from homeassistant.helpers import condition
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import DOMAIN, PLATFORMS, UKF_config, hvac_setting, services
from .const import (
    ATTR_ANTI_CALC_ACTIVE,
    ATTR_CIRCUIT,
    ATTR_CIRCUIT_PLAN,
    ATTR_CONTROL_OFFSET,
    ATTR_CONTROL_PWM_OUTPUT,
    ATTR_CURRENT_OUTDOOR_TEMPERATURE,
    ATTR_CURRENT_TEMP_VEL,
    ATTR_EMERGENCY_MODE,
    ATTR_FILTER_MODE,
    ATTR_HVAC_DEFINITION,
    ATTR_STUCK_LOOP,
    ATTR_VALUE,
    CONF_AREA,
    CONF_DETAILED_OUTPUT,
    CONF_ENABLE_OLD_INTEGRAL,
    CONF_ENABLE_OLD_PARAMETERS,
    CONF_ENABLE_OLD_STATE,
    CONF_EXTRA_PRESETS,
    CONF_FILTER_MODE,
    CONF_INITIAL_HVAC_MODE,
    CONF_INITIAL_PRESET_MODE,
    CONF_MASTER,
    CONF_PASSIVE_CHECK_TIME,
    CONF_PASSIVE_SWITCH_CHECK,
    CONF_PRECISION,
    CONF_SENSOR,
    CONF_SENSOR_OUT,
    CONF_STALE_DURATION,
    CONTROL_START_DELAY,
    NC_SWITCH_MODE,
    NO_SWITCH_MODE,
    PRESET_EMERGENCY,
    PRESET_RESTORE,
    PRESET_STANDBY,
    SERVICE_SET_VALUE,
)
from .platform_schema import PLATFORM_SCHEMA  # noqa: F401
from .circuit_plan import (
    CircuitPlan,
    CircuitPlanBuilder,
    RoomDemand,
    ValveSlot,
    check_time_in_window,
)
from .zone_registry import async_get_registry

if TYPE_CHECKING:
    from .select import CircuitSelect

ERROR_STATE = [STATE_UNAVAILABLE, STATE_UNKNOWN, STATE_PROBLEM]
NOT_SUPPORTED_SWITCH_STATES = [STATE_OPEN, STATE_OPENING, STATE_CLOSED, STATE_CLOSING]
HVAC_ACTIVE = [HVACMode.HEAT, HVACMode.COOL]


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the multizone thermostat platform."""

    name = config.get(CONF_NAME)
    sensor_entity_id = config.get(CONF_SENSOR)
    filter_mode = config.get(CONF_FILTER_MODE)
    sensor_out_entity_id = config.get(CONF_SENSOR_OUT)
    initial_hvac_mode = config.get(CONF_INITIAL_HVAC_MODE)
    precision = config.get(CONF_PRECISION)
    unit = hass.config.units.temperature_unit
    unique_id = config.get(CONF_UNIQUE_ID)
    initial_preset_mode = config.get(CONF_INITIAL_PRESET_MODE)
    area = config.get(CONF_AREA)
    sensor_stale_duration = config.get(CONF_STALE_DURATION)
    passive_switch = config.get(CONF_PASSIVE_SWITCH_CHECK)
    passive_switch_time = config.get(CONF_PASSIVE_CHECK_TIME)
    detailed_output = config.get(CONF_DETAILED_OUTPUT)
    enable_old_state = config.get(CONF_ENABLE_OLD_STATE)
    enable_old_parameters = config.get(CONF_ENABLE_OLD_PARAMETERS)
    enable_old_integral = config.get(CONF_ENABLE_OLD_INTEGRAL)
    master_entity_id = config.get(CONF_MASTER)

    hvac_def = {}
    custom_presets = []
    enabled_hvac_modes = []

    for mode in HVAC_ACTIVE:
        conf = config.get(mode)
        if not conf:
            continue
        enabled_hvac_modes.append(mode)
        hvac_def[mode] = conf
        custom_presets.append(list(conf[CONF_EXTRA_PRESETS].keys()))

    custom_presets = list({key_i for list_i in custom_presets for key_i in list_i})
    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
    services.register_services(list(set(custom_presets)))

    async_add_entities(
        [
            MultiZoneThermostat(
                name,
                unit,
                unique_id,
                precision,
                area,
                sensor_entity_id,
                filter_mode,
                sensor_out_entity_id,
                hvac_def,
                enabled_hvac_modes,
                initial_hvac_mode,
                initial_preset_mode,
                detailed_output,
                enable_old_state,
                enable_old_parameters,
                enable_old_integral,
                sensor_stale_duration,
                passive_switch,
                passive_switch_time,
                master_entity_id,
            )
        ]
    )


class MultiZoneThermostat(ClimateEntity, RestoreEntity):
    """Representation of a MultiZone Thermostat device."""

    _attr_should_poll = False
    _attr_supported_features = (
        ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TARGET_TEMPERATURE
    )

    def __init__(
        self,
        name,
        unit,
        unique_id,
        precision,
        area,
        sensor_entity_id,
        filter_mode,
        sensor_out_entity_id,
        hvac_def,
        enabled_hvac_modes,
        initial_hvac_mode,
        initial_preset_mode,
        detailed_output,
        enable_old_state,
        enable_old_parameters,
        enable_old_integral,
        sensor_stale_duration,
        passive_switch,
        passive_switch_time,
        master_entity_id=None,
    ) -> None:
        """Initialize the thermostat."""
        self._temp_lock = asyncio.Lock()

        self._sensor_entity_id = sensor_entity_id
        self._sensor_out_entity_id = sensor_out_entity_id
        self._filter_mode = filter_mode
        self._kf_temp = None
        self._temp_precision = precision
        self._attr_temperature_unit = unit

        self._hvac_mode = HVACMode.OFF
        self._hvac_mode_init = initial_hvac_mode
        self._old_preset = None
        self._preset_mode = initial_preset_mode
        self._enabled_hvac_mode = enabled_hvac_modes
        self._enable_old_state = enable_old_state
        self._restore_parameters = enable_old_parameters
        self._restore_integral = enable_old_integral
        self._sensor_stale_duration = sensor_stale_duration
        self._passive_switch = passive_switch
        self._passive_switch_time = passive_switch_time
        self._area = area
        self._emergency_stop = []
        self._current_temperature = None
        self._outdoor_temperature = None
        self._old_mode = "off"
        self._active_hvac_setting = None
        self._controller_timer = None
        self._pwm_start_timer = None
        self._pwm_stop_timer = None
        self.control_output = {ATTR_CONTROL_OFFSET: 0, ATTR_CONTROL_PWM_OUTPUT: 0}
        self._registry_id: str | None = None
        self._circuit = None
        self._configured_master = master_entity_id
        self._master_missing_logged = False
        self._local_epoch_timer = None
        self._pid_tick_timers: list = []
        self._circuit_plan: CircuitPlan | None = None
        self._startup_complete = False

        self._attr_name = name

        # setup control modes
        self._hvac_settings = {}
        for hvac_mode, mode_config in hvac_def.items():
            self._hvac_settings[hvac_mode] = hvac_setting.HVACSetting(
                self._attr_name,
                hvac_mode,
                mode_config,
                self._area,
                detailed_output,
            )
        self._plan_builder = CircuitPlanBuilder.create_off(
            name=self._attr_name or "climate"
        )

        self._logger = logging.getLogger(DOMAIN).getChild(name)

        if unique_id is not None:
            self._attr_unique_id = unique_id
        else:
            self._attr_unique_id = None

    @property
    def configured_master(self) -> str | None:
        """Select entity_id from YAML, if this room belongs to a circuit."""
        return self._configured_master

    @property
    def room_area(self) -> float:
        """Configured floor area used for nesting."""
        return self._area

    @property
    def hvac_active(self) -> bool:
        """True when HVAC is heat or cool."""
        return self._hvac_mode in HVAC_ACTIVE

    @property
    def is_coordinated(self) -> bool:
        """True when a live circuit is nesting this room."""
        return self._circuit is not None and self._circuit.is_coordinated

    @property
    def is_idle(self) -> bool:
        """True when this room must not request heat or cool."""
        if self.preset_mode == PRESET_STANDBY:
            return True
        if self._circuit is not None and self._circuit.is_standby:
            return True
        return False

    def get_circuit(self) -> CircuitSelect | None:
        """Return the heating circuit, binding it if the Select now exists."""
        if self._circuit is not None:
            return self._circuit
        if not self._configured_master:
            return None
        registry = async_get_registry(self.hass)
        if registry.circuit(self._configured_master) is None:
            return None
        registry.register_climate(self)
        return self._circuit

    def bind_circuit(self, circuit) -> None:
        """Attach or detach the heating circuit. YAML is the source of membership."""
        if self._circuit is circuit:
            return
        self._circuit = circuit
        if not self._active_hvac_setting or not self._active_hvac_setting.is_proportional:
            return
        if self._owns_epoch_loop():
            self.start_room_control()
        else:
            self.stop_local_epoch()

    def _owns_epoch_loop(self) -> bool:
        """True when this room plans its own PWM window."""
        if not self._active_hvac_setting or not self._active_hvac_setting.is_proportional:
            return False
        if self._circuit is None:
            return True
        return self._circuit.is_uncoordinated

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added.

        Attach the listeners.
        """
        self._logger.info("Add thermostat to hass")
        await super().async_added_to_hass()
        self._registry_id = self.entity_id
        async_get_registry(self.hass).register_climate(self)

        # Add listeners to track changes from the temp sensor
        if self._sensor_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._sensor_entity_id],
                    self._async_indoor_temp_change,
                )
            )

        # Add listeners to track changes from the outdoor temp sensor
        if self._sensor_out_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._sensor_out_entity_id],
                    self._async_outdoor_temp_change,
                )
            )

        # routine to check if state updates from sensor have stopped
        if (
            self._sensor_entity_id or self._sensor_out_entity_id
        ) and self._sensor_stale_duration:
            self.async_on_remove(
                async_track_time_interval(
                    self.hass,
                    self._async_stale_sensor_check,
                    self._sensor_stale_duration,
                )
            )

        # Add listeners to track changes from the hvac switches
        entity_list = []
        for _, mode_def in self._hvac_settings.items():
            entity_list.append(mode_def.switch_entity)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                entity_list,
                self._async_switches_change,
            )
        )

        async def _async_startup(*_) -> None:
            """Init on startup."""
            self._logger.debug("Run start-up")
            save_state = False

            # read room temperature sensor
            if self._sensor_entity_id:
                sensor_state = self.hass.states.get(self._sensor_entity_id)
            else:
                sensor_state = None

            # process room temperature
            if sensor_state and sensor_state.state not in ERROR_STATE:
                await self._async_update_current_temp(sensor_state.state)
                save_state = True

            # check outdoor temperature
            if self._sensor_out_entity_id:
                sensor_state = self.hass.states.get(self._sensor_out_entity_id)
            else:
                sensor_state = None

            # process outdoor temperature
            if sensor_state and sensor_state.state not in ERROR_STATE:
                self._async_update_outdoor_temperature(sensor_state.state)
                save_state = True

            # RestoreEntity reads the previous run from its own store,
            # not hass.states. Restore before the first HA state write.
            if (old_state := await self.async_get_last_state()) is not None:
                if not self._enable_old_state:
                    if not self._hvac_mode_init:
                        self._logger.warning(
                            "no initial hvac mode specified: force off mode"
                        )
                        self._hvac_mode_init = HVACMode.OFF
                    self._logger.info(
                        "init default hvac mode: '%s'", self._hvac_mode_init
                    )
                else:
                    self.restore_old_state(old_state)

            if (
                self._configured_master
                and self.get_circuit() is None
                and not self._master_missing_logged
            ):
                self._logger.error(
                    "Configured circuit '%s' was not found after startup; "
                    "'%s' runs locally",
                    self._configured_master,
                    self.entity_id,
                )
                self._master_missing_logged = True

            await self.async_set_hvac_mode(self._hvac_mode_init)
            self._startup_complete = True
            if save_state:
                self.async_write_ha_state()

        if self.hass.state == CoreState.running:
            await _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

    async def async_will_remove_from_hass(self) -> None:
        """Drop zone membership when the entity is unloaded."""
        async_get_registry(self.hass).unregister_climate(self)
        await super().async_will_remove_from_hass()

    @callback
    def async_registry_entry_updated(self) -> None:
        """Follow entity_id changes so zone membership stays valid."""
        super().async_registry_entry_updated()
        old_id = self._registry_id
        new_id = self.entity_id
        if not old_id or old_id == new_id:
            return
        async_get_registry(self.hass).rekey_entity(old_id, new_id)
        self._registry_id = new_id

    def restore_old_state(self, old_state) -> None:
        """Restore old state/config."""
        self._logger.debug("Old state stored : '%s'", old_state)

        try:
            old_hvac_mode = old_state.state
            try:
                old_hvac_mode = HVACMode(old_hvac_mode) if old_hvac_mode else None
            except ValueError:
                old_hvac_mode = None
            old_preset_mode = old_state.attributes.get(ATTR_PRESET_MODE) or PRESET_NONE
            old_temperature = old_state.attributes.get(ATTR_TEMPERATURE)
            self._logger.debug(
                "Old state preset mode %s, hvac mode %s, temperature set point '%s'",
                old_preset_mode,
                old_hvac_mode,
                old_temperature,
            )

            if old_hvac_mode is None or old_hvac_mode not in self.hvac_modes:
                raise ValueError(f"hvac mode '{old_state.state}' is not enabled")
            if old_preset_mode not in self.valid_presets(old_hvac_mode):
                self._logger.warning(
                    "preset '%s' is not enabled; restoring as '%s'",
                    old_preset_mode,
                    PRESET_NONE,
                )
                old_preset_mode = PRESET_NONE

            self._logger.info("restore old controller settings")
            self._hvac_mode_init = old_hvac_mode
            self._preset_mode = old_preset_mode
            if ATTR_HVAC_DEFINITION in old_state.attributes:
                self.restore_controller_state(old_state)
            else:
                self._logger.warning(
                    "no hvac_def in last state; mode and setpoint only"
                )
                if (
                    old_hvac_mode != HVACMode.OFF
                    and old_temperature is not None
                    and old_hvac_mode in self._hvac_settings
                ):
                    self._hvac_settings[old_hvac_mode].target_temperature = (
                        old_temperature
                    )

        except Exception as e:
            self._logger.warning("restoring old state failed:%s", str(e))
            self._logger.debug(traceback.format_exc())
            if not self._hvac_mode_init:
                self._hvac_mode_init = HVACMode.OFF
            return

    def restore_controller_state(self, old_state) -> None:
        """Restore HVAC settings from HA state."""
        old_def = old_state.attributes[ATTR_HVAC_DEFINITION]
        old_hvac_mode = old_state.state
        try:
            old_hvac_mode = HVACMode(old_hvac_mode) if old_hvac_mode else None
        except ValueError:
            old_hvac_mode = None
        old_temperature = old_state.attributes.get(ATTR_TEMPERATURE)
        for key, data in old_def.items():
            try:
                mode = HVACMode(key)
            except ValueError:
                continue
            if mode in self._hvac_settings and isinstance(data, dict):
                self._hvac_settings[mode].restore_reboot(
                    data,
                    self._restore_parameters,
                    self._restore_integral,
                )
        if old_hvac_mode != HVACMode.OFF and old_hvac_mode in self._hvac_settings:
            min_temp, max_temp = self._hvac_settings[old_hvac_mode].target_temp_limits
            if (
                old_temperature is not None
                and min_temp is not None
                and max_temp is not None
                and min_temp <= old_temperature <= max_temp
            ):
                self._hvac_settings[old_hvac_mode].target_temperature = old_temperature

    @property
    def extra_state_attributes(self) -> dict:
        """Attributes to include in entity."""
        tmp_dict = {}
        for key, data in self._hvac_settings.items():
            tmp_dict[key] = data.extra_attrs

        attrs = {
            ATTR_HVAC_DEFINITION: tmp_dict,
            ATTR_EMERGENCY_MODE: self._emergency_stop,
            ATTR_ANTI_CALC_ACTIVE: self.anti_calc_active,
            ATTR_STUCK_LOOP: self.anti_calc_active,
            ATTR_CIRCUIT: self._circuit.entity_id if self._circuit else None,
            ATTR_CURRENT_TEMP_VEL: self.current_temperature_velocity,
            ATTR_CURRENT_OUTDOOR_TEMPERATURE: self.outdoor_temperature,
            ATTR_FILTER_MODE: self.filter_mode,
            CONF_AREA: self._area,
        }
        if self._circuit_plan is not None:
            attrs[ATTR_CIRCUIT_PLAN] = self._circuit_plan.as_dict()
        return attrs

    @property
    def anti_calc_active(self) -> bool:
        """Return if the current plan is a stuck-loop flush."""
        return bool(self._circuit_plan and self._circuit_plan.stuck_loop)

    def set_detailed_output(self, hvac_mode: HVACMode, new_mode: bool) -> None:
        """Configure attribute output level."""
        self._hvac_settings[hvac_mode].detailed_output = new_mode
        self.schedule_update_ha_state()

    @callback
    def async_set_pwm_threshold(
        self, hvac_mode: HVACMode, new_threshold: float
    ) -> None:
        """Set new PID Controller min pwm value."""
        self._logger.info(
            "new minimum for pwm scale for '%s' to: '%s'", hvac_mode, new_threshold
        )
        self._hvac_settings[hvac_mode].set_pwm_threshold(new_threshold)
        self.schedule_update_ha_state()

    @callback
    def async_set_pid(
        self,
        hvac_mode: HVACMode,
        kp: float | None = None,
        ki: float | None = None,
        kd: float | None = None,
        update: bool = False,
    ) -> None:  # pylint: disable=invalid-name
        """Set new PID Controller Kp,Ki,Kd value."""
        self._logger.info("new PID for '%s' to: %s;%s;%s", hvac_mode, kp, ki, kd)
        self._hvac_settings[hvac_mode].set_pid_param(kp=kp, ki=ki, kd=kd, update=update)
        self.schedule_update_ha_state()

    async def async_set_filter_mode(self, mode: int) -> None:
        """Change filter mode."""
        self.set_filter_mode(mode)
        self.schedule_update_ha_state()

    def set_filter_mode(self, mode: int) -> None:
        """Set new filter for the temp sensor."""
        self._filter_mode = mode
        self._logger.info("modified sensor filter mode to: '%s'", mode)

        # no ukf filter
        if mode == 0:
            self._current_temperature = self.current_temperature
            self._kf_temp = None
            if self._active_hvac_setting:
                self._active_hvac_setting.current_state = None
                self._active_hvac_setting.current_temperature = self.current_temperature
        else:
            cycle_time = 60  # dt is updated when calling predict

            # init ukf when mode from 0 to >0
            if not self._kf_temp:
                if self._current_temperature is not None:
                    self._kf_temp = UKF_config.UKFFilter(
                        self._current_temperature,
                        cycle_time,
                        self.filter_mode,
                    )
                else:
                    self._logger.info(
                        "new sensor filter mode (%s) but no temperature reading",
                        mode,
                    )
                    return

            # update active filter
            else:
                self._kf_temp.set_filter_mode(
                    self.filter_mode,
                    cycle_time,
                )

    def get_hvac_setting(
        self, hvac_mode: HVACMode | None = None
    ) -> hvac_setting.HVACSetting | None:
        """HVACSetting for a mode, or the active one when mode is None."""
        if hvac_mode is None:
            return self._active_hvac_setting
        if hvac_mode == HVACMode.OFF:
            return None
        return self._hvac_settings.get(hvac_mode)

    @callback
    def async_set_integral(self, hvac_mode: HVACMode, integral: float) -> None:
        """Set new PID Controller integral value."""
        self._logger.info("new PID integral for '%s' to: '%s'", hvac_mode, integral)
        self._hvac_settings[hvac_mode].integral = integral
        self.schedule_update_ha_state()

    @callback
    def async_set_ka_kb(
        self, hvac_mode: HVACMode, ka: float | None = None, kb: float | None = None
    ) -> None:  # pylint: disable=invalid-name
        """Set new weather Controller ka,kb value."""
        self._logger.info("new weatehr ka,kb '%s' to: %s;%s", hvac_mode, ka, kb)
        self._hvac_settings[hvac_mode].set_ka_kb(ka=ka, kb=kb)
        self.schedule_update_ha_state()

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)

        if hvac_mode is None:
            hvac_mode = self._hvac_mode
        elif hvac_mode not in self.hvac_modes:
            self._logger.warning(
                "Try to update temperature to '%s' for mode '%s' but this mode is not enabled",
                temperature,
                hvac_mode,
            )
            return

        if hvac_mode is None or hvac_mode == HVACMode.OFF:
            self._logger.warning("You cannot update temperature for OFF mode")
            return

        self._logger.debug(
            "Temperature updated to '%s' for mode '%s'", temperature, hvac_mode
        )

        # when custom preset mode is active, do not operate
        if self.preset_mode in self._active_hvac_setting.custom_presets:
            self._logger.debug(
                "Preset mode {self.preset_mode} active when temperature is updated : skipping change"
            )
            return

        self._active_hvac_setting.target_temperature = round(temperature, 3)

        if self._hvac_mode != HVACMode.OFF:
            await self.update_demand()

        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Change hvac mode."""
        async with self._temp_lock:
            if self._hvac_mode == hvac_mode:
                return

            setting = self.get_hvac_setting(hvac_mode)
            if hvac_mode != HVACMode.OFF and setting is None:
                return

            self._logger.info("HVAC mode changed to '%s'", hvac_mode)

            if self._active_hvac_setting:
                self._async_cancel_pwm_routines(self._hvac_mode)
                self._async_routine_controller()
                self.stop_local_epoch()
                self.control_output = {
                    ATTR_CONTROL_OFFSET: 0,
                    ATTR_CONTROL_PWM_OUTPUT: 0,
                }

            self._old_mode = self._hvac_mode
            self._hvac_mode = hvac_mode
            self._active_hvac_setting = None

            if self._hvac_mode == HVACMode.OFF:
                self._logger.info(
                    "HVAC mode is OFF. Turn the devices OFF and exit hvac change"
                )
                self._plan_builder = CircuitPlanBuilder.create_off(
                    name=self._attr_name or "climate"
                )
                if not self.is_coordinated:
                    await self._apply_window(self._build_window(time.time()))
                self.async_write_ha_state()
                return

            if self.preset_mode != setting.preset_mode:
                await self.async_set_preset_mode(
                    self.preset_mode, hvac_mode=self._hvac_mode
                )

            self._active_hvac_setting = setting
            self._plan_builder = self._plan_builder_for(setting)
            if self.is_idle:
                self._active_hvac_setting.reset_control_output()
                self.control_output = self._active_hvac_setting.control_output

            if self._active_hvac_setting.is_pid:
                self._active_hvac_setting.pid_reset_time()

            if self._active_hvac_setting.is_weather and self.outdoor_temperature is not None:
                self._active_hvac_setting.outdoor_temperature = self.outdoor_temperature

            if self._active_hvac_setting.is_on_off:
                if not self.is_coordinated and self._active_hvac_setting.control_interval:
                    self._async_routine_controller(self._active_hvac_setting.control_interval)
                    await self._apply_window(self._build_window(time.time()))
            elif self._active_hvac_setting.is_proportional:
                if self._owns_epoch_loop():
                    self.start_room_control()

            if not self.is_idle:
                await self._async_compute_demand()

            self.async_write_ha_state()

    def start_room_control(self) -> None:
        """Start the local PWM epoch (standalone / uncoordinated)."""
        if not self._owns_epoch_loop():
            return
        self._set_local_epoch_timer(time.time() + CONTROL_START_DELAY)

    def stop_local_epoch(self) -> None:
        """Cancel local epoch and in-window PID ticks. Valve timers stay."""
        self._clear_local_epoch_timer()
        self._clear_pid_tick_timers()

    def _set_local_epoch_timer(self, when: float) -> None:
        self._clear_local_epoch_timer()

        async def _fired(now: datetime.datetime | None = None) -> None:
            self._local_epoch_timer = None
            await self._on_local_epoch_timer(now)

        self._local_epoch_timer = async_track_point_in_utc_time(
            self.hass,
            _fired,
            datetime.datetime.fromtimestamp(when),
        )

    def _clear_local_epoch_timer(self) -> None:
        if self._local_epoch_timer is None:
            return
        self._local_epoch_timer()
        self._local_epoch_timer = None

    def _clear_pid_tick_timers(self) -> None:
        for unsub in self._pid_tick_timers:
            unsub()
        self._pid_tick_timers = []

    async def _on_local_epoch_timer(self, now: datetime.datetime | None = None) -> None:
        """One standalone PWM window: demand, then a local plan."""
        if not self._owns_epoch_loop():
            return

        now_ts = time.time()
        window = self._nominal_window()
        if self._circuit_plan is not None:
            epoch = self._circuit_plan.window_end
            if window > 0:
                while epoch + window <= now_ts:
                    epoch += window
        else:
            epoch = now_ts

        await self.update_demand()
        if self._active_hvac_setting:
            self.control_output[ATTR_CONTROL_OFFSET] = 0
            self._active_hvac_setting.time_offset = 0
        await self._apply_window(self._build_window(epoch))

    def _plan_builder_for(
        self, setting: hvac_setting.HVACSetting
    ) -> CircuitPlanBuilder:
        """Room builder for an active heat or cool setting; no circuit actuator."""
        pwm = setting.pwm_duration.total_seconds() if setting.pwm_duration else 0.0
        if pwm > 0:
            duration = pwm
        elif setting.control_interval:
            duration = setting.control_interval.total_seconds()
        else:
            duration = 0.0
        return CircuitPlanBuilder.create(
            name=self._attr_name or "climate",
            duration=duration,
            pwm_scale=setting.pwm_scale,
            pwm_threshold=setting.pwm_threshold,
            pwm_resolution=setting.pwm_resolution,
        )

    def _nominal_window(self) -> float:
        if self._active_hvac_setting and self._active_hvac_setting.pwm_duration:
            pwm = self._active_hvac_setting.pwm_duration.total_seconds()
            if pwm > 0:
                return pwm
        if self._active_hvac_setting and self._active_hvac_setting.control_interval:
            return self._active_hvac_setting.control_interval.total_seconds()
        return 0.0

    def _build_window(self, epoch: float) -> CircuitPlan:
        hvac_mode = self._hvac_mode if self._hvac_mode != HVACMode.OFF else None
        window = self._nominal_window()
        entity_id = self.entity_id

        open_s = 0.0
        if self._active_hvac_setting and self._active_hvac_setting.anti_calc_open:
            open_s = self._active_hvac_setting.anti_calc_open.total_seconds()
        if (
            entity_id
            and open_s > 0
            and self._passive_switch
            and self._local_stale()
            and check_time_in_window(epoch, window, self._passive_switch_time)
        ):
            return self._plan_builder.build_stuck_loop(
                epoch, hvac_mode, [entity_id], open_s, 0.0
            )

        if (
            self._hvac_mode == HVACMode.OFF
            or self.preset_mode == PRESET_EMERGENCY
            or self.is_idle
        ):
            return self._plan_builder.idle(epoch, hvac_mode)

        demand = self.get_demand(hvac_mode)
        if (
            demand is None
            and entity_id
            and self._active_hvac_setting
            and self._active_hvac_setting.is_on_off
        ):
            pwm = (
                (self.control_output.get(ATTR_CONTROL_PWM_OUTPUT, 0) or 0)
                if self.control_output
                else 0
            )
            if pwm > 0:
                demand = RoomDemand(
                    entity_id=entity_id,
                    area=self._area,
                    pwm=pwm,
                    pwm_scale=self._active_hvac_setting.pwm_scale,
                    pwm_duration=0.0,
                    master_scaled_bound=1.0,
                )
        demands = [demand] if demand else []
        return self._plan_builder.build(
            epoch, hvac_mode, demands, tot_area=self._area
        )

    def _local_stale(self) -> bool:
        if not self._active_hvac_setting:
            return False
        duration = self._active_hvac_setting.anti_calc_idle
        if not duration:
            return False
        if self.preset_mode == PRESET_EMERGENCY:
            return False
        if self.hvac_action in (HVACAction.HEATING, HVACAction.COOLING):
            return False
        if self._is_valve_open():
            return False
        last = self._active_hvac_setting.switch_last_change
        if last is not None and datetime.datetime.now(datetime.UTC) - last <= duration:
            return False
        return True

    async def _apply_window(self, plan: CircuitPlan) -> None:
        await self.apply_plan(plan)
        if self._owns_epoch_loop() and plan.duration > 0:
            self._set_local_epoch_timer(plan.window_end)
        self.async_write_ha_state()

    def _set_pid_tick_timers(self, plan: CircuitPlan) -> None:
        """PID samples inside this PWM window. Demand only, no valve reschedule."""
        self._clear_pid_tick_timers()
        if not plan.pid_ticks or not self._active_hvac_setting or not self._active_hvac_setting.is_pid:
            return
        interval = self._active_hvac_setting.control_interval.total_seconds()
        if interval >= plan.duration:
            return
        step = interval
        now_ts = time.time()
        while step < plan.duration:
            when = plan.epoch + step
            if when > now_ts:
                unsub = async_track_point_in_utc_time(
                    self.hass,
                    self._on_pid_tick_timer,
                    datetime.datetime.fromtimestamp(when),
                )
                self._pid_tick_timers.append(unsub)
            step += interval

    async def _on_pid_tick_timer(self, now: datetime.datetime | None = None) -> None:
        await self.update_demand()
        self.async_write_ha_state()

    async def update_demand(self) -> None:
        """Run PID/WC now and store demand. Does not schedule valves."""
        self.get_circuit()
        async with self._temp_lock:
            await self._async_compute_demand()

    def get_demand(self, circuit_mode: HVACMode | None) -> RoomDemand | None:
        """Demand for this window, or None when this room is idle."""
        if not self._active_hvac_setting or circuit_mode is None:
            return None
        if self._hvac_mode != circuit_mode:
            return None
        if self.is_idle:
            return None
        if not self._active_hvac_setting.is_proportional:
            return None
        if self.preset_mode in (PRESET_EMERGENCY, PRESET_STANDBY):
            return None
        pwm = self.control_output.get(ATTR_CONTROL_PWM_OUTPUT, 0) or 0
        if pwm <= 0:
            return None
        return RoomDemand(
            entity_id=self.entity_id,
            area=self._area,
            pwm=pwm,
            pwm_scale=self._active_hvac_setting.pwm_scale,
            pwm_duration=self._active_hvac_setting.pwm_duration.total_seconds(),
            master_scaled_bound=self._active_hvac_setting.master_scaled_bound or 1.0,
        )

    async def apply_plan(self, plan: CircuitPlan) -> None:
        """Apply a one-valve plan: clear timers, store it, run the slot."""
        self._clear_local_epoch_timer()
        self._clear_pid_tick_timers()
        self._circuit_plan = plan
        slot = plan.slot_for(self.entity_id)
        if slot is None and plan.rooms:
            slot = plan.rooms[0]
        if slot is None:
            slot = plan.circuit if plan.circuit.entity_id else ValveSlot(entity_id=self.entity_id)

        self._clear_pwm_timers()
        if slot.position is not None:
            if slot.position <= 0:
                await self._async_switch_turn_off()
            else:
                await self._async_switch_turn_on(control_val=slot.position)
        else:
            await slot.apply_on_off_slot(
                time.time(),
                self._async_switch_turn_on,
                self._async_switch_turn_off,
                self._set_pwm_start_timer,
                self._set_pwm_stop_timer,
            )
        self._set_pid_tick_timers(plan)
        self.async_write_ha_state()

    def switch_last_change(self):
        """Last valve movement, for circuit anti-calc idle checks."""
        if self._active_hvac_setting:
            return self._active_hvac_setting.switch_last_change
        return None

    async def async_run_stuck_prevention(self, force: bool = False) -> None:
        """Flush this room now if stale, or always when force. Coordinated rooms use the circuit."""
        if self._circuit is not None and not self._circuit.is_uncoordinated:
            return
        open_s = 0.0
        if self._active_hvac_setting and self._active_hvac_setting.anti_calc_open:
            open_s = self._active_hvac_setting.anti_calc_open.total_seconds()
        if open_s <= 0:
            return
        if not force and not self._local_stale():
            return
        hvac_mode = self._hvac_mode if self._hvac_mode != HVACMode.OFF else None
        await self._apply_window(
            self._plan_builder.build_stuck_loop(
                time.time(), hvac_mode, [self.entity_id], open_s, 0.0
            )
        )

    @callback
    def _async_routine_controller(self, interval=None) -> None:
        """Run on-off controller at specified interval."""
        if interval is None:
            self._clear_controller_timer()
            return
        self._set_controller_timer(interval)
        self.hass.async_create_task(self._on_controller_timer())

    def _set_controller_timer(self, interval) -> None:
        self._clear_controller_timer()
        self._controller_timer = async_track_time_interval(
            self.hass, self._on_controller_timer, interval
        )
        self.async_on_remove(self._controller_timer)

    def _clear_controller_timer(self) -> None:
        if self._controller_timer is None:
            return
        self._controller_timer()
        self._controller_timer = None

    @callback
    def _async_indoor_temp_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle temperature change.

        Only call emergency stop due to stale sensor, ignore invalid values
        """
        new_state = event.data.get("new_state")
        self._logger.debug("New sensor temperature '%s'", new_state.state)

        if new_state is None or new_state.state in ERROR_STATE:
            # Before the first valid reading this is the normal boot sequence
            # (unavailable → unknown). After that it means the sensor dropped out.
            self._logger.log(
                logging.WARNING
                if self._current_temperature is not None
                else logging.DEBUG,
                "Sensor temperature %s invalid: %s, skip current state",
                new_state.name if new_state else self._sensor_entity_id,
                None if new_state is None else new_state.state,
            )
            return
        elif not is_float(new_state.state):
            self._logger.warning(
                "Sensor temperature %s unclear: %s type %s, skip current state",
                new_state.name,
                new_state.state,
                type(new_state.state),
            )
            return
        elif float(new_state.state) < -50 or float(new_state.state) > 50:
            self._logger.warning(
                "Sensor temperature %s unrealistic: %s, skip current state",
                new_state.name,
                new_state.state,
            )
            return
        elif self.preset_mode == PRESET_EMERGENCY:
            self._async_restore_emergency_stop(self._sensor_entity_id)

        self.hass.async_create_task(self._async_update_current_temp(new_state.state))

    @callback
    def _async_outdoor_temp_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle outdoor temperature changes.

        Only call emergency stop due to stale sensor, ignore invalid values
        """
        new_state = event.data.get("new_state")
        self._logger.debug("New sensor outdoor temperature '%s'", new_state.state)
        if new_state is None or new_state.state in ERROR_STATE:
            self._logger.debug(
                "Outdoor sensor temperature %s invalid %s, skip current state",
                new_state.name,
                new_state.state,
            )
            return
        elif not is_float(new_state.state):
            self._logger.warning(
                "Outdoor sensor temperature %s unclear: %s type %s, skip current state",
                new_state.name,
                new_state.state,
                type(new_state.state),
            )
            return
        elif self.preset_mode == PRESET_EMERGENCY:
            self._async_restore_emergency_stop(self._sensor_out_entity_id)

        self._async_update_outdoor_temperature(new_state.state)

    @callback
    def _async_stale_sensor_check(self, now: datetime.datetime | None = None) -> None:
        """Check if the sensor has emitted a value during the allowed stale period."""
        entity_list = []
        if self._sensor_entity_id:
            entity_list.append(self._sensor_entity_id)
        if self._sensor_out_entity_id:
            entity_list.append(self._sensor_out_entity_id)

        # check all sensors
        for entity_id in entity_list:
            sensor_state = self.hass.states.get(entity_id)
            if (
                datetime.datetime.now(datetime.UTC) - sensor_state.last_updated
                > self._sensor_stale_duration
            ):
                self._logger.debug(
                    "'%s' last received update is %s, duration is '%s', limit is '%s'",
                    entity_id,
                    sensor_state.last_updated,
                    datetime.datetime.now(datetime.UTC) - sensor_state.last_updated,
                    self._sensor_stale_duration,
                )

                self._async_activate_emergency_stop("stale sensor", sensor=entity_id)

    @callback
    def _async_switches_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle device switch state changes."""
        new_state = event.data.get("new_state")
        entity_id = event.data.get(ATTR_ENTITY_ID)
        self._logger.debug(
            "'%s' switch changed to '%s'",
            entity_id,
            new_state.state,
        )
        # catch multipe options
        if new_state.state in ERROR_STATE:
            self._async_activate_emergency_stop(
                "switch to error state change", sensor=entity_id
            )
        elif new_state.state in NOT_SUPPORTED_SWITCH_STATES:
            self._async_activate_emergency_stop(
                f"not supported switch state {new_state.state}",
                sensor=entity_id,
            )
        # valid switch state
        else:
            if self.preset_mode == PRESET_EMERGENCY:
                self._async_restore_emergency_stop(entity_id)

            if self._hvac_mode in HVAC_ACTIVE and self._active_hvac_setting:
                other_mode = (
                    HVACMode.COOL
                    if self._hvac_mode == HVACMode.HEAT
                    else HVACMode.HEAT
                )
                other = self.get_hvac_setting(other_mode)
                if (
                    other is not None
                    and entity_id != self._active_hvac_setting.switch_entity
                    and self._is_valve_open()
                    and self._is_valve_open(hvac_mode=other_mode)
                ):
                    self._logger.warning(
                        "valve of %s is open. Other hvac mode switch '%s' changed to %s, keep in closed state",
                        self._hvac_mode,
                        entity_id,
                        new_state.state,
                    )

                    self.hass.async_create_task(
                        self._async_switch_turn_off(hvac_mode=other_mode)
                    )

            elif self._startup_complete:
                # not a current active thermostat thus switch state change should not be triggered
                # unless stuck loop prevention is running
                for hvac_mode, data in self._hvac_settings.items():
                    if (
                        data.switch_entity == entity_id
                        and not (self._circuit_plan and self._circuit_plan.stuck_loop)
                        and self._is_valve_open(hvac_mode=hvac_mode)
                    ):
                        self._logger.warning(
                            "No switches should be activated in hvac 'off' mode: restore switch '%s' from  %s to 'idle' state",
                            entity_id,
                            new_state.state,
                        )
                        self.hass.async_create_task(
                            self._async_switch_turn_off(hvac_mode=hvac_mode)
                        )

        self.schedule_update_ha_state(force_refresh=False)

    async def _async_update_current_temp(
        self, current_temp: float | None = None
    ) -> None:
        """Update thermostat, optionally with latest state from sensor."""
        if current_temp:
            self._logger.debug("Room temperature updated to '%s'", current_temp)
            # store local in case current hvac mode is off
            self._current_temperature = float(current_temp)

            # setup filter after first temp reading
            if not self._kf_temp and self.filter_mode > 0:
                self.set_filter_mode(self.filter_mode)

        # update ukf filter
        if self._kf_temp:
            self._kf_temp.kf_predict()
            if current_temp:
                tmp_temperature = float(current_temp)
            elif self._current_temperature is not None:
                tmp_temperature = self._current_temperature
            else:
                tmp_temperature = None

            if tmp_temperature:
                self._kf_temp.kf_update(tmp_temperature)

            self._logger.debug(
                "filtered sensor update temp '%.2f'", self._kf_temp.get_temp
            )

        # PID/PWM windows sample on their own ticks; on-off waits for the next plan.
        if current_temp:
            self.async_write_ha_state()

    @callback
    def _async_update_outdoor_temperature(
        self, current_temp: float | None = None
    ) -> None:
        """Update thermostat with latest state from outdoor sensor."""
        if current_temp:
            self._logger.debug("Outdoor temperature updated to '%s'", current_temp)
            self._outdoor_temperature = float(current_temp)
            if self._active_hvac_setting:
                self._active_hvac_setting.outdoor_temperature = self._outdoor_temperature

    async def _async_update_controller_temp(self) -> None:
        """Update temperature to controller routines."""
        # TODO: async needed?
        if self._active_hvac_setting:
            if not self._kf_temp:
                self._active_hvac_setting.current_temperature = self._current_temperature
            else:
                self._active_hvac_setting.current_state = [
                    self._kf_temp.get_temp,
                    self._kf_temp.get_vel,
                ]

    async def _async_check_duration(self, routine: bool, force: bool) -> bool:
        """Check if switch change in on-off mode has been long enough.

        on_off is also true when pwm = 0 therefore != _is_pwm_active
        """

        # If the mode is OFF and the device is ON, turn it OFF and exit, else, just exit
        min_cycle_duration = self._active_hvac_setting.min_on_off_cycle

        # if the call was made by a sensor change, check the min duration
        # in case of keep-alive (time not none) this test is ignored due to sensor_change = false
        if not force and not routine and min_cycle_duration.seconds != 0:
            entity_id = self._active_hvac_setting.switch_entity
            state = self.hass.states.get(entity_id).state
            try:
                long_enough = condition.state(
                    self.hass, entity_id, state, min_cycle_duration
                )
            except ConditionError:
                long_enough = False

            if not long_enough:
                self._logger.debug(
                    "Return from %s temp  update. Min duration (%s min) for state '%s' not expired",
                    entity_id,
                    min_cycle_duration.seconds / 60,
                    state,
                )
                return False

            else:
                return True
        else:
            return True

    async def _async_compute_demand(self) -> None:
        """PID/WC (and on-off) calculation. Caller holds `_temp_lock`."""
        if self.preset_mode == PRESET_EMERGENCY:
            if not self._emergency_stop:
                self._async_restore_emergency_stop("")
            self._logger.debug("Controller cancelled due to 'emergency mode'")
            return

        if self.is_idle:
            self._logger.debug("Controller skipped: idle (standby or circuit standby)")
            if self._active_hvac_setting:
                self._active_hvac_setting.reset_control_output()
                self.control_output = self._active_hvac_setting.control_output
            return

        if not self._active_hvac_setting:
            self._logger.debug(
                "Control update skipped: hvac mode is '%s'", self._hvac_mode
            )
            return

        await self._async_update_controller_temp()

        if (
            self._active_hvac_setting.is_on_off
            or self._active_hvac_setting.is_proportional
        ):
            if self._sensor_entity_id and self._active_hvac_setting.current_temperature is None:
                self._logger.debug(
                    "cancel control loop: current temp is None while running controller routine."
                )
                return

        if self._active_hvac_setting.is_weather:
            if self._sensor_out_entity_id and (
                self._active_hvac_setting.outdoor_temperature is None
                or self._active_hvac_setting.target_temperature is None
            ):
                self._logger.warning(
                    "cancel control loop: current outdoor temp is '%s' and setpoint is '%s' cannot run weather mode",
                    self._active_hvac_setting.outdoor_temperature,
                    self._active_hvac_setting.target_temperature,
                )
                return

        self._active_hvac_setting.calculate(routine=False, force=False, current_offset=0)
        self._active_hvac_setting.calc_control_output()
        self.control_output = self._active_hvac_setting.control_output
        self._logger.debug(
            "Obtained current control output: '%s'", self.control_output
        )

    async def _on_controller_timer(
        self, now: datetime.datetime | None = None, force: bool = False
    ) -> None:
        """On-off hysteresis loop. PWM rooms use update_demand() instead."""
        async with self._temp_lock:
            routine = now is not None
            self._logger.debug(
                "Controller: calculate output, routine=%s; forced=%s", routine, force
            )

            if self.preset_mode == PRESET_EMERGENCY:
                if not self._emergency_stop:
                    self._async_restore_emergency_stop("")
                return

            if self.is_idle:
                if self._active_hvac_setting:
                    self._active_hvac_setting.reset_control_output()
                    self.control_output = self._active_hvac_setting.control_output
                if not self.is_coordinated:
                    self.hass.async_create_task(
                        self._apply_window(self._build_window(time.time()))
                    )
                return

            if not self._active_hvac_setting:
                return

            await self._async_update_controller_temp()

            if self._sensor_entity_id and self._active_hvac_setting.current_temperature is None:
                self._logger.debug(
                    "cancel control loop: current temp is None while running controller routine."
                )
                return

            if self._active_hvac_setting.is_on_off:
                if not await self._async_check_duration(routine, force):
                    return

            self._active_hvac_setting.calculate(routine=routine, force=force, current_offset=0)
            self._active_hvac_setting.calc_control_output()
            self.control_output = self._active_hvac_setting.control_output
            self._logger.debug(
                "Obtained current control output: '%s'", self.control_output
            )

            if (force or self._active_hvac_setting.is_on_off) and not self.is_coordinated:
                self.hass.async_create_task(
                    self._apply_window(self._build_window(time.time()))
                )

            if self._active_hvac_setting.is_switch_on_off:
                self.async_write_ha_state()

    @callback
    def _async_cancel_pwm_routines(self, hvac_mode: HVACMode | None = None) -> None:
        """Cancel scheduled switch routines and close the valve."""
        self._clear_pwm_timers()
        self.hass.async_create_task(
            self._async_switch_turn_off(hvac_mode=hvac_mode)
        )

    def _set_pwm_start_timer(self, when: float) -> None:
        self._clear_pwm_start_timer()

        async def _fired(_now: datetime.datetime) -> None:
            self._pwm_start_timer = None
            await self._on_pwm_start_timer()

        self._pwm_start_timer = async_track_point_in_utc_time(
            self.hass,
            _fired,
            datetime.datetime.fromtimestamp(when),
        )

    def _clear_pwm_start_timer(self) -> None:
        if self._pwm_start_timer is None:
            return
        self._pwm_start_timer()
        self._pwm_start_timer = None

    def _set_pwm_stop_timer(self, when: float) -> None:
        self._clear_pwm_stop_timer()

        async def _fired(_now: datetime.datetime) -> None:
            self._pwm_stop_timer = None
            await self._on_pwm_stop_timer()

        self._pwm_stop_timer = async_track_point_in_utc_time(
            self.hass,
            _fired,
            datetime.datetime.fromtimestamp(when),
        )

    def _clear_pwm_stop_timer(self) -> None:
        if self._pwm_stop_timer is None:
            return
        self._pwm_stop_timer()
        self._pwm_stop_timer = None

    def _clear_pwm_timers(self) -> None:
        self._clear_pwm_start_timer()
        self._clear_pwm_stop_timer()

    async def _on_pwm_start_timer(self, _now: datetime.datetime | None = None) -> None:
        await self._async_switch_turn_on()

    async def _on_pwm_stop_timer(self, _now: datetime.datetime | None = None) -> None:
        await self._async_switch_turn_off()

    def _prop_valve_position(self, hvac_on, control_val: float | None = None):
        """Map requested opening to the switch scale, including NC/NO."""
        if control_val is None:
            valve_pos = self.control_output[ATTR_CONTROL_PWM_OUTPUT]
        else:
            valve_pos = control_val

        valve_pos = round(max(0, min(valve_pos, hvac_on.pwm_scale)), 0)

        if hvac_on.switch_mode == NO_SWITCH_MODE:
            valve_pos = hvac_on.pwm_scale - valve_pos

        return valve_pos

    async def _async_switch_turn_on(
        self, hvac_mode: HVACMode | None = None, control_val: float | None = None
    ) -> None:
        """Open valve or reposition proportional valve.

        NC/NO aware: NC conversion to NO
        """
        self._logger.debug("Turn ON")
        setting = self.get_hvac_setting(hvac_mode)
        if setting is None:
            self._logger.debug("No switch defined for %s", hvac_mode)
            return
        entity_id = setting.switch_entity

        # open valve
        if setting.is_switch_on_off:
            if self._is_valve_open(hvac_mode=hvac_mode):
                self._logger.debug("Switch already ON")
                return

            data = {ATTR_ENTITY_ID: entity_id}
            self._logger.debug("Order 'ON' sent to switch device '%s'", entity_id)

            setting.switch_last_change = datetime.datetime.now(datetime.UTC)

            if setting.switch_mode == NC_SWITCH_MODE:
                operation = SERVICE_TURN_ON
            else:
                operation = SERVICE_TURN_OFF

            await self.hass.services.async_call(
                HA_DOMAIN, operation, data, context=self._context
            )

        # change valve position
        else:
            valve_pos = self._prop_valve_position(setting, control_val)
            self._logger.debug(
                "Change state of heater '%s' to '%s'",
                entity_id,
                valve_pos,
            )

            setting.switch_last_change = datetime.datetime.now(datetime.UTC)
            data = {
                ATTR_ENTITY_ID: entity_id,
                ATTR_VALUE: valve_pos,
            }
            method = entity_id.split(".")[0]

            await self.hass.services.async_call(
                method,
                SERVICE_SET_VALUE,
                data,
                context=self._context,
            )

    async def _async_switch_turn_off(self, hvac_mode: HVACMode | None = None) -> None:
        """Close valve.

        NC/NO aware: NC converted to NO
        """
        self._logger.debug("Turn OFF called")
        setting = self.get_hvac_setting(hvac_mode)
        if setting is None:
            self._logger.debug("No switch defined for %s", hvac_mode)
            return
        entity_id = setting.switch_entity

        if setting.is_switch_on_off:
            if not self._is_valve_open(hvac_mode=hvac_mode):
                self._logger.debug("Switch already OFF")
                return

            data = {ATTR_ENTITY_ID: entity_id}
            self._logger.debug("Order 'OFF' sent to switch device '%s'", entity_id)

            if setting.switch_mode == NC_SWITCH_MODE:
                operation = SERVICE_TURN_OFF
            else:
                operation = SERVICE_TURN_ON

            await self.hass.services.async_call(
                HA_DOMAIN, operation, data, context=self._context
            )
        else:
            self._logger.debug(
                "Change state of switch '%s' to '%s'",
                entity_id,
                0,
            )

            if setting.switch_mode == NC_SWITCH_MODE:
                control_val = 0
            else:
                control_val = setting.pwm_scale

            data = {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: control_val}
            method = entity_id.split(".")[0]

            await self.hass.services.async_call(
                method,
                SERVICE_SET_VALUE,
                data,
                context=self._context,
            )

    @callback
    def _async_activate_emergency_stop(self, source: str, sensor: str) -> None:
        """Send an emergency OFF order to HVAC switch."""
        if sensor not in self._emergency_stop:
            self._logger.warning(
                "Emergency OFF order send from %s due to sensor %s", source, sensor
            )
            self._emergency_stop.append(sensor)

            # change to emergency mode in coase not yet activated
            if self.preset_mode != PRESET_EMERGENCY:
                self.hass.async_create_task(
                    self.async_set_preset_mode(PRESET_EMERGENCY)
                )
                # cancel scheduled switch routines
                self._async_cancel_pwm_routines()
        else:
            self._logger.debug("Emergency OFF recall send from %s", source)

    @callback
    def _async_restore_emergency_stop(self, entity_id: str) -> None:
        """Update emergency list."""
        # restore preset when called without any listing
        if not self._emergency_stop:
            self.hass.async_create_task(self.async_set_preset_mode(PRESET_RESTORE))

        elif entity_id in self._emergency_stop:
            self._emergency_stop.remove(entity_id)

            if not self._emergency_stop and self.preset_mode == PRESET_EMERGENCY:
                self._logger.info("Recover from emergency mode")
                self.hass.async_create_task(self.async_set_preset_mode(PRESET_RESTORE))

    async def async_set_preset_mode(
        self, preset_mode: str, hvac_mode: HVACMode | None = None
    ) -> None:
        """Set new preset mode."""
        self._logger.debug("Preset update to %s", preset_mode)

        if (
            preset_mode not in self.valid_presets(hvac_mode)
            and preset_mode != PRESET_RESTORE
        ):
            self._logger.warning(
                "This preset (%s) is not enabled (see the configuration)", preset_mode
            )
            return

        # already in emergency mode, skip
        if preset_mode == self.preset_mode == PRESET_EMERGENCY:
            return

        if preset_mode != PRESET_RESTORE and self.preset_mode == PRESET_EMERGENCY:
            self._logger.warning(
                "Preset mode change to '%s' not allowed while in emergency mode",
                preset_mode,
            )
            self.async_write_ha_state()
            return

        if preset_mode == PRESET_EMERGENCY and not self._emergency_stop:
            self._logger.warning(
                "Preset change '%s' not allowed as no listed errors. Return to previous mode.",
                preset_mode,
            )
            return

        if preset_mode == PRESET_RESTORE and self._emergency_stop:
            self._logger.warning(
                "Preset restore '%s' not allowed as listed errors present.",
                preset_mode,
            )
            return

        if self._active_hvac_setting:
            self._logger.debug("Set preset mode to '%s'", preset_mode)
            old_preset = self.preset_mode
            self._active_hvac_setting.preset_mode = preset_mode
            self._preset_mode = self._active_hvac_setting.preset_mode
            if (
                self._preset_mode in (PRESET_STANDBY, PRESET_EMERGENCY)
                and old_preset not in (PRESET_STANDBY, PRESET_EMERGENCY)
            ):
                self._active_hvac_setting.reset_control_output()
                self.control_output = self._active_hvac_setting.control_output
                if not self.is_coordinated:
                    await self._apply_window(self._build_window(time.time()))
                else:
                    self._async_cancel_pwm_routines()
            elif (
                old_preset in (PRESET_STANDBY, PRESET_EMERGENCY)
                and self._preset_mode not in (PRESET_STANDBY, PRESET_EMERGENCY)
            ):
                if self._active_hvac_setting.is_pid:
                    self._active_hvac_setting.pid_reset_time()
                if not self.is_coordinated:
                    await self.update_demand()
                    await self._apply_window(self._build_window(time.time()))
            elif self.preset_mode != PRESET_EMERGENCY:
                await self.update_demand()

        elif self._old_mode != HVACMode.OFF:
            self._logger.debug(
                "Set old hvac mode %s preset mode to '%s'", self._old_mode, preset_mode
            )
            self._hvac_settings[self._old_mode].preset_mode = preset_mode
            self._preset_mode = PRESET_NONE

        self.async_write_ha_state()

    def _is_valve_open(self, hvac_mode: HVACMode | None = None) -> bool:
        """Check if the valve is open.

        NC/NO aware: NO converted to NC
        """
        setting = self.get_hvac_setting(hvac_mode)

        if setting is None:
            self._logger.debug("no found entity for %s", hvac_mode)
            return False

        entity_id = setting.switch_entity
        try:
            switch_state = self.hass.states.get(entity_id).state
        except:
            self._async_activate_emergency_stop(
                "valve open check entity not found", sensor=entity_id
            )
            return False

        # check if error state
        if switch_state in ERROR_STATE or (
            not setting.is_switch_on_off and not is_float(switch_state)
        ):
            self._async_activate_emergency_stop(
                "active switch state check", sensor=entity_id
            )
            return False

        # restore from error state
        if self.preset_mode == PRESET_EMERGENCY:
            self._async_restore_emergency_stop(entity_id)

        return_val = False
        if setting.is_switch_on_off:
            if setting.switch_mode == NC_SWITCH_MODE:
                if switch_state == STATE_ON:
                    return_val = True
            elif switch_state == STATE_OFF:
                return_val = True
        else:
            if setting.switch_mode == NC_SWITCH_MODE:
                valve_position = float(switch_state)
            else:
                valve_position = setting.pwm_scale - float(switch_state)

            if valve_position > 0:
                return_val = True

        return return_val

    @property
    def room_min_temp(self) -> float | None:
        """Configured min setpoint while HVAC is on."""
        if self._active_hvac_setting:
            if self._hvac_mode != HVACMode.OFF:
                if self.preset_mode in self._active_hvac_setting.custom_presets:
                    return self._active_hvac_setting.preset_temp
                if self._active_hvac_setting.min_target_temp:
                    return self._active_hvac_setting.min_target_temp
            return None
        return None

    @property
    def room_max_temp(self) -> float | None:
        """Configured max setpoint while HVAC is on."""
        if self._active_hvac_setting:
            if self._hvac_mode != HVACMode.OFF:
                if self.preset_mode in self._active_hvac_setting.custom_presets:
                    return self._active_hvac_setting.preset_temp
                if self._active_hvac_setting.max_target_temp:
                    return self._active_hvac_setting.max_target_temp
            return None
        return None

    @property
    def room_current_temperature(self) -> float | None:
        """Filtered or raw room temperature."""
        if not self._kf_temp:
            return self._current_temperature
        return round(self._kf_temp.get_temp, 3)

    @property
    def room_current_temperature_velocity(self) -> float | None:
        """Filtered or PID room temperature velocity."""
        if not self._kf_temp:
            if self._active_hvac_setting:
                if self._active_hvac_setting.is_pid:
                    return self._active_hvac_setting.velocity
                return "no velocity calculated"
            return "only available when hvac on"
        return round(self._kf_temp.get_vel, 5)

    @property
    def room_target_temperature(self) -> float | None:
        """Active HVAC target temperature, or None when off."""
        if self._hvac_mode is HVACMode.OFF or self._hvac_mode is None or self._active_hvac_setting is None:
            return None
        return self._active_hvac_setting.target_temperature

    @property
    def precision(self) -> float:
        """Return the precision of the system."""
        if self._temp_precision is not None:
            return self._temp_precision
        return super().precision

    @property
    def target_temperature_step(self) -> float:
        """Return the supported step of target temperature."""
        # Since this integration does not yet have a step size parameter
        # we have to re-use the precision as the step size for now.
        return self.precision

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return self.room_min_temp

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return self.room_max_temp

    @property
    def current_temperature(self) -> float | None:
        """Return the sensor temperature."""
        return self.room_current_temperature

    @property
    def current_temperature_velocity(self) -> float | None:
        """Return the sensor temperature velocity."""
        return self.room_current_temperature_velocity

    @property
    def outdoor_temperature(self) -> float | None:
        """Return the sensor outdoor temperature."""
        return self._outdoor_temperature

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current operation."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        """Return the current running hvac operation if supported.

        Need to be one of HVACAction.*.
        """
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if not self._is_valve_open():
            return HVACAction.IDLE
        return (
            HVACAction.HEATING
            if self._hvac_mode == HVACMode.HEAT
            else HVACAction.COOLING
        )

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self.room_target_temperature

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """List of available operation modes."""
        return self._enabled_hvac_mode + [HVACMode.OFF]

    @property
    def preset_mode(self) -> str:
        """Return the current preset mode, e.g., home, away, temp."""
        return self._preset_mode

    @property
    def preset_modes(self) -> list:
        """Return a list of available preset modes."""
        return self.valid_presets()

    def valid_presets(self, hvac_mode: HVACMode | None = None):
        """Return a list of available preset modes."""

        setting = self.get_hvac_setting(hvac_mode)

        modes = [PRESET_NONE, PRESET_STANDBY, PRESET_EMERGENCY]
        if setting is not None and setting.custom_presets:
            modes = modes + list(setting.custom_presets.keys())
        return modes

    @property
    def filter_mode(self) -> int:
        """Return the UKF mode."""
        return self._filter_mode

    @filter_mode.setter
    def filter_mode(self, mode: int) -> None:
        """Set the UKF mode."""
        self._filter_mode = mode


def is_float(element) -> bool:
    """Check if input is float."""
    try:
        float(element)
        return True
    except ValueError:
        return False
