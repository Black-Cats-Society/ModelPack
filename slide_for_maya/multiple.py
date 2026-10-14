"""Create several cuts between selected source edges and the dragged cut."""

from __future__ import annotations

import math
from dataclasses import replace

import maya.cmds as cmds
from maya.api import OpenMaya as om

from .bevel import LiveBevel, analyze_bevel_selection, apply_bevel, _prepare_planar_fans
from .core import (LiveEdit, SlideError, _EPS, _mesh_fn, _mesh_position_tolerance, _selected_edges, _combined_plan,
                   analyze_selection, apply)


def _segments(mesh, edge_ids):
    fn = _mesh_fn(mesh)
    points = fn.getPoints(om.MSpace.kWorld)
    return tuple(
        (tuple(points[a])[:3], tuple(points[b])[:3])
        for edge in edge_ids
        for a, b in (fn.getEdgeVertices(edge),)
    )


def _vertex_pairs(mesh, edge_ids):
    fn = _mesh_fn(mesh)
    return tuple(tuple(fn.getEdgeVertices(edge)) for edge in edge_ids)


def _edges_on_segments(mesh, segments, vertex_pairs=None):
    fn = _mesh_fn(mesh)
    if vertex_pairs is not None:
        edge_map = {frozenset(fn.getEdgeVertices(edge)): edge for edge in range(fn.numEdges)}
        result = [edge_map.get(frozenset(pair)) for pair in vertex_pairs]
        if all(edge is not None for edge in result):
            return tuple(sorted(set(result)))
    # A later cut may subdivide an earlier contour. Resolve its segments only
    # when the saved vertex pairs no longer describe intact edges.
    positions = fn.getPoints(om.MSpace.kWorld)
    prepared = []
    for start, end in segments:
        direction = tuple(y - x for x, y in zip(start, end))
        squared = sum(axis * axis for axis in direction)
        if squared >= 1e-12:
            tolerance = _mesh_position_tolerance(fn, (start, end), math.sqrt(squared))
            prepared.append((start, direction, squared, tolerance * tolerance))
    result = []
    for edge in range(fn.numEdges):
        a, b = fn.getEdgeVertices(edge)
        points = (positions[a], positions[b])
        for start, direction, squared, tolerance_squared in prepared:
            vx, vy, vz = direction
            for point in points:
                dx, dy, dz = point.x - start[0], point.y - start[1], point.z - start[2]
                position = (dx * vx + dy * vy + dz * vz) / squared
                if not -1e-5 <= position <= 1.0 + 1e-5:
                    break
                if ((dx - vx * position) ** 2 + (dy - vy * position) ** 2 +
                        (dz - vz * position) ** 2 > tolerance_squared):
                    break
            else:
                result.append(edge)
                break
    return tuple(result)


def _distribution_fraction(layer: int, count: int, percent: float) -> float:
    """Map equal positions through a monotone bias anchored at 50% in the middle."""
    if layer == count:
        return 1.0
    position = layer / count
    bias = percent / 100.0
    return bias * position / (bias * position + (1.0 - bias) * (1.0 - position))


def _validate_options(count, mode, bias_percent):
    if not isinstance(count, int) or not 1 <= count <= 16:
        raise SlideError("Количество рёбер должно быть целым числом от 1 до 16.")
    if not math.isfinite(bias_percent) or not 0.0 <= bias_percent <= 100.0:
        raise SlideError("Положение промежуточных рёбер должно быть от 0 до 100%.")
    if mode not in ("uniform", "relative"):
        raise SlideError("Режим должен быть uniform или relative.")
    # At either endpoint all intermediates coincide with an existing contour.
    return 1 if bias_percent in (0, 100) else count


