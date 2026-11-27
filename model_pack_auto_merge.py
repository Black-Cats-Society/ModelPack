"""Window-scoped, deferred native welding of the active mesh only."""
from collections import defaultdict
from array import array
import hashlib
import math
import struct
import sys
import time
import weakref
from PySide6 import QtCore,QtWidgets
from shiboken6 import isValid
from maya import cmds
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn

DEFAULT_DISTANCE=.001  # Internal Maya centimeters, independent of scene units.
DISTANCE_OPTION='ModelPack_AutoMergeDistanceCM'
_PACK=struct.Struct('<3d').pack


def active_mesh():
    """The last active selection item; never traverse the rest of the scene."""
    selected=om.MGlobal.getActiveSelectionList()
    if not selected.length():return None
    try:path=selected.getDagPath(selected.length()-1)
    except (RuntimeError,TypeError):return None
    if path.node().hasFn(om.MFn.kTransform):
        shapes=cmds.listRelatives(path.fullPathName(),shapes=True,noIntermediate=True,type='mesh',fullPath=True) or []
        if len(shapes)!=1:return None
        fn=_mesh_fn(shapes[0])
    elif path.node().hasFn(om.MFn.kMesh):fn=om.MFnMesh(path)
    else:return None
    if fn.isIntermediateObject or fn.isInstanced():return None
    name=fn.fullPathName()
    if cmds.referenceQuery(name,isNodeReferenced=True):return None
    if om.MFnDependencyNode(fn.object()).findPlug('inMesh',False).isLocked:return None
    if (cmds.lockNode(name,q=True,lock=True) or [False])[0]:return None
    return fn


def fingerprint(fn,points=None):
    points=fn.getPoints(om.MSpace.kWorld) if points is None else points
    digest=hashlib.sha256()
    for p in points:digest.update(_PACK(p.x,p.y,p.z))
    for values in fn.getVertices():digest.update(array('i',values).tobytes())
    return fn.numVertices,fn.numPolygons,digest.digest()


def nearby_vertices(points,distance):
    """All vertices with a neighbor in range, using a local spatial hash."""
    if not math.isfinite(distance) or distance<=0:raise ValueError('Auto Merge: distance must be positive.')
    cells=defaultdict(list);keys=[]
    for vertex,p in enumerate(points):
        key=tuple(math.floor(p[i]/distance) for i in range(3))
        cells[key].append(vertex);keys.append(key)
    close=set();squared=distance*distance
    offsets=tuple((x,y,z) for x in (-1,0,1) for y in (-1,0,1) for z in (-1,0,1))
    for vertex,key in enumerate(keys):
        if vertex in close:continue
        p=points[vertex];found=False
        for dx,dy,dz in offsets:
            for other in cells.get((key[0]+dx,key[1]+dy,key[2]+dz),()):
                if other==vertex:continue
                vector=p-points[other]
                if vector*vector<=squared:
                    close.update((vertex,other));found=True;break
            if found:break
    return sorted(close)


def _polygon_key(vertices):
    clean=[v for i,v in enumerate(vertices) if not i or v!=vertices[i-1]]
    if len(clean)>1 and clean[0]==clean[-1]:clean.pop()
    if len(set(clean))<3:return None
    start=clean.index(min(clean));forward=clean[start:]+clean[:start]
    reverse=[forward[0]]+forward[:0:-1]
    return min(tuple(forward),tuple(reverse))


