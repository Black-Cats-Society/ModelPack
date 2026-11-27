"""Component path previews and clicks, preserving native manipulator gestures."""
from dataclasses import dataclass
import uuid
import weakref
import time
from maya import cmds
from maya.api import OpenMaya as om,OpenMayaUI as omui
from PySide6 import QtCore,QtGui,QtWidgets
from shiboken6 import isValid,wrapInstance
from slide_for_maya.core import _mesh_fn
from . import core

NATIVE_CONTEXTS=('selectSuperContext','moveSuperContext','RotateSuperContext','scaleSuperContext')
_MODE=None
KINDS={om.MFn.kMeshVertComponent:'vertex',om.MFn.kMeshEdgeComponent:'edge',om.MFn.kMeshPolygonComponent:'face'}
COMPONENTS={value:key for key,value in KINDS.items()}


@dataclass
class State:
    names:tuple
    components:dict
    last:object=None

    @property
    def signature(self):return tuple((key,tuple(sorted(values))) for key,values in sorted(self.components.items()))

    def contains(self,endpoint):
        return endpoint is not None and endpoint[2] in self.components.get(endpoint[:2],())


def snapshot():
    selected=om.MGlobal.getActiveSelectionList(True);components={};last=None
    for index in range(selected.length()):
        try:dag,component=selected.getComponent(index)
        except RuntimeError:continue
        if component.isNull() or component.apiType() not in KINDS:continue
        if dag.node().hasFn(om.MFn.kTransform):dag.extendToShape()
        if not dag.node().hasFn(om.MFn.kMesh):continue
        kind=KINDS[component.apiType()];fn=om.MFnMesh(dag);ids=om.MFnSingleIndexedComponent(component)
        elements=list(range({'vertex':fn.numVertices,'edge':fn.numEdges,'face':fn.numPolygons}[kind])) if ids.isComplete else list(ids.getElements())
        key=dag.fullPathName(),kind;components.setdefault(key,set()).update(elements)
        if elements:last=(*key,elements[-1])
    # Serialize at press: Maya may reuse mutable component objects during a pick.
    return State(tuple(selected.getSelectionStrings()),components,last)


def changed_endpoint(before,after):
    changed=[]
    for key in before.components.keys()|after.components.keys():
        changed.extend((*key,index) for index in before.components.get(key,set())^after.components.get(key,set()))
        if len(changed)>1:return None
    return changed[0] if changed else None


def component_under_cursor(fn,kind,view,x,y,tolerance=10.):
    """Raycast and project components without changing any Maya selection."""
    from model_pack_linear_shift.overlay import Projection
    if not fn.dagPath().isVisible():return None
    projection=Projection(view,view.portWidth(),view.portHeight())
    faces=set()
    offsets=((0,0),(-.6,0),(.6,0),(0,-.6),(0,.6),(-.6,-.6),(.6,.6),(-.6,.6),(.6,-.6))
    for dx,dy in offsets:
        origin,direction=om.MPoint(),om.MVector()
        view.viewToWorld(round(x+dx*tolerance),round(y+dy*tolerance),origin,direction)
        hit=fn.closestIntersection(om.MFloatPoint(origin),om.MFloatVector(direction),om.MSpace.kWorld,
                                  1e10,False,accelParams=om.MFnMesh.autoUniformGridParams())
        if hit is not None:
            if kind=='face':return hit[2] if dx==0 and dy==0 else None
            faces.add(hit[2])
            if dx==0 and dy==0:break
    if not faces:return None
    if kind=='vertex':
        vertices={v for face in faces for v in fn.getPolygonVertices(face)};candidates=[]
        for v in vertices:
            px,py,visible=projection.point(fn.getPoint(v,om.MSpace.kWorld))
            if visible:candidates.append(((px-x)**2+(py-y)**2,v))
    else:
        iterator=om.MItMeshPolygon(fn.dagPath());edges=set();candidates=[]
        for face in faces:iterator.setIndex(face);edges.update(iterator.getEdges())
        for edge in edges:
            a,b=fn.getEdgeVertices(edge)
            segment=projection.line(fn.getPoint(a,om.MSpace.kWorld),fn.getPoint(b,om.MSpace.kWorld))
            if segment is None:continue
            (ax,ay),(bx,by)=segment;dx,dy=bx-ax,by-ay
            factor=max(0.,min(1.,((x-ax)*dx+(y-ay)*dy)/max(dx*dx+dy*dy,1e-12)))
            candidates.append(((ax+factor*dx-x)**2+(ay+factor*dy-y)**2,edge))
    best=min(candidates,default=None)
    return best[1] if best is not None and best[0]<=tolerance*tolerance else None


