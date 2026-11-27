"""Create and move multiple support or clone rows along selected edges."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import replace

import maya.cmds as cmds
from maya.api import OpenMaya as om

from .bevel import (LiveBevel, analyze_bevel_selection, apply_bevel, _BoxSupports, _box_axes, _connect,
                    _use_native_bevel,
                    _insert_between, _prepare_planar_fans,
                    _weld_contacts, _weld_colliding_strips)
from .core import (LiveEdit, SlideError, _EPS, _mesh_fn, _mesh_position_tolerance, _selected_edges, _combined_plan,_normal_distances,
                   analyze_selection, apply)


def _segments(mesh, edge_ids):
    fn = _mesh_fn(mesh)
    points = fn.getPoints(om.MSpace.kWorld)
    return tuple(
        (tuple(points[a])[:3], tuple(points[b])[:3])
        for edge in edge_ids
        for a, b in (fn.getEdgeVertices(edge),)
    )


def _has_reflex_fold(fn, edges):
    """Find a folded junction whose flat sector wraps around a re-entrant tip."""
    selected = set(edges)
    points = fn.getPoints(om.MSpace.kWorld)
    corners = {v for edge in selected for v in fn.getEdgeVertices(edge)}
    vertex = om.MItMeshVertex(fn.dagPath())
    rail = om.MItMeshEdge(fn.dagPath())
    for corner in corners:
        vertex.setIndex(corner)
        incident = set(vertex.getConnectedEdges())
        unselected = incident - selected
        if len(incident) != 4 or len(unselected) != 1:
            continue
        rail.setIndex(next(iter(unselected)))
        sides = rail.getConnectedFaces()
        if len(sides) != 2:
            continue
        normals = [fn.getPolygonNormal(face, om.MSpace.kWorld) for face in sides]
        if normals[0] * normals[1] < .999:
            continue
        angle = 0.
        for face, normal in zip(sides, normals):
            ring = tuple(fn.getPolygonVertices(face))
            index = ring.index(corner)
            before = (points[ring[index - 1]] - points[corner]).normal()
            after = (points[ring[(index + 1) % len(ring)]] - points[corner]).normal()
            turn = math.atan2((after ^ before) * normal, before * after)
            angle += turn % math.tau
        if angle > math.pi + .05:
            return True
    return False


def _vertex_pairs(mesh, edge_ids):
    fn = _mesh_fn(mesh)
    return tuple(tuple(fn.getEdgeVertices(edge)) for edge in edge_ids)


def _hard_segments(mesh):
    fn = _mesh_fn(mesh)
    return _segments(mesh, (edge for edge in range(fn.numEdges) if not fn.isEdgeSmooth(edge)))


def _preserve_hard_edges(mesh, segments):
    if not segments:
        return
    fn = _mesh_fn(mesh)
    edges = [edge for edge in _edges_on_segments(mesh, segments) if fn.isEdgeSmooth(edge)]
    if edges:
        cmds.polySoftEdge([f'{mesh}.e[{edge}]' for edge in edges], angle=0, constructionHistory=True)


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
    if not positions or not segments:
        return ()
    axis = max(range(3), key=lambda k: max(p[k] for p in positions) -
                                     min(p[k] for p in positions))
    pairs = [fn.getEdgeVertices(edge) for edge in range(fn.numEdges)]
    ordered = sorted((positions[a][axis], edge) for edge, (a, _) in enumerate(pairs))
    coordinates = [coordinate for coordinate, _ in ordered]
    result = set()
    for start, end in segments:
        direction = tuple(y - x for x, y in zip(start, end))
        squared = sum(axis * axis for axis in direction)
        if squared < 1e-12:
            continue
        tolerance = _mesh_position_tolerance(fn, (start, end), math.sqrt(squared))
        tolerance_squared = tolerance * tolerance
        padding = tolerance + abs(direction[axis]) * 1e-5
        low = min(start[axis], end[axis]) - padding
        high = max(start[axis], end[axis]) + padding
        vx, vy, vz = direction
        for index in range(bisect_left(coordinates, low), bisect_right(coordinates, high)):
            edge = ordered[index][1]
            if edge in result:
                continue
            a, b = pairs[edge]
            if not low <= positions[b][axis] <= high:
                continue
            for point in (positions[a], positions[b]):
                dx, dy, dz = point.x - start[0], point.y - start[1], point.z - start[2]
                position = (dx * vx + dy * vy + dz * vz) / squared
                if not -1e-5 <= position <= 1.0 + 1e-5:
                    break
                if ((dx - vx * position) ** 2 + (dy - vy * position) ** 2 +
                        (dz - vz * position) ** 2 > tolerance_squared):
                    break
            else:
                result.add(edge)
    return tuple(sorted(result))


def _distribution_fraction(layer: int, count: int, percent: float) -> float:
    """Map equal positions through a monotone bias anchored at 50% in the middle."""
    if layer == count:
        return 1.0
    position = layer / count
    bias = percent / 100.0
    return bias * position / (bias * position + (1.0 - bias) * (1.0 - position))


def _bevel_fractions(count, percent):
    """Place the main row at the requested width and space the rest beyond it."""
    spacing = percent / 100.
    return tuple(1. + layer * spacing for layer in range(count - 1, -1, -1))


def _validate_options(count, mode, bias_percent, *, bevel=False):
    if not isinstance(count, int) or not 1 <= count <= 16:
        raise SlideError("Количество рёбер должно быть целым числом от 1 до 16.")
    if not math.isfinite(bias_percent) or not 0.0 <= bias_percent <= 100.0:
        raise SlideError("Положение промежуточных рёбер должно быть от 0 до 100%.")
    if mode not in ("uniform", "relative"):
        raise SlideError("Режим должен быть uniform или relative.")
    # Zero spacing combines all rows. Clone also combines them at 100%.
    return 1 if bias_percent == 0 or (not bevel and bias_percent == 100) else count


def _stop_crossing_faces(topology, start, end, tolerance):
    """Stop moving corners at the first crossing of two sides of a face."""
    from itertools import islice
    import numpy as np
    from model_pack_bevel.collision import _roots

    def cross(a, b):
        return a[0] * b[1] - a[1] * b[0]

    def subtract(a, b):
        return a[0] - b[0], a[1] - b[1]

    def orientation(a, b, c, delta):
        u, v = subtract(a, b), subtract(c, b)
        du, dv = subtract(delta[0], delta[1]), subtract(delta[2], delta[1])
        return (cross(u, v), cross(du, v) + cross(u, dv), cross(du, dv))

    def crossing(first, second, third, fourth, origin, velocity):
        triples = ((first, second, third), (first, second, fourth),
                   (third, fourth, first), (third, fourth, second))
        polynomials = [orientation(*(origin[i] for i in triple),
                                   [velocity[i] for i in triple]) for triple in triples]
        boundaries = sorted({0., 1., *(root for coefficients in polynomials
                                        for root in _roots(coefficients))})
        for low, high in zip(boundaries, boundaries[1:]):
            t = (low + high) * .5
            values = [a + t * (b + t * c) for a, b, c in polynomials]
            if values[0] * values[1] < 0 and values[2] * values[3] < 0:
                return low
        return None

    counts, indices = topology
    vertices = iter(indices)
    motions = [b - a for a, b in zip(start, end)]
    faces = []
    for count in counts:
        face = tuple(islice(vertices, count))
        if count < 4 or not any(motions[v].length() > tolerance for v in face):
            continue
        points = [start[v] for v in face]
        normal = om.MVector()
        for i in range(1, count - 1):
            normal += (points[i] - points[0]) ^ (points[i + 1] - points[0])
        if normal.length() <= tolerance * tolerance:
            continue
        coords = np.asarray([tuple(point - points[0])[:3] for point in points])
        faces.append((face, coords))
    stopped = {}
    for _ in range(8):
        events = []
        for face, coords in faces:
            count = len(face)
            directions = np.asarray([tuple(motions[v])[:3] for v in face])
            for sample in (coords, coords + directions):
                _, _, basis = np.linalg.svd(sample - sample.mean(axis=0), full_matrices=False)
                origin = (coords @ basis[:2].T).tolist()
                velocity = (directions @ basis[:2].T).tolist()
                for i in range(count - 2):
                    for j in range(i + 2, count - (i == 0)):
                        sides = i, i + 1, j, (j + 1) % count
                        t = crossing(*sides, origin, velocity)
                        if t is not None and t > 0.:
                            events.append((t, tuple(face[k] for k in sides)))
        if not events:
            break
        limits = {}
        for t, corners in events:
            for vertex in corners:
                length = motions[vertex].length()
                if length > tolerance:
                    safe = max(0., t - 16. * tolerance / length)
                    limits[vertex] = min(limits.get(vertex, 1.), safe)
        if not limits:
            break
        for vertex, fraction in limits.items():
            motions[vertex] *= fraction
            stopped[vertex] = start[vertex] + motions[vertex]

    # On a warped face, stopping one end of a row changes the plane of
    # the face. Check the resulting polygon and back off its corners together.
    for _ in range(8):
        corrections = {}
        for face, coords in faces:
            directions = np.asarray([tuple(motions[v])[:3] for v in face])
            sample = coords + directions
            _, _, basis = np.linalg.svd(sample - sample.mean(axis=0), full_matrices=False)
            ring = (sample @ basis[:2].T).tolist()
            count = len(face)
            crossed = False
            for i in range(count - 2):
                for j in range(i + 2, count - (i == 0)):
                    a, b, c, d = (ring[k] for k in (i, i + 1, j, (j + 1) % count))
                    ab, cd = subtract(b, a), subtract(d, c)
                    x, y = cross(ab, subtract(c, a)), cross(ab, subtract(d, a))
                    z, w = cross(cd, subtract(a, c)), cross(cd, subtract(b, c))
                    if (x * y < 0 and z * w < 0
                            and min(abs(x), abs(y)) > tolerance * math.hypot(*ab)
                            and min(abs(z), abs(w)) > tolerance * math.hypot(*cd)):
                        crossed = True
                        break
                if crossed:
                    break
            if crossed:
                for vertex in face:
                    if motions[vertex].length() > tolerance:
                        corrections[vertex] = .95
        if not corrections:
            break
        for vertex, fraction in corrections.items():
            motions[vertex] *= fraction
            stopped[vertex] = start[vertex] + motions[vertex]
    return stopped


class _ParallelRows:
    """Keep a support at one perpendicular offset along its source edge."""

    def __init__(self, edges, seed, zero, source_edges, tolerance):
        import numpy as np

        pairs = np.asarray(edges, dtype=int).reshape((-1, 2))
        widths = {}
        for start, end in source_edges:
            start = np.asarray(start)
            direction = np.asarray(end) - start
            length = np.linalg.norm(direction)
            if length < tolerance * 16.:
                continue
            tangent = direction / length
            traces = zero[pairs] - start
            distance = traces @ tangent
            error = np.linalg.norm(traces - distance[:, :, None] * tangent, axis=2)
            matches = ((error.max(axis=1) <= tolerance * 3.)
                       & (distance.min(axis=1) >= -tolerance * 3.)
                       & (distance.max(axis=1) <= length + tolerance * 3.)
                       & (abs(distance[:, 1] - distance[:, 0]) > tolerance * 16.))
            for a, b in pairs[matches]:
                section = seed[[a, b]] - start
                offsets = section - (section @ tangent)[:, None] * tangent
                normal = offsets.mean(axis=0)
                span = np.linalg.norm(normal)
                if span < tolerance * 8.:
                    continue
                normal /= span
                depth = offsets @ normal
                if (depth.min() < tolerance * 4.
                        or np.linalg.norm(offsets - depth[:, None] * normal, axis=1).max() > tolerance * 3.):
                    continue
                widths.setdefault((a, b), (normal, start @ normal))

        self._setup(widths, seed, zero, tolerance)

    @classmethod
    def from_guides(cls, guides, seed, zero, tolerance, source_faces):
        rows = cls.__new__(cls)
        rows._setup(guides, seed, zero, tolerance, source_faces)
        return rows

    def _setup(self, widths, seed, zero, tolerance, source_faces=None):
        import numpy as np

        by_vertex = {}
        groups = list(range(len(widths)))
        for index, ((a, b), (normal, _)) in enumerate(widths.items()):
            by_vertex.setdefault(a, []).append((index, 0, normal))
            by_vertex.setdefault(b, []).append((index, 1, normal))

        def group(index):
            while groups[index] != index:
                index = groups[index]
            return index

        for constraints in by_vertex.values():
            for index, (row, _, normal) in enumerate(constraints):
                for previous, _, other in constraints[:index]:
                    if normal @ other >= 1. - 1e-9:
                        groups[group(previous)] = group(row)
        self.groups = np.asarray([group(index) for index in range(len(groups))], dtype=int)
        self.pairs = np.asarray(list(widths), dtype=int).reshape((-1, 2))
        self.normals = np.asarray([normal for normal, _ in widths.values()]).reshape((-1, 3))
        self.offsets = np.asarray([offset for _, offset in widths.values()])
        vertices, rows, ends, bases = [], [], [], []
        for vertex, constraints in by_vertex.items():
            constraints = list({self.groups[row]: (row, end, normal)
                                for row, end, normal in constraints}.values())
            if len(constraints) > 2:
                continue
            indices, sides, normals = zip(*constraints)
            normals = np.asarray(normals)
            rail = seed[vertex] - zero[vertex]
            if len(indices) == 1:
                depth = normals[0] @ rail
                if depth < tolerance * 4.:
                    continue
                basis = np.column_stack((rail / depth, np.zeros(3)))
                indices += indices
                sides += sides
            else:
                # The corner is the intersection of the two offset lines.
                if abs(normals[0] @ normals[1]) > .999:
                    continue
                basis = np.linalg.pinv(normals)
                same_face = source_faces is not None and source_faces[indices[0]] == source_faces[indices[1]]
                if not same_face and np.linalg.norm(rail - basis @ (normals @ rail)) > tolerance * 3.:
                    continue
            vertices.append(vertex)
            rows.append(indices)
            ends.append(sides)
            bases.append(basis)
        self.vertices = np.asarray(vertices, dtype=int)
        self.rows = np.asarray(rows, dtype=int).reshape((-1, 2))
        self.ends = np.asarray(ends, dtype=int).reshape((-1, 2))
        self.bases = np.asarray(bases).reshape((-1, 3, 2))

    def align(self, points):
        import numpy as np

        offsets = np.einsum('eij,ej->ei', points[self.pairs], self.normals) - self.offsets[:, None]
        widths = np.full(len(self.pairs), np.inf)
        np.minimum.at(widths, self.groups, offsets.min(axis=1))
        shifts = widths[self.groups, None] - offsets
        points[self.vertices] += np.einsum('ijk,ik->ij', self.bases, shifts[self.rows, self.ends])


class _RowContacts:
    """Freeze independent supports in contact order, then interpolate the corner rows."""

    def __init__(self, topology, seed, base, interval, interpolation, tolerance,
                 row_weights=None, layer_motion=(), keep_convex=False, parallel=None,
                 dependent_vertices=(), minimum_depth=0., local_edge_contacts=False,
                 surface_contact=None):
        from itertools import islice
        import numpy as np

        self.seed = np.asarray([tuple(point)[:3] for point in seed])
        self.velocity = (np.asarray([tuple(point)[:3] for point in base]) - self.seed) / interval
        for vertices, scale, offset in layer_motion:
            self.velocity[vertices] /= scale
            self.seed[vertices] += self.velocity[vertices] * offset
        self.interpolation = interpolation
        self.parallel = parallel
        self.dependent_vertices = dependent_vertices
        self.row_weights = row_weights
        self.tolerance = tolerance
        self.minimum_depth = minimum_depth
        self.local_edge_contacts = local_edge_contacts
        self.surface_contact = surface_contact
        self.surface_faces = set()
        self.limits = np.full(len(seed), np.inf)
        speeds = np.linalg.norm(self.velocity, axis=1)
        self.speed = speeds.max()
        self.anchors = [{vertex} if speed * interval > tolerance else set()
                        for vertex, speed in enumerate(speeds)]
        for vertex, weights in interpolation.items():
            self.anchors[vertex] = set().union(*(self.anchors[index] for index, weight in weights if weight > 0.))

        points = self.points(0.)
        pairs, edges, face_groups, convex_quads = [], set(), {}, []
        counts, indices = topology
        vertices = iter(indices)
        for count in counts:
            face = tuple(islice(vertices, count))
            if not any(self.anchors[v] for v in face):
                continue
            edges.update(tuple(sorted((a, b))) for a, b in zip(face, face[1:] + face[:1])
                         if self.anchors[a] or self.anchors[b])
            first = len(pairs)
            if count == 3:
                pairs.append((*face, face[2]))
            for i in range(count - 2):
                for j in range(i + 2, count - (i == 0)):
                    pairs.append((face[i], face[i + 1], face[j], face[(j + 1) % count]))
            face_groups.setdefault(count, []).append((face, tuple(range(first, len(pairs)))))
            if keep_convex and count == 4:
                convex_quads.append(face)
        self.edges = np.asarray(sorted(edges), dtype=int).reshape((-1, 2))
        self.pairs = np.asarray(pairs, dtype=int).reshape((-1, 4))
        self.bases = np.empty((len(pairs), 2, 3))
        self.projection_owner = np.full(len(pairs), -1, dtype=int)
        face_sizes, face_vertices, face_normals = [], [], []
        for count, group in face_groups.items():
            rings, ids = zip(*group)
            rings, ids = np.asarray(rings), np.asarray(ids).ravel()
            vertices = points[rings]
            _, _, basis = np.linalg.svd(vertices - vertices.mean(axis=1, keepdims=True), full_matrices=False)
            owners = np.repeat(np.arange(len(rings)), len(ids) // len(rings))
            self.bases[ids] = basis[owners, :2]
            if count >= 4:
                self.projection_owner[ids] = len(face_sizes) + owners
                face_sizes.extend([count] * len(rings))
                face_vertices.extend(rings.ravel())
                face_normals.extend(basis[:, 2])
        self.projection_sizes = np.asarray(face_sizes, dtype=int)
        self.projection_starts = np.cumsum(self.projection_sizes) - self.projection_sizes
        self.projection_vertices = np.asarray(face_vertices, dtype=int)
        self.projection_faces = np.repeat(np.arange(len(face_sizes)), self.projection_sizes)
        self.projection_normals = np.asarray(face_normals).reshape((-1, 3))
        self.convex_quads = np.asarray(convex_quads, dtype=int).reshape((-1, 4))
        initial = self._quad_turns(points)
        convex = (initial >= -self.tolerance).all(axis=1)
        self.convex_quads = self.convex_quads[convex]
        self.quad_turn_floor = np.clip(initial[convex] * .5, -self.tolerance, self.tolerance)

    def _quad_turns(self, points):
        """Signed turn depths in each current quad's mean plane."""
        import numpy as np

        rings = points[self.convex_quads]
        span = rings - rings[:, :1]
        normal = np.cross(span[:, 1], span[:, 2]) + np.cross(span[:, 2], span[:, 3])
        normal /= np.maximum(np.linalg.norm(normal, axis=1)[:, None], 1e-30)
        sides = np.roll(rings, -1, axis=1) - rings
        outgoing = np.roll(sides, -1, axis=1)
        lengths = np.maximum(np.linalg.norm(sides, axis=2), np.linalg.norm(outgoing, axis=2))
        return np.einsum('ijk,ik->ij', np.cross(sides, outgoing), normal) / np.maximum(lengths, 1e-30)

    def points(self, width):
        import numpy as np

        points = self.seed + self.velocity * np.minimum(width, self.limits)[:, None]
        weights_by_vertex = self.row_weights(width, self.limits) if self.row_weights else self.interpolation
        for vertex, weights in weights_by_vertex.items():
            points[vertex] = sum(points[index] * weight for index, weight in weights)
        if self.parallel is not None:
            self.parallel.align(points)
            for vertex in self.dependent_vertices:
                points[vertex] = sum(points[index] * weight for index, weight in weights_by_vertex[vertex])
        return points

    def _first_contacts(self, start, end):
        import numpy as np

        displacement = end - start
        motions = displacement[self.pairs]
        deforming = np.any(motions != motions[:, :1], axis=(1, 2))
        times = np.full(len(self.pairs), np.inf)
        times[deforming] = self._crossing_times(start[self.pairs[deforming]], motions[deforming], self.bases[deforming])
        a, b = self.edges.T
        segments = start[b] - start[a]
        closing = displacement[b] - displacement[a]
        speed2 = np.einsum('ij,ij->i', closing, closing)
        nearest = -np.einsum('ij,ij->i', segments, closing) / np.maximum(speed2, 1e-30)
        separation = np.linalg.norm(segments + closing * nearest[:, None], axis=1)
        # Opposing rows can meet along a shared rail without crossing sides.
        meets = (separation <= self.tolerance) & (nearest > 0.) & (nearest <= 1.)
        edge_times = np.where(meets, nearest, np.inf)
        first = min(times.min(initial=np.inf), edge_times.min(initial=np.inf))
        # A warped face can cross in its current plane while its projection
        # on the seed plane still fits. Search up to the next contact, where
        # all positions still follow their current linear motion.
        high = min(first, 1.)
        moving = np.linalg.norm(motions, axis=2).max(axis=1) > self.tolerance * .25
        crossed = self._projected_crossings(start + displacement * high, moving)
        if len(crossed):
            low = 0.
            for _ in range(15):
                middle = (low + high) * .5
                trial = self._projected_crossings(start + displacement * middle, moving)
                if len(trial):
                    high, crossed = middle, trial
                else:
                    low = middle
            if high < first:
                vertices = self.pairs[crossed].ravel()
                return high, set().union(*(self.anchors[v] for v in vertices))
        if not np.isfinite(first):
            return first, set()
        collapsed = self.edges[edge_times <= first + 1e-8]
        crossings = self.pairs[times <= first + 1e-8]
        if self.local_edge_contacts and len(collapsed) and len(crossings):
            # A short rail collapsing also makes the two adjacent polygons
            # report crossings. Its endpoints are the ones that touched.
            # Let the far ends keep moving and detect any later contact.
            both = (crossings[:, None, :, None] == collapsed[None, :, None, :]).any(axis=2).all(axis=2)
            crossings = crossings[~both.any(axis=1)]
        vertices = set(crossings.ravel())
        vertices.update(collapsed.ravel())
        return first, set().union(*(self.anchors[v] for v in vertices))

    def _crossing_times(self, rings, motions, bases):
        import numpy as np
        from model_pack_bevel.collision import _root_array

        points = np.einsum('ijk,ilk->ijl', rings - rings[:, :1], bases)
        motion = np.einsum('ijk,ilk->ijl', motions - motions[:, :1], bases)
        triangles = np.all(rings[:, 2] == rings[:, 3], axis=1) & np.all(motions[:, 2] == motions[:, 3], axis=1)
        result = np.full(len(points), np.inf)
        # Disjoint swept segments cannot intersect in the seed plane.
        end = points + motion
        lower = np.minimum(points, end)
        upper = np.maximum(points, end)
        overlap = ((np.minimum(lower[:, 0], lower[:, 1]) <= np.maximum(upper[:, 2], upper[:, 3])) &
                   (np.minimum(lower[:, 2], lower[:, 3]) <= np.maximum(upper[:, 0], upper[:, 1]))).all(axis=1)
        candidates = np.flatnonzero(overlap | triangles)
        if not len(candidates):
            return result
        points, motion, triangles = points[candidates], motion[candidates], triangles[candidates]
        a, b, c = np.asarray(((0, 0, 2, 2), (1, 1, 3, 3), (2, 3, 0, 1)))
        u, v = points[:, b] - points[:, a], points[:, c] - points[:, a]
        du, dv = motion[:, b] - motion[:, a], motion[:, c] - motion[:, a]

        def cross(u, v):
            return u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0]

        constant = cross(u, v)
        linear = cross(du, v) + cross(u, dv)
        quadratic = cross(du, dv)
        coefficients = np.stack((constant, linear, quadratic, np.zeros_like(constant)), axis=-1)
        roots = _root_array(coefficients.reshape((-1, 4)))[:, :2].reshape((-1, 8))
        boundaries = np.sort(np.column_stack((np.zeros(len(points)), np.minimum(roots, 1.),
                                              np.ones(len(points)))), axis=1)
        times = (boundaries[:, :-1] + boundaries[:, 1:]) * .5
        signs = constant[:, None, :] + times[:, :, None] * (
            linear[:, None, :] + times[:, :, None] * quadratic[:, None, :])
        crossed = (signs[:, :, 0] * signs[:, :, 1] < 0.) & (signs[:, :, 2] * signs[:, :, 3] < 0.)
        if self.minimum_depth:
            lengths = np.linalg.norm(u[:, None, :, :] + times[:, :, None, None] * du[:, None], axis=-1)
            crossed &= (np.abs(signs) > lengths * self.minimum_depth).all(axis=2)
        crossed[triangles] = signs[triangles, :, 0] * constant[triangles, :1] < 0.
        # Projected near-touches already present at the seed are allowed to
        # open. Only a new crossing or one that deepens is a collision.
        initially_crossed = (constant[:, 0] * constant[:, 1] < 0.) & (constant[:, 2] * constant[:, 3] < 0.)
        if initially_crossed.any():
            lengths = np.linalg.norm(u[initially_crossed, None, :, :]
                                     + times[initially_crossed, :, None, None] * du[initially_crossed, None, :, :], axis=-1)
            initial_depth = np.min(np.abs(constant[initially_crossed]) /
                                   np.maximum(np.linalg.norm(u[initially_crossed], axis=-1), 1e-30), axis=1)
            crossed[initially_crossed] &= (
                np.abs(signs[initially_crossed]) >
                np.maximum(initial_depth, self.tolerance)[:, None, None] * lengths
            ).all(axis=2)
        result[candidates] = np.where(crossed & (boundaries[:, :-1] < boundaries[:, 1:]),
                                      boundaries[:, :-1], np.inf).min(axis=1)
        return result

    def update(self, width):
        import numpy as np

        # Evaluate from the same seed for every width. The path used to
        # reach it must not affect the result of a drag or numeric edit.
        self.limits.fill(np.inf)
        self.surface_faces.clear()
        last_contact = 0.
        while last_contact < width:
            start = self.points(last_contact)
            end = self.points(width)
            fraction, vertices = self._first_contacts(start, end)
            if self.surface_contact is not None:
                time, indices, faces = self.surface_contact(start, end, min(fraction, 1.))
                participants = set().union(*(self.anchors[v] for v in indices))
                if time < fraction and any(self.limits[v] > last_contact for v in participants):
                    fraction, vertices = time, participants
                    self.surface_faces.update(faces)
            active = [v for v in vertices if self.limits[v] > last_contact]
            if not active:
                break
            contact = last_contact + (width - last_contact) * fraction
            last_contact = max(last_contact, contact - 8. * self.tolerance / self.speed)
            self.limits[active] = last_contact
        targets = self.points(width)
        # After independent supports stop, recheck the resulting faces in
        # their current planes. Back off the rails in the affected faces.
        moving = np.ones(len(self.pairs), dtype=bool)
        for _ in range(8):
            crossed = self._projected_crossings(targets, moving)
            reflex = np.flatnonzero((self._quad_turns(targets) < -self.tolerance * .125).any(axis=1))
            if not crossed and not len(reflex):
                break
            vertices = np.concatenate((self.pairs[crossed].ravel(), self.convex_quads[reflex].ravel()))
            anchors = sorted(set().union(*(self.anchors[v] for v in vertices)))
            if not anchors:
                break
            limits = np.minimum(self.limits[anchors], width)
            low, high = 0., 1.
            affected = np.zeros(len(self.pairs), dtype=bool)
            affected[crossed] = True
            for _ in range(15):
                middle = (low + high) * .5
                self.limits[anchors] = limits * middle
                trial = self.points(width)
                if (self._projected_crossings(trial, affected)
                        or (self._quad_turns(trial)[reflex] < self.quad_turn_floor[reflex]).any()):
                    high = middle
                else:
                    low = middle
            self.limits[anchors] = limits * low
            targets = self.points(width)
        return targets

    def _projected_crossings(self, points, moving):
        import numpy as np

        ids = np.flatnonzero(moving & (self.projection_owner >= 0))
        if not len(ids):
            return []
        owners = self.projection_owner[ids]
        # The best-fit plane has no greater sum of squared residuals
        # than a plane using the seed normal. This bounds how far
        # projection can move either segment before their boxes touch.
        first = points[self.projection_vertices[self.projection_starts]]
        faces = self.projection_faces
        relative = points[self.projection_vertices] - first[faces]
        residuals = np.einsum('ij,ij->i', relative, self.projection_normals[faces])
        radii = 2. * np.sqrt(np.bincount(faces, weights=residuals ** 2, minlength=len(first)))
        margin = radii[owners, None] + self.tolerance * .01
        sides = points[self.pairs[ids]]
        a, b, c, d = sides.transpose(1, 0, 2)
        overlap = ((np.minimum(a, b) <= np.maximum(c, d) + margin) &
                   (np.minimum(c, d) <= np.maximum(a, b) + margin)).all(axis=1)
        if not overlap.any():
            return []
        ids, sides = ids[overlap], sides[overlap]
        owners, inverse = np.unique(owners[overlap], return_inverse=True)
        sizes = self.projection_sizes[owners]
        basis = np.empty((len(owners), 2, 3))
        for count in np.unique(sizes):
            matching = sizes == count
            slots = self.projection_starts[owners[matching], None] + np.arange(count)
            rings = points[self.projection_vertices[slots]]
            centered = rings - rings.mean(axis=1, keepdims=True)
            vectors = centered.swapaxes(1, 2)
            values, axes = np.linalg.eigh(vectors @ centered)
            fit = axes[:, :, 1:].swapaxes(1, 2).copy()
            # A near-collinear ring has no stable covariance plane. Keep the
            # direct fit for those rings, where squaring loses precision.
            ambiguous = values[:, 1] - values[:, 0] <= values[:, 2] * 1e-10
            if ambiguous.any():
                _, _, stable = np.linalg.svd(centered[ambiguous], full_matrices=False)
                fit[ambiguous] = stable[:, :2]
            basis[matching] = fit
        a, b, c, d = np.einsum('ijk,ilk->ijl', sides - sides[:, :1], basis[inverse]).transpose(1, 0, 2)
        ab, cd = b - a, d - c

        def cross(u, v):
            return u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]

        x, y, z, w = cross(ab, c - a), cross(ab, d - a), cross(cd, a - c), cross(cd, b - c)
        deep_ab = np.minimum(abs(x), abs(y)) > self.tolerance * .25 * np.linalg.norm(ab, axis=1)
        deep_cd = np.minimum(abs(z), abs(w)) > self.tolerance * .25 * np.linalg.norm(cd, axis=1)
        return ids[(x * y < 0.) & (z * w < 0.) & deep_ab & deep_cd].tolist()


