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
    implementation = importlib.import_module("model_pack_bevel.model_pack_gesture_bevel")
    importlib.reload(implementation)
    with without_undo():
        probe = cmds.createNode(NODE_NAME)
        try:
            selection = om.MSelectionList()
            selection.add(probe)
            cls = type(om.MFnDependencyNode(selection.getDependNode(0)).userNode())
            cls.compute = implementation.GestureBevelNode.compute
            cls.setDependentsDirty = implementation.GestureBevelNode.setDependentsDirty
            for node in cmds.ls(type=NODE_NAME) or []:
                selection = om.MSelectionList()
                selection.add(node)
                instance = om.MFnDependencyNode(selection.getDependNode(0)).userNode()
                # Older histories copied only connected matrices. polyBevel3
                # usually stores inputMatrix as a constant, so their round
                # template evaluated an identity transform on scaled meshes.
                flat = cmds.listConnections(node+".flatMesh",s=True,d=False,type="polyBevel3") or []
                rounded = cmds.listConnections(node+".roundMesh",s=True,d=False,type="polyBevel3") or []
                if flat and rounded:
                    _share_template_matrices(flat[0],rounded[0])
                instance._template = instance._key = instance._limits = None
                cmds.dgdirty(node)
        finally:
            cmds.delete(probe)
    _PLUGIN_REFRESHED = True


def _shape(mesh):
    return cmds.listRelatives(mesh,shapes=True,noIntermediate=True,fullPath=True)[0]


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


def _prepare_edges(mesh, edges, transverse_cuts=0):
    """Subdivide only when requested; preserve the input edge layout by default."""
    fn = _mesh_fn(mesh)
    original = fn.getPoints(om.MSpace.kWorld)
    pairs = {e: fn.getEdgeVertices(e) for e in edges}
    graph = EdgeGraph(original, pairs.items())
    polygon = om.MItMeshEdge(fn.dagPath())
    for edge in edges:
        polygon.setIndex(edge)
        if len(polygon.getConnectedFaces()) != 2:
            raise SlideError("Bevel: выберите внутренние рёбра; открытые границы пока не поддерживаются.")
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


