"""Planarize selected polygon faces with optional position and angle constraints."""
from collections import defaultdict
import math
import uuid

from maya import cmds
from maya.api import OpenMaya as om
import numpy as np

from slide_for_maya.core import _mesh_position_tolerance
from . import core


def _selection(*, reference=False):
    selection = om.MGlobal.getActiveSelectionList()
    message = ("Выделите опорные грани или вершины." if reference
               else "Выделите только полигоны, которые нужно выровнять.")
    targets = {}
    for index in range(selection.length()):
        try:
            path, component = selection.getComponent(index)
        except (RuntimeError, TypeError):
            raise ValueError(message) from None
        if component.hasFn(om.MFn.kMeshPolygonComponent):
            kind = "face"
        elif reference and component.hasFn(om.MFn.kMeshVertComponent):
            kind = "vertex"
        else:
            raise ValueError(message)
        if path.node().hasFn(om.MFn.kTransform):
            path.extendToShape()
        if path.isInstanced():
            raise ValueError("Make Planar нужен уникальный меш. У экземпляров общие вершины.")
        fn = om.MFnMesh(path)
        maximum = fn.numPolygons if kind == "face" else fn.numVertices
        elements = om.MFnSingleIndexedComponent(component)
        ids = range(maximum) if elements.isComplete else elements.getElements()
        if any(item < 0 or item >= maximum for item in ids):
            raise ValueError("Выделение устарело. Выделите компоненты заново.")
        if len(ids):
            targets.setdefault(path.fullPathName(), {}).setdefault(kind, set()).update(ids)
    if not targets:
        raise ValueError(message)
    return targets


def _points(fn, components):
    vertices = set(components.get("vertex", ()))
    for face in components.get("face", ()):
        vertices.update(fn.getPolygonVertices(face))
    return {vertex: fn.getPoint(vertex, om.MSpace.kWorld) for vertex in sorted(vertices)}


class Reference:
    def __init__(self):
        self.items = []
        self.count = 0
        for mesh, components in _selection(reference=True).items():
            fn = core._mesh_fn(mesh)
            self.items.append((om.MObjectHandle(fn.object()), core.identity(fn), components))
            self.count += sum(len(ids) for ids in components.values())

    def resolve(self, *, perpendicular=False):
        points = {}
        normals = []
        precision = 0.0
        for handle, signature, components in self.items:
            if not handle.isValid() or not handle.isAlive():
                raise ValueError("Опора удалена. Запомните её заново.")
            paths = om.MDagPath.getAllPathsTo(handle.object())
            if len(paths) != 1:
                raise ValueError("У опоры появились экземпляры. Выберите уникальный меш.")
            fn = om.MFnMesh(paths[0])
            if core.identity(fn) != signature:
                raise ValueError("Топология опоры изменилась. Запомните её заново.")
            positions = _points(fn, components)
            if not all(math.isfinite(value) for p in positions.values() for value in (p.x,p.y,p.z)):
                raise ValueError("У опорных вершин недопустимые координаты.")
            points[paths[0].fullPathName()] = positions
            precision = max(precision, _mesh_position_tolerance(fn, positions.values()))
            if perpendicular:
                for face in sorted(components.get("face", ())):
                    normal = fn.getPolygonNormal(face, om.MSpace.kWorld).normal()
                    if normal.length() < .5:
                        raise ValueError("У опорной грани нет корректной нормали.")
                    normals.append(tuple(normal))
        if perpendicular and not normals:
            plane = core.fit_plane([p for mesh in points.values() for p in mesh.values()],
                                   allow_secondary=False)
            if plane is None:
                raise ValueError("Выберите опорную грань или три вершины, не лежащие на одной прямой.")
            center, normal = plane
            if any(abs((om.MVector(p) - center) * normal) > precision
                   for mesh in points.values() for p in mesh.values()):
                raise ValueError("Опорные вершины должны лежать в одной плоскости.")
            normals.append(tuple(normal))
        return points, normals, precision


def _coordinates(points):
    return np.asarray([(point.x, point.y, point.z) for point in points], dtype=float).reshape(-1, 3)


def _axes(rows):
    # Ask for the complete right basis without allocating an NxN matrix.
    return np.linalg.svd(rows, full_matrices=len(rows) < 3)[1:]


def _constrained_plane(points, anchors, normals, precision):
    origin = anchors.mean(axis=0) if len(anchors) else points.mean(axis=0)
    offsets = anchors - origin
    constraints = list(normals)
    if len(anchors) >= 2:
        spans, axes = _axes(offsets)
        if len(spans) == 3 and max(abs(offsets @ axes[2])) > precision:
            raise ValueError("Закреплённые точки не лежат в одной плоскости.")
        constraints.extend(axis for axis in axes[:2] if max(abs(offsets @ axis)) > precision)
    basis = np.eye(3)
    if constraints:
        singular, axes = _axes(np.asarray(constraints))
        rank = sum(value > singular[0] * 1e-6 for value in singular)
        basis = axes[rank:].T
        if not basis.size:
            raise ValueError("Невозможно построить плоскость с этими опорами и углами.")
    coordinates = (points - origin) @ basis
    _, vectors = np.linalg.eigh(coordinates.T @ coordinates)
    normal = basis @ vectors[:, 0]
    if len(anchors) and max(abs(offsets @ normal)) > precision:
        raise ValueError("Невозможно оставить эти вершины на месте при выбранном угле.")
    return om.MVector(*origin), om.MVector(*normal)


