"""Model Pack host for Joe Wu's unchanged extrudeX 1.01 distribution."""
import importlib.util
from pathlib import Path
import sys
import math
from collections import Counter, defaultdict

from maya import cmds
from maya.api import OpenMaya as om

ROOT = Path(__file__).resolve().parent / 'vendor' / 'extrudeX'
RUNTIME = 'model_pack_extrudex_runtime'


def runtime():
    module = sys.modules.get(RUNTIME)
    if module is None:
        version = str(cmds.about(apiVersion=True))[:4]
        script = ROOT / 'extrudeX.py'
        plugin = ROOT / 'plug-ins' / version / 'extrudeX.mll'
        if not script.is_file() or not plugin.is_file():
            raise RuntimeError('Поместите оригинальный ExtrudeX 1.01 и модуль для вашей версии Maya в vendor/extrudeX. См. README.')
        if not cmds.pluginInfo('extrudeX', query=True, loaded=True):
            cmds.loadPlugin(str(plugin), quiet=True)
        spec = importlib.util.spec_from_file_location(RUNTIME, script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[RUNTIME] = module
    _install_contours(module)
    return module


def _internal_regions():
    """Find the filled, nearly planar side of explicit closed internal loops.

    Return None for the original open-border workflow. Read the API selection
    without running selection constraints, which can change user preferences.
    """
    selection = om.MGlobal.getActiveSelectionList()
    meshes = {}
    for index in range(selection.length()):
        try:
            path, component = selection.getComponent(index)
        except RuntimeError:
            return None
        if component.isNull() or not component.hasFn(om.MFn.kMeshEdgeComponent):
            return None
        if path.node().hasFn(om.MFn.kTransform):
            path.extendToShape()
        if not path.node().hasFn(om.MFn.kMesh):
            return None
        name = path.fullPathName()
        meshes.setdefault(name, (path, set()))[1].update(om.MFnSingleIndexedComponent(component).getElements())
    if not meshes:
        return None
    boundary_count = total = 0
    for path, selected in meshes.values():
        iterator = om.MItMeshEdge(path)
        for edge in selected:
            iterator.setIndex(edge)
            boundary_count += iterator.onBoundary()
            total += 1
    if boundary_count == total:
        return None
    if boundary_count:
        raise ValueError('Выделите отдельно открытые края или замкнутый внутренний контур.')
    result = []
    for name, (path, selected) in meshes.items():
        fn = om.MFnMesh(path)
        if path.isInstanced() or cmds.referenceQuery(name, isNodeReferenced=True):
            raise ValueError('Для Inset нужен локальный меш без экземпляров.')
        degree = Counter(v for edge in selected for v in fn.getEdgeVertices(edge))
        if not degree or any(value != 2 for value in degree.values()):
            raise ValueError('Для Inset выделите замкнутый контур без разветвлений.')
        edge_faces, neighbors = {}, defaultdict(set)
        iterator = om.MItMeshEdge(path)
        for edge in range(fn.numEdges):
            iterator.setIndex(edge)
            faces = set(iterator.getConnectedFaces())
            edge_faces[edge] = faces
            if edge in selected and len(faces) != 2:
                raise ValueError('Контур Inset должен лежать на manifold-поверхности.')
            if edge not in selected:
                for face in faces:
                    neighbors[face].update(faces - {face})
        polygon = om.MItMeshPolygon(path)
        face_edges, normals, areas = {}, {}, {}
        for face in range(fn.numPolygons):
            polygon.setIndex(face)
            face_edges[face] = tuple(polygon.getEdges())
            normals[face] = fn.getPolygonNormal(face, om.MSpace.kWorld)
            areas[face] = polygon.getArea(om.MSpace.kWorld)
        remaining = set(range(fn.numPolygons))
        candidates = []
        while remaining:
            seed = min(remaining)
            region, pending = {seed}, [seed]
            remaining.remove(seed)
            for face in pending:
                more = neighbors[face] & remaining
                remaining.difference_update(more)
                region.update(more)
                pending.extend(sorted(more))
            rim = {e for face in region for e in face_edges[face]
                   if len(edge_faces[e]) == 1 or edge_faces[e] - region}
            if not rim or not rim <= selected:
                continue
            area = sum(areas[f] for f in region)
            normal = sum((normals[f] * areas[f] for f in region), om.MVector())
            # A filled contour can contain tessellation noise; the other side
            # of a solid normally includes its back and walls and fails this.
            if area > 1e-12 and normal.length() / area > .999:
                candidates.append((region, rim))
        owners = Counter(edge for _, rim in candidates for edge in rim)
        if set(owners) != selected or any(count != 1 for count in owners.values()):
            raise ValueError('Не удалось однозначно найти плоскую область внутри контура Inset.')
        faces = sorted({face for region, _ in candidates for face in region})
        points = fn.getPoints()
        average = sum((points[b] - points[a]).length()
                      for a, b in (fn.getEdgeVertices(e) for e in selected)) / len(selected)
        result.append((name, faces, max(average, 1e-6)))
    return result


def _start_contours(module, regions, drag, rel, mode):
    class ContourSession(module._Session):
        internal_contour = True

        def apply(self, distance, live=False, spread=None):
            # Native face extrusion retains the old contour, inserts its ring,
            # and moves the filled interior. Negative distance matches the
            # original ExtrudeX inward drag convention.
            inset = min(max(0., -distance), *self.limits)
            for node in self.nodes:
                cmds.setAttr(node + '.offset', inset if self.mode == 'offset' else 0.)
                cmds.setAttr(node + '.localTranslateZ', -distance if self.mode == 'extrude' else 0.)
            self.narrow = inset if self.mode == 'offset' else abs(distance)

        def valid(self):
            return all(cmds.objExists(node) for node in self.nodes) and super().valid()

    session = ContourSession()
    session.nodes = []
    session.limits = []
    session.ratio = 0.
    session.mode = session.start_mode = mode
    session.avg = sum(average for _, _, average in regions) / len(regions)
    session.step = session.avg / 150.
    distance = (module.DIST_REL_DEFAULT if rel is None else rel) * session.avg
    cmds.undoInfo(openChunk=True, chunkName='ExtrudeX Inset')
    module._CHUNK[0] = True
    created = False
    try:
        inner_selection = []
        for shape, faces, average in regions:
            count = cmds.polyEvaluate(shape, vertex=True)
            node = cmds.polyExtrudeFacet([shape + '.f[%d]' % f for f in faces],
                                        keepFacesTogether=True, divisions=1,
                                        offset=0., localTranslateZ=0., constructionHistory=True)[0]
            created = True
            session.nodes.append(node)
            session.limits.append(_inset_limit(shape, node, faces, average * .01))
            session.shapes.append(dict(shape=shape, n_old=count,
                                       nverts=cmds.polyEvaluate(shape, vertex=True)))
            path = om.MSelectionList().add(shape).getDagPath(0)
            iterator = om.MItMeshEdge(path)
            cap = set(faces)
            while not iterator.isDone():
                connected = set(iterator.getConnectedFaces())
                if connected & cap and connected - cap:
                    inner_selection.append(shape + '.e[%d]' % iterator.index())
                iterator.next()
        session.apply(distance)
        session.d = -session.narrow if mode == 'offset' else distance
        cmds.select(inner_selection, replace=True)
        module._SESSION = session
        module._hud_refresh()
        if drag:
            module._drag_tool()
    except Exception:
        module._SESSION = None
        module._close_chunk()
        if created:
            cmds.undo()
        raise


def _inset_limit(shape, node, faces, probe):
    """Stop an inset before an original cap polygon reverses orientation."""
    path = om.MSelectionList().add(shape).getDagPath(0)
    fn = om.MFnMesh(path)
    before = fn.getPoints()
    rings = [list(fn.getPolygonVertices(face)) for face in faces]
    state = cmds.undoInfo(query=True, stateWithoutFlush=True)
    cmds.undoInfo(stateWithoutFlush=False)
    try:
        cmds.setAttr(node + '.offset', probe)
        after = om.MFnMesh(path).getPoints()
    finally:
        cmds.setAttr(node + '.offset', 0.)
        cmds.undoInfo(stateWithoutFlush=state)
    field = [(b - a) / probe for a, b in zip(before, after)]
    limit = math.inf
    for ring in rings:
        a = ring[0]
        normal = sum(((before[b] - before[a]) ^ (before[c] - before[a])
                      for b, c in zip(ring[1:-1], ring[2:])), om.MVector())
        area = normal.length()
        span = max((before[v] - before[a]).length() for v in ring)
        if area <= max(span * span * 1e-8, 1e-16):
            continue
        normal.normalize()
        linear = quadratic = 0.
        for b, c in zip(ring[1:-1], ring[2:]):
            u, v = before[b] - before[a], before[c] - before[a]
            du, dv = field[b] - field[a], field[c] - field[a]
            linear += ((du ^ v) + (u ^ dv)) * normal
            quadratic += (du ^ dv) * normal
        roots = []
        if abs(quadratic) < 1e-12:
            if linear < -1e-12:
                roots.append(-area / linear)
        else:
            discriminant = linear * linear - 4 * quadratic * area
            if discriminant >= 0:
                value = -.5 * (linear + math.copysign(math.sqrt(discriminant), linear))
                if abs(value) > 1e-20:
                    roots.extend((value / quadratic, area / value))
        limit = min([limit] + [r * .99 for r in roots if r > 1e-9])
    return limit


def _install_contours(module):
    """Adapt the host runtime; keep the supplied script and binary unchanged."""
    if getattr(module, '_model_pack_adapter', None) is sys.modules[__name__]:
        return
    names = ('_start', '_switch_mode', '_hud_text', '_hud_hint', '_ctx_press', '_hud_title')
    if not hasattr(module, '_model_pack_native'):
        module._model_pack_native = {name: getattr(module, name) for name in names}
    for name in names:
        module._model_pack_native.setdefault(name, getattr(module, name))
    original = module._model_pack_native

    def internal():
        return getattr(module._SESSION, 'internal_contour', False)

    def start(drag=True, rel=None, mode='offset'):
        if module._SESSION is not None:
            module._finish()
        regions = _internal_regions()
        if regions is None:
            return original['_start'](drag=drag, rel=rel, mode=mode)
        return _start_contours(module, regions, drag, rel, mode)

    def switch(mode):
        if not internal():
            return original['_switch_mode'](mode)
        module._SESSION.mode = mode
        module._hud_label()

    def text():
        if not internal():
            return original['_hud_text']()
        session = module._SESSION
        distance = session.d if session.live_d is None else session.live_d
        if session.mode == 'offset':
            return '%.4f' % session.narrow + ('   (limit)' if session.narrow < max(0., -distance) - 1e-6 else '')
        return '%.4f' % -distance

    def hint():
        if not internal():
            return original['_hud_hint']()
        mode = module._SESSION.start_mode
        return 'drag = %s   Shift drag = %s   Q = finish' % (
            'inset' if mode == 'offset' else 'extrude', 'extrude' if mode == 'offset' else 'inset')

    def title():
        return 'Inset distance' if internal() and module._SESSION.mode == 'offset' else original['_hud_title']()

    def press():
        original['_ctx_press']()
        if internal():
            module._DRAG['spread'] = False
            modifiers = cmds.draggerContext(module.CTX, query=True, modifier=True) or ''
            mode = module._SESSION.start_mode
            switch(('extrude' if mode == 'offset' else 'offset') if 'shift' in modifiers else mode)

    for name, function in zip(names, (start, switch, text, hint, press, title)):
        setattr(module, name, function)
    module._model_pack_adapter = sys.modules[__name__]


def active():
    module = sys.modules.get(RUNTIME)
    return module is not None and module._SESSION is not None


def shutdown():
    module = sys.modules.get(RUNTIME)
    if module is not None and (module._SESSION is not None or module._CHUNK[0]):
        module._finish()


def start(toolbox, mode='offset'):
    if mode not in ('offset', 'extrude'):
        raise ValueError(mode)
    toolbox._prepare_tool('extrudex')
    try:
        module = runtime()
        (module.run_extrude if mode == 'extrude' else module.run)()
    except (RuntimeError, ValueError) as error:
        shutdown()
        cmds.warning('ExtrudeX: ' + str(error))
    sync(toolbox)


def toggle(toolbox):
    if active():
        shutdown()
        sync(toolbox)
    else:
        start(toolbox)


def sync(toolbox):
    enabled = active()
    button = getattr(toolbox, 'extrudex_button', None)
    if button is not None:
        button.setChecked(enabled)
    if not enabled and toolbox._active_tool == 'extrudex':
        toolbox._active_tool = None


def menu(toolbox):
    from PySide6 import QtWidgets
    result = QtWidgets.QMenu(toolbox)
    for label, mode in (('Offset — Surface', 'offset'),
                        ('Extrude — Normal', 'extrude')):
        action = result.addAction(label)
        action.triggered.connect(lambda checked=False, value=mode: start(toolbox, value))
    return result
