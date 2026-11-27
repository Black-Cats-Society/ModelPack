"""Create editable Maya curves along polygon surface intersections."""

from collections import defaultdict
from itertools import product
import math
import uuid

from maya import cmds
from maya.api import OpenMaya as om

from model_pack_draft.core import path_for, ui_distance


def _selected_surfaces():
    objects = set()
    for item in cmds.ls(selection=True, long=True) or []:
        node = item.split('.', 1)[0]
        if cmds.nodeType(node) == 'mesh':
            node = node.rsplit('|', 1)[0]
        objects.add(node)

    if len(objects) != 2:
        raise ValueError('Выделите два полигональных объекта.')
    surfaces = [cmds.listRelatives(node, shapes=True, noIntermediate=True,
                                 type='mesh', fullPath=True) or []
                for node in sorted(objects)]
    if not all(surfaces):
        raise ValueError('Выделите два полигональных объекта.')
    return surfaces


def _bounds(items):
    return (tuple(min(item.low[axis] for item in items) for axis in range(3)),
            tuple(max(item.high[axis] for item in items) for axis in range(3)))


class _Triangle:
    def __init__(self, a, b, c):
        self.points = a, b, c
        self.normal = ((b - a) ^ (c - a)).normal()
        self.low = tuple(min(a[axis], b[axis], c[axis]) for axis in range(3))
        self.high = tuple(max(a[axis], b[axis], c[axis]) for axis in range(3))


def _triangles(shapes):
    triangles = []
    for shape in shapes:
        mesh = om.MFnMesh(path_for(shape))
        if not mesh.numPolygons:
            continue
        points = [om.MVector(point) for point in mesh.getPoints(om.MSpace.kWorld)]
        _, indices = mesh.getTriangles()
        for index in range(0, len(indices), 3):
            triangle = _Triangle(*(points[vertex] for vertex in indices[index:index + 3]))
            if triangle.normal.length() > 0.0:
                triangles.append(triangle)
    return triangles


class _BoundsTree:
    def __init__(self, triangles):
        self.low, self.high = _bounds(triangles)
        self.triangles = triangles
        self.children = ()
        self.size = len(triangles)
        if self.size > 8:
            axis = max(range(3), key=lambda dim: self.high[dim] - self.low[dim])
            triangles.sort(key=lambda tri: tri.low[axis] + tri.high[axis])
            middle = self.size // 2
            self.triangles = ()
            self.children = (_BoundsTree(triangles[:middle]), _BoundsTree(triangles[middle:]))


def _overlap(first, second, tolerance):
    return all(first.low[axis] <= second.high[axis] + tolerance and
               second.low[axis] <= first.high[axis] + tolerance for axis in range(3))


def _candidate_pairs(first, second, tolerance):
    stack = [(first, second)]
    while stack:
        a, b = stack.pop()
        if not _overlap(a, b, tolerance):
            continue
        if a.children and (not b.children or a.size >= b.size):
            stack.extend((child, b) for child in a.children)
        elif b.children:
            stack.extend((a, child) for child in b.children)
        else:
            yield from ((t1, t2) for t1 in a.triangles for t2 in b.triangles
                        if _overlap(t1, t2, tolerance))


def _plane_slice(triangle, plane, tolerance):
    distances = [(point - plane.points[0]) * plane.normal for point in triangle.points]
    distances = [0.0 if abs(distance) <= tolerance else distance for distance in distances]
    if min(distances) > 0.0 or max(distances) < 0.0:
        return []
    cut = [point for point, distance in zip(triangle.points, distances) if distance == 0.0]
    for index, a in enumerate(triangle.points):
        following = (index + 1) % 3
        da, db = distances[index], distances[following]
        if da * db < 0.0:
            b = triangle.points[following]
            cut.append(a + (b - a) * (da / (da - db)))
    return cut


def _intersection(first, second, tolerance):
    axis = first.normal ^ second.normal
    if axis.length() < 1e-10:
        # A shared plane may overlap in an area and does not determine one line.
        return None
    axis.normalize()
    a = _plane_slice(first, second, tolerance)
    b = _plane_slice(second, first, tolerance)
    if len(a) < 2 or len(b) < 2:
        return None
    origin = a[0]
    first_limits = [(point - origin) * axis for point in a]
    second_limits = [(point - origin) * axis for point in b]
    start = max(min(first_limits), min(second_limits))
    end = min(max(first_limits), max(second_limits))
    if end - start <= tolerance:
        return None
    return origin + axis * start, origin + axis * end


