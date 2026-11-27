"""Interactive face inset retaining the original rounded-corner radii."""
import math
import weakref
from pathlib import Path
from contextlib import contextmanager
from maya import cmds
from maya.api import OpenMaya as om,OpenMayaUI as omui
from PySide6 import QtCore,QtGui,QtWidgets
from shiboken6 import isValid,wrapInstance
from slide_for_maya.core import _mesh_fn
from model_pack_offset_geometry import Contour,CurvedRegion

CONTEXT='modelPackOffsetContext'
_HOST=None
MANIPULATORS={QtCore.Qt.Key_Q:'selectSuperContext',QtCore.Qt.Key_W:'moveSuperContext',
              QtCore.Qt.Key_E:'RotateSuperContext',QtCore.Qt.Key_R:'scaleSuperContext'}

@contextmanager
def transient():
    enabled=cmds.undoInfo(q=True,state=True)
    if enabled:cmds.undoInfo(stateWithoutFlush=False)
    try:yield
    finally:
        if enabled:cmds.undoInfo(stateWithoutFlush=True)

def display_scale():return om.MDistance(1.,om.MDistance.kCentimeters).asUnits(om.MDistance.uiUnit())

def selected_face():
    selected=om.MGlobal.getActiveSelectionList();found=[]
    for i in range(selected.length()):
        try:path,component=selected.getComponent(i)
        except RuntimeError:raise ValueError('Offset: выберите один полигон.')
        if component.isNull() or not component.hasFn(om.MFn.kMeshPolygonComponent):raise ValueError('Offset: выберите один полигон.')
        if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
        fn=om.MFnMesh(path);ids=om.MFnSingleIndexedComponent(component)
        found.extend((path.fullPathName(),f) for f in (range(fn.numPolygons) if ids.isComplete else ids.getElements()))
    found=list(dict.fromkeys(found))
    if not found or len({name for name,face in found})!=1:
        raise ValueError('Offset: выберите связанную область полигонов одного меша.')
    return found[0][0],found[0][1] if len(found)==1 else [face for name,face in found]


def region_boundary(fn,faces):
    counts={};directed={}
    for face in faces:
        vertices=list(fn.getPolygonVertices(face))
        for a,b in zip(vertices,vertices[1:]+vertices[:1]):
            key=tuple(sorted((a,b)));counts[key]=counts.get(key,0)+1;directed[key]=(a,b)
    if any(count>2 for count in counts.values()):raise ValueError('Offset: область содержит неманифолдные рёбра.')
    pairs=[directed[key] for key,count in counts.items() if count==1]
    following={};incoming=set()
    for a,b in pairs:
        if a in following or b in incoming:raise ValueError('Offset: у области неоднозначный контур.')
        following[a]=b;incoming.add(b)
    if not following:raise ValueError('Offset: у области нет внешней границы.')
    start=min(following);ring=[];vertex=start
    while vertex not in ring:
        ring.append(vertex)
        if vertex not in following:raise ValueError('Offset: разорванный контур области.')
        vertex=following[vertex]
    if vertex!=start or len(ring)!=len(pairs):
        raise ValueError('Offset: выберите одну связанную область без внутренних отверстий.')
    internal=[edge for edge in range(fn.numEdges) if counts.get(tuple(sorted(fn.getEdgeVertices(edge))))==2]
    return ring,internal

def signature(fn):return tuple(tuple(v) for v in fn.getVertices())

def zero_extrude(mesh,face,history):
    fn=_mesh_fn(mesh);old_faces=fn.numPolygons;pts=fn.getPoints()
    source=frozenset(tuple(pts[v]) for v in fn.getPolygonVertices(face))
    cmds.polyExtrudeFacet(mesh+'.f[%d]'%face,keepFacesTogether=True,offset=0,
                         localTranslateZ=0,divisions=1,constructionHistory=history)
    fn=_mesh_fn(mesh);pts=fn.getPoints()
    backs=[mesh+'.f[%d]'%f for f in range(old_faces,fn.numPolygons)
           if frozenset(tuple(pts[v]) for v in fn.getPolygonVertices(f))==source]
    if backs:cmds.delete(backs)


