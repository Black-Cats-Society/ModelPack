"""A quad groove following a mesh, including the normal field of its skirts."""
import math
from maya.api import OpenMaya as om
from model_pack_floater_geometry import OffsetError,skirt_support


class Surface:
    """Read an evaluated worldMesh; never modify the support geometry."""
    def __init__(self,data):
        self.data=data;self.fn=om.MFnMesh(data);matrix=om.MFnMeshData(data).matrix
        if not matrix.isEquivalent(om.MMatrix(),1e-12):
            # worldMesh carries its transform in mesh-data metadata. Bake a
            # private copy for correct world-distance queries under scale.
            points=[om.MPoint(p)*matrix for p in self.fn.getPoints()]
            normals=[om.MVector(n).transformAsNormal(matrix).normal() for n in self.fn.getNormals()]
            counts,normal_ids=self.fn.getNormalIds();_,vertex_ids=self.fn.getVertices()
            face_ids=[i for i,count in enumerate(counts) for _ in range(count)]
            self.data=om.MFnMeshData().create();om.MFnMesh().copy(data,self.data);self.fn=om.MFnMesh(self.data)
            self.fn.setPoints(points);self.fn.getNormals()
            self.fn.setFaceVertexNormals([normals[i] for i in normal_ids],face_ids,vertex_ids)
            om.MFnMeshData(self.data).matrix=om.MMatrix();self.fn.updateSurface()
        self.tree=om.MMeshIntersector();self.tree.create(self.data);self.normal_cache={}
        self.accel=self.fn.autoUniformGridParams()
        if not self.fn.numPolygons:raise ValueError('Floater: опорный меш пуст.')
    def normal(self,face,triangle,a,b):
        key=face,triangle
        if key not in self.normal_cache:
            vertices=self.fn.getPolygonTriangleVertices(*key)
            self.normal_cache[key]=[self.fn.getFaceVertexNormal(face,v) for v in vertices]
        normals=self.normal_cache[key]
        return (normals[0]*a+normals[1]*b+normals[2]*(1-a-b)).normal()
    def detail(self,point):
        hit=self.tree.getClosestPoint(point);base=om.MPoint(hit.point)
        face=hit.face
        normal=self.normal(hit.face,hit.triangle,*hit.barycentricCoords)
        # Closest points jump between neighboring facets when a curve moves
        # just beneath a curved mesh. Project along its smooth normal field
        # instead, on both sides, and retain the local sheet of the surface.
        radius=max((point-base).length()*3,1.)
        for _ in range(3):
            result=self.fn.closestIntersection(om.MFloatPoint(point),om.MFloatVector(normal),om.MSpace.kObject,radius,True,accelParams=self.accel)
            if result is None:break
            candidate=om.MPoint(result[0])
            if (candidate-base).length()>radius:break
            base=candidate;face=result[2];normal=self.normal(result[2],result[3],result[4],result[5])
        if normal.length()<.5:raise ValueError('Floater: у поверхности некорректные нормали.')
        return base,normal,face
    def closest(self,point):return self.detail(point)[:2]
    def triangles(self):
        if hasattr(self,'triangle_cache'):return self.triangle_cache
        points=self.fn.getPoints();counts,vertices=self.fn.getTriangles();triangles={};edges={};index=0
        for face,count in enumerate(counts):
            for triangle in range(count):
                ids=vertices[index:index+3];index+=3;p,q,r=[om.MPoint(points[v]) for v in ids]
                u=q-p;v=r-p;uu=u*u;uv=u*v;vv=v*v;den=uu*vv-uv*uv
                if den<=max(uu*vv*1e-14,1e-30):continue
                key=face,triangle
                triangles[key]=(ids,p,u,v,uu,uv,vv,den,(u^v).normal())
                for a,b in ((0,1),(1,2),(2,0)):edges.setdefault(tuple(sorted((ids[a],ids[b]))),[]).append(key)
        self.triangle_cache=triangles,edges
        return self.triangle_cache
    @staticmethod
    def barycentric(point,triangle):
        _,p,u,v,uu,uv,vv,den,_=triangle;w=point-p;wu=w*u;wv=w*v
        b=(wu*vv-wv*uv)/den;c=(wv*uu-wu*uv)/den
        return 1-b-c,b,c
    def walk(self,point,direction,length):
        """Unfold mesh triangles so offsets follow its shape and arc distance.

        Smooth shading normals are used only for the final height/shading,
        never to choose a different surface sheet or shear the quad rows.
        Internal triangulation of a flat polygon does not add profile loops.
        """
        triangles,edges=self.triangles();hit=self.tree.getClosestPoint(point)
        key=hit.face,hit.triangle;base=om.MPoint(hit.point)
        epsilon=max(length*1e-8,1e-8);candidates={key};pending=[key]
        # A curve vertex can lie exactly on a mesh edge/vertex. Choose the
        # incident triangle into which this side of the skirt actually goes.
        while pending:
            current=pending.pop();triangle=triangles.get(current)
            if triangle is None:continue
            ids=triangle[0];weights=self.barycentric(base,triangle)
            for i,w in enumerate(weights):
                if abs(w)>1e-5:continue
                edge=tuple(sorted(ids[j] for j in range(3) if j!=i))
                for neighbor in edges[edge]:
                    if neighbor not in candidates:candidates.add(neighbor);pending.append(neighbor)
        options=[]
        for candidate in sorted(candidates):
            triangle=triangles.get(candidate)
            if triangle is None:continue
            normal=triangle[-1];tangent=direction-normal*(direction*normal)
            if tangent.length()<1e-8:continue
            tangent.normalize();weights=self.barycentric(base,triangle);ahead=self.barycentric(base+tangent,triangle)
            derivatives=[b-a for a,b in zip(weights,ahead)]
            if not any(w<=1e-5 and d<-1e-7 for w,d in zip(weights,derivatives)):
                options.append((tangent*direction.normal(),candidate,tangent));continue
            # At a convex crease the smooth tangent can point above both
            # incident faces. Follow the shared edge instead of jumping sheets.
            for i,w in enumerate(weights):
                if abs(w)>1e-5:continue
                ids=triangle[0];edge_points=[self.fn.getPoint(ids[j]) for j in range(3) if j!=i]
                edge_tangent=(edge_points[1]-edge_points[0]).normal()
                if edge_tangent*direction<0:edge_tangent=-edge_tangent
                ahead=self.barycentric(base+edge_tangent,triangle);derivatives=[b-a for a,b in zip(weights,ahead)]
                if not any(w<=1e-5 and d<-1e-7 for w,d in zip(weights,derivatives)):
                    options.append((edge_tangent*direction.normal(),candidate,edge_tangent))
        if not options:raise OffsetError('Floater: юбка выходит за границу опорной поверхности.')
        _,key,tangent=max(options,key=lambda value:(value[0],value[1]))
        result=[(0.,base,self.closest(base)[1])];travel=0.;zero_crossings=set()
        for _ in range(8192):
            triangle=triangles[key];ids=triangle[0];weights=self.barycentric(base,triangle)
            ahead=self.barycentric(base+tangent,triangle);derivatives=[b-a for a,b in zip(weights,ahead)]
            exits=[(max(0.,-w/d),i) for i,(w,d) in enumerate(zip(weights,derivatives)) if d<-1e-10]
            if not exits:raise OffsetError('Floater: не удалось продолжить юбку по поверхности.')
            step,opposite=min(exits);remaining=length-travel
            if step>=remaining-epsilon:
                base=base+tangent*remaining;result.append((length,base,self.closest(base)[1]));return result
            base=base+tangent*step;travel+=step
            edge=tuple(sorted(ids[j] for j in range(3) if j!=opposite))
            neighbors=[k for k in edges[edge] if k!=key]
            if len(neighbors)!=1:raise OffsetError('Floater: юбка выходит за границу опорной поверхности.')
            neighbor=neighbors[0];next_triangle=triangles[neighbor]
            tangent=tangent.rotateBy(triangle[-1].rotateTo(next_triangle[-1])).normal()
            if triangle[-1]*next_triangle[-1]<1-1e-8:
                if result[-1][0]>travel-epsilon:result[-1]=(travel,base,self.closest(base)[1])
                else:result.append((travel,base,self.closest(base)[1]))
            if step<=epsilon:
                if (key,neighbor) in zero_crossings:raise OffsetError('Floater: юбка проходит через вырожденный угол поверхности.')
                zero_crossings.add((key,neighbor))
            else:zero_crossings.clear()
            key=neighbor
        raise OffsetError('Floater: юбка пересекает слишком много граней поверхности.')
    def face_plane(self,face):
        if not hasattr(self,'plane_cache'):self.plane_cache={};self.edge_cache={};self.points=self.fn.getPoints()
        if face not in self.plane_cache:
            self.plane_cache[face]=(self.points[self.fn.getPolygonVertices(face)[0]],self.fn.getPolygonNormal(face).normal())
        return self.plane_cache[face]
    def face_edges(self,face):
        self.face_plane(face)
        if face not in self.edge_cache:
            iterator=om.MItMeshPolygon(self.data);iterator.setIndex(face);self.edge_cache[face]=set(iterator.getEdges())
        return self.edge_cache[face]
    def breaks(self,a,b,pa=None,pb=None):
        """Only real support-face bends; smooth normals never add divisions."""
        pa=pa or self.detail(a);pb=pb or self.detail(b)
        length=(b-a).length();epsilon=max(length*1e-7,max(abs(p[k]) for p in (pa[0],pb[0]) for k in range(3))*2e-7,1e-7);result=[]
        def on_plane(point,face):
            origin,normal=self.face_plane(face);return abs((point-origin)*normal)<=epsilon
        def visit(low,high,left,right,depth=0):
            middle=(low+high)*.5;source=a+(b-a)*middle;center=self.detail(source)
            if on_plane(left[0],center[2]) and on_plane(right[0],center[2]):return
            shared=self.face_edges(left[2])&self.face_edges(right[2])
            if shared and center[2] in (left[2],right[2]):
                # Locate the change of physical face, rather than retaining
                # the dyadic probing points inside either polygon.
                first,last=low,high
                for _ in range(28):
                    t=(first+last)*.5;sample=self.detail(a+(b-a)*t)
                    if on_plane(sample[0],left[2]):first=t
                    else:last=t
                    if (last-first)*length<=epsilon:break
                t=(first+last)*.5;sample=self.detail(a+(b-a)*t);candidates=[]
                for edge in shared:
                    i,j=self.fn.getEdgeVertices(edge);p,q=self.points[i],self.points[j];vector=q-p;squared=vector*vector
                    point=p+vector*max(0.,min(1.,((sample[0]-p)*vector)/squared)) if squared else p
                    candidates.append(((point-sample[0]).length(),point,vector))
                distance,point,edge_direction=min(candidates,key=lambda item:item[0])
                if distance<=max(length*.01,epsilon*10) and low+1e-6<t<high-1e-6:
                    normal=self.closest(point)[1];result.append((t,point,normal,a+(b-a)*t,edge_direction))
                return
            if depth>=18 or (high-low)*length<=epsilon*4:return
            visit(low,middle,left,center,depth+1);visit(middle,high,center,right,depth+1)
        visit(0.,1.,pa,pb);result.sort(key=lambda item:item[0]);compact=[]
        for item in result:
            if not compact or (item[1]-compact[-1][1]).length()>epsilon*4:compact.append(item)
        return compact


