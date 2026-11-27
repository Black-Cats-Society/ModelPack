"""Radius-preserving contour inset and projection onto curved face regions."""
import math
import numpy as np

def add(a,b):return a[0]+b[0],a[1]+b[1]
def sub(a,b):return a[0]-b[0],a[1]-b[1]
def mul(a,s):return a[0]*s,a[1]*s
def dot(a,b):return a[0]*b[0]+a[1]*b[1]
def cross(a,b):return a[0]*b[1]-a[1]*b[0]
def length(a):return math.hypot(*a)
def unit(a):return mul(a,1/length(a))
def left(a):return -a[1],a[0]
def area(points):return sum(cross(a,b) for a,b in zip(points,points[1:]+points[:1]))*.5

def _cross_rows(a,b):
    return a[...,0]*b[...,1]-a[...,1]*b[...,0]


class CurvedRegion:
    """Inset in a consistent 2D chart, then sample the original surface."""
    def __init__(self,contour,positions,heights,triangles):
        self.xy=np.asarray(positions,dtype=float)
        self.heights=np.asarray(heights,dtype=float)[triangles]
        projected=self.xy[triangles]
        self.origin=projected[:,0]
        self.ab=projected[:,1]-self.origin
        self.ac=projected[:,2]-self.origin
        self.det=_cross_rows(self.ab,self.ac)
        if not len(self.det) or np.any(self.det*contour.winding<=contour.epsilon**2):
            raise ValueError('Offset: контур изгибается с перекрытием. Выделите участок с одной видимой стороной.')

        boundary=np.asarray(contour.points)
        velocity=np.asarray(contour.motion)
        motion=list(velocity)
        for point in self.xy[len(boundary):]:
            rays=boundary-point
            lengths=np.linalg.norm(rays,axis=1)
            if np.any(lengths<=contour.epsilon):
                raise ValueError('Offset: у изогнутой области совпадают вершины контура.')
            following=np.roll(rays,-1,axis=0)
            angles=np.arctan2(_cross_rows(rays,following),np.sum(rays*following,axis=1))
            half_angles=np.tan(angles*.5)
            weights=(half_angles+np.roll(half_angles,1))/lengths
            motion.append(weights@velocity/weights.sum())
        self.motion=np.asarray(motion)
        if not np.all(np.isfinite(self.motion)):
            raise ValueError('Offset: не удалось построить отступ внутри изогнутой области.')
        self.maximum=self._limit(triangles,contour.maximum)
        if self.maximum<=contour.epsilon*10:
            raise ValueError('Offset: внутри области недостаточно места для отступа.')

    def _limit(self,triangles,maximum):
        # Triangle areas are quadratic in the offset. Stop at the first
        # collapsed triangle, even if it would reopen further into the drag.
        motion=self.motion[triangles]*maximum
        ab=motion[:,1]-motion[:,0]
        ac=motion[:,2]-motion[:,0]
        linear=(_cross_rows(ab,self.ac)+_cross_rows(self.ab,ac))/self.det
        quadratic=_cross_rows(ab,ac)/self.det
        limit=1.0
        for b,c in zip(linear,quadratic):
            if abs(c)<1e-12:
                if b<0:limit=min(limit,-.999999/b)
                continue
            discriminant=b*b-4*c*.999999
            if discriminant<0:continue
            q=-.5*(b+math.copysign(math.sqrt(discriminant),b))
            for root in (q/c,.999999/q) if q else ():
                if 0<root<limit:limit=root
        return maximum*limit

    def at(self,amount):
        result=[]
        for xy in self.xy+self.motion*amount:
            relative=xy-self.origin
            b=_cross_rows(relative,self.ac)/self.det
            c=_cross_rows(self.ab,relative)/self.det
            a=1-b-c
            score=np.minimum(np.minimum(a,b),c)
            face=int(np.argmax(score))
            if score[face]<-1e-7:
                raise ValueError('Offset: внутренний контур вышел за исходную поверхность.')
            height=np.dot((a[face],b[face],c[face]),self.heights[face])
            result.append((float(xy[0]),float(xy[1]),float(height)))
        return result

def intersection(a,u,b,v):
    denominator=cross(u,v)
    if abs(denominator)<1e-9:return None
    return add(a,mul(u,cross(sub(b,a),v)/denominator))

