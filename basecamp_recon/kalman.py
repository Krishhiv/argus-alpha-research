"""
Constant-velocity Kalman filter - the trend/velocity estimator.

State x = [level, velocity]. We observe the price (microprice); the filter
estimates the hidden *velocity* (trend) as the optimal blend of a motion model
and the noisy measurement. Unlike an EMA it carries a velocity term, so on a
steady trend it *extrapolates* instead of lagging behind.

Model (continuous white-noise-acceleration, discretised per step):
    F = [[1, dt],        Q = q * [[dt^3/3, dt^2/2],
         [0,  1]]                 [dt^2/2, dt    ]]
    z = level + noise,   R = r          (scalar measurement variance)

The single knob that matters is the ratio q/r:
    high q/r  -> trust the data  -> responsive, low-lag, noisier velocity
    low  q/r  -> trust the model -> smooth, higher-lag
"""

from __future__ import annotations

import numpy as np


class ConstantVelocityKalman:
    def __init__(self, q: float = 1e-6, r: float = 1e-2, dt: float = 1.0) -> None:
        self.q = float(q)      # velocity process-noise spectral density
        self.r = float(r)      # measurement-noise variance
        self.dt0 = float(dt)
        self.x: np.ndarray | None = None   # state [level, velocity]
        self.P: np.ndarray | None = None   # state covariance

    def _F(self, dt: float) -> np.ndarray:
        return np.array([[1.0, dt], [0.0, 1.0]])

    def _Q(self, dt: float) -> np.ndarray:
        return self.q * np.array([[dt**3 / 3.0, dt**2 / 2.0],
                                  [dt**2 / 2.0, dt]])

    def reset(self, z0: float) -> "ConstantVelocityKalman":
        self.x = np.array([float(z0), 0.0])
        self.P = np.array([[1.0, 0.0], [0.0, 1.0]])   # diffuse-ish prior
        return self

    def step(self, z: float, dt: float | None = None) -> tuple[float, float]:
        """Advance one observation; return (level, velocity)."""
        if self.x is None:
            self.reset(z)
            return float(self.x[0]), float(self.x[1])
        dt = self.dt0 if (dt is None or dt <= 0) else float(dt)
        F, Q = self._F(dt), self._Q(dt)
        H = np.array([[1.0, 0.0]])
        # predict
        x = F @ self.x
        P = F @ self.P @ F.T + Q
        # update
        y = float(z) - float((H @ x)[0])              # innovation
        S = float((H @ P @ H.T)[0, 0]) + self.r        # innovation variance
        K = (P @ H.T)[:, 0] / S                         # Kalman gain (2-vector)
        x = x + K * y
        P = (np.eye(2) - np.outer(K, H[0])) @ P
        self.x, self.P = x, P
        return float(x[0]), float(x[1])

    def filter(self, prices, dts=None) -> tuple[np.ndarray, np.ndarray]:
        """Run over a price series; return (levels, velocities) arrays."""
        prices = np.asarray(prices, dtype=float)
        n = len(prices)
        dts = np.full(n, self.dt0) if dts is None else np.asarray(dts, dtype=float)
        levels = np.empty(n)
        vels = np.empty(n)
        self.x = self.P = None   # fresh run
        for i in range(n):
            lvl, vel = self.step(prices[i], self.dt0 if i == 0 else dts[i])
            levels[i], vels[i] = lvl, vel
        return levels, vels


def ema(prices, alpha: float) -> np.ndarray:
    """Plain EMA, for the side-by-side lag comparison vs the Kalman level."""
    prices = np.asarray(prices, dtype=float)
    out = np.empty(len(prices))
    acc = prices[0]
    for i, p in enumerate(prices):
        acc = (1 - alpha) * acc + alpha * p
        out[i] = acc
    return out
