"""Screen gestures with a frozen mode, axis and parameter snapshot."""
from dataclasses import dataclass
import math


def gesture_mode(shift=False, ctrl=False):
    return "local" if shift and ctrl else "segments" if shift else "shape" if ctrl else "width"


@dataclass
class Gesture:
    mode: str
    width: float
    shape: float
    segments: int
    scale: float
    axis: int | None = None
    direction: tuple | None = None

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
        if self.mode == "segments":
            step = math.floor(abs(amount) / 20) * (1 if amount >= 0 else -1)
            return max(1, min(64, self.segments + step))
        if self.mode == "shape":
            return max(-1, min(1, self.shape + amount / 100))
        if self.mode == "local":
            return amount * self.scale
        return max(0, self.width + amount * self.scale)