def _pinched_rings(mesh, rows):
    fn = _mesh_fn(mesh)
    size = 2 * (rows + 1)
    for face in range(fn.numPolygons):
        ring = tuple(fn.getPolygonVertices(face))
        if len(ring) == size and len(set(ring)) == size - 1:
            index = next((i for i in range(rows + 1) if ring[i] == ring[i + rows + 1]), None)
            if index is not None:
                yield face, index


def _stitch_pinched_rows(mesh, node, rows, pinched):
    """Continue the two sides of a pinched native corner across its seam."""
    cmds.setAttr(node + '.mergeVertices', False)
    fn = _mesh_fn(mesh)
    points = fn.getPoints(om.MSpace.kWorld)
    tolerance = _mesh_position_tolerance(fn, points)
    half = rows + 1
    stitches = []
    for face, index in pinched:
        ring = tuple(fn.getPolygonVertices(face))
        if len(ring) != 2 * half:
            continue
        ring = ring[index:] + ring[:index]
        if (points[ring[0]] - points[ring[half]]).length() <= tolerance:
            stitches.append((face, ring))
    if not stitches:
        cmds.setAttr(node + '.mergeVertices', True)
        return
    # Remove the pinched face before interpolating UVs on the two side edges.
    cmds.delete([f'{mesh}.f[{face}]' for face, _ in stitches])
    corners = set()
    for _, ring in stitches:
        a, b, c, d = ring[0], ring[1], ring[half], ring[-1]
        corners.update((a, c, *ring[2:half], *ring[half + 1:-1]))
        for step in range(1, rows):
            fraction = step / rows
            corners.add(_insert_between(mesh, a, b, fraction))
            corners.add(_insert_between(mesh, a, d, fraction))
    distance = om.MDistance(tolerance * 2.).asUnits(om.MDistance.uiUnit())
    cmds.polyMergeVertex([f'{mesh}.vtx[{vertex}]' for vertex in sorted(corners)],
                         distance=distance, constructionHistory=True)


