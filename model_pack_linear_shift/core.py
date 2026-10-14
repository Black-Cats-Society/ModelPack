"""Edge-chain analysis and reversible vertex movement without topology edits."""
from dataclasses import dataclass,replace
import math
from maya import cmds
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn, _ordered_chain, _edge_faces, _polygon_edges
from model_pack_bevel.session import without_undo


def identity(fn):
    counts,vertices=fn.getVertices()
    return (om.MFnDependencyNode(fn.object()).uuid().asString(),tuple(counts),tuple(vertices))


def fit_plane(points,transverse=(),*,allow_secondary=True):
    """Fit a section crossed by the rails, even for very narrow loops.

    The smallest covariance axis of a thin loop can be its thickness axis,
    parallel to the slide rails. Use the next axis in that case. The longest
    axis stays in the section; a straight chain has no unique section plane.
    """
    center=sum((om.MVector(p) for p in points),om.MVector())/len(points)
    offsets=[om.MVector(p)-center for p in points]
    matrix=[[sum(p[i]*p[j] for p in offsets) for j in range(3)] for i in range(3)]
    vectors=[[float(i==j) for j in range(3)] for i in range(3)]
    scale=sum(matrix[i][i] for i in range(3))
    for _ in range(30):
        p,q=max(((0,1),(0,2),(1,2)),key=lambda pair:abs(matrix[pair[0]][pair[1]]))
        if abs(matrix[p][q])<=scale*1e-14:break
        angle=.5*math.atan2(2*matrix[p][q],matrix[q][q]-matrix[p][p]);c,s=math.cos(angle),math.sin(angle)
        a,b,d=matrix[p][p],matrix[p][q],matrix[q][q]
        matrix[p][p]=c*c*a-2*s*c*b+s*s*d
        matrix[q][q]=s*s*a+2*s*c*b+c*c*d
        matrix[p][q]=matrix[q][p]=0.
        for r in range(3):
            if r not in (p,q):
                a,b=matrix[r][p],matrix[r][q]
                matrix[r][p]=matrix[p][r]=c*a-s*b
                matrix[r][q]=matrix[q][r]=s*a+c*b
            a,b=vectors[r][p],vectors[r][q]
            vectors[r][p]=c*a-s*b;vectors[r][q]=s*a+c*b
    order=sorted(range(3),key=lambda i:matrix[i][i])
    if matrix[order[1]][order[1]]<=max(scale*1e-10,1e-24):return None
    directions=[vector.normal() for vector in transverse if vector.length()>1e-12]
    for axis in order[:2 if allow_secondary else 1]:
        normal=om.MVector(*(vectors[i][axis] for i in range(3))).normal()
        if not directions or max(abs(normal*direction) for direction in directions)>.25:
            return center,normal
    return None


def plane_field(chain,grip,center,normal,*,pivot=None):
    """A helper grip chooses its own hinge; mesh pins remain vertex stops."""
    def project(point):
        offset=om.MVector(point)-center
        return offset-normal*(offset*normal)
    q=project(grip);pins=[project(chain.points[v]) for v in sorted(chain.pinned)]
    tolerance=chain.length*1e-8
    if pivot is not None:
        pivot=project(pivot);direction=q-pivot;span=direction*direction
        if span<=tolerance*tolerance:raise ValueError("Недостаточно места для наклона плоскости.")
        gradient=direction/span
        return gradient,-(gradient*pivot)
    if len(pins)>1:
        first=pins[0];last=max(pins,key=lambda p:(p-first).length());axis=last-first
        if axis.length()>tolerance:
            if any((axis^(p-first)).length()>tolerance*axis.length() for p in pins):
                raise ValueError("Закреплённые вершины фиксируют всю плоскость сечения. Выделите отдельный участок.")
            direction=(normal^axis).normal();span=(q-first)*direction
            if abs(span)<=tolerance:raise ValueError("Точка захвата лежит на закреплённой оси. Потяните за другой участок.")
            gradient=direction/span
            return gradient,-(gradient*first)
    if pins:pivot=pins[0];direction=q-pivot
    elif chain.closed:
        direction=q
        if direction.length()<=tolerance:raise ValueError("Не удалось определить сторону наклона сечения.")
        direction=direction.normal()
        pivot=min((project(p) for p in chain.points.values()),key=lambda p:p*direction)
    else:
        pivot=max((project(chain.points[v]) for v in (chain.vertices[0],chain.vertices[-1])),key=lambda p:(p-q).length())
        direction=q-pivot
    span=(q-pivot)*direction
    if abs(span)<=tolerance*tolerance:raise ValueError("Недостаточно места для наклона плоскости.")
    gradient=direction/span
    return gradient,-(gradient*pivot)


