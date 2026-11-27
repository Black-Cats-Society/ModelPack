"""Curve ribbons with a rounded recessed profile and two flat skirts."""
from collections import defaultdict
import math
import numpy as np
from maya.api import OpenMaya as om


class OffsetError(ValueError):
    """A smaller width/skirt can make this otherwise valid path usable."""


def skirt_support(width,skirt):
    """Keep the lip support close as the outer skirt grows."""
    return min(skirt*.5,width*.125)


def _simple(points,closed,epsilon):
    count=len(points) if closed else len(points)-1
    scale=max(max(p[k] for p in points)-min(p[k] for p in points) for k in (0,1))
    cell=max(scale/math.sqrt(len(points)),epsilon*10);buckets=defaultdict(list)
    def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def touches(a,b,p):
        return abs(cross(a,b,p))<=epsilon*math.dist(a,b) and all(min(a[k],b[k])-epsilon<=p[k]<=max(a[k],b[k])+epsilon for k in (0,1))
    for i in range(count):
        a,b=points[i],points[(i+1)%len(points)];keys=[];candidates=set()
        for x in range(math.floor((min(a[0],b[0])-epsilon)/cell),math.floor((max(a[0],b[0])+epsilon)/cell)+1):
            for y in range(math.floor((min(a[1],b[1])-epsilon)/cell),math.floor((max(a[1],b[1])+epsilon)/cell)+1):
                key=x,y;keys.append(key);candidates.update(buckets[key])
        for j in candidates:
            if i-j==1 or (closed and i==count-1 and j==0):continue
            c,d=points[j],points[(j+1)%len(points)]
            if (cross(a,b,c)*cross(a,b,d)<0 and cross(c,d,a)*cross(c,d,b)<0) or any((touches(a,b,p) for p in (c,d))) or any((touches(c,d,p) for p in (a,b))):
                return False
        for key in keys:buckets[key].append(i)
    return True


def frame(points,hint,strict=True):
    values=np.array([tuple(p)[:3] for p in points]);origin=values.mean(axis=0)
    _,singular,axes=np.linalg.svd(values-origin,full_matrices=False)
    scale=max(np.linalg.norm(values-origin,axis=1).max(),1e-10)
    if scale<1e-8:raise ValueError('Floater: у кривой нулевой размер.')
    if len(singular)<2 or singular[1]<scale*1e-7:
        tangent=max((b-a for a,b in zip(points,points[1:])),key=lambda p:p.length()).normal()
        normal=om.MVector(hint)-tangent*(om.MVector(hint)*tangent)
        if normal.length()<1e-8:
            seed=min((om.MVector(1,0,0),om.MVector(0,1,0),om.MVector(0,0,1)),key=lambda p:abs(p*tangent))
            normal=seed-tangent*(seed*tangent)
    else:normal=om.MVector(*axes[-1])
    normal.normalize()
    if normal*om.MVector(hint)<0:normal=-normal
    origin=om.MPoint(*origin)
    if strict and any(abs((p-origin)*normal)>max(scale*1e-5,1e-7) for p in points):
        raise ValueError('Floater: кривая должна лежать в одной плоскости.')
    u=max((p-origin for p in points),key=lambda p:p.length());u-=normal*(u*normal);u.normalize()
    v=normal^u
    return origin,u,v,normal,scale


def sample(curve,matrix,width):
    cvs=[om.MPoint(p)*matrix for p in curve.cvPositions()]
    if len(cvs)<2 or any(not math.isfinite(p[k]) for p in cvs for k in range(3)):
        raise ValueError('Floater: кривая не содержит корректных точек.')
    span=max(max(p[k] for p in cvs)-min(p[k] for p in cvs) for k in range(3))
    tolerance=max(span*1e-7,min(span*.0005,width*.05),1e-7)
    if curve.degree==1:points=cvs
    else:
        start,end=curve.knotDomain;knots=sorted(set([start,end]+[k for k in curve.knots() if start<k<end]));points=[]
        def point(t):return curve.getPointAtParam(t)*matrix
        def part(a,b,p,q,level=0):
            ts=[a+(b-a)*f for f in (.25,.5,.75)];middle=[point(t) for t in ts];edge=q-p;length=edge*edge
            error=max((r-(p+edge*max(0.,min(1.,((r-p)*edge)/length)))).length() if length>1e-20 else (r-p).length() for r in middle)
            if error<=tolerance:points.append(p)
            else:
                if level>=12 or len(points)>2048:raise ValueError('Floater: слишком сложная кривая. Упростите её или увеличьте Opening Width.')
                part(a,ts[1],p,middle[1],level+1);part(ts[1],b,middle[1],q,level+1)
        for a,b in zip(knots,knots[1:]):part(a,b,point(a),point(b))
        points.append(point(end))
    epsilon=max(span*1e-8,1e-8);closed=curve.form!=om.MFnNurbsCurve.kOpen or (points[-1]-points[0]).length()<=epsilon
    compact=[]
    for p in points:
        if not compact or (p-compact[-1]).length()>epsilon:compact.append(p)
    if closed and len(compact)>1 and (compact[0]-compact[-1]).length()<=epsilon:compact.pop()
    if len(compact)<(3 if closed else 2):raise ValueError('Floater: у контура недостаточно разных точек.')
    return compact,closed


