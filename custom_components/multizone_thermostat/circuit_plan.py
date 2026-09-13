"""Circuit PWM window: demand in, concrete open/close times out."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field, replace

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
    """When a valve opens/closes, or the proportional position to hold.

    On/off times:
    - both None: closed this window
    - open_at set, close_at None: open from open_at, no close in this plan
    - open_at None, close_at set: open from window start, close at close_at
    - both set: pulse (builder guarantees close_at > open_at)

    Prop: position set, both times None.
    """

    entity_id: str
    open_at: float | None = None
    close_at: float | None = None
    position: float | None = None

    @property
    def is_closed(self) -> bool:
        return (
            self.open_at is None
            and self.close_at is None
            and self.position is None
        )

    def as_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "open_at": self.open_at,
            "close_at": self.close_at,
            "position": self.position,
        }

    def is_open_at(self, now: float) -> bool:
        """Whether this on/off slot should be open at unix time now."""
        if self.is_closed or self.position is not None:
            return False
        started = self.open_at is None or self.open_at <= now
        not_done = self.close_at is None or self.close_at > now
        return started and not_done


@dataclass
class CircuitPlan:
    """One PWM window as executable valve slots."""

    epoch: float
    duration: float
    hvac_mode: HVACMode | None
    idle: bool
    plant: ValveSlot
    rooms: list[ValveSlot] = field(default_factory=list)
    stuck_loop: bool = False
    pid_ticks: bool = False

    @property
    def window_end(self) -> float:
        return self.epoch + self.duration

    def slot_for(self, entity_id: str) -> ValveSlot | None:
        """Plant or room slot, or None when this entity is not in the plan."""
        if self.plant.entity_id == entity_id:
            return self.plant
        for slot in self.rooms:
            if slot.entity_id == entity_id:
                return slot
        return None

    def for_entity(self, entity_id: str) -> CircuitPlan:
        """Copy this window with only the named valve."""
        slot = self.slot_for(entity_id) or ValveSlot(entity_id=entity_id)
        if entity_id == self.plant.entity_id:
            return replace(self, plant=slot, rooms=[])
        return replace(self, plant=ValveSlot(entity_id=""), rooms=[slot])

    def as_dict(self) -> dict:
        return {
            "epoch": self.epoch,
            "duration": self.duration,
            "window_end": self.window_end,
            "stuck_loop": self.stuck_loop,
            "idle": self.idle,
            "pid_ticks": self.pid_ticks,
            "hvac_mode": str(self.hvac_mode) if self.hvac_mode else None,
            "plant": self.plant.as_dict(),
            "rooms": [slot.as_dict() for slot in self.rooms],
        }

    @classmethod
    def idle_plan(
        cls,
        epoch: float,
        duration: float,
        hvac_mode: HVACMode | None,
        plant_entity_id: str = "",
    ) -> CircuitPlan:
        """Mode idle: everything closed, no PID ticks."""
        return cls(
            epoch=epoch,
            duration=duration,
            hvac_mode=hvac_mode,
            idle=True,
            stuck_loop=False,
            pid_ticks=False,
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

    def idle(
        self,
        epoch: float,
        hvac_mode: HVACMode | None,
    ) -> CircuitPlan:
        return CircuitPlan.idle_plan(
            epoch, self._duration, hvac_mode, self._plant_entity_id
        )

    def build(
        self,
        epoch: float,
        hvac_mode: HVACMode | None,
        demands: list[RoomDemand],
        tot_area: float,
    ) -> CircuitPlan:
        """Pack this heating window. Empty demand is closed slots, not mode-idle."""
        duration = self._duration
        if not demands:
            return CircuitPlan(
                epoch=epoch,
                duration=duration,
                hvac_mode=hvac_mode,
                idle=False,
                stuck_loop=False,
                pid_ticks=True,
                plant=ValveSlot(entity_id=self._plant_entity_id),
                rooms=[],
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

        step = duration / self._pwm_resolution
        plant_on = _round_to_step(occupancy.plant_duration * duration, step)
        plant = self._timed_slot(
            self._plant_entity_id,
            epoch + occupancy.plant_start * duration + self._valve_lag,
            plant_on,
            duration,
            pwm_scale=self._pwm_scale,
            pwm_threshold=self._pwm_threshold,
        )
        plant_duty = 0.0
        if not plant.is_closed:
            close = plant.close_at if plant.close_at is not None else epoch + duration
            open_at = plant.open_at if plant.open_at is not None else epoch
            plant_duty = max(0.0, (close - open_at) / duration)

        demand_by_id = {demand.entity_id: demand for demand in demands}
        rooms: list[ValveSlot] = []
        for entity_id, (start, duration_frac) in occupancy.rooms.items():
            on = duration_frac * duration
            rooms.append(
                self._timed_slot(
                    entity_id,
                    epoch + start * duration,
                    on,
                    duration,
                    pwm_scale=self._pwm_scale,
                    pwm_threshold=self._pwm_threshold,
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
            if position <= 0:
                rooms.append(ValveSlot(entity_id=entity_id))
            else:
                rooms.append(ValveSlot(entity_id=entity_id, position=position))

        return CircuitPlan(
            epoch=epoch,
            duration=duration,
            hvac_mode=hvac_mode,
            idle=False,
            stuck_loop=False,
            pid_ticks=True,
            plant=plant,
            rooms=rooms,
        )

    def build_stuck_loop(
        self,
        epoch: float,
        hvac_mode: HVACMode | None,
        room_ids: list[str],
        open_s: float,
        gap_s: float,
    ) -> CircuitPlan:
        """Sequential flush slots. Duration follows the last close."""
        rooms: list[ValveSlot] = []
        t = epoch
        last_close = epoch
        for i, entity_id in enumerate(room_ids):
            if i:
                t += gap_s
            open_at = t
            close_at = open_at + open_s
            if close_at > open_at:
                rooms.append(
                    ValveSlot(
                        entity_id=entity_id, open_at=open_at, close_at=close_at
                    )
                )
            else:
                rooms.append(ValveSlot(entity_id=entity_id))
            last_close = max(last_close, close_at)
            t = close_at
        duration = max(last_close - epoch, 0.0)
        if rooms and not rooms[0].is_closed:
            plant_open = epoch + self._valve_lag
            plant = ValveSlot(
                entity_id=self._plant_entity_id,
                open_at=plant_open,
                close_at=last_close if last_close > plant_open else None,
            )
            if plant.close_at is not None and plant.close_at <= plant.open_at:
                plant = ValveSlot(
                    entity_id=self._plant_entity_id,
                    open_at=plant_open,
                    close_at=None,
                )
        else:
            plant = ValveSlot(entity_id=self._plant_entity_id)
        return CircuitPlan(
            epoch=epoch,
            duration=duration,
            hvac_mode=hvac_mode,
            idle=False,
            stuck_loop=True,
            pid_ticks=False,
            plant=plant,
            rooms=rooms,
        )

    @staticmethod
    def stuck_local(
        epoch: float,
        hvac_mode: HVACMode | None,
        entity_id: str,
        open_s: float,
    ) -> CircuitPlan:
        """Single-valve flush window."""
        close_at = epoch + open_s if open_s > 0 else None
        if close_at is not None and close_at <= epoch:
            slot = ValveSlot(entity_id=entity_id)
            duration = 0.0
        elif close_at is None:
            slot = ValveSlot(entity_id=entity_id, open_at=epoch, close_at=None)
            duration = 0.0
        else:
            slot = ValveSlot(entity_id=entity_id, open_at=epoch, close_at=close_at)
            duration = open_s
        return CircuitPlan(
            epoch=epoch,
            duration=duration,
            hvac_mode=hvac_mode,
            idle=False,
            stuck_loop=True,
            pid_ticks=False,
            plant=ValveSlot(entity_id=""),
            rooms=[slot],
        )

    @staticmethod
    def build_local(
        epoch: float,
        hvac_mode: HVACMode | None,
        entity_id: str,
        pwm: float,
        pwm_scale: float,
        pwm_duration: float,
        pwm_threshold: float,
        *,
        on_off: bool = False,
        operate_cycle: float = 0.0,
    ) -> CircuitPlan:
        """One-room heating plan (uncoordinated / standalone)."""
        if on_off:
            duration = operate_cycle if operate_cycle > 0 else 0.0
            if pwm > 0:
                slot = ValveSlot(entity_id=entity_id, open_at=epoch, close_at=None)
            else:
                slot = ValveSlot(entity_id=entity_id)
        elif pwm_duration > 0:
            duration = pwm_duration
            scale = pwm_scale or 100.0
            on = min(max(pwm, 0.0), scale) / scale * duration
            slot = CircuitPlanBuilder._timed_slot(
                entity_id,
                epoch,
                on,
                duration,
                pwm_scale=pwm_scale,
                pwm_threshold=pwm_threshold,
            )
        else:
            duration = operate_cycle if operate_cycle > 0 else 0.0
            if pwm <= 0:
                slot = ValveSlot(entity_id=entity_id)
            else:
                slot = ValveSlot(entity_id=entity_id, position=pwm)

        return CircuitPlan(
            epoch=epoch,
            duration=duration,
            hvac_mode=hvac_mode,
            idle=False,
            stuck_loop=False,
            pid_ticks=True,
            plant=ValveSlot(entity_id=""),
            rooms=[slot],
        )

    @staticmethod
    def _timed_slot(
        entity_id: str,
        open_at: float,
        on: float,
        window: float,
        *,
        pwm_scale: float,
        pwm_threshold: float,
    ) -> ValveSlot:
        """On/off slot from a duration, applying min-on and full-window None."""
        min_on = 0.0
        if window > 0 and pwm_scale:
            min_on = pwm_threshold / pwm_scale * window
        if on <= 0 or on < min_on:
            return ValveSlot(entity_id=entity_id)
        if window > 0 and on >= window - 1e-6:
            return ValveSlot(entity_id=entity_id, open_at=open_at, close_at=None)
        close_at = open_at + on
        if close_at <= open_at:
            return ValveSlot(entity_id=entity_id)
        return ValveSlot(entity_id=entity_id, open_at=open_at, close_at=close_at)


def check_time_in_window(
    epoch: float, duration: float, check_time: datetime.time | datetime.datetime | None
) -> bool:
    """True when today's (or next day's) check clock falls in [epoch, epoch+duration)."""
    if check_time is None or duration <= 0:
        return False
    start = datetime.datetime.fromtimestamp(epoch)
    end = datetime.datetime.fromtimestamp(epoch + duration)
    check = start.replace(
        hour=check_time.hour,
        minute=check_time.minute,
        second=getattr(check_time, "second", 0) or 0,
        microsecond=0,
    )
    if check < start:
        check += datetime.timedelta(days=1)
    return start <= check < end


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
