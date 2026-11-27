"""Temporary NURBS controls with continuous mesh preview during CV dragging."""
import bisect
import heapq
import math
import numpy as np
import maya.cmds as cmds
import maya.api.OpenMaya as om


DEFAULT_FALLOFF = ((0., 1.), (1., 0.))


def lateral_binding(data, rows, radius, matrix, by_edges=False):
    """Bind nearby vertices to the nearest chain vertex along mesh edges."""
    if radius <= 0:
        return []
    seeds = sorted({vertex for row in rows for vertex in row})
    points = None if by_edges else [point * matrix for point in om.MFnMesh(data).getPoints()]
    limit = radius + 1 if by_edges else radius
    iterator = om.MItMeshVertex(data)
    distances = {vertex: (0., vertex) for vertex in seeds}
    queue = [(0., vertex, vertex) for vertex in seeds]
    heapq.heapify(queue)
    while queue:
        distance, seed, vertex = heapq.heappop(queue)
        if distances[vertex] != (distance, seed):
            continue
        iterator.setIndex(vertex)
        for neighbor in iterator.getConnectedVertices():
            step = 1. if by_edges else (points[neighbor] - points[vertex]).length()
            candidate = distance + step
            if candidate < limit and (candidate, seed) < distances.get(neighbor, (math.inf, seed)):
                distances[neighbor] = candidate, seed
                heapq.heappush(queue, (candidate, seed, neighbor))
    selected = set(seeds)
    return [(vertex, seed, distance / limit)
            for vertex, (distance, seed) in distances.items() if vertex not in selected]


def falloff_weight(distance, falloff, smooth=False):
    index = bisect.bisect_right([point[0] for point in falloff], distance)
    if index == 0:
        return falloff[0][1]
    if index == len(falloff):
        return falloff[-1][1]
    x0, y0 = falloff[index - 1]
    x1, y1 = falloff[index]
    t = (distance - x0) / (x1 - x0)
    if smooth == 1:
        t = t * t * (3. - 2. * t)
    elif smooth == 2:
        t = t ** 3
    elif smooth == 3:
        t = 1. - (1. - t) ** 3
    return y0 + (y1 - y0) * t


def lateral_weights(binding, falloff, smooth=False):
    for vertex, seed, distance in binding:
        yield vertex, seed, falloff_weight(distance, falloff, smooth)


def parameters(points):
    lengths = [0.0]
    for a, b in zip(points, points[1:]):
        lengths.append(lengths[-1]+math.dist(a, b))
    if lengths[-1] < 1e-10:
        raise ValueError('Cannot create controls for a zero-length chain.')
    return [s/lengths[-1] for s in lengths]


def curve_data(cvs, knots, degree, closed):
    data = om.MFnNurbsCurveData().create()
    fn = om.MFnNurbsCurve()
    fn.create([om.MPoint(*p) for p in cvs], knots, degree,
              om.MFnNurbsCurve.kPeriodic if closed else om.MFnNurbsCurve.kOpen,
              False, False, parent=data)
    return data, fn


def fit_curve(points, number, closed):
    """Fit a small editable control cage; preserve the mesh using delta binding."""
    degree = min(3, number-1)
    if closed:
        knots = list(range(1-degree, number+degree))
        seed = [(math.cos(2*math.pi*(i % number)/number),
                 math.sin(2*math.pi*(i % number)/number), 0)
                for i in range(number+degree)]
    else:
        full = [0.]*(degree+1)+[i/(number-degree) for i in range(1, number-degree)]+[1.]*(degree+1)
        knots = full[1:-1]
        seed = [(i/(number-1), 0, 0) for i in range(number)]
    data, fn = curve_data(seed, knots, degree, closed)
    lo, hi = fn.knotDomain
    steps = max(64, number*4)
    ts = np.linspace(0., 1., steps, endpoint=not closed)
    original = parameters(points)
    target = []
    for t in ts:
        i = min(len(points)-2, max(0, bisect.bisect_right(original, t)-1))
        w = (t-original[i])/max(1e-15, original[i+1]-original[i])
        target.append([a+(b-a)*w for a, b in zip(points[i], points[i+1])])
    basis = np.empty((len(ts), number))
    for column in range(number):
        fn.setCVPositions([om.MPoint(1. if i % number == column else 0., 0, 0)
                           for i in range(len(seed))])
        fn.updateCurve()
        basis[:, column] = [fn.getPointAtParam(lo+t*(hi-lo)).x for t in ts]
    if closed:
        cvs = np.linalg.lstsq(basis, np.array(target), rcond=None)[0]
        cvs = np.concatenate((cvs, cvs[:degree]))
    else:
        cvs = np.empty((number, 3))
        cvs[0], cvs[-1] = points[0], points[-1]
        rhs = np.array(target)-basis[:, :1]*cvs[0]-basis[:, -1:]*cvs[-1]
        cvs[1:-1] = np.linalg.lstsq(basis[:, 1:-1], rhs, rcond=None)[0]
    cvs = [tuple(map(float, p)) for p in cvs]
    return cvs, knots, degree


