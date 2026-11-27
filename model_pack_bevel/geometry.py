"""Cached deformation of Maya bevel templates, in physical world units."""
from collections import defaultdict
import itertools
import math

from maya.api import OpenMaya as om

from .profiles import Curve, profile_value
from .collision import first_contact
from .surface import open_rim, plane_axes

# Native mesh points use float storage; keep an authored 60-degree junction
# stable when rounding changes its computed angle by a few millionths.
BOUNDARY_COSINE=math.cos(math.radians(60.+1e-5))


def _nearest_source_vertices(source, points, cell):
    buckets = defaultdict(list)

    def key(point):
        return tuple(math.floor(point[i] / cell) for i in range(3))

    for vertex, point in enumerate(source):
        buckets[key(point)].append(vertex)
    matches = []
    for point in points:
        center = key(point)
        candidates = [v for delta in itertools.product((-1, 0, 1), repeat=3)
                      for v in buckets.get(tuple(a + b for a, b in zip(center, delta)), ())]
        closest = min(candidates, key=lambda v: (point - source[v]).length()) if candidates else None
        distance = (point - source[closest]).length() if closest is not None else math.inf
        matches.append(closest if distance < cell else None)
    return matches


def source_anchors(source, points, selected, reference_width):
    matches = _nearest_source_vertices(source, points, max(reference_width * 64, 1e-8))
    return [vertex if vertex in selected else None for vertex in matches]


def clean_sharp_template(mesh, matrix, reference):
    """Collapse coincident ends of connected edges created by the native miter."""
    from slide_for_maya.core import _position_tolerance

    fn = om.MFnMesh(mesh)
    points = [point * matrix for point in fn.getPoints()]
    tolerance = min(reference * .002, _position_tolerance(points, matrix))
    short = []
    for edge in range(fn.numEdges):
        a, b = fn.getEdgeVertices(edge)
        if (points[a] - points[b]).length() <= tolerance:
            short.append(edge)
    if not short:
        return mesh
    cleaned = om.MFnMeshData().create()
    fn = om.MFnMesh(om.MFnMesh().copy(mesh, cleaned))
    fn.collapseEdges(short)
    return cleaned


