"""Recover width motion across bevel strips and shared junctions."""
from collections import defaultdict
import numpy as np
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn
from .unbevel_faces import _paths


def manual_sections(mesh, faces):
    """Separate compact junction caps only within the explicit selection."""
    fn = _mesh_fn(mesh)
    points = fn.getPoints(om.MSpace.kWorld)
    ratios = {}
    caps = set()
    for face in faces:
        ring = list(fn.getPolygonVertices(face))
        if len(ring) != 4:
            caps.add(face)
            continue
        lengths = [(points[b] - points[a]).length() for a, b in zip(ring, ring[1:] + ring[:1])]
        a, b = lengths[0] + lengths[2], lengths[1] + lengths[3]
        ratios[face] = max(a, b) / max(min(a, b), 1e-12)
    if any(ratio >= 3 for ratio in ratios.values()):
        caps.update(face for face, ratio in ratios.items() if ratio < 2)
    # Three quad ribbons meeting around one vertex cannot share one global
    # transverse parity. Find that topological cycle even for elongated caps,
    # where an aspect-ratio test alone mistakes a junction for a strip cell.
    polygon = om.MItMeshPolygon(fn.dagPath())
    rings = {}
    owners = defaultdict(list)
    for face in ratios:
        polygon.setIndex(face)
        rings[face] = tuple(polygon.getEdges())
        for index, edge in enumerate(rings[face]):
            owners[edge].append((face, index % 2))
    while True:
        colors, parents = {}, {}
        cycle = None
        for seed in sorted(set(ratios) - caps):
            if seed in colors:
                continue
            colors[seed], parents[seed] = 0, None
            pending = [seed]
            for face in pending:
                for index, edge in enumerate(rings[face]):
                    for other, parity in owners[edge]:
                        if other == face or other in caps:
                            continue
                        expected = colors[face] ^ (index % 2) ^ parity
                        if other not in colors:
                            colors[other], parents[other] = expected, face
                            pending.append(other)
                        elif colors[other] != expected:
                            left, right = [], []
                            a, b = face, other
                            while a is not None:
                                left.append(a)
                                a = parents[a]
                            while b not in left:
                                right.append(b)
                                b = parents[b]
                            cycle = left[:left.index(b) + 1] + right
                            break
                    if cycle:
                        break
                if cycle:
                    break
            if cycle:
                break
        if not cycle:
            break
        caps.add(min(cycle, key=lambda face: (ratios[face], face)))
    strip = tuple(sorted(set(faces) - caps))
    if not strip:
        raise ValueError('Выделите полосу фаски вместе с общим углом, а не только угол.')
    # The far end of the selected strip also needs its shared cap to move;
    # treating that cap as a fixed parent plane would pin the whole strip.
    widths = []
    for face in strip:
        ring = list(fn.getPolygonVertices(face))
        lengths = [(points[b] - points[a]).length() for a, b in zip(ring, ring[1:] + ring[:1])]
        widths.append(min(lengths))
    from statistics import median
    limit = median(widths) * 3
    polygon = om.MItMeshPolygon(fn.dagPath())
    for face in strip:
        polygon.setIndex(face)
        for other in polygon.getConnectedFaces():
            if other in strip or other in caps:
                continue
            ring = list(fn.getPolygonVertices(other))
            if len(ring) not in (3, 4):
                continue
            lengths = [(points[b] - points[a]).length() for a, b in zip(ring, ring[1:] + ring[:1])]
            if max(lengths) <= limit:
                caps.add(other)
    return strip, ((strip,), tuple(sorted(caps)))


