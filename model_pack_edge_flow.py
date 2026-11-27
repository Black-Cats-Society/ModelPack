"""Edge Flow geometry and undoable edits."""
import bisect
import math

from maya import cmds
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn, _mesh_position_tolerance, _ordered_chain


def selected_edges():
    selection=om.MGlobal.getActiveSelectionList();targets={}
    for index in range(selection.length()):
        try:path,component=selection.getComponent(index)
        except RuntimeError:raise ValueError('Выделите рёбра полигонального меша для Edge Flow.')
        if component.isNull() or not component.hasFn(om.MFn.kMeshEdgeComponent):
            raise ValueError('Edge Flow работает с выделенными рёбрами полигонального меша.')
        if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
        if not path.node().hasFn(om.MFn.kMesh):raise ValueError('Выделите рёбра полигонального меша.')
        fn=om.MFnMesh(path);name=path.fullPathName()
        if path.isInstanced() or cmds.referenceQuery(name,isNodeReferenced=True):
            raise ValueError('Для Edge Flow нужен локальный меш без экземпляров (instances).')
        ids=om.MFnSingleIndexedComponent(component)
        edges=set(range(fn.numEdges) if ids.isComplete else ids.getElements())
        if any(e<0 or e>=fn.numEdges for e in edges):raise ValueError('Выделение рёбер устарело.')
        targets.setdefault(name,set()).update(edges)
    if not targets or not any(targets.values()):raise ValueError('Выделите рёбра для Edge Flow.')
    return targets


def topology(fn):
    return fn.numVertices,tuple(tuple(a) for a in fn.getVertices())


def _equal_length_components(points, pairs):
    """Store each component as a fixed center and offsets for unit edge length."""
    adjacency = {}
    for a, b in pairs:
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)

    components = []
    remaining = set(adjacency)
    while remaining:
        seed = min(remaining)
        offsets = {seed: om.MVector()}
        pending = [seed]
        for vertex in pending:
            for neighbor in adjacency[vertex]:
                if neighbor not in offsets:
                    direction = (points[neighbor] - points[vertex]).normal()
                    offsets[neighbor] = offsets[vertex] + direction
                    pending.append(neighbor)
        remaining.difference_update(offsets)
        center = sum((om.MVector(points[v]) for v in offsets), om.MVector()) / len(offsets)
        if sum(len(adjacency[v]) for v in offsets) > 2 * (len(offsets) - 1):
            # A cycle must close after equalization. Start from its original
            # shape and satisfy the length constraints with symmetric updates.
            loop_edges = [(a, b) for a in offsets for b in adjacency[a] if a < b]
            scale = sum((points[b] - points[a]).length() for a, b in loop_edges) / len(loop_edges)
            offsets = {v: (om.MVector(points[v]) - center) / scale for v in offsets}
            for iteration in range(768):
                error = 0.0
                order = loop_edges if iteration % 2 == 0 else reversed(loop_edges)
                for a, b in order:
                    vector = offsets[b] - offsets[a]
                    length = vector.length()
                    error = max(error, abs(length - 1.0))
                    direction = vector / length if length > 1e-12 else (points[b] - points[a]).normal()
                    adjustment = direction * ((length - 1.0) * 0.5)
                    offsets[a] += adjustment
                    offsets[b] -= adjustment
                if error < 1e-8:
                    break
            else:
                raise ValueError('Не удалось выровнять длины в замкнутом выделении. Выберите более простой участок.')
        mean = sum(offsets.values(), om.MVector()) / len(offsets)
        components.append((om.MPoint(center), {v: value - mean for v, value in offsets.items()}))
    return components