def world_mesh(path):
    return om.MFnDependencyNode(path.node()).findPlug('worldMesh',False).elementByLogicalIndex(path.instanceNumber()).asMObject()


def _compact_trace(trace,tolerance,stops=()):
    """Collapse only bends whose positional error is below the mesh tolerance."""
    # Groove/lip/support points are fixed before simplification, so growing
    # the distant outer boundary cannot move an existing support loop.
    original=trace;trace=list(trace)
    for stop in stops:
        if any(abs(d-stop)<1e-12 for d,p,n in trace):continue
        for (a,p,n),(b,q,m) in zip(original,original[1:]):
            if a<stop<b:
                t=(stop-a)/(b-a);trace.append((stop,p+(q-p)*t,(n*(1-t)+m*t).normal()));break
    trace.sort(key=lambda item:item[0])
    keep={0,len(trace)-1}|{i for i,(d,p,n) in enumerate(trace) if any(abs(d-stop)<1e-12 for stop in stops)}
    ordered=sorted(keep);pending=list(zip(ordered,ordered[1:]))
    while pending:
        a,b=pending.pop()
        if b<=a+1:continue
        da,p,_=trace[a];db,q,_=trace[b]
        error,index=max(((trace[i][1]-(p+(q-p)*((trace[i][0]-da)/(db-da)))).length(),i) for i in range(a+1,b))
        if error>tolerance:keep.add(index);pending.extend(((a,index),(index,b)))
    return [trace[i] for i in sorted(keep)]