def _matching_clone_side(reference) -> int:
    """Keep the geometric face side when Maya renumbers faces after a cut."""
    choices = []
    for side in (0, 1):
        try:
            plan = analyze_selection(side)
        except SlideError:
            continue
        alignment = []
        for start, end in zip(plan.starts, plan.ends):
            direction = tuple(b - a for a, b in zip(start, end))
            length = math.sqrt(sum(axis * axis for axis in direction))
            if length < 1e-9:
                continue
            nearest = min(range(len(reference.starts)),
                          key=lambda index: math.dist(start, reference.starts[index]))
            original = tuple(b - a for a, b in zip(
                reference.starts[nearest], reference.ends[nearest]))
            original_length = math.sqrt(sum(axis * axis for axis in original))
            if original_length < 1e-9:
                continue
            alignment.append(sum(a * b for a, b in zip(direction, original)) /
                             (length * original_length))
        if alignment:
            choices.append((sum(alignment) / len(alignment), side))
    if not choices:
        raise SlideError("Не удалось определить исходную сторону разреза.")
    return max(choices)[1]


def apply_multiple(amount: float, *, bevel: bool, count: int, mode: str,
                   side: int = 0, bias_percent: float = 50.0,
                   undo: bool = True) -> float:
    """Make ``count`` contours; bias distributes intermediates toward either end."""
    count = _validate_options(count, mode, bias_percent)
    mesh, original_ids = _selected_edges()
    originals = _segments(mesh, original_ids)
    original_pairs = _vertex_pairs(mesh, original_ids)
    reference = None if bevel else analyze_selection(side)
    if undo:
        cmds.undoInfo(openChunk=True, chunkName="Slide Bevel" if bevel else "Clone Slide")
    modified = False
    try:
        created = []
        created_pairs = []
        outer = None
        previous_fraction = 1.0
        for layer in range(count, 0, -1):
            if layer != count:
                source_edges = _edges_on_segments(mesh, originals, original_pairs)
                if not source_edges:
                    raise SlideError("Не удалось найти исходные рёбра для следующего разреза.")
                cmds.select([f"{mesh}.e[{edge}]" for edge in source_edges], replace=True)
            current_side = _matching_clone_side(reference) if not bevel and layer != count else side
            fraction = _distribution_fraction(layer, count, bias_percent)
            if layer == count:
                local_amount = amount
            elif mode == "uniform":
                local_amount = outer * fraction
            else:
                local_amount = fraction / previous_fraction
            modified = True
            actual = (apply_bevel(local_amount, mode=mode, _undo=False) if bevel else
                      apply(local_amount, side=current_side, clone=True,
                            mode=mode, _undo=False))
            if outer is None:
                outer = actual
            previous_fraction = fraction
            _, new_ids = _selected_edges()
            created.extend(_segments(mesh, new_ids))
            created_pairs.extend(_vertex_pairs(mesh, new_ids))
        result = _edges_on_segments(mesh, created, created_pairs)
        if not result:
            raise SlideError("Не удалось найти созданные рёбра.")
        cmds.select([f"{mesh}.e[{edge}]" for edge in result], replace=True)
    except Exception:
        if undo:
            cmds.undoInfo(closeChunk=True)
            if modified:
                cmds.undo()
        raise
    else:
        if undo:
            cmds.undoInfo(closeChunk=True)
    return outer


class _PlanGeometry:
    """Cache vertex references before later cuts renumber the source faces."""

    def __init__(self, plan, fn):
        self.plan = plan
        self.children = tuple(_PlanGeometry(child, fn) for child in plan.children)
        self.guides = {
            face: (tuple(fn.getPolygonVertices(face)),
                   fn.getPolygonNormal(face, om.MSpace.kWorld))
            for face in set(plan.guide_faces) if face >= 0
        }
        self.patch_vertices = tuple(sorted({
            vertex for face in plan.patch_faces for vertex in fn.getPolygonVertices(face)
        }))

    def refresh(self, points):
        plan = self.plan
        if self.children:
            return _combined_plan([child.refresh(points) for child in self.children])
        starts = tuple(tuple(points[vertex])[:3] for vertex in plan.vertices)
        if self.patch_vertices:
            center = om.MPoint()
            for vertex in self.patch_vertices:
                center += points[vertex]
            center /= len(self.patch_vertices)
            ends = (tuple(center)[:3],) * len(starts)
        else:
            ends = []
            for vertex, target, face in zip(plan.vertices, plan.targets, plan.guide_faces):
                if target >= 0:
                    ends.append(tuple(points[target])[:3])
                    continue
                polygon, normal = self.guides[face]
                center = om.MPoint()
                for member in polygon:
                    center += points[member]
                center /= len(polygon)
                origin = points[vertex]
                direction = center - origin
                intersections = []
                for index, first in enumerate(polygon):
                    second = polygon[(index + 1) % len(polygon)]
                    segment = points[second] - points[first]
                    denominator = (direction ^ segment) * normal
                    if abs(denominator) < 1e-12:
                        continue
                    offset = points[first] - origin
                    along = ((offset ^ segment) * normal) / denominator
                    across = ((offset ^ direction) * normal) / denominator
                    if along > _EPS and -_EPS <= across <= 1.0 + _EPS:
                        intersections.append(along)
                if not intersections:
                    raise SlideError("Не удалось обновить угловую направляющую разреза.")
                ends.append(tuple(origin + direction * min(intersections))[:3])
            ends = tuple(ends)
        return replace(plan, starts=starts, ends=ends)


