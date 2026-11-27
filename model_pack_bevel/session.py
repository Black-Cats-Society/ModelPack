"""One reversible Bevel session; release pauses, Enter creates one history step."""
from __future__ import annotations
from contextlib import contextmanager
import json
import itertools
import math
from pathlib import Path

import maya.cmds as cmds
from maya.api import OpenMaya as om

from slide_for_maya.core import _mesh_fn, _selected_edges, _mesh_position_tolerance, SlideError
from .graph import EdgeGraph
from .profiles import Curve
from model_pack_paths import plugin_path

NODE_NAME = "modelPackGestureBevel"
PLUGIN_PATH = plugin_path(Path(__file__).with_name("model_pack_gesture_bevel.py"))
_PLUGIN_REFRESHED = False
JUNCTION_PLUGIN=plugin_path(Path(__file__).resolve().parents[1]/'model_pack_bevel_junctions.py')
_JUNCTION_REFRESHED=False
MAX_SUPPORT_EDGES = 8
DEFAULT_SUPPORT_PERCENT = 20.


def ensure_junction_plugin():
    global _JUNCTION_REFRESHED
    if not cmds.pluginInfo(JUNCTION_PLUGIN,q=True,loaded=True):cmds.loadPlugin(JUNCTION_PLUGIN,quiet=True)
    if not _JUNCTION_REFRESHED:
        import importlib
        import model_pack_legacy_mesh as backend
        module=importlib.import_module('model_pack_bevel_junctions');importlib.reload(module)
        with without_undo():
            probe=cmds.createNode('modelPackBevelJunctions',skipSelect=True)
            try:
                for cls in backend.JUNCTION_NODE_CLASSES:cls.compute=module.JunctionNode.compute
            finally:cmds.delete(probe)
        _JUNCTION_REFRESHED=True


@contextmanager
def without_undo():
    enabled = cmds.undoInfo(q=True, state=True)
    if enabled:
        cmds.undoInfo(stateWithoutFlush=False)
    try:
        yield
    finally:
        if enabled:
            cmds.undoInfo(stateWithoutFlush=True)


def ensure_plugin():
    global _PLUGIN_REFRESHED
    if not cmds.pluginInfo(PLUGIN_PATH, q=True, loaded=True):
        cmds.loadPlugin(PLUGIN_PATH, quiet=True)
    if NODE_NAME not in (cmds.pluginInfo(PLUGIN_PATH,q=True,dependNode=True) or []):
        raise SlideError("Не удалось зарегистрировать узел Bevel. Проверьте загрузку model_pack_gesture_bevel.py.")
    # Make saved scenes load the node without relying on importing the toolbox.
    cmds.pluginInfo(PLUGIN_PATH, edit=True, autoload=True)
    if not cmds.about(batch=True):
        cmds.pluginInfo(savePluginPrefs=True)
    if not _PLUGIN_REFRESHED:
        refresh_plugin()


def refresh_plugin():
    """Update Python methods of loaded nodes without unloading scene history."""
    global _PLUGIN_REFRESHED
    if not cmds.pluginInfo(PLUGIN_PATH,q=True,loaded=True):
        return
    import importlib
    if cmds.pluginInfo(JUNCTION_PLUGIN,q=True,loaded=True):
        ensure_junction_plugin()
        with without_undo():
            for node in cmds.ls(type='modelPackBevelJunctions') or []:cmds.dgdirty(node)
    implementation = importlib.import_module("model_pack_bevel.model_pack_gesture_bevel")
    importlib.reload(implementation)
    with without_undo():
        probe = cmds.createNode(NODE_NAME,skipSelect=True)
        try:
            selection = om.MSelectionList()
            selection.add(probe)
            cls = type(om.MFnDependencyNode(selection.getDependNode(0)).userNode())
            cls.compute = implementation.GestureBevelNode.compute
            cls.setDependentsDirty = implementation.GestureBevelNode.setDependentsDirty
            nodes = [n for n in cmds.ls(type=NODE_NAME) or [] if n != probe]
            for node in nodes:
                selection = om.MSelectionList()
                selection.add(node)
                instance = om.MFnDependencyNode(selection.getDependNode(0)).userNode()
                # Older histories copied only connected matrices. polyBevel3
                # usually stores inputMatrix as a constant, so their round
                # template evaluated an identity transform on scaled meshes.
                flat = cmds.listConnections(node+".flatMesh",s=True,d=False,type="polyBevel3") or []
                rounded = cmds.listConnections(node+".roundMesh",s=True,d=False,type="polyBevel3") or []
                if flat and rounded:
                    meta = (json.loads(cmds.getAttr(node+".sessionMeta"))
                            if cmds.attributeQuery("sessionMeta",node=node,exists=True) else {})
                    if not meta.get("sharp"):
                        _share_template_matrices(flat[0],rounded[0])
                        _segment_source(node,flat[0],rounded[0], supports=meta.get("round_supports", 0))
                instance._template = instance._key = instance._limits = None
                cmds.dgdirty(node)
        finally:
            cmds.delete(probe)
    _PLUGIN_REFRESHED = True


def _shape(mesh):
    # API selections and Select Path can name edges on the mesh shape itself.
    # A shape has no child shapes: querying listRelatives(shapes=True) on it
    # returned None, so the final build failed after deleting its preview.
    if cmds.nodeType(mesh)=='mesh':return (cmds.ls(mesh,long=True) or [mesh])[0]
    shapes=cmds.listRelatives(mesh,shapes=True,noIntermediate=True,type='mesh',fullPath=True) or []
    if not shapes:raise SlideError('Не найден polygon mesh для фаски.')
    return shapes[0]


def _set_string(node, name, value):
    cmds.setAttr(node + "." + name, value if isinstance(value, str) else json.dumps(value), type="string")


def _share_template_matrices(flat, rounded):
    """Use the same transform for both native cross-section templates."""
    for attr in ("inputMatrix", "matrix"):
        if (cmds.attributeQuery(attr,node=flat,exists=True) and
                cmds.attributeQuery(attr,node=rounded,exists=True)):
            source, target = flat+"."+attr, rounded+"."+attr
            if not cmds.isConnected(source,target):
                # Connect the attribute itself, including Maya's constant
                # matrix value and any later edits or incoming connections.
                cmds.connectAttr(source,target,force=True)


def _support_driver(template):
    sources = cmds.listConnections(template + ".segments", s=True, d=False, skipConversionNodes=True) or []
    return next((node for node in sources if cmds.nodeType(node) in ("addDoubleLinear", "addDL")), None)


