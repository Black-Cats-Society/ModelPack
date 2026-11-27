"""Surface Fit panel, reversible mesh history and surface-following viewport handles."""
import json,math,time
import gc,runpy
from pathlib import Path
from contextlib import contextmanager
from PySide6 import QtCore,QtGui,QtWidgets
from shiboken6 import wrapInstance,isValid
from maya import cmds
from maya.api import OpenMaya as om,OpenMayaUI as omui
import model_pack_surface_fit as geo
from model_pack_linear_shift import overlay
from model_pack_mirror_viewport import Canvas as OccludedCanvas,distance

OWNER=None
EDGE_RAMP=[[0.,1.],[.25,1.],[.75,0.],[1.,0.]]
FIELDS=('axis','scale','offset','face','triangle','anchor','normal','tangent','weights','ramp','edgeMode','surfaceBinding')

def ensure_binding(node):
    if cmds.objExists(node+'.surfaceBinding'):return
    cmds.addAttr(node,ln='surfaceBinding',dt='string')
    target=target_path(node)
    if target and cmds.getAttr(node+'.face')>=0:
        fn=mesh(target);c,_,_,_=contact(node)
        set_value(node,'surfaceBinding',geo.surface_binding(fn,om.MPoint(c)*fn.dagPath().inclusiveMatrixInverse()))

def ensure_edge_mode(node):
    if cmds.objExists(node+'.edgeMode'):return
    if not cmds.objExists(node+'.source'):return
    source=cmds.listConnections(node+'.source',s=True,d=False) or []
    if not source:return
    enabled=geo.has_open_boundary(input_mesh(node))
    cmds.addAttr(node,ln='edgeMode',at='bool',defaultValue=enabled)
    cmds.dgdirty(node)

def refresh_nodes():
    """Update existing registered Python nodes without unloading scene history."""
    if not cmds.pluginInfo('model_pack_surface_fit_node',q=True,loaded=True):return
    fresh=runpy.run_path(str(Path(__file__).with_name('model_pack_surface_fit_node.py')),run_name='surface_fit_refresh')['SurfaceFit']
    for candidate in gc.get_objects():
        # gc also contains weakref proxies whose referents may be gone.
        # isinstance dereferences those proxies and can abort the whole reload.
        if type(candidate) is type and candidate.__name__=='SurfaceFit' and issubclass(candidate,om.MPxNode) and hasattr(candidate,'outputMesh'):
            candidate.compute=fresh.compute
            candidate.setDependentsDirty=fresh.setDependentsDirty
    for node in cmds.ls(type='modelPackSurfaceFit') or []:
        selected=om.MSelectionList();selected.add(node);instance=om.MFnDependencyNode(selected.getDependNode(0)).userNode()
        if instance is not None:instance.last_good=None;instance.edge_cache=None
        repair_inherited_tweaks(node)
        ensure_edge_mode(node)
        ensure_binding(node)
        cmds.dgdirty(node)
        results=cmds.listConnections(node+'.result',s=True,d=False) if cmds.objExists(node+'.result') else []
        if results and not cmds.objExists(node+'.outputFrame'):
            with chunk('Surface Fit pivot'):update_result_pivot(node,results[0])

def tweaks(shape):
    fn=om.MFnDependencyNode(mesh(shape).object());plug=fn.findPlug('pnts',False);values={}
    for index in plug.getExistingArrayAttributeIndices():
        element=plug.elementByLogicalIndex(index);value=tuple(element.child(i).asDouble() for i in range(3))
        if max(map(abs,value))>1e-9:values[index]=value
    return values

def repair_inherited_tweaks(node):
    """Repair the old duplicate() output only when its offsets match the source."""
    if cmds.objExists(node+'.cleanOutput'):return
    if not cmds.objExists(node+'.source') or not cmds.objExists(node+'.result'):return
    source=cmds.listConnections(node+'.source',s=True,d=False) or []
    result=cmds.listConnections(node+'.result',s=True,d=False) or []
    if not source or not result:return
    original=tweaks(source[0]);copied=tweaks(result[0])
    if copied and (copied.keys()!=original.keys() or any(
            max(abs(a-b) for a,b in zip(value,original[index]))>1e-7 for index,value in copied.items())):
        return  # Distinct downstream edits belong to the user.
    with chunk('Repair Surface Fit inherited offsets'):
        shape=mesh(result[0]).fullPathName()
        for index in copied:cmds.setAttr(shape+f'.pnts[{index}]',0,0,0,type='float3')
        cmds.addAttr(node,ln='cleanOutput',at='bool',defaultValue=True)

def copy_materials(source,result):
    shaders,indices=source.getConnectedShaders(source.dagPath().instanceNumber())
    if not shaders:cmds.sets(result,e=True,forceElement='initialShadingGroup');return
    for shader_index,shader in enumerate(shaders):
        faces=[i for i,value in enumerate(indices) if value==shader_index]
        if faces:
            group=om.MFnDependencyNode(shader).name()
            cmds.sets(result if len(faces)==source.numPolygons else [result+f'.f[{i}]' for i in faces],e=True,forceElement=group)

def mesh(name):
    s=om.MSelectionList();s.add(name);p=s.getDagPath(0)
    if p.node().hasFn(om.MFn.kTransform):
        shapes=cmds.listRelatives(name,shapes=True,noIntermediate=True,fullPath=True,type='mesh') or []
        if len(shapes)!=1:raise ValueError('Surface Fit: выберите один polygon mesh.')
        s=om.MSelectionList();s.add(shapes[0]);p=s.getDagPath(0)
    return om.MFnMesh(p)

def input_mesh(node):
    selection=om.MSelectionList();selection.add(node)
    plug=om.MFnDependencyNode(selection.getDependNode(0)).findPlug('inputMesh',False)
    return om.MFnMesh(plug.asMObject())

def viewport_flags(editor,**flags):
    # Presentation changes must not become extra scene Undo steps.
    enabled=cmds.undoInfo(q=True,state=True)
    try:
        cmds.undoInfo(stateWithoutFlush=False);cmds.modelEditor(editor,e=True,**flags)
    finally:cmds.undoInfo(stateWithoutFlush=enabled)

