"""Trace supported bevels across quad profiles and split them at junctions."""
from collections import defaultdict
from statistics import median

from .bevel_detection import _Mesh, _components


def _sections(data):
    """Find profiles with a narrow support on both sides of the bevel."""
    result = {}
    for face, edges in enumerate(data.face_edges):
        if len(edges) != 4:
            continue
        for start in edges:
            strip, rails, widths = [], [], []
            current, edge = face, start
            for _ in range(4):
                ring = data.face_edges[current]
                if len(ring) != 4 or current in strip:
                    break
                i = ring.index(edge)
                strip.append(current)
                rails.append(edge)
                widths.append((data.lengths[ring[i - 1]] + data.lengths[ring[(i + 1) % 4]]) * .5)
                edge = ring[(i + 2) % 4]
                following = [f for f in data.edge_faces[edge] if f != current]
                if len(following) != 1:
                    break
                current = following[0]
            if len(strip) != 4 or min(widths[1:3]) <= data.width_tolerance:
                continue
            outer = max(widths[0], widths[-1])
            inner = min(widths[1:3])
            if outer >= .42 * inner or max(widths[1:3]) >= 2.5 * inner:
                continue
            if data.normals[strip[1]] * data.normals[strip[2]] > .985:
                continue
            key = tuple(sorted(strip))
            rails.append(edge)
            if key not in result:
                result[key] = (tuple(strip), tuple(rails), sum(widths))
    ranked = sorted(result.values(), key=lambda row: (
        max(data.lengths[row[1][0]], data.lengths[row[1][-1]]) /
        max(row[2], data.width_tolerance), row[0]))
    kept = []
    directions = defaultdict(set)
    for strip, rails, width in ranked:
        orientation = {face: data.face_edges[face].index(edge) % 2 for face, edge in zip(strip, rails)}
        if any(orientation[face] in directions[face] for face in strip):
            continue
        for face, parity in orientation.items():
            directions[face].add(parity)
        kept.append((strip, rails, width))
    return tuple(kept)


def _detect(mesh):
    data = _Mesh(mesh)
    sections = _sections(data)
    if not sections:
        return (), ()
    members = defaultdict(set)
    for index, (strip, _, _) in enumerate(sections):
        for face in strip:
            members[face].add(index)
    # Crossing transverse profiles describe a shared junction.
    junction = {index for indices in members.values() if len(indices) > 1 for index in indices}
    neighbors = defaultdict(set)
    for index, (strip, rails, _) in enumerate(sections):
        if index in junction:
            continue
        for face, a, b in zip(strip, rails, rails[1:]):
            for edge in set(data.face_edges[face]) - {a, b}:
                for other in data.edge_faces[edge]:
                    for candidate in members[other] - junction - {index}:
                        neighbors[index].add(candidate)
    remaining = set(range(len(sections))) - junction
    bands = tuple(tuple(sorted({face for index in region for face in sections[index][0]}))
                  for region in _components(remaining, neighbors))
    regular = {face for band in bands for face in band}
    corners = {face for index in junction for face in sections[index][0]} - regular
    # Distorted or collapsed cap cells need not contain a complete profile.
    # Include their local quad patch so they share the junction displacement.
    size = median(width for _, _, width in sections) * 1.5
    eligible = {face for face, edges in enumerate(data.face_edges)
                if face not in regular and len(edges) in (3, 4)
                and max(data.lengths[e] for e in edges) <= size}
    pending = sorted(corners)
    for face in pending:
        added = data.neighbors[face] & eligible - corners
        corners.update(added)
        pending.extend(sorted(added))
    return bands, tuple(sorted(corners))


def detect(mesh):
    from .search_cache import CACHE

    entry = CACHE.get(mesh)
    cached = getattr(entry, '_supported_ribbons', None)
    if cached is None or cached[0] != entry.version:
        cached = entry.version, _detect(mesh)
        entry._supported_ribbons = cached
    return cached[1]