class _LengthMesh:
    def __init__(self, mesh, edges):
        fn = _mesh_fn(mesh)
        self.handle = om.MObjectHandle(fn.object())
        self.topology = topology(fn)
        self.matrix = fn.dagPath().inclusiveMatrix()
        points = fn.getPoints(om.MSpace.kWorld)
        pairs = [fn.getEdgeVertices(edge) for edge in sorted(edges)]
        self.lengths = [(points[b] - points[a]).length() for a, b in pairs]
        if any(not math.isfinite(length) or length <= 1e-12 for length in self.lengths):
            raise ValueError('В выделении есть ребро нулевой длины.')
        self.components = _equal_length_components(points, pairs)
        self.original = {v: om.MPoint(points[v]) for _, offsets in self.components for v in offsets}
        self.faces = []
        faces = set()
        vertex = om.MItMeshVertex(fn.dagPath())
        for index in self.original:
            vertex.setIndex(index)
            faces.update(vertex.getConnectedFaces())
        polygon = om.MItMeshPolygon(fn.dagPath())
        for face in faces:
            polygon.setIndex(face)
            _, triangle_ids = polygon.getTriangles(om.MSpace.kObject)
            for index in range(0, len(triangle_ids), 3):
                ids = tuple(triangle_ids[index:index + 3])
                if not any(v in self.original for v in ids):
                    continue
                a, b, c = (om.MPoint(points[v]) for v in ids)
                normal = (b - a) ^ (c - a)
                if normal.length() > 1e-20:
                    self.faces.append((ids, (a, b, c), normal))

    def resolve(self):
        if self.handle.isValid() and self.handle.isAlive():
            paths = om.MDagPath.getAllPathsTo(self.handle.object())
            if len(paths) == 1:
                fn = om.MFnMesh(paths[0])
                if self.matrix.isEquivalent(paths[0].inclusiveMatrix(), 1e-10) and self.topology == topology(fn):
                    return paths[0].fullPathName(), fn
        raise ValueError('Меш изменился. Запустите Equal Length на текущем выделении заново.')

    def positions(self, length):
        result = {v: center + offset * length for center, offsets in self.components
                  for v, offset in offsets.items()}
        for ids, original, normal in self.faces:
            a, b, c = (result.get(v, point) for v, point in zip(ids, original))
            area = ((b - a) ^ (c - a)) * normal
            if area <= (normal * normal) * 1e-9:
                raise ValueError('Эта длина приведёт к схлопыванию или перевороту соседних полигонов.')
        return result


class EqualLength:
    """Recompute equal-length edges from one captured selection and shape."""

    def __init__(self, targets):
        self.items = [_LengthMesh(mesh, edges) for mesh, edges in targets.items() if edges]
        lengths = [length for item in self.items for length in item.lengths]
        if not lengths:
            raise ValueError('Выделите рёбра для Equal Length.')
        self.default_length = sum(lengths) / len(lengths)

    @classmethod
    def capture(cls):
        return cls(selected_edges())

    def _jobs(self, length):
        if length is not None and (not math.isfinite(length) or length <= 1e-12):
            raise ValueError('Длина ребра должна быть положительной.')
        return [(item.resolve(), item.original if length is None else item.positions(length))
                for item in self.items]

    def positions(self, length):
        return {mesh: positions for (mesh, _), positions in self._jobs(length)}

    def _write(self, length):
        jobs = self._jobs(length)
        unit = om.MDistance.uiUnit()
        moves = []
        for (mesh, fn), positions in jobs:
            points = fn.getPoints(om.MSpace.kWorld)
            for vertex, point in positions.items():
                original = om.MPoint(points[vertex])
                if (original - point).length() > 1e-12:
                    moves.append((mesh, vertex, original, point))

        def move(mesh, vertex, point):
            translation = [om.MDistance(value).asUnits(unit) for value in (point.x, point.y, point.z)]
            cmds.xform(f'{mesh}.vtx[{vertex}]', worldSpace=True, translation=translation)

        moved = 0
        try:
            for mesh, vertex, _, point in moves:
                move(mesh, vertex, point)
                moved += 1
        except Exception:
            for mesh, vertex, original, _ in reversed(moves[:moved]):
                move(mesh, vertex, original)
            raise
        return moved

    def apply(self, length):
        """Apply in Maya's current undo chunk. Distances are world centimeters."""
        return self._write(length)

    def restore(self):
        return self._write(None)


