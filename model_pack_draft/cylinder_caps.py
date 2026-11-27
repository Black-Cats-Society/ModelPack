"""Cylinder cap topology and smoothing, evaluated by a persistent history node."""
from maya import cmds
from maya.api import OpenMaya as om

CAP_NAMES = ('Cap', 'Center', 'Parallel')
NODE_TYPE = 'modelPackCylinderCaps'


def cap_faces(ring, mode, center=None):
    """Preserve ring winding. Parallel strips introduce no extra vertices."""
    if mode == 1:
        return [[ring[i], ring[(i + 1) % len(ring)], center] for i in range(len(ring))]
    if mode != 2 or len(ring) <= 4:return [ring]
    faces = [[ring[-1], ring[0], ring[1]]]
    low, high = 1, len(ring) - 1
    while high - low >= 3:
        faces.append([ring[low], ring[low + 1], ring[high - 1], ring[high]])
        low += 1;high -= 1
    if high - low == 2:faces.append([ring[low], ring[low + 1], ring[high]])
    return faces


def evaluate(source, mode):
    """Rebuild only the upper cap; retain the native cylinder's UV layout."""
    original = om.MFnMesh(source)
    # API 2.0 array items borrow their array's memory; copy each point before
    # the source MPointArray goes out of scope, including on topology changes.
    source_points = original.getPoints()
    points = [om.MPoint(point) for point in source_points]
    maximum = max(p.y for p in points)
    span = max(p.y for p in points) - min(p.y for p in points)
    tolerance = max(span * 1e-6, 1e-7)
    counts, vertices = original.getVertices()
    uv_counts, uv_ids = original.getAssignedUVs()
    u, v = map(list, original.getUVs())
    have_uvs = bool(u) and all(a == b for a, b in zip(counts, uv_counts))
    faces, assignments, caps = [], [], []
    pointer = uv_pointer = 0
    for face_id, count in enumerate(counts):
        polygon = list(vertices[pointer:pointer + count]);pointer += count
        uv_count = uv_counts[face_id] if face_id < len(uv_counts) else 0
        face_uv = list(uv_ids[uv_pointer:uv_pointer + uv_count]);uv_pointer += uv_count
        top = all(abs(points[index].y - maximum) < tolerance for index in polygon)
        is_cap = top or all(abs(points[index].y - points[polygon[0]].y) < tolerance for index in polygon)
        by_vertex = dict(zip(polygon, face_uv)) if have_uvs else {}
        center = None
        if top and mode == 1:
            center = len(points)
            points.append(om.MPoint(sum(points[i].x for i in polygon) / count,
                                   maximum, sum(points[i].z for i in polygon) / count))
            if have_uvs:
                by_vertex[center] = len(u)
                u.append(sum(u[i] for i in face_uv) / count)
                v.append(sum(v[i] for i in face_uv) / count)
        replacements = cap_faces(polygon, mode, center) if top else [polygon]
        for replacement in replacements:
            faces.append(replacement);caps.append(is_cap)
            if have_uvs:assignments.extend(by_vertex[i] for i in replacement)
    output = om.MFnMeshData().create()
    fn = om.MFnMesh()
    fn.create(points, [len(face) for face in faces], [i for face in faces for i in face], parent=output)
    if have_uvs:
        fn.setUVs(u, v);fn.assignUVs([len(face) for face in faces], assignments)
    adjacent = {}
    for polygon, cap in zip(faces, caps):
        for a, b in zip(polygon, polygon[1:] + polygon[:1]):
            adjacent.setdefault(tuple(sorted((a, b))), []).append(cap)
    for edge in range(fn.numEdges):
        kinds = adjacent[tuple(sorted(fn.getEdgeVertices(edge)))]
        fn.setEdgeSmoothing(edge, len(set(kinds)) == 1)
    fn.cleanupEdgeSmoothing();fn.updateSurface()
    return output


def attach(transform, primitive):
    from model_pack_modifiers import ensure_plugin, PLUGIN
    ensure_plugin(PLUGIN)
    shape = cmds.listRelatives(transform, shapes=True, noIntermediate=True, fullPath=True)[0]
    node = cmds.createNode(NODE_TYPE, name='fdCylinderCaps#')
    cmds.setAttr(primitive + '.subdivisionsCaps', lock=True)
    cmds.setAttr(primitive + '.roundCap', lock=True)
    cmds.connectAttr(primitive + '.output', node + '.inputMesh')
    cmds.connectAttr(node + '.outputMesh', shape + '.inMesh', force=True)
    for name, plug, label in (('radius', primitive + '.radius', 'Radius'),
                              ('height', primitive + '.height', 'Height'),
                              ('sides', primitive + '.subdivisionsAxis', 'Sides'),
                              ('capType', node + '.capType', 'Top Cap')):
        cmds.addAttr(transform, longName=name, niceName=label, proxy=plug)
        cmds.setAttr(transform + '.' + name, keyable=True)
    return node


def side_edges(transform, footprint, depth=None):
    """Only cylinder-wall edges; never soften the target mesh's unrelated edges."""
    from .core import path_for
    shape = cmds.listRelatives(transform, shapes=True, noIntermediate=True, fullPath=True)[0]
    path = path_for(shape);fn = om.MFnMesh(path)
    frame = footprint.frame
    radius = (footprint.x ** 2 + footprint.z ** 2) ** .5
    tolerance = max(radius * 1e-5, 1e-6)
    local = []
    world_points = fn.getPoints(om.MSpace.kWorld)
    for point in world_points:
        delta = point - frame.origin
        local.append((delta * frame.u, delta * frame.normal, delta * frame.v))

    def on_wall(vertex):
        x, y, z = local[vertex]
        return (abs((x*x + z*z) ** .5 - radius) < tolerance and
                (depth is None or depth - tolerance <= y))

    walls = set()
    for face in range(fn.numPolygons):
        indices = fn.getPolygonVertices(face)
        heights = [local[index][1] for index in indices]
        if max(heights) - min(heights) > tolerance and all(on_wall(index) for index in indices):
            walls.add(face)
    iterator = om.MItMeshEdge(path);edges = []
    while not iterator.isDone():
        adjacent = iterator.getConnectedFaces()
        if len(adjacent) == 2 and all(face in walls for face in adjacent):
            edges.append('{}.e[{}]'.format(shape, iterator.index()))
        iterator.next()
    return edges


def soften_walls(transform, footprint, depth=None, history=False):
    edges = side_edges(transform, footprint, depth)
    if edges:cmds.polySoftEdge(edges, angle=180, constructionHistory=history)
    return edges
