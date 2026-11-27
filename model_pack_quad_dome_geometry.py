"""Convex quad domes: latitude bands and a harmonic rectangular crown."""
import math
import numpy as np


class Patch:
    def __init__(self, boundary, normal):
        p=np.asarray(boundary,dtype=float);n=np.asarray(normal,dtype=float)
        if len(p)<4 or len(p)%2:raise ValueError('Quad Dome: нужен чётный контур минимум из четырёх вершин.')
        n/=np.linalg.norm(n);self.normal=n;self.origin=p.mean(axis=0)
        offsets=p-self.origin;self.scale=float(np.linalg.norm(offsets,axis=1).max())
        if self.scale<1e-8:raise ValueError('Quad Dome: поверхность имеет нулевой размер.')
        if np.max(np.abs(offsets@n))>max(1e-7,self.scale*1e-4):raise ValueError('Quad Dome: выберите плоскую поверхность.')
        u=offsets[np.linalg.norm(offsets,axis=1).argmax()].copy();u-=n*(u@n);u/=np.linalg.norm(u)
        v=np.cross(n,u);self.u=u;self.v=v
        xy=np.column_stack((offsets@u,offsets@v))
        area=np.sum(xy[:,0]*np.roll(xy[:,1],-1)-xy[:,1]*np.roll(xy[:,0],-1))*.5
        self.reverse=area<0
        self.order=list(reversed(range(len(p)))) if self.reverse else list(range(len(p)))
        self.boundary=p[self.order];xy=xy[self.order]
        edges=np.roll(xy,-1,axis=0)-xy
        if np.min(np.linalg.norm(edges,axis=1))<self.scale*1e-8:raise ValueError('Quad Dome: в контуре есть совпадающие вершины.')
        for a,b in zip(xy,edges):
            delta=xy-a
            if np.min(b[0]*delta[:,1]-b[1]*delta[:,0])<-self.scale*self.scale*1e-8:
                raise ValueError('Quad Dome: нужен один выпуклый контур без отверстий.')
        # Polygon area centroid, independent of uneven perimeter sampling.
        q=np.roll(xy,-1,axis=0);cross=xy[:,0]*q[:,1]-xy[:,1]*q[:,0]
        center=np.sum((xy+q)*cross[:,None],axis=0)/(3*cross.sum())
        # Ignore inserted straight-edge samples when fitting the original
        # circular/elliptical support. Odd-side resampling must not bias it.
        previous=np.roll(edges,1,axis=0)
        turning=np.abs(previous[:,0]*edges[:,1]-previous[:,1]*edges[:,0])>self.scale**2*1e-9
        samples=xy[turning]/self.scale;fitted_metric=None
        circle=np.linalg.lstsq(np.column_stack((2*samples,np.ones(len(samples)))),np.sum(samples*samples,axis=1),rcond=None)[0]
        radii=np.linalg.norm(samples-circle[:2],axis=1)
        if radii.max()-radii.min()<1e-5:
            center=circle[:2]*self.scale;fitted_metric=np.eye(2)/(radii.mean()*self.scale)**2
        elif len(samples)>=5:
            x,y=samples[:,0],samples[:,1]
            fit=np.linalg.lstsq(np.column_stack((x*x,2*x*y,y*y,x,y)),np.ones(len(x)),rcond=None)[0]
            matrix=np.array([[fit[0],fit[1]],[fit[1],fit[2]]])
            if np.linalg.eigvalsh(matrix).min()>1e-8:
                c=-.5*np.linalg.solve(matrix,fit[3:]);metric=matrix/(1+c@matrix@c)
                error=np.max(np.abs(np.sum(((samples-c)@metric)*(samples-c),axis=1)-1))
                if error<1e-4:center=c*self.scale;fitted_metric=metric/self.scale**2
        self.origin+=u*center[0]+v*center[1];xy-=center
        self.xy=xy
        self.radius=math.sqrt(abs(area)/math.pi)
        # A positive quadratic fit gives ellipse normals after arbitrary scale
        # and shear. Fall back to a circular metric for undersampled contours.
        z=xy/self.scale
        coefficients=np.linalg.lstsq(np.column_stack((z[:,0]**2,2*z[:,0]*z[:,1],z[:,1]**2)),
                                     np.ones(len(z)),rcond=None)[0]
        metric=np.array([[coefficients[0],coefficients[1]],[coefficients[1],coefficients[2]]])/self.scale**2
        self.metric=fitted_metric if fitted_metric is not None else metric if np.linalg.eigvalsh(metric).min()>1e-10/self.scale**2 else np.eye(2)/self.radius**2

    def crown(self):
        count=len(self.xy);a=max(1,count//4);b=count//2-a
        if count==6:
            # Three ordinary quads meet at one valence-three vertex; no
            # triangulated pole and no flattened two-cell rectangular crown.
            return [tuple(p) for p in self.xy]+[(0.,0.)],[[6,i,(i+1)%6,(i+2)%6] for i in (0,2,4)]
        boundary=([(i,0) for i in range(a)]+[(a,j) for j in range(b)]+
                  [(i,b) for i in range(a,0,-1)]+[(0,j) for j in range(b,0,-1)])
        points={p:self.xy[i].copy() for i,p in enumerate(boundary)}
        interior=[(i,j) for j in range(1,b) for i in range(1,a)]
        if interior:
            lookup={p:i for i,p in enumerate(interior)}
            matrix=np.eye(len(interior))*4;rhs=np.zeros((len(interior),2))
            for p,row in lookup.items():
                for other in ((p[0]-1,p[1]),(p[0]+1,p[1]),(p[0],p[1]-1),(p[0],p[1]+1)):
                    if other in lookup:matrix[row,lookup[other]]-=1
                    else:rhs[row]+=points[other]
            for p,value in zip(interior,np.linalg.solve(matrix,rhs)):points[p]=value
        keys=boundary+interior;indices={p:i for i,p in enumerate(keys)}
        faces=[[indices[i,j],indices[i+1,j],indices[i+1,j+1],indices[i,j+1]] for j in range(b) for i in range(a)]
        return [tuple(points[p]) for p in keys],faces

    def radial(self, point):
        """Ellipsoid coordinates, without polygon-ray ripples at the crown."""
        p=np.asarray(point);length=float(np.linalg.norm(p))
        if length<self.scale*1e-12:return 0.,p
        return min(1.,math.sqrt(float(p@self.metric@p))),p

    def build(self, height, rings):
        if not math.isfinite(height) or abs(height)<=self.scale*1e-6:raise ValueError('Quad Dome: модуль высоты слишком мал.')
        if not isinstance(rings,int) or not 1<=rings<=32:raise ValueError('Quad Dome: Rings должен быть от 1 до 32.')
        count=len(self.xy);points=[tuple(p) for p in self.boundary];faces=[]
        top_angle=math.pi/12 if count==4 else math.pi/4
        for ring in range(1,rings+1):
            angle=math.pi/2+(top_angle-math.pi/2)*ring/rings
            for xy in self.xy:
                points.append(tuple(self.origin+self.u*xy[0]*math.sin(angle)+self.v*xy[1]*math.sin(angle)+self.normal*height*math.cos(angle)))
            base=(ring-1)*count;upper=ring*count
            faces.extend([base+i,base+(i+1)%count,upper+(i+1)%count,upper+i] for i in range(count))
        crown,cells=self.crown();lookup=list(range(rings*count,(rings+1)*count))
        for xy in crown[count:]:
            rho,p=self.radial(xy);angle=top_angle*rho
            radial=math.sin(angle)/rho if rho>1e-12 else top_angle
            lookup.append(len(points))
            points.append(tuple(self.origin+self.u*p[0]*radial+self.v*p[1]*radial+self.normal*height*math.cos(angle)))
        faces.extend([lookup[i] for i in cell] for cell in cells)
        normals=[]
        for index,point in enumerate(points):
            delta=np.asarray(point)-self.origin;xy=np.array([delta@self.u,delta@self.v])
            gradient=self.metric@xy
            normal=self.u*gradient[0]+self.v*gradient[1]+self.normal*(0. if index<count else delta@self.normal)/height**2
            # Reflection into the surface keeps the original face winding.
            # The recess points toward its empty interior, not into the solid.
            if height<0:normal=-normal
            normal/=np.linalg.norm(normal);normals.append(tuple(normal))
        if self.reverse:faces=[list(reversed(f)) for f in faces]
        return points,faces,normals