class Reference:
    """Read the current world direction; survive mesh rename/reparent safely."""
    def __init__(self,targets):
        self.items=[]
        for mesh,edges in targets.items():
            fn=_mesh_fn(mesh)
            self.items.append((om.MObjectHandle(fn.object()),tuple(sorted(edges)),topology(fn)))
        self.resolve()  # Reject ambiguous reference groups before replacing a good one.

    @classmethod
    def capture(cls):return cls(selected_edges())

    @property
    def count(self):return sum(len(edges) for _,edges,_ in self.items)

    def resolve(self):
        vectors=[];protected={}
        for handle,edges,signature in self.items:
            if not handle.isValid() or not handle.isAlive():
                raise ValueError('Опорный меш удалён. Выберите рёбра и нажмите Set Reference заново.')
            paths=om.MDagPath.getAllPathsTo(handle.object())
            if len(paths)!=1:raise ValueError('Опорный меш стал instance. Повторите Set Reference на уникальном меше.')
            fn=om.MFnMesh(paths[0])
            if topology(fn)!=signature:
                raise ValueError('Топология опорного меша изменилась. Повторите Set Reference.')
            points=fn.getPoints(om.MSpace.kWorld)
            protected[paths[0].fullPathName()]=set(edges)
            for edge in edges:
                a,b=fn.getEdgeVertices(edge);vector=points[b]-points[a]
                if not math.isfinite(vector.length()) or vector.length()<=1e-12:
                    raise ValueError('У опорного ребра нулевая длина.')
                vectors.append(vector)
        if not vectors:raise ValueError('Выберите опорные рёбра и нажмите Set Reference.')
        seed=max(vectors,key=lambda v:v.length()).normal()
        if any(abs(v.normal()*seed)<1.-1e-6 for v in vectors):
            raise ValueError('Опорные рёбра должны быть параллельны. Выберите одно направление.')
        direction=sum((v if v*seed>=0 else -v for v in vectors),om.MVector()).normal()
        return direction,protected


def last_reference():
    # Maya exposes reliable per-click order only when the user enabled tracking.
    # Explicit Set Reference also works with tracking off, without changing prefs.
    if not cmds.selectPref(query=True,trackSelectionOrder=True):
        raise ValueError('Выберите опорное ребро/рёбра и нажмите Set Reference, затем выделите рёбра и нажмите Parallel.')
    ordered=cmds.ls(orderedSelection=True,flatten=True,long=True) or []
    if len(ordered)<2:raise ValueError('Выделите рёбра для изменения, последним — опорное, или используйте Set Reference.')
    selection=om.MSelectionList();selection.add(ordered[-1])
    try:dag,component=selection.getComponent(0)
    except RuntimeError:raise ValueError('Последним выберите опорное ребро.')
    if component.isNull() or not component.hasFn(om.MFn.kMeshEdgeComponent):
        raise ValueError('Последним выберите опорное ребро.')
    if dag.node().hasFn(om.MFn.kTransform):dag.extendToShape()
    return Reference({dag.fullPathName():set(om.MFnSingleIndexedComponent(component).getElements())})


def _parallel_planes(fn, points, vertices):
    """Keep the planes of planar faces incident to the edited vertices."""
    tolerance = _mesh_position_tolerance(fn, (points[v] for v in vertices))
    iterator = om.MItMeshVertex(fn.dagPath())
    faces = {}
    planes = {}
    for vertex in vertices:
        iterator.setIndex(vertex)
        normals = []
        for face in iterator.getConnectedFaces():
            if face not in faces:
                normal = fn.getPolygonNormal(face, om.MSpace.kWorld).normal()
                ring = fn.getPolygonVertices(face)
                origin = points[ring[0]]
                planar = normal.length() > .5 and all(
                    abs((points[v] - origin) * normal) <= tolerance for v in ring
                )
                faces[face] = normal if planar else None
            normal = faces[face]
            if normal is not None and all(abs(normal * existing) < 1. - 1e-12 for existing in normals):
                normals.append(normal)
        planes[vertex] = normals
    return planes, tolerance


