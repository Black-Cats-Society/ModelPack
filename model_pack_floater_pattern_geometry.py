"""Projected surface grids with conservative full-footprint boundary tests."""
import math
import itertools
from collections import Counter
import numpy as np
from maya.api import OpenMaya as om


def hull(points):
    points = sorted(set(map(tuple, points)))
    def side(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    chains = []
    for sequence in (points, points[::-1]):
        chain = []
        for p in sequence:
            while len(chain)>1 and side(chain[-2], chain[-1], p)<=0:
                chain.pop()
            chain.append(p)
        chains.extend(chain[:-1])
    return chains


def inside(point, polygon):
    x, y = point
    result = False
    for a, b in zip(polygon, polygon[1:]+polygon[:1]):
        if (a[1]>y)!=(b[1]>y) and x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
            result = not result
    return result


def distance(p, a, b):
    p, a, b = np.asarray(p), np.asarray(a), np.asarray(b)
    delta = b-a
    t = np.clip(np.dot(p-a, delta)/max(np.dot(delta,delta),1e-30), 0, 1)
    return np.linalg.norm(p-a-t*delta)


def crosses(a,b,c,d):
    def side(p,q,r):
        return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    return side(a,b,c)*side(a,b,d)<-1e-16 and side(c,d,a)*side(c,d,b)<-1e-16


def frame(points, normal=None):
    origin = np.mean(points,axis=0)
    _, vectors = np.linalg.eigh(np.cov(np.asarray(points).T))
    normal = np.asarray(normal) if normal is not None else vectors[:,0]
    normal = normal/max(np.linalg.norm(normal),1e-15)
    # Project the most aligned world axis for stable grid orientation.
    axis = np.eye(3)[np.argmin(abs(normal))]
    u = np.cross(axis,normal);u /= np.linalg.norm(u)
    v = np.cross(normal,u)
    return origin,u,v,normal


def matrix(origin,u,v,n):
    return om.MMatrix([*u,0.,*v,0.,*n,0.,*origin,1.])


class Surface:
    def __init__(self, fn, faces):
        points = np.array([tuple(p)[:3] for p in fn.getPoints(om.MSpace.kWorld)])
        faces = set(range(fn.numPolygons)) if faces is None else set(faces)
        if not faces or max(faces)>=fn.numPolygons:
            raise ValueError('Surface: выделение полигонов устарело.')
        selected = sorted({v for f in faces for v in fn.getPolygonVertices(f)})
        normals = [(fn.getPolygonNormal(f,om.MSpace.kWorld),fn.getPolygonVertices(f)) for f in sorted(faces)]
        average = sum((n for n,_ in normals),om.MVector())
        if average.length()<1e-6:
            average = normals[0][0]
        self.origin,self.u,self.v,self.normal = frame(points[selected],tuple(average))
        faces = {f for f in faces if np.dot(tuple(fn.getPolygonNormal(f,om.MSpace.kWorld)),self.normal)>.1}
        if not faces:
            raise ValueError('Surface: не удалось определить сторону поверхности.')
        selected = sorted({v for f in faces for v in fn.getPolygonVertices(f)})
        self.axes = np.array([self.u,self.v])
        projected = (points-self.origin)@self.axes.T
        iterator = om.MItMeshPolygon(fn.dagPath())
        triangles = []
        edges = Counter()
        for f in sorted(faces):
            iterator.setIndex(f)
            _, ids = iterator.getTriangles(om.MSpace.kWorld)
            triangles.extend([ids[i:i+3] for i in range(0,len(ids),3)])
            edges.update(iterator.getEdges())
        self.triangles = points[np.asarray(triangles)]
        self.projected = projected[np.asarray(triangles)]
        self.a = self.projected[:,0]
        self.b = self.projected[:,1]-self.a
        self.c = self.projected[:,2]-self.a
        determinant = self.b[:,0]*self.c[:,1]-self.b[:,1]*self.c[:,0]
        self.valid = abs(determinant)>1e-14
        self.denominator = np.where(self.valid,determinant,1.)
        self.normals = np.cross(self.triangles[:,1]-self.triangles[:,0],self.triangles[:,2]-self.triangles[:,0])
        self.normals /= np.maximum(np.linalg.norm(self.normals,axis=1)[:,None],1e-30)
        self.boundary = [(projected[a],projected[b]) for e,count in edges.items() if count==1
                         for a,b in [fn.getEdgeVertices(e)]]
        self.low = projected[selected].min(axis=0)
        self.high = projected[selected].max(axis=0)

    def project(self, world):
        return (np.asarray(world)-self.origin)@self.axes.T

    def hit(self, point):
        a,b,c = self.a,self.b,self.c
        q = np.asarray(point)-a
        valid,denominator = self.valid,self.denominator
        s = (q[:,0]*c[:,1]-q[:,1]*c[:,0])/denominator
        t = (b[:,0]*q[:,1]-b[:,1]*q[:,0])/denominator
        ids = np.flatnonzero(valid&(s>=-1e-9)&(t>=-1e-9)&(s+t<=1+1e-9))
        if not len(ids):
            return None
        positions = self.triangles[ids,0]+s[ids,None]*(self.triangles[ids,1]-self.triangles[ids,0])+t[ids,None]*(self.triangles[ids,2]-self.triangles[ids,0])
        i = ids[np.argmax((positions-self.origin)@self.normal)]
        position = self.triangles[i,0]+s[i]*(self.triangles[i,1]-self.triangles[i,0])+t[i]*(self.triangles[i,2]-self.triangles[i,0])
        normal = self.normals[i]
        if normal@self.normal<.1:
            return None
        return position,normal


def curve_polygon(fn, surface, tolerance):
    low,high = fn.knotDomain
    first = np.array(tuple(fn.getPointAtParam(low,om.MSpace.kWorld))[:3])
    last = np.array(tuple(fn.getPointAtParam(high,om.MSpace.kWorld))[:3])
    if np.linalg.norm(first-last)>tolerance:
        raise ValueError('Boundary / Exclusions: нужны замкнутые кривые.')
    result = []
    def sample(a,b,p,q,depth):
        m = (a+b)*.5
        r = np.array(tuple(fn.getPointAtParam(m,om.MSpace.kWorld))[:3])
        if depth<12 and (np.linalg.norm(r-(p+q)*.5)>tolerance or depth<3):
            sample(a,m,p,r,depth+1);sample(m,b,r,q,depth+1)
        else:
            result.append(surface.project(p))
    spans = sorted(set([low,high]+[k for k in fn.knots() if low<k<high]))
    for a,b in zip(spans,spans[1:]):
        p = np.array(tuple(fn.getPointAtParam(a,om.MSpace.kWorld))[:3])
        q = np.array(tuple(fn.getPointAtParam(b,om.MSpace.kWorld))[:3])
        sample(a,b,p,q,0)
    if len(result)>4096:
        raise ValueError('Слишком плотная маска. Упростите кривую.')
    return [tuple(p) for p in result]


def outer_contour(surface, center):
    """Order the actual region border, retaining concave corners."""
    links = {}
    points = {}
    for a,b in surface.boundary:
        ka,kb = tuple(np.round(a,10)),tuple(np.round(b,10))
        if ka==kb:
            continue
        links.setdefault(ka,set()).add(kb)
        links.setdefault(kb,set()).add(ka)
        points[ka],points[kb] = tuple(a),tuple(b)
    if not links or any(len(neighbors)!=2 for neighbors in links.values()):
        raise ValueError('Guides: укажите внешнюю Boundary кривую.')
    remaining = set(links)
    loops = []
    while remaining:
        first = min(remaining)
        loop = []
        previous,current = None,first
        while current in remaining:
            remaining.remove(current)
            loop.append(points[current])
            following = min(links[current]-{previous})
            previous,current = current,following
        if current!=first:
            raise ValueError('Guides: граница поверхности разомкнута.')
        if inside(center,loop):
            loops.append(loop)
    if not loops:
        raise ValueError('Guides: направляющая должна быть внутри Surface.')
    return max(loops,key=lambda p:abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(p,p[1:]+p[:1]))))


