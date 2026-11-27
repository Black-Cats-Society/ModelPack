"""Screen gestures with a frozen mode, axis and parameter snapshot."""
from dataclasses import dataclass
import math


WIDTH_SENSITIVITY = .2


def right_gesture_mode(shift=False, ctrl=False):
    if shift:
        return "support_percent" if ctrl else "support_edges"
    return "width" if ctrl else "support_percent"


def gesture_mode(shift=False, ctrl=False, *, sharp=False):
    if ctrl:
        return "local" if shift else "shape"
    if shift and not sharp:
        return "segments"
    return "width"


@dataclass
class Gesture:
    mode: str
    width: float
    shape: float
    segments: int
    scale: float
    axis: int | None = None
    direction: tuple | None = None
    fine: bool = False
    support_edges: int = 0
    support_percent: float = 0.

    def amount(self, delta):
        if self.axis is None:
            if max(abs(delta[0]), abs(delta[1])) < 4:
                return 0.0
            self.axis = 0 if abs(delta[0]) > abs(delta[1]) else 1
        if self.mode == "width" and self.width <= 1e-9:
            # The first movement defines the direction of increasing width.
            # Moving back towards the anchor reduces it, in any screen quadrant.
            if self.direction is None:
                length = math.hypot(*delta)
                self.direction = tuple(value / length for value in delta)
            return sum(value * direction for value,direction in zip(delta,self.direction))
        return delta[self.axis]

    def value(self, delta):
        amount = self.amount(delta)
        if self.mode in ("segments", "support_edges"):
            step = math.floor(abs(amount) / 20) * (1 if amount >= 0 else -1)
            if self.mode == "segments":
                return max(1, min(64, self.segments + step))
            return max(0, self.support_edges + step)
        if self.mode == "support_percent":
            return max(0., self.support_percent + amount * .1)
        if self.mode == "shape":
            return max(-1, min(1, self.shape + amount / 100))
        width_delta = amount * self.scale * WIDTH_SENSITIVITY * (.2 if self.fine or self.mode == "local" else 1.)
        return width_delta if self.mode == "local" else max(0, self.width + width_delta)