def _parallel_line(points, vertices, lengths, direction, planes, anchors, tolerance):
    """Find the nearest feasible unit direction, then the constrained line origin."""
    import numpy as np

    span = lengths[-1]
    center = sum((om.MVector(points[v]) for v in vertices), om.MVector()) / len(vertices)
    mean = sum(lengths) / len(lengths)
    rows, distances, offsets = [], [], []
    pin_normals = (om.MVector.kXaxisVector, om.MVector.kYaxisVector, om.MVector.kZaxisVector)
    for vertex, distance in zip(vertices, lengths):
        normals = pin_normals if vertex in anchors else planes[vertex]
        for normal in normals:
            rows.append(tuple(normal))
            distances.append((distance - mean) / span)
            offsets.append(((om.MVector(points[vertex]) - center) * normal) / span)
    if not rows:
        return om.MPoint(center - direction * mean), direction

    # A point on the output line is center + shift + distance * axis.
    # Eliminate the translation, then choose the unit axis closest to the
    # reference in the remaining degrees of freedom.
    normals = np.asarray(rows)
    values = np.column_stack((normals * np.asarray(distances)[:, None], offsets))
    translation = np.linalg.lstsq(normals, values, rcond=1e-7)[0]
    constraints = values - normals @ translation
    matrix, height = constraints[:, :3], constraints[:, 3]
    left, singular, right = np.linalg.svd(matrix, full_matrices=len(rows) < 3)
    rank = np.count_nonzero(singular > 1e-7)
    axis = right[:rank].T @ ((left[:, :rank].T @ height) / singular[:rank])
    radius_squared = 1. - axis @ axis
    precision = max(1e-7, tolerance / span)
    basis = right[rank:]
    if radius_squared >= -precision and len(basis):
        preferred = basis @ np.asarray(tuple(direction))
        preferred_length = np.linalg.norm(preferred)
        if preferred_length < 1e-7:
            chord = points[vertices[-1]] - points[vertices[0]]
            preferred = basis @ np.asarray(tuple(chord.normal()))
            preferred_length = np.linalg.norm(preferred)
        if preferred_length >= 1e-7:
            axis += (basis.T @ preferred) * (math.sqrt(max(0., radius_squared)) / preferred_length)
    if (abs(1. - np.linalg.norm(axis)) > 1e-6
            or np.max(abs(matrix @ axis - height)) > precision):
        return None
    direction = om.MVector(*axis)
    shift = om.MVector(*(translation[:, 3] - translation[:, :3] @ axis)) * span
    return om.MPoint(center + shift - direction * mean), direction


def _slide_to_parallel_line(points, vertices, direction, planes, anchors, tolerance):
    """Fit a common line to the vertex planes with free distances along it."""
    import numpy as np

    coordinates = np.asarray([tuple(points[v])[:3] for v in vertices])
    center = coordinates.mean(axis=0)
    axis = np.asarray(tuple(direction))
    normals = [np.asarray([tuple(n) for n in planes[v]]).reshape(-1, 3) for v in vertices]
    # Very narrow faces can have noisy normals. Group nearly coplanar
    # constraints for the solve and check the result against the originals.
    angle_tolerance = .002
    if len(normals[0]) and all(len(rows) for rows in normals):
        scores = np.asarray([np.max(abs(normals[0] @ rows.T), axis=1) for rows in normals])
        heights = (coordinates - center) @ normals[0].T
        common = np.flatnonzero((scores.min(axis=0) > 1. - .5 * angle_tolerance ** 2)
                                & (np.ptp(heights, axis=0) <= 4. * tolerance))
        if common.size:
            candidate = normals[0][common[np.argmax(scores[:, common].mean(axis=0))]]
            aligned = [rows[np.argmax(abs(rows @ candidate))] for rows in normals]
            normal = sum(row if row @ candidate > 0. else -row for row in aligned)
            normal /= np.linalg.norm(normal)
            axis -= normal * (axis @ normal)
            length = np.linalg.norm(axis)
            if length < angle_tolerance:
                return None
            axis /= length

    _, _, basis = np.linalg.svd(axis[None, :], full_matrices=True)
    cross_section = basis[1:]
    projected = (coordinates - center) @ cross_section.T
    constraints, values, adjustments = [], [], []
    for i, (vertex, rows) in enumerate(zip(vertices, normals)):
        if vertex in anchors:
            tangent = np.empty((3, 0))
        else:
            _, singular, basis = np.linalg.svd(rows, full_matrices=len(rows) < 3)
            tangent = basis[np.count_nonzero(singular > angle_tolerance):].T
        left, singular, right = np.linalg.svd(cross_section @ tangent, full_matrices=True)
        rank = np.count_nonzero(singular > angle_tolerance)
        fixed = left[:, rank:].T
        constraints.extend(fixed)
        values.extend(fixed @ projected[i])
        adjustments.append(tangent @ ((right[:rank].T / singular[:rank]) @ left[:, :rank].T))

    offset = (np.linalg.lstsq(constraints, values, rcond=angle_tolerance)[0]
              if constraints else np.zeros(2))
    result = coordinates + [adjustment @ (offset - point)
                            for adjustment, point in zip(adjustments, projected)]
    deviations = (result - center) @ cross_section.T - offset
    if (np.max(np.linalg.norm(deviations, axis=1)) > tolerance
            or np.any(np.diff(result @ axis) <= tolerance)
            or any(np.max(abs(rows @ displacement), initial=0.) > 2. * tolerance
                   for rows, displacement in zip(normals, result - coordinates))):
        return None
    return [om.MPoint(*point) for point in result]


