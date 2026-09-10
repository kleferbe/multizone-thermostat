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
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import DOMAIN, PLATFORMS, UKF_config, hvac_setting, services
from .const import (
    ATTR_ANTI_CALC_ACTIVE,
    ATTR_CONTROL_OFFSET,
    ATTR_CONTROL_PWM_OUTPUT,
    ATTR_CURRENT_OUTDOOR_TEMPERATURE,
    ATTR_CURRENT_TEMP_VEL,
    ATTR_EMERGENCY_MODE,
    ATTR_FILTER_MODE,
    ATTR_HVAC_DEFINITION,
    ATTR_SELF_CONTROLLED,
    ATTR_VALUE,
    CLOSE_TO_PWM,
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
    CONF_MASTER_MODE,
    CONF_PASSIVE_CHECK_TIME,
    CONF_PASSIVE_SWITCH_CHECK,
    CONF_PRECISION,
    CONF_SENSOR,
    CONF_SENSOR_OUT,
    CONF_STALE_DURATION,
    NC_SWITCH_MODE,
    NO_SWITCH_MODE,
    PRESET_EMERGENCY,
    PRESET_RESTORE,
    PRESET_STANDBY,
    PWM_LAG,
    SERVICE_SET_VALUE,
    START_MISALINGMENT,
    OperationMode,
)
from .platform_schema import PLATFORM_SCHEMA  # noqa: F401
from .master_role import MasterRole
from .satellite_role import SatelliteRole
from .standalone_role import StandaloneRole
from .zone_registry import async_get_registry

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
    heat_conf = config.get(HVACMode.HEAT)
    cool_conf = config.get(HVACMode.COOL)
    master_entity_id = config.get(CONF_MASTER)

    hvac_def = {}
    custom_presets = []
    enabled_hvac_modes = []

    # Append the enabled hvac modes to the list
    if heat_conf:
        enabled_hvac_modes.append(HVACMode.HEAT)
        hvac_def[HVACMode.HEAT] = heat_conf
        custom_presets.append(list(heat_conf.get(CONF_EXTRA_PRESETS).keys()))
    if cool_conf:
        enabled_hvac_modes.append(HVACMode.COOL)
        hvac_def[HVACMode.COOL] = cool_conf
        custom_presets.append(list(cool_conf.get(CONF_EXTRA_PRESETS).keys()))

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
        self._hvac_on = None
        self._loop_controller = None
        self._loop_pwm = None
        self._start_pwm = None
        self._stop_pwm = None
        self.time_changed = None
        self._pwm_start_time = None
        self.control_output = {ATTR_CONTROL_OFFSET: 0, ATTR_CONTROL_PWM_OUTPUT: 0}
        self._self_controlled = OperationMode.SELF
        self._registry_id: str | None = None

        self._attr_name = name
        self._master_entity_id = master_entity_id
        is_master_conf = any(
            CONF_MASTER_MODE in mode_config for mode_config in hvac_def.values()
        )
        if is_master_conf:
            self._role = MasterRole(self)
        elif master_entity_id:
            self._role = SatelliteRole(self, master_entity_id)
        else:
            self._role = StandaloneRole(self)

        # setup control modes
        self._hvac_def = {}
        for hvac_mode, mode_config in hvac_def.items():
            self._hvac_def[hvac_mode] = hvac_setting.HVACSetting(
                self._attr_name,
                hvac_mode,
                mode_config,
                self._area,
                detailed_output,
            )

        self._logger = logging.getLogger(DOMAIN).getChild(name)

        if unique_id is not None:
            self._attr_unique_id = unique_id
        else:
            self._attr_unique_id = None

    @property
    def master_role(self) -> MasterRole | None:
        """Master strategy, if this entity coordinates satellites."""
        if isinstance(self._role, MasterRole):
            return self._role
        return None

    @property
    def satellite_role(self) -> SatelliteRole | None:
        """Satellite strategy, if this entity belongs to a master."""
        if isinstance(self._role, SatelliteRole):
            return self._role
        return None

    @property
    def is_master(self) -> bool:
        """True when this entity has master_mode."""
        return self.master_role is not None

    @property
    def room_area(self) -> float:
        """Configured satellite area, or summed membership on the master."""
        return self._area

    @property
    def hvac_active(self) -> bool:
        """True when HVAC is heat or cool."""
        return self._hvac_mode in HVAC_ACTIVE

    def control_is_idle(self) -> bool:
        """True when this entity must not request heat or cool.

        Own standby always applies. A satellite is also idle while its
        master is missing, pending, or in standby/emergency; the room
        preset is left unchanged.
        """
        if self.preset_mode == PRESET_STANDBY:
            return True
        if role := self.satellite_role:
            return role.blocks_controller() or role.plant_idle
        return False

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added.

        Attach the listeners.
        """
        self._logger.info("Add thermostat to hass")
        await super().async_added_to_hass()
        self._registry_id = self.entity_id
        await self._role.async_added()

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
        for _, mode_def in self._hvac_def.items():
            entity_list.append(mode_def.get_hvac_switch)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                entity_list,
                self._async_switches_change,
            )
        )

        if self._passive_switch:
            # run at night
            async_track_time_change(
                self.hass,
                self._async_stuck_switch_check,
                hour=self._passive_switch_time.hour,
                minute=self._passive_switch_time.minute,
                second=self._passive_switch_time.second,
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

            # sate the current state
            if save_state:
                self.async_write_ha_state()

            # Check if we have an old state, if so, restore it
            if (old_state := await self.async_get_last_state()) is not None:
                if not self._enable_old_state:
                    # init in case no restore is required
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

            await self.async_set_hvac_mode(self._hvac_mode_init)

        if self.hass.state == CoreState.running:
            await _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

    async def async_will_remove_from_hass(self) -> None:
        """Drop zone membership when the entity is unloaded."""
        await self._role.async_removed()
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
            old_preset_mode = old_state.attributes.get(ATTR_PRESET_MODE, PRESET_NONE)
            old_temperature = old_state.attributes.get(ATTR_TEMPERATURE)
            self._logger.debug(
                "Old state preset mode %s, hvac mode %s, temperature set point '%s'",
                old_preset_mode,
                old_hvac_mode,
                old_temperature,
            )

            # check if old state can be restored
            if (
                old_hvac_mode is None
                or old_hvac_mode not in self.hvac_modes
                or old_preset_mode not in self.preset_modes
                or ATTR_HVAC_DEFINITION not in old_state.attributes
            ):
                raise ValueError(
                    f"Invalid old hvac def '{old_hvac_mode}', start in off mode"
                )

            self._logger.info("restore old controller settings")
            self._hvac_mode_init = old_hvac_mode
            self._preset_mode = old_preset_mode
            self.restore_controller_state(old_state)
            self._role.restore_runtime_state(old_state)

        except Exception as e:
            self._hvac_mode_init = HVACMode.OFF
            self._logger.warning("restoring old state failed:%s", str(e))
            self._logger.debug(traceback.format_exc())
            return

    def restore_controller_state(self, old_state) -> None:
        """Restore HVAC settings and last self_controlled flag from HA state."""
        self._self_controlled = old_state.attributes.get(
            ATTR_SELF_CONTROLLED, OperationMode.SELF
        )
        old_def = old_state.attributes[ATTR_HVAC_DEFINITION]
        old_hvac_mode = old_state.state
        old_temperature = old_state.attributes.get(ATTR_TEMPERATURE)
        for key, data in old_def.items():
            if key in self._hvac_def:
                self._hvac_def[key].restore_reboot(
                    data,
                    self._restore_parameters,
                    self._restore_integral,
                )
            if old_hvac_mode != HVACMode.OFF:
                min_temp, max_temp = self._hvac_def[old_hvac_mode].get_target_temp_limits
                if (
                    old_temperature is not None
                    and min_temp <= old_temperature <= max_temp
                ):
                    self._hvac_def[old_hvac_mode].target_temperature = old_temperature

    @property
    def extra_state_attributes(self) -> dict:
        """Attributes to include in entity."""
        tmp_dict = {}
        for key, data in self._hvac_def.items():
            tmp_dict[key] = data.get_variable_attr

        attrs = {
            ATTR_HVAC_DEFINITION: tmp_dict,
            ATTR_EMERGENCY_MODE: self._emergency_stop,
            ATTR_ANTI_CALC_ACTIVE: self.anti_calc_active,
            ATTR_SELF_CONTROLLED: self._self_controlled,
            ATTR_CURRENT_TEMP_VEL: self.current_temperature_velocity,
            ATTR_CURRENT_OUTDOOR_TEMPERATURE: self.outdoor_temperature,
            ATTR_FILTER_MODE: self.filter_mode,
            CONF_AREA: self._area,
        }
        return self._role.extra_attributes(attrs)

    @property
    def anti_calc_active(self) -> bool:
        """Return if an anti-calc flush is running on this entity."""
        stuck = any(data.stuck_loop for data in self._hvac_def.values())
        if role := self.master_role:
            return role.anti_calc_active or stuck
        return stuck

    def set_detailed_output(self, hvac_mode: HVACMode, new_mode: bool) -> None:
        """Configure attribute output level."""
        self._hvac_def[hvac_mode].detailed_output = new_mode
        self.schedule_update_ha_state()

    @callback
    def async_set_pwm_threshold(
        self, hvac_mode: HVACMode, new_threshold: float
    ) -> None:
        """Set new PID Controller min pwm value."""
        self._logger.info(
            "new minimum for pwm scale for '%s' to: '%s'", hvac_mode, new_threshold
        )
        self._hvac_def[hvac_mode].set_pwm_threshold(new_threshold)
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
        self._hvac_def[hvac_mode].set_pid_param(kp=kp, ki=ki, kd=kd, update=update)
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
            if self._hvac_on:
                self._hvac_on.current_state = None
                self._hvac_on.current_temperature = self.current_temperature
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

    def get_hvac_data(self, hvac_mode: HVACMode) -> list:
        """Retrieve hvac config and entitiy for hvac mode."""
        found_mode = True
        hvac_on = None
        entity_id = None

        if hvac_mode is None:
            hvac_on = self._hvac_on
            hvac_mode = self._hvac_mode
        elif hvac_mode == HVACMode.OFF:
            pass
        elif hvac_mode not in self.hvac_modes:
            found_mode = False
            # self._logger.error(
            #     "Unrecognized hvac mode when retrieving data: '%s'", hvac_mode
            # )
        elif hvac_mode in [HVACMode.HEAT, HVACMode.COOL]:
            hvac_on = self._hvac_def[hvac_mode]

        if hvac_on:
            entity_id = hvac_on.get_hvac_switch

        return [found_mode, hvac_on, entity_id]

    @callback
    def async_set_integral(self, hvac_mode: HVACMode, integral: float) -> None:
        """Set new PID Controller integral value."""
        self._logger.info("new PID integral for '%s' to: '%s'", hvac_mode, integral)
        self._hvac_def[hvac_mode].set_integral(integral)
        self.schedule_update_ha_state()

    @callback
    def async_set_ka_kb(
        self, hvac_mode: HVACMode, ka: float | None = None, kb: float | None = None
    ) -> None:  # pylint: disable=invalid-name
        """Set new weather Controller ka,kb value."""
        self._logger.info("new weatehr ka,kb '%s' to: %s;%s", hvac_mode, ka, kb)
        self._hvac_def[hvac_mode].set_ka_kb(ka=ka, kb=kb)
        self.schedule_update_ha_state()

    @callback
    def async_set_satelite_mode(
        self,
        control_mode: OperationMode,
        offset: float | None = None,
        sat_id: int = 0,
        pwm_start_time: float = 0,
        master_delay: float = 0,
    ) -> None:
        """Satellite update from master (HA service satelite_mode)."""
        if role := self.satellite_role:
            role.set_satellite_mode(
                control_mode,
                offset=offset,
                sat_id=sat_id,
                pwm_start_time=pwm_start_time,
                master_delay=master_delay,
            )

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
        if self.preset_mode in self._hvac_on.custom_presets:
            self._logger.debug(
                "Preset mode {self.preset_mode} active when temperature is updated : skipping change"
            )
            return

        self._hvac_on.target_temperature = round(temperature, 3)

        # operate in all cases except off
        if self._hvac_mode != HVACMode.OFF:
            await self._async_controller(force=True)

        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Change hvac mode."""
        async with self._temp_lock:
            # No changes have been made
            if self._hvac_mode == hvac_mode:
                return

            found_mode, _hvac_on, _ = self.get_hvac_data(hvac_mode)

            if not found_mode:
                return

            self._logger.info("HVAC mode changed to '%s'", hvac_mode)

            # cancel active routines
            if self._hvac_on:
                # cancel scheduled switch routines
                self._async_cancel_pwm_routines(self._hvac_mode, end_stuck_loop=True)

                # stop controller loop
                self._async_routine_controller()

                # stop pwm loop when present
                if self._loop_pwm:
                    self._async_routine_pwm()

                # reset control output
                self.control_output = {
                    ATTR_CONTROL_OFFSET: 0,
                    ATTR_CONTROL_PWM_OUTPUT: 0,
                }

                await self._role.async_on_hvac_leaving()

            # set current mode
            self._old_mode = self._hvac_mode
            self._hvac_mode = hvac_mode
            self._hvac_on = None

            if self._hvac_mode == HVACMode.OFF:
                self._logger.info(
                    "HVAC mode is OFF. Turn the devices OFF and exit hvac change"
                )
                self._self_controlled = OperationMode.SELF
                self._role.on_hvac_entered()
                self.async_write_ha_state()
                return

            # # load current config
            # _hvac_on = self._hvac_def[self._hvac_mode]

            # check and sync preset mode
            if self.preset_mode != _hvac_on.preset_mode:
                await self.async_set_preset_mode(
                    self.preset_mode, hvac_mode=self._hvac_mode
                )

            self._hvac_on = _hvac_on
            if self.control_is_idle():
                self._hvac_on.reset_control_output()
                self.control_output = self._hvac_on.get_control_output

            # reset time stamp pid to avoid integral run-off
            if self._hvac_on.is_prop_pid_mode:
                self.time_changed = time.time()
                self._hvac_on.pid_reset_time()

            # start listening for outdoor sensors
            if self._hvac_on.is_wc_mode and self.outdoor_temperature is not None:
                self._hvac_on.outdoor_temperature = self.outdoor_temperature

            if role := self.satellite_role:
                if await role.should_wait_for_master():
                    self.async_write_ha_state()
                    return

            # thermostat in hysteris mode
            if self._hvac_on.is_hvac_on_off_mode:
                # no need for pwm routine as controller assures update
                if self._hvac_on.get_operate_cycle_time:
                    self._async_routine_controller(self._hvac_on.get_operate_cycle_time)

            # proportional or master start-up
            elif self._role.pwm_follows_controller(self._hvac_on):
                self._pwm_start_time = time.time() + self._role.control_start_delay

                # run controller before pwm loop
                async_track_point_in_utc_time(
                    self.hass,
                    self.async_routine_controller_factory(
                        self._hvac_on.get_operate_cycle_time
                    ),
                    datetime.datetime.fromtimestamp(self._pwm_start_time),
                )

                # run pwm just after controller
                if self._hvac_on.get_pwm_time:
                    async_track_point_in_utc_time(
                        self.hass,
                        self.async_routine_pwm_factory(self._hvac_on.get_pwm_time),
                        datetime.datetime.fromtimestamp(self._pwm_start_time + PWM_LAG),
                    )

            # Ensure we update the current operation after changing the mode
            self._role.on_hvac_entered()
            self.async_write_ha_state()

    @callback
    def async_routine_controller_factory(self, interval: float | None = None):
        """Generate turn on callbacks as factory."""

        # TODO: factory needed?
        async def async_run_routine(now):
            """Run controller with interval."""
            self._async_routine_controller(interval=interval)

        return async_run_routine

    @callback
    def _async_routine_controller(self, interval: float | None = None) -> None:
        """Run main controller at specified interval."""
        self._logger.debug("Update controller loop routine")
        if interval is None and self._loop_controller is not None:
            self._logger.debug("Cancel control loop")
            self._loop_controller()
            self._loop_controller = None
        elif interval is not None and self._loop_controller is not None:
            self._logger.debug("New loop, cancel current control loop")
            self._loop_controller()
            self._loop_controller = None
        elif interval is None:
            self._logger.debug("No control loop to stop")

        if interval and self._loop_controller is None:
            self._logger.debug("Define new control loop")
            self._loop_controller = async_track_time_interval(
                self.hass, self._async_controller, interval
            )
            self.async_on_remove(self._loop_controller)

            # run controller for first time
            self.hass.async_create_task(self._async_controller())

    @callback
    def async_routine_pwm_factory(self, interval: float | None = None):
        """Generate pwm controller callbacks as factory."""

        # TODO: factory needed?
        async def async_run_routine(now):
            """Run pwm controller."""
            self._async_routine_pwm(interval=interval)

        return async_run_routine

    @callback
    def _async_routine_pwm(self, interval: float | None = None) -> None:
        """Run main pwm at specified interval."""
        self._logger.debug("Update pwm loop routine")
        if interval is None and self._loop_pwm is not None:
            self._logger.debug("Cancel pwm loop")
            self._loop_pwm()
            self._loop_pwm = None
        elif interval is not None and self._loop_pwm is not None:
            self._logger.debug("New loop, cancel current pwm loop")
            self._loop_pwm()
            self._loop_pwm = None
        elif interval is None:
            self._logger.debug("No pwm loop to stop")

        if interval and self._loop_pwm is None:
            self._logger.debug("Define new pwm update routine")
            if interval.seconds == 0:
                # no routine needed for proportional valve
                return

            self._loop_pwm = async_track_time_interval(
                self.hass, self._async_controller_pwm, interval
            )
            self.hass.async_create_task(self._async_controller_pwm())
            self.async_on_remove(self._loop_pwm)

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
    def _async_stuck_switch_check(self, now) -> None:
        """Check if the switch has not changed for a certain period and force operation to avoid stuck or jammed."""
        self._role.on_stuck_switch_check()

    def _async_check_stuck_valves(self) -> None:
        """Open a local valve if it has not moved for the stale duration."""
        if self._hvac_on and self._is_valve_open():
            return

        # get data of all switches
        entity_list = {}
        for hvac_mode, mode_config in self._hvac_def.items():
            if mode_config.get_switch_stale:
                entity_list[hvac_mode] = [
                    mode_config.get_hvac_switch,
                    mode_config.get_switch_stale,
                    mode_config.switch_last_change,
                ]

        if not entity_list:
            self._logger.warning(
                "jamming/stuck prevention activated but no duration set for switches"
            )
            return

        # check each switch
        for hvac_mode, data in entity_list.items():
            # check if switch activated emergency mode
            if data[0] in self._emergency_stop:
                return

            self._logger.debug(
                "Switch '%s' stuck prevention check with last update '%s'",
                data[0],
                data[2],
            )

            # check if too long not operated
            if datetime.datetime.now(datetime.UTC) - data[2] > data[1]:
                self._logger.info(
                    "Switch '%s' stuck prevention activated: not changed state for '%s'",
                    data[0],
                    datetime.datetime.now(datetime.UTC) - data[2],
                )

                # run short operation of switch
                self.hass.async_create_task(
                    self._async_toggle_switch(hvac_mode, data[0])
                )

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

            if self._hvac_mode in [HVACMode.HEAT, HVACMode.COOL]:
                other_mode = [HVACMode.HEAT, HVACMode.COOL]
                other_mode.remove(self._hvac_mode)
                if (
                    entity_id != self._hvac_on.get_hvac_switch
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
                # elif (
                #         entity_id == self._hvac_on.get_hvac_switch
                #         and self._is_valve_open()
                #         and self._is_valve_open(hvac_mode=other_mode)
                #     ):

            else:
                # not a current active thermostat thus switch state change should not be triggered
                # unless stuck loop prevention is running
                for hvac_mode, data in self._hvac_def.items():
                    if (
                        data.get_hvac_switch == entity_id
                        and not data.stuck_loop
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

        # if pid/pwm mode is active: do not call operate but let pid/pwm cycle handle it
        if self._hvac_on is not None and self._hvac_on.is_hvac_on_off_mode:
            self.hass.async_create_task(self._async_controller())

        if current_temp:
            self.async_write_ha_state()  # called from controller thus not needed here

    @callback
    def _async_update_outdoor_temperature(
        self, current_temp: float | None = None
    ) -> None:
        """Update thermostat with latest state from outdoor sensor."""
        if current_temp:
            self._logger.debug("Outdoor temperature updated to '%s'", current_temp)
            self._outdoor_temperature = float(current_temp)
            if self._hvac_on:
                self._hvac_on.outdoor_temperature = self._outdoor_temperature

    async def _async_update_controller_temp(self) -> None:
        """Update temperature to controller routines."""
        # TODO: async needed?
        if self._hvac_on:
            if not self._kf_temp:
                self._hvac_on.current_temperature = self._current_temperature
            else:
                self._hvac_on.current_state = [
                    self._kf_temp.get_temp,
                    self._kf_temp.get_vel,
                ]

    async def _async_check_duration(self, routine: bool, force: bool) -> bool:
        """Check if switch change in on-off mode has been long enough.

        on_off is also true when pwm = 0 therefore != _is_pwm_active
        """

        # If the mode is OFF and the device is ON, turn it OFF and exit, else, just exit
        min_cycle_duration = self._hvac_on.get_min_on_off_cycle

        # if the call was made by a sensor change, check the min duration
        # in case of keep-alive (time not none) this test is ignored due to sensor_change = false
        if not force and not routine and min_cycle_duration.seconds != 0:
            entity_id = self._hvac_on.get_hvac_switch
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

    def update_pwm_time(self) -> None:
        """Determine if new pwm cycle has started and update cycle time.

        '_pwm_start_time refers' to start of current pwm cycle.
        """
        pwm_duration = self._hvac_on.get_pwm_time.seconds
        # if time.time() > self._pwm_start_time + pwm_duration:
        while time.time() > self._pwm_start_time + pwm_duration:
            self._pwm_start_time += pwm_duration

    @property
    def pwm_controller_time(self) -> bool:
        """Check if pwm loop is to be started soon."""
        next_pwm_loop = self._pwm_start_time
        now = time.time()
        time_diff = now - next_pwm_loop

        if time_diff > 0 and time_diff < 1:
            self._logger.debug(
                "pwm loop starts soon, time since pwm routine %.2f", time_diff
            )
            return True
        elif (
            time_diff < 0
            and abs(time_diff) / self._hvac_on.get_pwm_time.seconds < CLOSE_TO_PWM
        ):
            self._logger.debug(
                "master hvac init, time since pwm routine %.2f",
                time_diff,
            )
            return True
        else:
            self._logger.debug(
                "no pwm loop to start soon, time since pwm routine %.2f", time_diff
            )
            return False

    @callback
    def async_run_controller_factory(self, force: bool = False):
        """Generate controller callbacks as factory."""

        # TODO: factory needed?
        async def async_run_controller(now: datetime.datetime):
            """Run controller."""
            await self._async_controller(force=force)

        return async_run_controller

    async def _async_controller(
        self, now: datetime.datetime | None = None, force: bool = False
    ) -> None:
        """Check if we need to turn heating on or off."""
        async with self._temp_lock:
            # now is passed by to the callback the async_track_time_interval function , and is set to "now"
            routine = now is not None  # boolean

            self._logger.debug(
                "Controller: calculate output, routine=%s; forced=%s", routine, force
            )

            # check emergency mode
            if self.preset_mode == PRESET_EMERGENCY:
                if not self._emergency_stop:
                    self._async_restore_emergency_stop("")
                self._logger.debug("Controller cancelled due to 'emergency mode'")
                return

            if self.control_is_idle():
                self._logger.debug(
                    "Controller skipped: idle (standby or master plant idle)"
                )
                if self._hvac_on:
                    self._hvac_on.reset_control_output()
                    self.control_output = self._hvac_on.get_control_output
                self.hass.async_create_task(self._async_controller_pwm(force=True))
                return

            # routine should not be called when thermostat is off
            if not self._hvac_on:
                self._logger.warning(
                    "Control update should not be activate when hvac  mode is 'off', exit routine"
                )
                return

            # update and check current temperatures for pwm cycle
            if routine and self._role.uses_room_sensor:
                await self._async_update_current_temp()

            # send temperature to controller
            if self._role.uses_room_sensor:
                await self._async_update_controller_temp()

            # cancel whne no sensor readings are present
            if (
                self._hvac_on.is_hvac_on_off_mode
                or self._hvac_on.is_hvac_proportional_mode
            ):
                if self._sensor_entity_id and self._hvac_on.current_temperature is None:
                    self._logger.debug(
                        "cancel control loop: current temp is None while running controller routine."
                    )
                    return

            # cancel when no outdoor reading
            if self._hvac_on.is_wc_mode:
                if self._sensor_out_entity_id and (
                    self._hvac_on.outdoor_temperature is None
                    or self._hvac_on.target_temperature is None
                ):
                    self._logger.warning(
                        "cancel control loop: current outdoor temp is '%s' and setpoint is '%s' cannot run weather mode",
                        self._hvac_on.outdoor_temperature,
                        self._hvac_on.target_temperature,
                    )
                    return

            # for mode on_off
            if self._hvac_on.is_hvac_on_off_mode:
                if not await self._async_check_duration(routine, force):
                    return

            # determine point in time of current pwm loop
            if self._hvac_on.get_pwm_time.seconds:
                offset = (
                    time.time() - self._pwm_start_time
                ) / self._hvac_on.get_pwm_time.seconds
            else:
                offset = 0

            if role := self.master_role:
                if role.skip_control_near_pwm(routine, offset):
                    # too close to routine, do not include satellite changes
                    return

            # calculate actual pwm
            self._hvac_on.calculate(routine=routine, force=force, current_offset=offset)
            if role := self.master_role:
                role.publish_control()

            # get controller output
            self._hvac_on.calc_control_output()
            self.control_output = self._hvac_on.get_control_output
            self._logger.debug(
                "Obtained current control output: '%s'", self.control_output
            )

            # check if pwm loop needs update
            if (
                force  # forced run
                or self._hvac_on.is_hvac_on_off_mode  # hysteris
                or (
                    self._role.pwm_follows_controller(self._hvac_on)
                    and not self._hvac_on.get_pwm_time  # proportional valve
                )
            ):
                self._logger.debug(
                    "Running pwm controller from control loop with 'force=%s'", force
                )
                self.hass.async_create_task(self._async_controller_pwm(force=force))

            if self._hvac_on.is_hvac_switch_on_off:
                self.async_write_ha_state()

    async def _async_controller_pwm(
        self, now: datetime.datetime | None = None, force: bool = False
    ) -> None:
        """Convert control output to pwm loop."""
        async with self._temp_lock:
            self._logger.debug(
                "Running pwm routine, routine=%s, forced=%s", now is not None, force
            )

            # Anti-calc holds the valve; PWM must not close or reschedule it.
            if self._hvac_on and self._hvac_on.stuck_loop:
                self._logger.debug("PWM skipped: stuck_loop active")
                return

            # keep off in emergency, standby, master plant idle, or pwm = 0
            if (
                self.control_output[ATTR_CONTROL_PWM_OUTPUT] in [None, 0]
                or self._hvac_on is None
                or self.preset_mode == PRESET_EMERGENCY
                or self.control_is_idle()
            ):
                self._async_cancel_pwm_routines()
            # determine switch on-off or valve position
            else:
                if self._hvac_on.get_pwm_time:
                    pwm_duration = self._hvac_on.get_pwm_time.seconds
                else:
                    pwm_duration = None

                # on-off mode switches the pwm between 0 and 100
                if self._hvac_on.is_hvac_on_off_mode:
                    if self.control_output[ATTR_CONTROL_PWM_OUTPUT] <= 0:
                        await self._async_switch_turn_off()
                    else:
                        await self._async_switch_turn_on()

                # convert pwm to on-off switch
                elif pwm_duration:
                    # determine start and end time of valve open
                    now = time.time()
                    self.update_pwm_time()
                    pwm_scale = self._hvac_on.pwm_scale
                    scale_factor = pwm_duration / pwm_scale
                    start_time = (
                        self._pwm_start_time
                        + self.control_output[ATTR_CONTROL_OFFSET] * scale_factor
                    )
                    end_time = (
                        self._pwm_start_time
                        + min(
                            sum(self.control_output.values()), self._hvac_on.pwm_scale
                        )
                        * scale_factor
                    )

                    if self._hvac_on.is_hvac_master_mode:
                        start_time += self._hvac_on.compensate_valve_lag

                    # stop current schedules
                    if self._start_pwm is not None:
                        await self._async_start_pwm()
                    if self._stop_pwm is not None:
                        await self._async_stop_pwm()

                    # negative duration of valve
                    if (
                        # control time is too short
                        end_time <= start_time
                        # valve should be closed
                        or end_time < now
                        # opening time shorter than threshold
                        or end_time - now
                        < max(
                            self._hvac_on.pwm_threshold / pwm_scale * pwm_duration,
                            START_MISALINGMENT,
                        )
                    ):
                        if self._is_valve_open():
                            await self._async_switch_turn_off()
                        return

                    # check if current switch state is matching
                    # if self.control_output[ATTR_CONTROL_PWM_OUTPUT] == pwm_scale:
                    #     await self._async_switch_turn_on()
                    if (
                        start_time - now > START_MISALINGMENT or end_time <= now
                    ) and self._is_valve_open():
                        await self._async_switch_turn_off()
                    elif start_time <= now < end_time:
                        await self._async_switch_turn_on()

                    # schedule new switch changes
                    if start_time > now:
                        await self._async_start_pwm(start_time)
                    if (
                        end_time > now
                        and self.control_output[ATTR_CONTROL_PWM_OUTPUT] != pwm_scale
                    ):
                        await self._async_stop_pwm(end_time)

                # convert pwm to proportional switch and close
                else:
                    valve_open = self._is_valve_open()

                    if (
                        self._hvac_on.pwm_threshold
                        > self.control_output[ATTR_CONTROL_PWM_OUTPUT]
                        and valve_open
                    ):
                        await self._async_switch_turn_off()
                    # convert pwm to proportional switch and change position
                    else:
                        await self._async_switch_turn_on()

    @callback
    def _async_cancel_pwm_routines(self, hvac_mode: HVACMode | None = None, end_stuck_loop: bool = False) -> None:
        """Cancel scheduled switch routines."""
        if self._async_start_pwm is not None:
            self.hass.async_create_task(self._async_start_pwm())
        if self._async_stop_pwm is not None:
            self.hass.async_create_task(self._async_stop_pwm())

        # if self._hvac_on:
        #     # stop switch
        self.hass.async_create_task(self._async_switch_turn_off(hvac_mode=hvac_mode, end_stuck_loop=end_stuck_loop))

    async def _async_start_pwm(
        self, start_time: datetime.datetime | None = None
    ) -> None:
        """Start pwm at specified time."""
        if start_time is None and self._start_pwm is not None:
            self._logger.debug("cancel scheduled switch on")
            self._start_pwm()
            self._start_pwm = None
        elif start_time is not None and self._start_pwm is not None:
            self._logger.debug("Re-define scheduled switch on")
            self._start_pwm()
            self._start_pwm = None
        if start_time and self._start_pwm is None:
            self._logger.debug("Define scheduled switch on")
            self._start_pwm = async_track_point_in_utc_time(
                self.hass,
                self.async_turn_switch_on_factory(),
                datetime.datetime.fromtimestamp(start_time),
            )
            self.async_on_remove(self._start_pwm)

    async def _async_stop_pwm(self, stop_time: datetime.datetime | None = None) -> None:
        """Stop pwm at specified time."""
        if stop_time is None and self._stop_pwm is not None:
            self._logger.debug("cancel scheduled switch off")
            self._stop_pwm()
            self._stop_pwm = None
        elif stop_time is not None and self._stop_pwm is not None:
            self._logger.debug("Re-define scheduled switch off")
            self._stop_pwm()
            self._stop_pwm = None
        if stop_time and self._stop_pwm is None:
            self._logger.debug("Define scheduled switch off")
            self._stop_pwm = async_track_point_in_utc_time(
                self.hass,
                self.async_turn_switch_off_factory(),
                datetime.datetime.fromtimestamp(stop_time),
            )
            self.async_on_remove(self._stop_pwm)

    @callback
    def async_turn_switch_on_factory(
        self, hvac_mode: HVACMode | None = None, control_val: float | None = None
    ):
        """Generate turn on callbacks as factory."""

        # TODO: factory needed?
        async def async_turn_on_switch(now: datetime.datetime):
            """Turn on specific switch."""
            await self._async_switch_turn_on(
                hvac_mode=hvac_mode, control_val=control_val
            )

        return async_turn_on_switch

    def _prop_valve_position(self, hvac_on, control_val: float | None = None):
        """Determine master utilisation for proportional valve scale factor."""
        if not control_val:
            valve_pos = self.control_output[ATTR_CONTROL_PWM_OUTPUT]
        else:
            valve_pos = control_val

        if role := self.satellite_role:
            master_util = role.master_pwm_utilisation(hvac_on)
        else:
            master_util = 1.0

        # scale valve opening with master pwm
        valve_pos /= master_util
        valve_pos = round(max(0, min(valve_pos, hvac_on.pwm_scale)), 0)

        # NC-NO conversion
        if hvac_on.get_hvac_switch_mode == NO_SWITCH_MODE:
            valve_pos = hvac_on.pwm_scale - valve_pos

        return valve_pos

    async def _async_switch_turn_on(
        self, hvac_mode: HVACMode | None = None, control_val: float | None = None
    ) -> None:
        """Open valve or reposition proportional valve.

        NC/NO aware: NC conversion to NO
        """
        self._logger.debug("Turn ON")
        found_mode, _hvac_on, entity_id = self.get_hvac_data(hvac_mode)

        if not entity_id or not found_mode:
            self._logger.debug("No switch defined for %s", hvac_mode)
            return

        # open valve
        if _hvac_on.is_hvac_switch_on_off:
            if self._is_valve_open(hvac_mode=hvac_mode):
                self._logger.debug("Switch already ON")
                return

            data = {ATTR_ENTITY_ID: entity_id}
            self._logger.debug("Order 'ON' sent to switch device '%s'", entity_id)

            # storetime of operation for stuck switch check
            _hvac_on.switch_last_change = datetime.datetime.now(datetime.UTC)

            # NC-NO conversion
            if _hvac_on.get_hvac_switch_mode == NC_SWITCH_MODE:
                operation = SERVICE_TURN_ON
            else:
                operation = SERVICE_TURN_OFF

            await self.hass.services.async_call(
                HA_DOMAIN, operation, data, context=self._context
            )

        # change valve position
        else:
            valve_pos = self._prop_valve_position(_hvac_on, control_val)
            self._logger.debug(
                "Change state of heater '%s' to '%s'",
                entity_id,
                valve_pos,
            )

            # storetime of operation for stuck switch check
            _hvac_on.switch_last_change = datetime.datetime.now(datetime.UTC)
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

    @callback
    def async_turn_switch_off_factory(self, hvac_mode: HVACMode | None = None, end_stuck_loop: bool = False) -> None:
        """Generate turn on callbacks as factory."""

        async def async_turn_off_switch(now: datetime.datetime):
            """Turn off specific switch."""
            await self._async_switch_turn_off(hvac_mode=hvac_mode, end_stuck_loop=end_stuck_loop)

        return async_turn_off_switch

    async def _async_switch_turn_off(self, hvac_mode: HVACMode | None = None, end_stuck_loop: bool = False) -> None:
        """Close valve.

        NC/NO aware: NC converted to NO
        """
        self._logger.debug("Turn OFF called")
        found_mode, _hvac_on, entity_id = self.get_hvac_data(hvac_mode)

        if not entity_id or not found_mode:
            self._logger.debug("No switch defined for %s", hvac_mode)
            return

        # PWM (and other callers) must not close the valve or drop protection
        # while anti-calc holds it. Only the flush timeout / HVAC-off / emergency
        # may end the stuck loop.
        if _hvac_on.stuck_loop and not end_stuck_loop:
            self._logger.debug("Turn OFF skipped: stuck_loop active")
            return

        # operate on-off switch
        if _hvac_on.is_hvac_switch_on_off:
            if not self._is_valve_open(hvac_mode=hvac_mode):
                self._logger.debug("Switch already OFF")
                if end_stuck_loop and _hvac_on.stuck_loop:
                    _hvac_on.stuck_loop = False
                    self.async_write_ha_state()
                return

            data = {ATTR_ENTITY_ID: entity_id}
            self._logger.debug("Order 'OFF' sent to switch device '%s'", entity_id)

            # NC-NO conversion
            if _hvac_on.get_hvac_switch_mode == NC_SWITCH_MODE:
                operation = SERVICE_TURN_OFF
            else:
                operation = SERVICE_TURN_ON

            await self.hass.services.async_call(
                HA_DOMAIN, operation, data, context=self._context
            )

        # operate propoertional valve
        else:
            self._logger.debug(
                "Change state of switch '%s' to '%s'",
                entity_id,
                0,
            )

            if _hvac_on.get_hvac_switch_mode == NC_SWITCH_MODE:
                control_val = 0
            else:
                control_val = _hvac_on.pwm_scale

            data = {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: control_val}
            method = entity_id.split(".")[0]

            await self.hass.services.async_call(
                method,
                SERVICE_SET_VALUE,
                data,
                context=self._context,
            )

        if end_stuck_loop and _hvac_on.stuck_loop:
            _hvac_on.stuck_loop = False
            self.async_write_ha_state()

    async def async_run_stuck_prevention(self, force: bool = False) -> None:
        """Open the valve briefly to prevent sticking (anti-calc)."""
        await self._role.async_run_stuck_prevention(force)

    async def _async_run_local_stuck_prevention(self, force: bool = False) -> None:
        """Open this entity's valve briefly to prevent sticking."""
        if self.preset_mode == PRESET_EMERGENCY:
            self._logger.warning("stuck_prevention skipped: emergency mode")
            return

        hvac_mode = self._hvac_mode if self._hvac_on else HVACMode.HEAT
        if hvac_mode == HVACMode.OFF:
            if HVACMode.HEAT in self._hvac_def:
                hvac_mode = HVACMode.HEAT
            elif HVACMode.COOL in self._hvac_def:
                hvac_mode = HVACMode.COOL
            else:
                return

        found_mode, hvac_on, entity_id = self.get_hvac_data(hvac_mode)
        if not found_mode or not hvac_on or not entity_id:
            self._logger.warning(
                "stuck_prevention skipped: no switch for '%s'", hvac_mode
            )
            return

        if hvac_on.stuck_loop:
            self._logger.debug("stuck_prevention skipped: already active")
            return

        if self._is_valve_open(hvac_mode=hvac_mode):
            self._logger.debug(
                "stuck_prevention skipped: valve '%s' already open", entity_id
            )
            return

        if not hvac_on.get_switch_stale_open_time:
            self._logger.warning(
                "stuck_prevention skipped: no opening time for '%s'", entity_id
            )
            return

        await self._async_toggle_switch(hvac_mode, entity_id)

    async def _async_toggle_switch(self, hvac_mode: HVACMode, entity_id: str) -> None:
        """Toggle the state of a switch temporarily and hereafter set it to 0 or 1."""

        _, _hvac_on, _ = self.get_hvac_data(hvac_mode)
        if not _hvac_on:
            return

        duration = _hvac_on.get_switch_stale_open_time
        _hvac_on.stuck_loop = True
        self.async_write_ha_state()

        self._logger.info(
            "switch '%s' toggle state temporarily to ON for %s sec",
            entity_id,
            duration,
        )

        # Drop pending PWM on/off so a scheduled pulse end cannot close the valve.
        if self._start_pwm is not None:
            await self._async_start_pwm()
        if self._stop_pwm is not None:
            await self._async_stop_pwm()

        # NO-NC conversion
        if _hvac_on.get_hvac_switch_mode == NC_SWITCH_MODE:
            control_val = 0
        else:
            control_val = _hvac_on.pwm_scale

        await self._async_switch_turn_on(hvac_mode=hvac_mode, control_val=control_val)

        # schedule toggle
        async_track_point_in_utc_time(
            self.hass,
            self.async_turn_switch_off_factory(hvac_mode=hvac_mode, end_stuck_loop=True),
            datetime.datetime.fromtimestamp(time.time() + duration.total_seconds()),
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
                self._async_cancel_pwm_routines(end_stuck_loop=True)
            if role := self.master_role:
                role.finish_anti_calc()
        else:
            self._logger.debug("Emergency OFF recall send from %s", source)

    async def _async_check_emergency(self) -> None:
        """Check if emergency mode is valid."""
        # restore preset when empty
        state = True
        if not self._emergency_stop:
            await self.async_set_preset_mode(PRESET_RESTORE)
            state = False

        return state

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

        if self._hvac_on:
            self._logger.debug("Set preset mode to '%s'", preset_mode)
            old_preset = self.preset_mode
            self._hvac_on.preset_mode = preset_mode
            self._preset_mode = self._hvac_on.preset_mode
            if (
                self._preset_mode in (PRESET_STANDBY, PRESET_EMERGENCY)
                and old_preset not in (PRESET_STANDBY, PRESET_EMERGENCY)
            ):
                self._hvac_on.reset_control_output()
                self.control_output = self._hvac_on.get_control_output
                self._async_cancel_pwm_routines()
            elif (
                old_preset in (PRESET_STANDBY, PRESET_EMERGENCY)
                and self._preset_mode not in (PRESET_STANDBY, PRESET_EMERGENCY)
            ):
                if self._hvac_on.is_prop_pid_mode:
                    self._hvac_on.pid_reset_time()
                if self._role.should_run_controller_after_preset():
                    await self._async_controller(force=True)
            elif (
                self.preset_mode != PRESET_EMERGENCY
                and self._role.should_run_controller_after_preset()
            ):
                await self._async_controller(force=True)

        elif self._old_mode != HVACMode.OFF:
            self._logger.debug(
                "Set old hvac mode %s preset mode to '%s'", self._old_mode, preset_mode
            )
            self._hvac_def[self._old_mode].preset_mode = preset_mode
            self._preset_mode = PRESET_NONE

        self.async_write_ha_state()

    def _is_valve_open(self, hvac_mode: HVACMode | None = None) -> bool:
        """Check if the valve is open.

        NC/NO aware: NO converted to NC
        """
        found_mode, _hvac_on, entity_id = self.get_hvac_data(hvac_mode)

        if not entity_id or not found_mode:
            self._logger.debug("no found entity for %s", hvac_mode)
            return False

        try:
            switch_state = self.hass.states.get(entity_id).state
        except:
            self._async_activate_emergency_stop(
                "valve open check entity not found", sensor=entity_id
            )
            return False

        # check if error state
        if switch_state in ERROR_STATE or (
            not _hvac_on.is_hvac_switch_on_off and not is_float(switch_state)
        ):
            self._async_activate_emergency_stop(
                "active switch state check", sensor=entity_id
            )
            return False

        # restore from error state
        if self.preset_mode == PRESET_EMERGENCY:
            self._async_restore_emergency_stop(entity_id)

        return_val = False
        if _hvac_on.is_hvac_switch_on_off:
            if _hvac_on.get_hvac_switch_mode == NC_SWITCH_MODE:
                if switch_state == STATE_ON:
                    return_val = True
            elif switch_state == STATE_OFF:
                return_val = True
        else:
            if _hvac_on.get_hvac_switch_mode == NC_SWITCH_MODE:
                valve_position = float(switch_state)
            else:
                valve_position = _hvac_on.pwm_scale - float(switch_state)

            if valve_position > 0:
                return_val = True

        return return_val

    @property
    def room_min_temp(self) -> float | None:
        """Configured min setpoint while HVAC is on."""
        if self._hvac_on:
            if self._hvac_mode != HVACMode.OFF:
                if self.preset_mode in self._hvac_on.custom_presets:
                    return self._hvac_on.get_preset_temp
                if self._hvac_on.min_target_temp:
                    return self._hvac_on.min_target_temp
            return None
        return None

    @property
    def room_max_temp(self) -> float | None:
        """Configured max setpoint while HVAC is on."""
        if self._hvac_on:
            if self._hvac_mode != HVACMode.OFF:
                if self.preset_mode in self._hvac_on.custom_presets:
                    return self._hvac_on.get_preset_temp
                if self._hvac_on.max_target_temp:
                    return self._hvac_on.max_target_temp
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
            if self._hvac_on:
                if self._hvac_on.is_prop_pid_mode:
                    return self._hvac_on.get_velocity
                return "no velocity calculated"
            return "only available when hvac on"
        return round(self._kf_temp.get_vel, 5)

    @property
    def room_target_temperature(self) -> float | None:
        """Active HVAC target temperature, or None when off."""
        if self._hvac_mode is HVACMode.OFF or self._hvac_mode is None or self._hvac_on is None:
            return None
        return self._hvac_on.target_temperature

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
        if self.master_role:
            return None
        return self.room_min_temp

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        if self.master_role:
            return None
        return self.room_max_temp

    @property
    def current_temperature(self) -> float | None:
        """Return the sensor temperature."""
        if self.master_role and self._hvac_on:
            return None
        return self.room_current_temperature

    @property
    def current_temperature_velocity(self) -> float | None:
        """Return the sensor temperature velocity."""
        if self.master_role and self._hvac_on:
            return None
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
        elif self._hvac_mode == HVACMode.COOL:
            if self._is_valve_open():
                return HVACAction.COOLING
            else:
                return HVACAction.IDLE
        elif self._hvac_mode == HVACMode.HEAT:
            if self._is_valve_open():
                return HVACAction.HEATING
            else:
                return HVACAction.IDLE

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        if self.master_role:
            return 0
        return self.room_target_temperature

    @property
    def hvac_modes(self) -> HVACMode:
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

        _, _hvac_on, _ = self.get_hvac_data(hvac_mode)

        modes = [PRESET_NONE, PRESET_STANDBY, PRESET_EMERGENCY]
        modes = modes + self._role.extra_preset_keys(_hvac_on)
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
