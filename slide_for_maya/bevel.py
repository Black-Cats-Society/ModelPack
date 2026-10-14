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
    edge_lengths: tuple[float, ...]
    planar_fans: tuple = ()


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
    prepared = analyze_bevel_selection()
    return replace(prepared, planar_fans=tuple(fans))


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
               selected: set[int]) -> list[Plan]:
    polygon = om.MItMeshPolygon(fn.dagPath())
    polygon.setIndex(face)
    vertices = tuple(polygon.getVertices())
    edges = tuple(polygon.getEdges())
    count = len(edges)
    flags = [edge in selected for edge in edges]
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
                boundary = _face_ray_boundary(fn, face, vertex, center)
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


def analyze_bevel_selection() -> BevelPlan:
    mesh, edges = _selected_edges()
    fn = _mesh_fn(mesh)
    edge_faces = _edge_faces(fn)
    for edge in edges:
        if len(edge_faces[edge]) > 2:
            raise SlideError("Non-manifold ребро не поддерживается.")
    edges, collapsed = _across_collapsed_strips(fn, edges, edge_faces)
    selected = set(edges)
    runs = []
    for face in range(fn.numPolygons):
        if face not in collapsed and selected.intersection(_polygon_edges(fn, face)):
            runs.extend(_face_runs(mesh, fn, face, selected))
    if not runs:
        raise SlideError("У выбранных рёбер не найдены соседние грани.")
    pairs = tuple(tuple(fn.getEdgeVertices(edge)) for edge in edges)
    runs.sort(key=lambda run: bool(run.patch_faces))
    lengths = tuple(math.dist(tuple(fn.getPoint(a, om.MSpace.kWorld))[:3],
                              tuple(fn.getPoint(b, om.MSpace.kWorld))[:3])
                    for a, b in pairs)
    return BevelPlan(mesh, edges, pairs, tuple(runs), fn.numVertices, lengths)


def _cap_requests(plan: BevelPlan):
    """Only branch vertices need corner caps; ordinary turns form clean loops."""
    requests = {}
    edge_pairs = dict(zip(plan.edge_ids, plan.edge_vertices))
    selected_degree = {}
    for a, b in plan.edge_vertices:
        selected_degree[a] = selected_degree.get(a, 0) + 1
        selected_degree[b] = selected_degree.get(b, 0) + 1
    for run in plan.runs:
        for index, source in enumerate(run.vertices):
            if selected_degree.get(source, 0) < 3 or (
                    not run.patch_faces and run.guide_faces[index] < 0):
                continue
            adjacent = (run.edge_ids[(index - 1) % len(run.edge_ids)],
                        run.edge_ids[index % len(run.edge_ids)])
            for edge in adjacent:
                a, b = edge_pairs[edge]
                requests[(source, edge)] = b if a == source else a
    return requests


def _actual_amount(plan: BevelPlan, amount: float, mode: str) -> float:
    if not math.isfinite(amount) or amount <= 0:
        raise SlideError("Смещение должно быть положительным числом.")
    face_run_counts = {}
    for run in plan.runs:
        face_run_counts[run.faces[0]] = face_run_counts.get(run.faces[0], 0) + 1
    requests = _cap_requests(plan)
    capped_ends = {edge: sum((vertex, edge) in requests for vertex in pair)
                   for edge, pair in zip(plan.edge_ids, plan.edge_vertices)}
    cap_fractions = [0.49 if count == 2 else _MAX_FRACTION
                     for count in capped_ends.values() if count]
    if mode == "relative":
        run_limits = [0.49 if face_run_counts[run.faces[0]] > 1 else _MAX_FRACTION
                      for run in plan.runs]
        return min(amount, max(run_limits + cap_fractions))
    if mode == "uniform":
        run_limits = []
        for run in plan.runs:
            if run.patch_faces:
                run_limits.append(_patch_limit(run) * _MAX_FRACTION)
            else:
                widths = _normal_distances(run)
                scale = 0.49 if face_run_counts[run.faces[0]] > 1 else _MAX_FRACTION
                run_limits.append((min(widths) if scale == 0.49 else max(widths)) * scale)
        edge_limits = [length * (0.49 if capped_ends[edge] == 2 else _MAX_FRACTION)
                       for edge, length in zip(plan.edge_ids, plan.edge_lengths)
                       if capped_ends[edge]]
        limits = run_limits + edge_limits
        if not limits or max(limits) < _EPS:
            raise SlideError("Не удалось определить ширину для выбранных рёбер.")
        return min(amount, max(limits))
    raise SlideError("Режим должен быть relative или uniform.")