def node_path(uuid):
    nodes = cmds.ls(uuid, long=True) or []
    return nodes[0] if nodes else None


def dag_path(name):
    selection = om.MSelectionList()
    selection.add(name)
    return selection.getDagPath(0)


def relative_matrix(curve, mesh):
    """Cancel common parents before multiplying large world coordinates."""
    target = mesh.transform()
    current = om.MDagPath(curve)
    current.pop()
    result = om.MMatrix()
    while current.length():
        if current.node() == target:
            return result
        result = result*om.MFnTransform(current).transformation().asMatrix()
        current.pop()
    return curve.inclusiveMatrix()*mesh.inclusiveMatrixInverse()


class ControlCurves:
    def __init__(self, session, number):
        from recurve import QtCore
        self.session, self.number = session, number
        self.closed = False
        self.deformed = False
        self.updating = False
        self.last_signature = None
        self.previous_soft_selection = None
        self.group = None
        self.guides = []
        self.selection_mode = om.MGlobal.selectionMode()
        self.component_mask = om.MGlobal.componentSelectionMask()
        hilite = om.MGlobal.getHiliteList()
        self.hilite_names = [hilite.getDagPath(i).fullPathName() for i in range(hilite.length())]
        fn = om.MFnMesh(session.after)
        parent = om.MFnDagNode(dag_path(session.path).transform()).fullPathName()
        definitions = []
        for row in session.ids:
            points = [tuple(fn.getPoint(v))[:3] for v in row]
            closed = row[0] == row[-1]
            cvs, knots, degree = fit_curve(points, number, closed)
            definitions.append((cvs, knots, degree, closed))
        cmds.undoInfo(openChunk=True, chunkName='Recurve Create Control Curves')
        try:
            group = cmds.createNode('transform', name='recurveControls#', parent=parent)
            self.group = cmds.ls(group, uuid=True)[0]
            for index, (cvs, knots, degree, closed) in enumerate(definitions):
                curve = cmds.curve(name='recurveControl%d#' % (index+1), point=cvs,
                                   knot=knots, degree=degree, periodic=closed)
                curve = cmds.parent(curve, group, relative=True)[0]
                uuid = cmds.ls(curve, uuid=True)[0]
                shape = cmds.listRelatives(curve, shapes=True, fullPath=True)[0]
                cmds.setAttr(shape+'.overrideEnabled', True)
                cmds.setAttr(shape+'.overrideRGBColors', True)
                color = (1., .7, .15) if index % 2 == 0 else (.2, .85, 1.)
                cmds.setAttr(shape+'.overrideColorRGB', *color, type='double3')
                cmds.setAttr(shape+'.dispCV', True)
                baseline, baseline_fn = curve_data(cvs, knots, degree, closed)
                self.guides.append({'uuid': uuid, 'cvs': cvs, 'periodic': closed,
                                    'baseline': baseline, 'fn': baseline_fn})
            self.select()
        except Exception:
            if self.group and node_path(self.group):
                cmds.delete(node_path(self.group))
            raise
        finally:
            cmds.undoInfo(closeChunk=True)
        self.last_signature = self.signature()
        self.timer = QtCore.QTimer(session)
        self.timer.setInterval(33)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.timeout.connect(self.poll)

    def paths(self):
        result = []
        for guide in self.guides:
            name = node_path(guide['uuid'])
            if not name:
                return None
            path = dag_path(name)
            path.extendToShape()
            if not path.node().hasFn(om.MFn.kNurbsCurve):
                return None
            result.append(path)
        return result

    def signature(self):
        paths = self.paths()
        if paths is None:
            return None
        mesh = dag_path(self.session.path)
        result = []
        for path in paths:
            fn = om.MFnNurbsCurve(path)
            matrix = relative_matrix(path,mesh)
            result.extend(tuple(p)[:3] for p in fn.cvPositions())
            result.append(tuple(matrix))
        return tuple(result)

    def poll(self):
        if self.closed or self.updating or self.session.controls is not self:
            return
        self.updating = True
        try:
            try:
                signature = self.signature()
            except RuntimeError:
                signature = None
            if signature != self.last_signature:
                # This main-thread timer runs between input events. Only the
                # existing preview mesh data changes: no DAG/connection edits.
                # evalDeferred would postpone this until after a Maya drag.
                self.flush()
                self.last_signature = signature
        except Exception as exc:
            # Stop repeated failures; leave the last valid preview available
            # for cancellation, without deleting nodes inside the timer.
            self.timer.stop()
            from recurve import record_error
            record_error('control_preview', exc)
            self.session.message(str(exc))
        finally:
            self.updating = False

    def flush(self):
        if not self.closed and self.session.controls is self:
            self.session.update_controls()
            if not cmds.about(batch=True):
                # Scheduling a refresh also waits for idle. Refresh the active
                # view now so a held mouse button does not delay the display.
                from recurve import omui
                omui.M3dView.active3dView().refresh(False, True)

    def influence_binding(self, data, rows):
        by_edges = self.session.influence_by_edges
        radius = self.session.influence_edges if by_edges else self.session.influence_distance
        if radius <= 0:
            return []
        matrix = dag_path(self.session.path).inclusiveMatrix()
        key = (data, tuple(tuple(row) for row in rows), radius, by_edges, tuple(matrix))
        if getattr(self, '_influence_key', None) != key:
            self._influence_binding = lateral_binding(data, rows, radius, matrix, by_edges)
            self._influence_key = key
        return self._influence_binding

    def deform(self, data, rows, align=None):
        self.deformed = False
        if align is None:align = self.session.align_to_curve
        paths = self.paths()
        if paths is None:
            return data
        fn = om.MFnMesh(data)
        points = fn.getPoints()
        mesh = dag_path(self.session.path)
        deltas = {}
        for row, guide, path in zip(rows, self.guides, paths):
            original = [tuple(points[v])[:3] for v in row]
            ts = parameters(original)
            curve, baseline = om.MFnNurbsCurve(path), guide['fn']
            lo, hi = curve.knotDomain
            start, end = baseline.knotDomain
            matrix = relative_matrix(path,mesh)
            indices = row[:-1] if row[0] == row[-1] else row
            for vertex, t in zip(indices, ts):
                current = curve.getPointAtParam(lo+t*(hi-lo))*matrix
                initial = points[vertex] if align else baseline.getPointAtParam(start+t*(end-start))
                delta = current-initial
                deltas[vertex] = delta
        for vertex, seed, weight in lateral_weights(self.influence_binding(data, rows), self.session.influence_falloff, self.session.influence_smooth):
            deltas[vertex] = deltas[seed] * weight
        moved = [vertex for vertex, delta in deltas.items() if delta.length() > 1e-8]
        for vertex in moved:
            points[vertex] += deltas[vertex]
        if not moved:
            return data
        from recurve import copy_mesh_data
        result = copy_mesh_data(data)
        out = om.MFnMesh(result)
        out.setPoints(points)
        # Moved corners follow the edited geometry; unrelated authored locks stay.
        it = om.MItMeshVertex(result)
        unlock = []
        for vertex in moved:
            it.setIndex(vertex)
            for face in it.getConnectedFaces():
                corner = list(out.getPolygonVertices(face)).index(vertex)
                if out.isNormalLocked(out.getFaceNormalIds(face)[corner]):
                    unlock.append(vertex)
                    break
        if unlock:
            out.unlockVertexNormals(unlock)
        self.deformed = True
        return result

    def select(self):
        paths = self.paths()
        if paths is None:
            raise ValueError('Control curves are missing. Undo their deletion or remove and recreate controls.')
        selection = om.MSelectionList()
        hilite = om.MSelectionList()
        for path in paths:
            selection.add(path.fullPathName()+'.cv[0:%d]' % (self.number-1))
            transform = om.MDagPath(path)
            transform.pop()
            hilite.add(transform)
        om.MGlobal.setSelectionMode(om.MGlobal.kSelectComponentMode)
        om.MGlobal.setComponentSelectionMask(om.MSelectionMask.kSelectCVs)
        om.MGlobal.setHiliteList(hilite)
        om.MGlobal.setActiveSelectionList(selection)
        cmds.setToolTo('Move')

    def reset(self):
        paths = self.paths()
        if paths is None:
            raise ValueError('Control curves are missing. Undo their deletion or remove and recreate controls.')
        identity = tuple(om.MMatrix())
        cmds.undoInfo(openChunk=True, chunkName='Recurve Reset Control Curves')
        try:
            cmds.xform(node_path(self.group), objectSpace=True, matrix=identity)
            for path, guide in zip(paths, self.guides):
                transform = om.MFnDagNode(path.transform()).fullPathName()
                cmds.xform(transform, objectSpace=True, matrix=identity)
                for i, point in enumerate(guide['cvs'][:self.number]):
                    cmds.xform(path.fullPathName()+'.cv[%d]' % i, objectSpace=True, translation=point)
        finally:
            cmds.undoInfo(closeChunk=True)
        self.session.update_controls()
        self.last_signature = self.signature()
        self.select()

    @staticmethod
    def edit_soft_selection(**settings):
        undo_enabled = cmds.undoInfo(query=True, state=True)
        if undo_enabled:
            cmds.undoInfo(stateWithoutFlush=False)
        try:
            cmds.softSelect(edit=True, **settings)
        finally:
            if undo_enabled:
                cmds.undoInfo(stateWithoutFlush=True)

    def set_soft_selection(self, mode, radius):
        if mode == 0:
            self.restore_soft_selection()
            return
        if self.previous_soft_selection is None:
            self.previous_soft_selection = {
                flag: cmds.softSelect(query=True, **{flag: True})
                for flag in ('softSelectEnabled', 'softSelectFalloff', 'softSelectDistance')
            }
        self.edit_soft_selection(softSelectEnabled=True, softSelectFalloff=mode - 1,
                                 softSelectDistance=radius)

    def restore_soft_selection(self):
        if self.previous_soft_selection is not None:
            self.edit_soft_selection(**self.previous_soft_selection)
            self.previous_soft_selection = None

    def close(self,*,keep=False):
        self.closed = True
        self.restore_soft_selection()
        self.timer.stop()
        self.timer.deleteLater()
        if keep:return
        group = node_path(self.group) if self.group else None
        if group:
            # Native undoable deletion keeps earlier Maya CV-move commands valid.
            cmds.delete(group)
        om.MGlobal.setSelectionMode(self.selection_mode)
        om.MGlobal.setComponentSelectionMask(self.component_mask)
        hilite = om.MSelectionList()
        for name in self.hilite_names:
            if cmds.objExists(name):hilite.add(name)
        om.MGlobal.setHiliteList(hilite)


