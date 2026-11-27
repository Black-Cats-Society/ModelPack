"""Two-sided edge cloning, including selections that branch at a vertex.

Each selected edge is offset into every adjacent face. The original edges stay
in place; face-local cuts meet at shared rail vertices.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

import maya.cmds as cmds
from maya.api import OpenMaya as om

from .core import (Plan, SlideError, _EPS, _MAX_FRACTION, _edge_faces,
                   _face_ray_boundary, _mesh_fn, _normal_distances, _patch_limit,
                   _polygon_edges, _selected_edges, _select_extruded_edges,
                   _mesh_position_tolerance, _across_collapsed_strips,
                   preview_points)


@dataclass(frozen=True)
class BevelPlan:
    mesh: str
    edge_ids: tuple[int, ...]
    edge_vertices: tuple[tuple[int, int], ...]
    runs: tuple[Plan, ...]
    original_vertex_count: int
    planar_fans: tuple = ()
    transition_vertices: tuple[int, ...] = ()
    face_triangles: dict | None = None
    anchor_ends: bool = True
    has_ngon_turn: bool = False
    has_folded_junction: bool = False


def _use_native_bevel(plan, count):
    return plan.has_ngon_turn or (count > 1 and plan.has_folded_junction)


def _box_axes(plan):
    """Recognize a complete rectangular box selection in world space."""
    fn = _mesh_fn(plan.mesh)
    if (fn.numVertices, fn.numEdges, fn.numPolygons, len(plan.edge_ids)) != (8, 12, 6, 12):
        return ()
    if any(len(fn.getPolygonVertices(face)) != 4 for face in range(6)):
        return ()
    points = fn.getPoints(om.MSpace.kWorld)
    origin = om.MPoint(points[0])
    vectors = [points[b if a == 0 else a] - origin
               for a, b in plan.edge_vertices if 0 in (a, b)]
    if len(vectors) != 3 or any(vector.length() <= _EPS for vector in vectors):
        return ()
    directions = [vector.normal() for vector in vectors]
    if any(abs(directions[i] * directions[j]) > 1e-6 for i, j in ((0, 1), (0, 2), (1, 2))):
        return ()
    first, second, third = directions
    second = (second - first * (first * second)).normal()
    normal = first ^ second
    axes = (first, second, normal if normal * third > 0. else -normal)
    tolerance = _mesh_position_tolerance(fn, points)
    corners = set()
    for point in points:
        distances = [(point - origin) * axis for axis in axes]
        bits = tuple(int(distance > vector.length() * .5)
                     for distance, vector in zip(distances, vectors))
        expected = origin
        for bit, vector in zip(bits, vectors):
            expected = expected + vector * bit
        if (point - expected).length() > tolerance:
            return ()
        corners.add(bits)
    if len(corners) != 8:
        return ()
    return tuple((origin, axis, vector.length()) for axis, vector in zip(axes, vectors))


class _BoxSupports:
    """Cut parallel support planes through the box, including its corner cells."""

    def __init__(self, mesh, axes, mode, fractions):
        self.mesh = mesh
        self.axes = axes
        self.mode = mode
        self.fractions = fractions
        self.seed_width = None
        self.seed_points = None
        self.new_edges = ()
        self.spacing = 0.

    def update(self, amount):
        if not math.isfinite(amount) or amount <= 0:
            raise SlideError("Смещение должно быть положительным числом.")
        lengths = [length for _, _, length in self.axes]
        limits = [length * .49 for length in lengths] if self.mode == "uniform" else [.49] * 3
        spacings = [min(self.spacing, limit * .999) for limit in limits]
        widths = [min(amount, limit - spacing) for limit, spacing in zip(limits, spacings)]
        if self.seed_points is None:
            self._build((min(lengths) if self.mode == "uniform" else 1.) * .25)
        path = _mesh_fn(self.mesh).dagPath()
        inverse = path.inclusiveMatrixInverse()
        shape = path.fullPathName()
        steps = [spacing / max(1, len(self.fractions) - 1) for spacing in spacings]
        for vertex, (seed, columns) in enumerate(zip(self.seed_points, self.columns)):
            target = self.original_corners.get(vertex)
            if target is None:
                target = om.MPoint(self.axes[0][0])
                for (far_side, layer), (_, axis, length), width, step in zip(columns, self.axes, widths, steps):
                    scale = length if self.mode == "relative" else 1.
                    distance = (width + step * layer) * scale if layer >= 0 else 0.
                    target += axis * (length - distance if far_side else distance)
            offset = target - seed
            if offset.length() > self.tolerance or vertex in self.tweaked:
                local = offset * inverse
                cmds.setAttr(f"{shape}.pnts[{vertex}]", local.x, local.y, local.z, type="float3")
                self.tweaked.add(vertex)
        cmds.select([f"{self.mesh}.e[{edge}]" for edge in self.new_edges], replace=True)
        return max(widths)

    def _build(self, seed_width):
        original = _mesh_fn(self.mesh).getPoints(om.MSpace.kWorld)
        cmds.select(self.mesh, replace=True)
        for origin, axis, length in self.axes:
            rotation = om.MVector.kZaxisVector.rotateTo(axis).asEulerRotation()
            angles = tuple(om.MAngle(value).asUnits(om.MAngle.uiUnit()) for value in rotation)
            width = seed_width if self.mode == "uniform" else seed_width * length
            for fraction in self.fractions:
                for distance in (width * fraction, length - width * fraction):
                    center = origin + axis * distance
                    center = tuple(om.MDistance(value).asUnits(om.MDistance.uiUnit())
                                   for value in (center.x, center.y, center.z))
                    cmds.polyCut(self.mesh, cutPlaneCenter=center, cutPlaneRotate=angles,
                                 constructionHistory=True)
        fn = _mesh_fn(self.mesh)
        points = fn.getPoints(om.MSpace.kWorld)
        self.tolerance = _mesh_position_tolerance(fn, points) * .01
        coordinates = []
        for _, _, length in self.axes:
            width = seed_width if self.mode == "uniform" else seed_width * length
            distances = (0., *(width * fraction for fraction in reversed(self.fractions)))
            coordinates.append(tuple((side, layer - 1, length - distance if side else distance)
                                     for side in (0, 1) for layer, distance in enumerate(distances)))
        self.columns = [tuple(min(levels, key=lambda level: abs(level[2] - (point - origin) * axis))[:2]
                              for levels, (origin, axis, _) in zip(coordinates, self.axes))
                        for point in points]
        boundaries = [{index for index, (_, layer) in enumerate(columns) if layer < 0}
                      for columns in self.columns]
        self.original_corners = {
            vertex: om.MPoint(min(original, key=lambda corner: (corner - points[vertex]).length()))
            for vertex, sides in enumerate(boundaries) if len(sides) == 3
        }
        self.new_edges = tuple(edge for edge in range(fn.numEdges)
                               if len(set.intersection(*(boundaries[v] for v in fn.getEdgeVertices(edge)))) == 1)
        self.seed_width = seed_width
        self.seed_points = points
        self.tweaked = set()


def _prepare_planar_fans(plan):
    """Cut a flat triangle fan as sectors bounded by the selected spokes.

    The radial spokes are restored on each side's inset vertex after cutting.
    The selected spokes remain intact through ordinary and branching poles.
    """
    fn = _mesh_fn(plan.mesh)
    selected_neighbors = {}
    for a, b in plan.edge_vertices:
        selected_neighbors.setdefault(a, []).append(b)
        selected_neighbors.setdefault(b, []).append(a)
    iterator = om.MItMeshVertex(fn.dagPath())
    remove = set()
    fans = []
    points = fn.getPoints(om.MSpace.kWorld)
    for source, selected in selected_neighbors.items():
        if len(selected) < 2:
            continue
        if len(selected) == 2:
            first, second = (points[neighbor] - points[source] for neighbor in selected)
            if first.length() < _EPS or second.length() < _EPS:
                continue
            if first.normal() * second.normal() > -0.999999:
                continue
        iterator.setIndex(source)
        faces = tuple(iterator.getConnectedFaces())
        neighbors = tuple(iterator.getConnectedVertices())
        if len(faces) < 4 or len(faces) != len(neighbors):
            continue
        if any(len(fn.getPolygonVertices(face)) != 3 for face in faces):
            continue
        normal = fn.getPolygonNormal(faces[0], om.MSpace.kWorld)
        if any(normal * fn.getPolygonNormal(face, om.MSpace.kWorld) < 0.999999
               for face in faces):
            continue
        spokes = tuple(neighbor for neighbor in neighbors if neighbor not in selected)
        fans.append((source, spokes))
        remove.update(edge for edge in iterator.getConnectedEdges()
                      if edge not in plan.edge_ids)
    if not remove:
        return plan
    cmds.polyDelEdge([f"{plan.mesh}.e[{edge}]" for edge in sorted(remove)],
                     cleanVertices=False, constructionHistory=True)
    fn = _mesh_fn(plan.mesh)
    edge_map = {frozenset(fn.getEdgeVertices(edge)): edge for edge in range(fn.numEdges)}
    cmds.select([f"{plan.mesh}.e[{edge_map[frozenset(pair)]}]"
                 for pair in plan.edge_vertices], replace=True)
    prepared = analyze_bevel_selection(anchor_ends=plan.anchor_ends)
    return replace(prepared, planar_fans=tuple(fans), transition_vertices=plan.transition_vertices)


def _restore_planar_fans(plan, run_vertices):
    fn = _mesh_fn(plan.mesh)
    iterator = om.MItMeshVertex(fn.dagPath())
    existing = {frozenset(fn.getEdgeVertices(edge)) for edge in range(fn.numEdges)}
    for source, spokes in plan.planar_fans:
        for run, vertices in zip(plan.runs, run_vertices):
            if source not in run.vertices:
                continue
            pole = vertices[run.vertices.index(source)]
            replacements = dict(zip(run.vertices, vertices))
            iterator.setIndex(pole)
            neighbors = {vertex for face in iterator.getConnectedFaces()
                         for vertex in fn.getPolygonVertices(face)}
            for spoke in spokes:
                # Spokes on a selected outer contour end on its inset contour.
                # Other spokes keep their original boundary endpoint.
                target = replacements.get(spoke, spoke)
                pair = frozenset((pole, target))
                if target in neighbors and target != pole and pair not in existing:
                    cmds.polyConnectComponents(f"{plan.mesh}.vtx[{pole}]",
                                               f"{plan.mesh}.vtx[{target}]",
                                               constructionHistory=True)
                    existing.add(pair)


def _face_runs(mesh: str, fn: om.MFnMesh, face: int,
               selected: set[int], selected_degree, anchor_ends) -> list[Plan]:
    polygon = om.MItMeshPolygon(fn.dagPath())
    polygon.setIndex(face)
    vertices = tuple(polygon.getVertices())
    edges = tuple(polygon.getEdges())
    count = len(edges)
    flags = [edge in selected for edge in edges]
    selected_count = sum(flags)
    if not any(flags):
        return []
    if all(flags):
        center = om.MPoint()
        for vertex in vertices:
            center += fn.getPoint(vertex, om.MSpace.kWorld)
        center /= count
        starts = tuple(tuple(fn.getPoint(vertex, om.MSpace.kWorld))[:3]
                       for vertex in vertices)
        run = Plan(mesh, edges, vertices, (face,) * count,
                   (-1,) * count, (-1,) * count, (face,) * count,
                   starts, ((center.x, center.y, center.z),) * count,
                   fn.numVertices, True, (face,))
        return [run]
    result = []
    face_points=[fn.getPoint(v,om.MSpace.kWorld) for v in vertices]
    face_normal=om.MVector()
    for i,p in enumerate(face_points):face_normal+=(p-face_points[0])^(face_points[(i+1)%count]-face_points[0])
    face_normal.normalize()
    for first in range(count):
        if not flags[first] or flags[(first - 1) % count]:
            continue
        indices = [first]
        while flags[(indices[-1] + 1) % count]:
            indices.append((indices[-1] + 1) % count)
        run_edges = tuple(edges[index] for index in indices)
        run_vertices = tuple(vertices[(first + offset) % count]
                             for offset in range(len(indices) + 1))
        rails = []
        targets = []
        guide_faces = []
        starts = []
        ends = []
        center = om.MPoint()
        for vertex in vertices:
            center += fn.getPoint(vertex, om.MSpace.kWorld)
        center /= count
        for offset, vertex in enumerate(run_vertices):
            start = fn.getPoint(vertex, om.MSpace.kWorld)
            starts.append((start.x, start.y, start.z))
            if 0 < offset < len(indices):
                previous=(face_points[(first+offset)%count]-face_points[(first+offset-1)%count]).normal()
                following=(face_points[(first+offset+1)%count]-face_points[(first+offset)%count]).normal()
                direction=(face_normal^previous)+(face_normal^following)
                if direction.length()<_EPS:raise SlideError('Вырожденный угол выбранной грани.')
                boundary = _face_ray_boundary(fn, face, vertex, start+direction)
                rails.append(-1)
                targets.append(-1)
                guide_faces.append(face)
                ends.append((boundary.x, boundary.y, boundary.z))
            else:
                rail_index = (first - 1) % count if offset == 0 else (indices[-1] + 1) % count
                rail = edges[rail_index]
                a, b = fn.getEdgeVertices(rail)
                target = b if a == vertex else a
                end = fn.getPoint(target, om.MSpace.kWorld)
                edge_index=indices[0] if offset==0 else indices[-1]
                first_point=face_points[edge_index];last_point=face_points[(edge_index+1)%count]
                inward=face_normal^(last_point-first_point).normal()
                rail_direction = end - start
                edge_direction = (last_point - first_point).normal()
                toward_edge = edge_direction if offset == 0 else -edge_direction
                # At a true end, an obtuse neighboring edge sends the cut
                # sideways. Start at the vertex and offset inside this face.
                vertex_end = (anchor_ends and selected_count == 1 and selected_degree[vertex] == 1
                              and rail_direction * toward_edge < -.02 * rail_direction.length())
                if rail_direction * inward <= _EPS or vertex_end:
                    boundary=_face_ray_boundary(fn,face,vertex,start+inward)
                    rails.append(-1);targets.append(-1);guide_faces.append(face)
                    ends.append(tuple(boundary)[:3])
                else:
                    rails.append(rail)
                    targets.append(target)
                    guide_faces.append(-1)
                    ends.append((end.x, end.y, end.z))
        result.append(Plan(
            mesh, run_edges, run_vertices, (face,) * len(run_edges),
            tuple(rails), tuple(targets), tuple(guide_faces),
            tuple(starts), tuple(ends), fn.numVertices, False,
        ))
    return result


def analyze_bevel_selection(transition_vertices=None, *, anchor_ends=True) -> BevelPlan:
    mesh, edges = _selected_edges()
    fn = _mesh_fn(mesh)
    edge_faces = _edge_faces(fn)
    for edge in edges:
        if len(edge_faces[edge]) > 2:
            raise SlideError("Non-manifold ребро не поддерживается.")
    edges, collapsed = _across_collapsed_strips(fn, edges, edge_faces)
    selected = set(edges)
    pairs = tuple(tuple(fn.getEdgeVertices(edge)) for edge in edges)
    degree = {}
    for a, b in pairs:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    runs = []
    triangles = []
    for face in range(fn.numPolygons):
        face_edges = selected.intersection(_polygon_edges(fn, face))
        if face not in collapsed and face_edges:
            runs.extend(_face_runs(mesh, fn, face, selected, degree, anchor_ends))
            if transition_vertices is None and len(face_edges) >= 2:
                polygon = tuple(fn.getPolygonVertices(face))
                if len(polygon) == 3 and any(
                    fn.getPolygonNormal(face, om.MSpace.kWorld) *
                    fn.getPolygonNormal(neighbor, om.MSpace.kWorld) < .999
                    for edge in face_edges for neighbor in edge_faces[edge] if neighbor != face
                ):
                    triangles.append(polygon)
    if not runs:
        raise SlideError("У выбранных рёбер не найдены соседние грани.")
    folded_junction = False
    if transition_vertices is None:
        transitions = {v for triangle in triangles for v in triangle if degree[v] >= 3}
        vertex = om.MItMeshVertex(fn.dagPath())
        for source, count in degree.items():
            if count not in (2, 3, 4):
                continue
            vertex.setIndex(source)
            incident = vertex.getConnectedEdges()
            faces = vertex.getConnectedFaces()
            if count >= 3 and len(incident) == len(faces) == 4:
                normals = [fn.getPolygonNormal(face, om.MSpace.kWorld) for face in faces]
                # A subdivision can add a fourth edge to a folded corner.
                # Face-by-face offsets leave the two rows fanning into its
                # source tip. The native multi-row grid crosses the fold.
                angles = [first * second for i, first in enumerate(normals)
                          for second in normals[i + 1:]]
                folded_junction |= max(angles) > .9999 and min(angles) < .99
            if len(incident) == 3 and len(faces) == 3 and all(
                len(edge_faces[edge]) == 2 for edge in incident
            ):
                normals = [fn.getPolygonNormal(face, om.MSpace.kWorld) for face in faces]
                # Two-edge turns use separate rows at a square corner, as
                # Maya's non-chamfered bevel does. A sloped third rail can
                # carry the shared support from both sides of the turn.
                face_pairs = ((0, 1), (0, 2), (1, 2))
                square = all(abs(normals[i] * normals[j]) < 1e-5 for i, j in face_pairs)
                if (count == 3 and all(normals[i] * normals[j] < .999 for i, j in face_pairs)
                        or count == 2 and square
                        and all(len(fn.getPolygonVertices(face)) == 4 for face in faces)):
                    transitions.add(source)
                continue
            if count != 3 or len(incident) != 3 or len(faces) != 2:
                continue
            if sum(len(edge_faces[edge]) == 1 for edge in incident) != 2:
                continue
            first, second = (fn.getPolygonNormal(face, om.MSpace.kWorld) for face in faces)
            if first * second < .999:
                # At an open folded corner, carry the inset across the crease
                # from a cut on it, rather than fanning both ends into its tip.
                transitions.add(source)
        transition_vertices = tuple(sorted(transitions))
    runs.sort(key=lambda run: bool(run.patch_faces))
    guide_faces = {face for run in runs for face in (*run.patch_faces, *run.guide_faces) if face >= 0}
    polygon = om.MItMeshPolygon(fn.dagPath())
    surfaces = {}
    for face in guide_faces:
        polygon.setIndex(face)
        _, ids = polygon.getTriangles(om.MSpace.kObject)
        surfaces[face] = tuple(tuple(ids[i:i+3]) for i in range(0, len(ids), 3))
    return BevelPlan(mesh, edges, pairs, tuple(runs), fn.numVertices,
                     transition_vertices=transition_vertices, face_triangles=surfaces,
                     anchor_ends=anchor_ends, has_folded_junction=folded_junction,
                     has_ngon_turn=any(len(run.edge_ids) > 1
                                       and len(fn.getPolygonVertices(run.faces[0])) > 4
                                       for run in runs))


def _actual_amount(plan: BevelPlan, amount: float, mode: str) -> float:
    if not math.isfinite(amount) or amount <= 0:
        raise SlideError("Смещение должно быть положительным числом.")
    face_run_counts = {}
    for run in plan.runs:
        face_run_counts[run.faces[0]] = face_run_counts.get(run.faces[0], 0) + 1
    if mode == "relative":
        run_limits = [0.5 if face_run_counts[run.faces[0]] > 1 else _MAX_FRACTION
                      for run in plan.runs]
        return min(amount, max(run_limits))
    if mode == "uniform":
        run_limits = []
        for run in plan.runs:
            if run.patch_faces:
                run_limits.append(_patch_limit(run) * _MAX_FRACTION)
            else:
                widths = _normal_distances(run)
                run_limits.append(max(widths))
        if not run_limits or max(run_limits) < _EPS:
            raise SlideError("Не удалось определить ширину для выбранных рёбер.")
        return min(amount, max(run_limits))
    raise SlideError("Режим должен быть relative или uniform.")


def _segment_insert(fn: om.MFnMesh, start: om.MVector,
                    end: om.MVector, fraction: float, source_vertex, target_vertex):
    direction = end - start
    squared = direction * direction
    tolerance = _mesh_position_tolerance(fn, (start, end), math.sqrt(squared))
    # Position tolerance can be wider than a short rail. Do not snap a
    # quarter-rail cut to an original endpoint on that basis.
    parameter_tolerance = min(tolerance / math.sqrt(squared), min(fraction, 1.0 - fraction) * 0.5)
    # Follow the subdivided rail between its original endpoints. A nearby
    # branch can be within the same position tolerance without being this rail.
    iterator = om.MItMeshVertex(fn.dagPath())
    pending = [source_vertex]
    parents = {source_vertex: None}
    parameters = {source_vertex: 0.0, target_vertex: 1.0}
    for vertex in pending:
        if vertex == target_vertex:
            break
        iterator.setIndex(vertex)
        for edge in iterator.getConnectedEdges():
            a, b = fn.getEdgeVertices(edge)
            following = b if a == vertex else a
            if following in parents:
                continue
            offset = om.MVector(fn.getPoint(following, om.MSpace.kWorld)) - start
            along = (offset * direction) / squared
            if not parameters[vertex] + _EPS < along <= 1.0 + parameter_tolerance:
                continue
            if (offset - direction * along).length() > tolerance:
                continue
            parameters[following] = along
            parents[following] = (vertex, edge)
            pending.append(following)
    if target_vertex not in parents:
        raise SlideError("После предыдущего разреза не найдена боковая направляющая.")

    candidates = []
    vertex = target_vertex
    while vertex != source_vertex:
        previous, edge = parents[vertex]
        a, b = fn.getEdgeVertices(edge)
        ta, tb = parameters[a], parameters[b]
        vertex = previous
        if (min(ta, tb) - parameter_tolerance <= fraction <=
                max(ta, tb) + parameter_tolerance and abs(tb - ta) > _EPS):
            local_fraction = (fraction - ta) / (tb - ta)
            # Reuse a cut already placed on this rail. Floating point rounding
            # can otherwise turn the same endpoint into a second vertex.
            if abs(fraction - ta) <= parameter_tolerance:
                local_fraction = 0.0
            elif abs(fraction - tb) <= parameter_tolerance:
                local_fraction = 1.0
            candidates.append((abs(tb - ta), edge, max(0.0, min(1.0, local_fraction))))
    if not candidates:
        raise SlideError("После предыдущего разреза не найдена боковая направляющая.")
    _, edge, local_fraction = min(candidates)
    return edge, local_fraction


def _rail_insert(fn: om.MFnMesh, run: Plan, index: int, fraction: float):
    return _segment_insert(fn, om.MVector(*run.starts[index]),
                           om.MVector(*run.ends[index]), fraction, run.vertices[index], run.targets[index])


def _split_run(plan: BevelPlan, run: Plan, seed: float):
    fn = _mesh_fn(plan.mesh)
    if run.patch_faces:
        current = replace(run, original_vertex_count=fn.numVertices)
        node = cmds.polyExtrudeFacet(f"{plan.mesh}.f[{run.patch_faces[0]}]",
                                     keepFacesTogether=True, constructionHistory=True,
                                     offset=0)[0]
        _, vertices = _select_extruded_edges(current)
        return vertices, node
    return _split_path(plan, tuple((run, index) for index in range(len(run.vertices))), seed), None


def _guide_face(fn, run, index, triangles, position, points):
    """Find the current face on the oriented side of the selected edges."""
    source = run.vertices[index]
    vertex = om.MItMeshVertex(fn.dagPath())
    vertex.setIndex(source)
    original = {v for triangle in triangles for v in triangle}
    candidates = []
    distance = _mesh_position_tolerance(fn, (position,))
    iterator = om.MItMeshPolygon(fn.dagPath())
    for face in vertex.getConnectedFaces():
        polygon = list(fn.getPolygonVertices(face))
        normal = fn.getPolygonNormal(face, om.MSpace.kWorld)
        matched = len(original.intersection(polygon))
        if matched >= 2:
            iterator.setIndex(face)
            _, ids = iterator.getTriangles(om.MSpace.kObject)
            for offset in range(0, len(ids), 3):
                a, b, c = (points[v] for v in ids[offset:offset + 3])
                u, v, delta = b - a, c - a, position - a
                area = (u ^ v) * normal
                if abs(area) <= _EPS * _EPS:
                    continue
                s, t = ((delta ^ v) * normal) / area, ((u ^ delta) * normal) / area
                if min(s, t, 1. - s - t) >= -1e-6:
                    error = (a + u*s + v*t - position).length()
                    if error <= distance:
                        candidates.append((error, -matched, face))
        corner = polygon.index(source)
        if index > 0 and polygon[corner - 1] != run.vertices[index - 1]:
            continue
        if index + 1 < len(run.vertices) and polygon[(corner + 1) % len(polygon)] != run.vertices[index + 1]:
            continue
        return face
    if candidates:
        return min(candidates)[-1]
    raise SlideError("Не удалось найти грань угловой направляющей.")


def _on_face_surface(point, triangles, points, normal):
    """Evaluate the inset on Maya's original face triangulation."""
    hits = []
    for vertices in triangles:
        a, b, c = (points[v] for v in vertices)
        u, v, offset = b-a, c-a, point-a
        area = (u ^ v) * normal
        if abs(area) < _EPS * _EPS:
            continue
        s, t = ((offset ^ v) * normal) / area, ((u ^ offset) * normal) / area
        if min(s, t, 1. - s - t) >= -1e-7:
            hits.append(a + u*s + v*t)
    return min(hits, key=lambda hit: (hit-point).length()) if hits else point


