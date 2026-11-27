"""Persistent native 2D curves. Their edited boundary is the source of truth."""
import json
import math
from maya import cmds
from maya.api import OpenMaya as om
from . import core
from .preview import TURQUOISE

TAG='modelPackDraftPattern'
TARGET='draftTarget'
OWNED='modelPackDraftOwned'

def mark_created(name):
    if not cmds.attributeQuery(OWNED,node=name,exists=True):cmds.addAttr(name,longName=OWNED,attributeType='bool')
    cmds.setAttr(name+'.'+OWNED,True)

_LAST=globals().get('_LAST')


def path(uuid):
    names=cmds.ls(uuid,long=True) or []
    return names[0] if names else None


def choose(uuid):
    global _LAST
    if uuid and path(uuid):_LAST=uuid
    return _LAST


def all_patterns():
    result=[]
    for name in cmds.ls('*.'+TAG,objectsOnly=True,long=True) or []:
        if cmds.nodeType(name)=='transform':result.append((cmds.ls(name,uuid=True)[0],name))
    return result


def curve_node(name):
    """Keep a shape identity when a transform owns several visible curves."""
    dag=core.path_for(name)
    if dag.node().hasFn(om.MFn.kNurbsCurve):
        parent=om.MDagPath(dag);parent.pop()
        shapes=cmds.listRelatives(parent.fullPathName(),shapes=True,noIntermediate=True,type='nurbsCurve',fullPath=True) or []
        return parent.fullPathName() if len(shapes)==1 else dag.fullPathName()
    if not dag.node().hasFn(om.MFn.kTransform):return None
    shapes=cmds.listRelatives(name,shapes=True,noIntermediate=True,type='nurbsCurve',fullPath=True) or []
    if len(shapes)>1:raise ValueError('У объекта несколько кривых. Выберите нужный shape в Outliner.')
    return dag.fullPathName() if shapes else None


def all_curves():
    result={}
    for shape in cmds.ls(dag=True,type='nurbsCurve',long=True,noIntermediate=True) or []:
        name=curve_node(shape);result[cmds.ls(name,uuid=True)[0]]=name
    return list(result.items())


def selected():
    values=[]
    for name in cmds.ls(sl=True,objectsOnly=True,long=True) or []:
        name=curve_node(name)
        if name:
            uuid=cmds.ls(name,uuid=True)[0]
            if uuid not in values:values.append(uuid)
    if len(values)>1:raise ValueError('Выберите одну кривую для выдавливания.')
    return choose(values[0]) if values else None


def selected_target():
    values=[]
    for name in cmds.ls(sl=True,objectsOnly=True,long=True) or []:
        dag=core.path_for(name)
        if dag.node().hasFn(om.MFn.kTransform):
            shapes=cmds.listRelatives(name,shapes=True,noIntermediate=True,type='mesh',fullPath=True) or []
        else:shapes=[name] if dag.node().hasFn(om.MFn.kMesh) else []
        for shape in shapes:
            if shape not in values:values.append(shape)
    if len(values)>1:raise ValueError('Для вычитания выберите кривую и один меш.')
    return values[0] if values else None


def current():
    chosen=selected()
    if chosen:return chosen
    if _LAST and path(_LAST):return _LAST
    values=all_patterns()
    if len(values)==1:return choose(values[0][0])
    raise ValueError('Выберите кривую Maya или укажите её в списке Scene Curves.')


def _description(footprint):
    width,length,x,z=footprint.dimensions();frame=footprint.frame
    center=frame.point(x,0,z)
    matrix=list(frame.u)+[0.]+list(frame.normal)+[0.]+list(frame.v)+[0.]+[center.x,center.y,center.z,1.]
    data=dict(version=1,shape=footprint.shape,width=float(width),length=float(length),segments=int(footprint.segments))
    if footprint.shape=='cylinder':
        radius=width*.5
        points=[(radius*math.cos(i*math.tau/footprint.segments),0.,radius*math.sin(i*math.tau/footprint.segments))
                for i in range(footprint.segments)]
    else:points=[(-width*.5,0.,-length*.5),(width*.5,0.,-length*.5),(width*.5,0.,length*.5),(-width*.5,0.,length*.5)]
    return data,points+points[:1],matrix


def target(uuid):
    name=path(uuid)
    values=cmds.listConnections(name+'.'+TARGET,s=True,d=False,shapes=True) if name and cmds.objExists(name+'.'+TARGET) else None
    return (cmds.ls(values[0],long=True) or [None])[0] if values else None


def retarget(uuid,mesh):
    name=path(uuid)
    if not name or not mesh or not cmds.objExists(mesh):return
    dag=core.path_for(mesh)
    if dag.node().hasFn(om.MFn.kTransform):dag.extendToShape()
    if not dag.node().hasFn(om.MFn.kMesh):return
    if not cmds.attributeQuery(TARGET,node=name,exists=True):cmds.addAttr(name,longName=TARGET,attributeType='message')
    destination=name+'.'+TARGET;source=dag.fullPathName()+'.message'
    if not cmds.isConnected(source,destination):cmds.connectAttr(source,destination,force=True)