def _segment_insert(fn: om.MFnMesh, start: om.MVector,
                    end: om.MVector, fraction: float, source_vertex=None):
    direction = end - start
    squared = direction * direction
    tolerance = _mesh_position_tolerance(fn, (start, end), math.sqrt(squared))
    parameter_tolerance = tolerance / math.sqrt(squared)
    candidates = []
    # A rail remains a chain attached to its original endpoint after cuts.
    # Walk that chain instead of searching the whole mesh for every endpoint.
    iterator = om.MItMeshVertex(fn.dagPath()) if source_vertex is not None else None
    pending = [source_vertex] if iterator is not None else []
    visited = set()
    checked = set()

    def rail_edges():
        if iterator is None:
            yield from range(fn.numEdges)
            return
        while pending:
            vertex = pending.pop()
            if vertex in visited:
                continue
            visited.add(vertex)
            iterator.setIndex(vertex)
            for edge in iterator.getConnectedEdges():
                if edge not in checked:
                    checked.add(edge)
                    yield edge

    for edge in rail_edges():
        a, b = fn.getEdgeVertices(edge)
        pa = om.MVector(fn.getPoint(a, om.MSpace.kWorld))
        pb = om.MVector(fn.getPoint(b, om.MSpace.kWorld))
        ta = ((pa - start) * direction) / squared
        tb = ((pb - start) * direction) / squared
        if min(ta, tb) < -parameter_tolerance or max(ta, tb) > 1.0 + parameter_tolerance:
            continue
        if ((pa - start) - direction * ta).length() > tolerance:
            continue
        if ((pb - start) - direction * tb).length() > tolerance:
            continue
        # Coincident vertices on a collapsed strip link distinct rails. Crossing
        # that zero-length edge can select the rail on the other side instead.
        if abs(tb - ta) <= _EPS:
            continue
        if iterator is not None:
            pending.extend((a, b))
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
                           om.MVector(*run.ends[index]), fraction, run.vertices[index])


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


def _split_path(plan: BevelPlan, references, seed: float):
    fn = _mesh_fn(plan.mesh)
    inverse = fn.dagPath().inclusiveMatrixInverse()
    run_points = {}
    for run, _ in references:
        if id(run) not in run_points:
            run_points[id(run)] = preview_points(run, seed, "relative")[0]
    seed_points = [run_points[id(run)][index] for run, index in references]
    inserts = []
    reused = set()
    before = fn.numVertices
    for (run, index), point in zip(references, seed_points):
        if run.rails[index] >= 0:
            insertion = _rail_insert(fn, run, index, seed)
            inserts.append(insertion)
            edge, fraction = insertion
            if fraction in (0.0, 1.0):
                reused.add(fn.getEdgeVertices(edge)[int(fraction)])
        else:
            local = om.MPoint(*point) * inverse
            current_face = fn.getClosestPoint(om.MPoint(*point), om.MSpace.kWorld)[1]
            inserts.append((current_face, local.x, local.y, local.z))
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