def sections(mesh, selected, bands, corners):
    fn = _mesh_fn(mesh)
    points = fn.getPoints(om.MSpace.kWorld)
    polygon = om.MItMeshPolygon(fn.dagPath())
    iterator = om.MItMeshEdge(fn.dagPath())
    face_edges = {}
    edge_faces = {}
    pairs = {}
    for face in set(selected) | set(corners):
        polygon.setIndex(face)
        face_edges[face] = list(polygon.getEdges())
        for edge in face_edges[face]:
            if edge not in pairs:
                pairs[edge] = fn.getEdgeVertices(edge)
                iterator.setIndex(edge)
                edge_faces[edge] = set(iterator.getConnectedFaces())
    base = set(range(fn.numPolygons)) - set(corners) - {face for band in bands for face in band}
    centers, rows = [], []
    remaining = set(selected)
    while remaining:
        seed = min(remaining)
        same_band = set(next(band for band in bands if seed in band))
        parity = {edge: offset % 2 for offset, edge in enumerate(face_edges[seed])}
        pending, patch = [seed], {seed}
        for face in pending:
            edges = face_edges[face]
            shared = next(edge for edge in edges if edge in parity)
            start = edges.index(shared)
            for offset in range(len(edges)):
                edge = edges[(start + offset) % len(edges)]
                value = parity[shared] ^ (offset % 2)
                if parity.setdefault(edge, value) != value or len(edges) != 4:
                    raise ValueError('Полоса фаски должна состоять из согласованных квадов.')
                for neighbor in sorted(edge_faces[edge] & same_band & remaining - patch):
                    patch.add(neighbor)
                    pending.append(neighbor)
        remaining -= patch
        boundary = {e for e in parity if len(edge_faces[e] & patch) == 1}
        def score(direction):
            return sum((1 if edge_faces[e] & base else -1)
                       for e in boundary if parity[e] != direction)
        direction = max((0, 1), key=lambda d: (score(d), -sum(
            (points[a] - points[b]).length() for e, (a, b) in pairs.items() if parity.get(e) == d)))
        paths = _paths([edge for edge in parity if parity[edge] == direction], pairs)
        parents = defaultdict(set)
        for edge in boundary:
            for vertex in pairs[edge]:
                parents[vertex].update(edge_faces[edge] & base)
        for row in paths:
            middle = points[row[0]] + (points[row[-1]] - points[row[0]]) * .5
            half = max((points[v] - middle).length() for v in row)
            normals, offsets = [], []
            for vertex in (row[0], row[-1]):
                for face in sorted(parents[vertex]):
                    normal = fn.getPolygonNormal(face, om.MSpace.kWorld)
                    normals.append(tuple(normal))
                    offsets.append(normal * (points[vertex] - middle))
            if normals:
                delta, _, rank, _ = np.linalg.lstsq(normals, offsets, rcond=.05)
                if rank >= 2 and np.linalg.norm(delta) <= 3 * half:
                    middle += om.MVector(*delta)
            if len(row) >= 5:
                first, last = points[row[1]], points[row[-2]]
                left = (points[row[0]] - first).normal()
                right = (points[row[-1]] - last).normal()
                dot = left * right
                determinant = 1. - dot * dot
                if determinant > .01:
                    span = last - first
                    a = (span * left - dot * (span * right)) / determinant
                    b = (dot * (span * left) - span * right) / determinant
                    pa, pb = first + left * a, last + right * b
                    width = sum((points[v] - points[u]).length() for u, v in zip(row, row[1:]))
                    if (pa - pb).length() < width * .05 and (pa - middle).length() < width * 3:
                        middle = pa + (pb - pa) * .5
            centers.append(middle)
            rows.append(row)
    corner_faces = set(corners)
    linked = {f for face in selected for edge in face_edges[face] for f in edge_faces[edge] & corner_faces}
    pending = sorted(linked)
    for face in pending:
        for edge in face_edges[face]:
            neighbors = edge_faces[edge] & corner_faces - linked
            linked.update(neighbors)
            pending.extend(sorted(neighbors))
    graph = defaultdict(set)
    for edge in {e for f in linked for e in face_edges[f]}:
        a, b = pairs[edge]
        graph[a].add(b)
        graph[b].add(a)
    projectors = surface_projectors(fn, points, set(selected) | linked)
    return centers, rows, graph, projectors, points


def surface_planes(fn, points, excluded):
    planes = []
    for face in range(fn.numPolygons):
        if face in excluded:
            continue
        ring = list(fn.getPolygonVertices(face))
        coordinates = np.asarray([tuple(points[v])[:3] for v in ring])
        center = coordinates.mean(axis=0)
        centered = coordinates - center
        _, values, axes = np.linalg.svd(centered, full_matrices=True)
        if len(values) < 2 or values[1] <= max(values[0] * 1e-7, 1e-10):
            continue
        normal = axes[-1]
        if max(abs(centered @ normal)) > max(values[0] * 1e-6, 1e-8):
            continue
        planes.append((ring, center, normal, max(values[0] * 1e-6, 1e-8)))
    return planes


def surface_projectors(fn, points, excluded):
    """Constrain a vertex to every incident original planar support face."""
    normals = defaultdict(list)
    for ring, _, normal, _ in surface_planes(fn, points, excluded):
        for vertex in ring:
            if not any(abs(normal @ old) > 1 - 5e-11 for old in normals[vertex]):
                normals[vertex].append(normal)
    return _tangent_projectors(normals)