class _SelectionState:
    """Remap only the user's selected components, never the merge arguments."""
    def __init__(self,fn):
        self.selection=om.MGlobal.getActiveSelectionList();self.hilite=om.MGlobal.getHiliteList()
        self.mode=om.MGlobal.selectionMode();self.context=cmds.currentCtx()
        self.mode_flag=next((flag for flag in ('object','component','hierarchical') if cmds.selectMode(q=True,**{flag:True})),None)
        self.objects=om.MSelectionMask(om.MGlobal.objectSelectionMask());self.components=om.MSelectionMask(om.MGlobal.componentSelectionMask())
        self.entries={};self.points=None
        for index in range(self.selection.length()):
            try:path,component=self.selection.getComponent(index)
            except (RuntimeError,TypeError):continue
            if component.isNull() or path.node()!=fn.object():continue
            kind=component.apiType()
            if kind not in (om.MFn.kMeshVertComponent,om.MFn.kMeshEdgeComponent,om.MFn.kMeshPolygonComponent):continue
            ids=list(om.MFnSingleIndexedComponent(component).getElements())
            if kind==om.MFn.kMeshEdgeComponent:elements=[fn.getEdgeVertices(i) for i in ids]
            elif kind==om.MFn.kMeshPolygonComponent:elements=[list(fn.getPolygonVertices(i)) for i in ids]
            else:elements=[(i,) for i in ids]
            self.entries[index]=(kind,elements)
        if self.entries:self.points=fn.getPoints(om.MSpace.kWorld)

    def restore(self,fn,distance):
        result=om.MSelectionList();mapping={};edges=faces=None
        if self.entries:
            points=fn.getPoints(om.MSpace.kWorld);cells=defaultdict(list)
            def key(point):return tuple(math.floor(point[i]/distance) for i in range(3))
            for v,point in enumerate(points):cells[key(point)].append(v)
            def vertex(old):
                if old not in mapping:
                    point=self.points[old];x,y,z=key(point)
                    candidates=[v for dx in (-1,0,1) for dy in (-1,0,1) for dz in (-1,0,1)
                                for v in cells.get((x+dx,y+dy,z+dz),())]
                    # A long connected weld cluster may move its average
                    # farther than one threshold from its original end.
                    mapping[old]=min(candidates or range(len(points)),key=lambda v:((points[v]-point).length(),v),default=None)
                return mapping[old]
            if any(kind==om.MFn.kMeshEdgeComponent for kind,_ in self.entries.values()):
                edges={frozenset(fn.getEdgeVertices(e)):e for e in range(fn.numEdges)}
            if any(kind==om.MFn.kMeshPolygonComponent for kind,_ in self.entries.values()):
                faces={_polygon_key(list(fn.getPolygonVertices(f))):f for f in range(fn.numPolygons)}
        for index in range(self.selection.length()):
            if index not in self.entries:
                for item in self.selection.getSelectionStrings(index):result.add(item)
                continue
            kind,elements=self.entries[index];ids=set()
            for element in elements:
                remapped=[vertex(v) for v in element]
                if None in remapped:continue
                if kind==om.MFn.kMeshVertComponent:found=remapped[0]
                elif kind==om.MFn.kMeshEdgeComponent:found=edges.get(frozenset(remapped))
                else:found=faces.get(_polygon_key(remapped))
                if found is not None:ids.add(found)
            if ids:
                component=om.MFnSingleIndexedComponent();obj=component.create(kind);component.addElements(sorted(ids))
                result.add((fn.dagPath(),obj))
        if om.MGlobal.selectionMode()!=self.mode:om.MGlobal.setSelectionMode(self.mode)
        om.MGlobal.setObjectSelectionMask(self.objects);om.MGlobal.setComponentSelectionMask(self.components)
        om.MGlobal.setHiliteList(self.hilite)
        # An undoable selection command belongs in the same chunk as the weld,
        # so Redo also restores the original selection type and intent.
        strings=result.getSelectionStrings()
        if strings:cmds.select(list(strings),replace=True,noExpand=True)
        else:cmds.select(clear=True)
        if self.mode_flag and not cmds.selectMode(q=True,**{self.mode_flag:True}):cmds.selectMode(**{self.mode_flag:True})
        if self.context and cmds.currentCtx()!=self.context:cmds.setToolTo(self.context)


def merge(fn,distance,vertices=None):
    """One native Undo step; keep history and separate UV coordinates."""
    vertices=nearby_vertices(fn.getPoints(om.MSpace.kWorld),distance) if vertices is None else vertices
    if len(vertices)<2:return 0
    before=fn.numVertices;name=fn.fullPathName();selection=_SelectionState(fn)
    value=om.MDistance(distance,om.MDistance.kCentimeters).asUnits(om.MDistance.uiUnit())
    cmds.undoInfo(openChunk=True,chunkName='Auto Merge')
    try:
        cmds.polyMergeVertex([name+f'.vtx[{v}]' for v in vertices],distance=value,
            alwaysMergeTwoVertices=False,texture=False,worldSpace=True,
            constructionHistory=bool(cmds.constructionHistory(q=True,toggle=True)))
    finally:
        try:selection.restore(_mesh_fn(name),distance)
        finally:cmds.undoInfo(closeChunk=True)
    return before-_mesh_fn(name).numVertices


