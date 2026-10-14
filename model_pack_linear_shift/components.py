"""Projected falloff for face and vertex arrays, including stacked bevel rows."""
from dataclasses import dataclass
import heapq
import math
from maya.api import OpenMaya as om
from model_pack_bevel.session import without_undo
from . import core


@dataclass
class Patch:
    mesh: str
    kind: str
    components: tuple
    vertices: tuple
    points: dict
    topology: tuple
    plane: tuple
    edges: tuple
    segments: tuple
    length: float
    limits: dict
    pinned: frozenset=frozenset()
    closed: bool=False
    rails: object=None
    external: object=None
    support: object=None


def stops(external,normal):
    return {v:[(1 if d*normal>0 else -1,abs(d*normal)) for d in vectors
        if d.length()>1e-12 and abs(d.normal()*normal)>.999] for v,vectors in external.items()}


def guide_axis(patch,normal=None):
    """Use the coherent source rails, independent of the helper's next tilt."""
    normal=patch.plane[1] if normal is None else normal;groups=[]
    for vertex,vectors in patch.external.items():
        for vector in vectors:
            length=vector.length()
            if length<1e-12:continue
            axis=vector/length
            group=next((g for g in groups if abs(g['axis']*axis)>.999),None)
            if group is None:
                group=dict(axis=axis,total=om.MVector(),length=0.,vertices=set());groups.append(group)
            group['total']+=vector if group['axis']*axis>0 else -vector
            group['length']+=length;group['vertices'].add(vertex)
    if not groups:return normal
    group=max(groups,key=lambda g:(len(g['vertices']),g['length']))
    # A lone edge is insufficient evidence for a whole array's direction.
    if len(group['vertices'])<2:return normal
    axis=group['total'].normal()
    orientation=axis*normal
    if abs(orientation)<.25:orientation=max(tuple(axis),key=abs)
    return axis if orientation>0 else -axis


def guide_directions(patch,axis):
    """Carry each boundary's actual rail through the selected bevel layers."""
    axis=om.MVector(axis).normal();directions={};queue=[];distances={};adjacent={v:[] for v in patch.vertices}
    for a,b in patch.segments:
        vector=om.MVector(patch.points[b])-om.MVector(patch.points[a])
        cost=(vector-axis*(vector*axis)).length()+vector.length()*1e-5
        adjacent[a].append((b,cost));adjacent[b].append((a,cost))
    for vertex,vectors in patch.external.items():
        candidates=[d.normal() for d in vectors if d.length()>1e-12 and abs(d.normal()*axis)>1e-4]
        if not vectors:continue
        if not candidates:candidates=[om.MVector(axis)]
        normals=(patch.support or {}).get(vertex,())
        compatible=[d for d in candidates if all(abs(d*n)<1e-4 for n in normals)]
        direction=max(compatible or candidates,key=lambda d:abs(d*axis))
        if normals and not compatible:
            # A boundary vertex can touch several source surfaces. Following
            # just one of its edges would bend the others. Their common
            # tangent is the only direction that preserves all those planes.
            basis=[]
            for n in normals:
                residual=om.MVector(n)
                for b in basis:residual-=b*(residual*b)
                if residual.length()>1e-4:basis.append(residual.normal())
            for b in basis:direction-=b*(direction*b)
            if direction.length()>1e-8:direction=direction.normal()
            else:direction=om.MVector()
        if direction*axis<0:direction=-direction
        directions[vertex]=direction;distances[vertex]=0.;heapq.heappush(queue,(0.,vertex,vertex))
    roots=dict(directions)
    while queue:
        distance,vertex,root=heapq.heappop(queue)
        if distance!=distances[vertex]:continue
        for neighbor,cost in adjacent[vertex]:
            proposed=distance+cost
            if proposed>=distances.get(neighbor,float('inf')):continue
            distances[neighbor]=proposed;directions[neighbor]=roots[root]
            heapq.heappush(queue,(proposed,neighbor,root))
    return {v:directions.get(v,axis) for v in patch.vertices}


