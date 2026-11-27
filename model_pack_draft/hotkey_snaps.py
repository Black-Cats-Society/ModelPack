"""Read Maya's native snap modes; resolve points on a fixed drawing plane."""
import math
import numpy as np
from maya import cmds
from maya.api import OpenMaya as om
from model_pack_linear_shift.overlay import Projection
from . import core
from .snapping import Snap,ray


def mode():
    for name in ('point','curve','grid'):
        if cmds.snapMode(q=True,**{name:True}):return name
    return None


def tolerance():
    return float(cmds.snapMode(q=True,tolerance=True)) if cmds.snapMode(q=True,useTolerance=True) else None


def plane_tolerance(frame):
    return max(1e-5,max(abs(v) for v in (frame.origin.x,frame.origin.y,frame.origin.z))*3e-7)


def on_surface(snap,hit):
    closest,_=om.MFnMesh(hit.path).getClosestPoint(snap.point,om.MSpace.kWorld)
    return (closest-snap.point).length()<=plane_tolerance(core.Frame.make(hit.point,hit.normal))


def grid_point(point,frame):
    step=core.internal_distance(float(cmds.grid(q=True,spacing=True))/max(1,int(cmds.grid(q=True,divisions=True))))
    if step<=core.EPSILON:return None
    normal=frame.normal;locked=max(range(3),key=lambda i:abs(normal[i]))
    values=[point.x,point.y,point.z]
    for i in range(3):
        if i!=locked:values[i]=math.copysign(math.floor(abs(values[i])/step+.5)*step,values[i])
    anchor=[frame.origin.x,frame.origin.y,frame.origin.z]
    values[locked]=anchor[locked]-sum(normal[i]*(values[i]-anchor[i]) for i in range(3) if i!=locked)/normal[locked]
    return Snap(om.MPoint(values),'grid')


def unoccluded(point,view,meshes):
    x,y,shown=Projection(view).point(point)
    if not shown:return False
    origin,direction=ray(view,x,y);found=core.raycast(origin,direction,meshes)
    if found is None:return True  # A rounded silhouette pixel can miss its vertex.
    base=core.intersect_plane(origin,direction,point,direction)
    adjacent=core.intersect_plane(*ray(view,x+1,y),point,direction)
    pixel=(adjacent-base).length() if adjacent is not None and base is not None else 1e-5
    return (point-found.point)*direction<=max(pixel*2.5,1e-5)


def vertex_hit(snap,view,pointer,preferred=None):
    fn=om.MFnMesh(snap.path);iterator=om.MItMeshVertex(snap.path);iterator.setIndex(snap.index)
    faces=list(iterator.getConnectedFaces())
    if not faces:return None
    _,direction=ray(view,*pointer)
    if preferred is not None and preferred.path.fullPathName()==snap.path.fullPathName() and preferred.face in faces:
        face=preferred.face
    else:face=max(faces,key=lambda f:fn.getPolygonNormal(f,om.MSpace.kWorld)*(-direction))
    origin,_=ray(view,*pointer)
    return core.Hit(om.MDagPath(snap.path),om.MPoint(snap.point),
                    fn.getPolygonNormal(face,om.MSpace.kWorld).normal(),(snap.point-origin).length(),face)


