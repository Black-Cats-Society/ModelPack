"""Fit the three quad sectors around an existing sharp corner."""
from collections import Counter, defaultdict
from itertools import islice


def _sides(ring):
    return tuple(tuple(sorted(pair)) for pair in zip(ring, ring[1:] + ring[:1]))


def _distances(links, start):
    lengths = dict.fromkeys(start, 0)
    pending = list(start)
    for vertex in pending:
        for neighbor in links[vertex] - lengths.keys():
            lengths[neighbor] = lengths[vertex] + 1
            pending.append(neighbor)
    return lengths


def _corner_sectors(faces, outside, steps):
    edge_faces = defaultdict(set)
    links = defaultdict(set)
    adjacent = defaultdict(set)
    for face, ring in enumerate(faces):
        for a, b in _sides(ring):
            edge_faces[a, b].add(face)
            links[a].add(b)
            links[b].add(a)
        for i, vertex in enumerate(ring):
            adjacent[vertex, ring[i - 1]].add(ring[(i + 1) % 4])
            adjacent[vertex, ring[(i + 1) % 4]].add(ring[i - 1])
    border = {v for edge, uses in edge_faces.items() if len(uses) == 1 for v in edge}
    centers = [v for v, neighbors in links.items() if v not in border and len(neighbors) == 3]
    if len(centers) != 1:
        return ()
    center, = centers
    rails = []
    for neighbor in sorted(links[center]):
        path = [center, neighbor]
        while path[-1] not in border and len(path) <= steps:
            previous, vertex = path[-2:]
            following = links[vertex] - adjacent[vertex, previous] - set(path)
            if len(following) != 1:
                break
            path.append(next(iter(following)))
        if len(path) != steps + 1 or len(outside[path[-1]]) != 1 or path[-1] not in border:
            return ()
        rails.append((tuple(path), next(iter(outside[path[-1]]))))
    if len({v for _, v in rails}) != 3:
        return ()
    seams = {tuple(sorted(edge)) for path, _ in rails for edge in zip(path, path[1:])}
    pending = set(range(len(faces)))
    sectors = []
    while pending:
        part = [pending.pop()]
        sector_links = defaultdict(set)
        for face in part:
            for a, b in _sides(faces[face]):
                sector_links[a].add(b)
                sector_links[b].add(a)
                if (a, b) not in seams:
                    neighbors = edge_faces[a, b] & pending
                    pending.difference_update(neighbors)
                    part.extend(sorted(neighbors))
        borders = [i for i, (path, _) in enumerate(rails) if all(v in sector_links for v in path)]
        if len(part) != steps * steps or len(borders) != 2:
            return ()
        a, b = borders
        across = _distances(sector_links, rails[b][0])
        down = _distances(sector_links, rails[a][0])
        grid = {(across[v], down[v]): v for v in sector_links}
        if len(grid) != len(sector_links) or set(grid) != {(i, j) for i in range(steps + 1) for j in range(steps + 1)}:
            return ()
        sectors.append((a, b, grid))
    return (rails, sectors) if len({frozenset((a, b)) for a, b, _ in sectors}) == 3 else ()


def sharp_corner_grids(topology, anchors, selected, source, steps):
    counts, vertices = topology
    vertices = iter(vertices)
    sections, outside = defaultdict(list), defaultdict(set)
    degree = Counter(v for edge in selected for v in edge)
    for count in counts:
        ring = tuple(islice(vertices, count))
        members = {anchors[v] for v in ring}
        if count == 4 and len(members) == 1:
            anchor, = members
            if degree[anchor] == 3:
                sections[anchor].append(ring)
        for a, b in _sides(ring):
            first, second = anchors[a], anchors[b]
            if frozenset((first, second)) in selected:
                outside[a].add(second)
                outside[b].add(first)
    grids = {}
    for anchor, faces in sections.items():
        if len(faces) != 3 * steps * steps:
            continue
        matched = _corner_sectors(faces, outside, steps)
        if not matched:
            continue
        origin = source[anchor]
        rails, sectors = matched
        axes = tuple((source[v] - origin).normal() for _, v in rails)
        if any(abs(axes[a] * axes[b]) >= .98 for a, b, _ in sectors):
            continue
        grids[anchor] = origin, axes, tuple(path for path, _ in rails), sectors
    return grids


def fit_sharp_corner(grid, points, spacing, locked=()):
    origin, axes, rails, sectors = grid
    steps = len(rails[0]) - 1
    lengths = defaultdict(list)
    extents = []
    positions = {rails[0][0]: origin}
    for a, b, vertices in sectors:
        first, second = axes[a], axes[b]
        dot = first * second
        sine = (1. - dot * dot) ** .5
        tip = points[vertices[steps, steps]] - origin
        x, y = ((tip * first - dot * (tip * second)) / (sine * sine),
                (tip * second - dot * (tip * first)) / (sine * sine))
        if min(x, y) <= 1e-10:
            return {}
        stepx, stepy = (min(spacing / sine, extent / (steps - .99)) for extent in (x, y))
        values_x = tuple(x - (steps - i) * stepx for i in range(1, steps + 1))
        values_y = tuple(y - (steps - i) * stepy for i in range(1, steps + 1))
        lengths[a].append(values_x)
        lengths[b].append(values_y)
        extents.append((a, b, vertices, values_x, values_y))

    # Both faces of a seam must use the same stations along the original
    # edge. Computing the support points per sector and only then moving the
    # seam can put the first segment of an otherwise straight row off-axis.
    shared = {}
    for axis, choices in lengths.items():
        first, second = choices
        # A shallow mismatch can be shared between both faces. Averaging
        # keeps the row regular and splits the error against a locked border.
        if max(abs(a - b) for a, b in zip(first, second)) <= spacing * .04:
            shared[axis] = tuple((a + b) * .5 for a, b in zip(first, second))
    if locked and len(shared) != len(axes):
        return {}

    for a, b, vertices, values_x, values_y in extents:
        first, second = axes[a], axes[b]
        values_x = shared.get(a, values_x)
        values_y = shared.get(b, values_y)
        for i, j in vertices:
            if i and j:
                positions[vertices[i, j]] = origin + first * values_x[i - 1] + second * values_y[j - 1]
    for i, (axis, rail) in enumerate(zip(axes, rails)):
        values = shared[i] if i in shared else tuple(map(min, zip(*lengths[i])))
        for vertex, distance in zip(rail[1:], values):
            positions[vertex] = origin + axis * distance
    # The collision sweep owns the residual faces. Interior supports can
    # still be straightened inside the space allowed by those fixed borders.
    for vertex in locked:
        positions.pop(vertex, None)
    return positions