def _limit_parallel_contacts(fn, points, targets, tolerance, path_for_vertex):
    """Stop vertices at the first local face collapse along their slide."""
    from model_pack_bevel.collision import _roots

    iterator = om.MItMeshVertex(fn.dagPath())
    adjacent = set()
    for vertex in targets:
        iterator.setIndex(vertex)
        adjacent.update(iterator.getConnectedFaces())
    polygon = om.MItMeshPolygon(fn.dagPath())
    corners = []
    for face in sorted(adjacent):
        polygon.setIndex(face)
        ring = tuple(polygon.getVertices())
        for a, b, c in zip(ring[-1:] + ring[:-1], ring, ring[1:] + ring[:1]):
            first, second = points[b] - points[a], points[c] - points[a]
            normal = first ^ second
            if normal.length() > tolerance * max(first.length(), second.length(), tolerance):
                corners.append((a, b, c, normal))

    motions = {v: target - points[v] for v, target in targets.items()}
    still_moving = set(motions)
    limited = {v: om.MPoint(points[v]) for v in targets}
    elapsed = 0.
    zero = om.MVector()
    while still_moving and elapsed < 1.:
        contact = 1. - elapsed
        stopped = set()
        for a, b, c, normal in corners:
            moving = still_moving.intersection((a, b, c))
            if not moving:
                continue
            p, q, r = (limited.get(v, points[v]) for v in (a, b, c))
            da, db, dc = (motions[v] if v in moving else zero for v in (a, b, c))
            u, v = q - p, r - p
            du, dv = db - da, dc - da
            square = normal * normal
            area = (((u ^ v) * normal) / square - .001,
                    (((du ^ v) + (u ^ dv)) * normal) / square,
                    ((du ^ dv) * normal) / square)
            roots = _roots(area)
            if roots:
                hit = roots[0]
                speed = max(motions[v].length() for v in moving)
                safe = max(0., hit - tolerance * 4. / max(speed, tolerance))
                if safe <= contact:
                    if safe < contact:
                        stopped.clear()
                    contact = safe
                    stopped.update(moving)
        for v in still_moving:
            limited[v] += motions[v] * contact
        elapsed += contact
        if not stopped:
            break
        # A row shares one slide progress. Holding back only one of its
        # vertices can bend it further than the source.
        stopped = {v for vertex in stopped for v in path_for_vertex[vertex]}
        still_moving.difference_update(stopped)
    return limited


def parallel_positions(fn,edges,direction,reference_edges=()):
    """Straighten open chains along their support planes."""
    points=fn.getPoints(om.MSpace.kWorld);positions={}
    anchors={v for edge in reference_edges for v in fn.getEdgeVertices(edge)}
    paths = chains(fn, edges)
    planes, tolerance = _parallel_planes(fn, points, {v for vertices, _ in paths for v in vertices})
    uses_slide = False
    path_for_vertex = {}
    for vertices,closed in paths:
        if closed:raise ValueError('Замкнутую петлю нельзя сделать параллельной одному направлению. Выберите открытые участки.')
        lengths=[0.]
        for a,b in zip(vertices,vertices[1:]):
            length=(points[b]-points[a]).length()
            if not math.isfinite(length) or length<=1e-12:raise ValueError('В выделении есть ребро нулевой длины.')
            lengths.append(lengths[-1]+length)
        axis=direction if (points[vertices[-1]]-points[vertices[0]])*direction>=0 else -direction
        line = _parallel_line(points, vertices, lengths, axis, planes, anchors, tolerance)
        pinned = sum(v in anchors for v in vertices)
        if line is None and pinned > 1:
            raise ValueError('Участок закреплён опорными рёбрами с двух сторон. Уменьшите выделение или выберите другую опору.')
        targets = None
        if pinned <= 1 and (line is None or abs(line[1] * axis) < 1. - 1e-8):
            slide = _slide_to_parallel_line(points, vertices, axis, planes, anchors, tolerance)
            if slide is not None and (line is None or
                    abs((slide[-1] - slide[0]).normal() * axis) > abs(line[1] * axis) + 1e-8):
                targets = slide
                uses_slide = True
        if targets is None:
            if line is None:
                raise ValueError('Не удалось выровнять рёбра по опоре в плоскостях прилегающих граней.')
            origin, aligned = line
            targets = [origin + aligned * distance for distance in lengths]
        for vertex, point in zip(vertices, targets):
            if vertex in anchors:continue
            if (point-points[vertex]).length()>tolerance:positions[vertex]=point
        path_for_vertex.update((vertex, vertices) for vertex in vertices)
    if uses_slide:
        positions = _limit_parallel_contacts(fn, points, positions, tolerance, path_for_vertex)
    return {vertex: point for vertex, point in positions.items()
            if (point - points[vertex]).length() > tolerance}


