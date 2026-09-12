"""Circuit PWM window: demand in, concrete open/close times out."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.components.climate import HVACMode

from .const import (
    ATTR_CONTROL_PWM_OUTPUT,
    CONF_AREA,
    CONF_PWM_DURATION,
    CONF_PWM_SCALE,
    NestingMode,
)
from .pwm_nesting import Nesting


@dataclass
class RoomDemand:
    """One room's heat/cool request for this PWM window."""

    entity_id: str
    area: float
    pwm: float
    pwm_scale: float
    pwm_duration: float
    master_scaled_bound: float = 1.0


@dataclass
class ValveSlot:
    """When a valve opens/closes, or the proportional position to hold."""

    entity_id: str
    open_at: float | None = None
    close_at: float | None = None
    position: float | None = None

    @property
    def is_closed(self) -> bool:
        return self.open_at is None and self.position is None


@dataclass
class CircuitPlan:
    """One PWM window as executable valve slots."""

    epoch: float
    duration: float
    hvac_mode: HVACMode | None
    idle: bool
    plant: ValveSlot
    rooms: list[ValveSlot] = field(default_factory=list)

    def slot_for(self, entity_id: str) -> ValveSlot | None:
        """Plant or room slot, or None when this entity is not in the plan."""
        if self.plant.entity_id == entity_id:
            return self.plant
        for slot in self.rooms:
            if slot.entity_id == entity_id:
                return slot
        return None

    @classmethod
    def idle(
        cls,
        epoch: float,
        duration: float,
        hvac_mode: HVACMode | None,
        plant_entity_id: str = "",
    ) -> CircuitPlan:
        """Window with everything closed."""
        return cls(
            epoch=epoch,
            duration=duration,
            hvac_mode=hvac_mode,
            idle=True,
            plant=ValveSlot(entity_id=plant_entity_id),
            rooms=[],
        )


class CircuitPlanBuilder:
    """Circuit YAML in create(); per-window demands in build()."""

    def __init__(
        self,
        name: str,
        duration: float,
        operation_mode: NestingMode,
        pwm_scale: float,
        pwm_threshold: float,
        pwm_resolution: float,
        min_load: float,
        min_valve: float,
        valve_lag: float,
        plant_entity_id: str,
    ) -> None:
        self._name = name
        self._duration = duration
        self._operation_mode = operation_mode
        self._pwm_scale = pwm_scale
        self._pwm_threshold = pwm_threshold
        self._pwm_resolution = pwm_resolution
        self._min_load = min_load
        self._min_valve = min_valve
        self._valve_lag = valve_lag
        self._plant_entity_id = plant_entity_id

    @classmethod
    def create(
        cls,
        *,
        duration: float,
        operation_mode: NestingMode,
        pwm_scale: float,
        pwm_threshold: float,
        pwm_resolution: float,
        min_load: float,
        min_valve: float,
        valve_lag: float,
        plant_entity_id: str,
        name: str = "circuit",
    ) -> CircuitPlanBuilder:
        return cls(
            name=name,
            duration=duration,
            operation_mode=operation_mode,
            pwm_scale=pwm_scale,
            pwm_threshold=pwm_threshold,
            pwm_resolution=pwm_resolution,
            min_load=min_load,
            min_valve=min_valve,
            valve_lag=valve_lag,
            plant_entity_id=plant_entity_id,
        )

    def build(
        self,
        epoch: float,
        hvac_mode: HVACMode | None,
        demands: list[RoomDemand],
        tot_area: float,
    ) -> CircuitPlan:
        """Pack this window and return unix open/close times."""
        if self._duration <= 0 or not demands:
            return CircuitPlan.idle(
                epoch, self._duration, hvac_mode, self._plant_entity_id
            )

        nesting = Nesting(
            self._name,
            operation_mode=self._operation_mode,
            master_pwm=self._pwm_scale,
            tot_area=max(tot_area, 1.0),
            min_load=self._min_load,
            pwm_threshold=self._pwm_threshold,
            min_prop_valve_opening=self._min_valve,
        )
        sat_data = {
            demand.entity_id: {
                CONF_AREA: demand.area,
                CONF_PWM_SCALE: demand.pwm_scale,
                CONF_PWM_DURATION: demand.pwm_duration,
                ATTR_CONTROL_PWM_OUTPUT: demand.pwm,
            }
            for demand in demands
        }
        nesting.nest_rooms(sat_data)
        nesting.distribute_nesting()
        occupancy = nesting.window_occupancy()

        step = self._duration / self._pwm_resolution if self._pwm_resolution else 1
        plant_on = _round_to_step(occupancy.plant_duration * self._duration, step)
        if plant_on <= 0:
            plant = ValveSlot(entity_id=self._plant_entity_id)
            plant_duty = 0.0
        else:
            open_at = epoch + occupancy.plant_start * self._duration + self._valve_lag
            plant = ValveSlot(
                entity_id=self._plant_entity_id,
                open_at=open_at,
                close_at=open_at + plant_on,
            )
            plant_duty = plant_on / self._duration

        demand_by_id = {demand.entity_id: demand for demand in demands}
        rooms: list[ValveSlot] = []
        for entity_id, (start, duration_frac) in occupancy.rooms.items():
            on = duration_frac * self._duration
            if on <= 0:
                rooms.append(ValveSlot(entity_id=entity_id))
                continue
            open_at = epoch + start * self._duration
            rooms.append(
                ValveSlot(
                    entity_id=entity_id,
                    open_at=open_at,
                    close_at=open_at + on,
                )
            )

        for entity_id in occupancy.prop_ids:
            demand = demand_by_id.get(entity_id)
            if demand is None:
                continue
            bound = demand.master_scaled_bound or 1.0
            master_util = 1.0
            if bound > 1:
                master_util = max(1 / bound, plant_duty)
            position = round(
                max(0, min(demand.pwm / master_util, demand.pwm_scale)),
                0,
            )
            rooms.append(ValveSlot(entity_id=entity_id, position=position))

        return CircuitPlan(
            epoch=epoch,
            duration=self._duration,
            hvac_mode=hvac_mode,
            idle=False,
            plant=plant,
            rooms=rooms,
        )


def _round_to_step(value: float, step: float) -> float:
    """Round value to the nearest multiple of step."""
    if step <= 0:
        return value
    scaled = value / step
    if scaled % 1 >= 0.5:
        rounded = int(scaled) + 1
    else:
        rounded = int(scaled)
    return float(rounded * step)
