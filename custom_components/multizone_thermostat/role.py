"""Abstract thermostat role: strategy that climate.py dispatches for every entity."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .climate import MultiZoneThermostat


class ThermostatRole(ABC):
    """Strategy attached to a MultiZoneThermostat. Zone peers are typed roles."""

    def __init__(self, thermostat: MultiZoneThermostat) -> None:
        self.thermostat = thermostat

    @property
    def entity(self) -> MultiZoneThermostat:
        """HA climate shell for this role."""
        return self.thermostat

    @property
    @abstractmethod
    def uses_room_sensor(self) -> bool:
        """Whether the control loop reads a room temperature sensor."""

    @property
    @abstractmethod
    def control_start_delay(self) -> float:
        """Seconds to shift the first PWM/controller start."""

    @abstractmethod
    def extra_attributes(self, attrs: dict) -> dict:
        """Add attributes this role owns. Entity fields are already in attrs."""

    @abstractmethod
    async def async_added(self) -> None:
        """Entity was added to Home Assistant."""

    @abstractmethod
    async def async_removed(self) -> None:
        """Entity is being removed from Home Assistant."""

    @abstractmethod
    async def async_on_hvac_leaving(self) -> None:
        """Previous HVAC mode is shutting down."""

    @abstractmethod
    def on_hvac_entered(self) -> None:
        """New HVAC mode is active (or off)."""

    @abstractmethod
    def restore_runtime_state(self, old_state) -> None:
        """Role-specific restore after the entity restored controller state."""

    @abstractmethod
    def pwm_follows_controller(self, hvac_on) -> bool:
        """Start PWM/controller loops for this HVAC config."""

    @abstractmethod
    def on_stuck_switch_check(self) -> None:
        """Passive switch / anti-calc check at the configured time."""

    @abstractmethod
    async def async_run_stuck_prevention(self, force: bool = False) -> None:
        """Open valves briefly to prevent sticking."""

    @abstractmethod
    def should_run_controller_after_preset(self) -> bool:
        """Whether a preset change should force a controller run."""

    @abstractmethod
    def extra_preset_keys(self, hvac_on) -> list:
        """Custom preset names to advertise for the active HVAC config."""