class NativeSlideBevel:
    """Use Maya's non-chamfered bevel for selections across complex faces."""

    def __init__(self, mesh, edges, mode, count, bias_percent, *, spacing=None,
                 follow_width=False):
        self.mesh = mesh
        self.edges = edges
        self.mode = mode
        count = _validate_options(count, mode, bias_percent, bevel=True)
        if count == 1:
            spacing = 0.
        fractions = _bevel_fractions(count, bias_percent)
        self.outer_scale = fractions[0]
        self.fractions = tuple(fraction / self.outer_scale for fraction in fractions)
        self.spacing_ratio = bias_percent / 100. if count > 1 else 0.
        self.spacing = spacing
        self.follow_width = follow_width
        self.row_edges = {}
        self.row_cells = {}
        self.current_row_weights = {}
        self.native_fraction = mode == 'relative'
        self.source_edges = _segments(mesh, edges)
        lengths = [length for a, b in self.source_edges if (length := math.dist(a, b)) > _EPS]
        self.uniform_seed_width = min(lengths, default=.01) * .17
        self.row_seed_width = .1 if mode == 'relative' else self.uniform_seed_width
        self.all_source_edges = _segments(mesh, range(_mesh_fn(mesh).numEdges))
        self.hard_segments = _hard_segments(mesh)
        self.reflex_fold = count == 2 and _has_reflex_fold(_mesh_fn(mesh), edges)
        self.hard_topology = None
        self.nodes = []
        self.outer_grid = None
        self.actual = None
        self.new_edges = ()
        self.contact_seed = None
        self.contact_topology = None
        self.contact_tweaks = set()
        self.cached_offsets = {}
        self.row_sample = None
        self.motion_sample = None
        self.row_contacts = None
        self.row_interpolation = {}
        self.parallel_rows = None
        self.dependent_rows = ()

    def _set_width(self, node, amount):
        relative = self.native_fraction
        value = amount if relative else om.MDistance(amount).asUnits(om.MDistance.uiUnit())
        cmds.setAttr(node + ('.fraction' if relative else '.offset'), value)

    def _clear_contact_tweaks(self):
        shape = _mesh_fn(self.mesh).fullPathName()
        for vertex in self.contact_tweaks:
            cmds.setAttr(f'{shape}.pnts[{vertex}]', 0., 0., 0., type='float3')
        self.contact_tweaks.clear()

    def _stop_at_contacts(self, amount):
        fn = _mesh_fn(self.mesh)
        shape = fn.fullPathName()
        counts, indices = fn.getVertices()
        topology = tuple(counts), tuple(indices)
        target = fn.getPoints(om.MSpace.kWorld)
        if self.contact_topology != topology:
            seed_width = amount * .5
            for _ in range(7):
                self._set_sample_width(seed_width)
                seed_fn = _mesh_fn(self.mesh)
                sizes, vertices = seed_fn.getVertices()
                if (tuple(sizes), tuple(vertices)) != topology:
                    break
                self.contact_seed = seed_fn.getPoints(om.MSpace.kWorld)
                self.contact_topology = topology
                seed_width *= .5
            self._set_sample_width(amount)
            if self.contact_topology != topology:
                return
        inverse = fn.dagPath().inclusiveMatrixInverse()
        tolerance = _mesh_position_tolerance(fn, target)
        stopped = _stop_crossing_faces(topology, self.contact_seed, target, tolerance)
        for vertex, point in stopped.items():
            delta = (point - target[vertex]) * inverse
            cmds.setAttr(f'{shape}.pnts[{vertex}]', delta.x, delta.y, delta.z, type='float3')
        self.contact_tweaks.update(stopped)

    def _set_sample_width(self, amount):
        if self.mode == 'relative':
            self._set_width(self.nodes[0], amount)
        else:
            for node, fraction in zip(self.nodes, self.fractions):
                self._set_width(node, amount * fraction)

    def _cache_row_motion(self, amount, fraction_scale=1., layer_ranges=()):
        """Sample the native rows before their bevel nodes change topology."""
        fn = _mesh_fn(self.mesh)
        counts, indices = fn.getVertices()
        topology = tuple(counts), tuple(indices)
        origin = None
        for ratio in (.8, .9, .95):
            sample = amount * ratio
            self._set_sample_width(sample)
            sampled_fn = _mesh_fn(self.mesh)
            sizes, vertices = sampled_fn.getVertices()
            if (tuple(sizes), tuple(vertices)) == topology:
                origin = sampled_fn.getPoints(om.MSpace.kWorld)
                break
        self._set_sample_width(amount)
        if origin is None:
            return
        points = fn.getPoints(om.MSpace.kWorld)
        if len(self.fractions) == 2:
            self._map_additional_rows(points, origin)
        extensions = self._continue_rows() if len(self.fractions) > 1 else ()
        fn = _mesh_fn(self.mesh)
        counts, indices = fn.getVertices()
        for vertex, a, b, fraction in extensions:
            if vertex == len(origin):
                origin.append(origin[a] + (origin[b] - origin[a]) * fraction)
                self.row_interpolation[vertex] = ((a, 1. - fraction), (b, fraction))
        if len(origin) == fn.numVertices:
            base = fn.getPoints(om.MSpace.kWorld)
            tolerance = _mesh_position_tolerance(fn, base)
            self.motion_sample = ((tuple(counts), tuple(indices)), origin, base, sample / fraction_scale,
                                  (amount - sample) / fraction_scale, tolerance, layer_ranges)
            if len(self.fractions) == 2 and self.mode == 'uniform' and not self.reflex_fold:
                import numpy as np

                seed = np.asarray([tuple(point)[:3] for point in origin])
                positions = np.asarray([tuple(point)[:3] for point in base])
                zero = seed - (positions - seed) * (sample / (amount - sample))
                rebuilt = set(self.row_cells) | {vertex for vertex, *_ in extensions}
                edges = [tuple(fn.getEdgeVertices(edge)) for edge in range(fn.numEdges)]
                edges = [edge for edge in edges if rebuilt.isdisjoint(edge)]
                self.parallel_rows = _ParallelRows(edges, seed, zero, self.source_edges, tolerance)
                self.dependent_rows = tuple(vertex for vertex in self.row_interpolation if vertex in rebuilt)
            self._set_row_motion()

    def _set_row_motion(self):
        topology, origin, base, sample_width, interval, tolerance, layers = self.motion_sample
        motion_start = sample_width + self.spacing * max(0, len(layers) - 1)
        self.row_sample = motion_start, base
        layer_motion = tuple((vertices, scale, motion_start - sample_width * scale - index * self.spacing)
                             for index, (vertices, scale) in enumerate(zip(layers, self.fractions))
                             if vertices)
        self.row_contacts = _RowContacts(topology, origin, base, interval, self.row_interpolation,
                                         tolerance, self._fixed_row_weights if len(self.fractions) == 2 else None,
                                         layer_motion, keep_convex=self.reflex_fold, parallel=self.parallel_rows,
                                         dependent_vertices=self.dependent_rows)

    def _map_additional_rows(self, points, origin):
        """Move each axis of a corner cell independently in the one-row grid."""
        import numpy as np
        outer, edges, quads = self.outer_grid or self._one_row_grid()
        fn = _mesh_fn(self.mesh)
        tolerance = _mesh_position_tolerance(fn, outer)
        positions = np.asarray([tuple(p)[:3] for p in points])
        anchors = {}
        for vertex, point in enumerate(outer):
            distances = np.linalg.norm(positions - tuple(point)[:3], axis=1)
            closest = int(distances.argmin())
            if distances[closest] <= tolerance * 4.:
                anchors[vertex] = closest
        anchored = set(anchors.values())
        bias = self.fractions[1]
        neighbors = [set() for _ in positions]
        for edge in range(fn.numEdges):
            a, b = fn.getEdgeVertices(edge)
            neighbors[a].add(b)
            neighbors[b].add(a)
        midpoints = {}
        directions = {}
        links = {}
        cells = []
        # Tangential motion along the crease can be large at a miter.
        # Orient the row using only motion across its own cell.
        motion = [p - q for p, q in zip(points, origin)]

        def across(a, b):
            axis = (points[b] - points[a]).normal()
            return (motion[a] + motion[b]) * axis

        for a, b in edges:
            if a not in anchors or b not in anchors:
                continue
            a, b = sorted((anchors[a], anchors[b]))
            midpoint = points[a] + (points[b] - points[a]) * .5
            candidates = (neighbors[a] & neighbors[b]) - anchored
            if candidates:
                vertex = min(candidates, key=lambda index: (points[index] - midpoint).length())
            else:
                distances = np.linalg.norm(positions - tuple(midpoint)[:3], axis=1)
                vertex = int(distances.argmin())
            if vertex in anchored:
                continue
            if (points[vertex] - midpoint).length() > tolerance * 8.:
                continue
            edge = a, b
            midpoints[edge] = vertex
            directions[edge] = across(a, b)
            links[edge] = []

        def link(first, second):
            a, b = tuple(sorted(first)), tuple(sorted(second))
            orientation = 1 if (first == a) == (second == b) else -1
            links[a].append((b, orientation))
            links[b].append((a, orientation))

        for ring in quads:
            if not all(vertex in anchors for vertex in ring):
                continue
            a, b, c, d = (anchors[vertex] for vertex in ring)
            sides = [(a, b), (b, c), (d, c), (a, d)]
            keys = [tuple(sorted(side)) for side in sides]
            if len(set((a, b, c, d))) != 4:
                continue
            center = positions[[a, b, c, d]].mean(axis=0)
            mids = [midpoints.get(key) for key in keys]
            # Maya can put the center halfway along a diagonal of the
            # one-row quad. On skewed corner cells it is not the centroid.
            votes = {}
            mid_set = set(mids)
            for middle in mids:
                if middle is None:
                    continue
                for candidate in neighbors[middle] - anchored - mid_set:
                    votes[candidate] = votes.get(candidate, 0) + 1
            candidates = [candidate for candidate, count in votes.items() if count >= 2]
            if candidates:
                vertex = min(candidates, key=lambda index: (-votes[index], np.linalg.norm(positions[index] - center)))
            else:
                distances = np.linalg.norm(positions - center, axis=1)
                vertex = int(distances.argmin())
                if vertex in anchored or distances[vertex] > tolerance * 8.:
                    continue
            for key in keys:
                if key not in links:
                    links[key] = []
                    directions[key] = across(*key)
            cells.append((vertex, a, b, c, d))
            link(sides[0], sides[2])
            link(sides[3], sides[1])

        # Both sides of each corner cell must use the same parameter.
        # Vote across the connected band so one acute miter cannot flip it.
        orientations = {}
        for edge in links:
            if edge in orientations:
                continue
            signs = {edge: 1}
            pending = [edge]
            for side in pending:
                for other, orientation in links[side]:
                    if other not in signs:
                        signs[other] = signs[side] * orientation
                        pending.append(other)
            vote = sum(directions[side] * sign for side, sign in signs.items())
            direction = 1 if vote >= 0. else -1
            for side, sign in signs.items():
                orientations[side] = sign == direction

        def towards_end(a, b):
            return orientations[(a, b)] if a < b else not orientations[(b, a)]

        for (a, b), vertex in midpoints.items():
            forward = towards_end(a, b)
            t = bias if forward else 1. - bias
            self.row_interpolation[vertex] = ((a, 1. - t), (b, t))
            self.row_edges[vertex] = (a, b, forward)
        for vertex, a, b, c, d in cells:
            along_b, along_d = towards_end(a, b), towards_end(a, d)
            u, v = (bias if along_b else 1. - bias), (bias if along_d else 1. - bias)
            self.row_interpolation[vertex] = ((a, (1. - u) * (1. - v)), (b, u * (1. - v)),
                                              (c, u * v), (d, (1. - u) * v))
            self.row_cells[vertex] = (a, b, c, d, along_b, along_d)

    def _fixed_row_weights(self, width, limits):
        """Keep the offset from the outside row fixed as the band opens."""
        if self.row_sample is None or not self.spacing:
            return self.row_interpolation
        sample, _ = self.row_sample
        # The collision sweep starts from the native seed. Give the inside
        # row room on narrower samples so that the sweep has a valid start.
        def fraction(width_limit):
            span = sample + min(width, width_limit)
            return 1. - min(self.spacing / max(span, _EPS), .95)

        def axis(a, b, towards_b):
            t = fraction(limits[b if towards_b else a])
            return t if towards_b else 1. - t

        weights = dict(self.row_interpolation)
        for vertex, (a, b, towards_b) in self.row_edges.items():
            t = axis(a, b, towards_b)
            weights[vertex] = ((a, 1. - t), (b, t))
        for vertex, (a, b, c, d, along_b, along_d) in self.row_cells.items():
            u_a, u_d = axis(a, b, along_b), axis(d, c, along_b)
            v_a, v_b = axis(a, d, along_d), axis(b, c, along_d)
            du, dv = u_d - u_a, v_b - v_a
            u = (u_a + du * v_a) / (1. - du * dv)
            v = v_a + dv * u
            weights[vertex] = ((a, (1. - u_a) * (1. - v)), (b, u_a * (1. - v)),
                               (c, u_d * v), (d, (1. - u_d) * v))
        return weights

    def _move_cached_rows(self, amount):
        sample_width, base = self.row_sample
        fn = _mesh_fn(self.mesh)
        targets = self.row_contacts.update(amount - sample_width)
        self.current_row_weights = self._fixed_row_weights(amount - sample_width, self.row_contacts.limits)
        tolerance = self.row_contacts.tolerance
        matrix = fn.dagPath().inclusiveMatrix()
        inverse = matrix.inverse()
        shape = fn.fullPathName()
        for vertex, (target, point) in enumerate(zip(targets, base)):
            world_delta = om.MPoint(*target) - point
            delta = (world_delta * inverse if world_delta.length() > tolerance else om.MVector())
            previous = self.cached_offsets.get(vertex)
            if previous is None and delta.length() == 0.:
                continue
            if previous is not None and ((delta - previous) * matrix).length() <= tolerance:
                continue
            cmds.setAttr(f'{shape}.pnts[{vertex}]', delta.x, delta.y, delta.z, type='float3')
            if delta.length() > 0.:
                self.cached_offsets[vertex] = delta
            else:
                self.cached_offsets.pop(vertex, None)

    def _continue_rows(self):
        """Continue a transverse edge through a band with a split side."""
        fn = _mesh_fn(self.mesh)
        points = fn.getPoints(om.MSpace.kWorld)
        source = set(_edges_on_segments(self.mesh, self.all_source_edges))
        generated = {frozenset(fn.getEdgeVertices(edge))
                     for edge in range(fn.numEdges) if edge not in source}
        candidates = []
        for face in range(fn.numPolygons):
            ring = tuple(fn.getPolygonVertices(face))
            if len(ring) != 5:
                continue
            for index in range(5):
                middle, b, c, d, a = ring[index:] + ring[:index]
                if any(frozenset(pair) not in generated for pair in ((a, middle), (middle, b), (c, d))):
                    continue
                axis = points[b] - points[a]
                delta = points[middle] - points[a]
                span = axis.length()
                tolerance = _mesh_position_tolerance(fn, (points[a], points[b]), span)
                if span <= tolerance:
                    continue
                fraction = (delta * axis) / (span * span)
                if not tolerance / span < fraction < 1. - tolerance / span:
                    continue
                if (delta - axis * fraction).length() <= tolerance * 2:
                    candidates.append((middle, d, c, fraction))
                    break
        used = set()
        extensions = []
        for middle, a, b, fraction in candidates:
            edge = frozenset((a, b))
            if edge in used:
                continue
            outer = _insert_between(self.mesh, a, b, fraction)
            if outer not in (a, b):
                _connect(self.mesh, middle, outer)
                used.add(edge)
                extensions.append((outer, a, b, fraction))
        return extensions

    def update(self, amount):
        if not math.isfinite(amount) or amount <= 0:
            raise SlideError("Смещение должно быть положительным числом.")
        if amount == self.actual:
            return amount
        relative = self.mode == 'relative'
        actual = min(amount, 1.) if relative else amount
        old_spacing = self.spacing
        if self.spacing is None or self.follow_width:
            self.spacing = actual * self.spacing_ratio
        outer_width = actual + self.spacing * (len(self.fractions) - 1)
        if self.row_sample is not None:
            if old_spacing != self.spacing and len(self.fractions) > 2:
                self._set_row_motion()
            self._move_cached_rows(outer_width)
        elif len(self.fractions) == 1 and self.nodes:
            self._clear_contact_tweaks()
            self._set_width(self.nodes[0], actual)
            self._stop_at_contacts(actual)
        else:
            if self.nodes:
                return self.actual
            edges = self.edges
            cached = len(self.fractions) > 1 or not relative
            build_width = self.row_seed_width if cached else actual
            previous = 1.
            layers = self.fractions if len(self.fractions) > 2 else (1.,)
            layer_ranges = []
            for layer, fraction in enumerate(layers):
                before = _mesh_fn(self.mesh).numVertices
                width = (build_width if not layer else fraction / previous) if relative else build_width * fraction
                offset = om.MDistance(width).asUnits(om.MDistance.uiUnit()) if not relative else .2
                node, = cmds.polyBevel3(
                    [f'{self.mesh}.e[{edge}]' for edge in edges],
                    chamfer=False, segments=3 if len(self.fractions) == 2 else 1,
                    worldSpace=True, offsetAsFraction=relative,
                    offset=offset, fraction=width if relative else .5,
                    # A reflex fold needs transverse corner cells on both sides of the crease.
                    mitering=3 if self.reflex_fold else 0, miterAlong=0,
                    autoFit=True, angleTolerance=180, subdivideNgons=True, smoothingAngle=60,
                    mergeVertices=True, mergeVertexTolerance=.0001, constructionHistory=True,
                )
                self.nodes.append(node)
                if len(layers) > 2:
                    after = _mesh_fn(self.mesh).numVertices
                    layer_ranges.append(list(range(before, after)))
                previous = fraction
                if layer + 1 < len(layers):
                    edges = _edges_on_segments(self.mesh, self.source_edges)
                    if not edges:
                        raise SlideError("Не удалось найти исходные рёбра для следующей поддержки.")
            sample_width = build_width
            fraction_scale = 1.
            if relative and len(self.fractions) == 2 and self._has_invalid_uvs():
                # Maya can generate non-finite UVs in fractional mode.
                # Recover the equivalent world-space width from the native
                # outer row before switching modes.
                fn = _mesh_fn(self.mesh)
                fractional_points = fn.getPoints(om.MSpace.kWorld)
                fractional_topology = tuple(map(tuple, fn.getVertices()))
                self.native_fraction = False
                cmds.setAttr(self.nodes[0] + '.offsetAsFraction', False)
                sample_width = self.uniform_seed_width
                self._set_sample_width(sample_width)
                fn = _mesh_fn(self.mesh)
                if tuple(map(tuple, fn.getVertices())) == fractional_topology:
                    distance_points = fn.getPoints(om.MSpace.kWorld)
                    self._set_sample_width(sample_width * .8)
                    fn = _mesh_fn(self.mesh)
                    if tuple(map(tuple, fn.getVertices())) == fractional_topology:
                        direction = [p - q for p, q in zip(distance_points, fn.getPoints(om.MSpace.kWorld))]
                        square = sum(v * v for v in direction)
                        if square > 1e-20:
                            factor = sum((p - q) * v for p, q, v in
                                         zip(fractional_points, distance_points, direction)) / square
                            sample_width *= max(.1, 1. + .2 * factor)
                fraction_scale = sample_width / build_width
                self._set_sample_width(sample_width)
            if len(self.fractions) == 2:
                self._split_pinched_corners()
            if cached:
                self._cache_row_motion(sample_width, fraction_scale,
                                       layer_ranges if len(layers) > 2 else ())
                self._restore_hard_edges()
                if self.row_sample is not None:
                    self._move_cached_rows(outer_width)
                else:
                    actual = build_width / self.outer_scale
            else:
                self._stop_at_contacts(actual)
        existing = set(_edges_on_segments(self.mesh, self.all_source_edges))
        self.new_edges = tuple(edge for edge in range(_mesh_fn(self.mesh).numEdges) if edge not in existing)
        if not self.new_edges:
            raise SlideError("Не удалось построить опорные рёбра.")
        cmds.select([f'{self.mesh}.e[{edge}]' for edge in self.new_edges], replace=True)
        self.actual = actual
        self._restore_hard_edges()
        return actual

    def _has_invalid_uvs(self):
        fn = _mesh_fn(self.mesh)
        return any(not math.isfinite(value) for name in fn.getUVSetNames()
                   for axis in fn.getUVs(name) for value in axis)

    def _split_pinched_corners(self):
        """Stitch coincident corner rows with one-to-one edge connections."""
        pinched = tuple(_pinched_rings(self.mesh, 2))
        if pinched:
            self.outer_grid = self._one_row_grid()
            _stitch_pinched_rows(self.mesh, self.nodes[0], 2, pinched)

    def _one_row_grid(self):
        node = self.nodes[0]
        cmds.setAttr(node + '.segments', 1)
        try:
            fn = _mesh_fn(self.mesh)
            points = fn.getPoints(om.MSpace.kWorld)
            edges = [tuple(fn.getEdgeVertices(edge)) for edge in range(fn.numEdges)]
            quads = [tuple(fn.getPolygonVertices(face)) for face in range(fn.numPolygons)
                     if len(fn.getPolygonVertices(face)) == 4]
            return points, edges, quads
        finally:
            cmds.setAttr(node + '.segments', 3)

    def _restore_hard_edges(self):
        if self.hard_segments:
            counts, indices = _mesh_fn(self.mesh).getVertices()
            topology = tuple(counts), tuple(indices)
            if topology != self.hard_topology:
                _preserve_hard_edges(self.mesh, self.hard_segments)
                self.hard_topology = topology

    def cancel(self):
        # The tool deletes the disposable preview and its bevel history.
        self.nodes.clear()


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
                   undo: bool = True, spacing=None) -> float:
    """Make ``count`` contours; bias distributes intermediates toward either end."""
    count = _validate_options(count, mode, bias_percent, bevel=bevel)
    if bevel:
        if undo:cmds.undoInfo(openChunk=True,chunkName='Slide Bevel')
        modified=False
        try:
            plan = analyze_bevel_selection()
            if _use_native_bevel(plan, count):
                live = NativeSlideBevel(plan.mesh, plan.edge_ids, mode, count, bias_percent,
                                       spacing=spacing)
            else:
                live = LiveMultiple(bevel=True, count=count, mode=mode,
                                    bias_percent=bias_percent, spacing=spacing, _native_points=False)
            modified = True
            actual = live.update(amount)
        except Exception:
            if undo:
                cmds.undoInfo(closeChunk=True)
                if modified:cmds.undo()
            raise
        else:
            if undo:cmds.undoInfo(closeChunk=True)
        return actual
    mesh, original_ids = _selected_edges()
    originals = _segments(mesh, original_ids)
    original_pairs = _vertex_pairs(mesh, original_ids)
    reference = None if bevel else analyze_selection(side)
    bevel_reference=analyze_bevel_selection() if bevel and mode=='relative' else None
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
            elif bevel:
                local_amount=outer*fraction
            else:
                local_amount = fraction / previous_fraction
            modified = True
            actual = (apply_bevel(local_amount, mode=mode, _undo=False,
                                  _relative_reference=bevel_reference if layer!=count else None) if bevel else
                      apply(local_amount, side=current_side, clone=True,
                            mode=mode, _undo=False))
            if outer is None:
                outer = actual
            previous_fraction = fraction
            _, new_ids = _selected_edges()
            created.extend(_segments(mesh, new_ids))
            created_pairs.extend(_vertex_pairs(mesh, new_ids))
        result = _edges_on_segments(mesh, created, None if bevel else created_pairs)
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

    def __init__(self, plan, fn, *, bevel=False):
        self.plan = plan
        self.bevel=bevel
        self.children = tuple(_PlanGeometry(child, fn,bevel=bevel) for child in plan.children)
        self.guides = {
            face: (tuple(fn.getPolygonVertices(face)),
                   fn.getPolygonNormal(face, om.MSpace.kWorld))
            for face in set(plan.guide_faces) if face >= 0
        }
        if bevel:
            points=fn.getPoints(om.MSpace.kWorld)
            for face,(vertices,normal) in list(self.guides.items()):
                normal=om.MVector();origin=points[vertices[0]]
                for i,v in enumerate(vertices):normal+=(points[v]-origin)^(points[vertices[(i+1)%len(vertices)]]-origin)
                self.guides[face]=(vertices,normal.normal())
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
            for index,(vertex, target, face) in enumerate(zip(plan.vertices, plan.targets, plan.guide_faces)):
                if target >= 0:
                    ends.append(tuple(points[target])[:3])
                    continue
                polygon, normal = self.guides[face]
                origin = points[vertex]
                direction=om.MVector()
                if self.bevel:
                    if index>0 or plan.cyclic:
                        previous=(origin-points[plan.vertices[index-1]]).normal();direction+=normal^previous
                    if index<len(plan.vertices)-1 or plan.cyclic:
                        following=(points[plan.vertices[(index+1)%len(plan.vertices)]]-origin).normal();direction+=normal^following
                else:
                    center=om.MPoint()
                    for member in polygon:center+=points[member]
                    direction=center/len(polygon)-origin
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
        self.runs = tuple(_PlanGeometry(run, fn,bevel=True) for run in plan.runs) if bevel else (
            _PlanGeometry(plan, fn),)

    def refresh(self):
        points = _mesh_fn(self.plan.mesh).getPoints(om.MSpace.kWorld)
        runs = tuple(run.refresh(points) for run in self.runs)
        if not self.bevel:
            return runs[0]
        return replace(self.plan, runs=runs)


