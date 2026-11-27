"""Read native NURBS profiles; extrude planar solids or open 3D walls."""
from dataclasses import dataclass
import math
from maya import cmds
from maya.api import OpenMaya as om
from . import core


@dataclass
class Profile:
    frame: core.Frame
    ring: list
    shape: str
    segments: int
    primitive: object = None
    from_curve = True
    valid = True
    closed = True

    def dimensions(self):
        if self.primitive is not None:return self.primitive.dimensions()
        xs,zs=zip(*self.ring)
        return max(xs)-min(xs),max(zs)-min(zs),(max(xs)+min(xs))*.5,(max(zs)+min(zs))*.5

    def mesh(self,depth):
        return core.prism_mesh(self.frame,self.ring,depth)


@dataclass
class OpenProfile:
    frame: core.Frame
    samples: list
    shape = 'curve'
    segments = 32
    primitive = None
    from_curve = True
    valid = True
    closed = False

    def dimensions(self):
        xs,zs=zip(*(self.frame.coordinates(p) for p in self.samples))
        return max(xs)-min(xs),max(zs)-min(zs),(max(xs)+min(xs))*.5,(max(zs)+min(zs))*.5

    def mesh(self,depth):
        low,high=sorted((0.,depth))
        points=[p+self.frame.normal*y for p in self.samples for y in (low,high)]
        faces=[[2*i,2*i+1,2*i+3,2*i+2] for i in range(len(self.samples)-1)]
        return points,[4]*len(faces),[v for face in faces for v in face]


def _open_profile(points,center,matrix,preferred):
    area=om.MVector()
    for a,b in zip(points,points[1:]):area+=(a-center)^(b-center)
    hint=om.MVector(preferred) if preferred is not None else om.MVector(0,1,0)*matrix
    if area.length()>core.EPSILON:
        normal=-area.normal()
        if normal*hint<0:normal=-normal
    else:
        tangent=max((b-a for a,b in zip(points,points[1:])),key=lambda v:v.length()).normal()
        normal=hint-tangent*(hint*tangent)
        if normal.length()<core.EPSILON:
            hint=min((om.MVector(1,0,0),om.MVector(0,1,0),om.MVector(0,0,1)),key=lambda v:abs(v*tangent))
            normal=hint-tangent*(hint*tangent)
    frame=core.Frame.make(center,normal,points[-1]-points[0])
    if any(((b-a)^frame.normal).length()<(b-a).length()*1e-6 for a,b in zip(points,points[1:])):
        raise ValueError('Участок кривой параллелен направлению выдавливания. Поверните кривую.')
    return OpenProfile(frame,points)


def _samples(curve,tolerance):
    if curve.degree==1:
        return [om.MPoint(p) for p in curve.cvPositions(om.MSpace.kWorld)]
    # Evaluate the curve itself, not its control polygon. Start at distinct
    # knot spans so a closed curve can never be mistaken for a zero chord.
    start,end=curve.knotDomain
    knots=sorted(set([start,end]+[k for k in curve.knots() if start<k<end]))
    points=[]
    def sample(a,b,p,q,level=0):
        parameters=[a+(b-a)*fraction for fraction in (.25,.5,.75)]
        middle=[curve.getPointAtParam(t,om.MSpace.kWorld) for t in parameters]
        chord=q-p;length=chord*chord
        def distance(point):
            t=max(0.,min(1.,((point-p)*chord)/length)) if length else 0.
            return (point-(p+chord*t)).length()
        if max(map(distance,middle))<=tolerance:
            points.append(p)
        else:
            if level>=12 or len(points)>2048:
                raise ValueError('Контур слишком сложный. Упростите кривую перед выдавливанием.')
            sample(a,parameters[1],p,middle[1],level+1)
            sample(parameters[1],b,middle[1],q,level+1)
    for a,b in zip(knots,knots[1:]):
        sample(a,b,curve.getPointAtParam(a,om.MSpace.kWorld),curve.getPointAtParam(b,om.MSpace.kWorld))
    points.append(curve.getPointAtParam(end,om.MSpace.kWorld))
    return points