def _split_path(plan: BevelPlan, references, seed: float):
    fn = _mesh_fn(plan.mesh)
    inverse = fn.dagPath().inclusiveMatrixInverse()
    run_points = {}
    for run, _ in references:
        if id(run) not in run_points:
            run_points[id(run)] = preview_points(run, seed, "relative")[0]
    seed_points = [run_points[id(run)][index] for run, index in references]
    points = (fn.getPoints(om.MSpace.kWorld)
              if any(run.rails[index] < 0 for run, index in references) else None)
    inserts = []
    reused = set()
    guide_faces = {}
    before = fn.numVertices
    for slot, ((run, index), point) in enumerate(zip(references, seed_points)):
        if run.rails[index] >= 0:
            insertion = _rail_insert(fn, run, index, seed)
            inserts.append(insertion)
            edge, fraction = insertion
            if fraction in (0.0, 1.0):
                reused.add(fn.getEdgeVertices(edge)[int(fraction)])
        else:
            triangles = plan.face_triangles[run.guide_faces[index]]
            normal = om.MVector()
            for a, b, c in triangles:
                normal += (points[b] - points[a]) ^ (points[c] - points[a])
            position = _on_face_surface(om.MPoint(*point), triangles, points, normal.normal())
            current_face = _guide_face(fn, run, index, triangles, position, points)
            guide_faces[slot] = current_face
            seed_points[slot] = tuple(position)[:3]
            local = position * inverse
            inserts.append((current_face, local.x, local.y, local.z))
    for slot,at_start in ((0,True),(len(references)-1,False)):
        run,index=references[slot]
        if run.rails[index]>=0:continue
        source=run.vertices[index]
        polygon = om.MItMeshPolygon(fn.dagPath())
        polygon.setIndex(guide_faces[slot])
        edge = next(edge for edge in polygon.getEdges() if source in fn.getEdgeVertices(edge))
        a, b = fn.getEdgeVertices(edge)
        anchor=(edge,0. if a==source else 1.)
        if at_start:inserts.insert(0,anchor)
        else:inserts.append(anchor)
    cmds.polySplit(plan.mesh, insertpoint=inserts, constructionHistory=True)
    fn = _mesh_fn(plan.mesh)
    candidates = {vertex: tuple(fn.getPoint(vertex, om.MSpace.kWorld))[:3]
                  for vertex in set(range(before, fn.numVertices)) | reused}
    if not candidates:
        raise SlideError("Не удалось найти вершины после разреза грани.")
    result = []
    for point in seed_points:
        candidate = min(candidates, key=lambda vertex: math.dist(candidates[vertex], point))
        distance = math.dist(candidates[candidate], point)
        if distance > 1e-4:
            raise SlideError("Не удалось найти вершину после разреза грани.")
        result.append(candidate)
    return tuple(result)