def save(footprint,mesh=None,uuid=None,select=True):
    if not footprint.valid:raise ValueError('Нарисуйте непустое основание паттерна.')
    if getattr(footprint,'from_curve',False):
        if not uuid or not path(uuid):raise ValueError('Исходная кривая паттерна удалена.')
        # Resuming, cancelling or applying depth must never replace edited CVs,
        # rebuild history, degree, weights, transforms or native curve knots.
        choose(uuid)
        if select:select_pattern(uuid)
        return uuid
    data,points,matrix=_description(footprint)
    name=path(uuid) if uuid else None
    text=json.dumps(data,separators=(',',':'))
    # Leaving an unchanged resumed pattern must not add another Undo command.
    unchanged=(name and cmds.getAttr(name+'.'+TAG)==text and
               max(abs(a-b) for a,b in zip(cmds.xform(name,q=True,ws=True,matrix=True),matrix))<1e-10)
    if not unchanged:
        selection=cmds.ls(sl=True,long=True) or []
        with core.transaction('Save Pattern'):
            values=[tuple(core.ui_distance(v) for v in p) for p in points]
            if name:cmds.curve(name,replace=True,point=values,degree=1)
            else:
                name=cmds.curve(name='fdPattern#',point=values,degree=1,
                                periodic=True,knot=list(range(len(values))))
                cmds.addAttr(name,longName=TAG,dataType='string')
                cmds.addAttr(name,longName=TARGET,attributeType='message')
                uuid=cmds.ls(name,uuid=True)[0]
                mark_created(name)
            cmds.setAttr(name+'.'+TAG,text,type='string')
            cmds.xform(name,worldSpace=True,matrix=matrix)
            shape=cmds.listRelatives(name,shapes=True,noIntermediate=True,fullPath=True)[0]
            for attr,value in (('overrideEnabled',True),('overrideRGBColors',True)):
                cmds.setAttr(shape+'.'+attr,value)
            cmds.setAttr(shape+'.overrideColorRGB',*TURQUOISE,type='double3')
            if cmds.attributeQuery('alwaysDrawOnTop',node=shape,exists=True):cmds.setAttr(shape+'.alwaysDrawOnTop',True)
            retarget(uuid,mesh)
            if not select:
                if selection:cmds.select(selection,replace=True)
                else:cmds.select(clear=True)
    choose(uuid)
    if select:select_pattern(uuid)
    return uuid


def select_pattern(uuid=None):
    uuid=uuid or current();name=path(uuid)
    if not name:raise ValueError('Сохранённый паттерн удалён.')
    cmds.selectMode(object=True);cmds.select(name,replace=True);choose(uuid)
    return name


def read(uuid=None,mesh=None,normal=None):
    uuid=uuid or current();name=path(uuid)
    if not name:raise ValueError('Сохранённый паттерн удалён.')
    data=None
    if cmds.attributeQuery(TAG,node=name,exists=True):
        data=json.loads(cmds.getAttr(name+'.'+TAG))
        if data.get('version')!=1:raise ValueError('Неизвестный формат 2D-паттерна.')
    mesh=mesh or target(uuid)
    if mesh and data is None:
        dag=core.path_for(name)
        center=om.MFnDagNode(dag).boundingBox.center*dag.inclusiveMatrix()
        normal=om.MFnMesh(core.path_for(mesh)).getClosestPointAndNormal(center,om.MSpace.kWorld)[1]
    from .curve_profile import read as read_profile
    return read_profile(name,data,normal),mesh


def edge_direction():
    selection=om.MGlobal.getActiveSelectionList();vectors=[]
    for i in range(selection.length()):
        try:dag,component=selection.getComponent(i)
        except RuntimeError:continue
        if component.isNull() or component.apiType()!=om.MFn.kMeshEdgeComponent:continue
        fn=om.MFnMesh(dag);points=fn.getPoints(om.MSpace.kWorld)
        for edge in om.MFnSingleIndexedComponent(component).getElements():
            a,b=fn.getEdgeVertices(edge);vector=points[b]-points[a]
            if vector.length()>core.EPSILON:vectors.append(vector)
    if not vectors:raise ValueError('Выберите ребро для выравнивания паттерна.')
    seed=max(vectors,key=lambda vector:vector.length()).normal()
    if any(abs(vector.normal()*seed)<.999 for vector in vectors):
        raise ValueError('Выберите одно ребро или несколько параллельных рёбер.')
    return sum((v if v*seed>=0 else -v for v in vectors),om.MVector()).normal()


def align(uuid=None,in_plane=True):
    uuid=uuid or current();name=path(uuid);direction=edge_direction()
    footprint,_=read(uuid);normal=footprint.frame.normal
    if in_plane:
        direction-=normal*(direction*normal)
        if direction.length()<core.EPSILON:raise ValueError('Ребро перпендикулярно плоскости паттерна. Отключите Keep Drawing Plane для 3D-выравнивания.')
        direction.normalize()
    dag=core.path_for(name);dag.extendToShape();curve=om.MFnNurbsCurve(dag)
    points=[om.MPoint(p) for p in curve.cvPositions(om.MSpace.kWorld)]
    sides=[b-a for a,b in zip(points,points[1:]) if (b-a).length()>core.EPSILON]
    side=max(sides,key=lambda side:abs(side.normal()*direction)).normal()
    if side*direction<0:direction=-direction
    if in_plane:
        angle=math.atan2(normal*(side^direction),side*direction)
        rotation=om.MQuaternion(angle,normal)
    else:rotation=om.MQuaternion(side,direction)
    matrix=list(core.path_for(name).inclusiveMatrix())
    for row in range(3):
        vector=om.MVector(*matrix[row*4:row*4+3]).rotateBy(rotation)
        matrix[row*4:row*4+3]=list(vector)
    with core.transaction('Align Pattern to Edge'):cmds.xform(name,worldSpace=True,matrix=matrix)
    choose(uuid);return name