def geometry_busy(owner):
    if getattr(owner,'_changing_tool',False):return True
    if getattr(owner,'_active_tool',None) in (
            'bevel','recurve','clone','slide_bevel','linear_shift','mirror_cut','mirror_dup','reset_tube','unbevel','hard_edges','offset','quad_dome','floater'):
        return True
    for name,attribute in (('recurve','SESSION'),('model_pack_bevel.ui','_SESSION'),
            ('slide_for_maya.ui','_SESSION'),('slide_for_maya.ui','_STATE'),
            ('model_pack_linear_shift.ui','_DRAG'),('model_pack_mirror_session','ACTIVE'),
            ('model_pack_selection.ui','_BOUNDARIES')):
        module=sys.modules.get(name)
        if module is not None and getattr(module,attribute,None):return True
    module=sys.modules.get('model_pack_draft.tool');engine=getattr(module,'_active',None)
    return engine is not None and engine.footprint is not None


class Service(QtCore.QObject):
    def __init__(self,owner):
        super().__init__(owner);self.owner=weakref.ref(owner)
        self.enabled=False;self.closed=False;self.writing=False;self.scene_changing=False
        self.pending=False;self.force_check=False;self.last_change=0.
        self.last_key=None;self.last_signature=None;self.last_attempt=None;self.watched=None
        self.callbacks=[];self.mesh_callbacks=[]
        self.application_active=QtWidgets.QApplication.applicationState()==QtCore.Qt.ApplicationActive
        self.distance=float(cmds.optionVar(q=DISTANCE_OPTION)) if cmds.optionVar(exists=DISTANCE_OPTION) else DEFAULT_DISTANCE
        if not math.isfinite(self.distance) or self.distance<=0:self.distance=DEFAULT_DISTANCE
        self.timer=QtCore.QTimer(self);self.timer.setInterval(200);self.timer.timeout.connect(self.tick)
        owner.destroyed.connect(self.close)

    def set_enabled(self,enabled):
        if self.closed:return
        enabled=bool(enabled)
        if enabled==self.enabled:return
        self.enabled=enabled
        if enabled:
            self.last_attempt=None
            self.application_active=QtWidgets.QApplication.applicationState()==QtCore.Qt.ApplicationActive
            app=QtWidgets.QApplication.instance()
            if app is not None:app.installEventFilter(self)
            self.callbacks=[om.MEventMessage.addEventCallback('SelectionChanged',self.selection_changed)]
            self.callbacks += [om.MEventMessage.addEventCallback(event,self.history_changed) for event in ('Undo','Redo')]
            self.callbacks += [om.MSceneMessage.addCallback(event,self.before_scene) for event in
                (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen)]
            self.callbacks += [om.MSceneMessage.addCallback(event,self.after_scene) for event in
                (om.MSceneMessage.kAfterNew,om.MSceneMessage.kAfterOpen)]
            self.timer.start();self.schedule(force=True)
        else:
            self.timer.stop();self.pending=False;self.force_check=False
            self._unwatch();self._remove(self.callbacks);self.callbacks=[]
            app=QtWidgets.QApplication.instance()
            if app is not None:app.removeEventFilter(self)

    def set_distance(self,value):
        value=float(value)
        if not math.isfinite(value) or value<=0:raise ValueError('Auto Merge: distance must be positive.')
        self.distance=value;cmds.optionVar(floatValue=(DISTANCE_OPTION,value));self.schedule(force=True)

    @staticmethod
    def _remove(callbacks):
        for callback in callbacks:
            try:om.MMessage.removeCallback(callback)
            except RuntimeError:pass

    def _unwatch(self):
        self._remove(self.mesh_callbacks);self.mesh_callbacks=[];self.watched=None

    def _watch(self,fn,key):
        if key==self.watched:return
        self._unwatch()
        self.mesh_callbacks=[om.MNodeMessage.addNodeDirtyPlugCallback(fn.object(),self.dirty)]
        self.mesh_callbacks.append(om.MDagMessage.addWorldMatrixModifiedCallback(fn.dagPath(),self.dirty))
        self.watched=key

    def schedule(self,*,force=False):
        if not self.enabled or self.writing or self.scene_changing:return
        if om.MGlobal.isUndoing() or om.MGlobal.isRedoing():return
        self.pending=True;self.force_check|=force;self.last_change=time.monotonic()

    def dirty(self,*_):self.schedule()
    def selection_changed(self,*_):self.schedule(force=True)

    def history_changed(self,*_):
        if not self.enabled or self.writing or self.scene_changing:return
        # Remember the restored result. Undo must not immediately weld it
        # again or destroy Redo; only a later edit/selection can run a new merge.
        self.writing=True
        try:
            fn=active_mesh()
            self.last_key=(om.MFnDependencyNode(fn.object()).uuid().asString(),fn.fullPathName()) if fn else None
            self.last_signature=fingerprint(fn) if fn else None
            self.pending=False;self.force_check=False
            self.last_attempt=None
        except (RuntimeError,ValueError):self.last_key=None;self.last_signature=None;self.last_attempt=None;self.pending=False
        finally:self.writing=False

    def before_scene(self,*_):
        self.scene_changing=True;self.pending=False;self._unwatch()
        self.last_key=None;self.last_signature=None
        self.last_attempt=None

    def after_scene(self,*_):
        self.scene_changing=False;self.schedule(force=True)

    def eventFilter(self,target,event):
        if event.type()==QtCore.QEvent.ApplicationDeactivate:self.application_active=False
        elif event.type()==QtCore.QEvent.ApplicationActivate:
            self.application_active=True;self.schedule()
        return False

    def idle(self):
        owner=self.owner()
        if owner is None or not isValid(owner) or not owner.isVisible() or owner.isMinimized():return False
        if not self.application_active or self.scene_changing:return False
        if QtWidgets.QApplication.mouseButtons()!=QtCore.Qt.NoButton:return False
        if QtWidgets.QApplication.activeModalWidget() or QtWidgets.QApplication.activePopupWidget():return False
        focus=QtWidgets.QApplication.focusWidget()
        if isinstance(focus,(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit)):return False
        if om.MGlobal.isUndoing() or om.MGlobal.isRedoing() or cmds.play(q=True,state=True):return False
        if not cmds.undoInfo(q=True,state=True) or geometry_busy(owner):return False
        # Other native construction contexts can hold component IDs between
        # drags. Resume on ordinary selection/move/rotate/scale tools.
        return cmds.currentCtx().lower() in ('selectsupercontext','movesupercontext','rotatesupercontext',
                                           'scalesupercontext','move','rotate','scale')

    def tick(self,force=False):
        if not self.enabled or self.closed or self.writing or not self.pending:return
        if not self.idle() or (not force and time.monotonic()-self.last_change<.18):return
        self.writing=True
        try:
            fn=active_mesh();self.pending=False
            if fn is None:
                self._unwatch();self.force_check=False;return
            key=om.MFnDependencyNode(fn.object()).uuid().asString(),fn.fullPathName()
            self._watch(fn,key);points=fn.getPoints(om.MSpace.kWorld);signature=fingerprint(fn,points)
            check=self.force_check or key!=self.last_key or signature!=self.last_signature
            self.force_check=False;self.last_key=key;self.last_signature=signature
            if not check:return
            attempt=key,signature,self.distance
            if attempt==self.last_attempt:return
            self.last_attempt=attempt
            vertices=nearby_vertices(points,self.distance)
            if vertices:
                # The fixed update bridge already defers any active tool.
                # Reserve that idle-window slot only during the native write,
                # so a nested UI event cannot reload the package mid-merge.
                owner=self.owner();reserved=owner is not None and getattr(owner,'_active_tool',None) is None
                if reserved:owner._active_tool='auto_merge'
                try:merge(fn,self.distance,vertices)
                finally:
                    if reserved and getattr(owner,'_active_tool',None)=='auto_merge':owner._active_tool=None
                self.last_signature=fingerprint(_mesh_fn(fn.fullPathName()))
                self.last_attempt=key,self.last_signature,self.distance
        except (RuntimeError,ValueError,OverflowError) as error:
            self.pending=False;self.force_check=False
            cmds.warning('Auto Merge: '+str(error))
        finally:self.writing=False

    def close(self,*_):
        if self.closed:return
        self.set_enabled(False);self.closed=True