def _run_paths(plan: BevelPlan):
    """Join face cuts that share the same directed rail endpoint."""
    endpoints = {}
    keys = {}
    for run_index, run in enumerate(plan.runs):
        if run.patch_faces:
            continue
        for index in (0, len(run.vertices) - 1):
            key = (run.vertices[index], run.targets[index]) if run.rails[index] >= 0 else (
                "interior", run_index, index)
            keys[run_index, index] = key
            endpoints.setdefault(key, []).append((run_index, index))
    remaining = {run_index for run_index, _ in keys}
    open_starts = [entry for entry, key in keys.items() if len(endpoints[key]) != 2]
    open_starts.reverse()
    while remaining:
        # Prefer the end of an open path; closed paths can start anywhere.
        while open_starts and open_starts[-1][0] not in remaining:
            open_starts.pop()
        run_index, first = open_starts.pop() if open_starts else (next(iter(remaining)), 0)
        references = []
        positions = {}
        while run_index in remaining:
            remaining.remove(run_index)
            run = plan.runs[run_index]
            indices = list(range(len(run.vertices)))
            if first:
                indices.reverse()
            slots = [None] * len(indices)
            for offset, index in enumerate(indices):
                if offset == 0 and references:
                    slots[index] = len(references) - 1
                else:
                    slots[index] = len(references)
                    references.append((run, index))
            positions[run_index] = tuple(slots)
            matches = endpoints[keys[run_index, indices[-1]]]
            following = [entry for entry in matches if entry[0] in remaining]
            if len(matches) != 2 or len(following) != 1:
                break
            run_index, first = following[0]
        yield tuple(references), positions