@dataclass
class Chain:
    mesh: str
    edges: tuple
    vertices: tuple
    closed: bool
    points: dict
    distances: tuple
    length: float
    pinned: frozenset
    topology: tuple
    rails: dict

    def weights(self,edge,fraction):
        index=self.edges.index(edge);fraction=max(0.,min(1.,fraction))
        end=self.distances[index+1] if index+1<len(self.distances) else self.length
        grip=self.distances[index]+fraction*(end-self.distances[index])
        epsilon=self.length*1e-9
        if not self.closed:
            endpoint=self.vertices[0] if grip<=epsilon else self.vertices[-1] if self.length-grip<=epsilon else None
            if endpoint in self.pinned:
                raise ValueError("Этот конец закреплён. Потяните за соседний участок цепочки.")
        result={}
        for vertex,distance in zip(self.vertices,self.distances):
            if self.closed:
                separation=abs(distance-grip)
                weight=1.-2.*min(separation,self.length-separation)/self.length
            elif grip<=epsilon:weight=1.-distance/self.length
            elif self.length-grip<=epsilon:weight=distance/self.length
            elif distance<=grip:weight=distance/grip
            else:weight=(self.length-distance)/(self.length-grip)
            result[vertex]=0. if vertex in self.pinned else max(0.,min(1.,weight))
        return result


def selected_chains():
    """Return edge chains or a projected vertex/face array per selected mesh."""
    selected={};selection=om.MGlobal.getActiveSelectionList()
    for i in range(selection.length()):
        try:path,component=selection.getComponent(i)
        except RuntimeError:raise ValueError("Выделите рёбра, вершины или полигоны меша.")
        kind=next((name for name,api in (('edge',om.MFn.kMeshEdgeComponent),('vertex',om.MFn.kMeshVertComponent),('face',om.MFn.kMeshPolygonComponent))
            if not component.isNull() and component.hasFn(api)),None)
        if kind is None:raise ValueError("Выделите рёбра, вершины или полигоны меша.")
        if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
        selected.setdefault((path.fullPathName(),kind),set()).update(om.MFnSingleIndexedComponent(component).getElements())
    if not selected:raise ValueError("Выделите рёбра, вершины или полигоны для Plane Deform.")
    if len({kind for _,kind in selected})>1:raise ValueError("Используйте один тип компонентов: рёбра, вершины или полигоны.")
    from . import components
    return [target for (mesh,kind),ids in selected.items() for target in
        (analyze(mesh,ids) if kind=='edge' else [components.analyze(mesh,ids,kind)])]


def target_key(target):
    return (target.topology[0],getattr(target,'kind','edge'),
        tuple(sorted(getattr(target,'components',target.edges))))