def _build(mesh, edges, segments, transverse_cuts=0):
    prepared, limits, reference, subdivisions = _prepare_edges(mesh, edges, transverse_cuts)
    flat = cmds.polyBevel3([f"{mesh}.e[{e}]" for e in prepared], offset=reference,
                          offsetAsFraction=False, segments=1, depth=0,
                          worldSpace=True, constructionHistory=True, mergeVertices=False,
                          mitering=0, smoothingAngle=30, fillNgons=True)[0]
    rounded = cmds.createNode("polyBevel3", name="bevelRoundTemplate#")
    incoming = cmds.listConnections(flat + ".inputPolymesh", s=True, d=False, plugs=True)[0]
    cmds.connectAttr(incoming, rounded + ".inputPolymesh")
    components = cmds.getAttr(flat + ".inputComponents")
    cmds.setAttr(rounded + ".inputComponents", len(components), *components, type="componentList")
    for attr in ("offset", "offsetAsFraction", "worldSpace", "mitering", "miterAlong", "subdivideNgons",
                 "maya2015", "maya2016SP3", "maya2017Update1", "maya2025"):
        cmds.setAttr(rounded + "." + attr, cmds.getAttr(flat + "." + attr))
    cmds.setAttr(rounded + ".depth", 1)
    _share_template_matrices(flat,rounded)
    custom = cmds.createNode(NODE_NAME, name="gestureBevel#")
    cmds.addAttr(custom,longName="baseMesh",dataType="mesh",hidden=True)
    cmds.addAttr(custom,longName="sessionMeta",dataType="string",hidden=True)
    base = (cmds.listConnections(subdivisions[0]+".inputPolymesh",s=True,d=False,plugs=True)[0]
            if subdivisions else incoming)
    cmds.connectAttr(base,custom+".baseMesh")
    _set_string(custom,"sessionMeta",dict(edges=list(edges),cuts=transverse_cuts))
    cmds.setAttr(custom + ".referenceWidth", reference)
    cmds.setAttr(custom + ".segments", segments)
    _set_string(custom, "selectedEdges", prepared)
    _set_string(custom, "widthLimits", limits)
    cmds.connectAttr(incoming, custom + ".sourceMesh")
    cmds.connectAttr(flat + ".output", custom + ".flatMesh")
    cmds.connectAttr(rounded + ".output", custom + ".roundMesh")
    cmds.connectAttr(custom + ".segments", flat + ".segments", force=True)
    cmds.connectAttr(custom + ".segments", rounded + ".segments", force=True)
    cmds.connectAttr(mesh + ".worldMatrix[0]", custom + ".meshMatrix")
    cmds.connectAttr(custom + ".outputMesh", _shape(mesh) + ".inMesh", force=True)
    selection = om.MSelectionList()
    selection.add(incoming)
    source = om.MFnMesh(selection.getPlug(0).asMObject())
    transform_selection = om.MSelectionList()
    transform_selection.add(mesh)
    matrix = transform_selection.getDagPath(0).inclusiveMatrix()
    graph = EdgeGraph([point * matrix for point in source.getPoints()],
                      ((e, source.getEdgeVertices(e)) for e in prepared))
    return custom, graph, subdivisions + [flat, rounded, custom]


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
    def __init__(self, mesh=None, edges=None, *, segments=1, transverse_cuts=0, history_node=None,
                 keep_equal=True,all_sections=False):
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
            attr = "baseMesh" if cmds.attributeQuery("baseMesh",node=history_node,exists=True) else "sourceMesh"
            self._base_input = cmds.listConnections(history_node+"."+attr,s=True,d=False,plugs=True)[0]
            for name in ("width","shape","segments"):
                setattr(self,name,cmds.getAttr(history_node+"."+name))
            self.weights = {int(k):v for k,v in json.loads(cmds.getAttr(history_node+".widthWeights")).items()}
            self.profile_kind = cmds.getAttr(history_node+".profileKind")
            self.falloff_kind = cmds.getAttr(history_node+".falloffKind")
            self.profile_curve = Curve(tuple(tuple(p) for p in json.loads(cmds.getAttr(history_node+".profileCurve"))),False)
            self.falloff_curve = Curve(tuple(tuple(p) for p in json.loads(cmds.getAttr(history_node+".falloffCurve"))))
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
                om.MGlobal.setActiveSelectionList(self.selection)
                edge_selection(self.mesh)
                if history_node:
                    self.update()
        except Exception:
            self.cancel()
            raise

    def _base_copy(self,name):
        mesh = cmds.duplicate(self.mesh,returnRootsOnly=True,name=name)[0]
        if self._base_input:
            selection = om.MSelectionList(); selection.add(self._base_input)
            source = selection.getPlug(0).asMObject()
            shapes = cmds.listRelatives(mesh,shapes=True,fullPath=True) or []
            groups = cmds.listConnections(_shape(self.mesh),type="shadingEngine") or []
            cmds.delete(shapes)
            selection = om.MSelectionList(); selection.add(mesh)
            om.MFnMesh().copy(source,selection.getDependNode(0))
            if groups:
                cmds.sets(mesh,e=True,forceElement=groups[0])
        return mesh

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
            self.preview, self.edges, self.segments, self.transverse_cuts)
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
                             "keep_equal","all_sections")}

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
        rebuild = state["transverse_cuts"] != self.transverse_cuts or state["edges"] != self.edges
        for name,value in state.items():
            setattr(self,name,dict(value) if name == "weights" else value)
        if rebuild:
            self._rebuild_preview()
        self.update()

    def change_edges(self,edges):
        edges = tuple(sorted(set(edges)))
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
        numeric = dict(width=self.width,shape=self.shape,segments=self.segments)
        strings = dict(widthWeights=json.dumps(self.weights),profileKind=self.profile_kind,
                       profileCurve=json.dumps(self.profile_curve.data()),falloffKind=self.falloff_kind,
                       falloffCurve=json.dumps(self.falloff_curve.data()),
                       sessionMeta=json.dumps(dict(edges=list(self.edges),cuts=self.transverse_cuts,
                                                   keep_equal=self.keep_equal,all_sections=self.all_sections)))
        previous = self._written.setdefault(node,{})
        for attr,value in {**numeric,**strings}.items():
            if previous.get(attr) == value:
                continue
            if attr in strings:
                _set_string(node,attr,value)
            else:
                cmds.setAttr(node+"."+attr,value)
            previous[attr] = value

    def update(self, *, width=None, shape=None, segments=None):
        if self.closed:
            raise SlideError("Сессия фаски уже завершена.")
        if width is not None:
            if not math.isfinite(width):
                raise SlideError("Ширина должна быть конечным числом.")
            self.width = max(0, min(width,self.max_width) if self.keep_equal else width)
        elif self.keep_equal:
            self.width = min(self.width,self.max_width)
        if shape is not None:
            self.shape = max(-1, min(1, shape))
            if abs(self.shape) > 1e-6 and self.segments == 1 and segments is None:
                self.segments = 3
        if segments is not None:
            self.segments = max(1, min(64, int(segments)))
        with without_undo():
            self._write(self.node)
            if self.node and self.width>1e-9:
                _mesh_fn(self.preview).getPoints()
                selection = om.MSelectionList(); selection.add(self.node)
                template = om.MFnDependencyNode(selection.getDependNode(0)).userNode()._template
                effective = template.effective_width
                self.contact_faces = template.contact_faces if template.at_contact else ()
                if effective<self.width:
                    self.width = effective
                    self._write(self.node)
                if template.effective_weights is not None:
                    self.weights = dict(template.effective_weights)
                    self._write(self.node)
            else:
                self.contact_faces = ()
            self._mark_contact()
            show = self.width > 1e-9 and bool(self.edges)
            cmds.setAttr(self.mesh + ".visibility", self.visible and not show)
            cmds.setAttr(self.preview + ".visibility", self.visible and show)
            display = self.preview if show else self.mesh
            edge_selection(display)
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
            for group in self.isolate_sets:
                if cmds.objExists(group) and self.preview and cmds.objExists(self.preview):
                    cmds.sets(self.preview, remove=group)
            if self.preview and cmds.objExists(self.preview):
                cmds.delete(self.preview)
            existing = [node for node in self.history_nodes if cmds.objExists(node)]
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
        if (self.width <= 1e-9 or not self.edges) and not self.edit_node:
            self.cancel()
            return None
        self._remove_preview()
        cmds.undoInfo(openChunk=True, chunkName="Bevel")
        try:
            if self.edit_node:
                node = self._replace_history()
            else:
                node, _, _ = _build(self.mesh, self.edges, self.segments, self.transverse_cuts)
            self._write(node)
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
        node = self.edit_node
        old = []
        for attr in ("flatMesh","roundMesh"):
            old.extend(cmds.listConnections(node+"."+attr,s=True,d=False,type="polyBevel3") or [])
        source = cmds.listConnections(node+".sourceMesh",s=True,d=False,plugs=True)[0]
        while source != self._base_input:
            owner = source.split(".")[0]
            if cmds.nodeType(owner) != "polySubdEdge":
                break
            old.append(owner)
            source = cmds.listConnections(owner+".inputPolymesh",s=True,d=False,plugs=True)[0]
        if self.edges:
            stage = self._base_copy("bevelEditStage#")
            temporary,_,nodes = _build(stage,self.edges,self.segments,self.transverse_cuts)
            native = [n for n in nodes if cmds.nodeType(n)=="polyBevel3"]
            subdivisions = [n for n in nodes if cmds.nodeType(n)=="polySubdEdge"]
            if subdivisions:
                cmds.connectAttr(self._base_input,subdivisions[0]+".inputPolymesh",force=True)
            else:
                for n in native:
                    cmds.connectAttr(self._base_input,n+".inputPolymesh",force=True)
                cmds.connectAttr(self._base_input,temporary+".sourceMesh",force=True)
            for attr in ("sourceMesh","flatMesh","roundMesh"):
                plug = cmds.listConnections(temporary+"."+attr,s=True,d=False,plugs=True)[0]
                if not cmds.isConnected(plug,node+"."+attr):
                    cmds.connectAttr(plug,node+"."+attr,force=True)
            for attr in ("selectedEdges","widthLimits"):
                _set_string(node,attr,cmds.getAttr(temporary+"."+attr))
            cmds.setAttr(node+".referenceWidth",cmds.getAttr(temporary+".referenceWidth"))
            # These attributes were driven by the temporary staging node.
            for n in native:
                cmds.connectAttr(node+".segments",n+".segments",force=True)
            cmds.delete(temporary,stage)
        else:
            cmds.connectAttr(self._base_input,node+".sourceMesh",force=True)
            for attr in ("flatMesh","roundMesh"):
                cmds.connectAttr(self._base_input,node+"."+attr,force=True)
            _set_string(node,"selectedEdges",[])
            _set_string(node,"widthLimits",{})
        if not cmds.attributeQuery("baseMesh",node=node,exists=True):
            cmds.addAttr(node,longName="baseMesh",dataType="mesh",hidden=True)
            cmds.connectAttr(self._base_input,node+".baseMesh")
        if not cmds.attributeQuery("sessionMeta",node=node,exists=True):
            cmds.addAttr(node,longName="sessionMeta",dataType="string",hidden=True)
        _set_string(node,"sessionMeta",dict(edges=list(self.edges),cuts=self.transverse_cuts))
        # Delete only templates exclusively owned by this history, preserving
        # upstream source nodes and any externally shared outputs.
        for n in old:
            if not cmds.objExists(n):
                continue
            consumers = cmds.listConnections(n+".output",s=False,d=True) or []
            if all(owner in old for owner in consumers):
                cmds.delete(n)
        return node


def apply(width, *, segments=1, shape=0, local=None, transverse_cuts=0):
    session = BevelSession(segments=segments, transverse_cuts=transverse_cuts)
    try:
        session.update(width=width, shape=shape)
        for edge, fraction, delta in local or ():
            session.local_width(delta, session.influence(edge, fraction))
        return session.commit()
    except Exception:
        if not session.closed:
            session.cancel()
        raise
