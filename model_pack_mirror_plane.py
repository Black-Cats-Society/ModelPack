"""World-space reflection frames shared by the viewport and mirror histories."""
import math
from maya import cmds
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn

POSE='modelPackPlaneMatrix'
ENABLED='modelPackPlaneEnabled'
WORLD='modelPackPlaneMeshMatrix'
DIRECTION='modelPackDirection'


def fields(node):
    return ('axis','space') if cmds.nodeType(node)=='modelPackMirrorCut' else ('modelPackAxis','modelPackSpace')


def source(node):
    from model_pack_modifiers import related
    return related(node,'modelPackSource')


def direction(node):
    attr='direction' if cmds.nodeType(node)=='modelPackMirrorCut' else DIRECTION
    return int(cmds.getAttr(node+'.'+attr)) if cmds.objExists(node+'.'+attr) else 0


def prepare(node):
    if cmds.nodeType(node) not in ('modelPackMirrorCut','multMatrix'):return False
    if not cmds.objExists(node+'.'+POSE):
        cmds.addAttr(node,ln=POSE,dt='matrix',hidden=True)
        cmds.addAttr(node,ln=ENABLED,at='bool',defaultValue=False,hidden=True)
        cmds.setAttr(node+'.'+POSE,*list(frame(node)),type='matrix')
    if cmds.nodeType(node)=='modelPackMirrorCut':
        if not cmds.objExists(node+'.'+WORLD):cmds.addAttr(node,ln=WORLD,dt='matrix',hidden=True)
        destination=node+'.'+WORLD;mesh=source(node)
        if not cmds.isConnected(mesh+'.worldMatrix[0]',destination):
            cmds.connectAttr(mesh+'.worldMatrix[0]',destination,force=True)
    return True


def active(node):return cmds.objExists(node+'.'+ENABLED) and bool(cmds.getAttr(node+'.'+ENABLED))


def frame(node):
    if active(node):return om.MMatrix(cmds.getAttr(node+'.'+POSE))
    mesh=source(node);world=_mesh_fn(mesh).dagPath().inclusiveMatrix()
    axis_attr,space_attr=fields(node);axis=cmds.getAttr(node+'.'+axis_attr);space=cmds.getAttr(node+'.'+space_attr)
    if cmds.nodeType(node)=='modelPackMirrorCut':
        matrix=om.MMatrix(cmds.getAttr(node+'.meshMatrix'));pivot=om.MPoint(*cmds.getAttr(node+'.pivot')[0])
        if space and cmds.objExists(node+'.pivotFrameMatrix'):
            rotation=om.MMatrix(cmds.getAttr(node+'.pivotFrameMatrix'))
            sp=om.MVector(*cmds.getAttr(node+'.scalePivot')[0]);st=om.MVector(*cmds.getAttr(node+'.scalePivotTranslate')[0])
            translation=(-sp)*matrix+(sp+st)*rotation
            values=list(matrix);values[12:15]=list(translation);matrix=om.MMatrix(values)
            pivot*=rotation
        elif space:pivot*=matrix
        result=matrix.inverse()*world
        center=pivot*result
    elif space:
        result=om.MMatrix(world)
        # Match the actual local reflection matrix, including its stored pivot.
        reflect=cmds.getAttr(node+'.matrixIn[0]')
        path=_mesh_fn(mesh).dagPath();path.pop()
        pivot=om.MFnTransform(path).rotatePivot(om.MSpace.kWorld)*world.inverse()
        pivot[axis]=reflect[12+axis]*.5;center=pivot*world
    else:
        result=om.MMatrix();center=om.MPoint()
    if not space:
        fn=_mesh_fn(mesh);point=om.MFnDagNode(fn.dagPath()).boundingBox.center*world
        for index in range(3):
            if index!=axis:center[index]=point[index]
    values=list(result);values[12:15]=[center.x,center.y,center.z]
    return om.MMatrix(values)


def reflection(pose,axis):
    values=list(om.MMatrix());values[axis*4+axis]=-1
    return pose.inverse()*om.MMatrix(values)*pose


def dup_connections(node,pose):
    mesh=source(node);axis=cmds.getAttr(node+'.modelPackAxis')
    source_plug=mesh+'.worldMatrix[0]';destination=node+'.matrixIn[0]'
    for index in (0,1):
        target=node+f'.matrixIn[{index}]';incoming=cmds.connectionInfo(target,sourceFromDestination=True)
        if incoming and (index==1 or not cmds.isConnected(source_plug,target)):
            cmds.disconnectAttr(incoming,target)
    if not cmds.isConnected(source_plug,destination):cmds.connectAttr(source_plug,destination)
    cmds.setAttr(node+'.matrixIn[1]',*list(reflection(pose,axis)),type='matrix')


def write(node,pose):
    prepare(node)
    if cmds.nodeType(node)=='multMatrix':dup_connections(node,pose)
    cmds.setAttr(node+'.'+POSE,*list(pose),type='matrix')
    if not cmds.getAttr(node+'.'+ENABLED):cmds.setAttr(node+'.'+ENABLED,True)


def snapshot(node):
    return bool(cmds.getAttr(node+'.'+ENABLED)),tuple(cmds.getAttr(node+'.'+POSE))