def _slide_rails(fn,ordered,vertices,closed,edge_faces,selected,points,vertex_edges):
    """Orient face strips and slide on existing edges of the source surface.

    Production quads are often slightly warped, and Maya stores float vertex
    positions. Exact planarity tests pin otherwise usable rows at random.
    Allow small departures while retaining sharp corners and warped faces as
    stops. These tolerances admit guides, never move vertices off those edges.
    """
    sides=[]
    for index,edge in enumerate(ordered):
        if len(edge_faces[edge])>2:raise ValueError("Неманифолдные рёбра не поддерживаются.")
        a,b=vertices[index],vertices[(index+1)%len(vertices)];faces={}
        for face in edge_faces[edge]:
            polygon=list(fn.getPolygonVertices(face));offset=polygon.index(a)
            side=0 if polygon[(offset+1)%len(polygon)]==b else 1
            if side in faces:raise ValueError("Смежные поверхности имеют несогласованное направление.")
            faces[side]=face
        sides.append(faces)
    rails={0:{},1:{}}
    normals={f:fn.getPolygonNormal(f,om.MSpace.kWorld) for f in {f for v in vertices for e in vertex_edges[v] for f in edge_faces[e]}}
    planar={}
    for face,normal in normals.items():
        polygon=fn.getPolygonVertices(face);origin=points[polygon[0]]
        span=max((points[v]-origin).length() for v in polygon)
        precision=max(abs(points[v][axis]) for v in polygon for axis in range(3))*2e-7
        planar[face]=normal.length()>.5 and all(abs((points[v]-origin)*normal)<=max(1e-9,precision,span*.005) for v in polygon)
    for index,vertex in enumerate(vertices):
        links=[]
        if index>0 or closed:links.append(sides[(index-1)%len(ordered)])
        if index<len(ordered):links.append(sides[index])
        # Moving a selected boundary would change the silhouette. Boundary
        # endpoints can slide along an unselected straight boundary rail.
        if any(len(edge_faces[e])==1 for e in vertex_edges[vertex] if e in selected):continue
        incident_faces={f for e in vertex_edges[vertex] for f in edge_faces[e]}
        # A strongly warped face or a sharp ridge is a shape constraint.
        if any(not planar[f] for f in incident_faces):continue
        for side in (0,1):
            if any(side not in faces for faces in links):continue
            candidates=None
            for faces in links:
                incident={e for e in _polygon_edges(fn,faces[side]) if vertex in fn.getEdgeVertices(e) and e not in selected}
                candidates=incident if candidates is None else candidates&incident
            if not candidates or len(candidates)!=1:continue
            rail=next(iter(candidates));a,b=fn.getEdgeVertices(rail);target=b if a==vertex else a
            # Another selected strip acts as a stop, rather than a guide.
            if any(e in selected for e in vertex_edges[target]):continue
            vector=points[target]-points[vertex]
            if vector.length()<=1e-12:continue
            direction=vector.normal()
            if any(abs(direction*normals[f])>math.sin(math.radians(5)) for f in incident_faces):continue
            rails[side][vertex]=tuple(points[target])[:3]
    return rails


def analyze(mesh,edges):
    fn=_mesh_fn(mesh)
    if fn.dagPath().isInstanced():raise ValueError("Для сдвига используйте меш без экземпляров (instances).")
    edges=set(edges)
    if not edges or any(e<0 or e>=fn.numEdges for e in edges):raise ValueError("Выделение рёбер устарело.")
    pairs={e:fn.getEdgeVertices(e) for e in edges};incident={}
    for edge,pair in pairs.items():
        for vertex in pair:incident.setdefault(vertex,set()).add(edge)
    if any(len(e)>2 for e in incident.values()):raise ValueError("Выберите цепочки без разветвлений.")
    points=fn.getPoints(om.MSpace.kWorld);edge_faces=_edge_faces(fn);remaining=set(edges);chains=[]
    vertex_edges={}
    for edge in range(fn.numEdges):
        for vertex in fn.getEdgeVertices(edge):vertex_edges.setdefault(vertex,[]).append(edge)
    topology=identity(fn)
    while remaining:
        patch=set();pending=[min(remaining)]
        while pending:
            edge=pending.pop()
            if edge not in remaining:continue
            remaining.remove(edge);patch.add(edge)
            for v in pairs[edge]:pending.extend(incident[v]&remaining)
        ordered,vertices,closed=_ordered_chain(fn,tuple(sorted(patch)))
        distances=[0.];length=0.
        for index,edge in enumerate(ordered):
            a,b=pairs[edge];size=(points[a]-points[b]).length()
            if size<=1e-12:raise ValueError("В цепочке есть ребро нулевой длины.")
            length+=size
            if index+1<len(vertices):distances.append(length)
        pinned=set()
        if not closed:
            for v,neighbor,selected_edge in ((vertices[0],vertices[1],ordered[0]),
                                             (vertices[-1],vertices[-2],ordered[-1])):
                forward=(points[v]-points[neighbor]).normal()
                for other in vertex_edges[v]:
                    if other in edges:continue
                    a,b=fn.getEdgeVertices(other);tip=b if a==v else a
                    vector=points[tip]-points[v]
                    if vector.length()<=1e-12:continue
                    # Opposite edges at a quad vertex share no face. Geometry
                    # handles boundary/n-gon continuations and excludes rails.
                    separate=not set(edge_faces[other])&set(edge_faces[selected_edge])
                    threshold=.5 if separate else math.cos(math.radians(30))
                    if forward*vector.normal()>threshold:pinned.add(v);break
        rails=_slide_rails(fn,ordered,vertices,closed,edge_faces,edges,points,vertex_edges)
        pinned.update(v for v in vertices if v not in rails[0] and v not in rails[1])
        chains.append(Chain(fn.fullPathName(),ordered,vertices,closed,
                            {v:tuple(points[v])[:3] for v in vertices},tuple(distances),length,
                            frozenset(pinned),topology,rails))
    return chains