def _segment_source(node, flat=None, rounded=None, *, value=None, supports=0):
    """Keep segment control upstream of both templates and the final node.

    Driving the templates from node.segments creates a dependency cycle:
    template.output -> node.flatMesh -> node.segments -> template.segments.
    Maya bakes the rendered mesh but retains that cycle on Delete History.
    A native Segments proxy keeps the control editable without backward wires.
    This also upgrades saved nodes from the original connection layout.
    """
    plug = node+".segments"
    incoming = cmds.listConnections(plug,s=True,d=False,plugs=True) or []
    legacy = [target for target in (cmds.listConnections(plug,s=False,d=True,plugs=True) or [])
              if target in [n+".segments" for n in (flat,rounded) if n]]
    if incoming and not legacy:
        source = incoming[0]
    else:
        if value is None:value = cmds.getAttr(plug)
        settings = cmds.createNode("network",name="bevelParameters#",skipSelect=True)
        cmds.addAttr(settings,longName="segments",attributeType="long",minValue=1,maxValue=64,
                     defaultValue=value,keyable=True)
        source = settings+".segments"
        # Preserve an authored upstream driver, including animation, while
        # exposing it through the same proxy plug after migration.
        if incoming:
            cmds.disconnectAttr(incoming[0],plug)
            cmds.connectAttr(incoming[0],source)
        for target in legacy:
            cmds.disconnectAttr(plug,target)
        cmds.connectAttr(source,plug)
    driver = source
    if flat and supports:
        extra = _support_driver(flat) or cmds.createNode("addDoubleLinear", name="bevelSupportSegments#")
        if not cmds.isConnected(source, extra + ".input1", ignoreUnitConversion=True):
            cmds.connectAttr(source, extra + ".input1", force=True)
        if cmds.getAttr(extra + ".input2") != 2 * supports:
            cmds.setAttr(extra + ".input2", 2 * supports)
        driver = extra + ".output"
    for template in (flat,rounded):
        if template and not cmds.isConnected(driver,template+".segments", ignoreUnitConversion=True):
            cmds.connectAttr(driver,template+".segments",force=True)
    # Keep the registered segments input compatible with older plugin classes
    # and scene files. A per-node dynamic proxy is the editable Channel Box
    # control; changing a registered attribute's proxy flag in place is unsafe.
    if not cmds.attributeQuery("segmentCount",node=node,exists=True):
        cmds.addAttr(node,longName="segmentCount",niceName="Segments",proxy=source,keyable=True)
    elif not cmds.isConnected(source,node+".segmentCount"):
        cmds.connectAttr(source,node+".segmentCount",force=True)
    cmds.setAttr(plug,keyable=False,channelBox=False)
    return source


def _interior_edges(mesh, edges):
    if not edges:
        return ()
    accepted = []
    iterator = om.MItMeshEdge(mesh)
    for edge in edges:
        iterator.setIndex(edge)
        sides = len(iterator.getConnectedFaces())
        if sides == 2:
            accepted.append(edge)
        elif sides > 2:
            raise SlideError("Bevel: рёбра с более чем двумя гранями не поддерживаются.")
    return tuple(accepted)


def _prepare_edges(mesh, edges, transverse_cuts=0):
    """Subdivide only when requested; preserve the input edge layout by default."""
    fn = _mesh_fn(mesh)
    original = fn.getPoints(om.MSpace.kWorld)
    pairs = {e: fn.getEdgeVertices(e) for e in edges}
    graph = EdgeGraph(original, pairs.items())
    lengths = [math.dist(graph.points[a], graph.points[b]) for a, b in pairs.values()]
    scale = min(lengths)
    safe = {}
    vertices = om.MItMeshVertex(fn.dagPath())
    for vertex in graph.neighbors:
        vertices.setIndex(vertex)
        safe[vertex] = {"blend":[[vertex,1]],"rails":{str(vertex):list(vertices.getConnectedVertices())}}
    division_counts = {edge: transverse_cuts for edge in edges}
    nodes = []
    for divisions in sorted(set(division_counts.values()) - {0}):
        current = _mesh_fn(mesh)
        edge_map = {frozenset(current.getEdgeVertices(e)):e for e in range(current.numEdges)}
        ids = [edge_map[frozenset(pairs[e])] for e in edges if division_counts[e] == divisions]
        nodes.append(cmds.polySubdivideEdge([f"{mesh}.e[{e}]" for e in ids],
                                           divisions=divisions,constructionHistory=True)[0])
    fn = _mesh_fn(mesh)
    points = fn.getPoints(om.MSpace.kWorld)
    # polySubdivideEdge keeps original vertices. Locate only appended samples
    # on the selected segments, then use endpoint membership to recover edges.
    membership = {v: set() for v in range(fn.numVertices)}
    limits = dict(safe)
    for edge, (a, b) in pairs.items():
        membership[a].add(edge)
        membership[b].add(edge)
    for vertex in range(len(original), fn.numVertices):
        if vertex == len(original):
            # Hash expected subdivision samples once, avoiding a scan of all
            # selected edges for every new sample on large selections.
            tolerance = _mesh_position_tolerance(fn,original,scale)
            samples = {}
            def cell(point):
                return tuple(math.floor(p/tolerance) for p in tuple(point)[:3])
            for member,(a,b) in pairs.items():
                for step in range(1,division_counts[member]+1):
                    t = step/(division_counts[member]+1)
                    point = original[a]+(original[b]-original[a])*t
                    samples.setdefault(cell(point),[]).append((point,member,a,b,t))
        key = cell(points[vertex])
        choices = [(math.dist(tuple(points[vertex])[:3],tuple(point)[:3]),member,a,b,t)
                   for delta in itertools.product((-1,0,1),repeat=3)
                   for point,member,a,b,t in samples.get(tuple(x+y for x,y in zip(key,delta)),())]
        if not choices:
            raise SlideError("Bevel: не удалось связать отсчёт с исходным ребром.")
        error, edge, a, b, t = min(choices)
        if error > tolerance * 2:
            raise SlideError("Bevel: отсчёт разбиения сместился с исходного ребра.")
        membership[vertex].add(edge)
        limits[vertex] = {"blend":[[a,1-t],[b,t]],"rails":{**safe[a]["rails"],**safe[b]["rails"]}}
    prepared = [edge for edge in range(fn.numEdges)
                for a, b in (fn.getEdgeVertices(edge),)
                if membership[a] & membership[b]]
    return prepared, limits, max(scale * .005, 1e-6), nodes