def make_planar(reference=None, *, perpendicular=False):
    originals = {}
    for mesh, components in _selection().items():
        fn = core._mesh_fn(mesh)
        positions = _points(fn, components)
        originals[mesh] = (positions, _mesh_position_tolerance(fn, positions.values()))
    points = [point for positions, _ in originals.values() for point in positions.values()]
    if not all(math.isfinite(value) for p in points for value in (p.x, p.y, p.z)):
        raise ValueError("В выделении есть вершины с недопустимыми координатами.")
    pins = {}
    if reference is None:
        plane = core.fit_plane(points, allow_secondary=False)
        if plane is None:
            raise ValueError("Для определения плоскости нужны три точки, не лежащие на одной прямой.")
        center, normal = plane
    else:
        pins, normals, precision = reference.resolve(perpendicular=perpendicular)
        anchors = (point for mesh, positions in pins.items() for vertex, point in positions.items()
                   if not perpendicular or vertex in originals.get(mesh, ({}, 0.0))[0])
        center, normal = _constrained_plane(_coordinates(points), _coordinates(anchors),
                                            normals, precision)

    scale = om.MDistance(1., om.MDistance.kCentimeters).asUnits(om.MDistance.uiUnit())
    moves = []
    for mesh, (positions, tolerance) in originals.items():
        for vertex, point in positions.items():
            if vertex in pins.get(mesh, ()):
                continue
            distance = (om.MVector(point) - center) * normal
            if abs(distance) <= tolerance:
                continue
            projected = point - normal * distance
            moves.append((f"{mesh}.vtx[{vertex}]", (point.x * scale, point.y * scale, point.z * scale),
                          (projected.x * scale, projected.y * scale, projected.z * scale)))
    if not moves:
        return 0
    undo_enabled = cmds.undoInfo(query=True, state=True)
    if undo_enabled:
        cmds.undoInfo(openChunk=True, chunkName="Make Planar")
    applied = 0
    try:
        for component, _, target in moves:
            cmds.xform(component, worldSpace=True, translation=target)
            applied += 1
    except Exception:
        if undo_enabled:
            cmds.undoInfo(closeChunk=True)
            if applied:
                cmds.undo()
        else:
            for component, original, _ in reversed(moves[:applied]):
                cmds.xform(component, worldSpace=True, translation=original)
        raise
    else:
        if undo_enabled:
            cmds.undoInfo(closeChunk=True)
    return applied


def _face_regions(fn, faces):
    borders = defaultdict(list)
    neighbors = defaultdict(set)
    edges = {}
    polygon = om.MItMeshPolygon(fn.dagPath())
    for face in sorted(faces):
        polygon.setIndex(face)
        edges[face] = polygon.getEdges()
        for edge in edges[face]:
            borders[edge].append(face)
    for adjacent in borders.values():
        for face in adjacent:
            neighbors[face].update(adjacent)
    remaining = set(faces)
    for face in sorted(faces):
        if face not in remaining:
            continue
        remaining.remove(face)
        region = [face]
        for current in region:
            linked = neighbors[current] & remaining
            remaining.difference_update(linked)
            region.extend(sorted(linked))
        boundary = {v for f in region for e in edges[f] if len(borders[e]) != 2 for v in fn.getEdgeVertices(e)}
        yield region, boundary


def _region_normal(fn, region, boundary, positions, rings):
    facing = om.MVector()
    directions = []
    for face in region:
        ring = rings[face]
        origin = positions[ring[0]]
        area = sum(((positions[a] - origin) ^ (positions[b] - origin)
                    for a, b in zip(ring[1:-1], ring[2:])), om.MVector())
        direction = fn.getPolygonNormal(face, om.MSpace.kWorld)
        directions.append(direction)
        facing += direction * area.length()
    if not boundary or not facing.length() or not all(math.isfinite(v) for v in facing):
        raise ValueError('Выделите одну поверхность с границей и корректными нормалями.')
    anchors = [positions[v] for v in sorted(boundary)]
    plane = core.fit_plane(anchors, allow_secondary=False)
    if plane is not None:
        center, normal = plane
        precision = _mesh_position_tolerance(fn, positions.values())
        if all(abs((om.MVector(p) - center) * normal) <= precision for p in anchors):
            if normal * facing < 0.:
                normal = -normal
            # A face perpendicular to the target plane would collapse on projection.
            movable = all(direction * normal > .01 for direction in directions)
            return normal, (center, precision) if movable else None
    return facing.normal(), None


