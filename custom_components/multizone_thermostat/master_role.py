"""Master thermostat: coordinates registered satellites."""

from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    HVACAction,
    HVACMode,
)
from homeassistant.const import STATE_PROBLEM, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)

from .const import (
    ATTR_ANTI_CALC_QUEUE,
    ATTR_ANTI_CALC_SATELLITE,
    ATTR_EMERGENCY_MODE,
    ATTR_HVAC_DEFINITION,
    ATTR_LAST_SWITCH_CHANGE,
    ATTR_SELF_CONTROLLED,
    ATTR_STUCK_LOOP,
    CONTROL_START_DELAY,
    PRESET_EMERGENCY,
    PRESET_STANDBY,
    OperationMode,
)
from .role import ThermostatRole
from .zone_registry import async_get_registry

if TYPE_CHECKING:
    from homeassistant.core import Event

    from .climate import MultiZoneThermostat
    from .satellite_role import SatelliteRole

ERROR_STATE = [STATE_UNAVAILABLE, STATE_UNKNOWN, STATE_PROBLEM]
HVAC_ACTIVE = [HVACMode.HEAT, HVACMode.COOL]


class MasterRole(ThermostatRole):
    """Membership lives in the registry; HVAC off does not unregister."""

    def __init__(self, thermostat: MultiZoneThermostat) -> None:
        super().__init__(thermostat)
        self._anti_calc_queue: list[str] = []
        self._anti_calc_current: str | None = None
        self._anti_calc_unsub = None
        self._satelites = None

    @property
    def anti_calc_active(self) -> bool:
        return bool(self._anti_calc_current or self._anti_calc_queue)

    @property
    def uses_room_sensor(self) -> bool:
        return False

    @property
    def control_start_delay(self) -> float:
        return CONTROL_START_DELAY

    def extra_attributes(self, attrs: dict) -> dict:
        attrs[ATTR_ANTI_CALC_SATELLITE] = self._anti_calc_current
        attrs[ATTR_ANTI_CALC_QUEUE] = list(self._anti_calc_queue)
        return attrs

    def member_ids(self) -> list[str]:
        return sorted(async_get_registry(self.entity.hass).satellites_of(self))

    def restore_runtime_state(self, old_state) -> None:
        return

    def pwm_follows_controller(self, hvac_on) -> bool:
        return True

    def skip_control_near_pwm(self, routine, offset: float) -> bool:
        return routine is None and self.entity._hvac_on.close_to_routine(offset)

    def publish_control(self) -> None:
        satelite_info = self.entity._hvac_on.get_satelite_offset()
        self._change_satellite_modes(satelite_info)

    def on_stuck_switch_check(self) -> None:
        t = self.entity
        t.hass.async_create_task(self.async_start_anti_calc())

    async def async_run_stuck_prevention(self, force: bool = False) -> None:
        await self.async_start_anti_calc(force=force)

    def should_run_controller_after_preset(self) -> bool:
        return True

    @property
    def plant_idle(self) -> bool:
        """True when the plant is out of climate service."""
        return self.entity.preset_mode in (PRESET_STANDBY, PRESET_EMERGENCY)

    def extra_preset_keys(self, hvac_on) -> list:
        if hvac_on is None:
            return []
        return list(hvac_on.custom_presets.keys())

    def enroll_satellite(self, sat: SatelliteRole) -> None:
        self.sync_registered_satellites()
        self.bind_satellite(sat)
        self._refresh_satellite_tracking()

    def unenroll_satellite(self, sat: SatelliteRole) -> None:
        self.sync_registered_satellites()
        self._refresh_satellite_tracking()

    def bind_satellite(self, sat: SatelliteRole) -> None:
        t = self.entity
        if t.hvac_active and sat.entity.hvac_active:
            sat.apply_master_control(self)
        elif sat.entity._self_controlled != OperationMode.SELF:
            sat.apply_self_control()
        sat.entity.async_write_ha_state()

    def bind_all(self) -> None:
        registry = async_get_registry(self.entity.hass)
        for sat in list(registry.satellites_of(self).values()):
            self.bind_satellite(sat)

    def sync_registered_satellites(self) -> None:
        t = self.entity
        if t.entity_id is None:
            return
        sats = async_get_registry(t.hass).satellites_of(self)
        ids = sorted(sats)
        area = sum(sat.entity.room_area for sat in sats.values())
        t._area = area
        for mode in t._hvac_def.values():
            if mode.is_hvac_master_mode:
                mode.set_registered_satelites(ids)
                mode.update_area(area)

    def _refresh_satellite_tracking(self) -> None:
        t = self.entity
        if not t.hvac_active:
            return
        ids = self.member_ids()
        t.hass.async_create_task(self.async_track_satellites(ids or None))

    async def async_added(self) -> None:
        async_get_registry(self.entity.hass).register_master(self)

    async def async_removed(self) -> None:
        t = self.entity
        await self.async_track_satellites()
        registry = async_get_registry(t.hass)
        for sat in list(registry.satellites_of(self).values()):
            sat.apply_self_control()
        registry.unregister_master(self)

    async def async_on_hvac_leaving(self) -> None:
        t = self.entity
        self.finish_anti_calc()
        await self.async_track_satellites()
        if t._hvac_on:
            t._hvac_on.restore_satelites()

    def on_hvac_entered(self) -> None:
        t = self.entity
        self.sync_registered_satellites()
        if t.hvac_active:
            if t._hvac_on:
                t._hvac_on.restore_satelites()
            self._refresh_satellite_tracking()
        self.bind_all()

    async def async_track_satellites(self, entity_list: list | None = None) -> None:
        """Follow changes from satellite thermostats (full entity_ids)."""
        if self._satelites is not None:
            self._satelites()
            self._satelites = None

        if entity_list:
            self._satelites = async_track_state_change_event(
                self.entity.hass, entity_list, self._async_satellite_change
            )

    @callback
    def _async_satellite_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle satellite thermostat changes."""
        t = self.entity
        new_state = event.data.get("new_state")
        if not new_state:
            t._logger.error("Error receiving thermostat update. 'None' received")
            return
        t._logger.debug("Receiving update from '%s'", new_state.name)

        for hvac_def in new_state.attributes[ATTR_HVAC_DEFINITION].values():
            if hvac_def.get(ATTR_STUCK_LOOP):
                t._logger.debug(
                    "'%s' is in stuck loop, ignore update",
                    new_state.name,
                )
                return

        if not t._hvac_on:
            return

        sat = async_get_registry(t.hass).satellites_of(self).get(new_state.entity_id)
        if (
            sat
            and sat.entity.hvac_active
            and sat.entity._self_controlled != OperationMode.MASTER
        ):
            self.bind_satellite(sat)
            return

        update_required = t._hvac_on.update_satelite(new_state)
        if update_required and not t.pwm_controller_time:
            t._logger.debug(
                "Significant update from satelite: '%s' rerun controller",
                new_state.name,
            )
            t.hass.async_create_task(t._async_controller(force=True))

    def _change_satellite_modes(
        self, data: dict, control_mode: OperationMode = OperationMode.NO_CHANGE
    ) -> None:
        """Create tasks to update satellites and/or PWM offset."""
        t = self.entity
        if not data:
            t._logger.debug("No satelite data to send")
            return

        ids = self.member_ids()
        for satelite, offset in data.items():
            if control_mode == OperationMode.MASTER:
                try:
                    sat_id = ids.index(satelite) + 1
                except ValueError:
                    continue
                delay = t._hvac_on.compensate_valve_lag
            else:
                sat_id = 0
                delay = 0

            t.hass.async_create_task(
                self._async_send_satellite_data(
                    satelite,
                    offset,
                    control_mode=control_mode,
                    sat_id=sat_id,
                    pwm_start_time=t._pwm_start_time,
                    master_delay=delay,
                )
            )

    async def _async_send_satellite_data(
        self,
        satelite: str,
        offset: float,
        control_mode: OperationMode = OperationMode.NO_CHANGE,
        sat_id: int = 0,
        pwm_start_time: int = 0,
        master_delay: float = 0,
    ) -> None:
        """Send a control update to a satellite."""
        t = self.entity
        t._logger.debug(
            "send data to satelite %s %s %s", satelite, offset, control_mode
        )
        sat = async_get_registry(t.hass).satellite(t.entity_id, satelite)
        if sat is None:
            return
        sat.set_satellite_mode(
            control_mode,
            offset=offset,
            sat_id=sat_id,
            pwm_start_time=pwm_start_time,
            master_delay=master_delay,
        )

    def _cancel_anti_calc_schedule(self) -> None:
        if self._anti_calc_unsub is not None:
            self._anti_calc_unsub()
            self._anti_calc_unsub = None

    def finish_anti_calc(self) -> None:
        """Stop the anti-calc sequence without closing satellite valves."""
        t = self.entity
        self._cancel_anti_calc_schedule()
        had_work = bool(self._anti_calc_current or self._anti_calc_queue)
        self._anti_calc_queue = []
        self._anti_calc_current = None
        if had_work:
            t._logger.info("anti-calc sequence finished")
            t.async_write_ha_state()

    def _parse_switch_last_change(self, value) -> datetime.datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=datetime.UTC)
            return value
        if isinstance(value, str):
            try:
                parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=datetime.UTC)
            return parsed
        return None

    def _schedule_anti_calc_next(self, delay_s: float) -> None:
        t = self.entity
        self._cancel_anti_calc_schedule()
        if delay_s <= 0:
            t.hass.async_create_task(self._async_anti_calc_next())
            return

        async def _run(now: datetime.datetime) -> None:
            self._anti_calc_unsub = None
            await self._async_anti_calc_next()

        self._anti_calc_unsub = async_track_point_in_utc_time(
            t.hass,
            _run,
            datetime.datetime.fromtimestamp(time.time() + delay_s),
        )

    async def async_start_anti_calc(self, force: bool = False) -> None:
        """Build a satellite flush queue and start sequential anti-calc."""
        t = self.entity
        if self._anti_calc_current or self._anti_calc_queue:
            t._logger.debug("anti-calc skipped: sequence already running")
            return

        if t.preset_mode == PRESET_EMERGENCY:
            t._logger.warning("anti-calc skipped: emergency mode")
            return

        if t._hvac_mode not in HVAC_ACTIVE:
            t._logger.debug("anti-calc skipped: master not in heat/cool")
            return

        hvac_on = t._hvac_on
        if not hvac_on or not hvac_on.is_hvac_master_mode:
            return

        duration = hvac_on.get_switch_stale
        if not force and not duration:
            t._logger.warning(
                "anti-calc skipped: set heat.passive_switch_duration on the master"
            )
            return

        satelites = hvac_on.get_satelites or []
        now = datetime.datetime.now(datetime.UTC)
        queue: list[str] = []

        for sat in satelites:
            state = t.hass.states.get(sat)
            if not state or state.state in ERROR_STATE:
                continue
            if state.state != t._hvac_mode:
                continue
            if state.attributes.get(ATTR_PRESET_MODE) == PRESET_EMERGENCY:
                continue
            emergency = state.attributes.get(ATTR_EMERGENCY_MODE)
            if emergency:
                continue
            self_ctrl = state.attributes.get(ATTR_SELF_CONTROLLED)
            if self_ctrl == OperationMode.SELF:
                continue
            mode_def = (state.attributes.get(ATTR_HVAC_DEFINITION) or {}).get(
                t._hvac_mode
            ) or {}
            if mode_def.get(ATTR_STUCK_LOOP):
                continue
            if state.attributes.get("hvac_action") in (
                HVACAction.HEATING,
                HVACAction.COOLING,
            ):
                continue
            if not force and duration:
                last = self._parse_switch_last_change(
                    mode_def.get(ATTR_LAST_SWITCH_CHANGE)
                )
                if last is not None and now - last <= duration:
                    continue
            queue.append(sat)

        if not queue:
            t._logger.debug("anti-calc: no satellites need a flush")
            return

        t._logger.info("anti-calc sequence start: %s (force=%s)", queue, force)
        self._anti_calc_queue = queue
        self._anti_calc_current = None
        t.async_write_ha_state()
        await self._async_anti_calc_next()

    async def _async_anti_calc_next(self) -> None:
        """Flush the next satellite, or finish the sequence."""
        t = self.entity
        if t._hvac_mode not in HVAC_ACTIVE or t.preset_mode == PRESET_EMERGENCY:
            self.finish_anti_calc()
            return

        if not self._anti_calc_queue:
            self._anti_calc_current = None
            t._logger.info("anti-calc sequence complete")
            t.async_write_ha_state()
            return

        hvac_on = t._hvac_on
        if not hvac_on:
            self.finish_anti_calc()
            return

        sat = self._anti_calc_queue.pop(0)
        self._anti_calc_current = sat
        t.async_write_ha_state()

        t._logger.info(
            "anti-calc flushing '%s', remaining %s", sat, self._anti_calc_queue
        )

        sat_entity = async_get_registry(t.hass).satellite(t.entity_id, sat)
        if sat_entity is not None:
            await sat_entity.async_run_stuck_prevention()

        opening = hvac_on.get_switch_stale_open_time
        gap = hvac_on.get_switch_stale_gap
        opening_s = opening.total_seconds() if opening else 0
        gap_s = gap.total_seconds() if gap else 0

        if self._anti_calc_queue:
            delay_s = opening_s + gap_s
        else:
            delay_s = opening_s

        self._schedule_anti_calc_next(delay_s)