def _original_edge_ids(plan: BevelPlan, caps: dict[tuple[int, int], int]):
    fn = _mesh_fn(plan.mesh)
    edge_map = {frozenset(fn.getEdgeVertices(edge)): edge for edge in range(fn.numEdges)}
    original = []
    for source_edge, (a, b) in zip(plan.edge_ids, plan.edge_vertices):
        path = [a]
        if (a, source_edge) in caps:
            path.append(caps[(a, source_edge)])
        if (b, source_edge) in caps:
            path.append(caps[(b, source_edge)])
        path.append(b)
        for first, second in zip(path, path[1:]):
            edge = edge_map.get(frozenset((first, second)))
            if edge is None:
                raise SlideError("Исходное выделенное ребро не сохранилось как цепочка.")
            original.append(edge)
    return tuple(original)


def _new_edge_ids(plan: BevelPlan, run_vertices: tuple[tuple[int, ...], ...],
                  caps: dict[tuple[int, int], int], connectors):
    fn = _mesh_fn(plan.mesh)
    edge_map = {frozenset(fn.getEdgeVertices(edge)): edge for edge in range(fn.numEdges)}
    result = []
    for run, vertices in zip(plan.runs, run_vertices):
        for index in range(len(run.edge_ids)):
            edge = edge_map.get(frozenset((vertices[index], vertices[(index + 1) % len(vertices)])))
            if edge is None:
                raise SlideError("Новые рёбра не образовали разрез в грани.")
            result.append(edge)
    for a, b in connectors:
        edge = edge_map.get(frozenset((a, b)))
        if edge is None:
            raise SlideError("Не удалось замкнуть опорные рёбра у угла.")
        result.append(edge)
    _original_edge_ids(plan, caps)
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


def _build(plan: BevelPlan):
    run_vertices = [None] * len(plan.runs)
    patch_nodes = [None] * len(plan.runs)
    for references, positions in _run_paths(plan):
        vertices = _split_path(plan, references, 0.25)
        for run_index, slots in positions.items():
            run_vertices[run_index] = tuple(vertices[slot] for slot in slots)
    for index, run in enumerate(plan.runs):
        if run.patch_faces:
            run_vertices[index], patch_nodes[index] = _split_run(plan, run, 0.25)
    caps = {}
    for (source, edge), target in _cap_requests(plan).items():
        fn = _mesh_fn(plan.mesh)
        start = om.MVector(fn.getPoint(source, om.MSpace.kWorld))
        end = om.MVector(fn.getPoint(target, om.MSpace.kWorld))
        insertion = _segment_insert(fn, start, end, 0.25, source)
        vertex = fn.numVertices
        cmds.polySplit(plan.mesh, insertpoint=[insertion], constructionHistory=True)
        if _mesh_fn(plan.mesh).numVertices != vertex + 1:
            raise SlideError("Не удалось создать вершину опорного угла.")
        caps[(source, edge)] = vertex
    connectors = []
    selected_degree = {}
    for a, b in plan.edge_vertices:
        selected_degree[a] = selected_degree.get(a, 0) + 1
        selected_degree[b] = selected_degree.get(b, 0) + 1
    for run, vertices in zip(plan.runs, run_vertices):
        for index, (source, inserted, face) in enumerate(
                zip(run.vertices, vertices, run.guide_faces)):
            if (not run.patch_faces and face < 0 and
                    selected_degree.get(source, 0) >= 3):
                # A face subdivision can turn a branch corner into the end
                # of a run. Continue its support cut from the rail vertex
                # to the cap on the selected edge of this run.
                edge = run.edge_ids[0] if index == 0 else run.edge_ids[-1]
                cap = caps.get((source, edge))
                if cap is not None:
                    fn = _mesh_fn(plan.mesh)
                    if not any(frozenset(fn.getEdgeVertices(candidate)) ==
                               frozenset((inserted, cap))
                               for candidate in range(fn.numEdges)):
                        cmds.polyConnectComponents(
                            f"{plan.mesh}.vtx[{inserted}]",
                            f"{plan.mesh}.vtx[{cap}]",
                            constructionHistory=True,
                        )
                    connectors.append((inserted, cap))
            adjacent = (run.edge_ids[(index - 1) % len(run.edge_ids)],
                        run.edge_ids[index % len(run.edge_ids)])
            capped = all((source, edge) in caps for edge in adjacent)
            if capped and (run.patch_faces or face >= 0):
                for edge in adjacent:
                    cap = caps[(source, edge)]
                    cmds.polyConnectComponents(
                        f"{plan.mesh}.vtx[{inserted}]", f"{plan.mesh}.vtx[{cap}]",
                        constructionHistory=True,
                    )
                    connectors.append((inserted, cap))
            if run.patch_faces and capped:
                fn = _mesh_fn(plan.mesh)
                diagonal = next((edge for edge in range(fn.numEdges)
                                 if frozenset(fn.getEdgeVertices(edge)) ==
                                 frozenset((source, inserted))), None)
                if diagonal is None:
                    raise SlideError("Не удалось заменить диагональ угла четырёхугольником.")
                cmds.polyDelEdge(f"{plan.mesh}.e[{diagonal}]",
                                 cleanVertices=False, constructionHistory=True)
            elif (not run.patch_faces and face >= 0 and not capped
                  and selected_degree.get(source) == 2):
                # A normal two-edge corner needs a short spoke from the
                # original vertex to its inset point. polySplit creates the
                # contour, but does not split the strip face at this corner.
                fn = _mesh_fn(plan.mesh)
                if not any(frozenset(fn.getEdgeVertices(edge)) ==
                           frozenset((source, inserted))
                           for edge in range(fn.numEdges)):
                    cmds.polyConnectComponents(
                        f"{plan.mesh}.vtx[{source}]",
                        f"{plan.mesh}.vtx[{inserted}]",
                        constructionHistory=True,
                    )
    connectors.extend(_continue_two_edge_corners(plan, run_vertices, selected_degree))
    _restore_planar_fans(plan, run_vertices)
    selected_edges = _new_edge_ids(plan, tuple(run_vertices), caps, connectors)
    return tuple(run_vertices), selected_edges, tuple(patch_nodes), caps