def _new_edge_ids(plan: BevelPlan, run_vertices, connectors, caps, rows):
    fn = _mesh_fn(plan.mesh)
    edge_map = {frozenset(fn.getEdgeVertices(edge)): edge for edge in range(fn.numEdges)}
    result = []
    for run_index, (run, vertices) in enumerate(zip(plan.runs, run_vertices)):
        for index in range(len(run.edge_ids)):
            support = sorted(rows.get((run_index, index), ()))
            path = (vertices[index], *(vertex for _, vertex, _ in support),
                    vertices[(index + 1) % len(vertices)])
            for a, b in zip(path, path[1:]):
                edge = edge_map.get(frozenset((a, b)))
                if edge is None:
                    raise SlideError("Новые рёбра не образовали разрез в грани.")
                result.append(edge)
    for a, b in connectors:
        edge = edge_map.get(frozenset((a, b)))
        if edge is None:
            raise SlideError("Не удалось замкнуть опорные рёбра у угла.")
        result.append(edge)
    for edge, (a, b) in zip(plan.edge_ids, plan.edge_vertices):
        path = [a, *(caps[key] for key in ((a, edge), (b, edge)) if key in caps), b]
        if any(frozenset((first, second)) not in edge_map for first, second in zip(path, path[1:])):
            raise SlideError("Исходное выделенное ребро не сохранилось после разреза.")
    return tuple(sorted(set(result)))


def _continue_two_edge_corners(plan: BevelPlan, run_vertices, selected_degree):
    """Join two cuts across a small face fan around an ordinary corner."""
    endpoints = {}
    for run, vertices in zip(plan.runs, run_vertices):
        if run.patch_faces:
            continue
        for index in (0, len(run.vertices) - 1):
            source = run.vertices[index]
            if selected_degree.get(source) != 2 or run.guide_faces[index] >= 0:
                continue
            edge = run.edge_ids[0] if index == 0 else run.edge_ids[-1]
            endpoints.setdefault(source, set()).add((vertices[index], edge))

    selected_neighbors = {}
    for a, b in plan.edge_vertices:
        selected_neighbors.setdefault(a, set()).add(b)
        selected_neighbors.setdefault(b, set()).add(a)

    fn = _mesh_fn(plan.mesh)
    neighbors = {}
    existing = set()
    for edge in range(fn.numEdges):
        a, b = fn.getEdgeVertices(edge)
        neighbors.setdefault(a, set()).add(b)
        neighbors.setdefault(b, set()).add(a)
        existing.add(frozenset((a, b)))
    faces = {}
    for face in range(fn.numPolygons):
        for vertex in fn.getPolygonVertices(face):
            faces.setdefault(vertex, set()).add(face)

    connectors = []
    for source, entries in endpoints.items():
        if len(entries) != 2:
            continue
        (first, first_edge), (second, second_edge) = sorted(entries)
        if first == second or first_edge == second_edge:
            continue
        candidates = []
        for target in neighbors.get(source, set()) - selected_neighbors.get(source, set()):
            if target >= plan.original_vertex_count:
                continue
            if (faces.get(first, set()) & faces.get(target, set()) and
                    faces.get(second, set()) & faces.get(target, set())):
                candidates.append(target)
        if len(candidates) != 1:
            continue
        target = candidates[0]
        for inserted in (first, second):
            pair = frozenset((inserted, target))
            if pair not in existing:
                cmds.polyConnectComponents(
                    f"{plan.mesh}.vtx[{inserted}]",
                    f"{plan.mesh}.vtx[{target}]",
                    constructionHistory=True,
                )
                existing.add(pair)
            connectors.append((inserted, target))
    return connectors


def _insert_between(mesh, a, b, fraction):
    fn = _mesh_fn(mesh)
    points = fn.getPoints(om.MSpace.kWorld)
    edge, local = _segment_insert(fn, om.MVector(points[a]), om.MVector(points[b]), fraction, a, b)
    if local in (0., 1.):
        return fn.getEdgeVertices(edge)[int(local)]
    vertex = fn.numVertices
    cmds.polySplit(mesh, insertpoint=[(edge, local)], constructionHistory=True)
    if _mesh_fn(mesh).numVertices != vertex + 1:
        raise SlideError("Не удалось создать опорную вершину в углу.")
    return vertex


def _connect(mesh, a, b):
    cmds.polyConnectComponents(f"{mesh}.vtx[{a}]", f"{mesh}.vtx[{b}]", constructionHistory=True)


def _rail_fraction(point, start, end):
    rail = end - start
    squared = rail * rail
    return max(0., min(1., ((point - start) * rail) / squared)) if squared > _EPS * _EPS else 0.


def _quad_corner(points, vertices, first, second, surface):
    corner, following, opposite, previous = (points[v] for v in vertices)
    u = _rail_fraction(first, corner, following)
    v = _rail_fraction(second, corner, previous)
    near = corner + (following - corner) * u
    far = previous + (opposite - previous) * u
    point = near + (far - near) * v
    triangles, normal = surface
    return _on_face_surface(point, triangles, points, normal)