def chains(fn, edges):
    """Split disconnected paths, rejecting branches before any mesh is edited."""
    incident={};pairs={e:fn.getEdgeVertices(e) for e in edges}
    for edge,pair in pairs.items():
        for vertex in pair:incident.setdefault(vertex,set()).add(edge)
    if any(len(group)>2 for group in incident.values()):
        raise ValueError('Для Edge Flow выделите цепочки или петли без разветвлений.')
    remaining=set(edges);result=[]
    while remaining:
        pending=[min(remaining)];part=set()
        while pending:
            edge=pending.pop()
            if edge not in remaining:continue
            remaining.remove(edge);part.add(edge)
            for vertex in pairs[edge]:pending.extend(incident[vertex]&remaining)
        _,vertices,closed=_ordered_chain(fn,tuple(sorted(part)))
        result.append((vertices,closed))
    return result


def flow_paths(fn, edges):
    """Use selected paths, or the two longitudinal rails of selected cross edges."""
    selected=chains(fn,edges)
    result=[(vertices,closed) for vertices,closed in selected if len(vertices)>2]
    edge_for_pair={frozenset(fn.getEdgeVertices(e)):e for e in edges}
    singles={frozenset(vertices):edge_for_pair[frozenset(vertices)]
             for vertices,closed in selected if len(vertices)==2}
    if not singles:return result
    neighbors={e:{} for e in singles.values()}
    polygons={};face_edges={};iterator=om.MItMeshEdge(fn.dagPath())
    polygon=om.MItMeshPolygon(fn.dagPath())
    def face(face_id):
        if face_id not in polygons:
            polygon.setIndex(face_id);polygons[face_id]=list(polygon.getVertices())
            face_edges[face_id]=list(polygon.getEdges())
        return polygons[face_id]
    def adjacent(edge):
        iterator.setIndex(edge);return list(iterator.getConnectedFaces())
    def joined(first,second):
        # Recover one original quad from a triangulated strip, retaining its
        # boundary rather than treating the diagonal as a selected cross edge.
        boundary=set()
        for vertices in (first,second):
            for a,b in zip(vertices,vertices[1:]+vertices[:1]):
                pair=frozenset((a,b))
                if pair in boundary:boundary.remove(pair)
                else:boundary.add(pair)
        if len(boundary)!=4:return None
        links={}
        for a,b in boundary:links.setdefault(a,[]).append(b);links.setdefault(b,[]).append(a)
        if any(len(v)!=2 for v in links.values()):return None
        ordered=[min(links)];previous=None;current=ordered[0]
        for _ in range(3):
            following=min(v for v in links[current] if v!=previous)
            ordered.append(following);previous,current=current,following
        return ordered if len(set(ordered))==4 else None
    for edge in neighbors:
        a,b=fn.getEdgeVertices(edge)
        for owner in adjacent(edge):
            vertices=face(owner);quads=[vertices] if len(vertices)==4 else []
            if len(vertices)==3:
                for diagonal in face_edges[owner]:
                    if diagonal==edge:continue
                    for other in adjacent(diagonal):
                        if other==owner:continue
                        other_vertices=face(other)
                        if len(other_vertices)==3 and len(set(vertices)|set(other_vertices))==4:
                            quad=joined(vertices,other_vertices)
                            if quad is not None:quads.append(quad)
            for quad in quads:
                if not any({x,y}=={a,b} for x,y in zip(quad,quad[1:]+quad[:1])):continue
                remaining=frozenset(set(quad)-{a,b});other=singles.get(remaining)
                if other is None:continue
                mapping={}
                for vertex,partner in ((a,b),(b,a)):
                    index=quad.index(vertex)
                    options={quad[index-1],quad[(index+1)%4]}-{partner}
                    if len(options)!=1:break
                    mapping[vertex]=options.pop()
                if len(mapping)==2:neighbors[edge][other]=mapping
    if any(len(links)>2 for links in neighbors.values()):
        raise ValueError('Поперечные рёбра образуют разветвлённую полосу. Выделите отдельный участок.')
    pending=set(neighbors)
    while pending:
        seed=min(pending);members={seed};todo=[seed]
        while todo:
            for other in neighbors[todo.pop()]:
                if other not in members:members.add(other);todo.append(other)
        pending-=members
        if len(members)<2:continue  # One isolated edge has no spacing to change.
        ends=sorted(e for e in members if len(neighbors[e])==1)
        if len(ends) not in (0,2):raise ValueError('Не удалось упорядочить полосу выбранных рёбер.')
        closed=not ends;current=ends[0] if ends else min(members);start=current;previous=None
        rails=[[v] for v in fn.getEdgeVertices(current)];visited={current}
        while True:
            choices=sorted(e for e in neighbors[current] if e!=previous)
            if not choices:break
            following=choices[0];mapping=neighbors[current][following]
            pair=[mapping[rail[-1]] for rail in rails]
            if following==start:
                if not closed or pair!=[rail[0] for rail in rails]:raise ValueError('Полоса имеет несогласованный стык.')
                break
            if following in visited:raise ValueError('В выбранной полосе есть неоднозначная петля.')
            for rail,vertex in zip(rails,pair):rail.append(vertex)
            visited.add(following);previous,current=current,following
        if visited!=members:raise ValueError('Не удалось пройти всю выбранную полосу.')
        result.extend((tuple(rail),closed) for rail in rails)
    return result