class Resolver:
    def __init__(self):self.clear()

    def clear(self):
        self.vertices=None;self.curves=None;self.intersections={}

    def points(self,view,pointer,meshes,frame,limit):
        if self.vertices is None:
            self.vertices=[]
            for path in meshes:
                if not path.isValid() or not path.isVisible():continue
                points=np.asarray(om.MFnMesh(path).getPoints(om.MSpace.kWorld),dtype=float)
                if len(points):self.vertices.append((om.MDagPath(path),points))
        projection=Projection(view);nearby=[];camera_origin=ray(view,*pointer)[0]
        for path,points in self.vertices:
            if not path.isValid() or not path.isVisible():continue
            indices=np.arange(len(points))
            if frame is not None:
                delta=points[:,:3]-np.array((frame.origin.x,frame.origin.y,frame.origin.z))
                indices=indices[np.abs(delta@np.array(tuple(frame.normal)))<=plane_tolerance(frame)]
            if not len(indices):continue
            chosen=points[indices]
            if projection.matrix is not None:
                clip=chosen@np.asarray(projection.matrix).reshape(4,4);w=clip[:,3]
                shown=(w>1e-10)&np.all(np.abs(clip[:,:3])<=w[:,None],axis=1)
                indices=indices[shown];clip=clip[shown];w=w[shown]
                screen=(clip[:,:2]/w[:,None]+1)*np.array((projection.width,projection.height))*.5
            else:
                screen=np.array([projection.point(om.MPoint(tuple(p)))[:2] for p in chosen])
            if not len(indices):continue
            squared=np.sum((screen-np.array(pointer))**2,axis=1)
            eligible=np.flatnonzero(squared<=limit*limit) if limit is not None else np.arange(len(squared))
            if len(eligible)>32:eligible=eligible[np.argpartition(squared[eligible],31)[:32]]
            order=eligible[np.argsort(squared[eligible])]
            for offset in order:
                if limit is not None and squared[offset]>limit*limit:break
                vertex=int(indices[offset]);point=om.MPoint(tuple(points[vertex]))
                nearby.append((float(squared[offset]),(point-camera_origin).length(),path,vertex,point))
        for _,_,path,index,point in sorted(nearby,key=lambda item:item[:2]):
            if unoccluded(point,view,meshes):return Snap(point,'vertex',om.MDagPath(path),index)
        return None

    def curve_points(self,path,proposed,frame):
        fn=om.MFnNurbsCurve(path);epsilon=plane_tolerance(frame)
        closest,_=fn.closestPoint(proposed,space=om.MSpace.kWorld)
        if abs((closest-frame.origin)*frame.normal)<=epsilon:return [om.MPoint(closest)]
        # Nonplanar curves can still cross the drawing plane. Locate those
        # crossings without snapping the footprint out of its fixed plane.
        key=(path.fullPathName(),tuple(frame.origin),tuple(frame.normal))
        if key not in self.intersections:
            low,high=fn.knotDomain;parameters=np.linspace(low,high,max(65,fn.numSpans*8+1))
            samples=[(float(t),fn.getPointAtParam(float(t),om.MSpace.kWorld)) for t in parameters]
            roots=[]
            for (a,pa),(b,pb) in zip(samples,samples[1:]):
                da,db=((p-frame.origin)*frame.normal for p in (pa,pb))
                if abs(da)<=epsilon:roots.append(om.MPoint(pa))
                elif da*db<0:
                    for _ in range(30):
                        t=(a+b)*.5;p=fn.getPointAtParam(t,om.MSpace.kWorld);d=(p-frame.origin)*frame.normal
                        if d*da>0:a,da=t,d
                        else:b=t
                    roots.append(om.MPoint(fn.getPointAtParam((a+b)*.5,om.MSpace.kWorld)))
            if abs((samples[-1][1]-frame.origin)*frame.normal)<=epsilon:roots.append(om.MPoint(samples[-1][1]))
            self.intersections[key]=roots
        return self.intersections[key]

    def find(self,kind,view,pointer,meshes,frame=None,proposed=None,curves=()):
        if kind=='point':return self.points(view,pointer,meshes,frame,tolerance())
        if frame is None or proposed is None:return None
        if kind=='grid':return grid_point(proposed,frame)
        if kind!='curve':return None
        if self.curves is None:self.curves=[om.MDagPath(path) for path in curves]
        projection=Projection(view);limit=tolerance();nearby=[]
        for path in self.curves:
            if not path.isValid() or not path.isVisible():continue
            for point in self.curve_points(path,proposed,frame):
                x,y,shown=projection.point(point);distance=math.hypot(x-pointer[0],y-pointer[1])
                if shown and (limit is None or distance<=limit):nearby.append((distance,point))
        for _,point in sorted(nearby,key=lambda item:item[0]):
            if unoccluded(point,view,meshes):return Snap(point,'curve')
        return None