def absorb_topology_edits(node):
    """Move polygon edits from after the fit to its rest-space input branch."""
    results=cmds.listConnections(node+'.result',s=True,d=False) if cmds.objExists(node+'.result') else []
    if not results:return False
    shape=mesh(results[0]).fullPathName();destination=shape+'.inMesh'
    tail=cmds.connectionInfo(destination,sourceFromDestination=True)
    output=node+'.outputMesh'
    if not tail or tail==output:return False
    current=tail;seen=set();first_input=None
    supported={'polySplit','polySplitRing','polySubdFace','polySmoothFace','polyTriangulate','polyTweak','polyExtrudeFace','polyDelFacet','polyMergeVert','polyCloseBorder'}
    while current and current!=output:
        name=current.split('.')[0]
        if name in seen or cmds.nodeType(name) not in supported:return False
        seen.add(name);first_input=name+'.inputPolymesh'
        if not cmds.objExists(first_input):return False
        current=cmds.connectionInfo(first_input,sourceFromDestination=True)
    if current!=output:return False
    source=cmds.connectionInfo(node+'.inputMesh',sourceFromDestination=True)
    matrix=cmds.connectionInfo(node+'.sourceMatrix',sourceFromDestination=True)
    if not source:return False
    # Polygon edits use the result's local frame. Keep that frame fixed at
    # edit time while preserving the live source world transform.
    pre=cmds.createNode('transformGeometry',name='surfaceFitEditWorld#')
    post=cmds.createNode('transformGeometry',name='surfaceFitEditLocal#')
    cmds.connectAttr(source,pre+'.inputGeometry')
    if matrix:
        cmds.connectAttr(matrix,pre+'.transform')
    else:
        value=cmds.getAttr(node+'.sourceMatrix')
        cmds.setAttr(pre+'.transform',*value,type='matrix')
    if matrix and '.worldMatrix[' in matrix:
        cmds.connectAttr(matrix.replace('.worldMatrix[','.worldInverseMatrix['),post+'.transform')
    else:
        value=om.MMatrix(cmds.getAttr(node+'.sourceMatrix')).inverse()
        cmds.setAttr(post+'.transform',*tuple(value),type='matrix')
    matrices=[]
    if cmds.objExists(node+'.outputFrame'):
        frame=om.MMatrix(cmds.getAttr(node+'.outputFrame'))
        for target,index,constant in ((pre,1,frame.inverse()),(post,0,frame)):
            combined=cmds.createNode('multMatrix',name='surfaceFitEditFrame#');matrices.append(combined)
            plug=cmds.connectionInfo(target+'.transform',sourceFromDestination=True)
            if plug:
                cmds.disconnectAttr(plug,target+'.transform');cmds.connectAttr(plug,combined+f'.matrixIn[{1-index}]')
            else:cmds.setAttr(combined+f'.matrixIn[{1-index}]',*cmds.getAttr(target+'.transform'),type='matrix')
            cmds.setAttr(combined+f'.matrixIn[{index}]',*tuple(constant),type='matrix')
            cmds.connectAttr(combined+'.matrixSum',target+'.transform')
    cmds.disconnectAttr(output,first_input)
    cmds.disconnectAttr(tail,destination)
    cmds.disconnectAttr(source,node+'.inputMesh')
    cmds.connectAttr(pre+'.outputGeometry',first_input)
    cmds.connectAttr(tail,post+'.inputGeometry')
    cmds.connectAttr(post+'.outputGeometry',node+'.inputMesh')
    cmds.connectAttr(output,destination)
    cmds.dgdirty(node)
    if not cmds.getAttr(node+'.valid'):
        message=cmds.getAttr(node+'.status')
        cmds.disconnectAttr(output,destination);cmds.disconnectAttr(post+'.outputGeometry',node+'.inputMesh')
        cmds.disconnectAttr(pre+'.outputGeometry',first_input)
        cmds.connectAttr(source,node+'.inputMesh');cmds.connectAttr(output,first_input);cmds.connectAttr(tail,destination)
        cmds.delete(pre,post,*matrices);cmds.dgdirty(node)
        raise ValueError(message)
    return True

def prepare_ngons(node,result):
    """Add linear interior samples to polygon caps without smoothing the source."""
    if cmds.objExists(node+'.capsPrepared') and cmds.getAttr(node+'.capsPrepared'):return False
    fn=mesh(result);counts,_=fn.getVertices()
    caps=[result+f'.f[{i}]' for i,count in enumerate(counts) if count>4]
    if not caps:return False
    cmds.polyTriangulate(caps,constructionHistory=True)
    fn=mesh(result);counts,_=fn.getVertices()
    triangles=[result+f'.f[{i}]' for i,count in enumerate(counts) if count==3]
    if triangles:cmds.polySubdivideFacet(triangles,divisions=2,mode=0,constructionHistory=True)
    changed=absorb_topology_edits(node)
    if changed:
        if not cmds.objExists(node+'.capsPrepared'):cmds.addAttr(node,ln='capsPrepared',at='bool')
        cmds.setAttr(node+'.capsPrepared',True)
    return changed

@contextmanager
def chunk(name):
    cmds.undoInfo(openChunk=True,chunkName=name)
    try:yield
    finally:cmds.undoInfo(closeChunk=True)

def set_value(node,key,value):
    if key=='surfaceBinding' and not cmds.objExists(node+'.surfaceBinding'):
        cmds.addAttr(node,ln='surfaceBinding',dt='string')
    if key=='edgeMode' and not cmds.objExists(node+'.edgeMode'):
        cmds.addAttr(node,ln='edgeMode',at='bool',defaultValue=False)
    if key in ('anchor','normal','tangent','weights'):cmds.setAttr(node+'.'+key,*value,type='float3')
    elif key in ('ramp','surfaceBinding'):cmds.setAttr(node+'.'+key,value or '',type='string')
    else:cmds.setAttr(node+'.'+key,value)
    if key in ('edgeMode','surfaceBinding'):cmds.dgdirty(node)

def snapshot(node):
    return {key:('' if key=='surfaceBinding' else False) if key in ('edgeMode','surfaceBinding') and not cmds.objExists(node+'.'+key) else
        (cmds.getAttr(node+'.'+key)[0] if key in ('anchor','normal','tangent','weights') else cmds.getAttr(node+'.'+key)) for key in FIELDS}

def target_path(node):
    plug=cmds.connectionInfo(node+'.surface',sourceFromDestination=True)
    return plug.split('.worldMesh')[0] if plug else None

def fit_node(result):
    """Find the owner of this result, not a floater upstream on its target."""
    if cmds.objExists(result) and cmds.nodeType(result)=='mesh':
        parents=cmds.listRelatives(result,parent=True,fullPath=True) or []
        if parents:result=parents[0]
    if not cmds.objExists(result+'.message'):return None
    for plug in cmds.listConnections(result+'.message',s=False,d=True,plugs=True,type='modelPackSurfaceFit') or []:
        if plug.endswith('.result'):return plug.rsplit('.',1)[0]
    return None

def restore(node,state,target):
    old=cmds.connectionInfo(node+'.surface',sourceFromDestination=True)
    if old:cmds.disconnectAttr(old,node+'.surface')
    if target and cmds.objExists(target):cmds.connectAttr(target+'.worldMesh[0]',node+'.surface',force=True)
    for key,value in state.items():set_value(node,key,value)