def inset_polygon(polygon, amount):
    points = np.asarray(polygon)
    edges = np.roll(points,-1,axis=0)-points
    lengths = np.linalg.norm(edges,axis=1)
    if np.min(lengths)<1e-12:
        raise ValueError('Boundary: совпадающие точки контура.')
    directions = edges/lengths[:,None]
    signed = sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points,np.roll(points,-1,axis=0)))
    normals = np.column_stack((-directions[:,1],directions[:,0]))*(1 if signed>0 else -1)
    previous = np.roll(normals,1,axis=0)
    bisector = previous+normals
    denominator = np.sum(bisector*normals,axis=1)
    if np.min(denominator)<1e-8:
        raise ValueError('Boundary: слишком острый угол для отступа.')
    return [tuple(p) for p in points+bisector*(amount/denominator)[:,None]]


def ring_samples(polygon, count, phase, separation):
    ring = np.asarray(polygon)
    following = np.roll(ring,-1,axis=0)
    lengths = np.linalg.norm(following-ring,axis=1)
    accumulated = np.r_[0.,np.cumsum(lengths)]
    count = max(1,int(count))
    while True:
        values = (np.arange(count)+phase)*accumulated[-1]/count
        edges = np.minimum(len(ring)-1,np.searchsorted(accumulated,values,side='right')-1)
        fractions = (values-accumulated[edges])/np.maximum(lengths[edges],1e-15)
        tangents = following[edges]-ring[edges]
        points = ring[edges]+fractions[:,None]*tangents
        if count<=1 or np.min(np.linalg.norm(np.roll(points,-1,axis=0)-points,axis=1))>=separation:
            break
        count -= 1
    return [(p,t/np.linalg.norm(t)) for p,t in zip(points,tangents)]