def _opposite_quad_route(fn, first_quad, original_quads, original_ngons):
    """Follow an uncut strip to a boundary or an existing n-gon."""
    vertex = om.MItMeshVertex(fn.dagPath())
    edge = om.MItMeshEdge(fn.dagPath())
    route = [first_quad]
    visited = {frozenset(first_quad)}
    while True:
        _, _, c, d = route[-1]
        vertex.setIndex(c)
        rail = next((e for e in vertex.getConnectedEdges() if d in fn.getEdgeVertices(e)), None)
        if rail is None:
            return ()
        edge.setIndex(rail)
        adjacent = edge.getConnectedFaces()
        if len(adjacent) == 1:
            return route
        for face in adjacent:
            polygon = tuple(fn.getPolygonVertices(face))
            key = frozenset(polygon)
            if key in original_ngons:
                return route
            if key in visited or key not in original_quads:
                continue
            start = polygon.index(d)
            ordered = polygon[start:] + polygon[:start]
            if ordered[1] != c:
                continue
            route.append(ordered)
            visited.add(key)
            break
        else:
            return ()


def _join_quad_supports(plan, run_vertices, original_quads, original_ngons, connectors):
    """Continue rails across the neighboring quad while retaining its surface."""
    mesh = plan.mesh
    fn = _mesh_fn(mesh)
    rail_points = {vertex for run, vertices in zip(plan.runs, run_vertices)
                   for vertex, rail in zip(vertices, run.rails) if rail >= 0}
    original = plan.original_vertex_count
    candidates = []
    for face in range(fn.numPolygons):
        polygon = tuple(fn.getPolygonVertices(face))
        corners = tuple(v for v in polygon if v < original)
        if len(corners) == 4 and len(polygon) > 4 and frozenset(corners) in original_quads:
            candidates.append((polygon, corners))
    motions = []
    for polygon, corners in candidates:
        extras = [v for v in polygon if v >= original]
        if any(v not in rail_points for v in extras):
            continue
        sides = []
        for start in corners:
            side = []
            index = (polygon.index(start) + 1) % len(polygon)
            while polygon[index] >= original:
                side.append(polygon[index])
                index = (index + 1) % len(polygon)
            sides.append(side)
        active = [i for i, side in enumerate(sides) if side]
        if len(active) == 1:
            index = active[0]
            quad = corners[index:] + corners[:index]
            fn = _mesh_fn(mesh)
            route = _opposite_quad_route(fn, quad, original_quads, original_ngons)
            if not route:
                continue
            points = fn.getPoints(om.MSpace.kWorld)
            for first in sides[index]:
                a, b = quad[:2]
                fraction = _rail_fraction(points[first], points[a], points[b])
                for a, b, c, d in route:
                    second = _insert_between(mesh, d, c, fraction)
                    _connect(mesh, first, second)
                    connectors.append((first, second))
                    motions.append((second, (a, b, c, d), first, None))
                    first = second
            continue
        if len(active) == 2 and active[1] - active[0] == 2:
            left, right = (sides[i] for i in active)
            if len(left) == len(right):
                for first, second in zip(left, reversed(right)):
                    _connect(mesh, first, second)
                    connectors.append((first, second))
            continue
        if len(extras) != 2 or len(active) != 2:
            continue
        # Adjacent rails form a turn. An interior miter and a spoke to the
        # opposite corner divide the six-sided boundary into three quads.
        index = next(i for i in active if (i - 1) % 4 in active)
        first, = sides[index]
        second, = sides[index - 1]
        quad = corners[index:] + corners[:index]
        fn = _mesh_fn(mesh)
        points = fn.getPoints(om.MSpace.kWorld)
        ring = [points[v] for v in quad]
        normals = [(ring[(i + 1) % 4] - p) ^ (ring[i - 1] - p) for i, p in enumerate(ring)]
        if normals[0].length() <= _EPS * _EPS or any(normals[0] * n <= 0. for n in normals):
            continue
        iterator = om.MItMeshVertex(fn.dagPath())
        iterator.setIndex(first)
        face = next(f for f in iterator.getConnectedFaces()
                    if frozenset(fn.getPolygonVertices(f)) == frozenset(polygon))
        surface = original_quads[frozenset(quad)]
        insertion = _quad_corner(points, quad, points[first], points[second], surface)
        local = insertion * fn.dagPath().inclusiveMatrixInverse()
        anchors = []
        for vertex in (first, second):
            iterator.setIndex(vertex)
            edge = next(e for e in iterator.getConnectedEdges() if quad[0] in fn.getEdgeVertices(e))
            anchors.append((edge, float(fn.getEdgeVertices(edge)[1] == vertex)))
        middle = fn.numVertices
        cmds.polySplit(mesh, insertpoint=[anchors[0], (face, local.x, local.y, local.z), anchors[1]],
                       constructionHistory=True)
        after = _mesh_fn(mesh).numVertices
        if after == middle:
            # Maya may refuse an interior point on a very narrow quad.
            # Build the same junction by splitting its cross-cut first.
            _connect(mesh, first, second)
            middle = _insert_between(mesh, first, second, .5)
        elif after != middle + 1:
            raise SlideError("Не удалось соединить опорные рёбра на углу.")
        _connect(mesh, middle, quad[2])
        connectors.extend(((first, middle), (middle, second), (middle, quad[2])))
        motions.append((middle, quad, first, second))
    return tuple(motions)


def _route_transition_rows(plan, run_vertices):
    """Continue the offset through each side of a folded triangular junction."""
    transitions = set(plan.transition_vertices)
    if not transitions:
        return {}, {}, {}, []
    source_edges = dict(zip(plan.edge_ids, plan.edge_vertices))
    turns = []
    guides = {}
    for run, vertices in zip(plan.runs, run_vertices):
        indices = range(len(vertices)) if run.patch_faces else range(1, len(vertices) - 1)
        for index in indices:
            source, inset = run.vertices[index], vertices[index]
            if source not in transitions:
                continue
            before, after = run.edge_ids[index - 1], run.edge_ids[index % len(run.edge_ids)]
            first, second = (source, before), (source, after)
            turns.append((source, inset, first, second, bool(run.patch_faces)))
            guides.setdefault(first, []).append((inset, next(v for v in source_edges[after] if v != source)))
            guides.setdefault(second, []).append((inset, next(v for v in source_edges[before] if v != source)))
    caps = {}
    for source, edge in guides:
        target = next(v for v in source_edges[edge] if v != source)
        inserted = _insert_between(plan.mesh, source, target, .25)
        if inserted not in (source, target):
            caps[(source, edge)] = inserted

    connectors = []
    for source, inset, first, second, patch in turns:
        if first not in caps or second not in caps:
            continue
        for key in (first, second):
            _connect(plan.mesh, inset, caps[key])
            connectors.append((inset, caps[key]))
        if patch:
            fn = _mesh_fn(plan.mesh)
            spoke = next(e for e in range(fn.numEdges)
                         if set(fn.getEdgeVertices(e)) == {source, inset})
            cmds.polyDelEdge(f"{plan.mesh}.e[{spoke}]", cleanVertices=False, constructionHistory=True)

    rows = {}
    for run_index, (run, vertices) in enumerate(zip(plan.runs, run_vertices)):
        if run.patch_faces:
            continue
        for index, edge_index, fraction in ((0, 0, .25), (-1, len(run.edge_ids) - 1, .75)):
            key = run.vertices[index], run.edge_ids[edge_index]
            if key not in caps or run.rails[index] < 0:
                continue
            a, b = vertices[edge_index:edge_index + 2]
            inserted = _insert_between(plan.mesh, a, b, fraction)
            if inserted not in (a, b):
                rows.setdefault((run_index, edge_index), []).append((fraction, inserted, key))
            _connect(plan.mesh, caps[key], inserted)
            connectors.append((caps[key], inserted))
    return caps, guides, rows, connectors


def _extend_transition_rows(plan, run_vertices, rows, outer_contours, connectors):
    """Carry each transverse cut across the previously built inset bands."""
    extensions = []
    for (run_index, edge_index), supports in rows.items():
        a, b = run_vertices[run_index][edge_index:edge_index + 2]
        supports = sorted(supports)
        chain = (a, *(vertex for _, vertex, _ in supports), b)
        remaining = len(outer_contours)
        while remaining:
            fn = _mesh_fn(plan.mesh)
            iterator = om.MItMeshVertex(fn.dagPath())
            iterator.setIndex(chain[1])
            match = None
            for face in iterator.getConnectedFaces():
                polygon = tuple(fn.getPolygonVertices(face))
                if len(polygon) != len(chain) + 2:
                    continue
                for direction in (polygon, polygon[::-1]):
                    if chain[0] not in direction:
                        continue
                    index = direction.index(chain[0])
                    ordered = direction[index:] + direction[:index]
                    if ordered[:len(chain)] != chain:
                        continue
                    outer_a, outer_b = ordered[-1], ordered[-2]
                    layer = next((i for i in range(remaining - 1, -1, -1)
                                  if outer_a in outer_contours[i] and outer_b in outer_contours[i]), None)
                    if layer is not None:
                        match = outer_a, outer_b, layer
                        break
                if match is not None:
                    break
            if match is None:
                break
            outer_a, outer_b, remaining = match
            extended = []
            for (seed, _, key), inner in zip(supports, chain[1:-1]):
                outer = _insert_between(plan.mesh, outer_a, outer_b, seed)
                if outer in (outer_a, outer_b):
                    raise SlideError("Не удалось продолжить поперечное ребро через контур.")
                _connect(plan.mesh, inner, outer)
                connectors.append((inner, outer))
                extensions.append((outer, outer_a, outer_b, seed, key))
                outer_contours[remaining].add(outer)
                extended.append(outer)
            chain = (outer_a, *extended, outer_b)
    return tuple(extensions)