def contact(node):
    target=target_path(node);c=om.MVector(cmds.getAttr(node+'.anchor')[0]);n=om.MVector(cmds.getAttr(node+'.normal')[0])
    if not target or cmds.getAttr(node+'.face')<0:
        if cmds.connectionInfo(node+'.inputMesh',sourceFromDestination=True):
            matrix=om.MMatrix(cmds.getAttr(node+'.sourceMatrix'))
            points=[p*matrix for p in input_mesh(node).getPoints()]
            axis=list(geo.AXES)[cmds.getAttr(node+'.axis')]
            return geo.support_frame(points,axis,matrix)
    if target and cmds.getAttr(node+'.face')>=0:
        fn=mesh(target);face=cmds.getAttr(node+'.face');tri=cmds.getAttr(node+'.triangle');w=cmds.getAttr(node+'.weights')[0]
        binding=cmds.getAttr(node+'.surfaceBinding') if cmds.objExists(node+'.surfaceBinding') else ''
        rebound=geo.rebound_contact(fn,binding)
        if rebound is not None:
            p,normal=rebound;matrix=fn.dagPath().inclusiveMatrix()
            c=om.MVector(p*matrix);n=(normal*matrix.inverse().transpose()).normal()
        else:
            ids=fn.getPolygonTriangleVertices(face,tri)
            c=sum((om.MVector(fn.getPoint(v,om.MSpace.kWorld))*w[i] for i,v in enumerate(ids)),om.MVector())
            n=sum((fn.getFaceVertexNormal(face,v,om.MSpace.kWorld)*w[i] for i,v in enumerate(ids)),om.MVector()).normal()
    u=om.MVector(cmds.getAttr(node+'.tangent')[0]);u=(u-n*(u*n)).normal()
    if u.length()<.5:u=(n^om.MVector(0,0,1)).normal()
    return c,n,u,n^u

def update_result_pivot(node,result):
    c,n,u,v=contact(node);point=c+n*cmds.getAttr(node+'.offset')
    axis=list(geo.AXES)[cmds.getAttr(node+'.axis')];su,sn,sv=geo.basis(axis)
    def matrix(a,b,c):return om.MMatrix((*a,0.,*b,0.,*c,0.,0.,0.,0.,1.))
    rotation=matrix(su,sn,sv).inverse()*matrix(u,n,v)
    values=list(rotation);values[12:15]=tuple(point);frame=om.MMatrix(values)
    if not cmds.objExists(node+'.outputFrame'):
        cmds.addAttr(node,ln='outputFrame',dt='matrix')
        cmds.setAttr(node+'.outputFrame',*tuple(om.MMatrix()),type='matrix')
    stored=om.MMatrix(cmds.getAttr(node+'.outputFrame'))
    actual=mesh(result).dagPath().inclusiveMatrix()
    pivot=om.MVector(cmds.xform(result,q=True,worldSpace=True,rotatePivot=True))
    if max(abs(a-b) for a,b in zip(stored,frame))>1e-9 or max(abs(a-b) for a,b in zip(actual,frame))>1e-9 or (pivot-point).length()>1e-8:
        cmds.setAttr(node+'.outputFrame',*tuple(frame),type='matrix');cmds.dgdirty(node)
        cmds.xform(result,objectSpace=True,pivots=(0,0,0),preserve=True)
        cmds.xform(result,worldSpace=True,matrix=tuple(frame))

def barycentric(fn,face,tri,point):
    ids=fn.getPolygonTriangleVertices(face,tri);a,b,c=[om.MVector(fn.getPoint(v,om.MSpace.kWorld)) for v in ids]
    v0=b-a;v1=c-a;v2=om.MVector(point)-a
    d00=v0*v0;d01=v0*v1;d11=v1*v1;d20=v2*v0;d21=v2*v1;den=d00*d11-d01*d01
    if abs(den)<1e-16:raise ValueError('Вырожденный полигон поверхности')
    w1=(d11*d20-d01*d21)/den;w2=(d00*d21-d01*d20)/den
    return (1-w1-w2,w1,w2)

class Ramp(QtWidgets.QWidget):
    changed=QtCore.Signal();started=QtCore.Signal();finished=QtCore.Signal()
    def __init__(self,parent=None):
        super().__init__(parent);self.knots=[[0.,1.],[1.,0.]];self.grip=None
        self.setMinimumHeight(90);self.setToolTip('Перетащите любую точку по горизонтали и вертикали. Щелчок добавляет точку; ПКМ удаляет внутреннюю точку.')
    def paintEvent(self,event):
        p=QtGui.QPainter(self);p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(),QtGui.QColor('#25292f'));p.setPen(QtGui.QColor('#59616b'))
        for i in range(1,4):p.drawLine(8,int(8+(self.height()-16)*i/4),self.width()-8,int(8+(self.height()-16)*i/4))
        points=[QtCore.QPointF(8+x*(self.width()-16),8+(1-y)*(self.height()-16)) for x,y in self.knots]
        p.setPen(QtGui.QPen(QtGui.QColor('#b3ed85'),2))
        interpolator = getattr(self, 'interpolator', None)
        if interpolator is None:
            line = [QtCore.QPointF(8,points[0].y())] + points + [QtCore.QPointF(self.width()-8,points[-1].y())]
        else:
            samples = sorted({i / 128 for i in range(129)} | {x for x, _ in self.knots})
            line = [QtCore.QPointF(8+x*(self.width()-16),
                                   8+(1-interpolator(x, self.knots))*(self.height()-16))
                    for x in samples]
        p.drawPolyline(QtGui.QPolygonF(line))
        p.setBrush(QtGui.QColor('#e9cd7b'))
        for point in points:p.drawEllipse(point,4,4)
        p.end()
    def mousePressEvent(self,event):
        x=max(0.,min(1.,(event.position().x()-8)/max(1,self.width()-16)));y=max(0.,min(1.,1-(event.position().y()-8)/max(1,self.height()-16)))
        if event.button() not in (QtCore.Qt.LeftButton,QtCore.Qt.RightButton):return
        self.started.emit();nearest=min(range(len(self.knots)),key=lambda i:
            ((self.knots[i][0]-x)*(self.width()-16))**2+((self.knots[i][1]-y)*(self.height()-16))**2)
        if event.button()==QtCore.Qt.RightButton:
            if 0<nearest<len(self.knots)-1:self.knots.pop(nearest);self.changed.emit();self.update()
            self.finished.emit();return
        near=((self.knots[nearest][0]-x)*(self.width()-16))**2+((self.knots[nearest][1]-y)*(self.height()-16))**2<=12**2
        if not near and all(abs(k[0]-x)>.001 for k in self.knots):
            self.knots.append([x,y]);self.knots.sort();nearest=self.knots.index([x,y])
        self.grip=nearest;self.mouseMoveEvent(event)
    def mouseMoveEvent(self,event):
        if self.grip is None:return
        i=self.grip;y=max(0.,min(1.,1-(event.position().y()-8)/max(1,self.height()-16)))
        low=self.knots[i-1][0]+.001 if i else 0.
        high=self.knots[i+1][0]-.001 if i<len(self.knots)-1 else 1.
        x=max(low,min(high,(event.position().x()-8)/max(1,self.width()-16)))
        self.knots[i]=[x,y];self.changed.emit();self.update()
    def mouseReleaseEvent(self,event):self.grip=None;self.finished.emit()

