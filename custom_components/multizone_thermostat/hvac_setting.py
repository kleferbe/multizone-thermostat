"""Room HVAC setting: PID, weather and on-off for one heat or cool mode."""
from __future__ import annotations

import datetime
import logging
import time

import numpy as np

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    PRESET_NONE,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, CONF_ENTITY_ID
from homeassistant.helpers.typing import ConfigType

from . import DOMAIN, pid_controller
from .const import (
    ATTR_CONTROL_MODE,
    ATTR_CONTROL_OFFSET,
    ATTR_CONTROL_OUTPUT,
    ATTR_CONTROL_PWM_OUTPUT,
    ATTR_DETAILED_OUTPUT,
    ATTR_KA,
    ATTR_KB,
    ATTR_KD,
    ATTR_KI,
    ATTR_KP,
    ATTR_LAST_SWITCH_CHANGE,
    CONF_CONTROL_REFRESH_INTERVAL,
    CONF_EXTRA_PRESETS,
    CONF_HYSTERESIS_TOLERANCE_OFF,
    CONF_HYSTERESIS_TOLERANCE_ON,
    CONF_MASTER_SCALE_BOUND,
    CONF_MIN_CYCLE_DURATION,
    CONF_ON_OFF_MODE,
    CONF_PASSIVE_SWITCH_DURATION,
    CONF_PASSIVE_SWITCH_GAP,
    CONF_PASSIVE_SWITCH_OPEN_TIME,
    CONF_PID_MODE,
    CONF_PROPORTIONAL_MODE,
    CONF_PWM_DURATION,
    CONF_PWM_RESOLUTION,
    CONF_PWM_SCALE,
    CONF_PWM_SCALE_HIGH,
    CONF_PWM_SCALE_LOW,
    CONF_PWM_THRESHOLD,
    CONF_SENSOR_OUT,
    CONF_SWITCH_MODE,
    CONF_TARGET_TEMP_INIT,
    CONF_TARGET_TEMP_MAX,
    CONF_TARGET_TEMP_MIN,
    CONF_WC_MODE,
    CONF_WINDOW_OPEN_TEMPDROP,
    DEFAULT_PWM_RESOLUTION,
    PRESET_EMERGENCY,
    PRESET_RESTORE,
    PRESET_STANDBY,
)