def _validate(ring,tolerance):
    def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def touches(a,b,p):
        return (abs(cross(a,b,p))<=tolerance*math.dist(a,b) and
                all(min(a[k],b[k])-tolerance<=p[k]<=max(a[k],b[k])+tolerance for k in (0,1)))
    count=len(ring)
    for i,a in enumerate(ring):
        b=ring[(i+1)%count]
        for j in range(i+1,count):
            if j==i+1 or (i==0 and j==count-1):continue
            c,d=ring[j],ring[(j+1)%count]
            if ((cross(a,b,c)*cross(a,b,d)<0 and cross(c,d,a)*cross(c,d,b)<0) or
                    touches(a,b,c) or touches(a,b,d) or touches(c,d,a) or touches(c,d,b)):
                raise ValueError('Контур пересекает сам себя. Исправьте точки кривой.')
    area=sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(ring,ring[1:]+ring[:1]))
    if abs(area)<=tolerance*tolerance:raise ValueError('У контура нулевая площадь.')
    return ring if area>0 else list(reversed(ring))


def read(name,data=None,preferred_normal=None):
    dag=core.path_for(name);matrix=dag.inclusiveMatrix()
    if dag.node().hasFn(om.MFn.kTransform):dag.extendToShape()
    curve=om.MFnNurbsCurve(dag)
    cvs=[om.MPoint(p) for p in curve.cvPositions(om.MSpace.kWorld)]
    if not cvs or any(not math.isfinite(p[k]) for p in cvs for k in range(3)):
        raise ValueError('Кривая не содержит корректных точек.')
    span=om.MVector(*[max(p[k] for p in cvs)-min(p[k] for p in cvs) for k in range(3)]).length()
    if span<core.EPSILON:raise ValueError('У паттерна нулевой размер.')
    tolerance=max(span*1e-7,1e-7)
    points=_samples(curve,max(span*.0005,1e-5))
    closed=(data is not None or curve.form!=om.MFnNurbsCurve.kOpen or
            (points[0]-points[-1]).length()<=tolerance)
    compact=[]
    for p in points:
        if not compact or (p-compact[-1]).length()>tolerance:compact.append(p)
    if closed and len(compact)>1 and (compact[0]-compact[-1]).length()<=tolerance:compact.pop()
    if not closed:
        if len(compact)<2:raise ValueError('У кривой недостаточно точек для выдавливания.')
        center=om.MPoint(*[sum(p[k] for p in compact)/len(compact) for k in range(3)])
        return _open_profile(compact,center,matrix,preferred_normal)
    # Original v1 patterns were open degree-one curves with a repeated CV.
    # Keep their intentional closed boundary, including after native CV edits.
    if len(compact)<3:raise ValueError('Для выдавливания нужны хотя бы три точки контура.')
    center=om.MPoint(*[sum(p[k] for p in compact)/len(compact) for k in range(3)])
    area=om.MVector()
    for a,b in zip(compact,compact[1:]+compact[:1]):area+=(a-center)^(b-center)
    if area.length()<tolerance*tolerance:raise ValueError('Контур вырожден или пересекает сам себя.')
    u=om.MVector(1,0,0)*matrix;v=om.MVector(0,0,1)*matrix;hint=v^u
    if data is None:
        hint=om.MVector(preferred_normal) if preferred_normal is not None else -area
    normal=-area.normal()
    if hint.length()>core.EPSILON and normal*hint<0:normal=-normal
    if any(abs((p-center)*normal)>max(span*1e-5,1e-6) for p in cvs+compact):
        raise ValueError('Контур должен лежать в одной плоскости. Выровняйте точки кривой.')
    frame=core.Frame.make(center,normal,u)
    ring=_validate([frame.coordinates(p) for p in compact],tolerance)
    primitive=None
    # Untouched native boxes/cylinders retain their existing radius/height/cap
    # parameters. Any CV change, rebuild, shear or frozen transform is read as
    # the actual contour instead; metadata never substitutes for edited points.
    if data is not None and min(u.length(),v.length(),hint.length())>core.EPSILON and curve.degree==1:
        native_frame=core.Frame(om.MPoint()*matrix,hint.normal(),u,v)
        candidate=core.Footprint(native_frame,data['shape'],data['width']*.5,
            0. if data['shape']=='cylinder' else data['length']*.5,int(data['segments']),data['shape']=='box')
        expected=candidate.mesh(1)[0][:4 if candidate.shape=='box' else candidate.segments]
        if len(expected)==len(compact) and all((a-b).length()<tolerance*.1 for a,b in zip(expected,compact)):
            primitive=candidate
            frame=native_frame
            # Its two axes may include nonuniform object scale.
            inverse=matrix.inverse()
            ring=[((p*inverse).x,(p*inverse).z) for p in compact]
    return Profile(frame,ring,data['shape'] if data else 'curve',int(data['segments']) if data else 32,primitive)


