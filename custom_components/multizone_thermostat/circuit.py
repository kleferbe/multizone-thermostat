"""Shared types for the heating-circuit Select and room climates."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.components.climate import HVACMode


@dataclass
class CircuitPlan:
    """One PWM window: when the epoch starts and how each room is packed."""

    epoch: float
    hvac_mode: HVACMode | None
    offsets: dict[str, float] = field(default_factory=dict)
    master_pwm: float = 0.0
    master_offset: float = 0.0
    pwm_scale: float = 100.0
    pwm_duration: float = 0.0
    idle: bool = False