class Canvas(OccludedCanvas):
    def paint_frame(self,p):
        p.setCompositionMode(QtGui.QPainter.CompositionMode_Source);p.fillRect(self.rect(),QtCore.Qt.transparent)
        p.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver);p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setClipRegion(self.sync_occlusion());m=self.manager
        try:
            projection=overlay.Projection(m.view,self.width(),self.height());c,n,u,v=contact(m.node)
            c=c+n*cmds.getAttr(m.node+'.offset')
            radius=m.radius()*cmds.getAttr(m.node+'.scale')
            def point(value):
                x,y,visible=projection.point(om.MPoint(value));return QtCore.QPointF(x,self.height()-1-y)
            corners=[point(c+u*a*radius+v*b*radius) for a,b in ((-1,-1),(1,-1),(1,1),(-1,1))]
            colour=QtGui.QColor('#b3ed85' if cmds.getAttr(m.node+'.valid') else '#ed766f')
            fill=QtGui.QColor(colour);fill.setAlpha(30);p.setPen(QtGui.QPen(colour,1.5));p.setBrush(fill)
            polygon=QtGui.QPolygonF(corners);p.drawPolygon(polygon);center=point(c)
            m.handles=[('scale',QtCore.QRectF(center.x()-7,center.y()-7,14,14))];m.plane=polygon
            p.setBrush(QtGui.QColor('#e7c870'));p.drawRect(m.handles[0][1])
            def head(tip,previous,color):
                d=tip-previous;length=math.hypot(d.x(),d.y())
                if length<.1:return
                d=d/length;side=QtCore.QPointF(-d.y(),d.x());p.setBrush(color)
                p.drawPolygon(QtGui.QPolygonF([tip,tip-d*11+side*4.5,tip-d*11-side*4.5]))
            # Two double-ended arcs rotate around the mounting normal.
            for middle in (math.pi*.5,math.pi*1.5):
                arc=[point(c+(u*math.cos(a)+v*math.sin(a))*radius*1.5)
                     for a in (middle-.5+i/16 for i in range(17))]
                p.setPen(QtGui.QPen(colour,2));p.setBrush(QtCore.Qt.NoBrush)
                p.drawPolyline(QtGui.QPolygonF(arc));head(arc[-1],arc[-2],colour);head(arc[0],arc[1],colour)
                m.handles.append(('rotate',arc))
            # A screen-space fallback keeps normal arrows usable head-on.
            normal_tip=point(c+n*radius);direction=normal_tip-center;length=math.hypot(direction.x(),direction.y())
            direction=direction/length if length>=24 else QtCore.QPointF(.70710678,-.70710678)
            for sign,label in ((1,'offset+'),(-1,'offset-')):
                a=center+direction*(18*sign);b=center+direction*(72*sign);color=QtGui.QColor('#7acde4')
                p.setPen(QtGui.QPen(color,2));p.drawLine(a,b);head(b,a,color)
                p.drawText(b+QtCore.QPointF(5,-4),'+' if sign>0 else '−')
                m.handles.append((label,(a,b)))
        except (ValueError,RuntimeError):m.handles=[]