def _simple(points,closed,epsilon):
    """Reject actual 3D crossings without rejecting a loop around a cylinder."""
    from collections import defaultdict
    count=len(points) if closed else len(points)-1
    span=max(max(p[k] for p in points)-min(p[k] for p in points) for k in range(3))
    cell=max(span/math.sqrt(len(points)),epsilon*10);buckets=defaultdict(list)
    def distance(a,b,c,d):
        u=b-a;v=d-c;w=a-c;aa=u*u;bb=u*v;cc=v*v;dd=u*w;ee=v*w;den=aa*cc-bb*bb
        s=max(0.,min(1.,(bb*ee-cc*dd)/den)) if den>aa*cc*1e-12 else 0.
        t=(bb*s+ee)/cc if cc else 0.
        if t<0:t=0.;s=max(0.,min(1.,-dd/aa)) if aa else 0.
        elif t>1:t=1.;s=max(0.,min(1.,(bb-dd)/aa)) if aa else 0.
        return (w+u*s-v*t).length()
    for i in range(count):
        a,b=points[i],points[(i+1)%len(points)];keys=[];candidates=set()
        ranges=[range(math.floor((min(a[k],b[k])-epsilon)/cell),math.floor((max(a[k],b[k])+epsilon)/cell)+1) for k in range(3)]
        for x in ranges[0]:
            for y in ranges[1]:
                for z in ranges[2]:
                    key=x,y,z;keys.append(key);candidates.update(buckets[key])
        for j in candidates:
            if i-j==1 or closed and i==count-1 and j==0:continue
            if distance(a,b,points[j],points[(j+1)%len(points)])<=epsilon:return False
        for key in keys:buckets[key].append(i)
    return True


