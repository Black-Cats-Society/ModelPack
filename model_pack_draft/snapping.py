"""Read-only surface snapping for the first point of a Fast Draft footprint."""
from dataclasses import dataclass
import math
from maya.api import OpenMaya as om
from model_pack_linear_shift.overlay import Projection
from . import core

RADIUS=12.0


@dataclass
class Snap:
    point: om.MPoint
    kind: str
    path: object = None
    index: int = -1

    @property
    def label(self):
        return {'face':'Центр полигона','edge':'Середина ребра','vertex':'Вершина · V',
                'curve':'Кривая · C','grid':'Сетка · X'}[self.kind]


def ray(view,x,y):
    origin,direction=om.MPoint(),om.MVector()
    view.viewToWorld(int(round(x)),int(round(y)),origin,direction)
    return origin,direction.normal()


def polygon_center(fn,face,points):
    """Place the vertex center on this polygon's actual triangulated surface.

    A warped quad's average is generally between its triangles. Project along
    the polygon normal, keeping its in-plane center and rejecting projections
    outside a concave outline or inside a hole. Never borrow another face.
    """
    center=om.MPoint(sum((om.MVector(p) for p in points),om.MVector())/len(points))
    normal=fn.getPolygonNormal(face,om.MSpace.kWorld).normal()
    polygon=om.MItMeshPolygon(fn.dagPath());polygon.setIndex(face)
    triangles,_=polygon.getTriangles(om.MSpace.kWorld)
    candidates=[]
    for i in range(0,len(triangles),3):
        a,b,c=(triangles[i+j] for j in range(3));u,v=b-a,c-a
        plane=u^v;denominator=plane*normal
        if abs(denominator)<=plane.length()*1e-10:continue
        offset=((a-center)*plane)/denominator
        point=center+normal*offset;w=point-a
        uu,uv,vv=u*u,u*v,v*v;determinant=uu*vv-uv*uv
        if determinant<=uu*vv*1e-14:continue
        s=((w*u)*vv-(w*v)*uv)/determinant
        t=((w*v)*uu-(w*u)*uv)/determinant
        if min(s,t)>=-1e-8 and s+t<=1+1e-8:candidates.append((abs(offset),point))
    return min(candidates,key=lambda value:value[0])[1] if candidates else None


def features(hit):
    """Vertex center of the picked polygon, plus its edge midpoints."""
    fn=om.MFnMesh(hit.path);box=fn.boundingBox;matrix=hit.path.inclusiveMatrix()
    result=[]
    if hit.face>=0:
        vertices=list(fn.getPolygonVertices(hit.face))
        points={v:fn.getPoint(v,om.MSpace.kWorld) for v in vertices}
        center=polygon_center(fn,hit.face,list(points.values()))
        if center is not None:result.append(Snap(center,'face',hit.path,hit.face))
        for a,b in zip(vertices,vertices[1:]+vertices[:1]):
            result.append(Snap(points[a]+(points[b]-points[a])*.5,'edge'))
    diagonal=(om.MVector(box.max-box.min)*matrix).length()
    tolerance=max(diagonal*1e-6,max(abs(v) for v in (hit.point.x,hit.point.y,hit.point.z))*3e-7,1e-5)
    return fn,result,tolerance


def visible(snap,hit,fn,view,pixel,pointer,candidates,tolerance):
    # A concave polygon's center can lie outside its boundary or in a hole. It must
    # actually lie on this mesh before considering any screen-space tolerance.
    closest,_=fn.getClosestPoint(snap.point,om.MSpace.kWorld)
    if (closest-snap.point).length()>tolerance:return False
    x,y=pixel;origin,direction=ray(view,x,y)
    adjacent=core.intersect_plane(*ray(view,x+1,y),snap.point,direction)
    base=core.intersect_plane(origin,direction,snap.point,direction)
    pixel_size=(adjacent-base).length() if adjacent is not None and base is not None else tolerance
    probes=[(x,y)]
    dx,dy=pointer[0]-x,pointer[1]-y;length=math.hypot(dx,dy)
    if length>.1:
        # A silhouette can round onto its outside pixel. Probe just toward
        # the original surface hit, without moving the exact snap position.
        probes.append((x+dx/length*1.5,y+dy/length*1.5))
    for px,py in probes:
        found=core.raycast(*ray(view,px,py),candidates)
        if (found is not None and found.path.fullPathName()==hit.path.fullPathName() and
                (snap.kind!='face' or found.face==hit.face) and
                (found.point-snap.point).length()<=max(tolerance,pixel_size*2.5)):
            return True
    return False


def find(hit,view,pointer,candidates,radius=RADIUS):
    fn,choices,tolerance=features(hit);projection=Projection(view)
    nearby=[]
    for index,snap in enumerate(choices):
        x,y,shown=projection.point(snap.point)
        distance=math.hypot(x-pointer[0],y-pointer[1])
        if shown and distance<=radius:nearby.append((distance,index,snap,(x,y)))
    for _,_,snap,pixel in sorted(nearby,key=lambda value:value[:2]):
        if visible(snap,hit,fn,view,pixel,pointer,candidates,tolerance):return snap
    return None