def _native_corner_fill(mesh,edges):
    """Preserve a flat end cap when an independent bevel ends in a face fan."""
    fn=_mesh_fn(mesh);degree={}
    for edge in edges:
        for vertex in fn.getEdgeVertices(edge):degree[vertex]=degree.get(vertex,0)+1
    if any(value>1 for value in degree.values()):return True
    iterator=om.MItMeshVertex(fn.dagPath())
    for vertex in degree:
        iterator.setIndex(vertex);faces=iterator.getConnectedFaces()
        normals=[fn.getPolygonNormal(face).normal() for face in faces]
        if any(a*b>1-1e-7 for a,b in itertools.combinations(normals,2)):return False
    return True


def _native_sample(source, input_mesh, name):
    node = cmds.createNode("polyBevel3", name=name)
    cmds.connectAttr(input_mesh, node + ".inputPolymesh")
    components = cmds.getAttr(source + ".inputComponents")
    cmds.setAttr(node + ".inputComponents", len(components), *components, type="componentList")
    for attr in ("offset", "offsetAsFraction", "worldSpace", "mitering", "miterAlong",
                 "subdivideNgons", "smoothingAngle", "chamfer", "mergeVertices", "mergeVertexTolerance",
                 "maya2015", "maya2016SP3", "maya2017Update1", "maya2025"):
        cmds.setAttr(node + "." + attr, cmds.getAttr(source + "." + attr))
    _share_template_matrices(source,node)
    return node


def _split_reflex_quads(mesh, source, corners):
    """Split concave corner cells without changing the miter on other edges."""
    fn = _mesh_fn(mesh)
    points = fn.getPoints(om.MSpace.kWorld)
    original = [p * fn.dagPath().inclusiveMatrix() for p in source.getPoints()]
    faces = []
    for face in range(fn.numPolygons):
        ring = fn.getPolygonVertices(face)
        if len(ring) != 4 or len(set(ring)) != 4:
            continue
        first, second, third, fourth = (points[v] for v in ring)
        normal = ((second - first) ^ (third - first)) + ((third - first) ^ (fourth - first))
        sides = (second - first, third - second, fourth - third, first - fourth)
        tolerance = max(side * side for side in sides) ** 2 * 1e-6
        if not any(((a ^ b) * normal) < -tolerance for a, b in zip(sides, sides[1:] + sides[:1])):
            continue
        anchors = {min(range(len(original)), key=lambda i: (points[v] - original[i]).length()) for v in ring}
        if len(anchors) == 1 and anchors <= corners:
            faces.append(f"{mesh}.f[{face}]")
    if faces:
        cmds.polyTriangulate(faces, constructionHistory=True)


def _junction_sample(mesh, edges, reference, source, flat, rounded, *, sharp=False):
    ensure_junction_plugin()
    node = cmds.createNode("modelPackBevelJunctions", name="bevelJunctionTemplate#")
    if sharp:
        cmds.addAttr(node, longName="sharpJoin", attributeType="bool", defaultValue=True, hidden=True)
    for plug, attr in ((source, "sourceMesh"), (flat, "flatMesh"), (rounded, "roundMesh"),
                       (mesh + ".worldMatrix[0]", "meshMatrix")):
        cmds.connectAttr(plug, node + "." + attr)
    _set_string(node, "selectedEdges", edges)
    cmds.setAttr(node + ".referenceWidth", reference)
    return node


