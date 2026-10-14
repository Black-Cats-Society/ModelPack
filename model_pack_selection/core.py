"""Read-only topology operations for Model Pack Selection."""
from collections import defaultdict,deque
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn


class Mesh:
    def __init__(self,mesh):
        self.fn=_mesh_fn(mesh)
        self.name=self.fn.fullPathName()
        self.points=self.fn.getPoints(om.MSpace.kWorld)
        self.faces=[list(self.fn.getPolygonVertices(f)) for f in range(self.fn.numPolygons)]
        self.edges=[None]*len(self.faces)
        self.edge_faces=defaultdict(set)
        iterator=om.MItMeshPolygon(self.fn.dagPath())
        while not iterator.isDone():
            face=iterator.index();self.edges[face]=list(iterator.getEdges())
            for edge in self.edges[face]:self.edge_faces[edge].add(face)
            iterator.next()
        self.neighbors={f:set().union(*(self.edge_faces[e] for e in edges))-{f}
                        for f,edges in enumerate(self.edges)}
        self.identity=(om.MFnDependencyNode(self.fn.object()).uuid().asString(),
                       tuple(self.fn.getVertices()[0]),tuple(self.fn.getVertices()[1]))

    def checked(self,faces):
        faces=set(faces)
        if not faces or not faces<=set(range(len(self.faces))):
            raise ValueError("Выберите существующие полигоны.")
        return faces

    def across(self,face,edge):
        # Do not cross a nonmanifold edge into an arbitrary branch.
        adjacent=self.edge_faces[edge]
        return next(iter(adjacent-{face})) if len(adjacent)==2 else None


def components(faces,neighbors):
    remaining=set(faces)
    while remaining:
        first=min(remaining);stack=[first];patch=set()
        while stack:
            face=stack.pop()
            if face not in remaining:continue
            remaining.remove(face);patch.add(face)
            stack.extend(neighbors[face]&remaining)
        yield patch


def region(data,barrier,seed,keep_boundary=False):
    """Fill the connected side of a polygon barrier containing the seed."""
    if seed not in range(len(data.faces)):
        raise ValueError("Полигон больше не существует.")
    barrier=set(barrier)
    if not barrier<=set(range(len(data.faces))):
        raise ValueError("Контур изменился. Снова включите выделение области.")
    if seed in barrier:raise ValueError("Выберите полигон внутри области, вне выделенного контура.")
    shell=next(p for p in components(range(len(data.faces)),data.neighbors) if seed in p)
    available=shell-barrier
    patch=next(p for p in components(available,data.neighbors) if seed in p)
    if patch==available:
        # An open contour does not enclose a side. Select the whole target
        # mesh, as with an empty boundary, including its disconnected shells.
        return set(range(len(data.faces)))
    return patch|barrier if keep_boundary else patch


def chart(data,patch):
    """Propagate quad edge directions; stop at poles and inconsistent joins."""
    root=min(patch)
    if any(len(data.edges[f])!=4 for f in patch):
        raise ValueError("Выбор направления требует полосы четырёхугольников. Используйте обычный Grow/Shrink на треугольниках и n-гонах.")
    rotations={root:0};queue=deque([root])
    while queue:
        face=queue.popleft()
        for i,edge in enumerate(data.edges[face]):
            other=data.across(face,edge)
            if other not in patch:continue
            j=data.edges[other].index(edge)
            rotation=(i+rotations[face]+2-j)%4
            if other in rotations:
                if rotations[other]!=rotation:
                    raise ValueError("Направления полосы неоднозначны на этом стыке. Выберите отдельную полосу.")
            else:rotations[other]=rotation;queue.append(other)
    return rotations


def axis_edges(data,face,rotation,axis):
    return [edge for i,edge in enumerate(data.edges[face]) if (i+rotation)%2==axis]


def directional_neighbors(data,patch,rotations,axis):
    return {f:{n for e in axis_edges(data,f,rotations[f],axis)
               if (n:=data.across(f,e)) is not None and n in patch} for f in patch}


def wide_axis(data,patch,rotations):
    scores=[]
    for axis in (0,1):
        graph=directional_neighbors(data,patch,rotations,axis)
        count=max(map(len,components(patch,graph)))
        # Travel across the short edge pair follows the long dimension of
        # each face. World-space lengths also handle nonuniform transforms.
        length=sum(sum((data.points[a]-data.points[b]).length()
                       for e in axis_edges(data,f,rotations[f],1-axis)
                       for a,b in [data.fn.getEdgeVertices(e)])*.5 for f in patch)/len(patch)
        scores.append((count,length))
    return int(scores[1]>scores[0])