class Tool(QtCore.QObject):
    def __init__(self,window):
        super().__init__(window);self.window=window;self.drag=None;self.closed=False;self.canvas=None;self.view=None
        self.handles=[];self.plane=QtGui.QPolygonF();self.panel=None;self.callbacks=[];self.panel_chunk=False
        self.notice=None
        self.session_chunk=False;self.hidden_source=None;self.viewport_highlights={};self.records={}
        window._surface_fit_initializing=self
        selection=cmds.ls(sl=True,objectsOnly=True,long=True) or []
        if len(selection)!=1:raise ValueError('Surface Fit: выделите один polygon mesh.')
        fn=mesh(selection[0]);path=fn.dagPath();path.pop();selected=path.fullPathName()
        self.node=fit_node(selected)
        self.created=self.node is None;self.original_selection=om.MGlobal.getActiveSelectionList()
        if self.created:
            self.source=selected;self.visibility=cmds.getAttr(selected+'.visibility')
            plugin=str(Path(__file__).with_name('model_pack_surface_fit_node.py'))
            if not cmds.pluginInfo('model_pack_surface_fit_node',q=True,loaded=True):cmds.loadPlugin(plugin,quiet=True)
            if 'modelPackSurfaceFit' not in (cmds.pluginInfo('model_pack_surface_fit_node',q=True,dependNode=True) or []):
                raise RuntimeError('Не удалось загрузить узел Surface Fit.')
            cmds.undoInfo(openChunk=True,chunkName='Surface Fit');self.session_chunk=True
            with chunk('Surface Fit start'):
                # Never duplicate shape.pnts: outMesh already includes those edits.
                self.result=cmds.createNode('transform',name=selected.split('|')[-1]+'_surfaceFit')
                self.result=cmds.ls(self.result,long=True)[0]
                shape=cmds.createNode('mesh',parent=self.result,name=self.result.split('|')[-1]+'Shape')
                self.node=cmds.createNode('modelPackSurfaceFit')
                ensure_binding(self.node)
                cmds.connectAttr(fn.fullPathName()+'.outMesh',self.node+'.inputMesh')
                cmds.connectAttr(selected+'.worldMatrix[0]',self.node+'.sourceMatrix')
                cmds.connectAttr(self.node+'.outputMesh',shape+'.inMesh',force=True)
                cmds.addAttr(self.node,ln='cleanOutput',at='bool',defaultValue=True)
                mesh(self.result).getPoints();copy_materials(fn,self.result)
                for attr in ('displaySmoothMesh','smoothLevel','renderSmoothLevel','useSmoothPreviewForRender'):
                    if cmds.objExists(fn.fullPathName()+'.'+attr) and cmds.objExists(shape+'.'+attr) and cmds.getAttr(shape+'.'+attr,settable=True):
                        cmds.setAttr(shape+'.'+attr,cmds.getAttr(fn.fullPathName()+'.'+attr))
                for name,value in (('source',selected),('result',self.result)):
                    cmds.addAttr(self.node,ln=name,at='message');cmds.connectAttr(value+'.message',self.node+'.'+name)
                ensure_edge_mode(self.node)
                if prepare_ngons(self.node,self.result):set_value(self.node,'edgeMode',geo.has_open_boundary(input_mesh(self.node)))
                if cmds.getAttr(self.node+'.edgeMode'):set_value(self.node,'ramp',json.dumps(EDGE_RAMP))
                points=fn.getPoints(om.MSpace.kWorld);center=om.MVector((min(p.x for p in points)+max(p.x for p in points))*.5,min(p.y for p in points),(min(p.z for p in points)+max(p.z for p in points))*.5)
                set_value(self.node,'anchor',tuple(center));self.update_pivot()
                self.hide_source()
                cmds.select(self.result,r=True)
        else:
            self.source=cmds.listConnections(self.node+'.source',s=True,d=False)[0]
            self.result=selected;self.visibility=False
            cmds.undoInfo(openChunk=True,chunkName='Surface Fit');self.session_chunk=True
            with chunk('Surface Fit geometry'):
                absorb_topology_edits(self.node);prepare_ngons(self.node,self.result);self.hide_source()
        ensure_binding(self.node)
        self.initial=snapshot(self.node);self.initial_target=target_path(self.node)
        self.remember_current()
        ensure_edge_mode(self.node)
        self.last_edge_mode=cmds.getAttr(self.node+'.edgeMode')
        if not cmds.about(batch=True):cmds.setToolTo('selectSuperContext')
        self.sync_viewport_display()
        self.make_panel();self.callbacks=[om.MEventMessage.addEventCallback(name,lambda *_:self.sync()) for name in ('Undo','Redo','SelectionChanged')]
        self.callbacks += [om.MSceneMessage.addCallback(event,lambda *_:close(self.window))
                           for event in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen)]
        self.timer=QtCore.QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(33)
        QtWidgets.QApplication.instance().installEventFilter(self);self.tick()
        window._surface_fit_initializing=None
    def remember_current(self):
        self.records[self.node]={key:getattr(self,key) for key in ('node','result','source','created','initial','initial_target')}
    def recover_active(self):
        available=[node for node in self.records if all(cmds.objExists(node+'.'+key) for key in ('result','source','edgeMode','surfaceBinding'))
                   and (cmds.listConnections(node+'.result',s=True,d=False) or [])]
        if not available:return False
        selected=cmds.ls(sl=True,objectsOnly=True,long=True) or []
        owner=fit_node(selected[0]) if len(selected)==1 else None
        node=owner if owner in available else self.node if self.node in available else available[-1]
        record=self.records[node]
        for key,value in record.items():setattr(self,key,value)
        self.result=cmds.ls(cmds.listConnections(node+'.result',s=True,d=False)[0],long=True)[0]
        return True
    def duplicate_active(self):
        self.finish_drag();self.end_panel()
        if not self.recover_active():return
        original=self.node;previous_result=self.result;source=self.source
        node=None;result=None
        with chunk('Surface Fit duplicate'):
            absorb_topology_edits(original)
            try:
                result=cmds.createNode('transform',name=previous_result.split('|')[-1]+'_copy#')
                result=cmds.ls(result,long=True)[0]
                shape=cmds.createNode('mesh',parent=result,name=result.split('|')[-1]+'Shape')
                node=cmds.createNode('modelPackSurfaceFit')
                for name in ('inputMesh','sourceMatrix'):
                    plug=cmds.connectionInfo(original+'.'+name,sourceFromDestination=True)
                    if plug:cmds.connectAttr(plug,node+'.'+name)
                    elif name=='sourceMatrix':cmds.setAttr(node+'.sourceMatrix',*cmds.getAttr(original+'.sourceMatrix'),type='matrix')
                for name,value in (('source',source),('result',result)):
                    cmds.addAttr(node,ln=name,at='message');cmds.connectAttr(value+'.message',node+'.'+name)
                restore(node,snapshot(original),target_path(original))
                cmds.addAttr(node,ln='cleanOutput',at='bool',defaultValue=True)
                if cmds.objExists(original+'.capsPrepared'):
                    cmds.addAttr(node,ln='capsPrepared',at='bool',defaultValue=cmds.getAttr(original+'.capsPrepared'))
                cmds.connectAttr(node+'.outputMesh',shape+'.inMesh')
                update_result_pivot(node,result)
                fn=mesh(previous_result);copy_materials(fn,result)
                for attr in ('displaySmoothMesh','smoothLevel','renderSmoothLevel','useSmoothPreviewForRender'):
                    if cmds.objExists(fn.fullPathName()+'.'+attr):cmds.setAttr(shape+'.'+attr,cmds.getAttr(fn.fullPathName()+'.'+attr))
                for index,value in tweaks(previous_result).items():cmds.setAttr(shape+f'.pnts[{index}]',*value,type='float3')
                self.node=node;self.result=result;self.created=True
                self.initial=snapshot(node);self.initial_target=target_path(node);self.remember_current()
                cmds.select(result,r=True)
            except (RuntimeError,ValueError):
                if result and cmds.objExists(result):cmds.delete(result)
                if node and cmds.objExists(node):cmds.delete(node)
                self.node=original;self.recover_active();raise
        self.notice=None;self.sync()
    def abort_initialization(self):
        try:
            timer=getattr(self,'timer',None)
            if timer:timer.stop()
            QtWidgets.QApplication.instance().removeEventFilter(self)
            for callback in self.callbacks:om.MMessage.removeCallback(callback)
            if self.canvas:self.canvas.hide();self.canvas.deleteLater()
            self.restore_source();self.restore_viewport_display()
            if getattr(self,'created',False):
                result=getattr(self,'result',None);node=getattr(self,'node',None)
                if result and cmds.objExists(result):cmds.delete(result)
                if node and cmds.objExists(node):cmds.delete(node)
            if self.panel:self.panel.hide();self.window.layout().removeWidget(self.panel);self.panel.deleteLater()
        finally:
            if self.session_chunk:self.session_chunk=False;cmds.undoInfo(closeChunk=True)
            self.window._surface_fit_initializing=None
            self.closed=True;self.deleteLater()
    def hide_source(self):
        for plug in (self.source+'.visibility',mesh(self.source).fullPathName()+'.lodVisibility'):
            if cmds.getAttr(plug) and cmds.getAttr(plug,settable=True):
                self.hidden_source=(plug,cmds.getAttr(plug));cmds.setAttr(plug,False);return
    def restore_source(self):
        hidden=self.hidden_source;self.hidden_source=None
        if hidden and cmds.objExists(hidden[0]) and cmds.getAttr(hidden[0],settable=True):cmds.setAttr(hidden[0],hidden[1])
    def sync_viewport_display(self):
        # Suppress selected-object wire only. Wireframe on Shaded and the
        # viewport's normal wireframe mode remain controlled by Maya.
        for panel in cmds.getPanel(type='modelPanel') or []:
            editor=cmds.modelPanel(panel,q=True,modelEditor=True)
            if editor not in self.viewport_highlights:
                self.viewport_highlights[editor]=cmds.modelEditor(editor,q=True,selectionHiliteDisplay=True)
                viewport_flags(editor,selectionHiliteDisplay=False)
    def restore_viewport_display(self):
        for editor,value in self.viewport_highlights.items():
            if cmds.modelEditor(editor,exists=True):viewport_flags(editor,selectionHiliteDisplay=value)
        self.viewport_highlights.clear()
    def radius(self):
        matrix=om.MMatrix(cmds.getAttr(self.node+'.sourceMatrix'))
        p=[point*matrix for point in input_mesh(self.node).getPoints()];axis=list(geo.AXES)[cmds.getAttr(self.node+'.axis')]
        u,n,v=geo.source_frame(axis,matrix)
        values,_=geo.coordinates(p,axis,(u,n,v))
        return max(.01,max(max(abs(a),abs(b)) for a,_,b in values))
    def update_pivot(self):
        # Geometry is emitted in world coordinates under the result transform.
        # Moving its pivots with preserve=True does not move or deform vertices.
        update_result_pivot(self.node,self.result)
    def make_panel(self):
        panel=QtWidgets.QWidget(self.window);self.panel=panel;panel.setObjectName('modelPackSurfaceFitSettings')
        layout=QtWidgets.QVBoxLayout(panel);layout.setContentsMargins(0,6,0,0)
        group=QtWidgets.QButtonGroup(panel);self.axes=[]
        row=QtWidgets.QHBoxLayout()
        for index,name in enumerate(('X','Y','Z')):
            b=QtWidgets.QPushButton(name);b.setCheckable(True);group.addButton(b);self.axes.append(b);row.addWidget(b)
            b.setToolTip('Выбрать ось. Повторное нажатие меняет знак: '+name+' ↔ −'+name)
            b.clicked.connect(lambda checked=False,i=index:self.select_axis(i))
        layout.addLayout(row)
        self.scale=QtWidgets.QDoubleSpinBox();self.scale.setRange(.01,100);self.scale.setDecimals(3);self.scale.setSingleStep(.05);self.scale.setKeyboardTracking(False)
        self.offset=QtWidgets.QDoubleSpinBox();self.offset.setRange(-1000,1000);self.offset.setDecimals(4);self.offset.setSingleStep(.01);self.offset.setKeyboardTracking(False)
        row=QtWidgets.QHBoxLayout();row.setSpacing(5)
        for label,widget in (('Scale',self.scale),('Gap',self.offset)):
            row.addWidget(QtWidgets.QLabel(label));widget.setMinimumWidth(0)
            widget.setSizePolicy(QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Fixed)
            row.addWidget(widget,1)
        self.offset.setToolTip('Зазор между мешем и поверхностью, в сантиметрах.');layout.addLayout(row)
        self.scale.valueChanged.connect(lambda value:self.setting('scale',value));self.offset.valueChanged.connect(lambda value:self.setting('offset',value))
        self.edge_checkbox=QtWidgets.QCheckBox('From Open Edge',panel)
        self.edge_checkbox.setToolTip('Прилегание от граничных рёбер к внутренней части меша. Выключите для градиента по высоте.')
        self.edge_checkbox.toggled.connect(lambda value:self.setting('edgeMode',value));layout.addWidget(self.edge_checkbox)
        self.gradient_label=QtWidgets.QLabel();layout.addWidget(self.gradient_label)
        self.ramp=Ramp(panel);layout.addWidget(self.ramp)
        self.ramp.started.connect(self.begin_panel);self.ramp.changed.connect(lambda:self.setting('ramp',json.dumps(self.ramp.knots)))
        self.ramp.finished.connect(self.end_panel)
        update=QtWidgets.QPushButton('Update Geometry',panel)
        update.setToolTip('Учесть новые разрезы в деформации. Также обновляется при следующем изменении параметров или размещения.')
        update.clicked.connect(lambda:self.setting('scale',cmds.getAttr(self.node+'.scale')));layout.addWidget(update)
        self.status=QtWidgets.QLabel('Плоскость — снап; дуги — поворот; голубые стрелки — зазор; центр — масштаб');self.status.setWordWrap(True);layout.addWidget(self.status)
        row=QtWidgets.QHBoxLayout()
        for label,cancel in (('Apply',False),('Cancel',True)):
            b=QtWidgets.QPushButton(label);b.clicked.connect(lambda checked=False,c=cancel:close(self.window,c));row.addWidget(b)
        layout.addLayout(row);self.window.layout().addWidget(panel,self.window._panel_row,0,1,2);panel.show();self.sync();self.window.adjustSize()
    def sync(self):
        reset_timer = getattr(self.window, '_scene_reset_timer', None)
        if self.closed or (reset_timer is not None and reset_timer.isActive()) or not self.recover_active():return
        for widget,key in ((self.scale,'scale'),(self.offset,'offset')):
            block=QtCore.QSignalBlocker(widget);widget.setValue(cmds.getAttr(self.node+'.'+key));del block
        axis=cmds.getAttr(self.node+'.axis')
        for i,button in enumerate(self.axes):
            active=axis%3==i;button.setChecked(active);button.setText(('−' if active and axis>=3 else '')+'XYZ'[i])
        edge=cmds.getAttr(self.node+'.edgeMode')
        if edge!=getattr(self,'last_edge_mode',edge):cmds.dgdirty(self.node)
        self.last_edge_mode=edge
        block=QtCore.QSignalBlocker(self.edge_checkbox);self.edge_checkbox.setChecked(edge);del block
        available=geo.has_open_boundary(input_mesh(self.node));self.edge_checkbox.setEnabled(available or edge)
        self.gradient_label.setText('Градиент: край → центр' if edge and available else 'Градиент: основание → верх')
        self.ramp.setToolTip('Слева — открытый край, справа — внутренняя часть. Перетащите точки кривой.' if edge and available else
            'Слева — основание, справа — верх. Перетащите точки кривой.')
        self.ramp.knots=json.loads(cmds.getAttr(self.node+'.ramp'));self.ramp.update()
    def select_axis(self,index):
        current=cmds.getAttr(self.node+'.axis')
        self.setting('axis',(current+3)%6 if current%3==index else index)
    def begin_panel(self):
        if not cmds.objExists(self.node):return
        if not self.panel_chunk:
            self.panel_state=snapshot(self.node);self.panel_chunk=True;cmds.undoInfo(openChunk=True,chunkName='Surface Fit gradient')
    def end_panel(self):
        if self.panel_chunk:
            self.panel_chunk=False;cmds.undoInfo(closeChunk=True)
    def setting(self,key,value):
        if self.closed:return
        self.recover_active()
        if not cmds.objExists(self.node):
            self.inform('Создание отменено. Сначала выполните Redo.');return
        old=cmds.getAttr(self.node+'.'+key)
        with chunk('Surface Fit '+key):
            set_value(self.node,key,value)
            if key=='edgeMode' and value and not old and json.loads(cmds.getAttr(self.node+'.ramp'))==[[0,1],[1,0]]:
                set_value(self.node,'ramp',json.dumps(EDGE_RAMP))
            try:absorb_topology_edits(self.node)
            except ValueError as error:self.inform(str(error))
            if not cmds.getAttr(self.node+'.valid'):
                message=cmds.getAttr(self.node+'.status');set_value(self.node,key,old);self.inform(message)
            self.update_pivot()
        self.sync()
    def inform(self,message):
        self.notice=(time.monotonic()+2.,message);self.status.setText(message)
    def tick(self):
        reset_timer = getattr(self.window, '_scene_reset_timer', None)
        if self.closed or (reset_timer is not None and reset_timer.isActive()):return
        self.recover_active()
        self.sync_viewport_display()
        if not cmds.objExists(self.node):
            if self.canvas:self.canvas.hide()
            self.status.setText('Создание отменено. Redo вернёт флоатер; Завершить закроет сеанс.')
            return
        try:
            view=omui.M3dView.active3dView();parent=wrapInstance(int(view.widget()),QtWidgets.QWidget)
            if self.canvas is None or self.canvas.viewport!=parent:
                if self.canvas:self.canvas.hide();self.canvas.deleteLater()
                self.view=view;self.canvas=Canvas(parent,self);self.canvas.setObjectName('modelPackSurfaceFitOverlay')
            self.canvas.setGeometry(QtCore.QRect(parent.mapToGlobal(QtCore.QPoint()),parent.size()))
            self.canvas.setVisible(parent.isVisible());self.canvas.sync_occlusion();self.canvas.update()
            self.status.setText(self.notice[1] if self.notice and self.notice[0]>time.monotonic() else cmds.getAttr(self.node+'.status'))
        except RuntimeError:pass
    def pick(self,point,target=None):
        origin,direction=om.MPoint(),om.MVector();self.view.viewToWorld(int(point.x()*self.view.portWidth()/self.canvas.width()),int((self.canvas.height()-1-point.y())*self.view.portHeight()/self.canvas.height()),origin,direction)
        names=[target] if target else cmds.ls(type='mesh',dag=True,long=True,noIntermediate=True,allPaths=True) or []
        ignored=[mesh(self.source).object(),mesh(self.result).object()]
        for node in self.records:
            if cmds.objExists(node):
                for result in cmds.listConnections(node+'.result',s=True,d=False) or []:ignored.append(mesh(result).object())
        best=None
        for name in names:
            fn=mesh(name)
            if fn.object() in ignored or not fn.dagPath().isVisible():continue
            hit=fn.closestIntersection(om.MFloatPoint(origin),om.MFloatVector(direction),om.MSpace.kWorld,1e7,False,accelParams=fn.autoUniformGridParams())
            if hit is not None and (best is None or hit[1]<best[1][1]):best=(fn,hit)
        return best
    def place(self,hit):
        before=snapshot(self.node);before_target=target_path(self.node)
        fn,info=hit;w=barycentric(fn,info[2],info[3],info[0]);ids=fn.getPolygonTriangleVertices(info[2],info[3])
        n=sum((fn.getFaceVertexNormal(info[2],v,om.MSpace.kWorld)*w[i] for i,v in enumerate(ids)),om.MVector()).normal()
        _,oldn,u,_=contact(self.node)
        u=u.rotateBy(om.MQuaternion(oldn,n));u=(u-n*(u*n)).normal()
        plug=fn.fullPathName()+'.worldMesh[0]';old=cmds.connectionInfo(self.node+'.surface',sourceFromDestination=True)
        if old!=plug:
            if old:cmds.disconnectAttr(old,self.node+'.surface')
            cmds.connectAttr(plug,self.node+'.surface',force=True)
        for key,value in (('face',info[2]),('triangle',info[3]),('weights',w),('anchor',tuple(info[0])[:3]),('normal',tuple(n)),('tangent',tuple(u))):set_value(self.node,key,value)
        set_value(self.node,'surfaceBinding',geo.surface_binding(fn,om.MPoint(info[0])*fn.dagPath().inclusiveMatrixInverse()))
        if not cmds.getAttr(self.node+'.valid'):
            message=cmds.getAttr(self.node+'.status');restore(self.node,before,before_target);self.inform(message)
            return False
        self.update_pivot()
        self.notice=None
        return True
    def begin_drag(self,kind,point):
        self.end_panel();self.drag={'kind':kind,'point':point,'state':snapshot(self.node),'target':target_path(self.node),'frame':contact(self.node)}
        cmds.undoInfo(openChunk=True,chunkName='Surface Fit '+kind)
        absorb_topology_edits(self.node)
        c,n,u,v=self.drag['frame'];c=c+n*self.drag['state']['offset'];self.drag['frame']=(c,n,u,v)
        if kind=='rotate':
            self.drag['radial']=self.plane_radial(point,c,n);self.drag['last_angle']=0.;self.drag['angle']=0.
        if kind.startswith('offset'):
            projection=overlay.Projection(self.view,self.canvas.width(),self.canvas.height())
            def pixel(value):
                x,y,_=projection.point(om.MPoint(value));return QtCore.QPointF(x,self.canvas.height()-1-y)
            origin=pixel(c);normal=pixel(c+n)-origin;normal_length=math.hypot(normal.x(),normal.y())
            radius=self.radius()*self.drag['state']['scale']
            if normal_length*radius>=24:direction=normal/normal_length;unit=1/max(normal_length,1e-8)
            else:
                direction=QtCore.QPointF(.70710678,-.70710678)
                a=pixel(c+u)-origin;b=pixel(c+v)-origin
                unit=1/max(math.hypot(a.x(),a.y()),math.hypot(b.x(),b.y()),1e-8)
            self.drag['offset_direction']=direction;self.drag['offset_unit']=unit
    def plane_radial(self,point,center,normal):
        origin,direction=om.MPoint(),om.MVector()
        self.view.viewToWorld(int(point.x()*self.view.portWidth()/self.canvas.width()),
            int((self.canvas.height()-1-point.y())*self.view.portHeight()/self.canvas.height()),origin,direction)
        denominator=direction*normal
        if abs(denominator)<1e-8:return None
        radial=om.MVector(origin)+direction*(((center-om.MVector(origin))*normal)/denominator)-center
        return radial.normal() if radial.length()>1e-9 else None
    def gesture_setting(self,key,value):
        previous=cmds.getAttr(self.node+'.'+key)
        if key=='tangent':previous=previous[0]
        set_value(self.node,key,value)
        if not cmds.getAttr(self.node+'.valid'):
            message=cmds.getAttr(self.node+'.status');set_value(self.node,key,previous);self.inform(message)
        self.update_pivot()
    def move_drag(self,point):
        d=self.drag
        if d['kind']=='scale':
            delta=(point.x()-d['point'].x())-(point.y()-d['point'].y())
            self.gesture_setting('scale',max(.01,min(100.,d['state']['scale']*math.exp(delta*.008))))
        elif d['kind']=='rotate':
            c,n,u,v=d['frame'];radial=self.plane_radial(point,c,n)
            if d['radial'] is not None and radial is not None:
                angle=math.atan2(n*(d['radial']^radial),d['radial']*radial)
                d['angle']+=(angle-d['last_angle']+math.pi)%(2*math.pi)-math.pi;d['last_angle']=angle
            else:d['angle']=((point.x()-d['point'].x())-(point.y()-d['point'].y()))*.01
            self.gesture_setting('tangent',tuple(u.rotateBy(om.MQuaternion(d['angle'],n))))
        elif d['kind'].startswith('offset'):
            delta=point-d['point'];direction=d['offset_direction']
            value=d['state']['offset']+(delta.x()*direction.x()+delta.y()*direction.y())*d['offset_unit']
            self.gesture_setting('offset',max(-1000.,min(1000.,value)))
        else:
            position=point
            hit=self.pick(position,d['target'] if d['kind']!='free' else None)
            if hit:self.notice=None;self.place(hit)
            else:self.inform('Нет подходящей поверхности. Сохранено последнее положение.')
        self.sync();cmds.refresh(currentView=True)
    def finish_drag(self,cancel=False):
        if self.drag is None:return
        d=self.drag;self.drag=None
        try:
            if cancel or not cmds.getAttr(self.node+'.valid'):
                restore(self.node,d['state'],d['target']);self.update_pivot()
        finally:cmds.undoInfo(closeChunk=True)
        self.sync()
    def eventFilter(self,obj,event):
        if self.closed:return False
        kind=event.type()
        try:
            if kind in (QtCore.QEvent.KeyPress,QtCore.QEvent.ShortcutOverride):
                focus=QtWidgets.QApplication.focusWidget()
                editing=isinstance(focus,(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox))
                mods=event.modifiers()
                duplicate=event.key()==QtCore.Qt.Key_D and mods==QtCore.Qt.ControlModifier
                if not editing and duplicate:
                    if kind==QtCore.QEvent.KeyPress and not event.isAutoRepeat():self.duplicate_active()
                    event.accept();return True
                undo=event.matches(QtGui.QKeySequence.Undo) or (event.key()==QtCore.Qt.Key_Z and not mods)
                redo=event.matches(QtGui.QKeySequence.Redo) or (event.key()==QtCore.Qt.Key_Z and mods==QtCore.Qt.ShiftModifier)
                if not editing and (undo or redo):
                    if kind==QtCore.QEvent.KeyPress:
                        self.finish_drag();self.end_panel()
                        cmds.undo() if undo else cmds.redo();self.sync()
                    event.accept();return True
            if kind==QtCore.QEvent.ApplicationDeactivate:self.finish_drag()
            if kind==QtCore.QEvent.KeyPress:
                if event.key()==QtCore.Qt.Key_Escape:
                    if self.drag:self.finish_drag(True)
                    else:close(self.window,True)
                    return True
                if event.key() in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter) and self.drag is None:
                    if isinstance(QtWidgets.QApplication.focusWidget(),(QtWidgets.QLineEdit,QtWidgets.QAbstractSpinBox)):return False
                    close(self.window);return True
            if kind not in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.MouseMove,QtCore.QEvent.MouseButtonRelease):return False
            if not cmds.objExists(self.node):return False
            if self.canvas is None:return False
            point=QtCore.QPointF(self.canvas.viewport.mapFromGlobal(event.globalPosition().toPoint()))
            if self.drag:
                if kind==QtCore.QEvent.MouseMove:self.move_drag(point);return True
                if kind==QtCore.QEvent.MouseButtonRelease and event.button()==QtCore.Qt.LeftButton:self.finish_drag();return True
            if kind!=QtCore.QEvent.MouseButtonPress or event.button()!=QtCore.Qt.LeftButton or event.modifiers()&QtCore.Qt.AltModifier:return False
            parent=self.canvas.viewport
            widget_target=isinstance(obj,QtWidgets.QWidget) and (obj==parent or parent.isAncestorOf(obj))
            native_target=isinstance(obj,QtGui.QWindow) and obj in (parent.windowHandle(),parent.window().windowHandle())
            if not (widget_target or native_target):return False
            if not parent.rect().contains(point.toPoint()):return False
            for name,shape in self.handles:
                segments=list(zip(shape,shape[1:])) if name=='rotate' else [shape] if name!='scale' else []
                yes=shape.contains(point) if name=='scale' else any(distance((point.x(),point.y()),(a.x(),a.y()),(b.x(),b.y()))<9 for a,b in segments)
                if yes:self.begin_drag(name,point);return True
            self.begin_drag('free',point)
            if not self.plane.containsPoint(point,QtCore.Qt.OddEvenFill):self.move_drag(point)
            return True
        except (ValueError,RuntimeError) as error:
            self.finish_drag(True);self.inform(str(error))
        return False
    def dispose(self,cancel=False):
        if self.closed:return
        self.finish_drag(cancel);self.end_panel();self.closed=True;self.timer.stop()
        QtWidgets.QApplication.instance().removeEventFilter(self)
        for callback in self.callbacks:om.MMessage.removeCallback(callback)
        if self.canvas:self.canvas.hide();self.canvas.deleteLater();self.canvas=None
        self.restore_viewport_display()
        if cancel:
            with chunk('Cancel Surface Fit'):
                for node,record in reversed(list(self.records.items())):
                    if not cmds.objExists(node):continue
                    results=cmds.listConnections(node+'.result',s=True,d=False) or []
                    if record['created']:
                        if results:cmds.delete(results[0])
                        if cmds.objExists(node):cmds.delete(node)
                    else:
                        restore(node,record['initial'],record['initial_target'])
                        if results:update_result_pivot(node,results[0])
            om.MGlobal.setActiveSelectionList(self.original_selection)
        with chunk('Surface Fit finish'):self.restore_source()
        if self.session_chunk:
            self.session_chunk=False;cmds.undoInfo(closeChunk=True)
        if self.panel:self.panel.hide();self.window.layout().removeWidget(self.panel);self.panel.deleteLater()
        self.window._surface_fit=None
        if self.window._active_tool=='surface_fit':self.window._active_tool=None
        if hasattr(self.window,'surface_fit_button'):self.window.surface_fit_button.setChecked(False)
        self.window.adjustSize();self.deleteLater()

def close(window,cancel=False):
    tool=getattr(window,'_surface_fit',None)
    if tool:tool.dispose(cancel)

def toggle(window):
    if getattr(window,'_surface_fit',None):close(window);return
    window._prepare_tool('surface_fit')
    try:
        refresh_nodes()
        window._surface_fit=Tool(window)
        window.surface_fit_button.setChecked(True)
    except (ValueError,RuntimeError) as error:
        building=getattr(window,'_surface_fit_initializing',None)
        if building:building.abort_initialization()
        window._active_tool=None;cmds.warning(str(error));QtWidgets.QMessageBox.warning(window,'Surface Fit',str(error))