def positions_for(fn, edges, mode):
    points=fn.getPoints(om.MSpace.kWorld);positions={}
    for vertices,closed in flow_paths(fn,edges):
        path=list(vertices)+([vertices[0]] if closed else [])
        lengths=[0.]
        for a,b in zip(path,path[1:]):lengths.append(lengths[-1]+(points[b]-points[a]).length())
        total=lengths[-1]
        if not math.isfinite(total) or total<=1e-12:raise ValueError('У цепочки рёбер нулевая длина.')
        count=len(vertices) if closed else len(vertices)-1
        for index,vertex in enumerate(vertices):
            if index==0 or not closed and index==len(vertices)-1:continue
            if mode=='relax':
                # Average along this contour only. Never include the opposite
                # end of a wall edge: that would pull its top/bottom off plane.
                before=points[vertices[index-1]];after=points[vertices[(index+1)%len(vertices)]]
                point=points[vertex]+((before-points[vertex])+(after-points[vertex]))*.25
            else:
                distance=total*index/count
                segment=min(bisect.bisect_right(lengths,distance)-1,len(path)-2)
                span=lengths[segment+1]-lengths[segment]
                if span<=0:continue
                first,last=(points[v] for v in path[segment:segment+2])
                point=first+(last-first)*((distance-lengths[segment])/span)
            if (point-points[vertex]).length()>max(total*1e-12,1e-12):positions[vertex]=point
    return positions


def apply(mode,reference=None):
    if mode not in ('relax','space','parallel'):raise ValueError('Неизвестное действие Edge Flow.')
    selection=om.MGlobal.getActiveSelectionList();targets=selected_edges();jobs=[]
    if mode=='parallel':direction,protected=(reference or last_reference()).resolve()
    for mesh,edges in targets.items():
        fn=_mesh_fn(mesh)
        if mode=='parallel':
            reference_edges=protected.get(mesh,set())
            positions=parallel_positions(fn,edges-reference_edges,direction,reference_edges)
        else:positions=positions_for(fn,edges,mode)
        if positions:jobs.append((mesh,positions))
    if not jobs:return 0
    # Prepare every target first. A later native command failure rolls back
    # this action as a whole, including earlier meshes in the selection.
    changed=False;count=0
    cmds.undoInfo(openChunk=True,chunkName='Edge Flow '+mode.title())
    try:
        for mesh,values in jobs:
            unit=om.MDistance.uiUnit()
            for vertex,point in values.items():
                target=[om.MDistance(value).asUnits(unit) for value in (point.x,point.y,point.z)]
                cmds.xform(f'{mesh}.vtx[{vertex}]',worldSpace=True,translation=target)
                changed=True;count+=1
    except Exception:
        cmds.undoInfo(closeChunk=True)
        if changed:cmds.undo()
        raise
    else:cmds.undoInfo(closeChunk=True)
    finally:om.MGlobal.setActiveSelectionList(selection)
    return count