class RadiusSession:
    def __init__(self,mesh=None,face=None):
        if mesh is None:mesh,face=selected_face()
        fn=_mesh_fn(mesh);path=fn.dagPath();self.mesh=path.fullPathName()
        self.faces=sorted(set(face if isinstance(face,(list,tuple,set)) else [int(face)]))
        self.face=self.faces[0]
        if path.isInstanced() or cmds.referenceQuery(self.mesh,isNodeReferenced=True):raise ValueError('Для Offset нужен локальный меш без instances.')
        if any(not 0<=face<fn.numPolygons for face in self.faces):raise ValueError('Выделенный полигон больше не существует.')
        if not cmds.getAttr(self.mesh+'.visibility',settable=True) or om.MFnDependencyNode(fn.object()).findPlug('inMesh',False).isLocked:
            raise ValueError('Меш заблокирован для редактирования.')
        holes=fn.getHoles()
        if any(hole[0] in self.faces for hole in holes):raise ValueError('Offset пока работает с полигонами без внутренних отверстий.')
        self.ids,self.internal_edges=region_boundary(fn,self.faces)
        all_points=fn.getPoints(om.MSpace.kWorld)
        self.world=[om.MPoint(all_points[v]) for v in self.ids];self.origin=om.MPoint(self.world[0]);normal=om.MVector()
        for a,b in zip(self.world,self.world[1:]+self.world[:1]):normal+=(a-self.origin)^(b-self.origin)
        if normal.length()<1e-12:raise ValueError('Полигон имеет нулевую площадь.')
        normal.normalize();self.normal=normal
        self.origin+=sum((p-self.origin for p in self.world),om.MVector())/len(self.world)
        self.u=max((p-self.origin for p in self.world),key=lambda v:v.length()).normal()
        self.v=(normal^self.u).normal();self.u=(self.v^normal).normal()
        scale=max((p-self.origin).length() for p in self.world)
        self.heights=[(p-self.origin)*normal for p in self.world]
        self.contour=Contour([((p-self.origin)*self.u,(p-self.origin)*self.v) for p in self.world])
        self.maximum=self.contour.maximum;self.scale=scale;self.amount=0.;self.closed=False;self.preview=None
        self.surface=self.curved_region(fn,all_points)
        if self.surface is not None:
            self.maximum=self.surface.maximum
        self.topology=signature(fn);self.original_points=[tuple(p) for p in all_points]
        self.selection=om.MGlobal.getActiveSelectionList();self.hilite=om.MGlobal.getHiliteList()
        self.mode=om.MGlobal.selectionMode();self.mask=om.MGlobal.componentSelectionMask()
        self.visible=cmds.getAttr(self.mesh+'.visibility');self.handle=om.MObjectHandle(fn.object())
        self.shaders,self.materials=fn.getConnectedShaders(path.instanceNumber())
        if self.surface is None and len({self.materials[f] for f in self.faces})>1:
            raise ValueError('Offset: для объединения области назначьте её полигонам один материал.')
        data=om.MFnMeshData().create();om.MFnMesh().copy(fn.object(),data)
        try:
            with transient():
                self.preview=cmds.createNode('transform',name='offsetPreview#')
                parent=om.MSelectionList().add(self.preview).getDependNode(0)
                om.MFnTransform(parent).setTransformation(om.MTransformationMatrix(path.inclusiveMatrix()))
                copied=om.MFnMesh();copied.copy(data,parent);shape=copied.fullPathName()
                for index,shader in enumerate(self.shaders):
                    members=[shape+'.f[%d]'%f for f,s in enumerate(self.materials) if s==index]
                    if members:cmds.sets(members,edit=True,forceElement=om.MFnDependencyNode(shader).name())
                self.inner=self.build_inset(shape,False)
                self.preview_points=_mesh_fn(self.preview).getPoints(om.MSpace.kWorld)
                cmds.setAttr(self.preview+'.visibility',False)
                for panel in cmds.getPanel(type='modelPanel') or []:
                    if cmds.isolateSelect(panel,q=True,state=True):
                        group=cmds.isolateSelect(panel,q=True,viewObjects=True)
                        if group:cmds.sets(self.preview,add=group)
                om.MGlobal.setHiliteList(self.hilite);om.MGlobal.setActiveSelectionList(self.selection)
        except Exception:
            self.cancel();raise

    def curved_region(self,fn,points):
        self.source_faces={face:tuple(fn.getPolygonVertices(face)) for face in self.faces}
        interior=sorted({v for face in self.source_faces.values() for v in face}-set(self.ids))
        self.cap_vertices=self.ids+interior
        lookup={vertex:i for i,vertex in enumerate(self.cap_vertices)}
        coordinates=[points[v]-self.origin for v in self.cap_vertices]
        xy=[(point*self.u,point*self.v) for point in coordinates]
        heights=[point*self.normal for point in coordinates]
        if max(map(abs,heights))<=max(self.scale*.005,1e-7):
            return None
        polygon=om.MItMeshPolygon(fn.dagPath())
        triangles=[]
        for face in self.faces:
            polygon.setIndex(face)
            _,indices=polygon.getTriangles(om.MSpace.kWorld)
            triangles.extend([lookup[v] for v in indices[i:i+3]] for i in range(0,len(indices),3))
        return CurvedRegion(self.contour,xy,heights,triangles)

    def build_inset(self,mesh,history):
        self.cap_faces=self.faces if self.surface is not None else [self.merge_region(mesh,history)]
        self.face=self.cap_faces[0]
        if self.surface is None:
            zero_extrude(mesh,self.face,history)
        else:
            cmds.polyExtrudeFacet([f'{mesh}.f[{face}]' for face in self.cap_faces],
                                  keepFacesTogether=True,offset=0,localTranslateZ=0,
                                  divisions=1,constructionHistory=history)
        if history:self._region_changed=True
        fn=_mesh_fn(mesh)
        if self.surface is None:return self.match_cap(fn)
        points=fn.getPoints(om.MSpace.kWorld)
        tolerance=max(self.scale*1e-5,1e-7)
        mapping={}
        for face,source in self.source_faces.items():
            cap=fn.getPolygonVertices(face)
            if len(cap)!=len(source):
                raise ValueError('Maya изменила структуру области при Offset.')
            for vertex in source:
                point=self.original_points[vertex]
                projected=om.MPoint(point)
                target=min(cap,key=lambda v:(points[v]-projected).length())
                if (points[target]-projected).length()>tolerance or mapping.get(vertex,target)!=target:
                    raise ValueError('Не удалось сопоставить внутренние вершины Offset.')
                mapping[vertex]=target
        if len(set(mapping.values()))!=len(self.cap_vertices):
            raise ValueError('Область содержит неоднозначные вершины.')
        return [mapping[v] for v in self.cap_vertices]

    def merge_region(self,mesh,history):
        if not self.internal_edges:return self.faces[0]
        cmds.polyDelEdge([mesh+'.e[%d]'%edge for edge in self.internal_edges],cleanVertices=False,constructionHistory=history)
        if history:self._region_changed=True
        fn=_mesh_fn(mesh);points=fn.getPoints(om.MSpace.kWorld)
        tolerance=max(self.scale*1e-5,1e-7)
        for face in range(fn.numPolygons):
            ids=fn.getPolygonVertices(face)
            if len(ids)==len(self.world) and all(any((points[v]-p).length()<tolerance for v in ids) for p in self.world):return face
        raise ValueError('Offset: не удалось объединить выбранную область в общий контур.')

    def match_cap(self,fn):
        cap=list(fn.getPolygonVertices(self.face));points=fn.getPoints(om.MSpace.kWorld)
        if len(cap)!=len(self.world):raise ValueError('Maya изменила структуру контура при Offset.')
        result=[]
        for p in self.world:
            vertex=min(cap,key=lambda v:(points[v]-p).length())
            if (points[vertex]-p).length()>max(self.scale*1e-5,1e-7):raise ValueError('Не удалось найти внутренний контур Offset.')
            result.append(vertex)
        if len(set(result))!=len(result):raise ValueError('Контур содержит неоднозначные вершины.')
        return result

    def validate(self):
        if self.closed:raise ValueError('Offset уже завершён.')
        if not self.handle.isValid() or not self.handle.isAlive():raise ValueError('Исходный меш удалён.')
        paths=om.MDagPath.getAllPathsTo(self.handle.object())
        if len(paths)!=1:raise ValueError('Исходный меш стал instance.')
        self.mesh=paths[0].fullPathName();fn=om.MFnMesh(paths[0])
        if signature(fn)!=self.topology or [tuple(p) for p in fn.getPoints(om.MSpace.kWorld)]!=self.original_points:
            raise ValueError('Исходная геометрия изменилась. Запустите Offset заново.')
        return fn

    def positions(self,amount):
        if self.surface is not None:
            return [self.origin+self.u*x+self.v*y+self.normal*h for x,y,h in self.surface.at(amount)]
        return [self.origin+self.u*x+self.v*y+self.normal*h for (x,y),h in zip(self.contour.at(amount),self.heights)]

    def update(self,amount):
        self.validate()
        if not math.isfinite(amount):raise ValueError('Offset должен быть конечным числом.')
        self.amount=max(0.,min(float(amount),self.maximum))
        with transient():
            points=om.MPointArray(self.preview_points)
            for vertex,p in zip(self.inner,self.positions(self.amount)):points[vertex]=p
            fn=_mesh_fn(self.preview);fn.setPoints(points,om.MSpace.kWorld);fn.updateSurface()
            show=self.amount>self.scale*1e-9
            cmds.setAttr(self.mesh+'.visibility',self.visible and not show)
            cmds.setAttr(self.preview+'.visibility',self.visible and show)
            if show:
                selected=om.MSelectionList()
                for face in self.cap_faces:selected.add(fn.fullPathName()+'.f[%d]'%face)
                hilite=om.MSelectionList();hilite.add(self.preview)
                om.MGlobal.setHiliteList(hilite);om.MGlobal.setActiveSelectionList(selected)
            else:om.MGlobal.setHiliteList(self.hilite);om.MGlobal.setActiveSelectionList(self.selection)
        return self.amount

    def remove_preview(self):
        with transient():
            if self.preview and cmds.objExists(self.preview):cmds.delete(self.preview)
            self.preview=None
            if self.handle.isValid() and self.handle.isAlive():
                paths=om.MDagPath.getAllPathsTo(self.handle.object())
                if paths:cmds.setAttr(paths[0].fullPathName()+'.visibility',self.visible)
            om.MGlobal.setSelectionMode(self.mode);om.MGlobal.setComponentSelectionMask(self.mask)
            om.MGlobal.setHiliteList(self.hilite);om.MGlobal.setActiveSelectionList(self.selection)

    def cancel(self):
        if self.closed:return
        self.remove_preview();self.closed=True

    def commit(self):
        self.validate()
        if self.amount<=self.scale*1e-9:self.cancel();return None
        if not cmds.undoInfo(q=True,state=True):raise ValueError('Включите Undo для применения Offset.')
        self.remove_preview();cmds.undoInfo(openChunk=True,chunkName='Offset')
        self._region_changed=False
        try:
            inner=self.build_inset(self.mesh,True);factor=display_scale()
            for vertex,p in zip(inner,self.positions(self.amount)):
                cmds.xform(self.mesh+'.vtx[%d]'%vertex,worldSpace=True,translation=tuple(v*factor for v in tuple(p)[:3]))
            cmds.select([self.mesh+'.f[%d]'%face for face in self.cap_faces],replace=True)
        except Exception:
            cmds.undoInfo(closeChunk=True)
            # A failed first native command leaves an empty chunk. Undoing it
            # would undo the user's previous action rather than this Offset.
            if self._region_changed:cmds.undo()
            self.closed=True;raise
        cmds.undoInfo(closeChunk=True);self.closed=True
        return self.mesh+'.f[%d]'%self.face

