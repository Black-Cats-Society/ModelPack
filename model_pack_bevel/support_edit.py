"""Find and preserve the planar support rows of a baked bevel."""
from collections import Counter
from statistics import median

from .session import MAX_SUPPORT_EDGES


def _profile(points, planar_boundary, compact=False):
    steps = [b - a for a, b in zip(points, points[1:])]
    lengths = [step.length() for step in steps]
    if not lengths or min(lengths) <= 1e-9:
        return None
    directions = [step / length for step, length in zip(steps, lengths)]
    if not .65 < lengths[0] / lengths[-1] < 1.55:
        return None
    for count in range(1, min(MAX_SUPPORT_EDGES, (len(steps) - 1) // 2) + 1):
        ends = (range(count), range(len(steps) - 1, len(steps) - 1 - count, -1))
        if any(any(not .65 < lengths[i] / lengths[side[0]] < 1.55 or
                   directions[i] * directions[side[0]] < .99 for i in side) for side in ends):
            break
        if not all(lengths[j] > 1.55 * lengths[i] or
                   (planar_boundary and directions[i] * directions[j] < .999)
                   for i, j in ((count - 1, count), (-count, -count - 1))):
            continue
        if directions[0] * directions[-1] > .99:
            continue
        core = lengths[count:-count]
        if compact and max(core) > min(core) * 2.5:
            continue
        sharp = (len(steps) - 2 * count == 2 and directions[0] * directions[count] > .99
                 and directions[-1] * directions[-count - 1] > .99)
        return count, sharp
    return None


def support_profile(points, rows, planar_boundary=False, *, compact=False):
    """Require consistent support rows across the cross sections of the band."""
    if not rows or len({len(row) for row in rows}) != 1 or len(rows[0]) < 4:
        return None
    votes = Counter(_profile([points[v] for v in row], planar_boundary, compact) for row in rows)
    votes.pop(None, None)
    if not votes:
        return None
    # A miter at an oblique end can have a noisier transverse section.
    count = Counter({n: sum(v for (k, _), v in votes.items() if k == n) for n, _ in votes})
    n, sections = count.most_common(1)[0]
    if sections < len(rows) * .7:
        return None
    return n, votes[n, True] >= sections * .7


def baked_support_profile(data, faces, rows):
    profile = support_profile(data.points, rows)
    if profile is not None or not rows or min(map(len, rows)) < 4:
        return profile
    # A dense round profile can have shorter spans than its supports.
    # In that case the land must continue the original face tangentially.
    ends = [{row[i] for row in rows} for i in (0, -1)]
    adjacent = [[], []]
    for face in faces:
        for edge in data.face_edges[face]:
            outside = [f for f in data.edge_faces[edge] if f not in faces]
            if len(outside) != 1:
                continue
            pair = set(data.fn.getEdgeVertices(edge))
            for side, values in zip(ends, adjacent):
                if pair <= side:
                    values.append(data.normals[face] * data.normals[outside[0]] > .99995)
    if all(side and sum(side) >= len(side) * .8 for side in adjacent):
        return support_profile(data.points, rows, planar_boundary=True)
    return None


def read_support_profile(mesh, faces, rows):
    from .seeded_detection import LocalMesh

    data = LocalMesh(mesh)
    try:
        return baked_support_profile(data, set(faces), rows)
    finally:
        data.release()


def support_steps(points, rows, count):
    gaps = []
    for row in rows:
        positions = [points[v] for v in row]
        gaps.append((tuple(p - positions[count] for p in positions[:count][::-1]),
                     tuple(p - positions[-count - 1] for p in positions[-count:])))
    step = median(delta[-1].length() / count for pair in gaps for delta in pair)
    return gaps, step


def _across(data, seed, edge):
    face = seed
    seen = {seed}
    result = []
    for _ in range(64 + 2 * MAX_SUPPORT_EDGES):
        other = [f for f in data.edge_faces[edge] if f != face]
        if len(other) != 1 or other[0] in seen:
            break
        face = other[0]
        result.append((face, edge))
        edges = data.face_edges[face]
        if len(edges) != 4:
            break
        seen.add(face)
        edge = edges[(edges.index(edge) + 2) % 4]
    return result


def _cell_rows(data, path, first):
    rows = [[v] for v in data.fn.getEdgeVertices(first)]
    edge = first
    for face in path:
        edges = data.face_edges[face]
        pair = set(data.fn.getEdgeVertices(edges[(edges.index(edge) + 2) % 4]))
        ring = data.faces[face]
        for row in rows:
            index = ring.index(row[-1])
            adjacent = pair & {ring[index - 1], ring[(index + 1) % 4]}
            if len(adjacent) != 1:
                return ()
            row.append(adjacent.pop())
        edge = edges[(edges.index(edge) + 2) % 4]
    return rows


def _profiles_at(data, seed):
    edges = data.face_edges[seed]
    if len(edges) != 4:
        return
    for i in (0, 1):
        first, last = edges[i], edges[i + 2]
        before, after = _across(data, seed, first), _across(data, seed, last)
        for left, (surface_a, _) in enumerate(before):
            incoming = [e for _, e in before[:left][::-1]]
            previous = [f for f, _ in before[:left][::-1]]
            start = previous[0] if previous else seed
            if data.normals[start] * data.normals[surface_a] < .99:
                continue
            for right, (surface_b, _) in enumerate(after):
                outgoing = [e for _, e in after[:right]]
                following = [f for f, _ in after[:right]]
                end = following[-1] if following else seed
                if data.normals[end] * data.normals[surface_b] < .99:
                    continue
                path = previous + [seed] + following
                if not 3 <= len(path) <= 64 + 2 * MAX_SUPPORT_EDGES or len(set(path)) != len(path):
                    continue
                entrance = before[left][1]
                rows = _cell_rows(data, path, entrance)
                tangent = (data.normals[start] * data.normals[surface_a] > .99995 and
                           data.normals[end] * data.normals[surface_b] > .99995)
                if rows and support_profile(data.points, rows, tangent, compact=True):
                    entering = [entrance, *incoming, *outgoing]
                    yield [(f, data.face_edges[f].index(e) % 2) for f, e in zip(path, entering)], entering


def supported_band(mesh, faces, *, data=None):
    """Expand a seed only when a complete, supported strip can be recovered."""
    from .seeded_detection import LocalMesh, walk_profile
    from .unbevel_faces import prepare

    owned = data is None
    if owned:
        data = LocalMesh(mesh)
    try:
        candidates = set()
        for profile, edges in _profiles_at(data, min(faces)):
            band = frozenset(walk_profile(data, profile, edges))
            if not faces <= band:
                continue
            try:
                _, rows, _ = prepare(mesh, band)
            except ValueError:
                continue
            if baked_support_profile(data, band, rows):
                candidates.add(band)
        complete = [band for band in candidates if not any(band < other for other in candidates)]
        return complete[0] if len(complete) == 1 else None
    finally:
        if owned:
            data.release()