def build(points,closed,width,height,skirt,hint=(0,1,0)):
    if any(not math.isfinite(v) for v in (width,height,skirt)) or width<=0 or height<0 or skirt<0:
        raise ValueError('Floater: Opening Width должен быть больше нуля; Height и Skirt — неотрицательные.')
    origin,u,v,normal,scale=frame(points,hint,strict=False);epsilon=max(scale*1e-9,1e-9)
    xy=[((p-origin)*u,(p-origin)*v) for p in points]
    depth=[(p-origin)*normal for p in points]
    spatial=any(abs(value)>max(scale*1e-5,1e-7) for value in depth)
    if closed and sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(xy,xy[1:]+xy[:1]))<0:
        xy.reverse();depth.reverse()
    if not _simple(xy,closed,epsilon):
        raise ValueError('Floater: проекция кривой на её основную плоскость пересекает сама себя.' if spatial else 'Floater: кривая пересекает сама себя.')
    count=len(xy);edges=[]
    for i in range(count if closed else count-1):
        a,b=xy[i],xy[(i+1)%count];length=math.dist(a,b)
        if length<=epsilon:raise ValueError('Floater: в кривой совпадают соседние точки.')
        edges.append(((b[0]-a[0])/length,(b[1]-a[1])/length))
    offsets=[]
    for i in range(count):
        if not closed and i==0:t=edges[0];offsets.append((-t[1],t[0]));continue
        if not closed and i==count-1:t=edges[-1];offsets.append((-t[1],t[0]));continue
        a,b=edges[i-1],edges[i%len(edges)];den=1+a[0]*b[0]+a[1]*b[1]
        if den<1e-5:raise ValueError('Floater: слишком резкий разворот кривой.')
        offsets.append(((-a[1]-b[1])/den,(a[0]+b[0])/den))
    half=width*.5
    profile=[(-half,height,0.),(-width*.25,height*.5,-height*math.pi/width),(0.,0.,0.),
             (width*.25,height*.5,height*math.pi/width),(half,height,0.)]
    if skirt>epsilon:
        support=skirt_support(width,skirt)
        profile=[(-half-skirt,height,0.),(-half-support,height,0.)]+profile+[(half+support,height,0.),(half+skirt,height,0.)]
    loops=[]
    for distance,_,_ in profile:
        ring=[(p[0]+distance*m[0],p[1]+distance*m[1]) for p,m in zip(xy,offsets)]
        for i,t in enumerate(edges):
            a,b=ring[i],ring[(i+1)%count]
            if (b[0]-a[0])*t[0]+(b[1]-a[1])*t[1]<=epsilon:
                raise OffsetError('Floater: Opening Width или Skirt слишком велики для этого контура.')
        if not _simple(ring,closed,epsilon):raise OffsetError('Floater: юбка пересекается. Уменьшите Opening Width или Skirt.')
        loops.append(ring)
    vertices=[];normals=[];columns=len(profile)
    for i,m in enumerate(offsets):
        lateral=u*m[0]+v*m[1];length=lateral.length();direction=lateral/length
        for j,(_,h,slope) in enumerate(profile):
            # Preserve the actual curve depth, including freehand strokes
            # drawn on a gently curved surface, rather than flattening it.
            x,y=loops[j][i];vertices.append(origin+u*x+v*y+normal*(h+depth[i]))
            normals.append((normal-direction*(slope/length)).normal())
    if spatial:
        for i,m in enumerate(offsets):
            lateral=u*m[0]+v*m[1]
            for j,(_,_,slope) in enumerate(profile):
                index=i*columns+j;point=vertices[index];tangent=om.MVector()
                if closed or i>0:tangent+=(point-vertices[((i-1)%count)*columns+j]).normal()
                if closed or i<count-1:tangent+=(vertices[((i+1)%count)*columns+j]-point).normal()
                normals[index]=(tangent^(lateral+normal*slope)).normal()
    # A planar UV island has no artificial cut across the closed groove.
    projected=[loops[j][i] for i in range(count) for j in range(columns)]
    minimum=[min(p[k] for p in projected) for k in (0,1)]
    span=[max(p[k] for p in projected)-minimum[k] for k in (0,1)]
    uvs=[tuple((p[k]-minimum[k])/max(span[k],epsilon) for k in (0,1)) for p in projected];faces=[];uv_ids=[]
    for i in range(len(edges)):
        nxt=(i+1)%count
        for j in range(columns-1):
            faces.append([i*columns+j,nxt*columns+j,nxt*columns+j+1,i*columns+j+1])
            uv_ids.extend(faces[-1])
    return vertices,faces,normals,uvs,uv_ids


def mesh(plan):
    vertices,faces,normals,uvs,uv_ids=plan;data=om.MFnMeshData().create();fn=om.MFnMesh()
    fn.create(vertices,[4]*len(faces),[v for f in faces for v in f],parent=data)
    fn.setUVs([p[0] for p in uvs],[p[1] for p in uvs]);fn.assignUVs([4]*len(faces),uv_ids)
    fn=om.MFnMesh(data)
    fn.setEdgeSmoothings(list(range(fn.numEdges)),[True]*fn.numEdges);fn.cleanupEdgeSmoothing()
    fn=om.MFnMesh(data)
    # Flush Maya's pending smoothing normals before authoring the profile.
    fn.getNormals()
    # This setter already authors locked normals. An extra lock call on a
    # fresh mesh can replace them with geometric face normals in Maya.
    fn.setVertexNormals(normals,list(range(len(vertices))))
    fn.updateSurface()
    return data