def _build(mesh, edges, segments, transverse_cuts=0, junction_fill=False, *,
           sharp=False, support_edges=0):
    prepared, limits, reference, subdivisions = _prepare_edges(mesh, edges, transverse_cuts)
    merge_tolerance = 0.
    radial = False
    if sharp:
        from slide_for_maya.multiple import _has_reflex_fold

        reference *= 24.
        fn = _mesh_fn(mesh)
        radial = _has_reflex_fold(fn, prepared)
        tolerance = _mesh_position_tolerance(fn, fn.getPoints(om.MSpace.kWorld))
        merge_tolerance = om.MDistance(tolerance * .5).asUnits(om.MDistance.uiUnit())
    offset = om.MDistance(reference).asUnits(om.MDistance.uiUnit()) if sharp else reference
    flat = cmds.polyBevel3([f"{mesh}.e[{e}]" for e in prepared], offset=offset,
                          offsetAsFraction=False, segments=2 * support_edges + 1 if sharp else 1,
                          depth=0, chamfer=not sharp,
                          worldSpace=True, constructionHistory=True, mergeVertices=sharp,
                          mergeVertexTolerance=merge_tolerance,
                          mitering=3 if radial else 0, smoothingAngle=60,
                          fillNgons=sharp or not junction_fill and _native_corner_fill(mesh,prepared))[0]
    incoming = cmds.listConnections(flat + ".inputPolymesh", s=True, d=False, plugs=True)[0]
    # Native bevel can offset in opposite directions depending on whether a
    # DAG mesh's triangulation has already been queried (notably on mirrored
    # inputs). A fresh duplicate and the displayed original need identical,
    # clean geometry data. The native identity pass preserves geometry, UVs
    # and normals without modifying the user's source mesh or its history.
    template_source = cmds.createNode("polyTweak", name="bevelTemplateSource#")
    cmds.setAttr(template_source+".tweak[0]",0,0,0,type="double3")
    cmds.connectAttr(incoming, template_source + ".inputPolymesh")
    normalized = template_source + ".output"
    cmds.connectAttr(normalized, flat + ".inputPolymesh", force=True)
    repairs = []
    if sharp:
        previous = set(cmds.listHistory(mesh) or [])
        rounded = flat
        templates = [flat]
        if support_edges:
            from slide_for_maya.multiple import _pinched_rings, _stitch_pinched_rows

            pinched = tuple(_pinched_rings(mesh, support_edges + 1))
            if pinched:
                _stitch_pinched_rows(mesh, flat, support_edges + 1, pinched)
        source = om.MFnMesh(om.MSelectionList().add(incoming).getPlug(0).asMObject())
        _split_reflex_quads(mesh, source, {v for edge in prepared for v in source.getEdgeVertices(edge)})
        repairs = list(dict.fromkeys(n for n in cmds.listHistory(mesh) or [] if n not in previous))
    else:
        rounded = _native_sample(flat, normalized, "bevelRoundTemplate#")
        cmds.setAttr(rounded + ".depth", 1)
        templates = [flat, rounded]
    final_flat = cmds.listConnections(_shape(mesh) + ".inMesh",s=True,d=False,plugs=True)[0] if repairs else flat + ".output"
    if radial:
        automatic = _native_sample(flat, normalized, "bevelAutoTemplate#")
        cmds.setAttr(automatic + ".mitering", 0)
        cmds.setAttr(automatic + ".depth", 0)
        cmds.setAttr(automatic + ".segments", 2 * support_edges + 1)
        seam = _junction_sample(mesh, prepared, reference, normalized, final_flat, automatic + ".output", sharp=True)
        repairs.extend((automatic, seam))
        final_flat = seam + ".output"
    custom = cmds.createNode(NODE_NAME, name="gestureBevel#")
    cmds.addAttr(custom,longName="baseMesh",dataType="mesh",hidden=True)
    cmds.addAttr(custom,longName="sessionMeta",dataType="string",hidden=True)
    cmds.addAttr(custom,longName="templateSource",attributeType="message",hidden=True)
    cmds.connectAttr(template_source+".message",custom+".templateSource")
    if repairs:
        _connect_topology_nodes(custom, [flat, *repairs])
    base = (cmds.listConnections(subdivisions[0]+".inputPolymesh",s=True,d=False,plugs=True)[0]
            if subdivisions else incoming)
    cmds.connectAttr(base,custom+".baseMesh")
    _set_string(custom,"sessionMeta",dict(edges=list(edges),cuts=transverse_cuts,junction_fill=bool(junction_fill),
                                          sharp=sharp,support_edges=support_edges,weld_contacts=sharp,preserve_rails=sharp,
                                          clean_template=sharp,local_width_limits=sharp,corner_grids=True,preserve_open_rim=True,
                                          round_supports=0 if sharp else support_edges,round_side_contacts=not sharp))
    cmds.setAttr(custom + ".referenceWidth", reference)
    segment_source = _segment_source(custom,*(() if sharp else (flat,rounded)),value=segments,
                                     supports=0 if sharp else support_edges)
    if not sharp and support_edges:
        repairs.append(_support_driver(flat))
        _connect_topology_nodes(custom, repairs)
    cmds.setAttr(custom + ".segmentCount", segments)
    _set_string(custom, "selectedEdges", prepared)
    _set_string(custom, "widthLimits", limits)
    cmds.connectAttr(normalized, custom + ".sourceMesh")
    cmds.connectAttr(final_flat, custom + ".flatMesh")
    cmds.connectAttr(final_flat if sharp else rounded + ".output", custom + ".roundMesh")
    cmds.connectAttr(mesh + ".worldMatrix[0]", custom + ".meshMatrix")
    junction=None
    if junction_fill and not sharp:
        junction = _junction_sample(mesh, prepared, reference, normalized, flat+".output", rounded+".output")
        for attr,output in (('junctionFlat','output'),('junctionRound','roundOutput')):
            cmds.addAttr(custom,longName=attr,dataType='mesh',hidden=True,storable=False);cmds.connectAttr(junction+'.'+output,custom+'.'+attr)
        cmds.addAttr(custom,longName='junctionBlends',dataType='string',hidden=True,storable=False)
        cmds.connectAttr(junction+'.pointBlends',custom+'.junctionBlends')
        cmds.addAttr(custom,longName='junctionSource',attributeType='message',hidden=True)
        cmds.connectAttr(junction+'.message',custom+'.junctionSource')
    cmds.connectAttr(custom + ".outputMesh", _shape(mesh) + ".inMesh", force=True)
    source = om.MFnMesh(om.MSelectionList().add(incoming).getPlug(0).asMObject())
    transform_selection = om.MSelectionList()
    transform_selection.add(mesh)
    matrix = transform_selection.getDagPath(0).inclusiveMatrix()
    graph = EdgeGraph([point * matrix for point in source.getPoints()],
                      ((e, source.getEdgeVertices(e)) for e in prepared))
    return custom, graph, subdivisions + templates + repairs + [n for n in (template_source, junction, custom, segment_source.split('.')[0]) if n]


def _topology_nodes(node):
    if cmds.attributeQuery("supportTopology",node=node,exists=True):
        return cmds.listConnections(node + ".supportTopology",s=True,d=False) or []
    return []


def _connect_topology_nodes(node, nodes):
    if cmds.attributeQuery("supportTopology",node=node,exists=True):
        for index in cmds.getAttr(node + ".supportTopology",multiIndices=True) or []:
            cmds.removeMultiInstance(f"{node}.supportTopology[{index}]",b=True)
    elif nodes:
        cmds.addAttr(node,longName="supportTopology",attributeType="message",hidden=True,multi=True)
    for index, source in enumerate(nodes):
        cmds.connectAttr(source + ".message", f"{node}.supportTopology[{index}]")


def edge_selection(mesh):
    """Keep Maya in edge component mode, including the visible preview."""
    cmds.selectMode(component=True)
    cmds.selectType(allComponents=False)
    cmds.selectType(polymeshEdge=True)
    cmds.hilite(mesh, replace=True)


def _resample_weights(graph, weights, new_graph, only_near=False):
    """Interpolate the existing width field when explicit cuts change vertex IDs."""
    if not weights:
        return {}
    result = {}
    for vertex in new_graph.neighbors:
        point = new_graph.points[vertex]
        nearest = None
        for a, b in graph.edges.values():
            first, second = graph.points[a], graph.points[b]
            direction = tuple(y-x for x,y in zip(first,second))
            squared = sum(d*d for d in direction)
            t = max(0,min(1,sum((p-x)*d for p,x,d in zip(point,first,direction))/squared))
            distance = sum((p-x-t*d)**2 for p,x,d in zip(point,first,direction))
            if nearest is None or distance < nearest[0]:
                nearest = distance, weights.get(a,1)*(1-t)+weights.get(b,1)*t
        result[vertex] = (nearest[1] if nearest is not None and
                          (not only_near or nearest[0]<1e-12) else 1)
    return result


