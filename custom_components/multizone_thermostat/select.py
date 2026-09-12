"""Heating-circuit Select: heat / cool / standby / uncoordinated."""

from __future__ import annotations

import datetime
import logging
import time

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.components.select import SelectEntity
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_PROBLEM,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    DOMAIN as HA_DOMAIN,
    CoreState,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers import entity_platform
import voluptuous as vol
import homeassistant.helpers.config_validation as cv

from . import DOMAIN, PLATFORMS
from .circuit_plan import CircuitPlan, CircuitPlanBuilder, RoomDemand
from .const import (
    ATTR_ANTI_CALC_ACTIVE,
    ATTR_CONTROL_OFFSET,
    ATTR_CONTROL_PWM_OUTPUT,
    ATTR_SATELLITES,
    ATTR_TOTAL_AREA,
    CONF_AREA,
    CONF_CONTINUOUS_LOWER_LOAD,
    CONF_ENABLE_OLD_STATE,
    CONF_INCLUDE_VALVE_LAG,
    CONF_INITIAL_OPTION,
    CONF_MASTER_OPERATION_MODE,
    CONF_MIN_VALVE,
    CONF_PASSIVE_CHECK_TIME,
    CONF_PASSIVE_SWITCH_CHECK,
    CONF_PASSIVE_SWITCH_DURATION,
    CONF_PASSIVE_SWITCH_GAP,
    CONF_PASSIVE_SWITCH_OPEN_TIME,
    CONF_PWM_DURATION,
    CONF_PWM_RESOLUTION,
    CONF_PWM_SCALE,
    CONF_PWM_THRESHOLD,
    CONF_SUPPORTED_MODES,
    CONF_SWITCH_MODE,
    CONTROL_START_DELAY,
    DEFAULT_INCLUDE_VALVE_LAG,
    DEFAULT_MIN_DIFF,
    DEFAULT_MIN_LOAD,
    DEFAULT_MIN_VALVE_PWM,
    DEFAULT_OLD_STATE,
    DEFAULT_PASSIVE_CHECK_TIME,
    DEFAULT_PASSIVE_SWITCH,
    DEFAULT_PASSIVE_SWITCH_GAP,
    DEFAULT_PASSIVE_SWITCH_OPEN_TIME,
    DEFAULT_PWM,
    DEFAULT_PWM_RESOLUTION,
    DEFAULT_PWM_SCALE,
    NC_SWITCH_MODE,
    NO_SWITCH_MODE,
    PRESET_EMERGENCY,
    START_MISALINGMENT,
    CircuitMode,
    NestingMode,
)
from .validations import validate_stuck_time
from .zone_registry import async_get_registry

_LOGGER = logging.getLogger(DOMAIN)

ERROR_STATE = [STATE_UNAVAILABLE, STATE_UNKNOWN, STATE_PROBLEM]