def restore(node,state):
    enabled,pose=state
    cmds.setAttr(node+'.'+POSE,*pose,type='matrix');cmds.setAttr(node+'.'+ENABLED,enabled)
    if cmds.nodeType(node)=='multMatrix':
        if enabled:dup_connections(node,om.MMatrix(pose))
        else:
            from model_pack_modifiers import mirror_duplicate
            mirror_duplicate(source(node),cmds.getAttr(node+'.modelPackAxis'),cmds.getAttr(node+'.modelPackSpace'))


def manipulator_pivot():
    """Capture Maya's current world pivot before the toolbox changes context."""
    context=cmds.currentCtx()
    tools={'moveSuperContext':(cmds.manipMoveContext,'Move'),
           'RotateSuperContext':(cmds.manipRotateContext,'Rotate'),
           'scaleSuperContext':(cmds.manipScaleContext,'Scale')}
    entry=tools.get(context)
    if entry is None:return None
    command,name=entry
    try:point=command(name,query=True,position=True)
    except (RuntimeError,TypeError):return None
    if not point or len(point)<3 or not all(math.isfinite(v) for v in point[:3]):return None
    return [om.MDistance(v,om.MDistance.uiUnit()).asCentimeters() for v in point[:3]]


def selection_frames(meshes,pivot=None):
    """One selection center, with world or active-object rotation (no scale)."""
    if pivot is None:
        bounds=cmds.exactWorldBoundingBox(meshes)
        center=[om.MDistance((bounds[i]+bounds[i+3])*.5,om.MDistance.uiUnit()).asCentimeters() for i in range(3)]
    else:center=list(pivot)
    active_world=_mesh_fn(meshes[-1]).dagPath().inclusiveMatrix()
    local=om.MTransformationMatrix(active_world).rotation(asQuaternion=True).asMatrix()
    result={}
    for space,pose in ((0,om.MMatrix()),(1,local)):
        values=list(pose);values[12:15]=center if space else [0.,0.,0.];result[space]=om.MMatrix(values)
    return result


def automatic_direction(nodes):
    """Keep the occupied half only when the entire input is on one side."""
    distances=[]
    for node in nodes:
        if cmds.nodeType(node)!='modelPackMirrorCut':continue
        selection=om.MSelectionList();selection.add(node)
        dependency=om.MFnDependencyNode(selection.getDependNode(0))
        incoming=om.MFnMesh(dependency.findPlug('inputMesh',False).asMObject())
        world=_mesh_fn(source(node)).dagPath().inclusiveMatrix()
        inverse=frame(node).inverse();axis=cmds.getAttr(node+'.axis')
        distances.extend((point*world*inverse)[axis] for point in incoming.getPoints())
    if not distances:return None
    tolerance=max(1e-7,max(map(abs,distances))*1e-8)
    positive=any(d>tolerance for d in distances);negative=any(d<-tolerance for d in distances)
    if positive==negative:return None
    value=1 if positive else 0
    for node in nodes:
        if cmds.getAttr(node+'.direction')!=value:cmds.setAttr(node+'.direction',value)
    return value


def write_many(nodes,pose):
    for node in dict.fromkeys(nodes):
        if cmds.objExists(node):write(node,pose)


def snapshot_many(nodes):
    return {node:snapshot(node) for node in nodes if cmds.objExists(node)}


def restore_many(states):
    for node,state in states.items():
        if cmds.objExists(node):restore(node,state)


def basis(pose):return [om.MVector(*list(pose)[i*4:i*4+3]) for i in range(3)]
def origin(pose):return om.MVector(*list(pose)[12:15])


def moved(pose,delta):
    values=list(pose);values[12:15]=list(origin(pose)+om.MVector(delta));return om.MMatrix(values)


def rotated(pose,axis,angle):
    values=list(pose);center=values[12:15];values[12:15]=[0.,0.,0.]
    result=om.MMatrix(values)*om.MQuaternion(angle,om.MVector(axis).normal()).asMatrix()
    values=list(result);values[12:15]=center;return om.MMatrix(values)


def bounds(node,pose):
    nodes=[node] if isinstance(node,str) else node
    points=[];inverse=pose.inverse()
    for item in nodes:
        mesh=source(item);fn=_mesh_fn(mesh);box=om.MFnDagNode(fn.dagPath()).boundingBox
        low,high=box.min,box.max;world=fn.dagPath().inclusiveMatrix()
        points.extend(om.MPoint(x,y,z)*world*inverse for x in (low.x,high.x) for y in (low.y,high.y) for z in (low.z,high.z))
    axis=cmds.getAttr(nodes[0]+'.'+fields(nodes[0])[0]);flat=[i for i in range(3) if i!=axis]
    ranges=[(min(p[i] for p in points),max(p[i] for p in points)) for i in flat]
    size=max((b-a for a,b in ranges),default=1.);size=max(size,1e-4);padding=size*.08
    corners=[]
    for a,b in ((0,0),(1,0),(1,1),(0,1)):
        point=om.MPoint();point[flat[0]]=ranges[0][a]+(-padding if a==0 else padding)
        point[flat[1]]=ranges[1][b]+(-padding if b==0 else padding);corners.append(point*pose)
    return corners