def resampled_projectors(points, planes):
    normals = defaultdict(list)
    for vertex, point in enumerate(points):
        coordinate = np.asarray(tuple(point)[:3])
        for _, origin, normal, tolerance in planes:
            if abs((coordinate - origin) @ normal) <= tolerance:
                if not any(abs(normal @ old) > 1 - 5e-11 for old in normals[vertex]):
                    normals[vertex].append(normal)
    return _tangent_projectors(normals)


def _tangent_projectors(normals):
    result = {}
    for vertex, planes in normals.items():
        _, singular, axes = np.linalg.svd(planes, full_matrices=True)
        tangent = axes[sum(singular > singular[0] * 1e-5):]
        result[vertex] = tangent.T @ tangent
    return result


def constrained_motion(projectors, vertex, vector):
    projection = projectors.get(vertex)
    return vector if projection is None else om.MVector(*(projection @ tuple(vector)))


def normal_motion(fn, selected, points):
    """Offset an explicit face network locally, translating its support rows.

    A curved longitudinal loop cannot stay parallel to every original edge
    while changing width. Use the local bevel normal instead of imposing that
    global constraint. Coplanar lands inherit the motion of their core rim so
    their original transverse gaps do not scale with the bevel width.
    """
    selected = set(selected)
    polygon = om.MItMeshPolygon(fn.dagPath())
    rings, edges, owners = {}, {}, defaultdict(set)
    normals = {}
    for face in range(fn.numPolygons):
        polygon.setIndex(face)
        rings[face] = list(fn.getPolygonVertices(face))
        edges[face] = list(polygon.getEdges())
        normals[face] = fn.getPolygonNormal(face, om.MSpace.kWorld)
        for edge in edges[face]:
            owners[edge].add(face)
    # Only lands connected tangentially to the unselected parent surface are
    # supports; a flat chamfer itself must remain part of the moving core.
    lands = set()
    pending = []
    for face in selected:
        if any(normals[face] * normals[other] > .99995
               for edge in edges[face] for other in owners[edge] - selected):
            lands.add(face)
            pending.append(face)
    for face in pending:
        for edge in edges[face]:
            for other in (owners[edge] & selected) - lands:
                if normals[face] * normals[other] > .99995:
                    lands.add(other)
                    pending.append(other)
    core = selected - lands
    if not core:
        raise ValueError('Выделите фаску вместе с её опорными рёбрами, а не только поверхность.')
    core_vertices = {v for face in core for v in rings[face]}
    moving = {v for face in selected for v in rings[face]}
    field = defaultdict(lambda: om.MVector())
    widths = defaultdict(list)
    constraints = defaultdict(list)
    for face, ring in rings.items():
        for i, vertex in enumerate(ring):
            if vertex not in moving:
                continue
            if face in core:
                lengths = [(points[b] - points[a]).length()
                           for a, b in zip(ring, ring[1:] + ring[:1])]
                widths[vertex].append(min(lengths))
                a = (points[ring[i - 1]] - points[vertex]).normal()
                b = (points[ring[(i + 1) % len(ring)]] - points[vertex]).normal()
                angle = np.arccos(max(-1., min(1., a * b)))
                field[vertex] -= normals[face] * angle
            else:
                normal = np.asarray(tuple(normals[face]))
                if not any(abs(normal @ old) > 1 - 5e-11 for old in constraints[vertex]):
                    constraints[vertex].append(normal)
    # Follow transverse edges of the lands from each core rim. Union only
    # across the land, never along the longitudinal bevel loop.
    parent = {v: v for v in moving}
    def root(v):
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v
    parity = {}
    pending = []
    for face in sorted(lands):
        if len(edges[face]) != 4:
            continue
        rim = max((e for e in edges[face] if owners[e] & core),
                  key=lambda e: (points[fn.getEdgeVertices(e)[1]] - points[fn.getEdgeVertices(e)[0]]).length(),
                  default=None)
        if rim is not None:
            parity[face] = edges[face].index(rim) % 2
            pending.append(face)
    for face in pending:
        lengths = [(points[b] - points[a]).length() for a, b in
                   (fn.getEdgeVertices(e) for e in edges[face])]
        along = sum(lengths[i] for i in range(4) if i % 2 == parity[face])
        across = sum(lengths[i] for i in range(4) if i % 2 != parity[face])
        if along < across * 1.5:
            continue
        for i, edge in enumerate(edges[face]):
            if i % 2 != parity[face]:
                a, b = fn.getEdgeVertices(edge)
                parent[root(a)] = root(b)
            for other in ((owners[edge] & lands) - parity.keys() if i % 2 == parity[face] else ()):
                if len(edges[other]) == 4:
                    parity[other] = edges[other].index(edge) % 2 ^ (i % 2) ^ parity[face]
                    pending.append(other)
    groups = defaultdict(list)
    for vertex in sorted(moving):
        groups[root(vertex)].append(vertex)
    vectors = [om.MVector() for _ in points]
    support_groups = []
    # Collapse transverse support rows into nodes of the junction graph.
    neighbors = defaultdict(dict)
    for face in lands:
        ring = rings[face]
        for a, b in zip(ring, ring[1:] + ring[:1]):
            aroot, broot = root(a), root(b)
            length = (points[b] - points[a]).length()
            neighbors[aroot][broot] = length
            neighbors[broot][aroot] = length
    from statistics import median
    typical_width = median(median(values) for values in widths.values())
    projections, fixed = {}, {}
    for key, group in groups.items():
        anchors = [v for v in group if v in core_vertices]
        direction = sum((field[v].normal() for v in anchors), om.MVector())
        planes = [n for v in group for n in constraints[v]]
        projection = np.eye(3)
        if planes:
            _, singular, axes = np.linalg.svd(planes, full_matrices=True)
            tangent = axes[sum(singular > singular[0] * .05):]
            projection = tangent.T @ tangent
            direction = om.MVector(*(projection @ tuple(direction)))
        if direction.length() > 1e-9:
            direction.normalize()
        if anchors:
            direction *= median(median(widths[v]) for v in anchors) / typical_width
        projections[key] = projection
        if anchors:
            fixed[key] = np.asarray(tuple(direction))
        for vertex in group:
            vectors[vertex] = om.MVector(direction)
        if len(group) > 1:
            support_groups.append(tuple(group))
    # Smooth the motion through compact junctions in their local tangent
    # spaces. Nearest-neighbor copying leaves discontinuities in narrow lands.
    free = sorted(set(groups) - set(fixed))
    slots, bases, size = {}, {}, 0
    for key in free:
        _, values, axes = np.linalg.svd(projections.get(key, np.zeros((3, 3))))
        basis = axes[values > .5].T
        slots[key], bases[key] = size, basis
        size += basis.shape[1]
    equations, targets = [], []
    for a in sorted(neighbors):
        for b, length in sorted(neighbors[a].items()):
            if a >= b or (a in fixed and b in fixed):
                continue
            block, target = np.zeros((3, size)), np.zeros(3)
            for key, sign in ((a, 1.), (b, -1.)):
                if key in fixed:
                    target -= fixed[key] * sign
                else:
                    start, basis = slots[key], bases[key]
                    block[:, start:start + basis.shape[1]] += sign * basis
            weight = 1 / max(length, 1e-8) ** .5
            equations.extend(block * weight)
            targets.extend(target * weight)
    if size and equations:
        solution = np.linalg.lstsq(equations, targets, rcond=1e-7)[0]
        for key in free:
            start, basis = slots[key], bases[key]
            value = om.MVector(*(basis @ solution[start:start + basis.shape[1]]))
            for vertex in groups[key]:
                vectors[vertex] = value
    return vectors, tuple(sorted(moving)), tuple(support_groups)