class Contour:
    def __init__(self,points):
        self.points=[tuple(p) for p in points];n=len(points)
        if n<3 or any(not math.isfinite(v) for p in points for v in p):raise ValueError('Некорректный контур полигона.')
        self.scale=max(max(p[a] for p in points)-min(p[a] for p in points) for a in (0,1))
        self.epsilon=max(self.scale*1e-9,1e-10)
        self.edges=[sub(points[(i+1)%n],p) for i,p in enumerate(points)]
        self.lengths=[length(e) for e in self.edges]
        if min(self.lengths)<=self.epsilon:raise ValueError('В контуре есть совпадающие вершины. Сначала объедините их.')
        signed=area(self.points)
        if abs(signed)<=self.epsilon*self.scale:raise ValueError('У полигона нулевая площадь.')
        self.winding=1 if signed>0 else -1
        self.directions=[unit(e) for e in self.edges]
        self.normals=[mul(left(e),self.winding) for e in self.directions]
        if self.intersects(self.points):raise ValueError('Исходный контур пересекает сам себя.')
        turns=[abs(math.atan2(cross(self.directions[i-1],u),dot(self.directions[i-1],u))) for i,u in enumerate(self.directions)]
        if max(turns)<math.radians(55) and min(turns)>1e-6 and max(turns)<min(turns)*1.1 and max(self.lengths)<min(self.lengths)*1.6:
            raise ValueError('Полностью круглый контур нельзя уменьшить с тем же радиусом.')
        self.arcs=self.find_arcs()
        if not self.arcs and max(turns)<math.radians(55) and max(self.lengths)<min(self.lengths)*1.6:
            raise ValueError('Для сохранения радиуса нужны прямые стороны между скруглениями. Полностью круглый контур нельзя уменьшить с тем же радиусом.')
        self.motion=[]
        for i,p in enumerate(points):
            u,v=self.directions[i-1],self.directions[i]
            q=intersection(add(p,self.normals[i-1]),u,add(p,self.normals[i]),v)
            if q is None:
                if dot(u,v)<0:raise ValueError('В контуре есть разворот на 180 градусов.')
                self.motion.append(self.normals[i])
            else:self.motion.append(sub(q,p))
        for arc in self.arcs:
            s,e=arc['ids'][0],arc['ids'][-1]
            corner=intersection(points[s],self.directions[s-1],points[e],self.directions[e])
            moved=intersection(add(points[s],self.normals[s-1]),self.directions[s-1],
                               add(points[e],self.normals[e]),self.directions[e])
            velocity=sub(moved,corner)
            for i in arc['ids']:self.motion[i]=velocity
        bound=self.scale
        for i,edge in enumerate(self.edges):
            slope=dot(sub(self.motion[(i+1)%n],self.motion[i]),unit(edge))
            if slope<-1e-12:bound=min(bound,-self.lengths[i]/slope)
        last=0.
        for i in range(1,33):
            value=bound*i/32
            if not self.valid(value):
                lo,hi=last,value
                for _ in range(40):
                    mid=(lo+hi)*.5
                    if self.valid(mid):lo=mid
                    else:hi=mid
                bound=lo;break
            last=value
        self.maximum=bound*(1-1e-6)
        if self.maximum<=self.epsilon*10:raise ValueError('Для Offset с сохранением радиуса недостаточно места.')

    def line_extent(self,index,step):
        n=len(self.points);direction=self.directions[index%n];total=0.
        for _ in range(n):
            j=index%n
            if dot(direction,self.directions[j])<1-1e-8:break
            total+=self.lengths[j];index+=step
        return total

    def find_arcs(self):
        n=len(self.points);candidates=[]
        for s in range(n):
            incoming_extent=self.line_extent(s-1,-1)
            first,second,third=[self.points[(s+i)%n] for i in range(3)]
            circle=intersection(mul(add(first,second),.5),left(sub(second,first)),
                                mul(add(second,third),.5),left(sub(third,second)))
            circle_radius=length(sub(first,circle)) if circle is not None else 0.
            circle_start=(circle_radius>self.epsilon and abs(dot(sub(first,circle),self.directions[s-1]))<circle_radius*.02)
            long_incoming=incoming_extent>=self.lengths[s]*1.35
            if not long_incoming and not circle_start:continue
            for count in range(3,n):
                ids=[(s+i)%n for i in range(count)];e=ids[-1]
                circle_error=max(abs(length(sub(self.points[i],circle))-circle_radius) for i in ids)/circle_radius if circle_start else math.inf
                if not long_incoming and circle_error>.0075:break
                u,v=self.directions[s-1],self.directions[e]
                turn=math.atan2(cross(u,v),dot(u,v))
                if abs(turn)<math.radians(8) or abs(turn)>math.radians(172):continue
                chords=[self.lengths[i] for i in ids[:-1]]
                circular=circle_error<.0075 and abs(dot(sub(self.points[e],circle),v))<circle_radius*.02
                if min(incoming_extent,self.line_extent(e,1))<max(chords)*1.35 and not circular:continue
                corner=intersection(self.points[s],u,self.points[e],v)
                a,b=sub(self.points[s],corner),sub(self.points[e],corner);den=cross(a,b)
                if abs(den)<=self.epsilon**2:continue
                normalized=[]
                for i in ids[1:-1]:
                    q=sub(self.points[i],corner)
                    alpha,beta=cross(q,b)/den,cross(a,q)/den
                    normalized.append((alpha,beta))
                # This tangent triangle is affine-invariant. It recognizes a
                # circular fillet after nonuniform scale/shear too, and keeps
                # the entire sampled shape rigid rather than refitting it.
                if any(min(a,b)<-1e-5 or not .3<a+b<1-1e-7 for a,b in normalized):continue
                center=intersection(self.points[s],left(u),self.points[e],left(v))
                if center is None:continue
                radii=[length(sub(self.points[i],center)) for i in ids]
                radius=(radii[0]+radii[-1])*.5
                if radius<=self.epsilon:continue
                error=max(abs(r-radius) for r in radii)/radius
                # Nonuniformly scaled circles are ellipses; their original
                # points must still be preserved without forcing a new circle.
                signed=[]
                for a,b in zip(ids[:-1],ids[1:]):
                    signed.append(cross(sub(self.points[a],center),sub(self.points[b],center)))
                if not all(t*turn>self.epsilon**2 for t in signed):continue
                # Tangency endpoints must lead into the arc, rather than away
                # from it (a long sharp triangle is not a rounded corner).
                if dot(sub(self.points[ids[1]],self.points[s]),u)<=0:continue
                if dot(sub(self.points[e],self.points[ids[-2]]),v)<=0:continue
                candidates.append((count,-error,s,dict(ids=ids,center=center,radius=radius)))
        used=set();result=[]
        for _,_,_,arc in sorted(candidates,reverse=True):
            if used.intersection(arc['ids']):continue
            result.append(arc);used.update(arc['ids'])
        return result

    def at(self,amount):return [add(p,mul(v,amount)) for p,v in zip(self.points,self.motion)]

    def inside(self,p):
        result=False
        for a,b in zip(self.points,self.points[1:]+self.points[:1]):
            edge=sub(b,a);t=dot(sub(p,a),edge)/dot(edge,edge)
            if -1e-9<=t<=1+1e-9 and abs(cross(edge,sub(p,a)))<=self.epsilon*length(edge):return True
            if (a[1]>p[1])!=(b[1]>p[1]) and p[0]<(b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0]:result=not result
        return result

    def intersects(self,points):
        n=len(points)
        for i,a in enumerate(points):
            b=points[(i+1)%n];u=sub(b,a)
            for j in range(i+2,n):
                if i==0 and j==n-1:continue
                c,d=points[j],points[(j+1)%n];v=sub(d,c);den=cross(u,v)
                if abs(den)<=self.epsilon**2:
                    if abs(cross(u,sub(c,a)))<=self.epsilon*max(length(u),self.epsilon):
                        if max(min(a[0],b[0]),min(c[0],d[0]))<=min(max(a[0],b[0]),max(c[0],d[0]))+self.epsilon and max(min(a[1],b[1]),min(c[1],d[1]))<=min(max(a[1],b[1]),max(c[1],d[1]))+self.epsilon:return True
                    continue
                t=cross(sub(c,a),v)/den;s=cross(sub(c,a),u)/den
                if -1e-9<=t<=1+1e-9 and -1e-9<=s<=1+1e-9:return True
        return False

    def valid(self,amount):
        if amount==0:return True
        points=self.at(amount)
        if area(points)*self.winding<=self.epsilon*self.scale:return False
        if any(dot(sub(points[(i+1)%len(points)],p),self.directions[i])<=self.epsilon for i,p in enumerate(points)):return False
        if self.intersects(points):return False
        for i,p in enumerate(points):
            if not self.inside(p) or not self.inside(mul(add(p,points[(i+1)%len(points)]),.5)):return False
        return True