def _build(plan: BevelPlan, outer_contours=()):
    fn = _mesh_fn(plan.mesh)
    cut_faces = {face for run in plan.runs for face in run.faces}
    original_quads = {}
    original_ngons = set()
    polygon = om.MItMeshPolygon(fn.dagPath())
    for face in range(fn.numPolygons):
        if face in cut_faces:
            continue
        vertices = frozenset(fn.getPolygonVertices(face))
        if len(vertices) == 4:
            polygon.setIndex(face)
            _, ids = polygon.getTriangles(om.MSpace.kObject)
            triangles = tuple(tuple(ids[i:i + 3]) for i in range(0, len(ids), 3))
            original_quads[vertices] = triangles, fn.getPolygonNormal(face, om.MSpace.kWorld)
        elif len(vertices) > 4:
            original_ngons.add(vertices)
    run_vertices = [None] * len(plan.runs)
    patch_nodes = [None] * len(plan.runs)
    for references, positions in _run_paths(plan):
        directed_rails = {}
        for run, index in references:
            if run.rails[index] >= 0:
                directed_rails.setdefault(run.rails[index], set()).add(run.vertices[index])
        interior_ends = all(run.rails[index] < 0 for run, index in (references[0], references[-1]))
        if (any(len(sources) > 1 for sources in directed_rails.values())
                or (len(positions) > 1 and interior_ends)):
            # Split face by face when the native path crosses a repeated rail
            # or has a vertex-anchored end on a different face.
            for run_index in positions:
                run_vertices[run_index], _ = _split_run(plan, plan.runs[run_index], 0.25)
        else:
            vertices = _split_path(plan, references, 0.25)
            for run_index, slots in positions.items():
                run_vertices[run_index] = tuple(vertices[slot] for slot in slots)
    for index, run in enumerate(plan.runs):
        if run.patch_faces:
            run_vertices[index], patch_nodes[index] = _split_run(plan, run, 0.25)
    selected_degree = {}
    for a, b in plan.edge_vertices:
        selected_degree[a] = selected_degree.get(a, 0) + 1
        selected_degree[b] = selected_degree.get(b, 0) + 1
    caps, guides, rows, connectors = _route_transition_rows(plan, run_vertices)
    extensions = _extend_transition_rows(plan, run_vertices, rows, outer_contours, connectors)
    fn = _mesh_fn(plan.mesh)
    existing = {frozenset(fn.getEdgeVertices(edge)) for edge in range(fn.numEdges)}
    for run, vertices in zip(plan.runs, run_vertices):
        if run.patch_faces:
            continue  # Extrude already connects each corner to its inset.
        for index, (source, inserted) in enumerate(zip(run.vertices[1:-1], vertices[1:-1]), 1):
            if all((source, edge) in caps for edge in run.edge_ids[index - 1:index + 1]):
                continue
            pair = frozenset((source, inserted))
            if pair not in existing:
                # Split the band at the corner so each selected edge has a quad.
                _connect(plan.mesh, source, inserted)
                existing.add(pair)
    junctions = _join_quad_supports(plan, run_vertices, original_quads, original_ngons, connectors)
    connectors.extend(_continue_two_edge_corners(plan, run_vertices, selected_degree))
    _restore_planar_fans(plan, run_vertices)
    selected_edges = _new_edge_ids(plan, run_vertices, connectors, caps, rows)
    junction_surfaces = {quad: original_quads[frozenset(quad)]
                         for _, quad, _, second in junctions if second is not None}
    return (tuple(run_vertices), selected_edges, tuple(patch_nodes), (caps, guides, rows),
            extensions, junctions, junction_surfaces)


def _move_transition_rows(plan, run_vertices, transition, extensions, translations, points, amount, mode):
    caps, guides, rows = transition
    edges = dict(zip(plan.edge_ids, plan.edge_vertices))
    fractions = {}
    for (source, edge), vertex in caps.items():
        a, b = edges[edge]
        target = b if a == source else a
        axis = points[target] - points[source]
        length = axis.length()
        if length <= _EPS:
            continue
        axis /= length
        crossing = []
        for inset, opposite in guides[source, edge]:
            offset = om.MPoint(*translations[inset]) - points[source]
            other = points[opposite] - points[source]
            normal = other - axis * (other * axis)
            squared = normal * normal
            if squared > _EPS * _EPS:
                along = offset * axis - (offset * normal) * (other * axis) / squared
                if along > _EPS:
                    crossing.append(along / length)
        fallback = amount if mode == "relative" else amount / length
        fraction = sum(crossing) / len(crossing) if crossing else fallback
        # Both ends of a selected edge can carry an independent transition.
        # Keep their sections ordered even on a short edge.
        maximum = .49 if (target, edge) in caps else _MAX_FRACTION
        fraction = min(maximum, max(fraction, 1e-6))
        fractions[source, edge] = fraction
        translations[vertex] = tuple(points[source] + axis * (fraction * length))[:3]
    for (run_index, edge_index), supports in rows.items():
        a, b = run_vertices[run_index][edge_index:edge_index + 2]
        first = om.MPoint(*translations[a])
        last = om.MPoint(*translations[b])
        for seed, vertex, key in supports:
            amount = fractions.get(key, .25)
            fraction = amount if seed < .5 else 1. - amount
            translations[vertex] = tuple(first + (last - first) * fraction)[:3]
    for vertex, a, b, seed, key in extensions:
        first = om.MPoint(*translations[a]) if a in translations else points[a]
        last = om.MPoint(*translations[b]) if b in translations else points[b]
        fraction = fractions.get(key, .25)
        if seed > .5:
            fraction = 1. - fraction
        translations[vertex] = tuple(first + (last - first) * fraction)[:3]


def _relative_run_scale(run, spans, reference):
    """Convert a percentage into one perpendicular offset for a face run."""
    if reference is not None:
        # Later rows can have shorter rails after the outer row cuts them.
        # Match the original directions so their width remains in source units.
        source_spans = []
        for original in reference.runs:
            shared = set(run.vertices).intersection(original.vertices)
            if not shared or original.patch_faces:
                continue
            original_spans = _normal_distances(original)
            for vertex in shared:
                i, j = run.vertices.index(vertex), original.vertices.index(vertex)
                rail = (om.MPoint(*run.ends[i]) - om.MPoint(*run.starts[i])).normal()
                source = (om.MPoint(*original.ends[j]) - om.MPoint(*original.starts[j])).normal()
                if rail * source > .999:
                    source_spans.append(original_spans[j])
        if source_spans:
            spans = source_spans
    return min((span for span in spans if span > _EPS), default=1.)