def analyze(mesh,ids,kind):
    fn=core._mesh_fn(mesh)
    if fn.dagPath().isInstanced():raise ValueError('Для сдвига используйте меш без экземпляров (instances).')
    ids=tuple(sorted(set(ids)));maximum=fn.numPolygons if kind=='face' else fn.numVertices
    if not ids or any(i<0 or i>=maximum for i in ids):raise ValueError('Выделение компонентов устарело.')
    vertices=sorted({v for f in ids for v in fn.getPolygonVertices(f)}) if kind=='face' else list(ids)
    points=fn.getPoints(om.MSpace.kWorld);selected=set(vertices)
    positions={v:tuple(points[v])[:3] for v in vertices}
    plane=core.fit_plane(list(positions.values()))
    edges=[];segments=[];incident=set();external={v:[] for v in vertices}
    edge_faces=core._edge_faces(fn)
    for edge in range(fn.numEdges):
        a,b=fn.getEdgeVertices(edge)
        if a in selected or b in selected:incident.update(edge_faces[edge])
        if a in selected and b in selected:edges.append(edge);segments.append((a,b))
        elif a in selected:external[a].append(points[b]-points[a])
        elif b in selected:external[b].append(points[a]-points[b])
    if plane is None:
        # Small/collinear selections use the largest incident polygon's plane.
        if not incident:raise ValueError('Не удалось определить плоскость выбранных компонентов.')
        iterator=om.MItMeshPolygon(fn.dagPath())
        def area(face):
            iterator.setIndex(face)
            return iterator.getArea(om.MSpace.kWorld)
        face=max(incident,key=area)
        normal=fn.getPolygonNormal(face,om.MSpace.kWorld).normal()
        if normal.length()<.5:raise ValueError('Не удалось определить плоскость выбранных компонентов.')
        center=sum((om.MVector(p) for p in positions.values()),om.MVector())/len(positions)
        plane=center,normal
    length=max((om.MVector(p)-om.MVector(q)).length() for p in positions.values() for q in (positions[vertices[0]],))
    if length<1e-10:length=max((d.length() for vectors in external.values() for d in vectors),default=1.)
    normal=plane[1]
    # A boundary row stops before its next unselected vertex. Interior layers
    # share that stop through the common drag amount; they do not move or stop
    # independently. Outward directions without a guide remain free.
    limits=stops(external,normal)
    support={v:[] for v in vertices}
    for face in incident:
        poly=fn.getPolygonVertices(face)
        if set(poly)<=selected:continue
        n=fn.getPolygonNormal(face,om.MSpace.kWorld).normal();origin=points[poly[0]]
        span=max((points[v]-origin).length() for v in poly)
        if max(abs(n*(points[v]-origin)) for v in poly)>max(1e-8,span*1e-5):continue
        for v in poly:
            if v in selected:support[v].append(n)
    patch=Patch(fn.fullPathName(),kind,ids,tuple(vertices),positions,core.identity(fn),plane,
        tuple(edges),tuple(segments),length,limits,rails={},external=external,support=support)
    # A broad array's smallest PCA axis often measures the body's thickness.
    # The boundary continuations identify the section direction even when
    # the array includes many rows along that direction.
    source=guide_axis(patch)
    if abs(source*normal)<.25:patch.plane=(plane[0],source)
    return patch


