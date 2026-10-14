"""Geometry operations for continuous edge chains on polygon meshes.

The selected side can follow adjacent faces or turn inside one face. Complete
face perimeters use facet extrusion to build an inner loop.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import math
import re
from typing import Literal

import maya.cmds as cmds
from maya.api import OpenMaya as om


_EDGE_RE = re.compile(r"^(.+)\.e\[(\d+)\]$")
_EPS = 1e-6
_MAX_FRACTION = 0.99  # polySplit can merge points placed closer to an endpoint.


class SlideError(ValueError):
    """A selection or topology cannot be processed without ambiguity."""


@dataclass(frozen=True)
class Plan:
    mesh: str
    edge_ids: tuple[int, ...]
    vertices: tuple[int, ...]
    faces: tuple[int, ...]
    rails: tuple[int, ...]
    targets: tuple[int, ...]
    guide_faces: tuple[int, ...]
    starts: tuple[tuple[float, float, float], ...]
    ends: tuple[tuple[float, float, float], ...]
    original_vertex_count: int
    cyclic: bool
    patch_faces: tuple[int, ...] = ()
    children: tuple[Plan, ...] = ()


def _combined_plan(plans):
    if len(plans) == 1:
        return plans[0]
    attributes = ("edge_ids", "vertices", "faces", "rails", "targets", "guide_faces", "starts", "ends")
    return replace(plans[0],
                   **{name: tuple(value for plan in plans for value in getattr(plan, name))
                      for name in attributes},
                   cyclic=False, patch_faces=(), children=tuple(plans))


def _mesh_fn(mesh: str) -> om.MFnMesh:
    selection = om.MSelectionList()
    selection.add(mesh)
    path = selection.getDagPath(0)
    if path.node().hasFn(om.MFn.kTransform):
        path.extendToShape()
    if not path.node().hasFn(om.MFn.kMesh):
        raise SlideError("Выделение должно принадлежать polygon mesh.")
    return om.MFnMesh(path)


def _mesh_position_tolerance(fn: om.MFnMesh, points, span: float = 0.0) -> float:
    """Account for float vertex rounding in object space and transformed scale."""
    matrix = fn.dagPath().inclusiveMatrix()
    inverse = matrix.inverse()
    local_magnitude = 0.0
    world_magnitude = 0.0
    for point in points:
        world = om.MPoint(point)
        local = world * inverse
        local_magnitude = max(local_magnitude, abs(local.x), abs(local.y), abs(local.z))
        world_magnitude = max(world_magnitude, abs(world.x), abs(world.y), abs(world.z))
    scale = max((om.MVector(*axis) * matrix).length()
                for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1)))
    return max(_EPS, span * 1e-5, local_magnitude * scale * (2.0 ** -22),
               world_magnitude * (2.0 ** -50))


def _selected_edges() -> tuple[str, tuple[int, ...]]:
    components = cmds.ls(selection=True, flatten=True, long=True) or []
    if not components:
        raise SlideError("Выделите одно ребро, открытую цепочку или замкнутую петлю.")
    mesh = None
    edge_ids = []
    for component in components:
        match = _EDGE_RE.match(component)
        if not match:
            raise SlideError("Для этого действия выделите только рёбра polygon mesh.")
        if mesh is None:
            mesh = match.group(1)
        elif mesh != match.group(1):
            raise SlideError("Для одного жеста выделите рёбра одного объекта.")
        edge_ids.append(int(match.group(2)))
    return mesh, tuple(sorted(set(edge_ids)))


def _ordered_chain(fn: om.MFnMesh, edge_ids: tuple[int, ...]):
    incident: dict[int, list[int]] = {}
    edge_vertices = {}
    for edge_id in edge_ids:
        if edge_id < 0 or edge_id >= fn.numEdges:
            raise SlideError("Выделение устарело после изменения сетки.")
        a, b = fn.getEdgeVertices(edge_id)
        edge_vertices[edge_id] = (a, b)
        incident.setdefault(a, []).append(edge_id)
        incident.setdefault(b, []).append(edge_id)
    if any(len(edges) > 2 for edges in incident.values()):
        raise SlideError("Ветвящиеся выделения пока не поддерживаются.")
    endpoints = sorted(v for v, edges in incident.items() if len(edges) == 1)
    if len(endpoints) not in (0, 2):
        raise SlideError("Выделение должно быть одной цепочкой или петлёй.")
    cyclic = not endpoints
    start = endpoints[0] if endpoints else min(incident)
    vertices = [start]
    ordered_edges = []
    used = set()
    current = start
    while True:
        available = sorted(e for e in incident[current] if e not in used)
        if not available:
            break
        edge_id = available[0]
        used.add(edge_id)
        ordered_edges.append(edge_id)
        a, b = edge_vertices[edge_id]
        current = b if a == current else a
        if cyclic and current == start:
            break
        vertices.append(current)
    if len(used) != len(edge_ids):
        raise SlideError("Несвязанные цепочки обрабатываются отдельно.")
    if cyclic and len(ordered_edges) < 3:
        raise SlideError("Замкнутая петля должна содержать хотя бы три ребра.")
    return tuple(ordered_edges), tuple(vertices), cyclic


def _edge_faces(fn: om.MFnMesh) -> dict[int, tuple[int, ...]]:
    faces: dict[int, list[int]] = {e: [] for e in range(fn.numEdges)}
    iterator = om.MItMeshPolygon(fn.dagPath())
    while not iterator.isDone():
        for edge_id in iterator.getEdges():
            faces[edge_id].append(iterator.index())
        iterator.next()
    return {edge: tuple(ids) for edge, ids in faces.items()}


def _polygon_edges(fn: om.MFnMesh, face: int) -> tuple[int, ...]:
    iterator = om.MItMeshPolygon(fn.dagPath())
    iterator.setIndex(face)
    return tuple(iterator.getEdges())


def _across_collapsed_strips(fn, edges, edge_faces, *, equivalents=None):
    """Follow coincident boundary edges across zero-area faces, without welding."""
    polygon = om.MItMeshPolygon(fn.dagPath())
    points = {}

    def endpoints(edge):
        pair = fn.getEdgeVertices(edge)
        for vertex in pair:
            if vertex not in points:
                points[vertex] = fn.getPoint(vertex, om.MSpace.kWorld)
        return tuple(points[vertex] for vertex in pair)

    selected = {edge for edge in edges if (endpoints(edge)[1] - endpoints(edge)[0]).length() > _EPS}
    pending = list(selected)
    collapsed = set()
    checked = set()
    while pending:
        edge = pending.pop()
        a, b = endpoints(edge)
        for face in edge_faces[edge]:
            if face not in checked:
                polygon.setIndex(face)
                checked.add(face)
                vertices = polygon.getVertices()
                for vertex in vertices:
                    if vertex not in points:
                        points[vertex] = fn.getPoint(vertex, om.MSpace.kWorld)
                face_points = tuple(points[vertex] for vertex in vertices)
                direction = max((point - face_points[0] for point in face_points),
                                key=lambda vector: vector.length())
                span = direction.length()
                tolerance = _mesh_position_tolerance(fn, face_points, span)
                # Maya's area calculation can report a small positive area for
                # a collinear face far from the origin. Test its points directly.
                if span <= _EPS or all(((point - face_points[0]) ^ direction.normal()).length()
                                      <= tolerance for point in face_points):
                    collapsed.add(face)
            if face not in collapsed:
                continue
            tolerance = _mesh_position_tolerance(fn, (a, b), (b - a).length())
            for candidate in _polygon_edges(fn, face):
                if candidate == edge:
                    continue
                c, d = endpoints(candidate)
                if ((a - c).length() <= tolerance and (b - d).length() <= tolerance or
                        (a - d).length() <= tolerance and (b - c).length() <= tolerance):
                    if equivalents is not None:
                        equivalents.setdefault(edge, set()).add(candidate)
                        equivalents.setdefault(candidate, set()).add(edge)
                    if candidate not in selected:
                        selected.add(candidate)
                        pending.append(candidate)
    return tuple(sorted(selected)), collapsed


def _other_face_edge(fn: om.MFnMesh, face: int, vertex: int, selected_edge: int) -> int:
    edges = [e for e in _polygon_edges(fn, face) if vertex in fn.getEdgeVertices(e)]
    if len(edges) != 2 or selected_edge not in edges:
        raise SlideError("Не удалось определить боковое ребро выбранной грани.")
    return edges[0] if edges[1] == selected_edge else edges[1]


def _face_ray_boundary(fn: om.MFnMesh, face: int, vertex: int,
                       center: om.MPoint) -> om.MPoint:
    """Continue the corner-to-center guide to its first face boundary."""
    origin = fn.getPoint(vertex, om.MSpace.kWorld)
    direction = center - origin
    normal = fn.getPolygonNormal(face, om.MSpace.kWorld)
    polygon = fn.getPolygonVertices(face)
    intersections = []
    for index, first in enumerate(polygon):
        second = polygon[(index + 1) % len(polygon)]
        a = fn.getPoint(first, om.MSpace.kWorld)
        segment = fn.getPoint(second, om.MSpace.kWorld) - a
        denominator = (direction ^ segment) * normal
        if abs(denominator) < _EPS:
            continue
        offset = a - origin
        along = ((offset ^ segment) * normal) / denominator
        across = ((offset ^ direction) * normal) / denominator
        if along > _EPS and -_EPS <= across <= 1.0 + _EPS:
            intersections.append(along)
    if not intersections:
        raise SlideError("Не удалось найти границу грани для углового смещения.")
    return origin + direction * min(intersections)


def _planar_patch(fn, edges, vertices, edge_faces, seed):
    """Find a coplanar face region bounded exactly by the selected loop."""
    selected = set(edges)
    region = {seed}
    pending = [seed]
    while pending:
        face = pending.pop()
        for edge in _polygon_edges(fn, face):
            if edge in selected:
                continue
            for neighbor in edge_faces[edge]:
                if neighbor not in region:
                    region.add(neighbor)
                    pending.append(neighbor)
    boundary = {edge for face in region for edge in _polygon_edges(fn, face)
                if any(neighbor not in region for neighbor in edge_faces[edge])}
    if boundary != selected:
        return ()
    normal = fn.getPolygonNormal(seed, om.MSpace.kWorld).normal()
    origin = fn.getPoint(vertices[0], om.MSpace.kWorld)
    scale = max(math.dist(tuple(origin)[:3], tuple(fn.getPoint(v, om.MSpace.kWorld))[:3])
                for v in vertices)
    tolerance = max(scale * 1e-5, _EPS)
    for face in region:
        other = fn.getPolygonNormal(face, om.MSpace.kWorld).normal()
        if normal * other < 0.999:
            return ()
        for vertex in fn.getPolygonVertices(face):
            if abs((fn.getPoint(vertex, om.MSpace.kWorld) - origin) * normal) > tolerance:
                return ()
    return tuple(sorted(region))


def _patch_plan(mesh, fn, edges, vertices, edge_faces, side):
    adjacent = edge_faces[edges[0]]
    if side >= len(adjacent):
        return None
    patch = _planar_patch(fn, edges, vertices, edge_faces, adjacent[side])
    if not patch:
        return None
    region = set(patch)
    faces = tuple(next(f for f in edge_faces[edge] if f in region) for edge in edges)
    members = {v for face in patch for v in fn.getPolygonVertices(face)}
    center = om.MPoint()
    for vertex in members:
        center += fn.getPoint(vertex, om.MSpace.kWorld)
    center /= len(members)
    starts = tuple(tuple(fn.getPoint(v, om.MSpace.kWorld))[:3] for v in vertices)
    ends = (tuple(center)[:3],) * len(vertices)
    return Plan(mesh, edges, vertices, faces, (-1,) * len(vertices),
                (-1,) * len(vertices), faces, starts, ends, fn.numVertices, True, patch)


def analyze_selection(side: int = 0) -> Plan:
    """Find face strips for selected chains, grouping coincident strip boundaries.

    ``side`` chooses one of the two faces next to the first ordered edge.
    For a boundary edge there is only side 0.
    """
    if side not in (0, 1):
        raise SlideError("Сторона должна быть 0 или 1.")
    mesh, edge_ids = _selected_edges()
    fn = _mesh_fn(mesh)
    edge_faces = _edge_faces(fn)
    equivalents = {}
    edges, collapsed = _across_collapsed_strips(fn, edge_ids, edge_faces, equivalents=equivalents)
    if not edges:
        raise SlideError("У выбранных рёбер нет ненулевого участка для разреза.")
    components = _chain_components(fn, edges)
    roots = {}
    for edge in edges:
        if edge in roots:
            continue
        pending = [edge]
        while pending:
            member = pending.pop()
            if member in roots:
                continue
            roots[member] = edge
            pending.extend(equivalents.get(member, ()))
    groups = {}
    for component in components:
        signature = frozenset(roots[edge] for edge in component)
        groups.setdefault(signature, []).append(component)
    plans = []
    original = set(edge_ids)
    for group in groups.values():
        if len(group) == 1 and not collapsed:
            plans.append(_analyze_chain(mesh, fn, group[0], side, edge_faces))
            continue
        group.sort(key=lambda component: min(set(component) & original, default=fn.numEdges))
        options = []
        errors = []
        for component in group:
            for local_side in (0, 1):
                try:
                    options.append(_analyze_chain(mesh, fn, component, local_side, edge_faces, collapsed))
                except SlideError as exc:
                    errors.append(str(exc))
        if len(options) <= side:
            raise SlideError(errors[0] if not options else "У граничного ребра доступна только одна сторона.")
        plans.append(options[side])
    return _combined_plan(plans)


def _chain_components(fn, edge_ids):
    incident = {}
    pairs = {}
    for edge in edge_ids:
        pairs[edge] = fn.getEdgeVertices(edge)
        for vertex in pairs[edge]:
            incident.setdefault(vertex, []).append(edge)
    remaining = set(edge_ids)
    components = []
    for first in edge_ids:
        if first not in remaining:
            continue
        pending = [first]
        component = []
        while pending:
            edge = pending.pop()
            if edge not in remaining:
                continue
            remaining.remove(edge)
            component.append(edge)
            for vertex in pairs[edge]:
                pending.extend(incident[vertex])
        component = tuple(sorted(component))
        _ordered_chain(fn, component)  # Reject real branches within a contour.
        components.append(component)
    return components


def _analyze_chain(mesh, fn, edge_ids, side, edge_faces, collapsed=()):
    edges, vertices, cyclic = _ordered_chain(fn, edge_ids)
    first_faces = edge_faces[edges[0]]
    if not first_faces or len(first_faces) > 2:
        raise SlideError("Non-manifold ребро не поддерживается.")
    if side >= len(first_faces):
        raise SlideError("У граничного ребра доступна только одна сторона.")
    if first_faces[side] in collapsed:
        raise SlideError("Схлопнутая полоса не содержит места для разреза.")

    if cyclic:
        patch_plan = _patch_plan(mesh, fn, edges, vertices, edge_faces, side)
        if patch_plan is not None:
            return patch_plan

    faces = [first_faces[side]]
    for index in range(1, len(edges)):
        previous = faces[-1]
        if edges[index] in _polygon_edges(fn, previous):
            faces.append(previous)
            continue
        shared_vertex = vertices[index]
        rail = _other_face_edge(fn, previous, shared_vertex, edges[index - 1])
        candidates = [f for f in edge_faces[edges[index]] if rail in _polygon_edges(fn, f)]
        if len(candidates) != 1:
            raise SlideError("Угол цепочки не образует однозначную полосу граней.")
        faces.append(candidates[0])

    if cyclic:
        closure_rail = _other_face_edge(fn, faces[-1], vertices[0], edges[-1])
        if closure_rail not in _polygon_edges(fn, faces[0]):
            raise SlideError("Петля не замыкает однозначную полосу граней.")

    rails = []
    targets = []
    guide_faces = []
    starts = []
    ends = []
    for index, vertex in enumerate(vertices):
        previous_face = faces[index - 1] if index > 0 else (faces[-1] if cyclic else None)
        next_face = faces[index] if index < len(faces) else None
        start = fn.getPoint(vertex, om.MSpace.kWorld)
        starts.append((start.x, start.y, start.z))
        if previous_face is not None and previous_face == next_face:
            polygon = fn.getPolygonVertices(previous_face)
            centroid = om.MPoint()
            for member in polygon:
                centroid += fn.getPoint(member, om.MSpace.kWorld)
            centroid /= len(polygon)
            rails.append(-1)
            targets.append(-1)
            guide_faces.append(previous_face)
            boundary = _face_ray_boundary(fn, previous_face, vertex, centroid)
            ends.append((boundary.x, boundary.y, boundary.z))
            continue
        face_index = index if index < len(faces) else len(faces) - 1
        rail = _other_face_edge(fn, faces[face_index], vertex, edges[face_index])
        a, b = fn.getEdgeVertices(rail)
        target = b if a == vertex else a
        end = fn.getPoint(target, om.MSpace.kWorld)
        rails.append(rail)
        targets.append(target)
        guide_faces.append(-1)
        ends.append((end.x, end.y, end.z))
        if index > 0:
            prior = _other_face_edge(fn, faces[index - 1], vertex, edges[index - 1])
            if prior != rail:
                raise SlideError("Для вершины найдены разные направляющие скольжения.")
    # A contour around adjacent faces reaches their common rail from both
    # ends. Those two cuts are distinct points on one edge, not an ambiguous
    # crossing. Keep rejecting repeated guides from the same endpoint.
    directed_rails = [(rail, vertex) for rail, vertex in zip(rails, vertices) if rail >= 0]
    if len(set(directed_rails)) != len(directed_rails):
        raise SlideError("Направляющие пересекаются и не дают однозначного разреза.")
    for index, face in enumerate(faces):
        next_index = (index + 1) % len(vertices)
        for guide_index in (index, next_index):
            if rails[guide_index] >= 0:
                if rails[guide_index] not in _polygon_edges(fn, face):
                    raise SlideError("Выбранная грань не соединяет направляющие цепочки.")
            elif guide_faces[guide_index] != face:
                raise SlideError("Внутренняя направляющая лежит в другой грани.")
    return Plan(
        mesh, edges, vertices, tuple(faces), tuple(rails), tuple(targets),
        tuple(guide_faces), tuple(starts), tuple(ends), fn.numVertices, cyclic,
    )


@lru_cache(maxsize=4096)
def _normal_distances(plan: Plan):
    """How far each guide can move normal to its incident selected edges."""
    if plan.children:
        return tuple(distance for child in plan.children for distance in _normal_distances(child))
    results = []
    for index, (start, end) in enumerate(zip(plan.starts, plan.ends)):
        guide = om.MVector(*(b - a for a, b in zip(start, end)))
        distances = []
        adjacent = ((index - 1) % len(plan.edge_ids), index % len(plan.edge_ids)) if plan.cyclic else (
            tuple(i for i in (index - 1, index) if 0 <= i < len(plan.edge_ids)))
        for edge_index in adjacent:
            a = om.MVector(*plan.starts[edge_index])
            next_index = (edge_index + 1) % len(plan.vertices)
            b = om.MVector(*plan.starts[next_index])
            tangent = (b - a).normal()
            distances.append((guide - tangent * (guide * tangent)).length())
        results.append(min(distances))
    return tuple(results)


@lru_cache(maxsize=256)
def _patch_limit(plan: Plan):
    center = om.MVector(*plan.ends[0])
    distances = []
    for index, start in enumerate(plan.starts):
        next_start = plan.starts[(index + 1) % len(plan.starts)]
        a, b = om.MVector(*start), om.MVector(*next_start)
        tangent = (b - a).normal()
        vector = center - a
        distances.append((vector - tangent * (vector * tangent)).length())
    return min(distances)


@lru_cache(maxsize=4096)
def _fraction_limits(plan: Plan, mode: str):
    """Stop opposing cuts before they meet on their shared rail."""
    rails = {}
    for index, rail in enumerate(plan.rails):
        if rail >= 0:
            rails.setdefault(rail, []).append(index)
    opposing = [indices for indices in rails.values() if len(indices) > 1]
    if not opposing:
        return (_MAX_FRACTION,) * len(plan.vertices)
    spans = (_normal_distances(plan) if mode == "uniform" else
             tuple(math.dist(start, end) for start, end in zip(plan.starts, plan.ends))
             if mode == "distance" else (1.0,) * len(plan.vertices))
    face_limits = {}
    for indices in opposing:
        limit = _MAX_FRACTION / sum(1.0 / max(spans[i], _EPS) for i in indices)
        for index in indices:
            adjacent = ((index - 1) % len(plan.edge_ids), index % len(plan.edge_ids)) if plan.cyclic else (
                tuple(i for i in (index - 1, index) if 0 <= i < len(plan.edge_ids)))
            for edge in adjacent:
                face = plan.faces[edge]
                face_limits[face] = min(face_limits.get(face, math.inf), limit)
    results = []
    for index, span in enumerate(spans):
        adjacent = ((index - 1) % len(plan.edge_ids), index % len(plan.edge_ids)) if plan.cyclic else (
            tuple(i for i in (index - 1, index) if 0 <= i < len(plan.edge_ids)))
        limit = min((face_limits.get(plan.faces[edge], math.inf) for edge in adjacent), default=math.inf)
        results.append(min(_MAX_FRACTION, limit / span) if span > _EPS else 0.0)
    return tuple(results)


def _fractions(plan: Plan, amount: float, mode: Literal["relative", "distance", "uniform"]):
    if not math.isfinite(amount) or amount <= 0:
        raise SlideError("Смещение должно быть положительным числом.")
    if plan.children:
        results = [_fractions(child, amount, mode) for child in plan.children]
        return tuple(value for fractions, _ in results for value in fractions), max(actual for _, actual in results)
    lengths = []
    for start, end in zip(plan.starts, plan.ends):
        length = math.dist(start, end)
        if length < _EPS:
            raise SlideError("Найдена направляющая нулевой длины.")
        lengths.append(length)
    fraction_limits = _fraction_limits(plan, mode)
    if mode == "relative":
        fractions = tuple(min(amount, limit) for limit in fraction_limits)
        return fractions, max(fractions)
    if mode in ("distance", "uniform"):
        limits = lengths if mode == "distance" else _normal_distances(plan)
        if not limits or max(limits) < _EPS:
            raise SlideError("Нельзя определить одинаковый отступ для этого направления.")
        # Clamp each guide on its own. A short rail can stop at its endpoint
        # while longer rails in the same selection continue to follow the drag.
        actual = min(amount, max(span * limit for span, limit in zip(limits, fraction_limits)))
        # A uniform amount is measured perpendicular to the selected edge.
        # On a diagonal rail, moving ``amount`` along the rail would produce
        # a much smaller visible width, so divide by its normal span instead.
        fractions = tuple(
            min(amount / span, limit) if span > _EPS else 0.0
            for span, limit in zip(limits, fraction_limits)
        )
        # A closed planar patch is built with one Maya extrusion offset, so it
        # has one geometric limit for the whole inset region.
        if plan.patch_faces and mode == "uniform":
            actual = min(actual, _patch_limit(plan) * _MAX_FRACTION)
            fractions = tuple(actual / length for length in lengths)
        return fractions, actual
    raise SlideError("Режим должен быть relative, distance или uniform.")


def preview_points(plan: Plan, amount: float, mode: Literal["relative", "distance", "uniform"] = "relative"):
    """Return future world-space points without changing the mesh."""
    if plan.children:
        results = [preview_points(child, amount, mode) for child in plan.children]
        return tuple(point for points, _ in results for point in points), max(actual for _, actual in results)
    fractions, actual = _fractions(plan, amount, mode)
    points = []
    corner_limits = None
    for index, (start, end, fraction) in enumerate(zip(plan.starts, plan.ends, fractions)):
        if mode == "uniform" and not plan.patch_faces and plan.guide_faces[index] >= 0:
            adjacent = ((index - 1) % len(plan.edge_ids), index % len(plan.edge_ids)) if plan.cyclic else (
                tuple(i for i in (index - 1, index) if 0 <= i < len(plan.edge_ids)))
            if len(adjacent) == 2 and plan.faces[adjacent[0]] == plan.faces[adjacent[1]]:
                guide = om.MVector(*(b - a for a, b in zip(start, end)))
                normals = []
                for edge_index in adjacent:
                    a = om.MVector(*plan.starts[edge_index])
                    b = om.MVector(*plan.starts[(edge_index + 1) % len(plan.vertices)])
                    tangent = (b - a).normal()
                    perpendicular = guide - tangent * (guide * tangent)
                    normals.append(perpendicular.normal())
                dot = normals[0] * normals[1]
                if dot > -0.95:
                    if corner_limits is None:
                        corner_limits = _normal_distances(plan)
                    local_amount = fraction * corner_limits[index]
                    shift = (normals[0] + normals[1]) * (local_amount / (1.0 + dot))
                    points.append(tuple(om.MVector(*start) + shift)[:3])
                    continue
        points.append(tuple(a + (b - a) * fraction for a, b in zip(start, end)))
    return tuple(points), actual


def _new_vertex_on_rail(fn: om.MFnMesh, start: int, end: int,
                        original_vertex_count: int) -> int:
    """Find the newly inserted vertex by connectivity, independent of IDs."""
    iterator = om.MItMeshVertex(fn.dagPath())
    iterator.setIndex(start)
    near_start = set(iterator.getConnectedVertices())
    iterator.setIndex(end)
    near_end = set(iterator.getConnectedVertices())
    # A triangle's third vertex also neighbors both rail endpoints, even when
    # that triangle is on the opposite side of the cut. polySplit preserves
    # existing vertex IDs and appends its insertions; only these are candidates.
    candidates = {vertex for vertex in near_start & near_end
                  if vertex >= original_vertex_count}
    if len(candidates) != 1:
        raise SlideError("Не удалось определить новую вершину после разреза.")
    return next(iter(candidates))


def _insert_points(plan: Plan, fractions: tuple[float, ...]):
    fn = _mesh_fn(plan.mesh)
    insert_points = []
    inverse = fn.dagPath().inclusiveMatrixInverse()
    for index, (edge_id, vertex, fraction) in enumerate(zip(plan.rails, plan.vertices, fractions)):
        if edge_id >= 0:
            a, b = fn.getEdgeVertices(edge_id)
            insert_points.append((edge_id, fraction if a == vertex else 1.0 - fraction))
        else:
            start, end = plan.starts[index], plan.ends[index]
            world = om.MPoint(*(a + (b - a) * fraction for a, b in zip(start, end)))
            local = world * inverse
            insert_points.append((plan.guide_faces[index], local.x, local.y, local.z))
    if plan.cyclic:
        if plan.rails[0] < 0:
            first_edge = next(i for i, rail in enumerate(plan.rails) if rail >= 0)
            insert_points = insert_points[first_edge:] + insert_points[:first_edge]
        insert_points.append(insert_points[0])
    return insert_points


def _face_inset(plan: Plan) -> bool:
    return bool(plan.patch_faces)


def _select_connected_chain(plan: Plan, fn: om.MFnMesh, new_vertices: tuple[int, ...]):
    edge_map = {frozenset(fn.getEdgeVertices(e)): e for e in range(fn.numEdges)}
    new_edges = []
    for index in range(len(plan.edge_ids)):
        next_index = (index + 1) % len(new_vertices)
        edge_id = edge_map.get(frozenset((new_vertices[index], new_vertices[next_index])))
        if edge_id is None:
            raise SlideError("Новые рёбра не образовали ожидаемую цепочку.")
        new_edges.append(edge_id)
    cmds.select([f"{plan.mesh}.e[{edge}]" for edge in new_edges], replace=True)
    return tuple(new_edges), new_vertices


def _select_extruded_edges(plan: Plan):
    """Resolve inset vertices from the quads connected to the original rim.

    Extrude may reuse an old interior vertex ID for a new rim vertex and move
    the interior vertex to the end of the array. Vertex counts cannot identify
    the new contour on a triangulated cap. Resolve it at zero offset instead.
    """
    fn = _mesh_fn(plan.mesh)
    vertex_iterator = om.MItMeshVertex(fn.dagPath())
    edge_iterator = om.MItMeshEdge(fn.dagPath())
    polygon = om.MItMeshPolygon(fn.dagPath())
    mapping = {}
    for index, first in enumerate(plan.vertices):
        following = (index + 1) % len(plan.vertices)
        second = plan.vertices[following]
        vertex_iterator.setIndex(first)
        edge = next((candidate for candidate in vertex_iterator.getConnectedEdges()
                     if second in fn.getEdgeVertices(candidate)), None)
        if edge is None:
            raise SlideError("Extrude не сохранил исходное ребро контура.")
        # Extrude can bake the world transform into float vertex coordinates.
        # Compare both ends on the resulting mesh, rather than against the
        # pre-operation snapshot, which retains different rounding.
        starts = tuple(tuple(fn.getPoint(vertex, om.MSpace.kWorld))[:3]
                       for vertex in (first, second))
        edge_iterator.setIndex(edge)
        choices = []
        for face in edge_iterator.getConnectedFaces():
            polygon.setIndex(face)
            vertices = tuple(polygon.getVertices())
            if len(vertices) != 4:
                continue
            new_pair = []
            for source in (first, second):
                position = vertices.index(source)
                opposite = second if source == first else first
                candidates = [vertices[(position + shift) % 4] for shift in (-1, 1)
                              if vertices[(position + shift) % 4] != opposite]
                if len(candidates) != 1:
                    break
                new_pair.append(candidates[0])
            if len(new_pair) != 2:
                continue
            error = sum(math.dist(start, tuple(fn.getPoint(vertex, om.MSpace.kWorld))[:3])
                        for start, vertex in zip(starts, new_pair))
            choices.append((error, -face, tuple(new_pair)))
        if not choices:
            raise SlideError("Не найдена полоса граней нового контура после Extrude.")
        error, _, new_pair = min(choices)
        tolerance = _mesh_position_tolerance(fn, starts, math.dist(*starts))
        if error > 2 * tolerance:
            raise SlideError("Новый контур Extrude не совпадает с исходным при нулевом смещении.")
        for source, inserted in zip((first, second), new_pair):
            if source in mapping and mapping[source] != inserted:
                raise SlideError("Extrude не создал согласованный контур.")
            mapping[source] = inserted
    return _select_connected_chain(plan, fn, tuple(mapping[vertex] for vertex in plan.vertices))


def _select_new_edges(plan: Plan, fractions: tuple[float, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    fn = _mesh_fn(plan.mesh)
    new_vertices: list[int | None] = []
    unused = set(range(plan.original_vertex_count, fn.numVertices))
    rail_counts = {}
    for rail in plan.rails:
        rail_counts[rail] = rail_counts.get(rail, 0) + 1
    for index, (a, b) in enumerate(zip(plan.vertices, plan.targets)):
        if b >= 0 and rail_counts[plan.rails[index]] == 1:
            vertex = _new_vertex_on_rail(fn, a, b, plan.original_vertex_count)
        else:
            vertex = None
        if vertex is not None:
            new_vertices.append(vertex)
            unused.discard(vertex)
        else:
            new_vertices.append(None)
    for index, vertex in enumerate(new_vertices):
        if vertex is None:
            start, end = plan.starts[index], plan.ends[index]
            desired = tuple(a + (b - a) * fractions[index] for a, b in zip(start, end))
            if not unused:
                raise SlideError("Не удалось определить вершину внутри грани.")
            closest = min(unused, key=lambda v: math.dist(
                desired,
                tuple(fn.getPoint(v, om.MSpace.kWorld)[axis] for axis in range(3)),
            ))
            new_vertices[index] = closest
            unused.remove(closest)
    new_vertices = tuple(new_vertices)
    return _select_connected_chain(plan, fn, new_vertices)


def _connect_face_corners(plan: Plan, new_vertices: tuple[int, ...]) -> None:
    """Close the wedge left where a selected chain turns within one face."""
    if not any(face >= 0 for face in plan.guide_faces):
        return
    for source, inserted, face in zip(plan.vertices, new_vertices, plan.guide_faces):
        if face < 0:
            continue
        cmds.polyConnectComponents(
            f"{plan.mesh}.vtx[{source}]", f"{plan.mesh}.vtx[{inserted}]",
            constructionHistory=True,
        )
    # polyConnectComponents can renumber edges, so resolve the clone chain again.
    _select_connected_chain(plan, _mesh_fn(plan.mesh), new_vertices)


def _plan_vertex_pairs(plan):
    return tuple((plan.vertices[index], plan.vertices[(index + 1) % len(plan.vertices)])
                 for index in range(len(plan.edge_ids)))


def _select_vertex_pairs(mesh, pairs):
    fn = _mesh_fn(mesh)
    edges = {frozenset(fn.getEdgeVertices(edge)): edge for edge in range(fn.numEdges)}
    try:
        ids = [edges[frozenset(pair)] for pair in pairs]
    except KeyError:
        raise SlideError("Не удалось найти контур после изменения соседнего участка.") from None
    cmds.select([f"{mesh}.e[{edge}]" for edge in ids], replace=True)


def _refresh_plan_source(plan):
    """Resolve edge and face indices after another independent contour was cut."""
    from .multiple import _matching_clone_side
    _select_vertex_pairs(plan.mesh, _plan_vertex_pairs(plan))
    return analyze_selection(_matching_clone_side(plan))


def _apply_children(plan, amount, clone, mode, undo):
    _fractions(plan, amount, mode)
    if undo:
        cmds.undoInfo(openChunk=True, chunkName="Clone Slide" if clone else "Slide Edges")
    modified = False
    try:
        pairs = []
        actuals = []
        for child in plan.children:
            _select_vertex_pairs(child.mesh, _plan_vertex_pairs(child))
            from .multiple import _matching_clone_side
            side = _matching_clone_side(child)
            modified = True
            actuals.append(apply(amount, side=side, clone=clone, mode=mode, _undo=False))
            fn = _mesh_fn(plan.mesh)
            _, ids = _selected_edges()
            pairs.extend(tuple(fn.getEdgeVertices(edge)) for edge in ids)
        _select_vertex_pairs(plan.mesh, pairs)
    except Exception:
        if undo:
            cmds.undoInfo(closeChunk=True)
            if modified:
                cmds.undo()
        raise
    else:
        if undo:
            cmds.undoInfo(closeChunk=True)
    return max(actuals)


def apply(
    amount: float,
    *,
    side: int = 0,
    clone: bool = False,
    mode: Literal["relative", "distance", "uniform"] = "relative",
    _undo: bool = True,
) -> float:
    """Perform one undoable operation and return the applied amount.

    Selection may contain separate edge chains on one mesh. ``side`` is stable for a
    given topology, but its visual direction depends on edge and face indexing.
    """
    plan = analyze_selection(side)
    if plan.children:
        return _apply_children(plan, amount, clone, mode, _undo)
    fractions, actual = _fractions(plan, amount, mode)
    points = None if clone else preview_points(plan, amount, mode)[0]
    inset = clone and _face_inset(plan)
    seed = (0.25,) * len(plan.vertices) if mode == "uniform" else fractions
    insert_points = _insert_points(plan, seed) if clone and not inset else None
    if _undo:
        cmds.undoInfo(openChunk=True, chunkName="Clone Slide" if clone else "Slide Edges")
    modified = False
    try:
        if clone:
            if inset:
                node = cmds.polyExtrudeFacet(
                    [f"{plan.mesh}.f[{face}]" for face in plan.patch_faces],
                    keepFacesTogether=True, constructionHistory=True,
                    offset=0,
                )[0]
                modified = True
                _, new_vertices = _select_extruded_edges(plan)
                if mode == "uniform":
                    cmds.setAttr(node + ".offset", actual)
                else:
                    for vertex, point in zip(new_vertices, preview_points(plan, amount, mode)[0]):
                        cmds.xform(f"{plan.mesh}.vtx[{vertex}]", worldSpace=True, translation=point)
            else:
                cmds.polySplit(plan.mesh, insertpoint=insert_points, constructionHistory=True)
                modified = True
                _, new_vertices = _select_new_edges(plan, seed)
                _connect_face_corners(plan, new_vertices)
                if mode == "uniform":
                    for vertex, point in zip(new_vertices, preview_points(plan, amount, mode)[0]):
                        cmds.xform(f"{plan.mesh}.vtx[{vertex}]", worldSpace=True, translation=point)
        else:
            for vertex, point in zip(plan.vertices, points):
                cmds.xform(f"{plan.mesh}.vtx[{vertex}]", worldSpace=True, translation=point)
                modified = True
            cmds.select([f"{plan.mesh}.e[{edge}]" for edge in plan.edge_ids], replace=True)
    except Exception:
        if _undo:
            cmds.undoInfo(closeChunk=True)
            if modified:
                cmds.undo()
        raise
    else:
        if _undo:
            cmds.undoInfo(closeChunk=True)
    return actual


class LiveEdit:
    """Update an edge cut across a gesture, optionally on a disposable preview mesh."""

    def __init__(self, plan: Plan, *, clone: bool,
                 mode: Literal["relative", "distance", "uniform"], preview: bool = False,
                 _undo: bool = True):
        self.plan = plan
        self.clone = clone
        self.mode = mode
        self.preview = preview
        self._undo = _undo
        self._child_edits = None
        self._open = False
        self._modified = False
        self._new_vertices: tuple[int, ...] | None = None
        self._extrude_node: str | None = None
        self.actual: float | None = None

    def update(self, amount: float) -> float:
        points, actual = preview_points(self.plan, amount, self.mode)
        if not self._open:
            if not self.preview and self._undo:
                cmds.undoInfo(openChunk=True, chunkName="Clone Slide" if self.clone else "Slide Edges")
            self._open = True
        try:
            if self.plan.children:
                building = self._child_edits is None
                if building:
                    self._child_edits = []
                    for child in self.plan.children:
                        child = replace(child, mesh=self.plan.mesh)
                        if self._child_edits:
                            child = _refresh_plan_source(child)
                        live = LiveEdit(child, clone=self.clone, mode=self.mode,
                                        preview=self.preview, _undo=False)
                        self._child_edits.append(live)
                        self._modified = True
                        live.update(amount)
                else:
                    for child, live in zip(self.plan.children, self._child_edits):
                        guides = dict(zip(child.vertices, zip(child.starts, child.ends)))
                        live.plan = replace(live.plan,
                                            starts=tuple(guides[v][0] for v in live.plan.vertices),
                                            ends=tuple(guides[v][1] for v in live.plan.vertices))
                        live.update(amount)
                self._new_vertices = tuple(v for live in self._child_edits
                                           for v in live._new_vertices) if self.clone else None
                pairs = []
                for live in self._child_edits:
                    vertices = live._new_vertices if self.clone else live.plan.vertices
                    pairs.extend((vertices[i], vertices[(i + 1) % len(vertices)])
                                 for i in range(len(live.plan.edge_ids)))
                if building:
                    _select_vertex_pairs(self.plan.mesh, pairs)
                self.actual = max(live.actual for live in self._child_edits)
                return self.actual
            if self.clone and self._new_vertices is None:
                # Insert at a safe position before moving the new vertices to
                # the cursor value. A tiny first drag can otherwise snap the
                # polySplit points back onto the original vertices.
                if _face_inset(self.plan):
                    self._extrude_node = cmds.polyExtrudeFacet(
                        [f"{self.plan.mesh}.f[{face}]" for face in self.plan.patch_faces],
                        keepFacesTogether=True, constructionHistory=True, offset=0,
                    )[0]
                    self._modified = True
                    _, self._new_vertices = _select_extruded_edges(self.plan)
                else:
                    seed = (0.25,) * len(self.plan.vertices)
                    cmds.polySplit(
                        self.plan.mesh,
                        insertpoint=_insert_points(self.plan, seed),
                        constructionHistory=True,
                    )
                    self._modified = True
                    _, self._new_vertices = _select_new_edges(self.plan, seed)
                    _connect_face_corners(self.plan, self._new_vertices)
            if self._extrude_node and self.mode == "uniform":
                cmds.setAttr(self._extrude_node + ".offset", actual)
                self._modified = True
            else:
                vertices = self._new_vertices if self.clone else self.plan.vertices
                if self.preview:
                    fn = _mesh_fn(self.plan.mesh)
                    positions = fn.getPoints(om.MSpace.kWorld)
                    for vertex, point in zip(vertices, points):
                        positions[vertex] = om.MPoint(*point)
                    fn.setPoints(positions, om.MSpace.kWorld)
                    fn.updateSurface()
                else:
                    for vertex, point in zip(vertices, points):
                        cmds.xform(f"{self.plan.mesh}.vtx[{vertex}]", worldSpace=True, translation=point)
                self._modified = True
            self.actual = actual
            return actual
        except Exception:
            self.cancel()
            raise

    def finish(self) -> float | None:
        if self._open:
            if self._child_edits:
                for live in self._child_edits:
                    live.finish()
            if not self.preview and self._undo:
                cmds.undoInfo(closeChunk=True)
            self._open = False
        return self.actual

    def cancel(self) -> None:
        rollback_failed = False
        if self._open:
            if not self.preview and self._undo:
                cmds.undoInfo(closeChunk=True)
            self._open = False
            if self._modified and not self.preview and self._undo:
                cmds.undo()
                rollback_failed = (self.clone and
                                   _mesh_fn(self.plan.mesh).numVertices != self.plan.original_vertex_count)
        if self._child_edits:
            for live in self._child_edits:
                live.cancel()
        self._child_edits = None
        self._modified = False
        self._new_vertices = None
        self._extrude_node = None
        self.actual = None
        if rollback_failed:
            raise SlideError("Не удалось отменить предыдущий контур. Выполните Undo перед новым жестом.")
