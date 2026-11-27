"""Recover editable bevel strips between hard-edge guides."""
from collections import defaultdict
import math

from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn, SlideError

from .bevel_detection import _cone
from .seeded_detection import LocalMesh, walk_profile
from .support_edit import supported_band
from .unbevel_faces import prepare


def hard_edges(mesh, edges=None):
    fn = _mesh_fn(mesh)
    iterator = om.MItMeshEdge(fn.dagPath())
    result = set()
    for edge in range(fn.numEdges) if edges is None else edges:
        if fn.isEdgeSmooth(edge):
            continue
        iterator.setIndex(edge)
        if len(iterator.getConnectedFaces()) == 2:
            result.add(edge)
    return result


def creation_edges(mesh):
    """Object selection uses geometric creases, not imported normal splits.

    FBX can mark every tessellation edge hard. Beveling nearly coplanar
    splits generates microscopic miters that stop the entire equal-width
    operation. Explicit component selection still accepts these edges.
    """
    fn = _mesh_fn(mesh)
    iterator = om.MItMeshEdge(fn.dagPath())
    cosine = math.cos(math.radians(1.))
    result = set()
    for edge in hard_edges(mesh):
        iterator.setIndex(edge)
        first, second = iterator.getConnectedFaces()
        a = fn.getPolygonNormal(first, om.MSpace.kWorld)
        b = fn.getPolygonNormal(second, om.MSpace.kWorld)
        if a * b < cosine:
            result.add(edge)
    return result


def _depth(data, face, edge):
    a, b = (data.points[v] for v in data.fn.getEdgeVertices(edge))
    axis = (b - a).normal()
    return max(((data.points[v] - a) ^ axis).length() for v in data.faces[face])


def _sections(data, edge, first, guides):
    """Walk across a quad profile until its opposite hard rim."""
    parent = next(f for f in data.edge_faces[edge] if f != first)
    normal = data.normals[parent]
    section, incoming, seen = [], [], {parent}
    face, entry = first, edge
    width = 0.
    for _ in range(80):
        if face in seen:
            break
        ring = data.face_edges[face]
        if len(ring) != 4:
            break
        seen.add(face)
        index = ring.index(entry)
        width += (data.lengths[ring[(index - 1) % 4]] + data.lengths[ring[(index + 1) % 4]]) * .5
        section.append((face, index % 2))
        incoming.append(entry)
        opposite = ring[(index + 2) % 4]
        neighbors = [f for f in data.edge_faces[opposite] if f != face]
        if len(neighbors) != 1:
            break
        other = neighbors[0]
        if not data.fn.isEdgeSmooth(opposite):
            normals = (normal, data.normals[other])
            has_room = opposite in guides or width <= 1.3 * max(_depth(data, parent, edge), _depth(data, other, opposite))
            if has_room and all(_cone(data.normals[f], normals, 2) for f, _ in section):
                angle = math.acos(max(-1., min(1., normals[0] * normals[1])))
                yield section, incoming, angle
                break
            # An explicitly selected opposing guide bounds this candidate
            # even if the enclosed surface cannot be reconstructed.
            if opposite in guides:
                break
        face, entry = other, opposite


def _strip_key(data, band, rows, guides, require_rims):
    if not rows or len({len(row) for row in rows}) != 1:
        return (0, 0)
    owner = {v: index for index, row in enumerate(rows) for v in row}
    if len(owner) != sum(map(len, rows)):
        return (0, 0)
    columns = [set(column) for column in zip(*rows)]
    by_vertex = {v: col for col, members in enumerate(columns) for v in members}
    if require_rims:
        boundary = {edge for f in band for edge in data.face_edges[f]
                    if len(band.intersection(data.edge_faces[edge])) == 1}
        for column in (columns[0], columns[-1]):
            rails = [edge for edge in boundary if column.issuperset(data.fn.getEdgeVertices(edge))]
            if not rails or sum(not data.fn.isEdgeSmooth(edge) for edge in rails) < .6 * len(rails):
                return (0, 0)
    hits = defaultdict(int)
    for edge in guides:
        if not band.intersection(data.edge_faces[edge]):
            continue
        a, b = data.fn.getEdgeVertices(edge)
        if a in by_vertex and by_vertex[a] == by_vertex.get(b):
            hits[by_vertex[a]] += 1
    # Two matching rims identify even a wider planar chamfer next to a
    # narrower fillet. Ignore hard edges crossing the profile at its ends.
    paired = min(hits[0], hits[len(columns) - 1])
    return paired, sum(hits.values())