class Gesture(core.Gesture):
    def __init__(self,patch,grip,pivot,*,plane=None,axis=None,directions=None,field_axis=None):
        self.chain=patch;self.original=dict(patch.points);self.current=dict(self.original)
        self.plane=plane if plane is not None else patch.plane
        self.center,self.normal=self.plane;self.normal=self.normal.normal()
        self.current_plane=None
        axis=om.MVector(axis) if axis is not None else guide_axis(patch)
        axis=axis.normal()
        field_axis=om.MVector(field_axis) if field_axis is not None else guide_axis(patch)
        field_axis=field_axis.normal();field_crossing=field_axis*self.normal
        self.directions=directions if directions is not None else guide_directions(patch,field_axis)
        # A world frame can lie along some source rails. Intersecting those
        # rails with it either divides by zero or magnifies tiny gestures.
        # Keep the visible frame, but compute its shear in the source section.
        # The drag and all frame points still follow the original slide axis.
        self.source_section=(abs(field_crossing)<.25 or any(
            abs(d*self.normal)<.05*abs(d*field_axis) for d in self.directions.values()))
        if self.source_section:
            self.normal=om.MVector(field_axis);axis=om.MVector(field_axis);field_crossing=1.
            grip=om.MVector(grip);pivot=om.MVector(pivot) if pivot is not None else None
            if pivot is not None:
                span=grip-pivot;span-=field_axis*(span*field_axis)
                if span.length()<patch.length*.05:pivot=None
        self.plane=self.center,self.normal
        crossing=axis*self.normal
        if abs(crossing)<1e-4:raise ValueError('Плоскость параллельна направляющим. Заново включите Plane Deform.')
        self.helper_axis=field_axis
        offsets=[om.MVector(p)-self.center for p in patch.points.values()]
        projected=[p-field_axis*((p*self.normal)/field_crossing) for p in offsets]
        span=om.MVector(grip)-om.MVector(pivot) if pivot is not None else None
        uniform=span is not None and (span-field_axis*((span*self.normal)/field_crossing)).length()<patch.length*1e-6
        if uniform or (self.source_section and pivot is None) or max(p.length() for p in projected)<=patch.length*1e-8:
            # A single point or a stack normal to the plane has no span for
            # falloff. Move the entire stack by the requested distance.
            self.gradient,self.offset=om.MVector(),1.
        else:
            grip=om.MVector(grip)
            grip-=field_axis*(((grip-self.center)*self.normal)/field_crossing)
            self.gradient,self.offset=core.plane_field(patch,grip,self.center,self.normal,pivot=pivot)
            # Project along the source rails, not perpendicular to the tilted
            # helper. All layers on the same rail retain the same influence.
            self.gradient-=self.normal*((self.gradient*field_axis)/field_crossing)
        self.weights={v:self.gradient*(om.MVector(p)-self.center)+self.offset for v,p in patch.points.items()}
        if self.source_section:
            maximum=max(1.,*(abs(weight) for weight in self.weights.values()))
            self.gradient/=maximum;self.offset/=maximum
            self.weights={v:weight/maximum for v,weight in self.weights.items()}
        self.guides={0:axis*patch.length,1:-axis*patch.length}
        self.limits={v:stops({v:patch.external[v]},self.directions[v])[v] for v in patch.vertices}

    def moved(self,point):
        if self.current_plane is None:return point
        normal,offset=self.current_plane;point=om.MVector(point)
        distance=(offset-(normal-self.normal)*(point-self.center))/(normal*self.helper_axis)
        return point+self.helper_axis*distance

    def _positions(self,amount,side):
        if amount==0:
            self.current_plane=(self.normal,0.)
            return dict(self.original)
        direction=self.guides[side].normal();crossing=direction*self.normal
        scale=1.+amount*(self.gradient*direction)
        if scale<=1e-6:return None
        tilt=amount*crossing/scale
        normal=self.normal-self.gradient*tilt;offset=tilt*self.offset
        if (normal*self.helper_axis)/(self.normal*self.helper_axis)<.25:return None
        positions={}
        for v,p in self.original.items():
            rail=self.directions[v];initial=self.normal*rail;denominator=normal*rail
            if abs(initial)<1e-8:
                if abs(self.weights[v])>1e-8:return None
                positions[v]=p;continue
            if denominator/initial<.25:return None
            distance=tilt*self.weights[v]/denominator
            if abs(distance)>self.travel_limit:return None
            wanted=1 if distance>0 else -1
            if any(stop==wanted and abs(distance)>limit*(1.-1e-5) for stop,limit in self.limits[v]):return None
            positions[v]=tuple(om.MVector(p)+rail*distance)
        self.current_plane=(normal,offset)
        return positions

    def slide(self,amount,side):
        self._check()
        if not math.isfinite(amount) or amount<0 or side not in self.guides:raise ValueError('Недопустимое смещение.')
        # A near-tangent rail must never turn a small mouse drag into a huge
        # jump. Reduce the common tilt, preserving the section and its layers.
        self.travel_limit=amount*4.
        positions=self._positions(amount,side)
        if positions is None:
            low,high=0.,amount
            for _ in range(45):
                middle=(low+high)*.5
                if self._positions(middle,side) is None:high=middle
                else:low=middle
            positions=self._positions(low,side)
            if positions is None:raise ValueError('Нет безопасного движения по исходным направляющим.')
        try:
            with without_undo():self._write(positions)
        except RuntimeError:
            self.cancel();raise
        self.current=positions