class Gesture:
    def __init__(self,chain,edge,fraction,*,grip=None,pivot=None,plane=None,axis=None,helper_axis=None,movement_axis=None):
        # A point on the helper plane may project to a pinned endpoint of the
        # mesh. Its influence comes from the plane point, not that endpoint.
        self.chain=chain;self.weights=chain.weights(edge,max(1e-6,min(1.-1e-6,fraction)) if grip is not None else fraction)
        pins=chain.pinned
        if movement_axis is not None:
            pins=pins&{chain.vertices[0],chain.vertices[-1]} if not chain.closed else frozenset()
        self.original={v:chain.points[v] for v in chain.vertices if v not in pins}
        self.current=dict(self.original)
        self.movement_axis=om.MVector(movement_axis).normal() if movement_axis is not None else None
        index=chain.edges.index(edge)
        pair=(chain.vertices[index],chain.vertices[(index+1)%len(chain.vertices)])
        self.guides={}
        for side,rails in chain.rails.items():
            available={v:om.MPoint(end)-om.MPoint(chain.points[v]) for v,end in rails.items() if v in self.original}
            if not available:continue
            vectors=[(available[v],w) for v,w in zip(pair,(1-fraction,fraction)) if v in available and w>1e-12]
            if vectors:
                total=sum(w for _,w in vectors);guide=sum((vector*w for vector,w in vectors),om.MVector())/total
            else:guide=available[max(available,key=lambda v:self.weights[v])]
            if guide.length()>1e-12:self.guides[side]=guide
        if self.movement_axis is not None and self.original:
            self.guides={0:self.movement_axis*chain.length,1:-self.movement_axis*chain.length}
            helper_axis=self.movement_axis
        if not self.guides:raise ValueError("На этом участке нет направляющих для скольжения без изменения формы меша.")
        self.helper_axis=om.MVector(helper_axis).normal() if helper_axis is not None else max(self.guides.values(),key=lambda vector:vector.length()).normal()
        # A nearly straight open chain has no enclosed section to disambiguate
        # its axes. Keep its ordinary slide; thin closed contours can use the
        # secondary covariance axis instead of their thickness axis.
        self.plane=plane if plane is not None else fit_plane(list(chain.points.values()),tuple(self.guides.values()),allow_secondary=chain.closed)
        self.current_plane=None
        if axis is not None and self.plane is not None:
            if abs(self.helper_axis*self.plane[1])<1e-4:
                raise ValueError('Выбранная плоскость параллельна исходным направляющим. Выберите другую ось.')
            axis=om.MVector(axis).normal()
            self.guides={side:axis*(guide.length() if guide*axis>=0 else -guide.length()) for side,guide in self.guides.items()}
        # If guides lie in the section plane, its intersection is undefined.
        # Straight/in-plane chains retain ordinary linear sliding.
        if self.plane is not None and max(abs(guide.normal()*self.plane[1]) for guide in self.guides.values())>1e-4:
            self.center,self.normal=self.plane
            point=om.MPoint(grip) if grip is not None else om.MPoint(chain.points[pair[0]])+(om.MPoint(chain.points[pair[1]])-om.MPoint(chain.points[pair[0]]))*fraction
            span=om.MVector(point)-om.MVector(pivot) if pivot is not None else None
            if self.movement_axis is not None and span is not None and (span-self.normal*(span*self.normal)).length()<chain.length*1e-6:
                self.gradient,self.offset=om.MVector(),1.
            else:self.gradient,self.offset=plane_field(replace(chain,pinned=frozenset(pins)) if self.movement_axis is not None else chain,tuple(point)[:3],self.center,self.normal,pivot=pivot)
            if self.movement_axis is not None:
                self.gradient-=self.normal*((self.gradient*self.movement_axis)/(self.normal*self.movement_axis))
            self.weights={v:self.gradient*(om.MVector(p)-self.center)+self.offset for v,p in chain.points.items()}
            self.residuals={v:self.normal*(om.MVector(p)-self.center) for v,p in chain.points.items()}
        else:self.plane=None

    def _check(self):
        if identity(_mesh_fn(self.chain.mesh))!=self.chain.topology:
            raise ValueError("Сетка изменилась. Заново выделите компоненты.")

    def _write(self,positions):
        for vertex,point in positions.items():
            cmds.xform(self.chain.mesh+f".vtx[{vertex}]",worldSpace=True,translation=point)

    def update(self,delta):
        self._check()
        if len(delta)!=3 or not all(math.isfinite(value) for value in delta):raise ValueError("Недопустимое смещение.")
        vector=om.MVector(delta)
        amount,side=max((max(0.,vector*guide.normal()),side) for side,guide in self.guides.items())
        self.slide(amount,side)

    def slide(self,amount,side):
        self._check()
        if not math.isfinite(amount) or amount<0 or side not in self.guides:raise ValueError("Недопустимое смещение.")
        if self.plane is not None:
            positions=self._plane_positions(amount,side)
            if positions is None:
                low,high=0.,amount
                for _ in range(45):
                    middle=(low+high)*.5
                    if self._plane_positions(middle,side) is None:high=middle
                    else:low=middle
                positions=self._plane_positions(low,side)
        else:
            positions=dict(self.original)
            for vertex,point in self.original.items():
                target=self.chain.rails[side].get(vertex)
                if target is None:continue
                vector=om.MPoint(target)-om.MPoint(point);length=vector.length()
                distance=min(amount*self.weights[vertex],length*(1.-1e-5))
                positions[vertex]=tuple(om.MPoint(point)+vector*(distance/length))[:3]
        try:
            with without_undo():self._write(positions)
        except RuntimeError:
            self.cancel();raise
        self.current=positions

    def _plane_positions(self,amount,side):
        if amount==0:
            self.current_plane=(self.normal,0.)
            return dict(self.original)
        direction=self.guides[side].normal();denominator=1.+amount*(self.gradient*direction)
        if denominator<=1e-6:return None
        tilt=amount*(self.normal*direction)/denominator
        normal=self.normal-self.gradient*tilt;offset=tilt*self.offset
        source_crossing=self.normal*self.helper_axis
        if abs(source_crossing)<1e-8 or (normal*self.helper_axis)/source_crossing<1e-5:return None
        if self.movement_axis is not None:
            if (normal*self.movement_axis)/(self.normal*self.movement_axis)<.25:return None
            self.current_plane=(normal,offset)
            return {v:tuple(om.MVector(p)+self.movement_axis*(tilt*self.weights[v]/(normal*self.movement_axis))) for v,p in self.original.items()}
        positions=dict(self.original)
        for vertex,point in self.original.items():
            need=tilt*self.weights[vertex]
            if abs(need)<=1e-12:continue
            options=[]
            for rails in self.chain.rails.values():
                target=rails.get(vertex)
                if target is None:continue
                vector=om.MPoint(target)-om.MPoint(point);projection=normal*vector
                if abs(projection)<1e-12:continue
                fraction=need/projection
                if -1e-10<=fraction<=1.-1e-5:
                    options.append((vector.length()*max(0.,fraction),tuple(om.MPoint(point)+vector*max(0.,fraction))[:3]))
            if not options:return None
            positions[vertex]=min(options)[1]
        self.current_plane=(normal,offset)
        return positions

    def cancel(self,*,after_history=False):
        self._check()
        positions=self.original
        if after_history:
            points=_mesh_fn(self.chain.mesh).getPoints(om.MSpace.kWorld)
            tolerance=max(1e-8,self.chain.length*1e-6)
            positions={v:p for v,p in positions.items() if math.dist(tuple(points[v])[:3],self.current[v])<tolerance}
        with without_undo():self._write(positions)
        self.current=dict(self.original)

    def commit(self):
        self._check();result=dict(self.current)
        self.cancel()
        if all(math.dist(result[v],point)<1e-12 for v,point in self.original.items()):return False
        cmds.undoInfo(openChunk=True,chunkName="Plane Deform")
        try:self._write(result)
        except RuntimeError:
            cmds.undoInfo(closeChunk=True)
            cmds.undo()
            raise
        else:cmds.undoInfo(closeChunk=True)
        self.current=result
        return True
