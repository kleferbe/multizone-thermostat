"""Standalone thermostat: room PID/PWM, no zone membership."""

from __future__ import annotations

from .const import OperationMode
from .role import ThermostatRole


class StandaloneRole(ThermostatRole):
    """Room thermostat that is not a satellite and not a master."""

    @property
    def uses_room_sensor(self) -> bool:
        return True

    @property
    def control_start_delay(self) -> float:
        return 0

    def extra_attributes(self, attrs: dict) -> dict:
        return attrs

    async def async_added(self) -> None:
        return

    async def async_removed(self) -> None:
        return

    async def async_on_hvac_leaving(self) -> None:
        return

    def on_hvac_entered(self) -> None:
        return

    def restore_runtime_state(self, old_state) -> None:
        return

    def pwm_follows_controller(self, hvac_on) -> bool:
        return hvac_on.is_hvac_proportional_mode

    def on_stuck_switch_check(self) -> None:
        self.entity._async_check_stuck_valves()

    async def async_run_stuck_prevention(self, force: bool = False) -> None:
        await self.entity._async_run_local_stuck_prevention(force)

    def should_run_controller_after_preset(self) -> bool:
        return self.entity._self_controlled == OperationMode.SELF

    def extra_preset_keys(self, hvac_on) -> list:
        if hvac_on is not None and hvac_on.custom_presets:
            return list(hvac_on.custom_presets.keys())
        return []