class BevelSession:
    sharp = False
    support_edges = 0
    support_percent = DEFAULT_SUPPORT_PERCENT
    support_spacing = None

    def __init__(self, mesh=None, edges=None, *, segments=1, transverse_cuts=0, history_node=None,
                 keep_equal=True,all_sections=False,junction_fill=False,sharp=False,
                 support_edges=0,support_percent=DEFAULT_SUPPORT_PERCENT,support_spacing=None,
                 history_plan=None):
        if mesh is None:
            mesh, edges = _selected_edges()
        self.mesh = mesh
        self.edges = tuple(edges)
        self.selection = om.MGlobal.getActiveSelectionList()
        self.hilite = om.MGlobal.getHiliteList()
        self.visible = cmds.getAttr(mesh + ".visibility")
        self.width = 0.0
        self.shape = 0.0
        self.segments = max(1, min(64, int(segments)))
        self.transverse_cuts = max(0, min(64, int(transverse_cuts)))
        self.weights = {}
        self.keep_equal = bool(keep_equal)
        self.all_sections = bool(all_sections)
        self.junction_fill = bool(junction_fill)
        self.sharp = bool(sharp)
        self.preserve_open_rim = True
        self.support_edges = max(0, min(MAX_SUPPORT_EDGES, int(support_edges)))
        self.support_percent = self._support_value(support_percent) if support_spacing is None else None
        self.support_spacing = None if support_spacing is None else self._support_value(support_spacing)
        self.falloff_kind = "round"
        self.falloff_curve = Curve()
        self.profile_kind = "round"
        self.profile_curve = Curve(((0,0,None),(.5,.5,None),(1,0,None)), False)
        self.preview = None
        self.node = None
        self.history_nodes = []
        self.isolate_sets = []
        self.closed = False
        self._written = {}
        self._undo = []
        self._redo = []
        self._action_before = None
        self._display_mesh = None
        self.edit_node = history_node
        self.history_plan = history_plan
        self.width_only = bool(history_plan and history_plan.joined)
        self._base_input = None
        self.contact_faces = ()
        self._contact_marks = []
        if history_node:
            meta = (json.loads(cmds.getAttr(history_node+".sessionMeta"))
                    if cmds.attributeQuery("sessionMeta",node=history_node,exists=True) else
                    dict(edges=json.loads(cmds.getAttr(history_node+".selectedEdges")),cuts=0))
            self.edges = tuple(meta["edges"])
            self.transverse_cuts = meta["cuts"]
            self.keep_equal = meta.get("keep_equal",True)
            self.all_sections = meta.get("all_sections",False)
            self.sharp = meta.get("sharp", False)
            self.preserve_open_rim = meta.get("preserve_open_rim", False)
            self.support_edges = meta.get("support_edges", 0)
            self.support_spacing = meta.get("support_spacing")
            attr = "baseMesh" if cmds.attributeQuery("baseMesh",node=history_node,exists=True) else "sourceMesh"
            self._base_input = cmds.listConnections(history_node+"."+attr,s=True,d=False,plugs=True)[0]
            for name in ("width","shape","segments"):
                setattr(self,name,cmds.getAttr(history_node+"."+name))
            self.support_percent = meta.get("support_percent")
            if self.support_percent is None:
                self.support_percent = (100. * self.support_spacing / self.width
                                        if self.support_spacing is not None and self.width > 1e-9
                                        else DEFAULT_SUPPORT_PERCENT)
            self.weights = {int(k):v for k,v in json.loads(cmds.getAttr(history_node+".widthWeights")).items()}
            self.profile_kind = cmds.getAttr(history_node+".profileKind")
            self.falloff_kind = cmds.getAttr(history_node+".falloffKind")
            self.profile_curve = Curve(tuple(tuple(p) for p in json.loads(cmds.getAttr(history_node+".profileCurve"))),False)
            self.falloff_curve = Curve(tuple(tuple(p) for p in json.loads(cmds.getAttr(history_node+".falloffCurve"))))
            if history_plan is not None:
                self.edges = history_plan.edges
                if history_plan.joined:
                    self.width = history_plan.width
        self.edges = self._filter_edges(self.edges)
        ensure_plugin()
        points = _mesh_fn(mesh).getPoints(om.MSpace.kWorld)
        spans = [max(p[a] for p in points)-min(p[a] for p in points) for a in range(3)]
        self.motion_scale = min((v for v in spans if v>1e-8),default=1)*.005
        try:
            with without_undo():
                self._create_preview()
                for panel in cmds.getPanel(type="modelPanel") or []:
                    if cmds.isolateSelect(panel, q=True, state=True):
                        group = cmds.isolateSelect(panel, q=True, viewObjects=True)
                        if group:
                            cmds.sets(self.preview, add=group)
                            self.isolate_sets.append(group)
                edge_selection(self.mesh)
                om.MGlobal.setActiveSelectionList(self.selection)
                if history_node:
                    if history_plan is not None:
                        self.weights = history_plan.weights(self.graph)
                    self.update()
                    if history_plan is not None:
                        history_plan.original = self.snapshot()
        except Exception:
            self.cancel()
            raise

    def _base_copy(self,name, source_plug=None):
        mesh = cmds.duplicate(self.mesh, returnRootsOnly=True, name=name, transformsOnly=bool(source_plug))[0]
        if source_plug or self._base_input:
            selection = om.MSelectionList(); selection.add(source_plug or self._base_input)
            source = selection.getPlug(0).asMObject()
            shapes = cmds.listRelatives(mesh,shapes=True,fullPath=True) or []
            groups = cmds.listConnections(_shape(self.mesh),type="shadingEngine") or []
            if shapes:
                cmds.delete(shapes)
            selection = om.MSelectionList(); selection.add(mesh)
            om.MFnMesh().copy(source,selection.getDependNode(0))
            if groups:
                cmds.sets(mesh,e=True,forceElement=groups[0])
        return mesh

    def _filter_edges(self, edges):
        if self._base_input:
            mesh = om.MSelectionList().add(self._base_input).getPlug(0).asMObject()
        else:
            mesh = _mesh_fn(self.mesh).object()
        return _interior_edges(mesh, edges)

    def _create_preview(self):
        self.preview = self._base_copy("bevelPreview#")
        cmds.setAttr(self.preview + ".visibility", False)
        if not self.edges:
            self.node = None
            self.history_nodes = []
            self.graph = EdgeGraph(_mesh_fn(self.preview).getPoints(om.MSpace.kWorld),())
            self.max_width = math.inf
            return
        self.node, self.graph, self.history_nodes = _build(
            self.preview, self.edges, self.segments, self.transverse_cuts, self.junction_fill,
            sharp=self.sharp, support_edges=self._support_count())
        # _build already set this connected attribute. Rewriting it after the
        # initial evaluation would rebuild both native topology templates.
        self._written[self.node] = {"segments":self.segments}
        _mesh_fn(self.preview).getPoints()
        selection = om.MSelectionList()
        selection.add(self.node)
        self.max_width = om.MFnDependencyNode(selection.getDependNode(0)).userNode()._template.uniform_limit

    def _rebuild_preview(self):
        self._remove_preview(restore_selection=False)
        with without_undo():
            self._create_preview()
            for group in self.isolate_sets:
                if cmds.objExists(group):
                    cmds.sets(self.preview, add=group)
            om.MGlobal.setActiveSelectionList(self.selection)

    def snapshot(self):
        return {name: dict(self.weights) if name == "weights" else getattr(self,name)
                for name in ("width","shape","segments","transverse_cuts","weights",
                             "falloff_kind","falloff_curve","profile_kind","profile_curve","edges",
                             "keep_equal","all_sections","sharp","support_edges","support_percent","support_spacing")}

    def begin_action(self):
        if self._action_before is None:
            self._action_before = self.snapshot()

    def end_action(self):
        before, self._action_before = self._action_before, None
        if before is not None and before != self.snapshot():
            self._undo.append(before)
            self._redo.clear()

    @contextmanager
    def action(self):
        owned = self._action_before is None
        self.begin_action()
        try:
            yield
        finally:
            if owned:
                self.end_action()

    def _restore(self, state):
        count = self._support_count()
        rebuild = any(state[name] != getattr(self, name)
                      for name in ("transverse_cuts", "edges", "sharp"))
        for name,value in state.items():
            setattr(self,name,dict(value) if name == "weights" else value)
        if rebuild or self._support_count() != count:
            self._rebuild_preview()
        self.update()

    def change_edges(self,edges):
        if self.width_only:
            return
        requested = tuple(sorted(set(edges)))
        edges = self._filter_edges(requested)
        if requested and not edges:
            return
        if edges == self.edges:
            return
        # An empty selection is a valid editable state; do not build an empty
        # polyBevel command (Maya interprets it as beveling the whole object).
        with self.action():
            previous = self.snapshot()
            graph,weights = self.graph,dict(self.weights)
            self.edges = edges
            try:
                self._rebuild_preview()
                self.weights = _resample_weights(graph,weights,self.graph,only_near=True)
                self.update()
            except Exception:
                self._restore(previous)
                raise

    def undo(self):
        self.end_action()
        if not self._undo:
            return False
        self._redo.append(self.snapshot())
        self._restore(self._undo.pop())
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append(self.snapshot())
        self._restore(self._redo.pop())
        return True

    def add_transverse_cut(self):
        self.change_transverse_cuts(1)

    def remove_transverse_cut(self):
        self.change_transverse_cuts(-1)

    def change_transverse_cuts(self,delta):
        if self.width_only:
            return
        count = max(0,min(64,self.transverse_cuts+delta))
        if count == self.transverse_cuts:
            return
        with self.action():
            graph, weights = self.graph, dict(self.weights)
            self.transverse_cuts = count
            self._rebuild_preview()
            self.weights = _resample_weights(graph,weights,self.graph)
            self.update()

    def _write(self, node):
        if node is None:
            return
        width, weights = (self.history_plan.width_field(self.width, self.weights)
                          if self.width_only else (self.width, self.weights))
        numeric = dict(width=width,shape=self.shape,segments=self.segments)
        strings = dict(widthWeights=json.dumps(weights),profileKind=self.profile_kind,
                       profileCurve=json.dumps(self.profile_curve.data()),falloffKind=self.falloff_kind,
                       falloffCurve=json.dumps(self.falloff_curve.data()),
                       sessionMeta=json.dumps(dict(edges=list(self.edges),cuts=self.transverse_cuts,
                                                   keep_equal=self.keep_equal,all_sections=self.all_sections,
                                                   junction_fill=self.junction_fill,sharp=self.sharp,
                                                   support_edges=self.support_edges,support_percent=self.support_percent,
                                                   support_spacing=self.support_spacing,weld_contacts=self.sharp,
                                                   preserve_rails=self.sharp,clean_template=self.sharp,
                                                   local_width_limits=self.sharp,corner_grids=True,
                                                   preserve_open_rim=self.preserve_open_rim,
                                                   round_supports=0 if self.sharp else self._support_count(),
                                                   round_side_contacts=not self.sharp)))
        previous = self._written.setdefault(node,{})
        for attr,value in {**numeric,**strings}.items():
            if previous.get(attr) == value:
                continue
            if attr in strings:
                _set_string(node,attr,value)
            else:
                cmds.setAttr(node+"."+("segmentCount" if attr == "segments" else attr),value)
            previous[attr] = value

    @staticmethod
    def _support_value(value):
        if not math.isfinite(value) or value < 0.:
            raise SlideError("Шаг опорных рёбер должен быть конечным неотрицательным числом.")
        return float(value)

    def _support_count(self):
        step = self.support_spacing if self.support_spacing is not None else self.support_percent
        return self.support_edges if step else 0

    @property
    def display_support_percent(self):
        if self.support_spacing is not None and self.width > 1e-9:
            return 100. * self.support_spacing / self.width
        return DEFAULT_SUPPORT_PERCENT if self.support_percent is None else self.support_percent

    def confirm_support_spacing(self):
        if self.width_only:
            return
        if self._support_count() and self.width > 1e-9 and self.support_spacing is None:
            self.support_spacing = self.width * (self.support_percent / 100.)

    def change_supports(self, *, sharp=None, support_edges=None, support_percent=None, support_spacing=None):
        if self.width_only:
            raise SlideError('Опорные рёбра задаются общей операцией связанных фасок.')
        sharp = self.sharp if sharp is None else bool(sharp)
        count = self.support_edges if support_edges is None else max(0, min(MAX_SUPPORT_EDGES, int(support_edges)))
        percent = self.support_percent if support_percent is None else self._support_value(support_percent)
        spacing = self.support_spacing
        if support_spacing is not None:
            spacing = self._support_value(support_spacing)
            percent = 100. * spacing / self.width if self.width > 1e-9 else None
        elif support_percent is not None or not count:
            spacing = None
            if percent is None:
                percent = DEFAULT_SUPPORT_PERCENT
        with self.action():
            previous = self.snapshot()
            previous_count = self._support_count()
            self.sharp, self.support_edges, self.support_percent, self.support_spacing = sharp, count, percent, spacing
            try:
                if sharp != previous["sharp"] or self._support_count() != previous_count:
                    self._rebuild_preview()
                if support_percent is not None or not previous_count:
                    self.confirm_support_spacing()
                self.update()
            except Exception:
                self._restore(previous)
                raise

    def update(self, *, width=None, shape=None, segments=None):
        if self.closed:
            raise SlideError("Сессия фаски уже завершена.")
        if self.width_only and (shape not in (None, self.shape) or segments not in (None, self.segments)):
            raise SlideError('Форма и сегменты задаются общей операцией связанных фасок.')
        if width is not None:
            if not math.isfinite(width):
                raise SlideError("Ширина должна быть конечным числом.")
            self.width = max(0, min(width,self.max_width) if self.keep_equal and not self.sharp and not self.width_only else width)
        elif self.keep_equal and not self.sharp and not self.width_only:
            self.width = min(self.width,self.max_width)
        if self.support_percent is None and self.width > 1e-9:
            self.support_percent = 100. * self.support_spacing / self.width
        if shape is not None:
            self.shape = max(-1, min(1, shape))
        if segments is not None:
            self.segments = max(1, min(64, int(segments)))
        with without_undo():
            self._write(self.node)
            if self.node and (self.width > 1e-9 or self.width_only):
                _mesh_fn(self.preview).getPoints()
                selection = om.MSelectionList(); selection.add(self.node)
                template = om.MFnDependencyNode(selection.getDependNode(0)).userNode()._template
                effective = template.reported_width
                self.contact_faces = template.contact_faces if template.at_contact else ()
                if not self.width_only and effective < self.width:
                    self.width = effective
                    self._write(self.node)
                if not self.width_only and template.effective_weights is not None:
                    self.weights = dict(template.effective_weights)
                    self._write(self.node)
            else:
                self.contact_faces = ()
            self._mark_contact()
            show = (self.width > 1e-9 or bool(self.edit_node)) and bool(self.edges)
            cmds.setAttr(self.mesh + ".visibility", self.visible and not show)
            display = self.preview if show else self.mesh
            if self.history_plan is not None:
                display = self.history_plan.preview(self, display)
            cmds.setAttr(self.preview + ".visibility", self.visible and display == self.preview and show)
            edge_selection(display)
            if display != self._display_mesh:
                om.MGlobal.setActiveSelectionList(self.selection if display == self.mesh else om.MSelectionList())
            self._display_mesh = display
        return self.width

    def _mark_contact(self):
        selection = om.MGlobal.getActiveSelectionList()
        for mark in self._contact_marks:
            if cmds.objExists(mark):
                cmds.delete(mark)
        self._contact_marks = []
        if not self.contact_faces:
            om.MGlobal.setActiveSelectionList(selection)
            return
        fn = _mesh_fn(self.preview)
        points = fn.getPoints()
        for face in set(self.contact_faces):
            ids = fn.getPolygonVertices(face)
            outline = [tuple(points[i])[:3] for i in (*ids,ids[0])]
            mark = cmds.curve(degree=1,point=outline,name="bevelContact#")
            mark = cmds.parent(mark,self.preview,relative=True)[0]
            shape = cmds.listRelatives(mark,shapes=True)[0]
            cmds.setAttr(shape+".overrideEnabled",True)
            cmds.setAttr(shape+".overrideRGBColors",True)
            cmds.setAttr(shape+".overrideColorRGB",1,.12,.12,type="double3")
            cmds.setAttr(shape+".overrideDisplayType",2)
            cmds.setAttr(shape+".lineWidth",3)
            self._contact_marks.append(mark)
        om.MGlobal.setActiveSelectionList(selection)

    def influence(self, edge, fraction):
        return self.graph.influence(edge, fraction, self.falloff_kind, self.falloff_curve,self.all_sections)

    def local_width(self, delta, influence, initial_weights=None):
        if self.width <= 1e-9:
            raise SlideError("Сначала задайте общую ширину фаски.")
        previous = self.weights if initial_weights is None else initial_weights
        self.weights = {v: max(0, previous.get(v, 1) + delta * influence.get(v, 0) / self.width)
                        for v in self.graph.neighbors}
        self.update()

    def _remove_preview(self, restore_selection=True):
        # Maya may reuse the preview node's name for the final history node.
        # Its values must be written anew, even if the name is identical.
        self._written.pop(self.node,None)
        with without_undo():
            if self.history_plan is not None:
                self.history_plan.clear()
            for group in self.isolate_sets:
                if cmds.objExists(group) and self.preview and cmds.objExists(self.preview):
                    cmds.sets(self.preview, remove=group)
            existing = [node for node in self.history_nodes if cmds.objExists(node)]
            # Remove the preview and all of its private history together.
            if self.preview and cmds.objExists(self.preview):
                existing.append(self.preview)
            if existing:
                cmds.delete(existing)
            if cmds.objExists(self.mesh):
                cmds.setAttr(self.mesh + ".visibility", self.visible)
            if restore_selection:
                if cmds.objExists(self.mesh):
                    edge_selection(self.mesh)
                om.MGlobal.setActiveSelectionList(self.selection)
        self.preview = None
        self._contact_marks = []
        self.history_nodes = []
        self._display_mesh = None

    def cancel(self, restore_selection=True):
        self._remove_preview(restore_selection)
        self.closed = True

    def commit(self):
        if self.history_plan is not None:
            if self.history_plan.unchanged(self):
                self.cancel()
                return self.edit_node
            if self.history_plan.blocked:
                raise SlideError(self.history_plan.blocked)
        if (self.width <= 1e-9 or not self.edges) and not self.edit_node:
            self.cancel()
            return None
        self.confirm_support_spacing()
        self._remove_preview()
        cmds.undoInfo(openChunk=True, chunkName="Bevel")
        try:
            removed = self.edges if self.edit_node and self.width == 0 and not self.width_only else ()
            if removed:
                # Restore the unsplit base mesh only after leaving the live
                # preview. Do not retain zero-width bevel/template vertices.
                self.edges = ()
            if self.history_plan is not None:
                node = self.history_plan.commit(self)
            elif self.edit_node:
                node = self._replace_history()
            else:
                node, _, _ = _build(self.mesh, self.edges, self.segments, self.transverse_cuts, self.junction_fill,
                                    sharp=self.sharp, support_edges=self._support_count())
            self._write(node)
            if removed and self.history_plan is None:
                cmds.polySoftEdge([f"{self.mesh}.e[{e}]" for e in removed],angle=0,constructionHistory=True)
            cmds.select(clear=True)
            edge_selection(self.mesh)
        except Exception:
            cmds.undoInfo(closeChunk=True)
            cmds.undo()
            self.closed = True
            raise
        cmds.undoInfo(closeChunk=True)
        self.closed = True
        return node

    def _replace_history(self):
        """Replace templates inside one undo chunk, retaining the saved node."""
        replacement = None
        if self.edges:
            stage = self._base_copy("bevelEditStage#")
            temporary,_,nodes = _build(stage,self.edges,self.segments,self.transverse_cuts,self.junction_fill,
                                       sharp=self.sharp,support_edges=self._support_count())
            replacement = stage, temporary, nodes
        self._replace_templates(self.edit_node, self._base_input, replacement,
                                sharp=self.sharp, supports=self._support_count())
        return self.edit_node

    def _replace_templates(self, node, base, replacement, *, sharp, supports):
        base_attr = 'baseMesh' if cmds.attributeQuery('baseMesh', node=node, exists=True) else 'sourceMesh'
        previous_base = cmds.connectionInfo(node + '.' + base_attr, sourceFromDestination=True)
        old = _topology_nodes(node)
        for attr in ("flatMesh","roundMesh"):
            old.extend(cmds.listConnections(node+"."+attr,s=True,d=False,type="polyBevel3") or [])
        normalizers = (cmds.listConnections(node+".templateSource",s=True,d=False,type="polyTweak") or []
                       if cmds.attributeQuery("templateSource",node=node,exists=True) else [])
        old.extend(normalizers)
        if cmds.attributeQuery('junctionSource',node=node,exists=True):
            old.extend(cmds.listConnections(node+'.junctionSource',s=True,d=False,type='modelPackBevelJunctions') or [])
        source = cmds.listConnections((normalizers[0]+".inputPolymesh") if normalizers else (node+".sourceMesh"),
                                      s=True,d=False,plugs=True)[0]
        while source != previous_base:
            owner = source.split(".")[0]
            if cmds.nodeType(owner) != "polySubdEdge":
                break
            old.append(owner)
            source = cmds.listConnections(owner+".inputPolymesh",s=True,d=False,plugs=True)[0]
        if replacement:
            stage, temporary, nodes = replacement
            native = [n for n in nodes if cmds.nodeType(n)=="polyBevel3"]
            subdivisions = [n for n in nodes if cmds.nodeType(n)=="polySubdEdge"]
            template_source = cmds.listConnections(temporary+".templateSource",s=True,d=False)[0]
            destination = (subdivisions[0] if subdivisions else template_source) + ".inputPolymesh"
            if not cmds.isConnected(base,destination):
                cmds.connectAttr(base,destination,force=True)
            if not cmds.attributeQuery("templateSource",node=node,exists=True):
                cmds.addAttr(node,longName="templateSource",attributeType="message",hidden=True)
            cmds.connectAttr(template_source+".message",node+".templateSource",force=True)
            topology = _topology_nodes(temporary)
            _connect_topology_nodes(node, topology)
            for sample in topology:
                if cmds.attributeQuery("sharpJoin", node=sample, exists=True):
                    cmds.connectAttr(self.mesh+".worldMatrix[0]", sample+".meshMatrix", force=True)
            for attr in ("sourceMesh","flatMesh","roundMesh"):
                plug = cmds.listConnections(temporary+"."+attr,s=True,d=False,plugs=True)[0]
                if not cmds.isConnected(plug,node+"."+attr):
                    cmds.connectAttr(plug,node+"."+attr,force=True)
            for attr in ('junctionFlat','junctionRound','junctionBlends','junctionSource'):
                sources=cmds.listConnections(temporary+'.'+attr,s=True,d=False,plugs=True) if cmds.attributeQuery(attr,node=temporary,exists=True) else []
                if not sources:
                    if cmds.attributeQuery(attr,node=node,exists=True):
                        for plug in cmds.listConnections(node+'.'+attr,s=True,d=False,plugs=True) or []:cmds.disconnectAttr(plug,node+'.'+attr)
                    continue
                if not cmds.attributeQuery(attr,node=node,exists=True):
                    if attr=='junctionSource':cmds.addAttr(node,longName=attr,attributeType='message',hidden=True)
                    else:cmds.addAttr(node,longName=attr,dataType='string' if attr=='junctionBlends' else 'mesh',hidden=True,storable=False)
                plug=sources[0]
                cmds.connectAttr(plug,node+'.'+attr,force=True)
            for attr in ("selectedEdges","widthLimits"):
                _set_string(node,attr,cmds.getAttr(temporary+"."+attr))
            cmds.setAttr(node+".referenceWidth",cmds.getAttr(temporary+".referenceWidth"))
            # Keep the saved node's parameter source and discard the staging
            # source with its temporary node. No template drives back upstream.
            _segment_source(node, *(() if sharp else native), supports=0 if sharp else supports)
            parameters = cmds.listConnections(temporary+".segments",s=True,d=False,type="network") or []
            cmds.delete([temporary,stage]+parameters)
        else:
            _connect_topology_nodes(node, [])
            if normalizers:
                cmds.disconnectAttr(normalizers[0]+".message",node+".templateSource")
            cmds.connectAttr(base,node+".sourceMesh",force=True)
            for attr in ("flatMesh","roundMesh"):
                cmds.connectAttr(base,node+"."+attr,force=True)
            for attr in ('junctionFlat','junctionRound','junctionBlends','junctionSource'):
                if cmds.attributeQuery(attr,node=node,exists=True):
                    for source in cmds.listConnections(node+'.'+attr,s=True,d=False,plugs=True) or []:cmds.disconnectAttr(source,node+'.'+attr)
            _set_string(node,"selectedEdges",[])
            _set_string(node,"widthLimits",{})
        if not cmds.attributeQuery("baseMesh",node=node,exists=True):
            cmds.addAttr(node,longName="baseMesh",dataType="mesh",hidden=True)
        if not cmds.isConnected(base,node+".baseMesh"):
            cmds.connectAttr(base,node+".baseMesh",force=True)
        if not cmds.attributeQuery("sessionMeta",node=node,exists=True):
            cmds.addAttr(node,longName="sessionMeta",dataType="string",hidden=True)
        # Delete only templates exclusively owned by this history, preserving
        # upstream source nodes and any externally shared outputs.
        owned = {n for n in old if cmds.objExists(n)}
        retained = {n for n in owned if set(cmds.listConnections(n,s=False,d=True) or []) - owned}
        while retained:
            owned.difference_update(retained)
            retained = {n for n in owned if set(cmds.listConnections(n,s=False,d=True) or []) - owned}
        if owned:
            cmds.delete(sorted(owned))


def apply(width, *, segments=1, shape=0, local=None, transverse_cuts=0, sharp=False,
          support_edges=0, support_percent=DEFAULT_SUPPORT_PERCENT, support_spacing=None, keep_equal=True):
    session = BevelSession(segments=segments, transverse_cuts=transverse_cuts, sharp=sharp,
                           support_edges=support_edges, support_percent=support_percent,
                           support_spacing=support_spacing, keep_equal=keep_equal)
    try:
        session.update(width=width, shape=shape)
        for edge, fraction, delta in local or ():
            session.local_width(delta, session.influence(edge, fraction))
        return session.commit()
    except Exception:
        if not session.closed:
            session.cancel()
        raise