PLATFORM_SCHEMA = vol.All(
    cv.PLATFORM_SCHEMA.extend(
        {
            vol.Optional(CONF_NAME, default="Heating circuit"): cv.string,
            vol.Optional(CONF_UNIQUE_ID): cv.string,
            vol.Required(CONF_ENTITY_ID): cv.entity_id,
            vol.Optional(CONF_SWITCH_MODE, default=NC_SWITCH_MODE): vol.In(
                [NC_SWITCH_MODE, NO_SWITCH_MODE]
            ),
            vol.Optional(
                CONF_SUPPORTED_MODES, default=[CircuitMode.HEAT]
            ): vol.All(
                cv.ensure_list,
                [vol.In([CircuitMode.HEAT, CircuitMode.COOL])],
            ),
            vol.Optional(CONF_INITIAL_OPTION, default=CircuitMode.HEAT): vol.In(
                [
                    CircuitMode.HEAT,
                    CircuitMode.COOL,
                    CircuitMode.STANDBY,
                    CircuitMode.UNCOORDINATED,
                ]
            ),
            vol.Optional(CONF_ENABLE_OLD_STATE, default=DEFAULT_OLD_STATE): cv.boolean,
            vol.Optional(
                CONF_MASTER_OPERATION_MODE, default=NestingMode.MASTER_BALANCED
            ): vol.In(
                [
                    NestingMode.MASTER_BALANCED,
                    NestingMode.MASTER_MIN_ON,
                    NestingMode.MASTER_CONTINUOUS,
                ]
            ),
            vol.Optional(
                CONF_INCLUDE_VALVE_LAG, default=DEFAULT_INCLUDE_VALVE_LAG
            ): vol.All(cv.time_period, cv.positive_timedelta),
            vol.Optional(CONF_PWM_DURATION, default=DEFAULT_PWM): vol.All(
                cv.time_period, cv.positive_timedelta
            ),
            vol.Optional(CONF_PWM_SCALE, default=DEFAULT_PWM_SCALE): vol.Coerce(float),
            vol.Optional(
                CONF_PWM_RESOLUTION, default=DEFAULT_PWM_RESOLUTION
            ): vol.Coerce(float),
            vol.Optional(CONF_PWM_THRESHOLD, default=DEFAULT_MIN_DIFF): vol.Coerce(
                float
            ),
            vol.Optional(
                CONF_CONTINUOUS_LOWER_LOAD, default=DEFAULT_MIN_LOAD
            ): vol.Coerce(float),
            vol.Optional(CONF_MIN_VALVE, default=DEFAULT_MIN_VALVE_PWM): vol.Coerce(
                float
            ),
            vol.Optional(
                CONF_PASSIVE_SWITCH_CHECK, default=DEFAULT_PASSIVE_SWITCH
            ): cv.boolean,
            vol.Optional(
                CONF_PASSIVE_CHECK_TIME, default=DEFAULT_PASSIVE_CHECK_TIME
            ): vol.Datetime(format="%H:%M"),
            vol.Optional(CONF_PASSIVE_SWITCH_DURATION): vol.All(
                cv.time_period, cv.positive_timedelta
            ),
            vol.Optional(
                CONF_PASSIVE_SWITCH_OPEN_TIME,
                default=DEFAULT_PASSIVE_SWITCH_OPEN_TIME,
            ): vol.All(cv.time_period, cv.positive_timedelta),
            vol.Optional(
                CONF_PASSIVE_SWITCH_GAP, default=DEFAULT_PASSIVE_SWITCH_GAP
            ): cv.time_period,
        }
    ),
    validate_stuck_time(),
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up one heating-circuit Select entity."""
    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
    _register_services()
    async_add_entities([CircuitSelect(config)])


def _register_services() -> None:
    platform = entity_platform.current_platform.get()
    if platform is None:
        return
    platform.async_register_entity_service(
        "stuck_prevention",
        {vol.Optional("force", default=False): cv.boolean},
        "async_run_stuck_prevention",
    )


class CircuitSelect(SelectEntity, RestoreEntity):
    """Coordinates room climates: nesting, plant PWM, sequential anti-calc."""

    _attr_should_poll = False

    def __init__(self, config: ConfigType) -> None:
        self._attr_name = config.get(CONF_NAME)
        unique_id = config.get(CONF_UNIQUE_ID)
        self._attr_unique_id = unique_id
        self._switch_entity = config[CONF_ENTITY_ID]
        self._switch_mode = config.get(CONF_SWITCH_MODE, NC_SWITCH_MODE)
        climate_modes = list(config.get(CONF_SUPPORTED_MODES) or [CircuitMode.HEAT])
        self._attr_options = [str(mode) for mode in climate_modes] + [
            CircuitMode.STANDBY,
            CircuitMode.UNCOORDINATED,
        ]
        self._initial_option = str(config.get(CONF_INITIAL_OPTION, CircuitMode.HEAT))
        if self._initial_option not in self._attr_options:
            self._initial_option = self._attr_options[0]
        self._attr_current_option = self._initial_option
        self._restore_old_state = config.get(CONF_ENABLE_OLD_STATE, DEFAULT_OLD_STATE)

        self._pwm_duration = config.get(
            CONF_PWM_DURATION, datetime.timedelta(seconds=0)
        ).total_seconds()
        self._pwm_scale = float(config.get(CONF_PWM_SCALE, DEFAULT_PWM_SCALE))
        self._pwm_resolution = float(
            config.get(CONF_PWM_RESOLUTION, DEFAULT_PWM_RESOLUTION)
        )
        self._pwm_threshold = float(config.get(CONF_PWM_THRESHOLD, DEFAULT_MIN_DIFF))
        self._operation_mode = config.get(
            CONF_MASTER_OPERATION_MODE, NestingMode.MASTER_BALANCED
        )
        self._min_load = float(config.get(CONF_CONTINUOUS_LOWER_LOAD, DEFAULT_MIN_LOAD))
        self._min_valve = float(config.get(CONF_MIN_VALVE, DEFAULT_MIN_VALVE_PWM))
        lag = config.get(CONF_INCLUDE_VALVE_LAG, DEFAULT_INCLUDE_VALVE_LAG)
        self._valve_lag = lag.total_seconds() if lag else 0.0

        self._passive_switch = config.get(
            CONF_PASSIVE_SWITCH_CHECK, DEFAULT_PASSIVE_SWITCH
        )
        self._passive_switch_time = config.get(CONF_PASSIVE_CHECK_TIME)
        self._passive_duration = config.get(CONF_PASSIVE_SWITCH_DURATION)
        self._passive_open_time = config.get(
            CONF_PASSIVE_SWITCH_OPEN_TIME, DEFAULT_PASSIVE_SWITCH_OPEN_TIME
        )
        self._passive_gap = config.get(
            CONF_PASSIVE_SWITCH_GAP, DEFAULT_PASSIVE_SWITCH_GAP
        )

        self._logger = logging.getLogger(DOMAIN).getChild(self._attr_name)
        self._registry_id: str | None = None
        self._area = 0.0
        self._pwm_start_time: float | None = None
        self._epoch_timer = None
        self._pwm_start_timer = None
        self._pwm_stop_timer = None
        self._plant_output = {
            ATTR_CONTROL_OFFSET: 0.0,
            ATTR_CONTROL_PWM_OUTPUT: 0.0,
        }
        self._anti_calc_queue: list[str] = []
        self._anti_calc_current: str | None = None
        self._anti_calc_timer = None
        self._plan_builder = CircuitPlanBuilder.create(
            name=self._attr_name or "circuit",
            duration=self._pwm_duration,
            operation_mode=self._operation_mode,
            pwm_scale=self._pwm_scale,
            pwm_threshold=self._pwm_threshold,
            pwm_resolution=self._pwm_resolution,
            min_load=self._min_load,
            min_valve=self._min_valve,
            valve_lag=self._valve_lag,
            plant_entity_id=self._switch_entity,
        )

    @property
    def pwm_duration_seconds(self) -> float:
        """Length of one coordinated PWM window."""
        return self._pwm_duration

    @property
    def is_coordinated(self) -> bool:
        """True when rooms are nested and the plant is PWM'd."""
        return self._attr_current_option in (CircuitMode.HEAT, CircuitMode.COOL)

    @property
    def is_uncoordinated(self) -> bool:
        """True when rooms run their own PWM windows."""
        return self._attr_current_option == CircuitMode.UNCOORDINATED

    @property
    def plant_idle(self) -> bool:
        """True when the plant is out of climate service."""
        return self._attr_current_option == CircuitMode.STANDBY

    @property
    def circuit_hvac_mode(self) -> HVACMode | None:
        """HVAC mode rooms must match to be nested."""
        if self._attr_current_option == CircuitMode.HEAT:
            return HVACMode.HEAT
        if self._attr_current_option == CircuitMode.COOL:
            return HVACMode.COOL
        return None

    @property
    def anti_calc_active(self) -> bool:
        """True while a sequential flush is running (in memory only)."""
        return bool(self._anti_calc_current or self._anti_calc_queue)

    @property
    def extra_state_attributes(self) -> dict:
        registry = async_get_registry(self.hass)
        return {
            ATTR_SATELLITES: registry.member_ids(self.entity_id),
            ATTR_TOTAL_AREA: self._area,
            ATTR_CONTROL_OFFSET: self._plant_output[ATTR_CONTROL_OFFSET],
            ATTR_CONTROL_PWM_OUTPUT: self._plant_output[ATTR_CONTROL_PWM_OUTPUT],
            CONF_PWM_DURATION: self._pwm_duration,
            CONF_PWM_SCALE: self._pwm_scale,
            CONF_MASTER_OPERATION_MODE: self._operation_mode,
            ATTR_ANTI_CALC_ACTIVE: self.anti_calc_active,
            CONF_AREA: self._area,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._registry_id = self.entity_id
        async_get_registry(self.hass).register_circuit(self)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._switch_entity],
                self._async_switch_change,
            )
        )

        if self._passive_switch and self._passive_switch_time:
            async_track_time_change(
                self.hass,
                self._async_stuck_switch_check,
                hour=self._passive_switch_time.hour,
                minute=self._passive_switch_time.minute,
                second=self._passive_switch_time.second,
            )

        async def _async_startup(*_) -> None:
            if self._restore_old_state:
                old_state = await self.async_get_last_state()
                if old_state is not None and old_state.state in self._attr_options:
                    self._attr_current_option = old_state.state
            await self._async_apply_option(self._attr_current_option, starting=True)

        if self.hass.state == CoreState.running:
            await _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

    async def async_will_remove_from_hass(self) -> None:
        self._clear_epoch_timer()
        self._clear_pwm_timers()
        self.finish_anti_calc()
        async_get_registry(self.hass).unregister_circuit(self)
        await super().async_will_remove_from_hass()

    @callback
    def async_registry_entry_updated(self) -> None:
        super().async_registry_entry_updated()
        old_id = self._registry_id
        new_id = self.entity_id
        if not old_id or old_id == new_id:
            return
        async_get_registry(self.hass).rekey_entity(old_id, new_id)
        self._registry_id = new_id

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            self._logger.warning("Ignored unknown circuit option '%s'", option)
            return
        await self._async_apply_option(option)

    async def _async_apply_option(self, option: str, starting: bool = False) -> None:
        previous = self._attr_current_option
        self._attr_current_option = option
        if not starting and previous == option:
            return

        members = async_get_registry(self.hass).members(self.entity_id)
        self._refresh_area()

        if option == CircuitMode.STANDBY:
            self._clear_epoch_timer()
            self._clear_pwm_timers()
            await self._async_plant_off()
            for sat in members:
                await sat.cancel_scheduled_control(close_valve=True)
        elif option == CircuitMode.UNCOORDINATED:
            self._clear_epoch_timer()
            self._clear_pwm_timers()
            await self._async_plant_off()
            self._plant_output = {
                ATTR_CONTROL_OFFSET: 0.0,
                ATTR_CONTROL_PWM_OUTPUT: 0.0,
            }
            for sat in members:
                sat.start_room_control()
        else:
            for sat in members:
                sat.stop_local_epoch()
            self._start_epoch_loop()

        self.async_write_ha_state()
        for sat in members:
            sat.async_write_ha_state()

    def _refresh_area(self) -> None:
        members = async_get_registry(self.hass).members(self.entity_id)
        self._area = sum(sat.room_area for sat in members)

    def _start_epoch_loop(self) -> None:
        self._clear_epoch_timer()
        if not self.is_coordinated or self._pwm_duration <= 0:
            return
        self._pwm_start_time = time.time() + CONTROL_START_DELAY
        self._set_epoch_timer(self._pwm_start_time)

    def _set_epoch_timer(self, when: float) -> None:
        self._clear_epoch_timer()

        async def _fired(now: datetime.datetime | None = None) -> None:
            self._epoch_timer = None
            await self._on_epoch_timer(now)

        self._epoch_timer = async_track_point_in_utc_time(
            self.hass,
            _fired,
            datetime.datetime.fromtimestamp(when),
        )

    def _clear_epoch_timer(self) -> None:
        if self._epoch_timer is None:
            return
        self._epoch_timer()
        self._epoch_timer = None

    async def _on_epoch_timer(self, now: datetime.datetime | None = None) -> None:
        if not self.is_coordinated:
            return

        now_ts = time.time()
        if self._pwm_start_time is None:
            self._pwm_start_time = now_ts
        else:
            # Timer fires at window start. Snap onto the due window, then run it now.
            while self._pwm_start_time + self._pwm_duration <= now_ts:
                self._pwm_start_time += self._pwm_duration

        self._refresh_area()
        members = async_get_registry(self.hass).members(self.entity_id)
        hvac_mode = self.circuit_hvac_mode
        demands: list[RoomDemand] = []
        for sat in members:
            await sat.plan()
            data = sat.nesting_input(hvac_mode)
            if data:
                demands.append(data)

        plan = self._plan_builder.build(
            epoch=self._pwm_start_time,
            hvac_mode=hvac_mode,
            demands=demands,
            tot_area=self._area,
        )
        self._plant_output = self._plant_output_from(plan)
        await self._schedule_plant(plan)
        for sat in members:
            await sat.schedule_valve(plan)

        self.async_write_ha_state()
        self._set_epoch_timer(self._pwm_start_time + self._pwm_duration)

    def _plant_output_from(self, plan: CircuitPlan) -> dict:
        """Duty-cycle attributes in pwm_scale units, derived from plant times."""
        plant = plan.plant
        if (
            plant.open_at is None
            or plant.close_at is None
            or plan.duration <= 0
            or self._pwm_scale <= 0
        ):
            return {
                ATTR_CONTROL_OFFSET: 0.0,
                ATTR_CONTROL_PWM_OUTPUT: 0.0,
            }
        start = plant.open_at - self._valve_lag
        offset = max(0.0, (start - plan.epoch) / plan.duration * self._pwm_scale)
        pwm = (plant.close_at - plant.open_at) / plan.duration * self._pwm_scale
        return {
            ATTR_CONTROL_OFFSET: offset,
            ATTR_CONTROL_PWM_OUTPUT: pwm,
        }

    async def _schedule_plant(self, plan: CircuitPlan) -> None:
        self._clear_pwm_timers()
        plant = plan.plant
        start_time = plant.open_at
        end_time = plant.close_at
        if start_time is None or end_time is None or plan.duration <= 0:
            await self._async_plant_off()
            return

        now = time.time()
        min_on = (
            self._pwm_threshold / self._pwm_scale * plan.duration
            if self._pwm_scale
            else 0
        )
        if end_time <= start_time or end_time < now:
            await self._async_plant_off()
            return
        if end_time - now < max(min_on, START_MISALINGMENT):
            await self._async_plant_off()
            return

        if start_time - now > START_MISALINGMENT and self._is_plant_on():
            await self._async_plant_off()
        elif start_time <= now < end_time:
            await self._async_plant_on()

        if start_time > now:
            self._set_pwm_start_timer(start_time)
        full_window = end_time - start_time >= plan.duration - 1e-6
        if end_time > now and not full_window:
            self._set_pwm_stop_timer(end_time)

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
        await self._async_plant_on()

    async def _on_pwm_stop_timer(self, _now: datetime.datetime | None = None) -> None:
        await self._async_plant_off()

    def _is_plant_on(self) -> bool:
        state = self.hass.states.get(self._switch_entity)
        if state is None or state.state in ERROR_STATE:
            return False
        if self._switch_mode == NC_SWITCH_MODE:
            return state.state == STATE_ON
        return state.state == STATE_OFF

    async def _async_plant_on(self) -> None:
        if self._is_plant_on():
            return
        operation = (
            SERVICE_TURN_ON if self._switch_mode == NC_SWITCH_MODE else SERVICE_TURN_OFF
        )
        await self.hass.services.async_call(
            HA_DOMAIN,
            operation,
            {ATTR_ENTITY_ID: self._switch_entity},
            context=self._context,
        )

    async def _async_plant_off(self) -> None:
        if not self._is_plant_on():
            return
        operation = (
            SERVICE_TURN_OFF if self._switch_mode == NC_SWITCH_MODE else SERVICE_TURN_ON
        )
        await self.hass.services.async_call(
            HA_DOMAIN,
            operation,
            {ATTR_ENTITY_ID: self._switch_entity},
            context=self._context,
        )

    @callback
    def _async_switch_change(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        if new_state.state in ERROR_STATE:
            self._logger.warning(
                "Plant switch '%s' is %s", self._switch_entity, new_state.state
            )
        self.async_write_ha_state()

    @callback
    def _async_stuck_switch_check(self, now) -> None:
        self.hass.async_create_task(self.async_run_stuck_prevention())

    async def async_run_stuck_prevention(self, force: bool = False) -> None:
        """Build a satellite flush queue and run it sequentially."""
        if self._anti_calc_current or self._anti_calc_queue:
            self._logger.debug("anti-calc skipped: sequence already running")
            return
        if self.plant_idle:
            # Standby still flushes; uncoordinated rooms do it themselves.
            pass
        if self.is_uncoordinated:
            self._logger.debug("anti-calc skipped: circuit is uncoordinated")
            return

        duration = self._passive_duration
        if not force and not duration:
            self._logger.warning(
                "anti-calc skipped: set passive_switch_duration on the circuit"
            )
            return

        hvac_mode = self.circuit_hvac_mode
        now = datetime.datetime.now(datetime.UTC)
        queue: list[str] = []
        for sat in async_get_registry(self.hass).members(self.entity_id):
            if hvac_mode and sat.hvac_mode != hvac_mode:
                continue
            if sat.preset_mode in (PRESET_EMERGENCY,):
                continue
            if sat.anti_calc_active:
                continue
            if sat.hvac_action in (HVACAction.HEATING, HVACAction.COOLING):
                continue
            if not force and duration:
                last = sat.switch_last_change()
                if last is not None and now - last <= duration:
                    continue
            queue.append(sat.entity_id)

        if not queue:
            self._logger.debug("anti-calc: no satellites need a flush")
            return

        self._logger.info("anti-calc sequence start: %s (force=%s)", queue, force)
        self._anti_calc_queue = queue
        self._anti_calc_current = None
        self.async_write_ha_state()
        await self._on_anti_calc_timer()

    async def _on_anti_calc_timer(self) -> None:
        if self.is_uncoordinated:
            self.finish_anti_calc()
            return
        if not self._anti_calc_queue:
            self._anti_calc_current = None
            self._logger.info("anti-calc sequence complete")
            self.async_write_ha_state()
            return

        sat_id = self._anti_calc_queue.pop(0)
        self._anti_calc_current = sat_id
        self.async_write_ha_state()
        self._logger.info(
            "anti-calc flushing '%s', remaining %s", sat_id, self._anti_calc_queue
        )

        sat = async_get_registry(self.hass).climate(sat_id)
        if sat is not None:
            await sat.run_stuck_prevention()

        opening_s = (
            self._passive_open_time.total_seconds() if self._passive_open_time else 0
        )
        gap_s = self._passive_gap.total_seconds() if self._passive_gap else 0
        delay_s = opening_s + gap_s if self._anti_calc_queue else opening_s
        self._set_anti_calc_timer(delay_s)

    def _set_anti_calc_timer(self, delay_s: float) -> None:
        self._clear_anti_calc_timer()
        if delay_s <= 0:
            self.hass.async_create_task(self._on_anti_calc_timer())
            return

        async def _fired(_now: datetime.datetime) -> None:
            self._anti_calc_timer = None
            await self._on_anti_calc_timer()

        self._anti_calc_timer = async_track_point_in_utc_time(
            self.hass,
            _fired,
            datetime.datetime.fromtimestamp(time.time() + delay_s),
        )

    def _clear_anti_calc_timer(self) -> None:
        if self._anti_calc_timer is None:
            return
        self._anti_calc_timer()
        self._anti_calc_timer = None

    def finish_anti_calc(self) -> None:
        """Stop the in-memory flush sequence. Do not close satellite valves."""
        self._clear_anti_calc_timer()
        had_work = bool(self._anti_calc_current or self._anti_calc_queue)
        self._anti_calc_queue = []
        self._anti_calc_current = None
        if had_work:
            self._logger.info("anti-calc sequence finished")
            self.async_write_ha_state()