def _move(plan: BevelPlan, run_vertices, patch_nodes, transition, extensions, junctions, junction_surfaces,
          amount: float, mode: str, native=False, relative_reference=None):
    targets = {};rails={};contacts=set();motions={};corners=set();stops={}
    seams = {}
    fn = _mesh_fn(plan.mesh)
    world_points = fn.getPoints(om.MSpace.kWorld)
    surfaces = {}
    for face, triangles in plan.face_triangles.items():
        normal = om.MVector()
        for a, b, c in triangles:
            normal += (world_points[b] - world_points[a]) ^ (world_points[c] - world_points[a])
        surfaces[face] = triangles, normal.normal()
    transition_sources = set(plan.transition_vertices) if mode == "uniform" else set()
    for run, vertices, node in zip(plan.runs, run_vertices, patch_nodes):
        if mode == "uniform" and run.patch_faces:
            # Extrude creates the topology at zero offset. Move its inset in
            # world space so short fully-selected faces can collapse locally,
            # instead of retaining the old 99% gap on their narrow side.
            if node is not None:
                cmds.setAttr(node + ".offset", 0.)
            starts=[om.MPoint(*p) for p in run.starts];normal=om.MVector()
            for i,p in enumerate(starts):normal+=(p-starts[0])^(starts[(i+1)%len(starts)]-starts[0])
            normal.normalize()
            for i,vertex in enumerate(vertices):
                previous=(starts[i]-starts[i-1]).normal();following=(starts[(i+1)%len(starts)]-starts[i]).normal()
                a=normal^previous;b=normal^following;den=1.+a*b
                if den<1e-7:raise SlideError('Не удалось определить отступ вырожденного угла грани.')
                velocity=(a+b)/den;p=starts[i]+velocity*amount
                triangles, face_normal = surfaces[run.patch_faces[0]]
                p = _on_face_surface(p, triangles, world_points, face_normal)
                targets.setdefault(vertex,[]).append(tuple(p)[:3])
                motions.setdefault(vertex,[]).append((starts[i],velocity,math.inf));corners.add(vertex)
            continue
        widths = (1.,) * len(run.vertices) if run.patch_faces else _normal_distances(run)
        scale = _relative_run_scale(run, widths, relative_reference) if mode == "relative" and not run.patch_faces else 1.
        movement_mode = "relative" if run.patch_faces else "uniform"
        spans = tuple(width / scale for width in widths)
        limits = (1.,) * len(run.vertices)
        points, _ = preview_points(run, amount * scale, movement_mode, fraction_limits=limits)
        probe=max(min(spans)*.25,1e-10)
        probe_points = preview_points(run, probe * scale, movement_mode, fraction_limits=limits)[0]
        for i,vertex in enumerate(vertices):
            start=om.MPoint(*run.starts[i]);velocity=(om.MPoint(*probe_points[i])-start)/probe
            motions.setdefault(vertex,[]).append((start,velocity,spans[i]))
            if run.guide_faces[i]>=0:corners.add(vertex)
        for vertex, point, face in zip(vertices, points, run.guide_faces):
            if face >= 0 and not run.patch_faces:
                triangles, face_normal = surfaces[face]
                position = _on_face_surface(om.MPoint(*point), triangles, world_points, face_normal)
                point = tuple(position)[:3]
            targets.setdefault(vertex, []).append(point)
        for index,vertex in enumerate(vertices):
            if run.rails[index]<0:continue
            source,target=run.vertices[index],run.targets[index]
            entry=rails.setdefault(frozenset((source,target)),{})
            entry.setdefault((source,target,vertex),[]).append(spans[index])
            if source in transition_sources and spans[index] > _EPS:
                edge = 0 if index == 0 else len(run.edge_ids) - 1
                tangent = (om.MPoint(run.starts[edge + 1]) - om.MPoint(run.starts[edge])).normal()
                guide = om.MPoint(run.ends[index]) - om.MPoint(run.starts[index])
                inward = (guide - tangent * (guide * tangent)).normal()
                seams.setdefault(vertex, []).append((source, target, inward, tangent ^ inward, spans[index]))
    translations = {}
    for vertex, positions in targets.items():
        point = tuple(sum(position[axis] for position in positions) / len(positions)
                      for axis in range(3))
        translations[vertex] = point
    for vertex, guides in seams.items():
        if len(guides) != 2 or len(targets[vertex]) != 2:
            continue
        (source, target, first, face_a, width_a), (other, far, second, face_b, width_b) = guides
        span = min(width_a, width_b)
        cosine = first * second
        if ((source, target) != (other, far) or abs(face_a * face_b) < .9994
                or cosine < -.95 or amount >= span):
            continue
        # Almost coplanar faces can turn across a seam. The common point is
        # the intersection of the two offset lines, not the average rail hit.
        origin = motions[vertex][0][0]
        velocity = (first + second) / (1.0 + cosine)
        # The miter is valid only in both face planes. At a fold the common
        # support must stay on the shared rail even when the widths differ.
        if max(abs(velocity * face_a), abs(velocity * face_b)) > 1e-6:
            continue
        translations[vertex] = tuple(origin + velocity * amount)[:3]
        motions[vertex] = [(origin, velocity, span)]
    # Opposite cuts stop only on their own rail, at their common meeting
    # point. Other rails in the same face keep moving with the drag.
    for entries in rails.values():
        sides={}
        for (source,target,vertex),spans in entries.items():
            sides.setdefault((source,target),[]).append((vertex,spans))
        if len(sides)==2:
            (source,target),first=next(iter(sides.items()));second=sides.get((target,source))
            if second:
                def fraction(group,value):
                    return sum(sum(min(value/max(span,_EPS),1.) for span in spans)/len(spans)
                               for vertex,spans in group)/len(group)
                if fraction(first,amount)+fraction(second,amount)>=1.-1e-10:
                    low,high=0.,amount
                    for _ in range(48):
                        middle=(low+high)*.5
                        if fraction(first,middle)+fraction(second,middle)<1.:low=middle
                        else:high=middle
                    value=(low+high)*.5;f=fraction(first,value)
                    # Original endpoints are unchanged by the new cuts.
                    a=next(om.MPoint(*run.starts[i]) for run in plan.runs for i,v in enumerate(run.vertices) if v==source)
                    b=next(om.MPoint(*run.ends[i]) for run in plan.runs for i,v in enumerate(run.vertices)
                           if v==source and run.targets[i]==target)
                    point=a+(b-a)*f
                    for vertex,spans in first+second:
                        translations[vertex]=tuple(point)[:3];contacts.add(vertex);stops[vertex]=(value,point)
                    continue
        for (source,target),group in sides.items():
            for vertex,spans in group:
                if all(amount>=span-1e-10 for span in spans):
                    contacts.update((vertex,target));stops[vertex]=(max(spans),om.MPoint(*translations[vertex]))
    contacts.update(_corner_contacts(plan,translations,motions,corners,stops,amount))
    points = world_points
    if transition[0]:
        _move_transition_rows(plan, run_vertices, transition, extensions, translations, points, amount, mode)
    for middle, quad, first, second in junctions:
        left = om.MPoint(*translations[first])
        if second is None:
            a, b, c, d = (points[v] for v in quad)
            position = d + (c - d) * _rail_fraction(left, a, b)
        else:
            right = om.MPoint(*translations[second])
            triangles, _ = junction_surfaces[quad]
            normal = om.MVector()
            for a, b, c in triangles:
                normal += (points[b] - points[a]) ^ (points[c] - points[a])
            position = _quad_corner(points, quad, left, right, (triangles, normal.normal()))
        translations[middle] = tuple(position)[:3]
    if native:
        for vertex, point in translations.items():
            points[vertex] = om.MPoint(*point)
        fn.setPoints(points, om.MSpace.kWorld)
        fn.updateSurface()
    else:
        for vertex, point in translations.items():
            cmds.xform(f"{plan.mesh}.vtx[{vertex}]", worldSpace=True, translation=point)
    return contacts


def _corner_contacts(plan,translations,motions,corners,stops,amount):
    """Stop collapsing inset edges inside a face, including corner fan spokes.

    A three-sided cut in a narrow quad has two interior miter points. They
    meet before either reaches its guide endpoint; rail-only limits miss it.
    Follow the piecewise-linear trajectories and weld their first contact.
    """
    if not corners:return set()
    fn=_mesh_fn(plan.mesh);candidates=[];fixed={};parent={};contacts=set()
    for edge in range(fn.numEdges):
        a,b=fn.getEdgeVertices(edge)
        if a not in corners and b not in corners:continue
        if any(v not in motions and v>=plan.original_vertex_count for v in (a,b)):continue
        candidates.append((a,b))
        for v in (a,b):
            parent[v]=v
            if v not in motions:fixed[v]=om.MPoint(fn.getPoint(v,om.MSpace.kWorld))
    def root(v):
        while parent[v]!=v:v=parent[v]
        return v
    def point(v,t):
        if v in fixed:return fixed[v]
        if v in stops and t>=stops[v][0]-1e-12:return stops[v][1]
        terms=motions[v];p=om.MPoint()
        for start,velocity,limit in terms:p+=start+velocity*min(t,limit)
        return p/len(terms)
    def event(a,b,floor):
        tolerance=_mesh_position_tolerance(fn,(point(a,0),point(b,0)))
        if floor>1e-9 and (point(a,floor)-point(b,floor)).length()<=tolerance:
            return floor,(point(a,floor)+om.MVector(point(b,floor)))*.5
        breaks={floor,amount}
        for v in (a,b):
            for start,velocity,limit in motions.get(v,()):
                if floor<limit<amount:breaks.add(limit)
            if v in stops and floor<stops[v][0]<amount:breaks.add(stops[v][0])
        values=sorted(breaks)
        for low,high in zip(values,values[1:]):
            if high-low<1e-12:continue
            p,q=point(a,low),point(b,low);delta=q-p
            change=((point(b,high)-q)-(point(a,high)-p))/(high-low);squared=change*change
            if squared<1e-20:continue
            dt=-(delta*change)/squared;t=low+dt
            if t<=1e-9 or dt<-1e-9 or t>high+1e-9:continue
            if (delta+change*dt).length()<=tolerance:
                return max(low,min(t,high)),(point(a,t)+om.MVector(point(b,t)))*.5
        return None
    floor=0.
    for _ in range(len(candidates)):
        events=[]
        for a,b in candidates:
            if root(a)==root(b):continue
            hit=event(a,b,floor)
            if hit:events.append((hit[0],a,b,hit[1]))
        if not events:break
        t,a,b,p=min(events,key=lambda value:value[:3]);parent[root(b)]=root(a);floor=t
        for v in (a,b):
            contacts.add(v)
            if v in motions:
                prior=stops.get(v)
                stops[v]=(min(t,prior[0]) if prior else t,p)
                translations[v]=tuple(p)[:3]
    return contacts