def grow(data,faces,*,narrow=False,shrink=False,directions=None):
    """One layer; optional local edge axes preserve direction between clicks."""
    faces=data.checked(faces);result=set(faces);next_directions={}
    for patch in components(faces,data.neighbors):
        rotations=chart(data,patch)
        inherited={directions[f]^(rotations[f]%2) for f in patch if directions is not None and f in directions}
        if len(inherited)>1:
            raise ValueError("Полосы с разными направлениями соединились. Заново выделите нужную полосу.")
        axis=next(iter(inherited)) if inherited else wide_axis(data,patch,rotations)^bool(narrow)
        for face in patch:
            local_axis=axis^(rotations[face]%2)
            if face in next_directions and next_directions[face]!=local_axis:
                raise ValueError("Направления полосы неоднозначны на этом стыке. Выберите отдельную полосу.")
            next_directions[face]=local_axis
            edges=axis_edges(data,face,rotations[face],axis)
            others=[data.across(face,e) for e in edges]
            if shrink:
                if any(n is None or n not in faces for n in others):result.discard(face)
            else:
                for edge,other in zip(edges,others):
                    if other is None:continue
                    result.add(other)
                    local_axis=data.edges[other].index(edge)%2
                    if other in next_directions and next_directions[other]!=local_axis:
                        raise ValueError("Направления полосы неоднозначны на этом стыке. Выберите отдельную полосу.")
                    next_directions[other]=local_axis
    if directions is not None:
        directions.clear();directions.update(next_directions)
    return result


def _trace(data,start,edge,limit=None):
    """Walk a face loop through opposite edges, ending at a cap or pole."""
    path=[start];face=start;seen={start}
    while True:
        other=data.across(face,edge)
        if other is None or other in seen or len(data.edges[other])!=4 or (limit is not None and other not in limit):break
        path.append(other);seen.add(other)
        edge=data.edges[other][(data.edges[other].index(edge)+2)%4];face=other
    return path


def _loop_axis(data,seed,rotation):
    scores=[]
    for axis in (0,1):
        edges=axis_edges(data,seed,rotation,axis)
        first,second=(_trace(data,seed,e) for e in edges)
        closed=len(first)>1 and first[-1] in second[1:]
        scores.append((closed,len(set(first)|set(second))))
    if scores[0]==scores[1]:return wide_axis(data,{seed},{seed:rotation})
    return int(scores[1]>scores[0])


def periodic(data,faces,*,across=False,skip=1,extend=None):
    """Select one polygon, skip N; a single seed follows its complete loop."""
    faces=data.checked(faces)
    if not isinstance(skip,int) or not 0<=skip<=1000:raise ValueError("Шаг должен быть целым числом от 0 до 1000.")
    period=skip+1;result=set()
    for patch in components(faces,data.neighbors):
        rotations=chart(data,patch)
        axis=(_loop_axis(data,min(patch),rotations[min(patch)]) if len(patch)==1
              else wide_axis(data,patch,rotations))^bool(across)
        if len(patch)==1 or extend:
            for seed in sorted(patch):
                edges=axis_edges(data,seed,rotations[seed],axis)
                first=_trace(data,seed,edges[0]);second=_trace(data,seed,edges[1])
                # Closed rings are traversed once, with the seed as phase 0.
                path=first if len(first)>1 and first[-1] in second[1:] else list(reversed(second[1:]))+first
                anchor=path.index(seed)
                result.update(f for i,f in enumerate(path) if (i-anchor)%period==0)
            continue
        graph=directional_neighbors(data,patch,rotations,axis)
        lines=list(components(patch,graph));root=min(patch)
        # Cross-direction moves preserve phase between neighboring rows.
        # Assign anchors along a cross-section before traversing each line.
        anchors={root};queue=deque([root])
        while queue:
            face=queue.popleft()
            for edge in axis_edges(data,face,rotations[face],1-axis):
                other=data.across(face,edge)
                if other in patch and other not in anchors:anchors.add(other);queue.append(other)
        for line in lines:
            seed=min(line&anchors or line)
            edge=axis_edges(data,seed,rotations[seed],axis)[0]
            first=_trace(data,seed,edge,line)
            second=_trace(data,seed,axis_edges(data,seed,rotations[seed],axis)[1],line)
            path=first if len(first)>1 and first[-1] in second[1:] else list(reversed(second[1:]))+first
            anchor=path.index(seed)
            result.update(f for i,f in enumerate(path) if (i-anchor)%period==0)
    return result