def build(points,closed,width,height,skirt,surface,_fallback=False,reverse=False):
    if not all(math.isfinite(v) for v in (width,height,skirt)) or width<=0 or min(height,skirt)<0:
        raise ValueError('Floater: неверные размеры.')
    # A closed outline taken from a cap is a profile, rather than an interior
    # surface trace. Continue its cap/skirt outward instead of unfolding the
    # outer half down the wall at an ambiguous hard corner. Keep the original
    # per-point heights for slightly twisted imported caps.
    if closed:
        tolerance=max(max(abs(p[k]) for p in points for k in range(3))*2e-7,1e-7)
        vertices=surface.fn.getPoints()
        for face in range(surface.fn.numPolygons):
            ring=list(surface.fn.getPolygonVertices(face))
            if len(ring)!=len(points):continue
            matched=[min(range(len(ring)),key=lambda i:(vertices[ring[i]]-p).length()) for p in points]
            if len(set(matched))!=len(ring):continue
            if any((vertices[ring[i]]-p).length()>tolerance for i,p in zip(matched,points)):continue
            steps={(b-a)%len(ring) for a,b in zip(matched,matched[1:]+matched[:1])}
            if steps not in ({1},{len(ring)-1}):continue
            from model_pack_floater_geometry import build as cap_profile
            return cap_profile(points,closed,width,height,skirt,surface.fn.getPolygonNormal(face)*(-1 if reverse else 1))
    span=max(max(p[k] for p in points)-min(p[k] for p in points) for k in range(3));epsilon=max(span*1e-8,1e-8)
    rows=[];projected=[surface.detail(p) for p in points]
    def refine(a,b,pa,pb,depth=0):
        middle=a+(b-a)*.5;pm=surface.detail(middle);vector=pb[0]-pa[0];squared=vector*vector
        nearest=pa[0]+vector*max(0.,min(1.,((pm[0]-pa[0])*vector)/squared)) if squared else pa[0]
        if (pm[0]-nearest).length()<=max(width*.01,span*1e-6,1e-7) or depth>=12:
            rows.append((*pa[:2],a,None));return
        refine(a,middle,pa,pm,depth+1);refine(middle,b,pm,pb,depth+1)
    for i in range(len(points) if closed else len(points)-1):
        nxt=(i+1)%len(points)
        if _fallback:refine(points[i],points[nxt],projected[i],projected[nxt])
        else:
            rows.append((*projected[i][:2],points[i],None))
            rows.extend((p,n,source,edge) for t,p,n,source,edge in surface.breaks(points[i],points[nxt],projected[i],projected[nxt]))
        if len(rows)>4096:raise ValueError('Floater: кривая пересекает слишком много граней поверхности.')
    if not closed:rows.append((*projected[-1][:2],points[-1],None))
    compact=[]
    for row in rows:
        if not compact or (row[0]-compact[-1][0]).length()>epsilon*10:compact.append(row)
    rows=compact;count=len(rows)
    if count<(3 if closed else 2):raise ValueError('Floater: проекция кривой на меш имеет нулевой размер.')
    if not _simple([row[0] for row in rows],closed,epsilon):raise ValueError('Floater: проекция кривой на меш пересекает сама себя.')
    laterals=[]
    for i,(point,normal,source,edge) in enumerate(rows):
        directions=[]
        if closed or i>0:directions.append(source-rows[(i-1)%count][2])
        if closed or i<count-1:directions.append(rows[(i+1)%count][2]-source)
        directions=[(d-normal*(d*normal)).normal() for d in directions]
        if any(d.length()<.5 for d in directions):raise ValueError('Floater: кривая идёт поперёк нормали поверхности.')
        tangent=sum(directions,om.MVector());den=1. if len(directions)==1 else math.sqrt(max(0.,(1+directions[0]*directions[1])*.5))
        if den<.01 or tangent.length()<1e-6:raise ValueError('Floater: слишком резкий разворот кривой на поверхности.')
        lateral=(normal^tangent.normal())/den
        laterals.append(lateral)
    half=width*.5
    anchors=[-half,-width*.25,0.,width*.25,half]
    if skirt>epsilon:
        support=skirt_support(width,skirt)
        anchors=[-half-skirt,-half-support]+anchors+[half+support,half+skirt]
    traces=[]
    for (point,normal,_,_),lateral in zip(rows,laterals):
        length=lateral.length();sides=[]
        for sign in (-1.,1.):
            trace=surface.walk(point,lateral*sign,anchors[-1]*length)
            trace=_compact_trace(trace,max(width*.001,span*1e-7,1e-7),[abs(a)*length for a in anchors if a*sign>0])
            sides.append([(sign*d/length,p,n) for d,p,n in trace])
        traces.append(list(reversed(sides[0]))+sides[1][1:])
    def at(trace,distance):
        for (a,p,n),(b,q,m) in zip(trace,trace[1:]):
            if distance<=b+epsilon:
                t=max(0.,min(1.,(distance-a)/(b-a))) if b>a else 0.
                point=p+(q-p)*t
                return point,surface.closest(point)[1]
        return trace[-1][1:]
    # Each row walks across the actual support faces. Match the minimal
    # number of physical bends per band without inserting triangle diagonals.
    row_distances=[[anchors[0]] for _ in rows]
    for a,b in zip(anchors,anchors[1:]):
        sections=[[a]+[d for d,p,n in trace if a+epsilon<d<b-epsilon]+[b] for trace in traces]
        divisions=max(len(section)-1 for section in sections)
        if divisions>32:raise OffsetError('Floater: юбка слишком велика для кривизны поверхности.')
        for distances,section in zip(row_distances,sections):
            while len(section)<divisions+1:
                i=max(range(len(section)-1),key=lambda j:section[j+1]-section[j]);section.insert(i+1,(section[i]+section[i+1])*.5)
            distances.extend(section[1:])
    columns=len(row_distances[0]);vertices=[];normals=[];clearance=max(width*1e-5,1e-7)
    for i,((point,normal,_,_),lateral) in enumerate(zip(rows,laterals)):
        for distance in row_distances[i]:
            base,n=at(traces[i],distance)
            if n*normal<.1:raise OffsetError('Floater: ширина или юбка переходит на другую сторону меша.')
            h=height*.5*(1-math.cos(math.tau*distance/width)) if abs(distance)<half else height
            slope=height*math.pi/width*math.sin(math.tau*distance/width) if abs(distance)<half else 0.
            side=(lateral-n*(lateral*n)).normal()
            vertices.append(base+n*((h+clearance)*(-1 if reverse else 1)));normals.append((n*(-1 if reverse else 1)-side*(slope/lateral.length())).normal())
    # Check every quad, not just the outer rails: a folded interior strip
    # would still have four indices but is not a usable quad skirt.
    for i in range(count if closed else count-1):
        nxt=(i+1)%count
        for j in range(columns-1):
            ids=[i*columns+j,nxt*columns+j,nxt*columns+j+1,i*columns+j+1]
            if reverse:ids.reverse()
            p,q,r,s=[vertices[k] for k in ids];a=(q-p)^(r-p);b=(r-p)^(s-p)
            n=sum((normals[k] for k in ids),om.MVector())
            if a*b<=0 or min(a*n,b*n)<=0:
                if not _fallback:return build(points,closed,width,height,skirt,surface,True,reverse=reverse)
                raise OffsetError('Floater: ширина или юбка складывает квады на этом изгибе.')
    # Reject folded strips, including a width which overtakes a tight bend.
    for j in (0,columns-1):
        ring=[vertices[i*columns+j] for i in range(count)]
        for i in range(count if closed else count-1):
            nxt=(i+1)%count
            if (ring[nxt]-ring[i])*(rows[nxt][0]-rows[i][0])<=epsilon*epsilon:
                if not _fallback:return build(points,closed,width,height,skirt,surface,True,reverse=reverse)
                raise OffsetError('Floater: ширина или юбка слишком велики для этого изгиба.')
        if not _simple(ring,closed,epsilon):
            if not _fallback:return build(points,closed,width,height,skirt,surface,True,reverse=reverse)
            raise OffsetError('Floater: юбка пересекает сама себя.')
    lengths=[0.]
    for i in range(count-1):lengths.append(lengths[-1]+(rows[i+1][0]-rows[i][0]).length())
    total=lengths[-1]+((rows[0][0]-rows[-1][0]).length() if closed else 0.)
    uvs=[((d-anchors[0])/(anchors[-1]-anchors[0]),length/max(total,epsilon)) for length,distances in zip(lengths,row_distances) for d in distances]
    if closed:uvs.extend(((d-anchors[0])/(anchors[-1]-anchors[0]),1.) for d in row_distances[0])
    faces=[];uv_ids=[]
    for i in range(count if closed else count-1):
        nxt=(i+1)%count
        for j in range(columns-1):
            face=[i*columns+j,nxt*columns+j,nxt*columns+j+1,i*columns+j+1]
            uv=[i*columns+j,(i+1)*columns+j,(i+1)*columns+j+1,i*columns+j+1]
            faces.append(face[::-1] if reverse else face)
            uv_ids.extend(uv[::-1] if reverse else uv)
    return vertices,faces,normals,uvs,uv_ids