def _editable_key(data, mesh, band, guides, *, require_rims=True):
    details = {}
    try:
        _, rows, _ = prepare(mesh, band, details=details)
    except ValueError:
        return (0, 0)
    if details.get('regions'):
        return (0, 0)
    return _strip_key(data, band, rows, guides, require_rims)


def _guide_bands(data, mesh, guides):
    scores = {}
    turns = {}
    visited = set()
    for edge in sorted(guides):
        for face in data.edge_faces[edge]:
            for section, incoming, angle in _sections(data, edge, face, guides):
                count = len(section)
                if (edge, face, count) in visited:
                    continue
                band = frozenset(walk_profile(data, section, incoming))
                if band in scores:
                    continue
                scores[band] = _editable_key(data, mesh, band, guides)
                turns[band] = angle
                if scores[band][1] >= 2:
                    visited.update((e, f, count) for e in guides for f in data.edge_faces[e] if f in band)
    return scores, turns


def bands_from_hard_rims(mesh):
    """Find separate editable strips when the full mesh search merges them."""
    guides = hard_edges(mesh)
    if len(guides) < 2:
        return ()
    data = LocalMesh(mesh)
    try:
        scores, _ = _guide_bands(data, mesh, guides)
        candidates = sorted((band for band, score in scores.items() if score[0]),
                            key=lambda band: (scores[band], len(band), -min(band)), reverse=True)
        occupied = set()
        bands = []
        for band in candidates:
            if band.isdisjoint(occupied):
                bands.append(tuple(sorted(band)))
                occupied.update(band)
        return tuple(bands)
    finally:
        data.release()


def find_band(mesh, guides):
    """Resolve the selected rails to a reconstructible bevel on this mesh."""
    data = LocalMesh(mesh)
    try:
        scores, turns = _guide_bands(data, mesh, guides)
        strongest = max(scores.values(), default=(0, 0))
        # Supports can lie on tangent faces beyond the hard core rim. A
        # retained original crease can also run through their middle.
        promising = [band for band, score in scores.items() if score == strongest and score[1] >= 2]
        if len(promising) > 1 and not strongest[0]:
            # On a thin shell, a neighboring return can share the whole
            # guide. A much smaller turn is the ordinary chamfer candidate.
            least_turn = min(turns[band] for band in promising)
            promising = [band for band in promising if turns[band] <= least_turn + math.radians(15.)]
        if len(promising) == 1:
            band = promising[0]
            seeds = sorted({f for e in guides for f in data.edge_faces[e] if f in band})
        elif promising:
            raise SlideError('Выделенные хард-рёбра относятся к нескольким фаскам. Выберите границу одной фаски.')
        else:
            band = None
            seeds = sorted({f for e in guides for f in data.edge_faces[e]})
        # Sample both ends and the middle to avoid repeating a strip search
        # for every face of a long or fully selected loop.
        if seeds:
            sample = {seeds[i * (len(seeds) - 1) // 5] for i in range(6)}
            supported = set()
            for face in sorted(sample):
                if any(face in match for match in supported):
                    continue
                match = supported_band(mesh, {face}, data=data)
                if (match and (band is None or band <= match)
                        and _editable_key(data, mesh, match, guides, require_rims=False)[1] >= 2):
                    supported.add(match)
            if len(supported) == 1:
                return tuple(sorted(supported.pop()))
            if len(supported) > 1 and band is None:
                raise SlideError('Выделенные хард-рёбра относятся к нескольким фаскам. Выберите границу одной фаски.')
        return tuple(sorted(band)) if band is not None else None
    finally:
        data.release()
