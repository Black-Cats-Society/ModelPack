"""Local NURBS fillets with exact preservation of the remaining curve spans."""
from bisect import bisect_left, bisect_right
import math
import numpy as np
from maya.api import OpenMaya as om


def insert(points, knots, degree, value):
    """Insert one knot in homogeneous coordinates."""
    n = len(points) - 1
    k = n if value >= knots[n+1] else bisect_right(knots, value) - 1
    multiplicity = sum(abs(u-value) < 1e-12 for u in knots)
    result = [None] * (len(points)+1)
    for i in range(k-degree+1):
        result[i] = points[i].copy()
    for i in range(k-multiplicity, n+1):
        result[i+1] = points[i].copy()
    for i in range(k-degree+1, k-multiplicity+1):
        alpha = (value-knots[i]) / (knots[i+degree]-knots[i])
        result[i] = points[i-1]*(1-alpha) + points[i]*alpha
    return result, knots[:k+1] + [value] + knots[k+1:]


def elevate(points, target):
    points = [p.copy() for p in points]
    while len(points)-1 < target:
        degree = len(points)-1
        result = [points[0]]
        for i in range(1, degree+1):
            alpha = i/(degree+1)
            result.append(points[i-1]*alpha + points[i]*(1-alpha))
        points = result + [points[-1]]
    return points


def curve_data(points, knots, degree, closed=False):
    owner = om.MFnNurbsCurveData().create()
    cvs = [om.MPoint(*(p[:3]/p[3]), p[3]) for p in points]
    om.MFnNurbsCurve().create(cvs, knots[1:-1], degree,
        om.MFnNurbsCurve.kClosed if closed else om.MFnNurbsCurve.kOpen,
        False, any(abs(p[3]-1)>1e-12 for p in points), owner)
    return owner


def _linear_keep(points, closed, tolerance):
    """Keep turns and bound the departure of each simplified segment."""
    indices = [0]
    for index in range(1, len(points)):
        if points[index] != points[indices[-1]]:
            indices.append(index)
    if closed:
        if len(indices) > 1 and points[indices[-1]] == points[0]:
            indices.pop()
        if len(indices) < 3:
            return None
    elif len(indices) < 2:
        return None
    else:
        indices[-1] = len(points) - 1

    turns = []
    for i in (range(len(indices)) if closed else range(1, len(indices) - 1)):
        before, point, after = (points[indices[j % len(indices)]] for j in (i - 1, i, i + 1))
        incoming, outgoing = point - before, after - point
        if incoming * outgoing <= 0 or (incoming ^ outgoing).length() > tolerance * (after - before).length():
            turns.append(i)
    if closed:
        if len(turns) < 2:
            return None
        indices += indices[:turns[0] + 1]
        stops = [*turns, len(indices) - 1]
    else:
        stops = [0, *turns, len(indices) - 1]

    pending = list(zip(stops, stops[1:]))
    kept = {indices[i] for i in stops}
    while pending:
        first, last = pending.pop()
        if last - first < 2:
            continue
        start, end = points[indices[first]], points[indices[last]]
        chord = end - start
        length = chord.length()
        direction = chord / length if length else chord
        def distance(i):
            delta = points[indices[i]] - start
            along = max(0., min(length, delta * direction))
            return (delta - direction * along).length()

        split = max(range(first + 1, last), key=distance)
        if distance(split) > tolerance:
            kept.add(indices[split])
            pending.extend(((first, split), (split, last)))
    return sorted(kept) if len(kept) >= (3 if closed else 2) else None