def guided_candidates(surface, boundary, guides, pitch_x, pitch_y, angle, staggered,
                      row_count=None, per_row=None, clearance=0., separation=0., flow='mosaic'):
    """Blend nested contour rings and distribute samples by arc length."""
    def area(poly):
        return abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(poly,poly[1:]+poly[:1])))
    guides = sorted(guides,key=area)
    smallest = guides[0]
    cross = [a[0]*b[1]-b[0]*a[1] for a,b in zip(smallest,smallest[1:]+smallest[:1])]
    signed = sum(cross)
    if abs(signed)<1e-12:
        raise ValueError('Guides: у направляющей нет площади.')
    center = np.array([sum((a[i]+b[i])*w for a,b,w in zip(smallest,smallest[1:]+smallest[:1],cross))/(3*signed) for i in range(2)])
    outer = boundary if boundary else outer_contour(surface,center)
    if clearance:
        outer = inset_polygon(outer,clearance)
    theta = np.arange(256)*2*math.pi/256 + math.radians(angle)
    directions = np.column_stack((np.cos(theta),np.sin(theta)))
    profiles = [np.zeros(len(theta))]
    for polygon in guides+[outer]:
        if not inside(center,polygon):
            raise ValueError('Guides: используйте вложенные замкнутые контуры с общим центром.')
        a = np.asarray(polygon)-center
        b = np.roll(a,-1,axis=0)-a
        radii = []
        for direction in directions:
            determinant = direction[0]*b[:,1]-direction[1]*b[:,0]
            valid = abs(determinant)>1e-12
            denominator = np.where(valid,determinant,1.)
            radius = (a[:,0]*b[:,1]-a[:,1]*b[:,0])/denominator
            t = (a[:,0]*direction[1]-a[:,1]*direction[0])/denominator
            hits = sorted(radius[valid&(radius>0)&(t>=-1e-9)&(t<1-1e-9)])
            unique = [r for i,r in enumerate(hits) if i==0 or r-hits[i-1]>1e-7]
            if len(unique)!=1:
                raise ValueError('Guides: контуры должны быть без самопересечений и видимы из общего центра.')
            radii.append(unique[0])
        radii = np.array(radii)
        if np.min(radii-profiles[-1])<=1e-7:
            raise ValueError('Guides: направляющие должны быть вложены и лежать внутри Boundary.')
        profiles.append(radii)
    if row_count is not None:
        if flow=='waves':
            from model_pack_pattern_waves import waves
            yield from waves(outer,guides,PolygonMask,ring_samples,inset_polygon,separation,
                             row_count,per_row,angle,staggered,center,center+directions*profiles[1][:,None])
            return
        from model_pack_pattern_lattice import lattice
        yield from lattice(outer,guides,PolygonMask,ring_samples,separation,
                           row_count,per_row,angle,staggered)
        return
    yield center,np.array([1.,0.])
    row = 0
    total = 1
    for first,last in zip(profiles,profiles[1:]):
        rows = max(1,round(np.mean(last-first)/pitch_y))
        if rows>1000:
            raise ValueError('Guides: уменьшите плотность.')
        for index in range(1,rows+1):
            row += 1
            radii = first+(last-first)*(index/rows)
            ring = center+directions*radii[:,None]
            following = np.roll(ring,-1,axis=0)
            lengths = np.linalg.norm(following-ring,axis=1)
            accumulated = np.r_[0.,np.cumsum(lengths)]
            count = max(1,round(accumulated[-1]/pitch_x))
            total += count
            if total>20000:
                raise ValueError('Guides: уменьшите плотность.')
            for position in range(count):
                value = (position+(.5 if staggered and row%2 else 0))*accumulated[-1]/count
                edge = min(len(ring)-1,np.searchsorted(accumulated,value,side='right')-1)
                fraction = (value-accumulated[edge])/max(lengths[edge],1e-15)
                tangent = following[edge]-ring[edge]
                yield ring[edge]+fraction*tangent,tangent/np.linalg.norm(tangent)