class TemplateGeometry:
    def __init__(self, source, flat, rounded, graph, reference_width, matrix, topology=None,
                 triangles=None, point_blends=None, sharp=False, round_supports=0, segments=1,
                 round_side_contacts=False, corner_grids=False, source_topology=None):
        self.matrix = matrix
        self.inverse = matrix.inverse()
        self.reference_width = reference_width
        self.source = [om.MPoint(p) * matrix for p in source]
        self.flat = [om.MPoint(p) * matrix for p in flat]
        self.rounded = [om.MPoint(p) * matrix for p in rounded]
        if len(self.flat) != len(self.rounded):
            raise ValueError("Шаблоны сечения имеют разную топологию.")
        self._source_matches = _nearest_source_vertices(self.source, self.flat, max(reference_width * 64, 1e-8))
        self.anchors = [vertex if vertex in graph.neighbors else None for vertex in self._source_matches]
        self.samples = [.5] * len(self.flat)
        groups = defaultdict(list)
        for index, anchor in enumerate(self.anchors):
            if anchor is not None:
                groups[anchor].append(index)
        self.groups = dict(groups)
        # New sections between source corners follow the same interpolated
        # motion as their endpoints, including independent local widths.
        self.point_blends={int(i):{int(v):w for v,w in weights.items()}
                           for i,weights in (point_blends or {}).items() if self.anchors[int(i)] is None}
        self.anchor_sets=[{a} if a is not None else set() for a in self.anchors]
        for i,weights in self.point_blends.items():
            self.anchor_sets[i]=set().union(*(self.anchor_sets[v] for v in weights))
        self.origins=self._blend([self.source[a] if a is not None else p for a,p in zip(self.anchors,self.flat)])
        # Flat samples lie on straight cross-section chords. Find their two
        # fixed border points to orient and evaluate the spline consistently.
        tolerance = max(reference_width * 1e-3, 1e-8)
        for members in (() if sharp else self.groups.values()):
            borders = [i for i in members
                       if (self.flat[i] - self.rounded[i]).length() <= tolerance]
            for vertex in members:
                if (self.flat[vertex] - self.rounded[vertex]).length() <= tolerance:
                    self.samples[vertex] = 0
                    continue
                choices = []
                for a, b in itertools.combinations(borders, 2):
                    direction = self.flat[b] - self.flat[a]
                    squared = direction * direction
                    if squared < 1e-16:
                        continue
                    t = ((self.flat[vertex] - self.flat[a]) * direction) / squared
                    residual = (self.flat[vertex] - (self.flat[a] + direction * t)).length()
                    if -1e-4 <= t <= 1 + 1e-4 and residual <= tolerance:
                        # Orient by coordinates, not native vertex ID changes.
                        forward = tuple(flat[a])[:3] < tuple(flat[b])[:3]
                        choices.append((squared, t if forward else 1-t))
                if choices:
                    self.samples[vertex] = max(choices)[1]
        self.constraints = []
        self.constraint_faces = []
        self.bevel_faces = set()
        self.strip_faces = set()
        self._selected_pairs = {frozenset(pair) for pair in graph.edges.values()}
        self._round_paths = (self._rounded_support_paths(topology, segments + 2 * round_supports)
                             if topology and (round_supports or round_side_contacts) else ())
        self._rounded_topology = topology if round_supports or round_side_contacts else None
        self._round_side_contacts = round_side_contacts and bool(self._round_paths)
        self._round_support_guides = {}
        self._round_support_seed = None
        self._rim_rails, self._rim_fixed, self.rim_caps = {}, {}, ()
        self._rim_ends = frozenset()
        self._residual_planes, self._source_axes, self._support_axes = {}, {}, {}
        self._plane_tolerance = 0.
        self._smoothing_topology = topology
        self._sharp_topology = topology if sharp else None
        self._smoothing = None
        self._independent_key = None
        self._independent_bound = 0
        self._independent_limits = {}
        self._independent_contacts = []
        self._independent_cache = {"adjacency_positions":[tuple(p)[:3] for p in self.origins]}
        self.uniform_limit = math.inf
        self.triangles = []
        self._contact_key = None
        self._contact_limit = math.inf
        self._shared_contact_faces = ()
        self._contact_bound = 0
        self._contact_cache = {}
        self._local_contact_cache = {"adjacency_positions":[tuple(p)[:3] for p in self.origins]}
        self.contact_faces = ()
        self._previous = None
        if topology is not None:
            counts,connects = topology
            origins = self.origins
            vectors = [(p-origin)/reference_width for p,origin in zip(self.flat,origins)]
            start = 0
            for face,count in enumerate(counts):
                ids = list(connects[start:start+count])
                start += count
                if not any(self.anchor_sets[i] for i in ids):
                    continue
                points = [origins[i] for i in ids]
                root = points[0]
                normal = sum(((first - root) ^ (second - root)
                              for first, second in zip(points[1:-1], points[2:])), om.MVector())
                if normal.length() < 1e-15:
                    # Newly generated strips collapse at zero width; original
                    # residual faces determine where those strips can expand.
                    if all(self.anchor_sets[i] for i in ids):
                        self.bevel_faces.add(face)
                        anchors=set().union(*(self.anchor_sets[i] for i in ids))
                        if any(frozenset(pair) in self._selected_pairs for pair in itertools.combinations(anchors,2)):
                            self.strip_faces.add(face)
                    continue
                normal.normalize()
                self.constraints.append((ids,normal))
                self.constraint_faces.append(face)
            self.uniform_limit = self._collision(origins,vectors)*(1-1e-4)
            if triangles is not None:
                tri_counts,tri_vertices = triangles
                start = 0
                for face,count in enumerate(tri_counts):
                    for _ in range(count):
                        self.triangles.append((tuple(tri_vertices[start:start+3]),face))
                        start+=3
        if round_supports and corner_grids and self._round_paths:
            self._round_support_guides = self._rounded_support_rows(round_supports)
        if source_topology is not None and topology is not None:
            from slide_for_maya.core import _position_tolerance

            self._plane_tolerance = _position_tolerance(self.source, matrix)
            native_precision = max(abs(point[i]) for point in self.flat for i in range(3)) * (2. ** -22)
            rim_tolerance = 2. * max(self._plane_tolerance, native_precision)
            self._rim_rails, self._rim_fixed, self._rim_ends, self.rim_caps = open_rim(
                source_topology, topology, self.source, self.flat, self.groups, self.anchors,
                rim_tolerance, sharp, round_supports)
            planes = defaultdict(list)
            for ids, normal in self.constraints:
                origin = self.origins[ids[0]]
                if any(abs((self.origins[v] - origin) * normal) > self._plane_tolerance for v in ids):
                    continue
                for v in ids:
                    if self.anchor_sets[v]:
                        planes[v].append(normal)
            self._residual_planes = dict(planes)
            self._source_axes = {v: plane_axes(normals) for v, normals in planes.items()}
            if not sharp:
                self._support_axes = {
                    v: plane_axes([*planes.get(v, ()), normal])
                    for v, guides in self._round_support_guides.items()
                    for normal in (guides[0][0],)
                    if all(any(abs(n * candidate) > 1. - 1e-8 for candidate in planes.get(border, ()))
                           and abs(normal * n) > 1. - 1e-8 for n, _, border, _ in guides)
                }

    def _on_source(self, vertex, point, *, supports=True, rim_profile=True):
        if vertex in self._rim_fixed and (rim_profile or self.anchors[vertex] is None):
            return self._rim_fixed[vertex]
        axes = self._support_axes.get(vertex) if supports else None
        axes = self._source_axes.get(vertex, ()) if axes is None else axes
        rail = self._rim_rails.get(vertex) if rim_profile or vertex in self._rim_ends else None
        if rail is not None:
            origin = self.origins[vertex]
            return origin + rail * ((point - origin) * rail)
        if axes:
            delta = point - self.origins[vertex]
            point = point - sum((normal * (delta * normal) for normal in axes), om.MVector())
        return point

    def preserve_rounded_surface(self, result):
        vertices = self._source_axes.keys() | self._rim_rails.keys() | self._rim_fixed.keys()
        if not self._support_axes:
            for v in vertices:
                result[v] = self._on_source(v, result[v] * self.matrix) * self.inverse
            return result

        from slide_for_maya.multiple import _RowContacts
        import numpy as np

        original = [p * self.matrix for p in result]
        for v in vertices:
            original[v] = self._on_source(v, original[v], supports=False)
        proposed = list(original)
        sections = defaultdict(list)
        for v in self._support_axes.keys() - (self._rim_rails.keys() | self._rim_fixed.keys()):
            point = self._on_source(v, original[v])
            if (point - original[v]).length() > 1e-10:
                proposed[v] = point
                sections[self.anchors[v]].append(v)
        if sections:
            # A native rounded junction can share several strip profiles in
            # a single cap. Keep the safe section if projecting it would fold
            # the cap, while preserving the planes of the surrounding rows.
            tolerance = max(1e-8, min(self.reference_width * .001, 1e-6))
            probe = _RowContacts(self._rounded_topology, original, proposed, 1., {}, tolerance)
            moving = np.ones(len(probe.pairs), dtype=bool)
            while sections:
                trial = np.array([tuple(p)[:3] for p in proposed])
                crossings = probe._projected_crossings(trial, moving)
                rejected = {self.anchors[v] for v in probe.pairs[crossings].ravel()} & sections.keys()
                if not rejected:
                    break
                for anchor in rejected:
                    for v in sections.pop(anchor):
                        proposed[v] = original[v]
        for v in vertices | self._support_axes.keys():
            result[v] = proposed[v] * self.inverse
        return result

    def _blend(self,values):
        for i,weights in self.point_blends.items():
            values[i]=type(values[i])([sum(values[v][axis]*w for v,w in weights.items()) for axis in range(3)])
        return values

    @staticmethod
    def _rail_section(center, end, axis, length, rays, neighbors, group, rows, tolerance):
        """Follow the native cross-section from the crease to the residual face."""
        distances = {v: rays[v] * axis for v in group}
        allowed = {v for v, along in distances.items()
                   if tolerance < along <= length + tolerance
                   and (rays[v] - axis * along).length() <= along * .65}
        if end not in allowed:
            return ()
        paths = {center: ()}
        for _ in range(rows):
            following = {}
            for vertex, section in paths.items():
                for neighbor in neighbors[vertex] & allowed:
                    if distances[neighbor] > distances[vertex] + tolerance:
                        following[neighbor] = (None if neighbor in following or section is None
                                               else (*section, neighbor))
            paths = following
        return paths.get(end) or ()

    def _sharp_source_rails(self, source_topology, count):
        from itertools import islice
        from slide_for_maya.core import _position_tolerance

        # Maya's native world-space bevel can round its intermediate points
        # before transforming them back to the mesh's local space.
        world_precision = max(abs(point[i]) for point in self.flat for i in range(3)) * (2. ** -22)
        tolerance = min(self.reference_width * .02, 1.5 * max(
            world_precision, _position_tolerance(self.flat, self.matrix)))
        rays = [point - origin for point, origin in zip(self.flat, self.origins)]
        close = {v for v, ray in enumerate(rays)
                 if self.anchors[v] is not None and ray.length() <= tolerance}
        fixed, native_edges = set(), set()
        neighbors = defaultdict(set)
        counts, vertices = self._sharp_topology
        vertices = iter(vertices)
        for size in counts:
            ring = tuple(islice(vertices, size))
            for a, b in zip(ring, ring[1:] + ring[:1]):
                native_edges.add(tuple(sorted((a, b))))
                neighbors[a].add(b)
                neighbors[b].add(a)
                if a in close and b in close and frozenset((self.anchors[a], self.anchors[b])) in self._selected_pairs:
                    fixed.update((a, b))

        selected_degree = defaultdict(int)
        for edge in self._selected_pairs:
            for vertex in edge:
                selected_degree[vertex] += 1
        incident = defaultdict(dict)
        counts, vertices = source_topology
        vertices = iter(vertices)
        for size in counts:
            ring = tuple(islice(vertices, size))
            for a, b in zip(ring, ring[1:] + ring[:1]):
                if frozenset((a, b)) not in self._selected_pairs:
                    incident[a][b] = self.source[b] - self.source[a]
                    incident[b][a] = self.source[a] - self.source[b]

        centers, ends = defaultdict(list), defaultdict(list)
        for vertex in fixed:
            centers[self.anchors[vertex]].append(vertex)
        for a, b in sorted(native_edges):
            for v, other in ((a, b), (b, a)):
                anchor, target = self.anchors[v], self.anchors[other]
                if selected_degree[anchor] != 2:
                    continue
                if target is None:
                    originals = [w for w in incident[anchor] if not selected_degree[w]
                                 and (self.flat[other] - self.source[w]).length() <= tolerance]
                    if len(originals) == 1:
                        target = originals[0]
                if target in incident[anchor]:
                    ends[anchor, target].append(v)
        marked = defaultdict(list)
        for (anchor, target), section_ends in ends.items():
            if len(centers[anchor]) != 1 or len(section_ends) != 1:
                continue
            direction = incident[anchor][target]
            length = direction.length()
            if length <= tolerance:
                continue
            axis = direction / length
            section = self._rail_section(centers[anchor][0], section_ends[0], axis, length,
                                         rays, neighbors, self.groups[anchor], count + 1, tolerance)
            for vertex in section:
                marked[vertex].append(axis)

        rails = {}
        for vertex, (anchor, ray) in enumerate(zip(self.anchors, rays)):
            if vertex in fixed:
                rays[vertex] = om.MVector()
            elif len(marked[vertex]) == 1:
                axis = marked[vertex][0]
                rails[vertex] = axis
                rays[vertex] = axis * (ray * axis)
            elif selected_degree[anchor] == 2:
                choices = []
                for edge in incident[anchor].values():
                    length = edge.length()
                    if length <= tolerance:
                        continue
                    axis = edge / length
                    along = ray * axis
                    if tolerance < along <= length + tolerance:
                        error = (ray - axis * along).length()
                        if error <= tolerance:
                            choices.append((error, axis, along))
                if choices:
                    _, axis, along = min(choices, key=lambda choice: choice[0])
                    rails[vertex] = axis
                    rays[vertex] = axis * along
        return rays, rails, fixed

    @staticmethod
    def _strip_path(connections, size):
        path = defaultdict(set)
        for a, b in connections:
            path[a].add(b)
            path[b].add(a)
        ends = sorted(edge for edge, adjacent in path.items() if len(adjacent) == 1)
        if len(ends) != 2 or len(path) != size or any(len(adjacent) > 2 for adjacent in path.values()):
            return ()
        order = [ends[0]]
        previous = None
        while following := path[order[-1]] - {previous}:
            previous = order[-1]
            order.append(next(iter(following)))
        return order if len(order) == len(path) else ()

    @staticmethod
    def _strip_support_bands(connections, guides, rays, rows):
        """Trace both support rows by following the quads across a bevel strip."""
        order = TemplateGeometry._strip_path(connections, 2 * rows + 1)
        if not order:
            return ()

        def score(edge, side):
            normal, axis = guides[side]
            return sum(abs(rays[v] * normal) + max(0., -(rays[v] * axis)) for v in edge)

        if score(order[0], 0) + score(order[-1], 1) > score(order[0], 1) + score(order[-1], 0):
            order.reverse()
        return order[:rows][::-1], order[rows + 1:]

    def _rounded_support_paths(self, topology, segments):
        """Find the native sample order across each longitudinal strip."""
        from itertools import islice

        counts, indices = topology
        indices = iter(indices)
        connections = defaultdict(list)
        for count in counts:
            ring = tuple(islice(indices, count))
            pair = frozenset(self.anchors[v] for v in ring)
            if pair not in self._selected_pairs:
                continue
            edges = {tuple(sorted((a, b))) for a, b in zip(ring, ring[1:] + ring[:1])
                     if self.anchors[a] != self.anchors[b]}
            if len(edges) == 2:
                connections[pair].append(tuple(sorted(edges)))
        sections = set()
        self._round_strips = []
        for strips in connections.values():
            order = self._strip_path(strips, segments + 1)
            if not order:
                continue
            self._round_strips.append(order)
            for a in order[0]:
                anchor = self.anchors[a]
                section = tuple(next(v for v in row if self.anchors[v] == anchor) for row in order)
                if len(set(section)) == len(section):
                    sections.add(min(section, section[::-1]))
        return tuple(sorted(sections))

    def _rounded_support_rows(self, supports):
        rims = {}
        for ids, normal in self.constraints:
            for a, b in zip(ids, ids[1:] + ids[:1]):
                if frozenset((self.anchors[a], self.anchors[b])) in self._selected_pairs:
                    inward = (normal ^ (self.origins[b] - self.origins[a]).normal()).normal()
                    rims[tuple(sorted((a, b)))] = normal, inward
        rows = defaultdict(list)
        for strip in self._round_strips:
            for side in (strip, strip[::-1]):
                outside = side[0]
                if outside not in rims:
                    continue
                normal, inward = rims[outside]
                ends = {self.anchors[v]: v for v in outside}
                for rank, edge in enumerate(side[1:supports + 1], 1):
                    for vertex in edge:
                        v = ends[self.anchors[vertex]]
                        if (self.flat[v] - self.origins[v]) * inward > self.reference_width * .1:
                            rows[vertex].append((normal, inward, v, rank))
        return rows

    def _fit_rounded_support_end(self, vertex, point, original, supports, spacing):
        guides = self._round_support_guides.get(vertex, ())
        if not guides:
            return point
        corrections = []
        for _, axis, border, rank in guides:
            if any(axis * other > 1. - 1e-8 for other, _ in corrections):
                continue
            width = (original[border] - self.origins[border]) * axis
            if width <= 0.:
                return point
            step = min(spacing, width / (supports + .01))
            corrections.append((axis, (original[border] - point) * axis - rank * step))
        planes = plane_axes([normal for normal, _, _, _ in guides])
        if len(planes) == 2:
            origin = self.origins[vertex]
            rail = self._rim_rails.get(vertex, planes[0] ^ planes[1])
            projections = [rail * axis for axis, _ in corrections]
            denominator = sum(value * value for value in projections)
            if denominator > 1e-4:
                station = sum(value * (((point - origin) * axis) + delta)
                              for value, (axis, delta) in zip(projections, corrections)) / denominator
                return origin + rail * station
            return point
        if len(planes) != 1:
            return point
        first, dx = corrections[0]
        if len(corrections) == 1:
            rail = self._rim_rails.get(vertex)
            if rail is not None and abs(rail * first) > .01:
                origin = self.origins[vertex]
                return origin + rail * ((((point - origin) * first) + dx) / (rail * first))
            return point + first * dx
        second, dy = corrections[1]
        dot = first * second
        if len(corrections) != 2 or abs(dot) > .98:
            return point
        return point + (first * (dx - dot * dy) + second * (dy - dot * dx)) / (1. - dot * dot)

    def fit_rounded_supports(self, result, supports, spacing, *, check_stopped=True):
        """Keep the outer borders and redistribute the native profile samples."""
        if not self._round_paths:
            return result
        from slide_for_maya.multiple import _RowContacts
        import numpy as np

        original = [self._on_source(v, point * self.matrix, supports=False, rim_profile=False)
                    for v, point in enumerate(result)]
        fitted = defaultdict(list)
        for section in self._round_paths:
            first, last = section[0], section[-1]
            origin = self.origins[first]
            size = len(section) - 1
            core_segments = size - 2 * supports
            spans = {}
            for v in (first, last):
                ray = self.flat[v] - origin
                if ray.length() > 1e-12:
                    spans[v] = max(0., ((original[v] - origin) * ray) * self.reference_width / (ray * ray))
            if not all(spans.get(v, 0.) > 1e-12 for v in (first, last)):
                continue
            step = {v: min(spacing / span, 1. / (supports + .01)) for v, span in spans.items()}
            rays = {v: original[v] - origin for v in spans}
            core_a, core_b = (1. - supports * step[v] for v in (first, last))
            for index, vertex in enumerate(section[1:-1], 1):
                row = min(index, size - index)
                if row <= supports:
                    edge = first if index <= supports else last
                    point = origin + rays[edge] * (1. - row * step[edge])
                else:
                    fraction = (index - supports) / core_segments
                    sample = fraction * size
                    lower = math.floor(sample)
                    a, b = (original[section[v]] for v in (lower, lower + 1))
                    original_chord = origin + rays[first] * (1. - fraction) + rays[last] * fraction
                    profile = a + (b - a) * (sample - lower) - original_chord
                    core = core_a * (1. - fraction) + core_b * fraction
                    point = (origin + rays[first] * (core_a * (1. - fraction)) +
                             rays[last] * (core_b * fraction) + profile * core)
                fitted[vertex].append(point)
        if not fitted:
            return result
        outer = {v for section in self._round_paths for v in (section[0], section[-1])}
        target = list(original)
        for vertex, samples in fitted.items():
            if vertex in outer:
                continue
            center = sum((om.MVector(p) for p in samples), om.MVector()) / len(samples)
            target[vertex] = self._fit_rounded_support_end(vertex, om.MPoint(center), original, supports, spacing)
        target = [self._on_source(v, point, supports=False) for v, point in enumerate(target)]
        # Extra native corner cells can share samples with several strips.
        # Preserve their face order while keeping the outer borders fixed.
        tolerance = max(1e-8, min(self.reference_width * .001, 1e-6))
        seed = [self._on_source(v, point, supports=False) for v, point in enumerate(original)]
        contacts = _RowContacts(self._rounded_topology, seed, target, 1., {}, tolerance,
                                minimum_depth=tolerance * .25)
        points = contacts.points(1.)
        moving = np.ones(len(contacts.pairs), dtype=bool)
        if contacts._projected_crossings(points, moving):
            points = contacts.update(1.)
        if (check_stopped and self.at_contact and self._rim_rails
                and contacts._projected_crossings(points, moving)):
            # Near contact, projecting a curved template onto the rim can
            # already fold its starting section. Sweep from a small fitted
            # section, with both poses on the rim and the supporting planes.
            profile = supports, spacing
            if self._round_support_seed is None or self._round_support_seed[0] != profile:
                span = max(self.reference_width, spacing * (supports + 2))
                flat = [origin + (p - origin) * (span / self.reference_width)
                        for origin, p in zip(self.origins, self.flat)]
                start = self.fit_rounded_supports(om.MPointArray([p * self.inverse for p in flat]),
                                                  supports, spacing, check_stopped=False)
                self._round_support_seed = profile, [p * self.matrix for p in start]
            start = self._round_support_seed[1]
            end = [om.MPoint(*p) for p in points]
            contact = _RowContacts(self._rounded_topology, start, end, 1., {}, tolerance,
                                   minimum_depth=tolerance * .25, local_edge_contacts=True)
            points = contact.update(1.)
        return om.MPointArray([om.MPoint(*p) * self.inverse for p in points])

    def _support_motion(self, source_topology, count, rays, rails, fit_supports):
        """Use the same row rank at both ends of each longitudinal edge."""
        from itertools import islice

        faces = defaultdict(list)
        edge_guides = defaultdict(list)
        planar_guides = defaultdict(list)
        sizes, vertices = source_topology
        vertices = iter(vertices)
        for size in sizes:
            ring = tuple(islice(vertices, size))
            selected = [(a, b) for a, b in zip(ring, ring[1:] + ring[:1])
                        if frozenset((a, b)) in self._selected_pairs]
            if not selected:
                continue
            root = self.source[ring[0]]
            normal = sum(((self.source[a] - root) ^ (self.source[b] - root)
                          for a, b in zip(ring[1:-1], ring[2:])), om.MVector()).normal()
            planar = self._plane_tolerance and all(abs((self.source[v] - root) * normal) <= self._plane_tolerance
                                                   for v in ring)
            directions = defaultdict(list)
            for a, b in selected:
                inward = normal ^ (self.source[b] - self.source[a]).normal()
                pair = frozenset((a, b))
                edge_guides[pair].append((normal, inward))
                if planar:
                    planar_guides[pair].append(normal)
                directions[a].append(inward)
                directions[b].append(inward)
            for corner, axes in directions.items():
                faces[corner].append((normal, axes))

        rows = count + 1
        sample_step = self.reference_width / rows
        longitudinal = defaultdict(list)
        bands = defaultdict(list)
        connections = defaultdict(list)
        strip_partners = defaultdict(list)
        attached = set()
        sizes, vertices = self._sharp_topology
        vertices = iter(vertices)
        seen = set()
        for size in sizes:
            ring = tuple(islice(vertices, size))
            strip_edges = defaultdict(set)
            for a, b in zip(ring, ring[1:] + ring[:1]):
                if self.anchors[a] != self.anchors[b]:
                    attached.update((a, b))
                pair = frozenset((self.anchors[a], self.anchors[b]))
                edge = tuple(sorted((a, b)))
                if pair not in edge_guides:
                    continue
                if fit_supports:
                    strip_edges[pair].add(edge)
                if edge in seen:
                    continue
                seen.add(edge)
                choices = []
                for side, (normal, axis) in enumerate(edge_guides[pair]):
                    error = max(abs(rays[a] * normal), abs(rays[b] * normal)) / sample_step
                    offsets = [rays[v] * axis / sample_step for v in edge]
                    if error < .38 and min(offsets) > .16:
                        choices.append((error, side, sum(offsets) * .5))
                if choices:
                    error, side, offset = min(choices)
                    bands[pair, side].append((offset, edge, error))
            if len(strip_edges) == 1 and len({self.anchors[v] for v in ring}) == 2:
                pair, cross_section = next(iter(strip_edges.items()))
                if len(cross_section) == 2:
                    connections[pair].append(tuple(sorted(cross_section)))

        for pair, strips in connections.items():
            guides = edge_guides[pair]
            if len(guides) != 2:
                continue
            layout = self._strip_support_bands(strips, guides, rays, rows)
            for side, edges in enumerate(layout):
                normal, axis = guides[side]
                outer = {self.anchors[v]: v for v in edges[-1]}
                for rank, edge in enumerate(edges[:-1], 1):
                    for v in edge:
                        other = outer[self.anchors[v]]
                        gap = (rays[other] - rays[v]) * axis
                        if gap > sample_step * .15:
                            strip_partners[v].append((other, rank, gap))
                errors = [max(abs(rays[v] * normal) for v in edge) / sample_step for edge in edges]
                if max(errors) < .38:
                    bands[pair, side] = [(rank, edge, error)
                                     for rank, (edge, error) in enumerate(zip(edges, errors), 1)]
        for (pair, side), edges in bands.items():
            normal, axis = edge_guides[pair][side]
            for index, (offset, edge, error) in enumerate(sorted(edges), 1):
                rank = index if len(edges) == rows else round(offset)
                if 0 < rank <= rows:
                    for vertex in edge:
                        longitudinal[vertex].append((error, normal, axis, rank))

        support_planes = defaultdict(list)
        for (pair, side), edges in bands.items():
            normal, _ = edge_guides[pair][side]
            if not any(normal * planar > 1. - 1e-9 for planar in planar_guides[pair]):
                continue
            for _, edge, error in edges:
                if error < .08:
                    for v in edge:
                        support_planes[v].append(normal)
        self._support_axes = {v: plane_axes([*self._residual_planes.get(v, ()), *normals])
                              for v, normals in support_planes.items()}

        layouts = []
        for vertex, (ray, anchor) in enumerate(zip(rays, self.anchors)):
            linked = longitudinal[vertex]
            if len(linked) == 2 and vertex not in rails:
                _, first_plane, first_axis, first_rank = linked[0]
                _, second_plane, second_axis, second_rank = linked[1]
                if first_rank == second_rank and first_plane * second_plane < .9999:
                    # Both rows meet on an existing fold. Move along its rail
                    # so the supports stay on their respective source faces.
                    factor = ray * (first_axis + second_axis)
                    if abs(factor) > sample_step * .05:
                        direction = ray / factor
                        layouts.append(((first_axis, first_rank, direction), (second_axis, second_rank, direction)))
                        continue
            choices = []
            for normal, directions in faces.get(anchor, ()):
                error = abs(ray * normal)
                matches = [(axis, rank) for _, plane, axis, rank in linked if plane * normal > 1. - 1e-9]
                if not matches and error > max(self.reference_width * .01, ray.length() * .02):
                    continue
                guides = []
                for axis in directions:
                    offset = ray * axis / sample_step
                    native_rank = round(offset)
                    match = next((rank for guide, rank in matches if guide * axis > 1. - 1e-9), None)
                    if match is not None:
                        guides.append((axis, match))
                    elif 0 < native_rank <= rows and abs(offset - native_rank) < .42:
                        guides.append((axis, native_rank))
                if guides:
                    choices.append((-len(matches), -len(guides), error, normal, guides))
            if not choices or ray.length() <= 1e-12:
                layouts.append(())
                continue
            *_, guides_plane, guides = min(choices, key=lambda choice: choice[:3])
            axis, rank = guides[0]
            if vertex in rails:
                rail = rails[vertex]
                axis, rank = max(guides, key=lambda guide: rail * guide[0])
                projection = rail * axis
                layouts.append(((axis, rank, rail / projection),) if projection > .05 else ())
                continue
            if len(guides) == 2:
                other, other_rank = guides[1]
                cosine = axis * other
                if abs(cosine) < .999:
                    first = (axis - other * cosine) / (1. - cosine * cosine)
                    second = (other - axis * cosine) / (1. - cosine * cosine)
                    layouts.append(((axis, rank, first), (other, other_rank, second)))
                    continue
            on_rail = any(abs(ray * normal) < max(sample_step * .02, ray.length() * .01)
                          and abs(normal * guides_plane) < .9999 for normal, _ in faces[anchor])
            direction = ray / (ray * axis) if on_rail else axis
            layouts.append(((axis, rank, direction),))

        result = []
        for ray, layout in zip(rays, layouts):
            offset = sum((basis * (rank * sample_step - ray * axis) for axis, rank, basis in layout), om.MVector())
            step = sum((basis * (rank - rows) for _, rank, basis in layout), om.MVector())
            result.append((ray + offset, step))

        # A rail shared by differently angled source faces can move farther
        # on one face than the other. Space the inner rows from the actual
        # outer row on each face so they stay in order around that miter.
        outer = {}
        for vertex, links in longitudinal.items():
            ray, step = result[vertex]
            for _, _, axis, rank in links:
                if rank == rows:
                    outer[self.anchors[vertex], tuple(axis)] = (ray * axis, step * axis)
        for vertex, (layout, (ray, step)) in enumerate(zip(layouts, result)):
            base_correction, step_correction = om.MVector(), om.MVector()
            for axis, rank, basis in layout:
                key = self.anchors[vertex], tuple(axis)
                if rank < rows and key in outer:
                    offset, movement = outer[key]
                    base_correction += basis * (offset + (rank - rows) * sample_step - ray * axis)
                    step_correction += basis * (movement + rank - rows - step * axis)
            result[vertex] = ray + base_correction, step + step_correction
        adjustments = {}
        for vertex, candidates in strip_partners.items():
            if vertex in self._sharp_fixed:
                continue
            outer_vertex, rank, gap = max(candidates, key=lambda item: item[-1])
            other_ray, other_step = result[outer_vertex]
            across = rays[outer_vertex] - rays[vertex]
            if across.length() > gap * 6. or (other_ray - rays[outer_vertex]).length() > rays[outer_vertex].length() * .5:
                continue
            # The topology supplies the corresponding outer vertex even when
            # a tightly folded native miter puts its supports at uneven offsets.
            shared_rail = (vertex in rails and outer_vertex in rails
                           and rails[vertex] * rails[outer_vertex] > .9999)
            current_gap = other_ray - result[vertex][0]
            pinched = (len(layouts[vertex]) < 2 and
                       all(current_gap * guide <= sample_step * .25 for guide, _, _ in layouts[vertex]))
            if shared_rail or pinched:
                direction = across / gap
                adjustments[vertex] = (other_ray + direction * ((rank - rows) * sample_step),
                                       other_step + direction * (rank - rows))
        for vertex, motion in adjustments.items():
            result[vertex] = motion
        irregular = {self.anchors[vertex] for vertex, links in longitudinal.items()
                     if any(abs(rays[vertex] * axis - rank * sample_step) > sample_step * .06
                            for _, _, axis, rank in links)}
        self._interpolate_support_cells(rays, attached, irregular, result, sample_step)
        self._support_corner_cells = self._movable_corner_cells(attached, irregular, sample_step)
        return result

    def _interpolate_support_cells(self, rays, attached, irregular, result, sample_step):
        """Keep subdivided corner sections between their actual strip ends."""
        import numpy as np

        tolerance = sample_step * .045
        for anchor in irregular:
            group = self.groups[anchor]
            fixed, interior = [], []
            for vertex in group:
                (fixed if vertex in attached or rays[vertex].length() <= tolerance else interior).append(vertex)
            if len(fixed) < 2 or not interior:
                continue
            data = np.asarray([tuple(rays[v]) for v in fixed])
            a, b = np.triu_indices(len(fixed), 1)
            first, second = data[a], data[b]
            directions = second - first
            squared = np.einsum('ij,ij->i', directions, directions)
            for vertex in interior:
                ray = rays[vertex]
                relative = np.asarray(tuple(ray)) - first
                fraction = np.einsum('ij,ij->i', relative, directions) / np.maximum(squared, 1e-30)
                errors = np.linalg.norm(relative - directions * fraction[:, None], axis=1)
                options = np.flatnonzero((fraction > .01) & (fraction < .99) & (errors <= tolerance))
                if len(options):
                    index = options[np.argmin(squared[options])]
                    v, w, t = fixed[a[index]], fixed[b[index]], fraction[index]
                    base_a, step_a = result[v]
                    base_b, step_b = result[w]
                    residual = ray - (rays[v] * (1. - t) + rays[w] * t)
                    result[vertex] = (base_a * (1. - t) + base_b * t + residual,
                                      step_a * (1. - t) + step_b * t)

    def _movable_corner_cells(self, attached, irregular, sample_step):
        from itertools import islice

        around = defaultdict(list)
        counts, vertices = self._sharp_topology
        vertices = iter(vertices)
        for count in counts:
            ring = tuple(islice(vertices, count))
            for i, vertex in enumerate(ring):
                if vertex not in attached and self.anchors[vertex] in irregular:
                    around[vertex].append(ring[i:] + ring[:i])
        result = []
        for vertex, cells in around.items():
            if any(len(cell) != 4 or len({self.anchors[v] for v in cell}) != 1 for cell in cells):
                continue
            if (self.flat[vertex] - self.origins[vertex]).length() <= sample_step * .045:
                continue
            origin = self.flat[vertex]
            spans = [(self.flat[a] - origin, self.flat[b] - origin, self.flat[c] - origin)
                     for _, a, b, c in cells]
            normals = [((a ^ b) + (b ^ c)).normal() for a, b, c in spans]
            normal = normals[0]
            if any(normal * n < .999 for n in normals):
                continue
            axis = spans[0][0].normal()
            result.append((vertex, [cell[1:] for cell in cells], (tuple(axis), tuple(normal ^ axis))))
        return result

    def _keep_support_cells_convex(self, points):
        import numpy as np

        for vertex, cells, axes in self._support_corner_cells:
            origin = points[vertex]
            a, b, c = (np.array([[tuple(points[i] - origin) for i in cell] for cell in cells])
                       @ np.array(axes).T).transpose(1, 0, 2)
            starts = np.concatenate((a, b, a))
            edges = np.concatenate((b - a, c - b, c - a))
            lengths = np.linalg.norm(edges, axis=1)
            if lengths.min() <= 1e-12:
                continue
            normals = np.column_stack((-edges[:, 1], edges[:, 0])) / lengths[:, None]
            offsets = np.einsum('ij,ij->i', normals, starts)
            margin = max(self.reference_width * 1e-5, 1e-10)
            if offsets.max() <= margin:
                continue
            offsets += margin
            first, second = np.triu_indices(len(normals), 1)
            x, y = normals[first], normals[second]
            determinant = x[:, 0] * y[:, 1] - x[:, 1] * y[:, 0]
            usable = abs(determinant) > 1e-8
            dv = (offsets[first, None] * y[:, ::-1] - offsets[second, None] * x[:, ::-1])
            intersections = dv[usable] * np.array((1., -1.)) / determinant[usable, None]
            choices = np.concatenate((normals * offsets[:, None], intersections))
            feasible = choices[(choices @ normals.T >= offsets - margin * .05).all(axis=1)]
            if len(feasible):
                correction = feasible[np.argmin(np.einsum('ij,ij->i', feasible, feasible))]
                points[vertex] += om.MVector(*(correction @ np.array(axes)))

    def _bevel_parallel_rows(self, tolerance, reference):
        """Build offset lines on the original faces at the ends of each strip."""
        from slide_for_maya.multiple import _ParallelRows
        import numpy as np

        guides = {}
        source_faces = {}
        for (ids, normal), face in zip(self.constraints, self.constraint_faces):
            for a, b in zip(ids, ids[1:] + ids[:1]):
                if frozenset((self.anchors[a], self.anchors[b])) not in self._selected_pairs:
                    continue
                tangent = (self.origins[b] - self.origins[a]).normal()
                inward = normal ^ tangent
                cross_section = [(reference[v] - self.origins[v]) * inward for v in (a, b)]
                if min(cross_section) < self.reference_width * .1:
                    continue
                edge = tuple(sorted((a, b)))
                guides.setdefault(edge, (np.asarray(tuple(inward)), om.MVector(self.origins[a]) * inward))
                source_faces.setdefault(edge, face)
        if not guides:
            return None
        seed, zero = (np.asarray([tuple(p)[:3] for p in points])
                      for points in (reference, self.origins))
        return _ParallelRows.from_guides(guides, seed, zero, tolerance,
                                         [source_faces[edge] for edge in guides])

    def evaluate_sharp(self, width, weights, count, spacing, keep_equal, source_topology, preserve_rails,
                       limit_supports=False, *, local_width_limits=False, corner_grids=False):
        """Keep the source crease and measure support spacing along its faces."""
        import numpy as np
        from slide_for_maya.multiple import _RowContacts

        self.effective_width, self.effective_weights = width, None
        self.at_contact, self.contact_faces = False, ()
        self._stopped_vertices = set()
        self._contact_seed = None
        self._fitted_corner_rails = ()
        if width <= 0.:
            return om.MPointArray([point * self.inverse for point in self.flat])

        profile = count, preserve_rails, limit_supports, corner_grids
        if getattr(self, "_sharp_rows", None) != profile:
            self._support_corner_cells = ()
            if preserve_rails:
                rays, rails, self._sharp_fixed = self._sharp_source_rails(source_topology, count)
                self._sharp_reference = [origin + ray for origin, ray in zip(self.origins, rays)]
            else:
                rays = [point - origin for point, origin in zip(self.flat, self.origins)]
                rails, self._sharp_fixed = {}, set()
                self._sharp_reference = self.flat
            self.__dict__.pop("_creases", None)
            self._support_vectors = (self._support_motion(source_topology, count, rays,
                                                          rails if limit_supports else {}, limit_supports)
                                     if count else [(ray, om.MVector()) for ray in rays])
            for vertex, axis in rails.items():
                ray, step = self._support_vectors[vertex]
                self._support_vectors[vertex] = axis * (ray * axis), axis * (step * axis)
            if self._source_axes or self._support_axes or self._rim_rails or self._rim_fixed:
                self._sharp_reference = [self._on_source(v, point) for v, point in enumerate(self._sharp_reference)]
            self._sharp_rows = profile
            self._sharp_seed = None
            self.__dict__.pop("_bevel_parallel", None)
            self._sharp_contact_topology = ([len(ids) for ids, _ in self.constraints],
                                            [v for ids, _ in self.constraints for v in ids])
            self._sharp_outer_vertices = sorted(set(self._sharp_contact_topology[1]))
            self._corner_grids = {}
            if corner_grids and count:
                from .corner_rows import sharp_corner_grids

                self._corner_grids = sharp_corner_grids(self._sharp_topology, self.anchors, self._selected_pairs,
                                                       self.source, count + 1)
        sample_step = self.reference_width / (count + 1)
        wanted = {a: max(width * max(0., weights.get(a, 1.)) + count * spacing,
                         self.reference_width * .005) for a in self.groups}

        def positions(widths):
            result = []
            for anchor, origin, (ray, motion) in zip(self.anchors, self.origins, self._support_vectors):
                if anchor is None:
                    result.append(origin + ray)
                    continue
                span = widths[anchor]
                scale = span / self.reference_width
                # Leave a positive strip at the crease if the rows
                # have to fit inside a narrow section.
                step = min(spacing, span / (count + .01))
                result.append(origin + ray * scale + motion * (step - sample_step * scale))
            if self._support_corner_cells:
                self._keep_support_cells_convex(result)
            return [self._on_source(v, point) for v, point in enumerate(result)]

        tolerance = max(1e-8, min(self.reference_width * .001, 1e-6))
        def sweep(seed, target, topology=None, parallel=None, *, independent=False):
            contacts = _RowContacts(self._sharp_topology if topology is None else topology,
                                    seed, target, 1., {}, tolerance,
                                    keep_convex=True, minimum_depth=tolerance * .25, parallel=parallel,
                                    local_edge_contacts=independent or topology is not None)
            if not independent:
                for group in self.groups.values():
                    moving = {vertex for vertex in group if contacts.anchors[vertex]}
                    for vertex in moving:
                        contacts.anchors[vertex] = moving
            return contacts

        seed = self._sharp_reference
        capped = None
        if count and limit_supports:
            # Only the residual source faces can run out of room. The
            # added rows are fitted after the available width is known.
            contacts = sweep(seed, positions(wanted), self._sharp_contact_topology)
            result = contacts.update(1.)
            limit = contacts.limits.min()
            if np.isfinite(limit) and keep_equal:
                result = contacts.points(limit)
            capped = {
                a: self.reference_width + (wanted[a] - self.reference_width) *
                   min(1., limit if keep_equal else contacts.limits[group].min())
                for a, group in self.groups.items()
            }
        elif count:
            # Establish the requested spacing before expanding the bevel.
            # A corner that stops during expansion retains that spacing.
            cached = self._sharp_seed
            if cached is None or cached[0] != spacing:
                span = max(self.reference_width * .15, spacing * (count + .25))
                candidate = positions(dict.fromkeys(self.groups, span))
                initial = sweep(self._sharp_reference, candidate)
                seed = initial.update(1.)
                self._sharp_seed = (spacing, seed)
            else:
                seed = cached[1]
        if not count and local_width_limits:
            if not hasattr(self, '_bevel_parallel'):
                self._bevel_parallel = self._bevel_parallel_rows(tolerance, seed)
                samples = np.asarray([tuple(p)[:3] for p in seed])
                if self._bevel_parallel is not None:
                    self._bevel_parallel.align(samples)
                self._bevel_parallel_seed = [self._on_source(v, om.MPoint(*p)) for v, p in enumerate(samples)]
            parallel = self._bevel_parallel if not any(abs(w - 1.) > 1e-9 for w in weights.values()) else None
            target = positions(wanted)
            if parallel is not None:
                # Start with parallel rows so their offsets stay in sync up
                # to contact. Other rows at the same corner can still move.
                seed = self._bevel_parallel_seed
                target = [origin + (p - origin) * (wanted[a] / self.reference_width) if a is not None else p
                          for p, origin, a in zip(seed, self.origins, self.anchors)]
            contacts = sweep(seed, target, parallel=parallel, independent=True)
            result = contacts.update(1.)
        elif capped is None:
            contacts = sweep(seed, positions(wanted))
            result = contacts.update(1.)
        stopped = set(np.flatnonzero(np.isfinite(contacts.limits)))
        if stopped:
            self.at_contact = True
            self._stopped_vertices = stopped
            self._contact_seed = contacts.seed
            self.contact_faces = tuple(face for (ids, _), face in zip(self.constraints, self.constraint_faces)
                                       if stopped.intersection(ids))
            if keep_equal and capped is None:
                result = contacts.points(contacts.limits.min())
        if capped is not None:
            # A row that would fold a miter keeps its place within the
            # native strip. The bevel contour can still move freely.
            proposed = positions(capped)
            proportional = [origin + (p - origin) * (capped[a] / self.reference_width)
                            if a is not None else p
                            for p, origin, a in zip(seed, self.origins, self.anchors)]
            for v in self._sharp_outer_vertices:
                proposed[v] = proportional[v] = om.MPoint(*result[v])
            result = sweep(proportional, proposed).update(1.)
            if stopped:
                # Contact can make an otherwise valid added face cross its
                # own boundary as an adjoining strip continues to move.
                # Recheck the actual output before joining the contact edges.
                final = sweep(seed, [om.MPoint(*point) for point in result])
                if final._projected_crossings(result, np.ones(len(final.pairs), dtype=bool)):
                    result = final.update(1.)
                    self._stopped_vertices |= set(np.flatnonzero(np.isfinite(final.limits)))
        if self._corner_grids:
            result = self._fit_sharp_corner_grids(result, spacing, tolerance)
        if self._source_axes or self._support_axes or self._rim_rails or self._rim_fixed:
            result = [self._on_source(v, om.MPoint(*p)) for v, p in enumerate(result)]
        if count and spacing and self._rim_rails:
            result = self._align_sharp_rim_rows(result, spacing, tolerance)
        return om.MPointArray([om.MPoint(*point) * self.inverse for point in result])

    def _align_sharp_rim_rows(self, points, spacing, tolerance):
        """Match a support row's interior offset at its open-border end."""
        from itertools import islice
        from slide_for_maya.multiple import _RowContacts

        counts, vertices = self._sharp_topology
        vertices = iter(vertices)
        ends = defaultdict(set)
        stopped = {self.anchors[v] for v in self._stopped_vertices}
        for count in counts:
            ring = tuple(islice(vertices, count))
            for a, b in zip(ring, ring[1:] + ring[:1]):
                if (a in self._rim_rails) == (b in self._rim_rails):
                    continue
                rim, inside = (a, b) if a in self._rim_rails else (b, a)
                pair = frozenset((self.anchors[rim], self.anchors[inside]))
                if pair in self._selected_pairs and not pair & stopped and rim not in self._rim_fixed:
                    ends[rim].add(inside)
        target = list(points)
        changed = False
        for rim, neighbors in ends.items():
            origin, rail = self.origins[rim], self._rim_rails[rim]
            current = (points[rim] - origin) * rail
            stations = []
            for inside in neighbors:
                tangent = (self.origins[inside] - origin).normal()
                across = rail - tangent * (rail * tangent)
                squared = across * across
                if squared < 1e-8:
                    break
                offset = points[inside] - origin
                station = (offset * across) / squared
                error = offset - tangent * (offset * tangent) - across * station
                if error.length() > max(tolerance, spacing * .05, abs(current) * .01):
                    break
                stations.append(station)
            else:
                if max(stations) - min(stations) <= max(tolerance, spacing * .05):
                    station = sum(stations) / len(stations)
                    shift = station - current
                    # The required travel along an oblique border can exceed
                    # the perpendicular spacing. Preserve the rail's side;
                    # the topology sweep below limits travel at contact.
                    if current * station > 0 and tolerance < abs(shift) <= max(spacing, abs(current)):
                        target[rim] = points[rim] + rail * shift
                        changed = True
        if not changed:
            return points
        return _RowContacts(self._sharp_topology, points, target, 1., {}, tolerance,
                            keep_convex=True, minimum_depth=tolerance * .25).update(1.)

    def _fit_sharp_corner_grids(self, result, spacing, tolerance):
        from .corner_rows import fit_sharp_corner
        from slide_for_maya.multiple import _RowContacts
        import numpy as np

        points = [om.MPoint(*p) for p in result]
        stopped = {self.anchors[v] for v in self._stopped_vertices}
        locked = defaultdict(set)
        for vertex in self._sharp_outer_vertices:
            anchor = self.anchors[vertex]
            if anchor in stopped:
                locked[anchor].add(vertex)
        corners = {anchor: fit_sharp_corner(grid, points, spacing, locked.get(anchor, ()))
                   for anchor, grid in self._corner_grids.items()}
        corners = {anchor: changes for anchor, changes in corners.items() if changes}
        while corners:
            proposed = list(points)
            for changes in corners.values():
                for v, point in changes.items():
                    proposed[v] = point
            probe = _RowContacts(self._sharp_topology, points, proposed, 1., {}, tolerance)
            trial = probe.points(1.)
            crossings = probe._projected_crossings(trial, np.ones(len(probe.pairs), dtype=bool))
            rejected = {self.anchors[v] for v in probe.pairs[crossings].ravel()} & corners.keys()
            if not rejected:
                self._fitted_corner_rails = tuple(path for anchor in corners for path in self._corner_grids[anchor][2])
                return trial
            for anchor in rejected:
                del corners[anchor]
        return result

    def weld_sharp_contacts(self, mesh):
        """Join generated edges that collapsed to the same position at contact."""
        from slide_for_maya.core import _position_tolerance

        if not self._stopped_vertices:
            return
        # MFnMesh has already rounded the deformed coordinates to float.
        points = [p * self.matrix for p in mesh.getPoints()]
        tolerance = _position_tolerance(points, self.matrix)
        squared = tolerance * tolerance
        short_edges = []
        for edge in range(mesh.numEdges):
            a, b = mesh.getEdgeVertices(edge)
            if a not in self._stopped_vertices and b not in self._stopped_vertices:
                continue
            gap = points[a] - points[b]
            if gap * gap > squared:
                continue
            # A short input edge or a deliberately small spacing present
            # before the width sweep is not a weld caused by contact.
            seed_edge = self._contact_seed[a] - self._contact_seed[b]
            if seed_edge @ seed_edge > squared:
                short_edges.append(edge)
        if short_edges:
            edges = [mesh.getEdgeVertices(edge) for edge in short_edges]
            topology = mesh.getVertices()
            mesh.collapseEdges(short_edges)
            return topology, points, edges, tolerance

    def _sharp_creases(self, mesh):
        if not hasattr(self, "_creases"):
            threshold = max(1e-10, self.reference_width * 1e-4)
            fixed = {i for i, (point, origin) in enumerate(zip(self.flat, self.origins))
                     if self.anchors[i] is not None and (point - origin).length() < threshold}
            fixed.update(getattr(self, "_sharp_fixed", ()))
            self._creases = []
            for edge in range(mesh.numEdges):
                a, b = mesh.getEdgeVertices(edge)
                if a in fixed and b in fixed and frozenset((self.anchors[a], self.anchors[b])) in self._selected_pairs:
                    self._creases.append(edge)
        creases = self._creases
        if self._fitted_corner_rails:
            seams = {tuple(sorted(edge)) for rail in self._fitted_corner_rails for edge in zip(rail, rail[1:])}
            ends = {rail[-1] for rail in self._fitted_corner_rails}
            candidates = set(self._sharp_fixed) | ends
            creases = [edge for edge in range(mesh.numEdges)
                       for a, b in (mesh.getEdgeVertices(edge),)
                       if tuple(sorted((a, b))) in seams or (
                           (a in ends or b in ends) and a in candidates and b in candidates and
                           frozenset((self.anchors[a], self.anchors[b])) in self._selected_pairs)]
            creases = sorted(set(creases).union(self._creases))
        return creases

    def _inherited_edge_flags(self, source):
        """Match surviving edges back to their authored hard/soft flags."""
        from slide_for_maya.core import _position_tolerance

        tolerance = _position_tolerance(self.source, self.matrix)
        native_precision = max(abs(p[axis]) for p in self.source for axis in range(3)) * (2. ** -22)
        tolerance = 2. * max(tolerance, native_precision)
        origins = [vertex if vertex is not None and (anchor is not None or
                   (p - self.source[vertex]).length() <= tolerance) else None
                   for vertex, anchor, p in zip(self._source_matches, self.anchors, self.flat)]
        flags = {}
        for edge in range(source.numEdges):
            pair = frozenset(source.getEdgeVertices(edge))
            if pair not in self._selected_pairs:
                flags[pair] = source.isEdgeSmooth(edge)
        return origins, flags

    def apply_edge_smoothing(self, mesh, source, *, multi=True, sharp=False):
        """Smooth the added faces and keep the flags on surviving source edges."""
        if self._smoothing is None:
            edge_faces = defaultdict(list)
            counts, connects = mesh.getVertices()
            template_faces = len(self._smoothing_topology[0])
            new_faces = self.bevel_faces | set(range(template_faces, len(counts)))
            start = 0
            for face, count in enumerate(counts):
                ids = list(connects[start:start+count])
                start += count
                for a, b in zip(ids, ids[1:] + ids[:1]):
                    edge_faces[tuple(sorted((a, b)))].append(face)
            origins, source_flags = self._inherited_edge_flags(source)
            edges, corners, inherited = [], set(), {}
            flags = [mesh.isEdgeSmooth(edge) for edge in range(mesh.numEdges)]
            for edge in range(mesh.numEdges):
                vertices = mesh.getEdgeVertices(edge)
                faces = edge_faces[tuple(sorted(vertices))]
                if len(faces) != 2:
                    continue
                source_edge = frozenset(origins[v] for v in vertices) if max(vertices) < len(origins) else None
                if source_edge in source_flags:
                    inherited[edge] = source_flags[source_edge]
                    flags[edge] = inherited[edge]
                elif new_faces.intersection(faces):
                    # A planar end cap also collapses at zero width, but is
                    # not a longitudinal bevel strip. Its border needs the
                    # angle rule even when both faces were generated by Bevel.
                    edges.append((edge,tuple(faces),len(self.strip_faces.intersection(faces))==2))
                    corners.update((face, vertex) for face in faces
                                   for vertex in vertices)
            pairs = sorted(corners)
            self._smoothing = edges, pairs, flags, inherited
            self._smoothing_topology = None
        edges, corners, source_flags, inherited = self._smoothing
        flags = list(source_flags)
        if edges:
            # Profile and width edits change the actual angle. Cache only the
            # affected topology, and evaluate its normals after each deformation.
            normals={face:mesh.getPolygonNormal(face).transformAsNormal(self.matrix).normal()
                     for _,adjacent,_ in edges for face in adjacent}
            for edge, (a, b), internal in edges:
                flags[edge] = internal and multi or normals[a] * normals[b] >= BOUNDARY_COSINE
        if sharp:
            for edge in self._sharp_creases(mesh):
                if edge not in inherited:
                    flags[edge] = False
        if corners:
            # Imported locked corner normals can keep a visible seam after
            # the edge flag is soft. Unlock only the locally generated joins.
            mesh.getNormals()
            faces, vertices = zip(*corners)
            mesh.unlockFaceVertexNormals(faces, vertices)
        # Unlocking can also harden adjacent edges. Restore the cached flags
        # on the rest of the mesh after updating the local normals.
        mesh.setEdgeSmoothings(list(range(mesh.numEdges)), flags)
        mesh.cleanupEdgeSmoothing()

    @staticmethod
    def _area(points,normal):
        center = points[0]
        return sum(((points[i]-center)^(points[i+1]-center))*normal
                   for i in range(1,len(points)-1))

    @staticmethod
    def _positive_root(constant,linear,quadratic):
        epsilon = 1e-12*max(abs(constant),abs(linear),abs(quadratic),1e-20)
        if abs(quadratic) <= epsilon:
            return -constant/linear if linear < -epsilon else math.inf
        discriminant = linear*linear-4*quadratic*constant
        if discriminant < -epsilon*max(abs(linear),abs(quadratic),abs(constant)):
            return math.inf
        root = math.sqrt(max(0,discriminant))
        q = -.5*(linear+math.copysign(root,linear))
        choices = (q/quadratic,constant/q) if abs(q)>epsilon else (-linear/(2*quadratic),)
        return min((v for v in choices if v>1e-8),default=math.inf)

    def _collision(self,origins,vectors):
        limit = math.inf
        for ids,normal in self.constraints:
            border,_ = self._border_contact(origins,vectors,ids)
            limit = min(limit,border)
            points = [origins[i] for i in ids]
            directions = [vectors[i] for i in ids]
            constant = self._area(points,normal)
            plus = self._area([p+d for p,d in zip(points,directions)],normal)
            minus = self._area([p-d for p,d in zip(points,directions)],normal)
            limit = min(limit,self._positive_root(constant,(plus-minus)*.5,(plus+minus)*.5-constant))
        return limit

    @staticmethod
    def _border_contact(points,movements,ids):
        """A residual border may collapse at one end before face area is zero."""
        limit,members = math.inf,()
        for a,b in zip(ids,ids[1:]+ids[:1]):
            span = points[b]-points[a]
            motion = movements[b]-movements[a]
            squared = motion*motion
            if squared<=1e-24:
                continue
            time = -(span*motion)/squared
            if time<=1e-8 or time>=limit:
                continue
            if (span+motion*time).length()<=max(span.length()*1e-8,1e-10):
                limit,members = time,(a,b)
        return limit,members

    def _rounded_units(self, shape, profile_kind, curve):
        units = []
        for i,(flat,anchor,origin) in enumerate(zip(self.flat,self.anchors,self.origins)):
            if anchor is None:
                units.append(om.MVector())
                continue
            t = self.samples[i]
            denominator = math.sin(math.pi*t)
            factor = profile_value(profile_kind,t,shape,curve)/denominator if denominator>1e-7 else 0
            units.append(((flat-origin)+(self.rounded[i]-flat)*factor)/self.reference_width)
        return units

    def _evaluate_independent(self, width, shape, weights, limits, profile_kind, curve):
        """Advance source sections up to contact, reusing the checked interval."""
        origins = self.origins
        units = self._rounded_units(shape, profile_kind, curve)
        key = (shape,profile_kind,curve.points,tuple(sorted(weights.items())))
        groups = self.groups

        def positions(field):
            return self._blend([origin + unit * field.get(anchor, 0.)
                                for origin, unit, anchor in zip(origins, units, self.anchors)])

        def motion(start, end):
            delta = {group: end[group] - start[group] for group in groups}
            return self._blend([unit * delta.get(anchor, 0.) for unit, anchor in zip(units, self.anchors)])

        # Most local gestures change only a few samples by a small amount.
        # Validate that short motion first; a fresh sweep from the collapsed
        # source would unnecessarily revisit the whole bevel on every frame.
        prior = getattr(self,"_independent_previous",None)
        if (key!=self._independent_key and prior and prior[0]==(width,shape,profile_kind,curve.points)
                and not self._independent_contacts):
            target = {a:max(width*max(0,weights.get(a,1)),self.reference_width*.005) for a in groups}
            points = positions(prior[1])
            movements = motion(prior[1], target)
            contact,_ = first_contact(points,movements,self.triangles,1,self._local_contact_cache)
            if min(contact,self._collision(points,movements))>1:
                self._independent_key,self._independent_bound = key,width
                self._independent_limits = dict.fromkeys(groups,math.inf)
        if key!=self._independent_key or width>self._independent_bound:
            bound = max(width*1.25,self.reference_width)
            intended = {a:max(0,weights.get(a,1))*bound for a in groups}
            current = dict.fromkeys(groups,0.0)
            frozen = set()
            caps = dict.fromkeys(groups, math.inf)
            contacts = []
            for _ in range(len(groups)+1):
                points = positions(current)
                movements = motion(current, intended)
                # Residual faces must retain positive projected area, even
                # when both faces of a contact share a border and CCD skips it.
                residual = []
                for (ids,normal),face in zip(self.constraints,self.constraint_faces):
                    base = [points[i] for i in ids]
                    directions = [movements[i] for i in ids]
                    constant = self._area(base,normal)
                    plus = self._area([p+d for p,d in zip(base,directions)],normal)
                    minus = self._area([p-d for p,d in zip(base,directions)],normal)
                    root = self._positive_root(constant,(plus-minus)*.5,(plus+minus)*.5-constant)
                    border,border_ids = self._border_contact(points,movements,ids)
                    limiting_ids = ids
                    if border < root:
                        root,limiting_ids = border,border_ids
                    if root <= 1.:
                        residual.append((root, face, limiting_ids))
                # Triangle sweeps only need to reach the first residual-face
                # limit. Keep the full motion in the solver for root accuracy.
                bound_fraction = min((event[0] for event in residual), default=1.)
                collision, faces = first_contact(points, movements, self.triangles, 1, self._independent_cache,
                                                 fraction_limit=min(1., bound_fraction + 1e-8))
                members = set(self._independent_cache.get("contact_vertices", ()))
                for root, face, limiting_ids in residual:
                    if root<collision-1e-8:
                        collision,faces,members = root,(face,),set(limiting_ids)
                    elif root<=collision+1e-8 and root<=1:
                        members.update(limiting_ids)
                        faces = tuple(set(faces+(face,)))
                if collision>1:
                    break
                factor = max(0,collision*(1-1e-4))
                for a in groups:
                    current[a] += (intended[a]-current[a])*factor
                affected = set().union(*(self.anchor_sets[i] for i in members if movements[i].length()>1e-12))-frozen
                if not affected:
                    # Degenerate input: retain the last safe state.
                    affected = set(groups)-frozen
                for a in affected:
                    caps[a] = intended[a] = current[a]
                threshold = min((current[a]/weights.get(a,1) for a in affected
                                 if weights.get(a,1)>1e-12),default=bound)
                contacts.append((threshold,faces))
                frozen.update(affected)
                if len(frozen)==len(groups):
                    break
            self._independent_key,self._independent_bound = key,bound
            self._independent_limits,self._independent_contacts = caps,contacts
        field = {a:max(self.reference_width*.005,min(width*max(0,weights.get(a,1)),
                                                   self._independent_limits[a])) for a in groups}
        self.contact_faces = tuple(sorted({face for threshold,faces in self._independent_contacts
                                          if width>=threshold*(1-1e-6) for face in faces}))
        self.at_contact = bool(self.contact_faces)
        self.clamped = sum(width*weights.get(a,1)>self._independent_limits[a] for a in groups)
        self.effective_width,self.effective_weights = width,None
        self._independent_previous = ((width,shape,profile_kind,curve.points),dict(field))
        return om.MPointArray([point * self.inverse for point in positions(field)])

    def _evaluate_rounded_sides(self, width, shape, weights, profile_kind, curve):
        """Advance each rim independently and keep the outline parallel at contact."""
        from slide_for_maya.multiple import _RowContacts
        import numpy as np

        tolerance = max(1e-8, min(self.reference_width * .001, 1e-6))
        uniform = not any(abs(value - 1.) > 1e-9 for value in weights.values())
        if uniform and not hasattr(self, "_round_parallel"):
            reference = [self._on_source(v, p, supports=False, rim_profile=False) for v, p in enumerate(self.flat)]
            self._round_parallel = self._bevel_parallel_rows(tolerance, reference)
        rows = self._round_parallel if uniform else None
        units = self._rounded_units(shape, profile_kind, curve)
        seed = self._blend([origin + unit * self.reference_width for origin, unit in zip(self.origins, units)])
        if rows is not None:
            aligned = np.asarray([tuple(point)[:3] for point in seed])
            rows.align(aligned)
            seed = [om.MPoint(*point) for point in aligned]
        seed = [self._on_source(v, point, supports=False, rim_profile=False) for v, point in enumerate(seed)]
        target = self._blend([origin + (point - origin) *
                              max(.005, width * max(0., weights.get(a, 1.)) / self.reference_width)
                              for origin, point, a in zip(self.origins, seed, self.anchors)])

        def surface(start, end, bound):
            cache = self._independent_cache
            fraction, faces = first_contact(start, end - start, self.triangles, 1., cache, fraction_limit=bound)
            return fraction, cache["contact_vertices"], faces

        interpolation = {v: tuple(blend.items()) for v, blend in self.point_blends.items()}
        contact = _RowContacts(self._rounded_topology, seed, target, 1., interpolation, tolerance,
                               parallel=rows, minimum_depth=tolerance * .25, local_edge_contacts=True,
                               surface_contact=surface)
        result = contact.update(1.)
        stopped = set(np.flatnonzero(np.isfinite(contact.limits)))
        if stopped and self._rim_rails:
            # Parallel row correction can advance one end of a residual
            # face after a neighboring end has stopped. Check the actual
            # positions on the source planes and border rails for crossing.
            target = [self._on_source(v, om.MPoint(*p), supports=False, rim_profile=False)
                      for v, p in enumerate(result)]
            check = _RowContacts(self._rounded_topology, seed, target, 1., interpolation, tolerance,
                                 minimum_depth=tolerance * .25, local_edge_contacts=True)
            result = check.points(1.)
            if check._projected_crossings(result, np.ones(len(check.pairs), dtype=bool)):
                result = check.update(1.)
                stopped.update(np.flatnonzero(np.isfinite(check.limits)))
        faces = {face for (ids, _), face in zip(self.constraints, self.constraint_faces) if stopped.intersection(ids)}
        self.contact_faces = tuple(sorted(faces | contact.surface_faces))
        self.at_contact = bool(self.contact_faces)
        self.clamped = len(stopped)
        self.effective_width, self.effective_weights = width, None
        return om.MPointArray([om.MPoint(*point) * self.inverse for point in result])

    def evaluate(self, width, shape, weights, limits, profile_kind="round", curve=None, keep_equal=True):
        curve = curve or Curve(((0, 0, None), (.5, .5, None), (1, 0, None)), False)
        if not keep_equal:
            self._previous = None
            if self._round_side_contacts:
                return self._evaluate_rounded_sides(width, shape, weights, profile_kind, curve)
            return self._evaluate_independent(width,shape,weights,limits,profile_kind,curve)
        # Use the same extent for both the deformation and its contact sweep.
        # Support rows can push the requested extent beyond the residual faces.
        if self.constraints:
            width = min(width, self.uniform_limit)
        parameters = (width,shape,profile_kind,curve.points)
        if self._previous and self._previous[0]==parameters and self._previous[1]==weights:
            return om.MPointArray(self._previous[2])
        self.effective_weights = None
        result = om.MPointArray()
        self.clamped = 0
        # Bound width by the first collapse of an original residual face.
        # Short segments along the selected line do not impose a width limit.
        selected_weights = [weights.get(v,1.0) for v in self.groups]
        uniform = bool(selected_weights) and max(selected_weights)-min(selected_weights) <= 1e-9
        shared_limit = min((limits.get(v,math.inf) for v in self.groups),default=math.inf)
        base = width
        field = {a:max(base*weights.get(a,1),self.reference_width*.005) for a in self.groups}
        if self.constraints and uniform:
            field = {a:min(value,self.uniform_limit) for a,value in field.items()}
        if self.constraints and not uniform:
            origins = self._blend([self.source[a]+(p-self.source[a])*(base/self.reference_width)
                       if a is not None else p for a,p in zip(self.anchors,self.flat)])
            vectors = self._blend([(p-self.source[a])*((field[a]-base)/self.reference_width)
                       if a is not None else om.MVector() for a,p in zip(self.anchors,self.flat)])
            factor = min(1,self._collision(origins,vectors)*(1-1e-4))
            field = {a:base+(target-base)*factor for a,target in field.items()}
        for index, flat in enumerate(self.flat):
            anchor = self.anchors[index]
            if anchor is None:
                result.append(flat * self.inverse)
                continue
            requested = field[anchor]
            limit = math.inf if self.constraints else shared_limit if uniform else limits.get(anchor, math.inf)
            actual = min(requested, limit)
            if requested > limit:
                self.clamped += 1
            # Keep a valid strip at zero local weight. The whole zero-width
            # operation is displayed as the original mesh and cannot commit.
            actual = max(actual, self.reference_width * .005)
            t = self.samples[index]
            denominator = math.sin(math.pi * t)
            value = profile_value(profile_kind, t, shape, curve)
            factor = value / denominator if denominator > 1e-7 else 0
            vector = flat - self.source[anchor]
            vector += (self.rounded[index] - flat) * factor
            point = self.source[anchor] + vector * (actual / self.reference_width)
            result.append(point * self.inverse)
        self._blend(result)
        self.effective_width = width
        self.at_contact = False
        local = self._previous and self._previous[0]==parameters and self._previous[1]!=weights
        if self.triangles and width>1e-9 and local:
            previous = [p*self.matrix for p in self._previous[2]]
            vectors = [p*self.matrix-origin for p,origin in zip(result,previous)]
            limit,faces = first_contact(previous,vectors,self.triangles,1,self._local_contact_cache)
            factor = min(1,limit*(1-1e-4))
            if factor<1:
                self.contact_faces = faces
                self.at_contact = True
                self.clamped += 1
                old_field = self._previous[3]
                self.effective_weights = {a:(old_field[a]+(field[a]-old_field[a])*factor)/base
                                          for a in field}
                field = {a:old_field[a]+(value-old_field[a])*factor for a,value in field.items()}
                result = om.MPointArray([(origin+vector*factor)*self.inverse
                                         for origin,vector in zip(previous,vectors)])
        elif self.triangles and width>1e-9:
            origins = self.origins
            vectors = [(p*self.matrix-origin)/width for p,origin in zip(result,origins)]
            # Width motion reuses a cached contact distance; profile and local
            # field changes invalidate it. No new topology per mouse update.
            key = tuple(tuple(round(v,9) for v in vector) for vector in vectors)
            if key!=self._contact_key or (width>self._contact_bound and math.isinf(self._contact_limit)):
                # Test a little ahead of the requested width. Sweeping to a
                # distant theoretical maximum makes small profile edits slow
                # on long curved selections; a checked interval is reusable.
                bound = min(self.uniform_limit,max(width*1.25,self.reference_width))
                self._contact_limit,self._shared_contact_faces = first_contact(origins,vectors,self.triangles,bound,self._contact_cache)
                self._contact_key = key
                self._contact_bound = bound
            self.contact_faces = self._shared_contact_faces
            factor = min(1,self._contact_limit*(1-1e-4)/width)
            self.at_contact = width>=self._contact_limit*(1-1e-4)*(1-1e-6)
            if factor<1:
                self.effective_width = width*factor
                self.clamped += 1
                result = om.MPointArray([(origin+vector*self.effective_width)*self.inverse
                                         for origin,vector in zip(origins,vectors)])
        self._previous = (parameters,dict(weights if self.effective_weights is None else self.effective_weights),
                          om.MPointArray(result),dict(field))
        return result
