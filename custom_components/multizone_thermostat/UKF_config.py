"""UKF for room temperature: state [T °C, velocity °C/s]."""
import time

import numpy as np

from .UKF_filter.discretization import Q_discrete_white_noise
from .UKF_filter.sigma_points import MerweScaledSigmaPoints
from .UKF_filter.UKF import UnscentedKalmanFilter

# Typical room-sensor step (datasheet or HA history). filter_mode stretches R.
# Override per climate via filter_resolution.
# Prior velocity ~ 1 °C/h — FBH magnitude, not 0.2 (°C/s)².
_VEL_PRIOR = 1.0 / 3600.0
# Do not coast a stale velocity across a long outage.
_MAX_DT = 3600.0
# Clip for D-term / window-open (allows ~10 °C/h drops).
_MAX_ABS_VEL = 10.0 / 3600.0
# White-noise acceleration (°C/s²): ~1 °C/h change over 15 min at mode 1.
_ACCEL_SIGMA = 3.0e-7
# Velocity mean-reverts so a Zigbee report-on-change plateau does not coast forever.
_VEL_TAU = 3600.0


class UKFFilter:
    """initiate the UKF filter for thermostat"""

    def __init__(self, current_temp, timedelta, filter_mode, resolution):
        """init Unscented kalman filter"""
        self._last_update = time.time()
        self._mode = filter_mode
        self._resolution = float(resolution)
        self._interval = max(float(timedelta or 1.0), 1.0)
        sigmas = MerweScaledSigmaPoints(n=2, alpha=0.001, beta=2, kappa=0)
        self._kf_temp = UnscentedKalmanFilter(
            dim_x=2, dim_z=1, dt=self._interval, hx=hx, fx=fx, points=sigmas
        )
        self._kf_temp.x = np.array([float(current_temp), 0.0])
        self._kf_temp.P = np.diag([self._resolution**2, _VEL_PRIOR**2])
        self.set_Q_R(self._interval)

    def kf_predict(self):
        """Predict with the actual elapsed time and matching process noise."""
        now = time.time()
        dt = now - self._last_update
        if dt < 0.05:
            return
        if dt > _MAX_DT:
            self._kf_temp.x[1] = 0.0
            dt = _MAX_DT
        self._last_update = now
        self.set_Q_R(dt)
        self._kf_temp.predict(dt=dt)
        vel = float(self._kf_temp.x[1])
        if abs(vel) > _MAX_ABS_VEL:
            self._kf_temp.x[1] = np.copysign(_MAX_ABS_VEL, vel)

    def kf_update(self, current_temp):
        """run UKF update"""
        self._kf_temp.update(float(current_temp))

    @property
    def get_temp(self):
        """return filtered temperature"""
        return float(self._kf_temp.x[0])

    @property
    def get_vel(self):
        """return filtered velocity"""
        return float(self._kf_temp.x[1])

    def set_Q_R(self, timedelta=None):  # pylint: disable=invalid-name
        """Q for this dt; R is sensor noise × filter_mode (not dt)."""
        dt = float(timedelta if timedelta is not None else self._interval)
        dt = max(dt, 1.0)
        var = (_ACCEL_SIGMA / max(self.filter_mode, 1)) ** 2
        self._kf_temp.Q = Q_discrete_white_noise(dim=2, dt=dt, var=var)
        sigma_z = self._resolution * max(self.filter_mode, 1)
        self._kf_temp.R = np.diag([sigma_z**2])

    @property
    def interval(self):
        """return time step"""
        return self._interval

    @interval.setter
    def interval(self, timedelta):  # pylint: disable=invalid-name
        """set time step"""
        if timedelta != self._interval:
            self._interval = timedelta
            self.set_Q_R()

    @property
    def filter_mode(self):
        """return current filter mode"""
        return self._mode

    def set_filter_mode(self, val, timedelta=None):
        """set current filter mode"""
        if val != self._mode:
            self._mode = val
            self.set_Q_R(timedelta=timedelta)


def fx(x, dt):  # pylint: disable=invalid-name
    """Constant-velocity with damped v: T coasts, v → 0 over _VEL_TAU."""
    decay = np.exp(-dt / _VEL_TAU)
    vel = float(x[1])
    dT = vel * _VEL_TAU * (1.0 - decay)
    return np.array([float(x[0]) + dT, vel * decay])


def hx(x):  # pylint: disable=invalid-name
    return x[:1]  # return position [x]