def _remove_exact_knot(points, knots, degree, value, tolerance):
    """Invert one knot insertion and verify all affected coefficients."""
    index = bisect_right(knots, value) - 1
    multiplicity = index - bisect_left(knots, value)
    first, last = index - degree, index - 1 - multiplicity
    if not 0 < first <= last < len(points) - 1:
        return None
    width = last - first
    system = np.zeros((width + 1, width))
    target = points[first:last + 1].copy()
    for row, i in enumerate(range(first, last + 1)):
        base = knots[i]
        end = i + degree
        span = knots[end + (end >= index)] - base
        if span <= 0:
            return None
        alpha = (value - base) / span
        if row:
            system[row, row - 1] = 1. - alpha
        else:
            target[row] -= (1. - alpha) * points[first - 1]
        if row < width:
            system[row, row] = alpha
        else:
            target[row] -= alpha * points[last + 1]
    controls = np.linalg.lstsq(system, target, rcond=None)[0] if width else np.empty((0, 4))
    error = np.abs(system @ controls - target)
    if (not np.all(controls[:, 3] > 0) or np.max(error[:, :3]) > tolerance or
            np.max(error[:, 3]) > 1e-12):
        return None
    return np.concatenate((points[:first], controls, points[last + 1:])), knots[:index] + knots[index + 1:]


def clean_geometry(fn, tolerance_percent=0.):
    """Reduce a polyline within a world-space tolerance or remove exact NURBS knots."""
    if not math.isfinite(tolerance_percent) or not 0. <= tolerance_percent <= 100.:
        raise ValueError('Clean Points: укажите допуск от 0 до 100%.')
    # Maya returns CVs backed by the point array. Copy the coordinates before
    # that temporary array is released.
    controls = [tuple(p) for p in fn.cvPositions()]
    if len(controls) <= fn.degree + 1 or any(p[3] <= 0 or not all(map(math.isfinite, p)) for p in controls):
        return None
    degree, form, knots = fn.degree, fn.form, list(fn.knots())
    points = [om.MPoint(*p[:3]) for p in controls]
    if degree == 1 and tolerance_percent:
        matrix = fn.dagPath().inclusiveMatrix()
        origin = points[0]
        points = [om.MPoint((p - origin) * matrix) for p in points]
    size = max(max(p[i] for p in points) - min(p[i] for p in points) for i in range(3))
    scale = max(abs(p[i]) for p in points for i in range(3))
    tolerance = max(size * 1e-9, min(16 * math.ulp(scale), size * 1e-7))
    closed = form != om.MFnNurbsCurve.kOpen
    if degree == 1:
        tolerance = max(tolerance, size * tolerance_percent / 100.)
        indices = _linear_keep(points[:-1] if closed else points, closed, tolerance)
        if indices is None:
            return None
        kept = [controls[i] for i in indices]
        parameters = [knots[i] for i in indices]
        if closed:
            kept.append(kept[0])
            length = knots[-1] - knots[0]
            first = parameters[0]
            parameters = [u - first + knots[0] for u in parameters] + [knots[0] + length]
    else:
        origin, weight = points[0], controls[0][3]
        homogeneous = np.array([(*((point - origin) * (p[3] / weight)), p[3] / weight)
                                for p, point in zip(controls, points)])
        full = [2 * knots[0] - knots[1], *knots, 2 * knots[-1] - knots[-2]]
        low, high = fn.knotDomain
        limit = tolerance * homogeneous[:, 3].min() * .1
        for value in sorted({u for u in knots if low < u < high}):
            while len(homogeneous) > degree + 1 and (candidate := _remove_exact_knot(
                    homogeneous, full, degree, value, limit)) is not None:
                new_points, new_knots = candidate
                if form == om.MFnNurbsCurve.kPeriodic and (
                        not np.array_equal(new_points[:degree], new_points[-degree:]) or
                        new_knots[:degree + 1] != full[:degree + 1] or
                        new_knots[-degree - 1:] != full[-degree - 1:]):
                    break
                homogeneous, full = candidate
                if value not in full:
                    break
        kept = [(origin.x + p[0] / p[3], origin.y + p[1] / p[3], origin.z + p[2] / p[3], p[3] * weight)
                for p in homogeneous]
        parameters = full[1:-1]
    if len(kept) >= len(controls):
        return None
    return dict(points=kept, knots=parameters, degree=degree, form=form)


