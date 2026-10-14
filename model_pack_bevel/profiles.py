"""Bounded cubic curves shared by the viewport, editor and saved history node."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Curve:
    # x, y, optional tangent dy/dx. None requests a shape-preserving tangent.
    points: tuple = ((0.0, 1.0, None), (1.0, 0.0, None))
    falloff: bool = True

    def __post_init__(self):
        if len(self.points) < 2:
            raise ValueError("Кривая должна содержать хотя бы две точки.")
        if any(not all(math.isfinite(float(v)) for v in p if v is not None)
               for p in self.points):
            raise ValueError("Координаты кривой должны быть конечными числами.")
        if any(b[0] <= a[0] for a, b in zip(self.points, self.points[1:])):
            raise ValueError("Точки профиля должны идти слева направо.")
        if abs(self.points[0][0]) > 1e-8 or abs(self.points[-1][0] - 1) > 1e-8:
            raise ValueError("Края кривой должны лежать на X=0 и X=1.")
        endpoints = (1, 0) if self.falloff else (0, 0)
        if any(abs(self.points[i][1] - y) > 1e-8 for i, y in zip((0, -1), endpoints)):
            raise ValueError("Граничные точки профиля закреплены.")
        if any(not (0 <= p[1] <= 1) if self.falloff else not (-1 <= p[1] <= 1)
               for p in self.points):
            raise ValueError("Точка находится вне диапазона профиля.")
        if self.falloff and any(b[1] > a[1] for a, b in zip(self.points, self.points[1:])):
            raise ValueError("Затухание должно убывать от точки воздействия.")

    def value(self, x):
        x = max(0.0, min(1.0, x))
        pts = self.points
        secants = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(pts, pts[1:])]
        slopes = [secants[0]]
        for a, b in zip(secants, secants[1:]):
            slopes.append(0.0 if a * b <= 0 else 2 * a * b / (a + b))
        slopes.append(secants[-1])
        slopes = [p[2] if len(p) > 2 and p[2] is not None else slope
                  for p, slope in zip(pts, slopes)]
        index = next((i for i, p in enumerate(pts[1:]) if x <= p[0]), len(pts) - 2)
        a, b = pts[index:index + 2]
        h = b[0] - a[0]
        t = (x - a[0]) / h
        m0, m1 = slopes[index:index + 2]
        if self.falloff:
            d = secants[index]
            if abs(d) < 1e-12:
                m0 = m1 = 0
            else:
                alpha, beta = max(0, m0 / d), max(0, m1 / d)
                norm = math.hypot(alpha, beta)
                if norm > 3:
                    alpha, beta = alpha * 3 / norm, beta * 3 / norm
                m0, m1 = alpha * d, beta * d
        result = ((2*t**3 - 3*t*t + 1) * a[1] + (t**3 - 2*t*t + t) * h * m0 +
                  (-2*t**3 + 3*t*t) * b[1] + (t**3 - t*t) * h * m1)
        return max(0 if self.falloff else -1, min(1, result))

    def data(self):
        return [list(p) for p in self.points]


def falloff_value(kind, x, curve=None):
    if x >= 1:
        return 0.0
    if x <= 0:
        return 1.0
    if kind == "linear":
        return 1 - x
    if kind == "custom":
        return curve.value(x)
    return (1 + math.cos(math.pi * x)) * .5


def profile_value(kind, x, shape, curve=None):
    if x <= 1e-7 or x >= 1 - 1e-7:
        return 0.0
    base = curve.value(x) if kind == "custom" else 0.0
    return max(-1, min(1, base + shape * math.sin(math.pi * x)))