def _segment_graph(segments, tolerance):
    points = []
    cells = defaultdict(list)
    graph = defaultdict(set)
    adjacent_cells = tuple(product((-1, 0, 1), repeat=3))
    squared_tolerance = tolerance ** 2

    def weld(point):
        x, y, z = (math.floor(value / tolerance) for value in point)
        for dx, dy, dz in adjacent_cells:
            for index in cells.get((x + dx, y + dy, z + dz), ()):
                delta = points[index] - point
                if delta * delta <= squared_tolerance:
                    return index
        index = len(points)
        points.append(point)
        cells[x, y, z].append(index)
        return index

    for start, end in segments:
        a, b = weld(start), weld(end)
        if a != b:
            graph[a].add(b)
            graph[b].add(a)
    return points, graph


def _chains(graph):
    visited = set()

    def walk(start, following):
        path = [start]
        previous = start
        while (previous, following) not in visited:
            visited.add((previous, following))
            visited.add((following, previous))
            path.append(following)
            neighbors = graph[following]
            if following == start or len(neighbors) != 2:
                break
            previous, following = following, next(iter(neighbors - {previous}))
        return path

    for start in sorted(graph, key=lambda index: (len(graph[index]) == 2, index)):
        for following in sorted(graph[start]):
            if (start, following) not in visited:
                yield walk(start, following)


def _simplify(points, closed, tolerance):
    """Drop interior points from straight spans created by triangulation."""
    keep = []
    for index, point in enumerate(points):
        if not closed and index in (0, len(points) - 1):
            keep.append(point)
            continue
        before, after = points[index - 1], points[(index + 1) % len(points)]
        chord = after - before
        along = point - before
        if (along * (after - point) <= 0.0 or
                (chord ^ along).length() > tolerance * chord.length()):
            keep.append(point)
    minimum = 3 if closed else 2
    return keep if len(keep) >= minimum else points


def _curve_paths(first, second):
    a, b = _BoundsTree(first), _BoundsTree(second)
    extent = min(max(root.high[axis] - root.low[axis] for axis in range(3))
                 for root in (a, b))
    coordinate_scale = max(abs(value) for root in (a, b) for value in (*root.low, *root.high))
    tolerance = max(extent * 1e-8, math.ulp(coordinate_scale) * 16, 1e-10)
    segments = (_intersection(triangle, other, tolerance)
                for triangle, other in _candidate_pairs(a, b, tolerance))
    points, graph = _segment_graph((segment for segment in segments if segment), tolerance * 4)
    for chain in _chains(graph):
        closed = chain[0] == chain[-1]
        indices = chain[:-1] if closed else chain
        path = _simplify([points[index] for index in indices], closed, tolerance * 4)
        yield path, closed


def create_intersection_curves():
    """Create and select the intersection curves of two selected polygon objects."""
    first, second = [_triangles(shapes) for shapes in _selected_surfaces()]
    if not first or not second:
        raise ValueError('На обоих объектах должны быть полигоны ненулевой площади.')
    paths = list(_curve_paths(first, second))
    if not paths:
        raise ValueError('Линия пересечения не найдена. Касание в одной точке или совпадение плоскостей не образует кривую.')
    if not cmds.undoInfo(query=True, state=True):
        raise ValueError('Включите Undo в Maya для создания кривой пересечения.')

    token = 'Intersection Curve ' + uuid.uuid4().hex[:8]
    cmds.undoInfo(openChunk=True, chunkName=token)
    curves = []
    try:
        for points, closed in paths:
            values = [tuple(ui_distance(value) for value in point) for point in points]
            if closed:
                values.append(values[0])
                curve = cmds.curve(name='intersectionCurve#', degree=1, point=values,
                                   periodic=True, knot=list(range(len(values))))
            else:
                curve = cmds.curve(name='intersectionCurve#', degree=1, point=values)
            curves.append(curve)
        cmds.selectMode(object=True)
        cmds.select(curves, replace=True)
    except Exception:
        cmds.undoInfo(closeChunk=True)
        if cmds.undoInfo(query=True, undoName=True) == token:
            cmds.undo()
        raise
    else:
        cmds.undoInfo(closeChunk=True)
    return curves