class _StageGeometry:
    def __init__(self, plan, bevel):
        self.plan = plan
        self.bevel = bevel
        fn = _mesh_fn(plan.mesh)
        self.runs = tuple(_PlanGeometry(run, fn) for run in plan.runs) if bevel else (
            _PlanGeometry(plan, fn),)

    def refresh(self):
        points = _mesh_fn(self.plan.mesh).getPoints(om.MSpace.kWorld)
        runs = tuple(run.refresh(points) for run in self.runs)
        if not self.bevel:
            return runs[0]
        lengths = tuple((points[b] - points[a]).length()
                        for a, b in self.plan.edge_vertices)
        return replace(self.plan, runs=runs, edge_lengths=lengths)


class LiveMultiple:
    """Build disposable preview topology once, then move cached cut vertices."""

    def __init__(self, *, bevel, count, mode, side=0, bias_percent=50):
        self.count = _validate_options(count, mode, bias_percent)
        self.bevel = bevel
        self.mode = mode
        self.side = side
        self.fractions = tuple(_distribution_fraction(layer, self.count, bias_percent)
                               for layer in range(self.count, 0, -1))
        self.mesh, original_ids = _selected_edges()
        self.originals = _segments(self.mesh, original_ids)
        self.original_pairs = _vertex_pairs(self.mesh, original_ids)
        self.reference = None if bevel else analyze_selection(side)
        self.stages = []
        self.new_edges = ()
        self.actual = None

    def update(self, amount):
        building = not self.stages
        created = []
        created_pairs = []
        previous_fraction = 1.0
        outer = None
        for index, fraction in enumerate(self.fractions):
            if building:
                if index:
                    original_ids = _edges_on_segments(self.mesh, self.originals, self.original_pairs)
                    cmds.select([f"{self.mesh}.e[{edge}]" for edge in original_ids], replace=True)
                if self.bevel:
                    plan = _prepare_planar_fans(analyze_bevel_selection())
                    live = LiveBevel(plan, mode=self.mode, preview=True)
                else:
                    side = self.side if not index else _matching_clone_side(self.reference)
                    plan = analyze_selection(side)
                    live = LiveEdit(plan, clone=True, mode=self.mode, preview=True)
                geometry = _StageGeometry(plan, self.bevel)
                self.stages.append((live, geometry))
            else:
                live, geometry = self.stages[index]
                # The outer stage references only unchanged source vertices.
                # Inner stages follow the moved outer contours and need refresh.
                if index:
                    live.plan = geometry.refresh()
            local_amount = (amount if not index else outer * fraction
                            if self.mode == "uniform" else fraction / previous_fraction)
            actual = live.update(local_amount)
            if not index:
                outer = actual
            previous_fraction = fraction
            if building:
                if self.bevel:
                    cmds.select([f"{self.mesh}.e[{edge}]" for edge in live._new_edges], replace=True)
                _, new_ids = _selected_edges()
                created.extend(_segments(self.mesh, new_ids))
                created_pairs.extend(_vertex_pairs(self.mesh, new_ids))
        if building:
            self.new_edges = _edges_on_segments(self.mesh, created, created_pairs)
            cmds.select([f"{self.mesh}.e[{edge}]" for edge in self.new_edges], replace=True)
        self.actual = outer
        return outer

    def cancel(self):
        for live, _ in self.stages:
            live.cancel()
        self.stages = []
        self.actual = None