def _weld_contacts(mesh,vertices,edges):
    """Weld only intentional rail contacts, retaining the resulting contours."""
    if len(vertices)<2:return edges,False
    from .multiple import _segments,_edges_on_segments
    segments=_segments(mesh,edges);fn=_mesh_fn(mesh);before=fn.numVertices
    points=[fn.getPoint(v,om.MSpace.kWorld) for v in vertices]
    tolerance=_mesh_position_tolerance(fn,points)
    cmds.polyMergeVertex([f'{mesh}.vtx[{v}]' for v in sorted(vertices)],
                         distance=tolerance,constructionHistory=True)
    merged=_mesh_fn(mesh).numVertices<before
    return _edges_on_segments(mesh,segments),merged


def _weld_colliding_strips(mesh,original_vertices,edges):
    """Join generated contours where a corner band overtakes its neighbor.

    Nested branch caps can meet between stages, beyond a single run's rails.
    Only crossed/degenerate generated faces qualify; source vertices remain
    fixed, and the older contour anchors the joined section.
    """
    changed=False
    for _ in range(512):
        fn=_mesh_fn(mesh);points=fn.getPoints(om.MSpace.kWorld);candidates=[]
        polygons = None
        for face in range(fn.numPolygons):
            ids=list(fn.getPolygonVertices(face))
            generated = sum(v >= original_vertices for v in ids)
            if len(ids)<3 or generated<2:continue
            ps=[points[v] for v in ids];normal=om.MVector()
            for i in range(1,len(ps)-1):
                n=(ps[i]-ps[0])^(ps[i+1]-ps[0])
                if n.length()>normal.length():normal=n
            tolerance=_mesh_position_tolerance(fn,ps)
            crossed=normal.length()<tolerance*tolerance
            if not crossed and generated == len(ids) == 3:
                # Maya can round a very thin generated triangle to zero area.
                # Check the native area only when its height is below mesh precision.
                span = max((point - ps[0]).length() for point in ps[1:])
                if normal.length() < tolerance * span:
                    if polygons is None:
                        polygons = om.MItMeshPolygon(fn.dagPath())
                    polygons.setIndex(face)
                    crossed = polygons.getArea(om.MSpace.kWorld) <= tolerance * tolerance
            if not crossed:
                axis=max(range(3),key=lambda k:abs(normal[k]));axes=[k for k in range(3) if k!=axis]
                xy=[(p[axes[0]],p[axes[1]]) for p in ps]
                def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
                for i in range(len(ids)):
                    for j in range(i+2,len(ids)):
                        if i==0 and j==len(ids)-1:continue
                        a,b=xy[i],xy[(i+1)%len(ids)];c,d=xy[j],xy[(j+1)%len(ids)]
                        ab=tolerance*math.dist(a,b);cd=tolerance*math.dist(c,d)
                        x,y,z,w=cross(a,b,c),cross(a,b,d),cross(c,d,a),cross(c,d,b)
                        if ((x>ab and y<-ab or y>ab and x<-ab) and
                                (z>cd and w<-cd or w>cd and z<-cd)):crossed=True;break
                    if crossed:break
            if crossed:
                for a,b in zip(ids,ids[1:]+ids[:1]):
                    if min(a,b)>=original_vertices:candidates.append(((points[a]-points[b]).length(),min(a,b),max(a,b)))
        if not candidates:return edges,changed
        _,keep,remove=min(candidates)
        cmds.xform(f'{mesh}.vtx[{remove}]',worldSpace=True,translation=tuple(points[keep])[:3])
        edges,merged=_weld_contacts(mesh,{keep,remove},edges)
        if not merged:raise SlideError('Не удалось соединить встретившиеся контуры угла.')
        changed=True
    raise SlideError('Слишком много пересечений контуров в выбранном углу.')


def _restore_preview(mesh,data,shading):
    """Restore disposable topology before re-opening a previously welded cut."""
    from .edit_session import assign_materials
    cmds.delete(mesh,constructionHistory=True)
    _mesh_fn(mesh).copyInPlace(data)
    assign_materials(mesh,*shading)


def apply_bevel(amount: float, *, mode: str = "relative",
                _undo: bool = True, _relative_reference=None) -> float:
    plan = analyze_bevel_selection()
    actual = _actual_amount(plan, amount, mode)
    if _undo:
        cmds.undoInfo(openChunk=True, chunkName="Slide Bevel")
    modified = False
    try:
        modified = True
        axes = _box_axes(plan)
        if plan.has_ngon_turn:
            from .multiple import NativeSlideBevel
            actual = NativeSlideBevel(plan.mesh, plan.edge_ids, mode, 1, 50).update(amount)
        elif axes:
            actual = _BoxSupports(plan.mesh, axes, mode, (1.,)).update(amount)
        else:
            plan = _prepare_planar_fans(plan)
            actual = _actual_amount(plan, amount, mode)
            vertices, edges, patch_nodes, transition, extensions, junctions, surfaces = _build(plan)
            contacts=_move(plan, vertices, patch_nodes, transition, extensions, junctions, surfaces, actual, mode,
                           relative_reference=_relative_reference)
            edges,_=_weld_contacts(plan.mesh,contacts,edges)
            edges,_=_weld_colliding_strips(plan.mesh,plan.original_vertex_count,edges)
            cmds.select([f"{plan.mesh}.e[{edge}]" for edge in edges], replace=True)
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


class LiveBevel:
    def __init__(self, plan: BevelPlan, *, mode: str, preview: bool = False,
                 defer_contacts=False, outer_contours=()):
        self.plan = plan
        self.mode = mode
        self.preview = preview
        self._open = False
        self._modified = False
        self._run_vertices = None
        self._new_edges = None
        self._patch_nodes = None
        self._transition = None
        self._extensions = ()
        self._junctions = ()
        self._junction_surfaces = {}
        self.outer_contours = outer_contours
        self.contour_vertices = set()
        self.actual = None
        self.contacts=set();self._merged=False;self._source_plan=plan
        self.defer_contacts=defer_contacts
        self.relative_reference=None
        self.native_points=preview
        axes = _box_axes(plan)
        self._box = _BoxSupports(plan.mesh, axes, mode, (1.,)) if axes else None
        if preview and not defer_contacts:
            from .edit_session import snapshot,materials
            self._base_mesh=snapshot(plan.mesh);self._base_materials=materials(plan.mesh)

    def update(self, amount: float) -> float:
        if self._merged and self.preview:
            _restore_preview(self.plan.mesh,self._base_mesh,self._base_materials)
            self.plan=self._source_plan;self._run_vertices=None;self._merged=False
        actual = _actual_amount(self.plan, amount, self.mode)
        if not self._open:
            if not self.preview:
                cmds.undoInfo(openChunk=True, chunkName="Slide Bevel")
            self._open = True
        try:
            if self._box is not None:
                self._modified = True
                self.actual = self._box.update(amount)
                self._new_edges = self._box.new_edges
                return self.actual
            if self._run_vertices is None:
                self._modified = True
                self.plan = _prepare_planar_fans(self.plan)
                actual = _actual_amount(self.plan, amount, self.mode)
                (self._run_vertices, self._new_edges, self._patch_nodes,
                 self._transition, self._extensions, self._junctions,
                 self._junction_surfaces) = _build(self.plan, self.outer_contours)
                self.contour_vertices = {v for vertices in self._run_vertices for v in vertices}
                self.contour_vertices.update(v for supports in self._transition[2].values()
                                             for _, v, _ in supports)
            self.contacts=_move(self.plan, self._run_vertices, self._patch_nodes,
                                self._transition, self._extensions, self._junctions,
                                self._junction_surfaces, actual, self.mode, native=self.native_points,
                                relative_reference=self.relative_reference)
            if self.preview and not self.defer_contacts:
                self._new_edges,self._merged=_weld_contacts(self.plan.mesh,self.contacts,self._new_edges)
                self._new_edges,joined=_weld_colliding_strips(self.plan.mesh,self._source_plan.original_vertex_count,self._new_edges)
                self._merged|=joined
                cmds.select([f'{self.plan.mesh}.e[{e}]' for e in self._new_edges],replace=True)
            self.actual = actual
            return actual
        except Exception:
            self.cancel()
            raise

    def finish(self):
        if self._open:
            if not self.preview:
                if self._box is None:
                    self._new_edges,self._merged=_weld_contacts(self.plan.mesh,self.contacts,self._new_edges)
                    self._new_edges,_=_weld_colliding_strips(self.plan.mesh,self._source_plan.original_vertex_count,self._new_edges)
                cmds.undoInfo(closeChunk=True)
            self._open = False
        return self.actual

    def cancel(self):
        if self._open:
            if not self.preview:
                cmds.undoInfo(closeChunk=True)
                if self._modified:
                    cmds.undo()
            self._open = False
        self._run_vertices = None
        self._new_edges = None
        self._patch_nodes = None
        self._transition = None
        self._extensions = ()
        self._junctions = ()
        self._junction_surfaces = {}
        if self._box is not None:
            self._box.seed_points = None
        self.contour_vertices.clear()
        self.actual = None