class HVACSetting:
    """Controller and config for one HVAC mode of a room."""

    def __init__(
        self,
        name: str,
        hvac_mode: HVACMode,
        conf: ConfigType,
        area: float,
        detailed_output: bool,
    ) -> None:
        self._name = name + "." + hvac_mode
        self._logger = logging.getLogger(DOMAIN).getChild(self._name)
        self._logger.debug("Init config for hvac_mode: '%s'", hvac_mode)

        self._hvac_mode = hvac_mode
        self._preset_mode = PRESET_NONE
        self._old_preset = None
        self._hvac_settings = conf
        self._switch_entity = conf[CONF_ENTITY_ID]
        self.area = area
        self.detailed_output = detailed_output
        self._store_integral = False

        self._last_change = datetime.datetime.now(datetime.UTC)
        self._control_output = {
            ATTR_CONTROL_OFFSET: 0,
            ATTR_CONTROL_PWM_OUTPUT: 0,
        }
        self._time_offset = 0.0

        self._target_temp = None
        self._current_state = None
        self._current_temperature = None
        self._outdoor_temperature = None
        self.restore_temperature = None
        self._pwm_threshold = None

        self._on_off = conf.get(CONF_ON_OFF_MODE)
        self._proportional = conf.get(CONF_PROPORTIONAL_MODE)
        self._pid = None
        self._wc = None
        if self._proportional:
            self._wc = self._proportional.get(CONF_WC_MODE)
            self._pid = self._proportional.get(CONF_PID_MODE)

        self.init_mode()

    def init_mode(self) -> None:
        """Init the defined control modes."""
        if self.is_on_off:
            self._logger.debug("Setup control mode 'on_off'")
            self._on_off[ATTR_CONTROL_PWM_OUTPUT] = 0
        if self.is_proportional:
            self._logger.debug("Setup control mode 'proportional'")
            self._pwm_threshold = self._proportional[CONF_PWM_THRESHOLD]
            if self.is_pid:
                self.start_pid()
                self._pid[ATTR_CONTROL_PWM_OUTPUT] = 0
            if self.is_weather:
                self._logger.debug("Init 'weather control' settings")
                self._wc[ATTR_CONTROL_PWM_OUTPUT] = 0

    def calculate(
        self, routine: bool = False, force: bool = False, current_offset: float = 0
    ) -> None:
        """Calculate control values for the active modes."""
        if self.is_on_off:
            self.run_on_off()
            return
        if not self.is_proportional:
            return
        if self.is_weather:
            self.run_wc()
        if self.is_pid:
            self.run_pid(force)
            if self._wc and self._pid:
                pid = self._pid_cntrl.get_PID_parts
                if (
                    pid["p"] < 0
                    and pid["i"] < -self._wc[ATTR_CONTROL_PWM_OUTPUT]
                    and self._wc[ATTR_CONTROL_PWM_OUTPUT]
                    + self._pid[ATTR_CONTROL_PWM_OUTPUT]
                    < 0
                ):
                    self.integral = -self._wc[ATTR_CONTROL_PWM_OUTPUT]

    def start_pid(self) -> None:
        """Init the PID controller."""
        self._logger.debug("Init pid settings")
        lower_pwm_scale, upper_pwm_scale = self.pwm_scale_limits(self._pid)
        kp, ki, kd = self.pid_param(self._pid)
        self._pid_cntrl = pid_controller.PIDController(
            self._name,
            CONF_PID_MODE,
            self.control_interval.seconds,
            kp,
            ki,
            kd,
            time.time,
            lower_pwm_scale,
            upper_pwm_scale,
        )
        self._pid[ATTR_CONTROL_PWM_OUTPUT] = 0

    def run_on_off(self) -> None:
        """Determine switch state for hvac on_off."""
        tolerance_on, tolerance_off = self.hysteresis
        target_temp = self.target_temperature
        current_temp = self.current_temperature
        self._logger.debug(
            "on-off - target %s, on %s, off %s, current %.2f",
            target_temp,
            tolerance_on,
            tolerance_off,
            current_temp,
        )
        heat_positive = self._hvac_mode == HVACMode.HEAT
        tol_high = tolerance_off if heat_positive else tolerance_on
        tol_low = tolerance_on if heat_positive else tolerance_off
        too_warm = current_temp >= target_temp + tol_high
        too_cold = current_temp <= target_temp - tol_low
        if too_warm == too_cold:
            return
        self._on_off[ATTR_CONTROL_PWM_OUTPUT] = 0 if too_warm else 100

    def run_wc(self) -> None:
        """Calculate weather compensation."""
        ka, kb = self.ka_kb
        lower_pwm_scale, upper_pwm_scale = self.pwm_scale_limits(self._wc)
        if self.outdoor_temperature is None:
            self._logger.warning("no outdoor temperature; continue with previous data")
            return
        temp_diff = self.target_temperature - self.outdoor_temperature
        self._wc[ATTR_CONTROL_PWM_OUTPUT] = min(
            max(lower_pwm_scale, temp_diff * ka + kb), upper_pwm_scale
        )
        self._logger.debug(
            "weather control contribution %.2f", self._wc[ATTR_CONTROL_PWM_OUTPUT]
        )

    def run_pid(self, force: bool = False) -> None:
        """Calculate the PID for the current timestep."""
        if isinstance(self.current_state, (list, tuple, np.ndarray)):
            current = self.current_state
            if self.check_window_open(current[1]):
                return
        else:
            current = self.current_temperature
        self._pid[ATTR_CONTROL_PWM_OUTPUT] = self._pid_cntrl.calc(
            current, self.target_temperature, force=force
        )

    def calc_control_output(self) -> None:
        """Combine sub-controllers into offset and pwm_out."""
        if self.is_on_off:
            control_output = self._on_off[ATTR_CONTROL_PWM_OUTPUT]
        else:
            control_output = 0
            if self.is_pid:
                control_output += self._pid[ATTR_CONTROL_PWM_OUTPUT]
            if self.is_weather:
                control_output += self._wc[ATTR_CONTROL_PWM_OUTPUT]
            if control_output > self.pwm_scale:
                control_output = self.pwm_scale
            elif control_output < self.pwm_threshold:
                control_output = 0
            control_output = get_rounded(
                control_output, self.pwm_scale / self.pwm_resolution
            )

        self._control_output = {
            ATTR_CONTROL_OFFSET: round(self._time_offset, 3),
            ATTR_CONTROL_PWM_OUTPUT: round(control_output, 3),
        }

    @property
    def control_output(self) -> dict:
        """Offset and valve position for this mode."""
        return self._control_output

    def reset_control_output(self) -> None:
        """Clear PWM output so standby does not keep a heat or cool request."""
        self._time_offset = 0.0
        self._control_output = {
            ATTR_CONTROL_OFFSET: 0,
            ATTR_CONTROL_PWM_OUTPUT: 0,
        }

    @property
    def time_offset(self) -> float:
        """PWM phase offset (room demand, not valve times)."""
        return self._time_offset

    @time_offset.setter
    def time_offset(self, offset: float) -> None:
        if self.is_proportional:
            self._time_offset = offset

    @property
    def min_target_temp(self) -> float:
        return self._hvac_settings[CONF_TARGET_TEMP_MIN]

    @property
    def max_target_temp(self) -> float:
        return self._hvac_settings[CONF_TARGET_TEMP_MAX]

    @property
    def target_temp_limits(self) -> list:
        return [self.min_target_temp, self.max_target_temp]

    @property
    def target_temperature(self) -> float:
        if self._target_temp is None:
            self._target_temp = self._hvac_settings[CONF_TARGET_TEMP_INIT]
        return self._target_temp

    @target_temperature.setter
    def target_temperature(self, target_temp: float) -> None:
        self._target_temp = target_temp

    @property
    def preset_temp(self) -> float | None:
        if self.preset_mode in self.custom_presets:
            return self.custom_presets[self.preset_mode]
        return None

    @property
    def custom_presets(self) -> dict:
        return self._hvac_settings[CONF_EXTRA_PRESETS]

    @property
    def preset_mode(self) -> str:
        return self._preset_mode

    @preset_mode.setter
    def preset_mode(self, mode: str) -> None:
        if mode == PRESET_EMERGENCY:
            self._old_preset = self.preset_mode
        elif mode == PRESET_RESTORE:
            mode = self._old_preset

        if mode not in [PRESET_EMERGENCY, PRESET_RESTORE, PRESET_STANDBY]:
            if self._preset_mode == PRESET_NONE and mode in self.custom_presets:
                self.restore_temperature = self.target_temperature
                self.target_temperature = self.custom_presets[mode]
            elif self._preset_mode in self.custom_presets and mode == PRESET_NONE:
                if self.restore_temperature is not None:
                    self.target_temperature = self.restore_temperature
                else:
                    self.target_temperature = self._hvac_settings[CONF_TARGET_TEMP_INIT]
            elif (
                self._preset_mode in self.custom_presets and mode in self.custom_presets
            ):
                self.target_temperature = self.custom_presets[mode]
        self._preset_mode = mode

    @property
    def switch_entity(self) -> str:
        return self._switch_entity

    @property
    def is_switch_on_off(self) -> bool:
        """True when the actuator is a binary switch (on-off or PWM window)."""
        return self.is_on_off or self.pwm_duration.total_seconds() != 0

    @property
    def master_scaled_bound(self) -> float:
        if self.is_proportional:
            return self._proportional[CONF_MASTER_SCALE_BOUND]
        return 1

    @property
    def switch_mode(self) -> str:
        return self._hvac_settings[CONF_SWITCH_MODE]

    @property
    def anti_calc_idle(self):
        return self._hvac_settings.get(CONF_PASSIVE_SWITCH_DURATION)

    @property
    def anti_calc_open(self) -> datetime.timedelta:
        return self._hvac_settings[CONF_PASSIVE_SWITCH_OPEN_TIME]

    @property
    def anti_calc_gap(self) -> datetime.timedelta:
        return self._hvac_settings[CONF_PASSIVE_SWITCH_GAP]

    @property
    def switch_last_change(self) -> datetime.datetime:
        return self._last_change

    @switch_last_change.setter
    def switch_last_change(self, val: datetime.datetime) -> None:
        self._last_change = val

    @property
    def pwm_duration(self) -> datetime.timedelta:
        if self.is_proportional:
            return self._proportional[CONF_PWM_DURATION]
        return datetime.timedelta(0)

    @property
    def pwm_resolution(self) -> float:
        if self.is_proportional:
            return self._proportional[CONF_PWM_RESOLUTION]
        return DEFAULT_PWM_RESOLUTION

    @property
    def pwm_scale(self) -> float:
        if self.is_proportional:
            return self._proportional[CONF_PWM_SCALE]
        return 100.0

    def pwm_scale_limits(self, hvac_data: dict) -> list:
        """Bandwidth for control value."""
        upper_pwm_scale = hvac_data.get(CONF_PWM_SCALE_HIGH, self.pwm_scale)
        if CONF_PWM_SCALE_LOW in hvac_data:
            lower_pwm_scale = hvac_data[CONF_PWM_SCALE_LOW]
        elif self.is_pid and self.is_weather:
            lower_pwm_scale = -1 * upper_pwm_scale
        else:
            lower_pwm_scale = 0
        return [lower_pwm_scale, upper_pwm_scale]

    @property
    def pwm_threshold(self) -> float:
        return self._pwm_threshold if self._pwm_threshold is not None else 0

    def set_pwm_threshold(self, new_threshold: float) -> None:
        if self.is_on_off:
            raise ValueError("min diff cannot be set for on-off controller")
        self._pwm_threshold = new_threshold

    @property
    def control_interval(self) -> datetime.timedelta:
        if self.is_on_off:
            return self._on_off.get(
                CONF_CONTROL_REFRESH_INTERVAL, datetime.timedelta(0)
            )
        return self._proportional[CONF_CONTROL_REFRESH_INTERVAL]

    @property
    def min_on_off_cycle(self) -> datetime.timedelta | None:
        if self.is_on_off:
            return self._on_off.get(CONF_MIN_CYCLE_DURATION)
        return None

    @property
    def hysteresis(self) -> list:
        return [
            self._on_off[CONF_HYSTERESIS_TOLERANCE_ON],
            self._on_off[CONF_HYSTERESIS_TOLERANCE_OFF],
        ]

    @property
    def current_state(self) -> list | None:
        return self._current_state

    @current_state.setter
    def current_state(self, state: list) -> None:
        self._current_state = state
        if self._current_state:
            self.current_temperature = state[0]

    @property
    def detailed_output(self) -> bool:
        return self._detailed_output

    @detailed_output.setter
    def detailed_output(self, new_mode: bool) -> None:
        self._detailed_output = new_mode

    @property
    def current_temperature(self) -> float | None:
        return self._current_temperature

    @current_temperature.setter
    def current_temperature(self, current_temp: float | None) -> None:
        self._current_temperature = current_temp

    @property
    def outdoor_temperature(self) -> float | None:
        return self._outdoor_temperature

    @outdoor_temperature.setter
    def outdoor_temperature(self, current_temp: float | None) -> None:
        self._outdoor_temperature = current_temp

    def check_window_open(self, current: float) -> bool:
        """True when the temperature slope looks like an open window."""
        if not self._pid or CONF_WINDOW_OPEN_TEMPDROP not in self._pid:
            return False
        window_threshold = self._pid[CONF_WINDOW_OPEN_TEMPDROP] / 3600
        sign = 1.0 if self._hvac_mode == HVACMode.HEAT else -1.0
        tripped = sign * current < sign * window_threshold
        if tripped:
            self._logger.debug(
                "open window detected (slope %.5f), keep control value", current
            )
        return tripped

    def pid_param(self, hvac_data: dict) -> tuple:
        return (hvac_data.get(ATTR_KP), hvac_data.get(ATTR_KI), hvac_data.get(ATTR_KD))

    def set_pid_param(
        self,
        kp: float | None = None,
        ki: float | None = None,
        kd: float | None = None,
        update: bool = False,
    ) -> None:
        if kp is not None:
            self._pid[ATTR_KP] = kp
        if ki is not None:
            self._pid[ATTR_KI] = ki
        if kd is not None:
            self._pid[ATTR_KD] = kd
        if update:
            self._pid_cntrl.set_pid_param(kp=kp, ki=ki, kd=kd)

    def pid_reset_time(self) -> None:
        """Reset PID time so the integral does not overflow across HVAC modes."""
        self._pid_cntrl.reset_time()

    @property
    def integral(self) -> float:
        return self._pid_cntrl.integral

    @integral.setter
    def integral(self, value: float) -> None:
        self._pid_cntrl.integral = value

    @property
    def velocity(self) -> float:
        return self._pid_cntrl.differential

    @property
    def ka_kb(self) -> tuple:
        if self.is_weather:
            return (self._wc[ATTR_KA], self._wc[ATTR_KB])
        return (None, None)

    @property
    def wc_sensor(self) -> str | None:
        if self.is_weather:
            return self._wc[CONF_SENSOR_OUT]
        return None

    def set_ka_kb(self, ka: float | None = None, kb: float | None = None) -> None:
        if ka is not None:
            self._wc[ATTR_KA] = ka
        if kb is not None:
            self._wc[ATTR_KB] = kb

    @property
    def control_mode(self) -> str:
        if self.is_on_off:
            return CONF_ON_OFF_MODE
        return CONF_PROPORTIONAL_MODE

    @property
    def is_on_off(self) -> bool:
        return bool(self._on_off)

    @property
    def is_proportional(self) -> bool:
        return bool(self._proportional)

    @property
    def is_pid(self) -> bool:
        return bool(self._pid)

    @property
    def is_weather(self) -> bool:
        return bool(self._wc)

    @property
    def extra_attrs(self) -> ConfigType:
        """Attributes for the climate entity."""
        open_window = None
        if (
            isinstance(self.current_state, (list, tuple, np.ndarray))
            and self.is_proportional
        ):
            open_window = self.check_window_open(self.current_state[1])
        tmp_dict = {
            ATTR_PRESET_MODE: self.preset_mode,
            ATTR_TEMPERATURE: self.target_temperature,
            ATTR_CONTROL_MODE: self.control_mode,
            CONF_CONTROL_REFRESH_INTERVAL: self.control_interval.seconds,
            CONF_PWM_DURATION: self.pwm_duration.seconds,
            CONF_PWM_SCALE: self.pwm_scale,
            ATTR_CONTROL_OUTPUT: self.control_output,
            ATTR_DETAILED_OUTPUT: self.detailed_output,
            ATTR_LAST_SWITCH_CHANGE: self.switch_last_change,
            "Open_window": open_window,
        }
        if self.is_proportional and self.is_pid:
            tmp_dict["PID_values"] = self.pid_param(self._pid)
            pid_parts = self._pid_cntrl.get_PID_parts
            if self.detailed_output:
                tmp_dict["PID_P"] = round(pid_parts["p"], 3)
                tmp_dict["PID_I"] = round(pid_parts["i"], 3)
                tmp_dict["PID_D"] = round(pid_parts["d"], 3)
                tmp_dict["PID_valve_pos"] = round(
                    self._pid[ATTR_CONTROL_PWM_OUTPUT], 3
                )
            elif self._store_integral:
                tmp_dict["PID_P"] = None
                tmp_dict["PID_I"] = round(pid_parts["i"], 3)
                tmp_dict["PID_D"] = None
                tmp_dict["PID_valve_pos"] = None
        if self.is_proportional and self.is_weather:
            tmp_dict["ab_values"] = self.ka_kb
            tmp_dict["wc_valve_pos"] = (
                round(self._wc[ATTR_CONTROL_PWM_OUTPUT], 3)
                if self.detailed_output
                else None
            )
        return tmp_dict

    def restore_reboot(
        self, data: ConfigType, restore_parameters: bool, restore_integral: bool
    ) -> None:
        """Restore attributes after restart."""
        self._store_integral = restore_integral
        self.target_temperature = data[ATTR_TEMPERATURE]
        self.switch_last_change = datetime.datetime.strptime(
            data[ATTR_LAST_SWITCH_CHANGE], "%Y-%m-%dT%H:%M:%S.%f%z"
        )
        if self.is_pid:
            if restore_parameters and "PID_values" in data:
                kp, ki, kd = data["PID_values"]
                self.set_pid_param(kp=kp, ki=ki, kd=kd, update=True)
            if restore_integral:
                restored_i = data.get("PID_I", data.get("PID_integral"))
                if restored_i is not None:
                    self.integral = restored_i
            self.pid_reset_time()


def get_rounded(input_val: float, min_clip: float) -> float:
    """Round float to min_clip."""
    scaled = input_val / min_clip
    return np.where(scaled % 1 >= 0.5, np.ceil(scaled), np.floor(scaled)) * min_clip