class FaceControlCurves(ControlCurves):
    """Contour CVs drive every vertex of the selected face region smoothly."""
    def deform(self, data, rows):
        self.deformed = False
        paths = self.paths()
        if paths is None:return data
        fn = om.MFnMesh(data)
        points = fn.getPoints()
        delta = np.zeros((len(points),3))
        contributions = np.zeros(len(points))
        mesh = dag_path(self.session.path)
        for row, guide, path in zip(rows,self.guides,paths):
            ts = parameters([tuple(points[v])[:3] for v in row])
            curve, baseline = om.MFnNurbsCurve(path), guide['fn']
            lo, hi = curve.knotDomain
            start, end = baseline.knotDomain
            matrix = relative_matrix(path,mesh)
            for vertex, t in zip(row[:-1],ts):
                current = curve.getPointAtParam(lo+t*(hi-lo))*matrix
                initial = baseline.getPointAtParam(start+t*(end-start))
                delta[vertex] += tuple(current-initial)
                contributions[vertex] += 1
        border = self.session.face_region.boundary
        delta[border] /= contributions[border,None]
        if not np.any(np.abs(delta[border])>1e-8):return data
        delta = self.session.face_region.spread(delta)
        moved = [v for v in self.session.face_region.vertices if np.linalg.norm(delta[v])>1e-8]
        if not moved:return data
        for vertex in moved:points[vertex] += om.MVector(*delta[vertex])
        from recurve import copy_mesh_data
        result = copy_mesh_data(data)
        out = om.MFnMesh(result)
        out.setPoints(points)
        # Like edge controls, unlock only authored normals on moved vertices.
        # Asking Maya to unlock an uninitialized MeshData normal array can crash.
        potential = [v for v in moved if v in self.session.face_region.locked_vertices]
        if potential:out.getNormals()
        iterator = om.MItMeshVertex(result)
        unlock = []
        for vertex in potential:
            iterator.setIndex(vertex)
            for face in iterator.getConnectedFaces():
                corner = list(out.getPolygonVertices(face)).index(vertex)
                if out.isNormalLocked(out.getFaceNormalIds(face)[corner]):
                    unlock.append(vertex)
                    break
        if unlock:out.unlockVertexNormals(unlock)
        self.deformed = True
        return result