class LiveMultiple:
    """Build disposable preview topology once, then move cached cut vertices."""

    def __init__(self, *, bevel, count, mode, side=0, bias_percent=50, spacing=None,
                 follow_width=False, _native_points=True):
        self.count = _validate_options(count, mode, bias_percent, bevel=bevel)
        self.bevel = bevel
        self.mode = mode
        self.side = side
        fractions = (_bevel_fractions(self.count, bias_percent) if bevel else
                     tuple(_distribution_fraction(layer, self.count, bias_percent)
                           for layer in range(self.count, 0, -1)))
        self.outer_scale = fractions[0]
        self.fractions = tuple(fraction / self.outer_scale for fraction in fractions)
        self.spacing_ratio = bias_percent / 100. if bevel and self.count > 1 else 0.
        self.spacing = 0. if self.count == 1 else spacing
        self.follow_width = follow_width
        self.mesh, original_ids = _selected_edges()
        self.originals = _segments(self.mesh, original_ids)
        self.original_pairs = _vertex_pairs(self.mesh, original_ids)
        self.reference = None if bevel else analyze_selection(side)
        self.bevel_reference=analyze_bevel_selection() if bevel and mode=='relative' else None
        self.stages = []
        self.new_edges = ()
        self.actual = None
        self._merged=False
        self.original_vertex_count=_mesh_fn(self.mesh).numVertices
        self.native_points=_native_points
        self._unmerged = None
        self._box = None
        if bevel:
            source=analyze_bevel_selection()
            axes = _box_axes(source)
            if axes:
                self._box = _BoxSupports(self.mesh, axes, mode, self.fractions)
            self.transition_vertices = source.transition_vertices
            widths=[distance for run in source.runs for distance in _normal_distances(run) if distance>_EPS]
            self.seed_amount=min(widths)*.05 if mode=='uniform' else .05

    def update(self, amount):
        if self.bevel and (self.spacing is None or self.follow_width):
            self.spacing = amount * self.spacing_ratio
        if self._box is not None:
            self._box.spacing = self.spacing * (self.count - 1)
            self.actual = self._box.update(amount)
            self.new_edges = self._box.new_edges
            return self.actual
        if self._merged and self._unmerged is not None:
            from .edit_session import assign_materials
            mesh_data, shading, self.new_edges = self._unmerged
            # Maya retains native merge data on the old shape. Replace only
            # the edited shape and keep its component paths and parent.
            path = _mesh_fn(self.mesh).dagPath()
            shape = path.fullPathName()
            path.pop()
            cmds.delete(shape, constructionHistory=True)
            cmds.delete(shape)
            restored = om.MFnMesh().copy(mesh_data, path.node())
            restored_path = om.MFnDagNode(restored).fullPathName()
            name = shape.rsplit("|", 1)[-1]
            if restored_path != shape:
                cmds.rename(restored_path, name)
            assign_materials(shape, *shading)
            for live, _ in self.stages:
                live._patch_nodes = (None,) * len(live._patch_nodes)
            self._merged = False
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
                    plan = _prepare_planar_fans(analyze_bevel_selection(self.transition_vertices, anchor_ends=index == 0))
                    live = LiveBevel(plan, mode=self.mode, preview=True, defer_contacts=True,
                                     outer_contours=tuple(stage.contour_vertices for stage, _ in self.stages))
                    live.native_points=self.native_points
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
            local_amount = (amount + (self.count - 1 - index) * self.spacing if self.bevel else
                            amount if not index else
                            outer * fraction if self.mode == "uniform" else fraction / previous_fraction)
            if building and self.bevel:
                local_amount=self.seed_amount if not index else outer*fraction
            if self.bevel and index:live.relative_reference=self.bevel_reference
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
        if building and self.bevel:
            if self.native_points:
                from .edit_session import snapshot, materials
                self._unmerged = (snapshot(self.mesh), materials(self.mesh), self.new_edges)
            for index, (live, geometry) in enumerate(self.stages):
                if index:live.plan=geometry.refresh()
                live.update(amount + (self.count - 1 - index) * self.spacing)
        if self.bevel:
            contacts={v for live,_ in self.stages for v in live.contacts}
            self.new_edges,self._merged=_weld_contacts(self.mesh,contacts,self.new_edges)
            self.new_edges,joined=_weld_colliding_strips(self.mesh,self.original_vertex_count,self.new_edges)
            self._merged|=joined
            cmds.select([f'{self.mesh}.e[{e}]' for e in self.new_edges],replace=True)
        self.actual = self.stages[-1][0].actual if self.bevel else outer
        return self.actual

    def cancel(self):
        if self._box is not None:
            self._box.seed_points = None
        for live, _ in self.stages:
            live.cancel()
        self.stages = []
        self.actual = None
