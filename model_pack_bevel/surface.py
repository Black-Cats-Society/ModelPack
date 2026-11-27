"""Source planes and open-border rails for the bevel template."""
from collections import Counter, defaultdict
from itertools import islice

from maya.api import OpenMaya as om


def _boundary(topology):
    counts, vertices = topology
    vertices = iter(vertices)
    edges = {}
    for face, count in enumerate(counts):
        ring = tuple(islice(vertices, count))
        for a, b in zip(ring, ring[1:] + ring[:1]):
            key = tuple(sorted((a, b)))
            edges[key] = (a, b, face) if key not in edges else None
    neighbors = defaultdict(set)
    for a, b in (edge for edge, use in edges.items() if use is not None):
        neighbors[a].add(b)
        neighbors[b].add(a)
    return neighbors, edges


def open_rim(source_topology, topology, source, flat, groups, anchors, tolerance, sharp, supports):
    """Match each split termination to the two original border edges."""
    source_rim, source_edges = _boundary(source_topology)
    if not source_rim.keys() & groups.keys():
        return {}, {}, frozenset(), ()
    template_rim, template_edges = _boundary(topology)
    # Match uncut neighbors to the known endpoint of their original border.
    def on_source(vertex, endpoint):
        anchor = anchors[vertex]
        return (anchor == endpoint if anchor is not None else
                (flat[vertex] - source[endpoint]).length() <= tolerance)

    rails, fixed, ends_on_rails, caps = {}, {}, set(), []
    for origin in sorted(source_rim.keys() & groups.keys()):
        ends = sorted(source_rim[origin])
        members = set(groups[origin]) & template_rim.keys()
        if len(ends) != 2 or not members:
            continue
        if any(len(template_rim[v]) != 2 for v in members):
            continue
        terminals = [[v for v in members if any(on_source(w, end)
                      for w in template_rim[v] - members)] for end in ends]
        if any(len(side) != 1 for side in terminals):
            continue
        path = [terminals[0][0]]
        remaining = members - set(path)
        while remaining:
            following = template_rim[path[-1]] & remaining
            if len(following) != 1:
                break
            vertex = following.pop()
            path.append(vertex)
            remaining.remove(vertex)
        if remaining or path[-1] != terminals[1][0]:
            continue
        for vertex, end in zip((path[0], path[-1]), ends):
            for neighbor in template_rim[vertex] - members:
                if anchors[neighbor] is None and on_source(neighbor, end):
                    fixed[neighbor] = source[end]
        rays = [source[end] - source[origin] for end in ends]
        if min(ray.length() for ray in rays) <= tolerance:
            continue
        first, second = (ray.normal() for ray in rays)
        ends_on_rails.update((path[0], path[-1]))
        if (first + second).length() <= 1e-7:
            rails.update(dict.fromkeys(path, first))
            continue
        if sharp:
            # The flat profile already contains its crease vertex. Use its
            # topology station on asymmetric junctions as well.
            middle = min(range(len(path)), key=lambda i: (flat[path[i]] - source[origin]).length())
            fixed[path[middle]] = source[origin]
            rails.update(dict.fromkeys(path[:middle], first))
            rails.update(dict.fromkeys(path[middle + 1:], second))
        elif len(path) >= 2:
            rows = min(supports + 1, len(path) // 2)
            core = path[rows - 1:len(path) - rows + 1]
            edges = [template_edges[tuple(sorted((a, b)))] for a, b in zip(core, core[1:])]
            source_face = source_edges[tuple(sorted((origin, ends[0])))][2]
            caps.append((edges, origin, source_face))
            rails.update(dict.fromkeys(path[:rows], first))
            rails.update(dict.fromkeys(path[-rows:], second))
        else:
            fixed[path[0]] = source[origin]
    return rails, fixed, ends_on_rails, tuple(caps)


def plane_axes(normals):
    """Normals of independent planes through the same source point."""
    basis = []
    for normal in normals:
        missing = normal - sum((axis * (normal * axis) for axis in basis), om.MVector())
        if missing.length() > .001:
            basis.append(missing.normal())
    return tuple(basis)


def append_rim_caps(mesh, source, caps):
    """Close the chord of a split rim termination with its source corner."""
    if not caps:
        return ()
    points = mesh.getPoints()
    count = Counter(tuple(point)[:3] for point in points)
    source_points = source.getPoints()
    cap_points = []
    for edges, anchor, source_face in caps:
        corner = om.MPoint(source_points[anchor])
        corner_key = tuple(corner)[:3]
        keys = [tuple(points[v])[:3] for v in {v for a, b, _ in edges for v in (a, b)}]
        # A coincident shell must never be joined by a positional match.
        if count[corner_key] or any(count[key] != 1 for key in keys):
            continue
        corner_uvs = {}
        for a, b, bevel_face in edges:
            face = mesh.addPolygon((points[b], points[a], corner), True, 1e-12)
            _cap_uvs(mesh, face, bevel_face, b, a, source, source_face, anchor, corner_uvs)
        cap_points.append(corner)
        count[corner_key] += 1
    return tuple(cap_points)


def _cap_uvs(mesh, face, bevel_face, a, b, source, source_face, anchor, corner_uvs):
    rim = list(mesh.getPolygonVertices(bevel_face))
    source_corner = list(source.getPolygonVertices(source_face)).index(anchor)
    for uv_set in set(mesh.getUVSetNames()) & set(source.getUVSetNames()):
        try:
            ends = [mesh.getPolygonUVid(bevel_face, rim.index(v), uv_set) for v in (a, b)]
            if uv_set not in corner_uvs:
                u, v = source.getPolygonUV(source_face, source_corner, uv_set)
                corner = mesh.numUVs(uv_set)
                mesh.setUV(corner, u, v, uv_set)
                corner_uvs[uv_set] = corner
        except RuntimeError:  # Inputs may have no UVs for these faces.
            continue
        for index, uv in enumerate((*ends, corner_uvs[uv_set])):
            mesh.assignUV(face, index, uv, uv_set)