class Curve:
    def __init__(self, fn, selected):
        self.degree = fn.degree
        self.closed = fn.form != om.MFnNurbsCurve.kOpen
        self.matrix = fn.dagPath().inclusiveMatrix()
        self.inverse = self.matrix.inverse()
        cvs = fn.cvPositions()
        self.points = []
        for p in cvs:
            # MPoint * matrix scales translation by w. Transform the
            # cartesian CV first, then restore its independent NURBS weight.
            world = om.MPoint(p.x, p.y, p.z) * self.matrix
            self.points.append(np.array((world.x*p.w, world.y*p.w, world.z*p.w, p.w)))
        knots = list(fn.knots())
        self.knots = [2*knots[0]-knots[1]] + knots + [2*knots[-1]-knots[-2]]
        # Use a world-space copy so width remains a distance under scaled parents.
        self.owner = curve_data(self.points, self.knots, self.degree)
        self.fn = om.MFnNurbsCurve(self.owner)
        self.length = self.fn.length()
        if self.length < 1e-8:
            raise ValueError('Curve Bevel: кривая слишком короткая.')
        low, high = fn.knotDomain
        count = fn.numCVs - (fn.degree if fn.form == om.MFnNurbsCurve.kPeriodic else 1 if self.closed else 0)
        centers = []
        all_positions = []
        for i in range(count):
            value = sum(self.knots[i+1:i+self.degree+1])/self.degree
            value = low + (value-low) % (high-low) if self.closed else max(low, min(high, value))
            all_positions.append(self.fn.findLengthFromParam(value))
        for index in selected:
            index = index % count if self.closed else index
            if index < 0 or index >= count or (not self.closed and index in (0, count-1)):
                raise ValueError('Curve Bevel: выберите внутренние точки кривой.')
            centers.append(all_positions[index])
        self.centers = sorted(set(centers))
        if not self.centers:
            raise ValueError('Curve Bevel: выделите точки CV кривой.')
        if self.closed:
            gaps = [(b-a, a) for a,b in zip(self.centers, self.centers[1:]+[self.centers[0]+self.length])]
            gap, start = max(gaps)
            self.seam = (start+gap*.5) % self.length
        else:
            self.seam = 0.
        self.positions = sorted((s-self.seam) % self.length if self.closed else s for s in self.centers)
        limits = []
        for center in self.centers:
            distances = [abs(center-s) for s in all_positions if abs(center-s)>self.length*1e-9]
            if self.closed:
                distances = [min(d, self.length-d) for d in distances]
            limits.extend(d for d in distances if d > self.length*1e-9)
        self.maximum = min(limits, default=self.length)*.49
        self.minimum_points = max(3 if self.degree == 1 else 4, self.degree+1)
        if fn.form == om.MFnNurbsCurve.kPeriodic:
            # Move the original seam into the interior of an extended periodic
            # control net, so ordinary knot insertion is valid on both ends.
            points, knots = self.points, self.knots
            period = high-low
            p = self.degree
            n = count
            self.points = points[n-p:n] + points + points[p:2*p]
            self.knots = ([knots[i+n]-period for i in range(-p, 0)] + knots +
                          [knots[i-n]+period for i in range(len(knots), len(knots)+p)])

    def parameter(self, distance):
        distance = max(0., min(self.length, distance))
        return self.fn.findParamFromLength(distance)

    def pieces(self, start, end):
        if end-start < self.length*1e-12:
            return []
        a, b = self.parameter(start), self.parameter(end)
        points = self.points
        knots = self.knots
        values = sorted({a, b} | {u for u in knots if a < u < b})
        for value in values:
            while sum(abs(u-value)<1e-12 for u in knots) < self.degree:
                points, knots = insert(points, knots, self.degree, value)
        result = []
        for k in range(self.degree, len(points)):
            if knots[k+1]-knots[k] > 1e-12 and knots[k] >= a-1e-12 and knots[k+1] <= b+1e-12:
                result.append(points[k-self.degree:k+1])
        return result

    def unchanged(self, start, end):
        if not self.closed:
            return self.pieces(start, end)
        start += self.seam
        end += self.seam
        if start >= self.length:
            return self.pieces(start-self.length, end-self.length)
        if end <= self.length:
            return self.pieces(start, end)
        return self.pieces(start, self.length) + self.pieces(0., end-self.length)

    def fillet(self, center, width, degree, count):
        start = center-width
        end = center+width
        if self.closed:
            start %= self.length
            end %= self.length
        a, b = self.parameter(start), self.parameter(end)
        first, last = self.fn.getPointAtParam(a), self.fn.getPointAtParam(b)
        incoming, outgoing = self.fn.tangent(a).normal(), self.fn.tangent(b).normal()
        if (first-last).length() < self.length*1e-10:
            raise ValueError('Curve Bevel: вырожденный угол.')
        angle = math.acos(max(-1., min(1., incoming*outgoing)))
        if self.degree == 1 and angle < 1e-5:
            raise ValueError('Curve Bevel: выделенная точка лежит на прямой.')
        if angle > math.pi-1e-4:
            raise ValueError('Curve Bevel: нельзя скруглить разворот на 180°.')
        if self.degree == 1:
            corner = self.fn.getPointAtParam(self.parameter(center))
            weight = math.cos(angle*.5)
            points = [np.array(tuple(first)), np.array(tuple(corner))*weight, np.array(tuple(last))]
            # MPoint.w is homogeneous weight, not a fourth spatial coordinate.
            points[1][3] = weight
            points = elevate(points, degree)
        else:
            chord = (last-first).length()
            points = [np.array(tuple(p)) for p in (
                first, first+incoming*(chord/3), last-outgoing*(chord/3), last)]
            points = elevate(points, degree)
        knots = [0.]*(degree+1) + [1.]*(degree+1)
        extra = count-len(points)
        for i in range(extra):
            points, knots = insert(points, knots, degree, (i+1)/(extra+1))
        return points, knots

    def build(self, width, count):
        if not math.isfinite(width) or width <= 0:
            raise ValueError('Curve Bevel: ширина должна быть больше нуля.')
        width = min(width, self.maximum)
        degree = max(2 if self.degree == 1 else 3, self.degree)
        count = max(degree+1, min(64, int(count)))
        pieces = []
        start = 0.
        for position in self.positions:
            pieces.extend((elevate(p, degree), [0.]*(degree+1)+[1.]*(degree+1))
                          for p in self.unchanged(start, position-width))
            center = (position+self.seam) % self.length if self.closed else position
            pieces.append(self.fillet(center, width, degree, count))
            start = position+width
        pieces.extend((elevate(p, degree), [0.]*(degree+1)+[1.]*(degree+1))
                      for p in self.unchanged(start, self.length))
        points = []
        knots = [0.]*(degree+1)
        for i, (controls, local) in enumerate(pieces):
            if points:
                controls = [p*(points[-1][3]/controls[0][3]) for p in controls]
            points.extend(controls if not points else controls[1:])
            knots.extend(i+u for u in local[degree+1:-degree-1])
            knots.extend([float(i+1)]*(degree if i<len(pieces)-1 else degree+1))
        # Native setAttr consumes local cartesian CV coordinates and weights.
        local = []
        for p in points:
            point = om.MPoint(*(p[:3]/p[3])) * self.inverse
            local.append((point.x, point.y, point.z, float(p[3])))
        return dict(points=local, knots=knots[1:-1], degree=degree,
                    form=om.MFnNurbsCurve.kClosed if self.closed else om.MFnNurbsCurve.kOpen)