class Session:
    """Rebuild from the untouched input for every edit; commit one native action."""
    def __init__(self,mesh=None,face=None):
        if mesh is None:mesh,face=selected_face()
        fn=_mesh_fn(mesh);path=fn.dagPath();self.mesh=path.fullPathName()
        self.faces=sorted(set(face if isinstance(face,(list,tuple,set)) else [int(face)]))
        if path.isInstanced() or cmds.referenceQuery(self.mesh,isNodeReferenced=True):
            raise ValueError('Offset requires a local, non-instanced mesh.')
        if not self.faces or any(f<0 or f>=fn.numPolygons for f in self.faces):raise ValueError('Select mesh faces.')
        if not cmds.getAttr(self.mesh+'.visibility',settable=True) or om.MFnDependencyNode(fn.object()).findPlug('inMesh',False).isLocked:
            raise ValueError('The mesh is locked.')
        self.handle=om.MObjectHandle(fn.object());self.topology=signature(fn)
        self.original_points=[tuple(p) for p in fn.getPoints(om.MSpace.kWorld)]
        self.selection=om.MGlobal.getActiveSelectionList();self.hilite=om.MGlobal.getHiliteList()
        self.mode=om.MGlobal.selectionMode();self.mask=om.MGlobal.componentSelectionMask()
        self.visible=cmds.getAttr(self.mesh+'.visibility');self.preview=None;self.closed=False;self.amount=0.
        pts=[om.MPoint(self.original_points[v]) for f in self.faces for v in fn.getPolygonVertices(f)]
        self.scale=max((p-pts[0]).length() for p in pts) or 1.
        self.maximum=self.scale*100;self.preserve=True;self.max_angle=95.;self.variation=0.;self.subdivisions=0
        self.radius=len(self.faces)==1;self.legacy=None;self.radius_available=False
        try:
            self.legacy=RadiusSession(self.mesh,self.faces);self.radius_available=True;self.maximum=self.legacy.maximum
        except ValueError:pass

    def validate(self):
        if self.closed:raise ValueError('Offset is already closed.')
        if not self.handle.isValid() or not self.handle.isAlive():raise ValueError('Input mesh was deleted.')
        paths=om.MDagPath.getAllPathsTo(self.handle.object())
        if len(paths)!=1:raise ValueError('Input mesh became instanced.')
        self.mesh=paths[0].fullPathName();fn=om.MFnMesh(paths[0])
        if signature(fn)!=self.topology or [tuple(p) for p in fn.getPoints(om.MSpace.kWorld)]!=self.original_points:
            raise ValueError('Input geometry changed. Restart Offset.')
        return fn

    def configure(self,preserve=True,max_angle=95.,variation=0.,subdivisions=0,radius=True):
        self.preserve=bool(preserve);self.max_angle=float(max_angle);self.variation=max(0.,min(100.,float(variation)))
        self.subdivisions=int(subdivisions);self.radius=bool(radius)

    def groups(self,fn):
        if not self.preserve:return [[f] for f in self.faces]
        parent={f:f for f in self.faces}
        def root(f):
            while parent[f]!=f:parent[f]=parent[parent[f]];f=parent[f]
            return f
        it=om.MItMeshEdge(fn.dagPath());selected=set(self.faces)
        while not it.isDone():
            pair=list(it.getConnectedFaces())
            if len(pair)==2 and set(pair)<=selected:
                a,b=pair;na=fn.getPolygonNormal(a,om.MSpace.kWorld);nb=fn.getPolygonNormal(b,om.MSpace.kWorld)
                if math.degrees(na.angle(nb))<=self.max_angle+1e-7:parent[root(b)]=root(a)
            it.next()
        groups={}
        for face in self.faces:groups.setdefault(root(face),[]).append(face)
        return list(groups.values())

    def use_radius(self):
        # The existing rounded-contour algorithm is an optional planar mode.
        return self.radius and self.radius_available and self.preserve and self.max_angle>=.001 and self.subdivisions==0 and len(self.groups(_mesh_fn(self.mesh)))==1

    @property
    def effective_amount(self):
        return self.amount * (1.0 - self.variation / 100.0)

    def build(self,mesh,history):
        groups=self.groups(_mesh_fn(mesh))
        amount=self.effective_amount
        bands=self.subdivisions+1;backs={};self._build_changed=False
        for group in groups:
            fn=_mesh_fn(mesh);old_faces=fn.numPolygons;points=fn.getPoints()
            source={frozenset(tuple(points[v]) for v in fn.getPolygonVertices(f)) for f in group}
            caps=[mesh+'.f[%d]'%f for f in group]
            cmds.polyExtrudeFacet(caps,keepFacesTogether=True,offset=amount*display_scale(),
                                 localTranslateZ=0,divisions=bands,constructionHistory=history)
            self._build_changed=True
            fn=_mesh_fn(mesh);points=fn.getPoints()
            for f in range(old_faces,fn.numPolygons):
                key=frozenset(tuple(points[v]) for v in fn.getPolygonVertices(f))
                # Maya closes open boundaries with coincident reversed faces.
                if key in source:backs[key]=fn.getPolygonNormal(f)
        if backs:
            fn=_mesh_fn(mesh);points=fn.getPoints();remove=[]
            for f in range(fn.numPolygons):
                key=frozenset(tuple(points[v]) for v in fn.getPolygonVertices(f))
                if key in backs and fn.getPolygonNormal(f)*backs[key]>.99:remove.append(mesh+'.f[%d]'%f)
            if remove:cmds.delete(remove)
        return mesh

    def update(self,amount):
        self.validate()
        if not math.isfinite(amount):raise ValueError('Offset must be finite.')
        self.amount=float(amount)
        if self.use_radius() and self.amount>=0:
            self.clear_native()
            if self.legacy is None:self.legacy=RadiusSession(self.mesh,self.faces)
            self.amount=min(self.amount,self.legacy.maximum)
            self.legacy.update(self.effective_amount)
            return self.amount
        if self.legacy is not None:self.legacy.cancel();self.legacy=None
        with transient():
            self.clear_native()
            if abs(self.effective_amount)<=self.scale*1e-9:return self.amount
            self.preview=cmds.duplicate(self.mesh,returnRootsOnly=True,name='offsetPreview#')[0]
            cmds.delete(self.preview,constructionHistory=True)
            for panel in cmds.getPanel(type='modelPanel') or []:
                if cmds.isolateSelect(panel,q=True,state=True):
                    group=cmds.isolateSelect(panel,q=True,viewObjects=True)
                    if group:cmds.sets(self.preview,add=group)
            self.build(self.preview,False)
            cmds.setAttr(self.preview+'.visibility',self.visible)
            cmds.setAttr(self.mesh+'.visibility',False)
            hilite=om.MSelectionList();hilite.add(self.preview);om.MGlobal.setHiliteList(hilite)
            sel=om.MSelectionList();sel.add(self.preview);om.MGlobal.setActiveSelectionList(sel)
        return self.amount

    def clear_native(self):
        with transient():
            if self.preview and cmds.objExists(self.preview):cmds.delete(self.preview)
            self.preview=None
            if cmds.objExists(self.mesh):cmds.setAttr(self.mesh+'.visibility',self.visible)
            om.MGlobal.setHiliteList(self.hilite);om.MGlobal.setActiveSelectionList(self.selection)

    def cancel(self):
        if self.closed:return
        if self.legacy:self.legacy.cancel();self.legacy=None
        self.clear_native();self.closed=True

    def commit(self):
        self.validate()
        if self.legacy is not None:
            result=self.legacy.commit();self.closed=True;return result
        if abs(self.effective_amount)<=self.scale*1e-9:self.cancel();return None
        if not cmds.undoInfo(q=True,state=True):raise ValueError('Enable Maya Undo before applying Offset.')
        self.clear_native();cmds.undoInfo(openChunk=True,chunkName='Offset Inset');changed=False
        try:
            self._build_changed=False
            self.build(self.mesh,True);changed=self._build_changed
            cmds.select([self.mesh+'.f[%d]'%f for f in self.faces],replace=True)
        except Exception:
            changed=changed or self._build_changed
            cmds.undoInfo(closeChunk=True)
            if changed:cmds.undo()
            self.closed=True;raise
        cmds.undoInfo(closeChunk=True);self.closed=True;return self.mesh