def _move(plan: BevelPlan, run_vertices, patch_nodes, caps,
          amount: float, mode: str, native=False):
    face_run_counts = {}
    for run in plan.runs:
        face_run_counts[run.faces[0]] = face_run_counts.get(run.faces[0], 0) + 1
    targets = {}
    for run, vertices, node in zip(plan.runs, run_vertices, patch_nodes):
        if mode == "uniform" and node is not None:
            local_limit = _patch_limit(run) * _MAX_FRACTION
            cmds.setAttr(node + ".offset", min(amount, local_limit))
            continue
        local_amount = amount
        if face_run_counts[run.faces[0]] > 1:
            if mode == "relative":
                local_amount = min(local_amount, 0.49)
            else:
                limits = (_normal_distances(run) if mode == "uniform" else
                          tuple(math.dist(start, end)
                                for start, end in zip(run.starts, run.ends)))
                local_amount = min(local_amount, min(limits) * 0.49)
        points, _ = preview_points(run, local_amount, mode)
        for vertex, point in zip(vertices, points):
            targets.setdefault(vertex, []).append(point)
    translations = {}
    for vertex, positions in targets.items():
        point = tuple(sum(position[axis] for position in positions) / len(positions)
                      for axis in range(3))
        translations[vertex] = point
    fn = _mesh_fn(plan.mesh)
    edge_pairs = dict(zip(plan.edge_ids, plan.edge_vertices))
    corner_guides = {}
    for run, vertices in zip(plan.runs, run_vertices):
        for index, source in enumerate(run.vertices):
            if not run.patch_faces and run.guide_faces[index] < 0:
                continue
            adjacent = (run.edge_ids[(index - 1) % len(run.edge_ids)],
                        run.edge_ids[index % len(run.edge_ids)])
            for edge in adjacent:
                if (source, edge) in caps:
                    corner_guides.setdefault((source, edge), set()).add(vertices[index])
    for (source, edge), vertex in caps.items():
        a, b = edge_pairs[edge]
        other = b if a == source else a
        start = fn.getPoint(source, om.MSpace.kWorld)
        end = fn.getPoint(other, om.MSpace.kWorld)
        length = (end - start).length()
        limit = 0.49 if sum((endpoint, edge) in caps for endpoint in edge_pairs[edge]) == 2 else _MAX_FRACTION
        fraction = amount if mode == "relative" else amount / length
        guides = corner_guides.get((source, edge), ())
        if len(guides) == 2:
            # Flat junctions need the split of the line joining their two
            # offset corners. Its distance along the spoke depends on angle:
            # three or six branches cannot use the right-angle distance.
            first, second = (om.MVector(*translations[guide]) if guide in translations
                             else om.MVector(fn.getPoint(guide, om.MSpace.kWorld))
                             for guide in guides)
            origin = om.MVector(start)
            tangent = (end - start).normal()
            first -= origin
            second -= origin
            perpendicular = first - tangent * (first * tangent)
            delta = second - first
            perpendicular_delta = delta - tangent * (delta * tangent)
            squared = perpendicular_delta * perpendicular_delta
            if squared > _EPS * _EPS:
                parameter = -(perpendicular * perpendicular_delta) / squared
                intersection = first + delta * parameter
                along = intersection * tangent
                residual = intersection - tangent * along
                if (0 <= parameter <= 1 and along > 0 and
                        residual.length() <= max(length * 1e-6, _EPS)):
                    fraction = along / length
        fraction = min(fraction, limit)
        point = start + (end - start) * fraction
        translations[vertex] = (point.x, point.y, point.z)
    if native:
        points = fn.getPoints(om.MSpace.kWorld)
        for vertex, point in translations.items():
            points[vertex] = om.MPoint(*point)
        fn.setPoints(points, om.MSpace.kWorld)
        fn.updateSurface()
    else:
        for vertex, point in translations.items():
            cmds.xform(f"{plan.mesh}.vtx[{vertex}]", worldSpace=True, translation=point)


