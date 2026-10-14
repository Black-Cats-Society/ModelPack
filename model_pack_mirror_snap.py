"""Read-only point/pivot snapshots and ground-grid snapping for Mirror planes."""
import math
import numpy as np
from maya import cmds
from maya.api import OpenMaya as om


def point_mode():
    return bool(cmds.snapMode(q=True,point=True))


def grid_mode():
    return bool(cmds.snapMode(q=True,grid=True))


def grid_position(proposed,origin,ray,up_axis,axis=None,center=None):
    spacing=float(cmds.grid(q=True,spacing=True) or 1.)
    divisions=max(1,int(cmds.grid(q=True,divisions=True) or 1))
    step=om.MDistance(spacing/divisions,om.MDistance.uiUnit()).asCentimeters()
    if step<=1e-12:return None
    def rounded(value):return math.copysign(math.floor(abs(value)/step+.5)*step,value)
    if axis is not None:
        index=max(range(3),key=lambda i:abs(axis[i]))
        return center+axis*((rounded(proposed[index])-center[index])/axis[index])
    up='xyz'.index(up_axis);position=om.MVector(proposed)
    if abs(ray[up])>1e-8:
        distance=-origin[up]/ray[up]
        if distance<0:return None
        position=origin+ray*distance
    position[up]=0.
    for index in range(3):
        if index!=up:position[index]=rounded(position[index])
    return position


def tolerance():
    if not cmds.snapMode(q=True,useTolerance=True):return None
    value=cmds.snapMode(q=True,tolerance=True)
    return float(value if value is not None else 12.)


def isolate_members(panel):
    if panel is None or not cmds.isolateSelect(panel,q=True,state=True):return None
    group=cmds.isolateSelect(panel,q=True,viewObjects=True)
    result=[]
    for name in (cmds.sets(group,q=True) or []) if group else []:
        try:
            selection=om.MSelectionList();selection.add(name)
            path,component=selection.getComponent(0)
            result.append((path.fullPathName(),component))
        except RuntimeError:continue
    return result


def isolated_vertices(path,members):
    """Return None for a whole object, a vertex set for a component subset."""
    if members is None:return None
    name=path.fullPathName();vertices=set();fn=om.MFnMesh(path)
    for root,component in members:
        if name!=root and not name.startswith(root+'|'):continue
        if component.isNull():return None
        indices=om.MFnSingleIndexedComponent(component).getElements()
        kind=component.apiType()
        if kind==om.MFn.kMeshVertComponent:vertices.update(indices)
        elif kind==om.MFn.kMeshPolygonComponent:
            for index in indices:vertices.update(fn.getPolygonVertices(index))
        elif kind==om.MFn.kMeshEdgeComponent:
            for index in indices:vertices.update(fn.getEdgeVertices(index))
    return vertices


class PointCache:
    """Project each visible mesh once per drag, then search arrays without DG reads."""
    def __init__(self,projection,panel=None):
        self.world=[];self.screen=[];self.depth=[]
        members=isolate_members(panel)
        meshes=panel is None or cmds.modelEditor(panel,q=True,polymeshes=True)
        for name in (cmds.ls(type='mesh',dag=True,long=True,allPaths=True,noIntermediate=True) or []) if meshes else []:
            try:
                selection=om.MSelectionList();selection.add(name);path=selection.getDagPath(0)
                if not path.isVisible():continue
                fn=om.MFnMesh(path);indices=isolated_vertices(path,members)
                if indices is not None and not indices:continue
                points=np.asarray(fn.getPoints(om.MSpace.kWorld),dtype=np.float64)
                if not len(points):continue
                if indices is not None:points=points[sorted(indices)]
                self.append(points,projection)
            except (RuntimeError,ValueError):continue
        # Object pivots belong to the same V/Snap to Points mode as vertices.
        # Read transform paths individually to preserve instanced world pivots.
        for name in cmds.ls(type='transform',dag=True,long=True,allPaths=True) or []:
            try:
                selection=om.MSelectionList();selection.add(name);path=selection.getDagPath(0)
                if not path.isVisible():continue
                if members is not None and not any(name==root or name.startswith(root+'|') or root.startswith(name+'|') for root,_ in members):continue
                fn=om.MFnDagNode(path);shapes=[]
                for i in range(fn.childCount()):
                    child=fn.child(i)
                    if child.hasFn(om.MFn.kTransform):continue
                    shape=om.MDagPath(path);shape.push(child);shapes.append(shape)
                def displayed(shape):
                    if om.MFnDagNode(shape).isIntermediateObject or not shape.isVisible():return False
                    if panel is None:return True
                    flags=((om.MFn.kMesh,'polymeshes'),(om.MFn.kCamera,'cameras'),(om.MFn.kLocator,'locators'))
                    return all(not shape.node().hasFn(kind) or cmds.modelEditor(panel,q=True,**{flag:True}) for kind,flag in flags)
                if shapes and not any(displayed(shape) for shape in shapes):continue
                pivot=om.MFnTransform(path).rotatePivot(om.MSpace.kWorld)
                self.append(np.array([[pivot.x,pivot.y,pivot.z,1.]]),projection)
            except (RuntimeError,ValueError):continue
        self.world=np.concatenate(self.world) if self.world else np.empty((0,3))
        self.screen=np.concatenate(self.screen) if self.screen else np.empty((0,2))
        self.depth=np.concatenate(self.depth) if self.depth else np.empty(0)

    def append(self,points,projection):
        if projection.matrix is not None:
            clip=points@np.asarray(projection.matrix).reshape(4,4)
            w=clip[:,3];visible=(w>1e-10)&np.all(np.abs(clip[:,:3])<=w[:,None],axis=1)
            points=points[visible];clip=clip[visible];w=w[visible]
            xy=(clip[:,:2]/w[:,None]+1)*np.array((projection.width,projection.height))*.5
            depth=clip[:,2]/w
        else:
            projected=np.array([projection.point(om.MPoint(tuple(p))) for p in points])
            visible=projected[:,2].astype(bool)
            xy=projected[visible,:2];points=points[visible];depth=np.zeros(len(points))
        inside=np.all(np.isfinite(xy),axis=1)&(xy[:,0]>=0)&(xy[:,0]<=projection.width)&(xy[:,1]>=0)&(xy[:,1]<=projection.height)
        if np.any(inside):
            self.world.append(points[inside,:3]);self.screen.append(xy[inside]);self.depth.append(depth[inside])

    def nearest(self,point,limit=None):
        if not len(self.screen):return None
        delta=self.screen-np.asarray(point)
        mask=np.all(np.abs(delta)<=limit,axis=1) if limit is not None else np.ones(len(delta),dtype=bool)
        indices=np.flatnonzero(mask)
        if not len(indices):return None
        squared=np.einsum('ij,ij->i',delta[indices],delta[indices]);minimum=float(np.min(squared))
        # Coincident screen vertices prefer the one closer to the camera.
        tied=indices[squared<=minimum+1e-8];index=tied[np.argmin(self.depth[tied])]
        return om.MVector(*self.world[index])