def path_selection(before,anchor,endpoint,add_only=False,replace=False):
    """Preflight and merge into the exact pre-click selection, without editing it."""
    if anchor is None or anchor[:2]!=endpoint[:2] or not before.contains(anchor):return None
    mesh,kind,end=endpoint
    path=core.shortest_path(_mesh_fn(mesh),kind,anchor[2],end)
    selection=om.MSelectionList()
    if not replace:
        for name in before.names:selection.add(name)
    component=om.MFnSingleIndexedComponent().create(COMPONENTS[kind])
    om.MFnSingleIndexedComponent(component).addElements(path)
    dag=_mesh_fn(mesh).dagPath()
    remove=not replace and not add_only and before.contains(endpoint)
    selection.merge(dag,component,om.MSelectionList.kRemoveFromList if remove else om.MSelectionList.kMergeNormal)
    return selection


def enabled():return _MODE is not None and not _MODE.closed


def shutdown():
    global _MODE
    mode,_MODE=_MODE,None
    if mode is not None:mode.close()


def enable(window):
    global _MODE
    shutdown();_MODE=Mode(window);_MODE.start();return True


class Mode(QtCore.QObject):
    def __init__(self,window):
        super().__init__(window);self.owner=weakref.ref(window);self.closed=False;self.guard=False
        self.state=snapshot();self.anchor=self.state.last;self.history={};self.callbacks=[]
        self.pending=None;self.chunk=None
        self.viewport_view=None
        self.preview=None;self.hover_key=None;self.hover_time=0.
        self.mouse_tracking=[]

    def start(self):
        self.callbacks=[om.MEventMessage.addEventCallback('SelectionChanged',self.selection_changed),
            om.MEventMessage.addEventCallback('ToolChanged',self.tool_changed)]
        self.callbacks.extend(om.MSceneMessage.addCallback(kind,lambda *_:shutdown())
            for kind in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen))
        QtWidgets.QApplication.instance().installEventFilter(self)
        for panel in cmds.getPanel(type='modelPanel') or []:
            try:
                view=omui.M3dView.getM3dViewFromModelPanel(panel)
                widget=wrapInstance(int(view.widget()),QtWidgets.QWidget)
                self.mouse_tracking.append((widget,widget.hasMouseTracking()));widget.setMouseTracking(True)
            except (RuntimeError,TypeError):continue

    def selection_changed(self,*_):
        if self.closed or self.guard:return
        self.clear_preview()
        current=snapshot();endpoint=changed_endpoint(self.state,current)
        remembered=self.history.get(current.signature)
        if current.contains(remembered):self.anchor=remembered
        elif current.contains(endpoint):self.anchor=endpoint
        elif not current.contains(self.anchor):self.anchor=current.last
        self.state=current

    def tool_changed(self,*_):
        if self.closed:return
        context=cmds.currentCtx()
        if self.pending and self.pending['context']!=context:
            self.finish_click();self.pending=None
        if context not in NATIVE_CONTEXTS:shutdown()

    def finish_click(self,expected=None):
        if expected is not None and expected!=self.chunk:return
        if self.chunk is not None:
            self.chunk=None;cmds.undoInfo(closeChunk=True)

    def begin_click(self,position,modifiers,widget=None,synthetic=False):
        if self.pending and self.pending.get('released'):self.complete_click(self.pending)
        self.finish_click();self.selection_changed()
        token=None
        # Only Select can wrap a purely native selection gesture. Manipulator
        # contexts keep their own Undo and are never automatically undone.
        plain=modifiers==QtCore.Qt.NoModifier;endpoint=None
        if plain and self.anchor is not None and widget is not None:
            try:endpoint=self.native_pick(widget,position,self.viewport_view)
            except (RuntimeError,ValueError):pass
        captured=plain and endpoint is not None and cmds.currentCtx()=='selectSuperContext'
        if ((modifiers&QtCore.Qt.ShiftModifier or captured) and cmds.currentCtx()=='selectSuperContext' and
                cmds.undoInfo(q=True,state=True)):
            token=uuid.uuid4().hex
            cmds.undoInfo(openChunk=True,chunkName='Selection Path');self.chunk=token
        self.pending=dict(before=self.state,anchor=self.anchor,position=position,token=token,
                          modifiers=modifiers,context=cmds.currentCtx(),widget=widget,
                          view=self.viewport_view,released=False,synthetic=synthetic,
                          plain=plain,captured=captured,endpoint=endpoint)

    def native_pick(self,widget,position,view=None):
        """Read the hovered component on the anchor mesh, also for Ctrl+Shift."""
        if self.anchor is None:return None
        view=view or omui.M3dView.active3dView();local=widget.mapFromGlobal(position)
        x=local.x()*view.portWidth()/widget.width()
        y=view.portHeight()-1-local.y()*view.portHeight()/widget.height()
        mesh,kind,_=self.anchor
        mask={'edge':'polymeshEdge','vertex':'polymeshVertex','face':'polymeshFace'}[kind]
        if cmds.selectType(query=True,**{mask:True}) is False:return None
        index=component_under_cursor(_mesh_fn(mesh),kind,view,x,y,10.*view.portWidth()/widget.width())
        return (mesh,kind,index) if index is not None else None

    def complete_click(self,pending):
        if self.closed or self.pending is not pending:return False
        self.pending=None
        try:
            if pending['context']!=cmds.currentCtx():return False
            modifiers=pending['modifiers']
            plain=pending.get('plain',False)
            if not modifiers&QtCore.Qt.ShiftModifier and not plain:
                self.selection_changed();return False
            current=snapshot();endpoint=pending.get('endpoint') if pending.get('captured') else changed_endpoint(pending['before'],current)
            if plain and not pending.get('captured'):
                # In manipulator contexts only extend an actual native pick;
                # clicking a handle must continue to move the current selection.
                if current.signature==pending['before'].signature:return False
                endpoint=current.last if sum(map(len,current.components.values()))==1 else None
            if endpoint is None and pending['widget'] is not None and (
                    pending['synthetic'] or (modifiers&QtCore.Qt.ControlModifier and pending['before'].signature==current.signature)):
                endpoint=self.native_pick(pending['widget'],pending['position'],pending['view'])
            if endpoint is None:return False  # Native marquee/ambiguous picking stays native.
            selection=path_selection(pending['before'],pending['anchor'],endpoint,
                                     add_only=bool(modifiers&QtCore.Qt.ControlModifier),replace=plain)
            if selection is None:return False
            self.guard=True
            try:
                if pending['before'].contains(pending['anchor']):
                    self.history[pending['before'].signature]=pending['anchor']
                # Keep the native endpoint pick and the resulting path in one
                # Select chunk. Never undo an earlier click or a transform.
                own_chunk=pending['token'] is None
                if own_chunk:cmds.undoInfo(openChunk=True,chunkName='Selection Path')
                try:om.MGlobal.selectCommand(selection,om.MGlobal.kReplaceList)
                finally:
                    if own_chunk:cmds.undoInfo(closeChunk=True)
                self.state=snapshot()
                self.anchor=endpoint if self.state.contains(endpoint) else self.state.last
                self.history[self.state.signature]=self.anchor
                while len(self.history)>32:self.history.pop(next(iter(self.history)))
            finally:self.guard=False
            return True
        except (RuntimeError,ValueError) as error:
            cmds.warning('Path: '+str(error));return False
        finally:self.finish_click(pending['token'])

    def viewport(self,target,event):
        for panel in cmds.getPanel(type='modelPanel') or []:
            try:
                view=omui.M3dView.getM3dViewFromModelPanel(panel)
                widget=wrapInstance(int(view.widget()),QtWidgets.QWidget)
                if not widget or not isValid(widget) or not widget.isVisible():continue
                direct=isinstance(target,QtWidgets.QWidget) and (target is widget or widget.isAncestorOf(target))
                native=False;ancestor=target
                if isinstance(target,QtGui.QWindow):
                    while ancestor is not None:
                        if ancestor in (widget.windowHandle(),widget.window().windowHandle()):native=True;break
                        ancestor=ancestor.parent()
                point=event.globalPosition().toPoint();local=widget.mapFromGlobal(point)
                hit=QtWidgets.QApplication.widgetAt(point)
                covered=(hit is not None and (hit is widget or widget.isAncestorOf(hit) or hit.isAncestorOf(widget)))
                parent=isinstance(target,QtWidgets.QWidget) and target.isAncestorOf(widget)
                if direct or ((native or parent) and covered and widget.rect().contains(local)):
                    self.viewport_view=view;return widget
            except (RuntimeError,TypeError,AttributeError):continue
        return None

    def close_after_native_release(self):
        self.pending=None;token=self.chunk
        if token is not None:QtCore.QTimer.singleShot(0,lambda:self.finish_click(token))

    def clear_preview(self):
        self.hover_key=None
        if self.preview is not None:self.preview.close();self.preview=None

    def hover(self,widget,position):
        if self.anchor is None or self.pending is not None:
            self.clear_preview();return
        now=time.monotonic()
        if now-self.hover_time<.04:return
        self.hover_time=now
        try:endpoint=self.native_pick(widget,position,self.viewport_view)
        except (ValueError,RuntimeError):self.clear_preview();return
        if endpoint is None or endpoint[:2]!=self.anchor[:2]:self.clear_preview();return
        key=self.anchor,endpoint
        if key==self.hover_key:return
        try:
            mesh,kind,end=endpoint;fn=_mesh_fn(mesh)
            route=core.shortest_path(fn,kind,self.anchor[2],end)
            points=fn.getPoints(om.MSpace.kWorld)
            if kind=='edge':pairs=[fn.getEdgeVertices(e) for e in route]
            elif kind=='vertex':pairs=list(zip(route,route[1:]))
            else:
                pairs=[]
                for face in route:
                    vertices=list(fn.getPolygonVertices(face));pairs.extend(zip(vertices,vertices[1:]+vertices[:1]))
            from model_pack_draft.preview import WireOverlay
            if self.preview is None:self.preview=WireOverlay()
            self.preview.points=points;self.preview.edges=pairs;self.preview.color=(1.,.72,.18)
            self.preview.bind(self.viewport_view);self.hover_key=key
        except (ValueError,RuntimeError):self.clear_preview()

    def eventFilter(self,target,event):
        if self.closed:return False
        kind=event.type()
        if kind==QtCore.QEvent.ApplicationDeactivate:
            self.clear_preview();self.finish_click();self.pending=None;return False
        if kind not in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.MouseButtonRelease,
                        QtCore.QEvent.MouseButtonDblClick,QtCore.QEvent.MouseMove):return False
        if kind in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.MouseButtonDblClick) and self.pending and self.pending.get('released'):
            self.complete_click(self.pending)
        if cmds.currentCtx() not in NATIVE_CONTEXTS:return False
        if kind==QtCore.QEvent.MouseMove and self.pending and not self.pending.get('released') and event.buttons()&QtCore.Qt.LeftButton:
            if (event.globalPosition().toPoint()-self.pending['position']).manhattanLength()>QtWidgets.QApplication.startDragDistance():
                self.pending['dragged']=True
        widget=self.viewport(target,event)
        if widget is None:
            self.clear_preview()
            if kind==QtCore.QEvent.MouseButtonRelease and event.button()==QtCore.Qt.LeftButton and self.pending:
                captured=self.pending.get('captured',False)
                self.close_after_native_release()
                if captured:event.accept();return True
            return False
        if kind==QtCore.QEvent.MouseMove:
            if self.pending and self.pending.get('captured'):return True
            if event.buttons()==QtCore.Qt.NoButton and not event.modifiers()&QtCore.Qt.AltModifier:
                self.hover(widget,event.globalPosition().toPoint())
            else:self.clear_preview()
            return False
        self.clear_preview()
        if event.button()!=QtCore.Qt.LeftButton:return False
        modifiers=event.modifiers()
        if modifiers&(QtCore.Qt.AltModifier|QtCore.Qt.MetaModifier):
            if self.pending and self.pending.get('released'):self.complete_click(self.pending)
            self.finish_click();self.pending=None;return False
        if kind in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.MouseButtonDblClick):
            if om.MGlobal.selectionMode()!=om.MGlobal.kSelectComponentMode and not snapshot().components:
                self.finish_click();self.pending=None;return False
            synthetic=kind==QtCore.QEvent.MouseButtonDblClick and bool(modifiers&QtCore.Qt.ShiftModifier)
            if kind==QtCore.QEvent.MouseButtonDblClick and not synthetic:return False
            self.begin_click(event.globalPosition().toPoint(),modifiers,widget,synthetic)
            if synthetic or self.pending.get('captured'):event.accept();return True
            return False
        if kind==QtCore.QEvent.MouseButtonRelease:
            pending=self.pending
            if pending is None:return False
            if pending.get('dragged'):
                self.close_after_native_release();return bool(pending.get('captured'))
            pending['released']=True
            if pending.get('captured'):
                self.complete_click(pending);event.accept();return True
            # Maya may pick on release; resolve its single-component change
            # only after the native handler, with this exact gesture identity.
            QtCore.QTimer.singleShot(0,lambda:self.complete_click(pending))
            if pending['synthetic']:event.accept();return True
            return False
        return False

    def close(self):
        if self.closed:return
        self.clear_preview()
        self.finish_click();self.closed=True;self.pending=None
        app=QtWidgets.QApplication.instance()
        if app is not None:app.removeEventFilter(self)
        for callback in self.callbacks:om.MMessage.removeCallback(callback)
        self.callbacks=[]
        for widget,tracking in self.mouse_tracking:
            if isValid(widget):widget.setMouseTracking(tracking)
        self.mouse_tracking=[]
        window=self.owner()
        if window is not None and isValid(window):window.selection_actions['Path'].setChecked(False)
        self.deleteLater()