def align_strip_motion(fn, faces, rows, vectors, projectors, points):
    """Project the width field onto parallel longitudinal edge motion."""
    vertices = sorted({v for row in rows for v in row})
    transverse = {frozenset((a, b)) for row in rows for a, b in zip(row, row[1:])}
    edges = {tuple(sorted((a, b))) for face in faces for ring in [list(fn.getPolygonVertices(face))]
             for a, b in zip(ring, ring[1:] + ring[:1]) if frozenset((a, b)) not in transverse}
    from .bevel_detection import _components

    neighbors = defaultdict(set)
    for a, b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)
    for connected in _components(vertices, neighbors):
        chain = sorted(connected)
        slots = {v: 3 * i for i, v in enumerate(chain)}
        size = 3 * len(chain)
        constraints = []
        for a, b in sorted(edges):
            if a not in connected:
                continue
            axis = np.asarray(tuple((points[b] - points[a]).normal()))
            perpendicular = np.eye(3) - np.outer(axis, axis)
            block = np.zeros((3, size))
            block[:, slots[a]:slots[a] + 3] = perpendicular
            block[:, slots[b]:slots[b] + 3] = -perpendicular
            constraints.extend(block)
        for vertex in chain:
            projection = projectors.get(vertex)
            if projection is not None:
                block = np.zeros((3, size))
                block[:, slots[vertex]:slots[vertex] + 3] = np.eye(3) - projection
                constraints.extend(block)
        if not constraints:
            continue
        matrix = np.asarray(constraints)
        target = np.asarray([value for v in chain for value in vectors[v]])
        correction = np.linalg.lstsq(matrix, matrix @ target, rcond=1e-6)[0]
        result = (target - correction).reshape((-1, 3))
        for vertex, value in zip(chain, result):
            vectors[vertex] = om.MVector(*value)