def apply_bevel(amount: float, *, mode: str = "relative",
                _undo: bool = True) -> float:
    plan = analyze_bevel_selection()
    actual = _actual_amount(plan, amount, mode)
    if _undo:
        cmds.undoInfo(openChunk=True, chunkName="Slide Bevel")
    modified = False
    try:
        modified = True
        plan = _prepare_planar_fans(plan)
        actual = _actual_amount(plan, amount, mode)
        vertices, edges, patch_nodes, caps = _build(plan)
        _move(plan, vertices, patch_nodes, caps, actual, mode)
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
    def __init__(self, plan: BevelPlan, *, mode: str, preview: bool = False):
        self.plan = plan
        self.mode = mode
        self.preview = preview
        self._open = False
        self._modified = False
        self._run_vertices = None
        self._new_edges = None
        self._patch_nodes = None
        self._caps = None
        self.actual = None

    def update(self, amount: float) -> float:
        actual = _actual_amount(self.plan, amount, self.mode)
        if not self._open:
            if not self.preview:
                cmds.undoInfo(openChunk=True, chunkName="Slide Bevel")
            self._open = True
        try:
            if self._run_vertices is None:
                self._modified = True
                self.plan = _prepare_planar_fans(self.plan)
                actual = _actual_amount(self.plan, amount, self.mode)
                (self._run_vertices, self._new_edges,
                 self._patch_nodes, self._caps) = _build(self.plan)
            _move(self.plan, self._run_vertices, self._patch_nodes,
                  self._caps, actual, self.mode, native=self.preview)
            self.actual = actual
            return actual
        except Exception:
            self.cancel()
            raise

    def finish(self):
        if self._open:
            if not self.preview:
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
        self._caps = None
        self.actual = None