def _contour_edits(mesh, faces):
    fn = core._mesh_fn(mesh)
    rings = {face: list(fn.getPolygonVertices(face)) for face in faces}
    positions = _points(fn, {'face': faces})
    if not all(math.isfinite(value) for p in positions.values() for value in (p.x, p.y, p.z)):
        raise ValueError('В выделении есть вершины с недопустимыми координатами.')
    moves = {}
    corners = {}
    fans = defaultdict(set)
    vertex = om.MItMeshVertex(fn.dagPath())
    connected = {}
    inverse = fn.dagPath().inclusiveMatrixInverse()
    for region, boundary in _face_regions(fn, faces):
        inside = {v for f in region for v in rings[f]}
        members = set(region)
        for v in sorted(inside):
            if v not in connected:
                vertex.setIndex(v)
                connected[v] = set(vertex.getConnectedFaces())
            if connected[v] - members:
                boundary.add(v)
        normal, geometry_plane = _region_normal(fn, region, boundary, positions, rings)
        if geometry_plane is not None:
            center, precision = geometry_plane
            for v in inside - boundary:
                point = positions[v]
                distance = (om.MVector(point) - center) * normal
                if abs(distance) > precision:
                    moves[v] = point - normal * distance
        local = tuple(normal.transformAsNormal(inverse).normal())
        for face in region:
            for v, index in zip(rings[face], fn.getFaceNormalIds(face)):
                corners[v, face] = local, index
                fans[v, index].add(local)

    # Carry the selected normal to each adjacent corner in the same Maya
    # smoothing fan. An existing hard edge has a different normal ID.
    outside = {}
    for (v, index), targets in fans.items():
        if len(targets) != 1:
            continue
        direction, = targets
        for face in connected[v] - faces:
            if face not in outside:
                outside[face] = dict(zip(fn.getPolygonVertices(face), fn.getFaceNormalIds(face)))
            if index == outside[face][v]:
                corners[v, face] = direction, index

    normals = fn.getNormals()
    edits = defaultdict(list)
    smoothing = {}
    for (v, face), (direction, index) in sorted(corners.items()):
        if len(fans[v, index]) != 1:
            continue
        if moves or (om.MVector(normals[index]) - om.MVector(*direction)).length() > 1e-6:
            edits[direction].append(f'{mesh}.vtxFace[{v}][{face}]')
            vertex.setIndex(v)
            smoothing.update((e, fn.isEdgeSmooth(e)) for e in vertex.getConnectedEdges())
    return moves, edits, smoothing


def preserve_contour():
    """Keep boundary vertices and give the selected patches planar shading."""
    selection = _selection()
    plans = [(mesh, *_contour_edits(mesh, parts['face'])) for mesh, parts in selection.items()]
    if not any(moves or normals for _, moves, normals, _ in plans):
        return 0, 0
    if not cmds.undoInfo(query=True, state=True):
        raise ValueError('Включите Undo в Maya перед выравниванием.')
    scale = om.MDistance(1., om.MDistance.kCentimeters).asUnits(om.MDistance.uiUnit())
    selected = cmds.ls(selection=True, long=True) or []
    chunk = 'Make Planar · Сохранить контур ' + uuid.uuid4().hex
    cmds.undoInfo(openChunk=True, chunkName=chunk)
    try:
        for mesh, moves, normals, smoothing in plans:
            for direction, corners in normals.items():
                cmds.polyNormalPerVertex(corners, xyz=direction)
            if smoothing:
                # The native face-vertex edit can split a smooth edge when
                # it locks normals. Restore the original edge flags while
                # keeping the explicitly assigned directions.
                fn = core._mesh_fn(mesh)
                restore = defaultdict(list)
                for edge, smooth in smoothing.items():
                    if fn.isEdgeSmooth(edge) != smooth:
                        restore[smooth].append(f'{mesh}.e[{edge}]')
                for smooth, components in restore.items():
                    cmds.polySoftEdge(components, angle=180 if smooth else 0, constructionHistory=True)
            for v, point in sorted(moves.items()):
                location = (point.x * scale, point.y * scale, point.z * scale)
                cmds.xform(f'{mesh}.vtx[{v}]', worldSpace=True, translation=location)
        if selected != (cmds.ls(selection=True, long=True) or []):
            cmds.select(selected, replace=True)
    except Exception:
        cmds.undoInfo(closeChunk=True)
        if cmds.undoInfo(query=True, undoName=True) == chunk:
            cmds.undo()
        raise
    else:
        cmds.undoInfo(closeChunk=True)
    return (sum(len(moves) for _, moves, _, _ in plans),
            sum(len(selection[mesh]['face']) for mesh, _, normals, _ in plans if normals))