def junction_motion(graph, boundary, vectors, projectors, points):
    """Extend selected-strip displacements across the common corner mesh."""
    remaining = set(graph)
    moving = []
    while remaining:
        seed = min(remaining)
        component, pending = {seed}, [seed]
        remaining.remove(seed)
        for vertex in pending:
            neighbors = graph[vertex] & remaining
            remaining.difference_update(neighbors)
            component.update(neighbors)
            pending.extend(sorted(neighbors))
        anchors = sorted(component & boundary)
        if not anchors:
            continue
        origin = np.mean([tuple(points[v])[:3] for v in component], axis=0)
        scale = max((np.linalg.norm(np.asarray(tuple(points[v])[:3]) - origin) for v in component),
                    default=1.)
        scale = max(scale, 1e-10)
        def row(vertex):
            return np.append((np.asarray(tuple(points[vertex])[:3]) - origin) / scale, 1.)
        # Fit one affine displacement field for the junction, preserving
        # straight grid lines instead of independently smoothing vertices.
        def design(vertex):
            return np.kron(row(vertex).reshape(1, 4), np.eye(3))
        matrix = np.vstack([design(v) for v in anchors])
        target = np.asarray([tuple(vectors[v]) for v in anchors]).reshape(-1)
        constraints = []
        for vertex in sorted(component):
            projection = projectors.get(vertex)
            if projection is not None:
                constraints.extend((np.eye(3) - projection) @ design(vertex))
        tangent = np.eye(12)
        if constraints:
            _, singular, axes = np.linalg.svd(np.asarray(constraints), full_matrices=True)
            rank = sum(singular > max(singular[0] * 1e-7, 1e-10))
            tangent = axes[rank:].T
        # Solve the affine field WITH the incident-plane constraints. Applying
        # separate projections afterwards bends an originally straight row.
        system = np.vstack((matrix, np.eye(12) * 1e-4)) @ tangent
        target = np.append(target, np.zeros(12))
        transform = tangent @ np.linalg.lstsq(system, target, rcond=1e-7)[0]
        for vertex in sorted(component - boundary):
            vectors[vertex] = om.MVector(*(design(vertex) @ transform))
            moving.append(vertex)
    return tuple(moving)


def preserve_collapsed_caps(fn, boundary, vectors, points):
    """Keep an existing collinear cap on its original edge interpolation."""
    moved = set()
    for face in range(fn.numPolygons):
        ring = list(fn.getPolygonVertices(face))
        if len(ring) != 3 or not any(vectors[v].length() > 1e-12 for v in ring):
            continue
        a, b = max(((a, b) for a in ring for b in ring if a < b),
                   key=lambda pair: (points[pair[0]] - points[pair[1]]).length())
        c = next(v for v in ring if v not in (a, b))
        axis = points[b] - points[a]
        squared = axis * axis
        if squared < 1e-20 or ((points[c] - points[a]) ^ axis).length() > squared * 1e-5:
            continue
        t = ((points[c] - points[a]) * axis) / squared
        if c not in boundary:
            vectors[c] = vectors[a] * (1 - t) + vectors[b] * t
            moved.add(c)
        elif b not in boundary and t > 1e-8:
            vectors[b] = (vectors[c] - vectors[a] * (1 - t)) / t
            moved.add(b)
        elif a not in boundary and 1 - t > 1e-8:
            vectors[a] = (vectors[c] - vectors[b] * t) / (1 - t)
            moved.add(a)
    return moved