def create(profile,depth,overlap=0.,editable=False):
    if not profile.closed:
        points,_,_=profile.mesh(depth)
        # Keep the seed grid in centimeters too; a one-meter seed leaves
        # unnecessarily large float tweaks when the resulting stroke is small.
        node=cmds.polyPlane(name='fdSurface#',width=core.ui_distance(1),height=core.ui_distance(1),subdivisionsX=1,
                            subdivisionsY=len(profile.samples)-1,constructionHistory=False)[0]
        # Native component commands retain the exact 3D stroke and are undoable.
        for i,p in enumerate(points):
            cmds.xform(node+'.vtx[%s]'%i,worldSpace=True,
                       translation=tuple(core.ui_distance(p[k]) for k in range(3)))
        cmds.polySoftEdge(node,angle=60,constructionHistory=False)
        return cmds.ls(node,long=True)[0]
    if profile.primitive is not None:
        return core.create_primitive(profile.primitive,depth,overlap,editable)
    low,high=sorted((0.,depth));high+=overlap
    # Native polygon commands preserve complete Undo/Redo and saved scene data.
    # A cap is one n-gon; extrusion creates only its side quads.
    # polyCreateFacet's point flag is a raw vector in internal centimeters;
    # polyExtrudeFacet's translate flag below is a UI-distance attribute.
    points=[tuple(list(profile.frame.point(x,low,z))[:3])
            for x,z in reversed(profile.ring)]
    node=cmds.polyCreateFacet(name='fdProfile#',point=points,constructionHistory=False)[0]
    delta=profile.frame.normal*(high-low)
    cmds.polyExtrudeFacet(node+'.f[0]',translate=tuple(core.ui_distance(v) for v in delta),
                          constructionHistory=False,keepFacesTogether=True)
    cmds.polyCloseBorder(node,constructionHistory=False)
    cmds.polySoftEdge(node,angle=60,constructionHistory=False)
    if profile.shape=='cylinder':soften_walls(node,profile,history=False)
    return cmds.ls(node,long=True)[0]


def soften_walls(node,profile,depth=None,history=True):
    """A curve cut affects only its walls, never the rest of the target mesh."""
    if profile.primitive is not None:
        if profile.shape=='cylinder':
            from .cylinder_caps import soften_walls as soften_cylinder
            return soften_cylinder(node,profile.primitive,depth,history)
        return []
    dag=core.path_for(node)
    if dag.node().hasFn(om.MFn.kTransform):dag.extendToShape()
    fn=om.MFnMesh(dag);points=fn.getPoints(om.MSpace.kWorld)
    width,length,_,_=profile.dimensions();tolerance=max(width,length)*1e-5+1e-6
    local=[profile.frame.coordinates(p) for p in points]
    heights=[(p-profile.frame.origin)*profile.frame.normal for p in points]
    def on_wall(index):
        p=local[index]
        if depth is not None and not depth-tolerance<=heights[index]<=tolerance:return False
        for a,b in zip(profile.ring,profile.ring[1:]+profile.ring[:1]):
            dx,dz=b[0]-a[0],b[1]-a[1];squared=dx*dx+dz*dz
            t=max(0.,min(1.,((p[0]-a[0])*dx+(p[1]-a[1])*dz)/squared))
            if math.hypot(p[0]-a[0]-dx*t,p[1]-a[1]-dz*t)<tolerance:return True
        return False
    walls=set()
    for face in range(fn.numPolygons):
        ids=fn.getPolygonVertices(face)
        if (max(heights[i] for i in ids)-min(heights[i] for i in ids)>tolerance and
                all(on_wall(i) for i in ids)):walls.add(face)
    edges=[];iterator=om.MItMeshEdge(dag)
    while not iterator.isDone():
        faces=iterator.getConnectedFaces()
        if len(faces)==2 and all(i in walls for i in faces):
            edges.append('{}.e[{}]'.format(dag.fullPathName(),iterator.index()))
        iterator.next()
    if edges:cmds.polySoftEdge(edges,angle=180 if profile.shape=='cylinder' else 60,constructionHistory=history)
    return edges