class PolygonMask:
    def __init__(self,polygon):
        self.a = np.asarray(polygon)
        self.b = np.roll(self.a,-1,axis=0)
        dy = self.b[:,1]-self.a[:,1]
        self.slopes = (self.b[:,0]-self.a[:,0])/np.where(dy!=0,dy,1.)

    def contains(self,points):
        points = np.asarray(points)
        x,y = points[:,0,None],points[:,1,None]
        crossings = ((self.a[:,1]>y)!=(self.b[:,1]>y)) & (x<self.slopes*(y-self.a[:,1])+self.a[:,0])
        return np.count_nonzero(crossings,axis=1)%2==1


def placements(source_fn, surface, boundary, exclusions, pitch_x, pitch_y, angle, margin, offset, staggered, guides=None,
               row_count=None, per_row=None, flow='mosaic'):
    if min(pitch_x,pitch_y)<=0 or not all(math.isfinite(v) for v in (pitch_x,pitch_y,angle,margin,offset)):
        raise ValueError('Pitch должен быть больше нуля.')
    points = np.array([tuple(p)[:3] for p in source_fn.getPoints(om.MSpace.kWorld)])
    origin,u,v,n = frame(points)
    average = sum((source_fn.getPolygonNormal(f,om.MSpace.kWorld) for f in range(source_fn.numPolygons)),om.MVector())
    if np.linalg.norm(average)>1e-8 and n@np.asarray(tuple(average))<0:
        n = -n;v = -v
    local = (points-origin)@np.array([u,v,n]).T
    center = (local.min(axis=0)+local.max(axis=0))*.5
    center[2] = local[:,2].max()
    origin += center[0]*u+center[1]*v+center[2]*n
    footprint = hull(local[:,:2]-center[:2])
    if len(footprint)<3:
        raise ValueError('Source: у флоатера нет площади.')
    source_frame = matrix(origin,u,v,n)
    source_matrix = source_fn.dagPath().inclusiveMatrix()
    rotation = math.radians(angle)
    rotate = np.array([[math.cos(rotation),-math.sin(rotation)],[math.sin(rotation),math.cos(rotation)]])
    bounds = np.array([surface.low,[surface.low[0],surface.high[1]],surface.high,[surface.high[0],surface.low[1]]])@rotate
    low,high = bounds.min(axis=0),bounds.max(axis=0)
    nx,ny = math.ceil((high[0]-low[0])/pitch_x)+1,math.ceil((high[1]-low[1])/pitch_y)+1
    separation = 2*max(np.linalg.norm(p) for p in footprint)
    if row_count is not None and not guides:
        low = low+separation*.5+margin+1e-6
        high = high-separation*.5-margin-1e-6
        if np.any(high<=low):
            return []
        nx = min(per_row,max(1,math.floor((high[0]-low[0])/(separation*1.01))+1))
        ny = min(row_count,max(1,math.floor((high[1]-low[1])/(separation*1.01))+1))
        pitch_x = (high[0]-low[0])/max(1,nx-1)
        pitch_y = (high[1]-low[1])/max(1,ny-1)
    if nx*ny>20000 and row_count is None:
        raise ValueError('Слишком много позиций. Уменьшите плотность рисунка.')
    segments = list(surface.boundary)
    for polygon in ([boundary] if boundary else [])+exclusions:
        segments.extend(zip(polygon,polygon[1:]+polygon[:1]))
    segment_array = np.asarray(segments).reshape(-1,2,2)
    segment_low = segment_array.min(axis=1)
    segment_high = segment_array.max(axis=1)
    outer_mask = PolygonMask(boundary) if boundary else None
    excluded_masks = [PolygonMask(p) for p in exclusions]
    def mask_allows(points):
        return ((outer_mask is None or np.all(outer_mask.contains(points))) and
                not any(np.any(mask.contains(points)) for mask in excluded_masks))
    if row_count is not None and guides:
        candidates = guided_candidates(surface,boundary,guides,pitch_x,pitch_y,angle,staggered,
                                       row_count,per_row,separation*.5+margin+1e-6,separation*1.01,flow=flow)
    elif guides:
        candidates = ((p,t,False) for p,t in guided_candidates(surface,boundary,guides,pitch_x,pitch_y,angle,staggered))
    else:
        candidates = ((np.array([low[0]+column*pitch_x+(pitch_x*.5 if staggered and row%2 else 0),low[1]+row*pitch_y])@rotate.T,
                       np.array([math.cos(rotation),math.sin(rotation)]),False) for row in range(ny) for column in range(nx))
    result = []
    accepted = {}
    for p,tangent,anchor in candidates:
        hit = surface.hit(p)
        if hit is None or not mask_allows([p]):
            continue
        position,normal = hit
        direction = surface.u*tangent[0]+surface.v*tangent[1]
        x = direction-normal*(direction@normal)
        if np.linalg.norm(x)<1e-8:
            continue
        x /= np.linalg.norm(x)
        y = np.cross(normal,x)
        outline = [tuple(surface.project(position+normal*offset+x*a+y*b)) for a,b in footprint]
        if not mask_allows(outline) or not all(surface.hit(q) is not None for q in outline):
            continue
        bounds = np.asarray(outline)
        low_outline = bounds.min(axis=0)-margin-1e-7
        high_outline = bounds.max(axis=0)+margin+1e-7
        nearby = segment_array[np.all(segment_high>=low_outline,axis=1)&np.all(segment_low<=high_outline,axis=1)]
        if any(distance(q,a,b)<=margin+1e-7 for q in outline for a,b in nearby):
            continue
        if any(distance(q,a,b)<=margin+1e-7 for q,_ in nearby
               for a,b in zip(outline,outline[1:]+outline[:1])):
            continue
        if any(crosses(a,b,c,d) for a,b in zip(outline,outline[1:]+outline[:1]) for c,d in nearby):
            continue
        if any(inside(a,outline) for a,b in nearby):
            continue
        if guides:
            key = tuple(math.floor(value/separation) for value in position)
            neighbors = (other for delta in itertools.product((-1,0,1),repeat=3)
                         for other in accepted.get(tuple(a+b for a,b in zip(key,delta)),()))
            if any(np.dot(position-other,position-other)<separation*separation for other in neighbors):
                if anchor:
                    raise ValueError('Guides: два закреплённых ряда слишком близко. Уменьшите Source или раздвиньте контуры.')
                continue
            accepted.setdefault(key,[]).append(position)
        result.append(source_matrix*source_frame.inverse()*matrix(position+normal*offset,x,y,normal))
        if len(result)>1500:
            raise ValueError('Более 1500 флоатеров. Уменьшите Rows / Max per Row.')
    return result
