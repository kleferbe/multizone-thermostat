"""Satellite thermostat: belongs to a master entity_id."""

from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING

from homeassistant.components.climate import HVACMode
from homeassistant.helpers.event import async_track_point_in_utc_time

from .const import (
    ATTR_CONTROL_OFFSET,
    ATTR_CONTROL_PWM_OUTPUT,
    CONTROL_START_DELAY,
    MASTER_CONTROL_LEAD,
    PRESET_EMERGENCY,
    PRESET_STANDBY,
    PWM_LAG,
    SAT_CONTROL_LEAD,
    OperationMode,
)
from .standalone_role import StandaloneRole
from .zone_registry import async_get_registry

if TYPE_CHECKING:
    from .master_role import MasterRole


class SatelliteRole(StandaloneRole):
    """Room thermostat that belongs to a master entity_id."""

    def __init__(self, thermostat, master_id: str) -> None:
        super().__init__(thermostat)
        self.master_id = master_id
        self._sat_id = 0

    @property
    def associated_master_role(self) -> MasterRole | None:
        """Registered master for this satellite, if it is already in hass."""
        return async_get_registry(self.entity.hass).master(self.master_id)

    async def async_added(self) -> None:
        async_get_registry(self.entity.hass).register_satellite(self, self.master_id)

    async def async_removed(self) -> None:
        self.apply_self_control()
        async_get_registry(self.entity.hass).unregister_satellite(self, self.master_id)

    def on_hvac_entered(self) -> None:
        if master := self.associated_master_role:
            master.bind_satellite(self)

    def restore_runtime_state(self, old_state) -> None:
        t = self.entity
        if t._self_controlled == OperationMode.MASTER:
            t._logger.info("change state to pending master update")
            t._self_controlled = OperationMode.PENDING

    def on_stuck_switch_check(self) -> None:
        t = self.entity
        if t._self_controlled in (OperationMode.MASTER, OperationMode.PENDING):
            return
        t._async_check_stuck_valves()

    def blocks_controller(self) -> bool:
        """True while waiting for the master role to exist and claim this satellite."""
        if self.associated_master_role is None:
            return True
        return self.entity._self_controlled == OperationMode.PENDING

    @property
    def plant_idle(self) -> bool:
        """True when the master has taken the plant out of climate service."""
        if self.entity._self_controlled not in (
            OperationMode.MASTER,
            OperationMode.PENDING,
        ):
            return False
        if master := self.associated_master_role:
            return master.plant_idle
        return False

    def should_run_controller_after_preset(self) -> bool:
        t = self.entity
        if self.blocks_controller():
            return False
        if t.preset_mode in (PRESET_STANDBY, PRESET_EMERGENCY):
            return False
        if self.plant_idle:
            return False
        return True

    async def should_wait_for_master(self) -> bool:
        t = self.entity
        if t._self_controlled == OperationMode.MASTER:
            t._logger.info(
                "HVAC change to active state in MASTER state, wait for MASTER. Set on PENDING and wait for master"
            )
            t._self_controlled = OperationMode.PENDING
            self.on_hvac_entered()
            return True
        if t._self_controlled == OperationMode.PENDING:
            t._logger.info(
                "HVAC change in pending state, wait for MASTER. Turn the switch OFF and exit hvac change"
            )
            await t._async_switch_turn_off()
            self.on_hvac_entered()
            return True
        return False

    def apply_master_control(self, master: MasterRole) -> None:
        ids = master.member_ids()
        try:
            sat_id = ids.index(self.entity.entity_id) + 1
        except ValueError:
            sat_id = 1
        delay = 0
        if master.entity._hvac_on:
            delay = master.entity._hvac_on.compensate_valve_lag
        self.set_satellite_mode(
            OperationMode.MASTER,
            offset=0,
            sat_id=sat_id,
            pwm_start_time=master.entity._pwm_start_time or time.time(),
            master_delay=delay,
        )

    def apply_self_control(self) -> None:
        self.set_satellite_mode(OperationMode.SELF)

    def set_satellite_mode(
        self,
        control_mode: OperationMode,
        offset: float | None = None,
        sat_id: int = 0,
        pwm_start_time: float = 0,
        master_delay: float = 0,
    ) -> None:
        """Apply a control update from the master (or the satelite_mode service)."""
        t = self.entity
        pwm_loop = False
        t._logger.info(
            "sat update received for mode:'%s'; offset:'%s'", control_mode, offset
        )

        if t._old_mode == HVACMode.OFF and t._hvac_on is None:
            t._self_controlled = control_mode
            t._pwm_start_time = pwm_start_time
            return

        if t._hvac_on is None:
            return

        if not t._hvac_on.is_hvac_proportional_mode:
            t._logger.warning("sat update for non-proportional thermostat")
            return

        if offset is not None:
            if t.control_output[ATTR_CONTROL_OFFSET] != offset:
                t._hvac_on.time_offset = offset
                t.control_output[ATTR_CONTROL_OFFSET] = offset
            pwm_loop = True
        else:
            t._hvac_on.time_offset = 0
            t.control_output[ATTR_CONTROL_OFFSET] = 0
            pwm_loop = True

        if (
            control_mode == OperationMode.SELF
            and t._self_controlled != OperationMode.SELF
        ):
            t._logger.debug("sat update to self-controlled state")
            t._self_controlled = OperationMode.SELF
            t._hvac_on.master_delay = 0
            self._sat_id = 0
            t._async_routine_controller()
            t._async_cancel_pwm_routines(end_stuck_loop=True)
            t._pwm_start_time = time.time() + CONTROL_START_DELAY

            async_track_point_in_utc_time(
                t.hass,
                t.async_routine_controller_factory(t._hvac_on.get_operate_cycle_time),
                datetime.datetime.fromtimestamp(t._pwm_start_time),
            )
            async_track_point_in_utc_time(
                t.hass,
                t.async_routine_pwm_factory(t._hvac_on.get_pwm_time),
                datetime.datetime.fromtimestamp(t._pwm_start_time + PWM_LAG),
            )

        elif control_mode == OperationMode.MASTER:
            if t._self_controlled in [
                OperationMode.PENDING,
                OperationMode.SELF,
            ]:
                t._pwm_start_time = pwm_start_time
                self._sat_id = sat_id
                t._self_controlled = OperationMode.MASTER
                t._hvac_on.master_delay = master_delay

                if t._loop_controller:
                    t._logger.debug("sat update: stopping controller routine")
                    t._async_routine_controller()
                if t._loop_pwm:
                    t._logger.debug("sat update: stopping pwm routine")
                    t._async_routine_pwm()

                t._async_cancel_pwm_routines(end_stuck_loop=True)

                async_track_point_in_utc_time(
                    t.hass,
                    t.async_routine_controller_factory(
                        t._hvac_on.get_operate_cycle_time
                    ),
                    datetime.datetime.fromtimestamp(
                        t._pwm_start_time
                        - sat_id * SAT_CONTROL_LEAD
                        - MASTER_CONTROL_LEAD
                    ),
                )
                pwm_loop = False

        if pwm_loop:
            t.hass.async_create_task(t._async_controller_pwm(force=True))

    def master_pwm_utilisation(self, hvac_on) -> float:
        t = self.entity
        if t._self_controlled != OperationMode.MASTER or hvac_on.master_scaled_bound <= 1:
            return 1.0
        if not (master := self.associated_master_role) or master.entity.hvac_mode != t.hvac_mode:
            return 1.0
        master_hvac = master.entity._hvac_on
        if not master_hvac or master_hvac.pwm_scale <= 0:
            return 1.0
        return max(
            1 / hvac_on.master_scaled_bound,
            master.entity.control_output[ATTR_CONTROL_PWM_OUTPUT] / master_hvac.pwm_scale,
        )