def active():return _HOST is not None and not _HOST.stopped

def shutdown(apply=False):
    global _HOST
    host,_HOST=_HOST,None
    if host is not None and isValid(host):host.stop(apply)

def toggle(window):
    global _HOST
    if active() and _HOST.window() is window:shutdown(apply=True);return
    shutdown();window._prepare_tool('offset')
    host=Host(window);_HOST=host
    try:host.start()
    except Exception as exc:
        shutdown();cmds.warning(str(exc))

def set_button(window,checked):
    window.offset_button.setChecked(checked)
    icon='offset_active.svg' if checked else 'offset.svg'
    window.offset_button.setIcon(QtGui.QIcon(str(Path(__file__).resolve().parent/'icons'/icon)))

def sync(window):
    if _HOST is not None and _HOST.window() is window and cmds.currentCtx()!=CONTEXT:shutdown(apply=True)
    checked=active() and _HOST.window() is window
    if window.offset_button.isChecked()!=checked:set_button(window,checked)

class Host(QtCore.QObject):
    def __init__(self,window):
        super().__init__(window);self.window_ref=weakref.ref(window);self.panel=None;self.session=None
        self.compact_size=QtCore.QSize(window.size());self.stopped=False;self.callbacks=[];self.anchor=None;self.history=None
    def window(self):
        w=self.window_ref();return w if w is not None and isValid(w) else None
    def start(self):
        self.session=Session();window=self.window();self.panel=QtWidgets.QWidget(window)
        self.panel.setObjectName('modelPackOffsetSettings');layout=QtWidgets.QVBoxLayout(self.panel)
        layout.setContentsMargins(0,5,0,0);layout.setSpacing(6)
        label_width=self.panel.fontMetrics().horizontalAdvance('Offset Variation')+10
        def label(text):
            widget=QtWidgets.QLabel(text,self.panel);widget.setMinimumWidth(label_width);return widget
        row=QtWidgets.QHBoxLayout();row.addWidget(label('Offset'))
        self.amount=QtWidgets.QDoubleSpinBox(self.panel);self.amount.setDecimals(3)
        self.amount.setRange(-self.session.scale*100*display_scale(),self.session.scale*100*display_scale());self.amount.setKeyboardTracking(False);self.amount.setSuffix(' '+cmds.currentUnit(q=True,linear=True))
        self.amount.setSingleStep(max(self.session.scale*display_scale()*.01,.001));row.addWidget(self.amount);layout.addLayout(row)
        self.sliders=[]
        def slider_row(row,field,lo,hi,unit=1.):
            field.setFixedWidth(86)
            slider=QtWidgets.QSlider(QtCore.Qt.Horizontal,self.panel);slider.setRange(lo,hi);slider.setValue(round(field.value()/unit))
            slider.setMinimumWidth(65);row.addWidget(slider,1)
            slider.valueChanged.connect(lambda v:field.setValue(v*unit))
            def sync_slider(v):
                blocker=QtCore.QSignalBlocker(slider);slider.setValue(round(v/unit))
            field.valueChanged.connect(sync_slider);self.sliders.append(slider)
            slider.sliderPressed.connect(lambda:self.history.begin() if self.history else None)
            slider.sliderReleased.connect(lambda:self.history.end() if self.history else None)
            return slider
        slider_row(row,self.amount,-1000,1000,self.session.scale*display_scale()/1000.)
        self.variation=QtWidgets.QDoubleSpinBox(self.panel);self.variation.setRange(0,100);self.variation.setSuffix(' %');self.variation.setKeyboardTracking(False)
        self.subdivisions=QtWidgets.QSpinBox(self.panel);self.subdivisions.setRange(0,64);self.subdivisions.setKeyboardTracking(False)
        self.max_angle=QtWidgets.QDoubleSpinBox(self.panel);self.max_angle.setRange(0,180);self.max_angle.setValue(95);self.max_angle.setSuffix('°');self.max_angle.setKeyboardTracking(False)
        for text,field in (('Offset Variation',self.variation),('Subdivision',self.subdivisions),('Maximum Angle',self.max_angle)):
            r=QtWidgets.QHBoxLayout();r.addWidget(label(text));r.addWidget(field);layout.addLayout(r)
            slider_row(r,field,0,1800 if field is self.max_angle else (100 if field is self.variation else 64),.1 if field is self.max_angle else 1.)
        self.preserve=QtWidgets.QCheckBox('Preserve Groups',self.panel);self.preserve.setChecked(True)
        self.radius=QtWidgets.QCheckBox('Keep Corner Radius',self.panel);self.radius.setChecked(self.session.radius_available and self.session.radius)
        self.radius.setEnabled(self.session.radius_available)
        self.radius.setToolTip('Режим сохранения радиусов плоского контура. Отключите для обычного Inset; разбиения используют обычный Inset.')
        for field in (self.preserve,self.radius):layout.addWidget(field)
        for field in (self.variation,self.subdivisions,self.max_angle):field.valueChanged.connect(lambda *_:self.change(self.amount.value()))
        for field in (self.preserve,self.radius):field.toggled.connect(lambda *_:self.change(self.amount.value()))
        self.hint=QtWidgets.QLabel('Drag LMB · Negative offset expands outward\nEnter — Apply · Esc — Cancel',self.panel)
        self.hint.setWordWrap(True);self.hint.setAlignment(QtCore.Qt.AlignCenter);self.hint.setStyleSheet('color:#a4adb7;font-size:10px;')
        layout.addWidget(self.hint);row=QtWidgets.QHBoxLayout()
        self.apply_button=QtWidgets.QPushButton('Apply',self.panel);self.cancel_button=QtWidgets.QPushButton('Cancel',self.panel)
        for button in (self.apply_button,self.cancel_button):button.setAutoDefault(False);button.setMinimumHeight(30);row.addWidget(button)
        self.apply_button.setProperty('primary',True);self.apply_button.clicked.connect(lambda:shutdown(apply=True))
        self.cancel_button.clicked.connect(lambda:shutdown());layout.addLayout(row)
        self.amount.valueChanged.connect(self.change)
        window.layout().addWidget(self.panel,window._panel_row,0,1,2);self.panel.show()
        cmds.draggerContext(CONTEXT,pressCommand=self.press,dragCommand=self.drag,releaseCommand=self.release,
                            cursor='hand',space='screen',undoMode='all')
        cmds.setToolTo(CONTEXT)
        self.callbacks=[om.MEventMessage.addEventCallback('ToolChanged',lambda *_:self.tool_changed())]
        self.callbacks.extend(om.MSceneMessage.addCallback(kind,lambda *_:shutdown()) for kind in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen))
        QtWidgets.QApplication.instance().installEventFilter(self);set_button(window,True)
        self.change(min(self.session.scale*.04,self.session.maximum*.25)*display_scale())
        if self.stopped:return
        from model_pack_theme import PreviewHistory
        self.history=PreviewHistory(self,(self.amount,self.variation,self.subdivisions,self.max_angle,self.preserve,self.radius),lambda:self.change(self.amount.value()))
        window.layout().activate();window.resize(window.width(),window.sizeHint().height())
    def change(self,value):
        if self.stopped:return
        try:
            self.max_angle.setEnabled(self.preserve.isChecked())
            self.sliders[3].setEnabled(self.preserve.isChecked())
            self.radius.setEnabled(self.session.radius_available and self.preserve.isChecked() and self.subdivisions.value()==0)
            self.session.configure(self.preserve.isChecked(),self.max_angle.value(),self.variation.value(),self.subdivisions.value(),self.radius.isChecked())
            actual=self.session.update(float(value)/display_scale())*display_scale()
            blocker=QtCore.QSignalBlocker(self.amount);self.amount.setValue(actual);del blocker
            self.apply_button.setEnabled(abs(self.session.effective_amount)>self.session.scale*1e-9)
            cmds.refresh(currentView=True)
        except Exception as exc:cmds.warning(str(exc));shutdown()
    def press(self):
        if cmds.draggerContext(CONTEXT,q=True,button=True)!=1 or QtWidgets.QApplication.keyboardModifiers()&QtCore.Qt.AltModifier:
            self.anchor=None;return
        self.anchor=tuple(cmds.draggerContext(CONTEXT,q=True,anchorPoint=True))
        if self.history:self.history.begin()
        self.start_amount=self.session.amount
    def drag(self):
        if self.anchor is None:return
        end=cmds.draggerContext(CONTEXT,q=True,dragPoint=True)
        self.change((self.start_amount+(end[0]-self.anchor[0])*self.session.scale*.003)*display_scale())
    def release(self):
        self.drag();self.anchor=None
        if self.history:self.history.end()
    def tool_changed(self):
        QtCore.QTimer.singleShot(0, self.finish_context_change)

    def finish_context_change(self):
        if self.stopped or _HOST is not self or cmds.currentCtx() == CONTEXT:
            return
        timer = getattr(self.window(), '_scene_reset_timer', None)
        if timer is None or not timer.isActive():
            shutdown(apply=True)
    def owns_input(self,target):
        window=self.window()
        if window is not None and (target is window or window.isAncestorOf(target)):return True
        for panel in cmds.getPanel(type='modelPanel') or []:
            try:
                widget=wrapInstance(int(omui.M3dView.getM3dViewFromModelPanel(panel).widget()),QtWidgets.QWidget)
                if target is widget or widget.isAncestorOf(target):return True
            except (RuntimeError,ValueError):pass
        return False
    def eventFilter(self,target,event):
        if self.stopped or not active():return False
        if event.type() not in (QtCore.QEvent.KeyPress,QtCore.QEvent.KeyRelease,QtCore.QEvent.ShortcutOverride):return False
        inputs=(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox,QtWidgets.QTextEdit,QtWidgets.QPlainTextEdit)
        if isinstance(target,inputs) or isinstance(QtWidgets.QApplication.instance().focusWidget(),inputs):return False
        if event.modifiers()!=QtCore.Qt.NoModifier or not self.owns_input(target):return False
        key=event.key()
        if key not in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter,QtCore.Qt.Key_Escape) and key not in MANIPULATORS:return False
        if event.type()==QtCore.QEvent.KeyPress and not event.isAutoRepeat():
            context=MANIPULATORS.get(key);shutdown(apply=key!=QtCore.Qt.Key_Escape)
            if context:cmds.setToolTo(context)
        event.accept();return True
    def stop(self,apply=False):
        if self.stopped:return
        if self.history:self.history.close()
        self.stopped=True
        app=QtWidgets.QApplication.instance()
        if app:app.removeEventFilter(self)
        for callback in self.callbacks:om.MMessage.removeCallback(callback)
        self.callbacks=[]
        if cmds.currentCtx()==CONTEXT:cmds.setToolTo('selectSuperContext')
        if cmds.draggerContext(CONTEXT,exists=True):cmds.deleteUI(CONTEXT,toolContext=True)
        try:
            if self.session is not None:
                if apply:
                    try:self.session.commit()
                    except Exception as exc:self.session.cancel();cmds.warning(str(exc))
                else:self.session.cancel()
        finally:
            window=self.window()
            if self.panel is not None and isValid(self.panel):
                self.panel.hide()
                if window is not None:window.layout().removeWidget(self.panel)
                self.panel.deleteLater()
            if window is not None:
                if window._active_tool=='offset':window._active_tool=None
                set_button(window,False);window.layout().activate();window.resize(self.compact_size)
            self.deleteLater()
