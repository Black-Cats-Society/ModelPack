"""Find a complete longitudinal tube rail, preferring cuts in the active UV set."""
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn


def longitudinal_samples(rings, segments, closed=False):
    """Keep source stations; split bands or remove least important profiles."""
    import heapq,math
    count=len(rings);bands=count if closed else count-1
    segments=max(3 if closed else 1,min(1024,int(segments)))
    centers=[tuple(sum(p[k] for p in ring)/len(ring) for k in range(3)) for ring in rings]
    distances=[math.dist(centers[j],centers[(j+1)%count]) for j in range(bands)]
    # Degenerate centreline bands can still change radius or orientation.
    distances=[max(d,max(math.dist(a,b) for a,b in zip(rings[j],rings[(j+1)%count]))*1e-6,1e-12)
               for j,d in enumerate(distances)]
    if segments>=bands:
        divisions=[1]*bands;queue=[(-d,j) for j,d in enumerate(distances)];heapq.heapify(queue)
        for _ in range(segments-bands):
            _,j=heapq.heappop(queue);divisions[j]+=1
            heapq.heappush(queue,(-distances[j]/divisions[j],j))
        result=[j+i/divisions[j] for j in range(bands) for i in range(divisions[j])]
        return result if closed else result+[float(bands)]
    knots=[0.]
    for d in distances:knots.append(knots[-1]+d)
    previous=[(j-1)%count for j in range(count)];following=[(j+1)%count for j in range(count)]
    alive=set(range(count));versions=[0]*count;queue=[]
    def score(j):
        a,b=previous[j],following[j]
        end=b if b>a else b+count
        total=(knots[b]-knots[a]) if b>a else (knots[-1]-knots[a]+knots[b])
        error=0.
        for index in range(a+1,end):
            v=index%count;distance=knots[v]-knots[a]+(knots[-1] if index>=count else 0.)
            t=distance/max(total,1e-12)
            for p,left,right in zip(rings[v],rings[a],rings[b]):
                error=max(error,sum((p[k]-((1-t)*left[k]+t*right[k]))**2 for k in range(3)))
        return error
    def schedule(j):
        if j not in alive or j==0 or (not closed and j==count-1):return
        versions[j]+=1;heapq.heappush(queue,(score(j),j,versions[j]))
    for j in range(count):schedule(j)
    wanted=segments if closed else segments+1
    while len(alive)>wanted:
        _,j,version=heapq.heappop(queue)
        if j not in alive or version!=versions[j]:continue
        a,b=previous[j],following[j];alive.remove(j);following[a]=b;previous[b]=a
        schedule(a);schedule(b)
    return [float(j) for j in sorted(alive)]


def source_sections(node):
    """Return the original longitudinal band count and closure from live input."""
    import json
    from tube_resection import _MeshData,_order_seam,_extract_rings
    selection=om.MSelectionList();selection.add(node)
    fn=om.MFnDependencyNode(selection.getDependNode(0))
    md=_MeshData(fn.findPlug('inputMesh',False).asMObject())
    rail,closed=_order_seam(md,json.loads(fn.findPlug('seamEdges',False).asString()))
    return len(_extract_rings(md,rail,closed)[1]),closed


def angle_samples(rings, threshold, closed=False, angle_tolerance=.1):
    """Remove low mean-dihedral loops, preferring the shortest affected span."""
    import heapq,math
    count=len(rings);sides=len(rings[0]);threshold=max(0.,min(180.,float(threshold)))
    previous=[(i-1)%count for i in range(count)];following=[(i+1)%count for i in range(count)]
    alive=set(range(count));versions=[0]*count;queue=[]
    def normal(a,b,c,d):
        origin=om.MVector(a);points=tuple(om.MVector(p)-origin for p in (a,b,c,d))
        result=om.MVector()
        for p,q in zip(points,points[1:]+points[:1]):result+=p^q
        return result.normal() if result.length()>1e-15 else None
    def measure(j):
        a,b=previous[j],following[j];angles=[];length=0.
        for i in range(sides):
            k=(i+1)%sides
            left=normal(rings[a][i],rings[a][k],rings[j][k],rings[j][i])
            right=normal(rings[j][i],rings[j][k],rings[b][k],rings[b][i])
            if left is None or right is None:return 180.,float('inf')
            angles.append(math.degrees(math.acos(max(-1.,min(1.,left*right)))))
            length+=math.dist(rings[a][i],rings[j][i])+math.dist(rings[j][i],rings[b][i])
        return sum(angles)/sides,length/sides
    def schedule(j):
        # End profiles and the closed tube's seam anchor are stable.
        if j not in alive or j==0 or (not closed and j==count-1):return
        versions[j]+=1;angle,length=measure(j)
        if not math.isfinite(length):return
        heapq.heappush(queue,(angle,length,j,versions[j]))
    def valid(item):return item[2] in alive and item[3]==versions[item[2]]
    for j in range(count):schedule(j)
    minimum=3 if closed else 2
    while len(alive)>minimum:
        while queue and not valid(queue[0]):heapq.heappop(queue)
        if not queue or queue[0][0]>threshold+1e-5:break
        first=heapq.heappop(queue);candidates=[first]
        limit=min(threshold+1e-5,1e-5 if first[0]<=1e-5 else first[0]+angle_tolerance)
        while queue and queue[0][0]<=limit:
            item=heapq.heappop(queue)
            if valid(item):candidates.append(item)
        chosen=min(candidates,key=lambda item:(item[1],item[0],item[2]))
        for item in candidates:
            if item is not chosen:heapq.heappush(queue,item)
        _,_,j,_=chosen;a,b=previous[j],following[j]
        alive.remove(j);following[a]=b;previous[b]=a
        schedule(a);schedule(b)
    return [float(j) for j in sorted(alive)]


def add_samples(rings, stations, extra, closed=False):
    """Subdivide retained bands while keeping the existing source stations."""
    import heapq,math
    extra=max(0,min(1024,int(extra)));ends=stations[1:]+([float(len(rings))] if closed else [])
    bands=list(zip(stations,ends));divisions=[1]*len(bands)
    lengths=[sum(math.dist(p,q) for p,q in zip(rings[int(a)],rings[int(b)%len(rings)]))/len(rings[0]) for a,b in bands]
    queue=[(-length,i) for i,length in enumerate(lengths)];heapq.heapify(queue)
    for _ in range(extra):
        _,i=heapq.heappop(queue);divisions[i]+=1
        heapq.heappush(queue,(-lengths[i]/divisions[i],i))
    result=[a+(b-a)*j/divisions[i] for i,(a,b) in enumerate(bands) for j in range(divisions[i])]
    return result if closed else result+[stations[-1]]


def _checked_rings(md, edges):
    from tube_resection import _order_seam, _extract_rings
    rail, closed = _order_seam(md, edges)
    rings, bands, sides = _extract_rings(md, rail, closed)
    vertices = [v for ring in rings for v in ring]
    faces = [f for _, _, band in bands for f in band]
    if sides < 3 or len(vertices) != len(set(vertices)) or len(faces) != len(set(faces)):
        raise ValueError('Шов не задаёт однозначные кольца трубки.')
    # Every remaining face must belong to one complete end cap. A partial
    # rail or a different shell must never silently disappear during Reset.
    remaining = set(range(len(md.faceVerts))) - set(faces)
    ring_vertices = set(vertices)
    ends = [set(rings[0]), set(rings[-1])] if not closed else []
    while remaining:
        seed = remaining.pop(); stack = [seed]; cap = set()
        while stack:
            face = stack.pop(); fv = md.faceVerts[face]; cap.update(fv)
            for a, b in zip(fv, fv[1:] + fv[:1]):
                adjacent = md.edge_faces(a, b) & remaining
                remaining.difference_update(adjacent); stack.extend(adjacent)
        touch = cap & ring_vertices
        if touch not in ends:
            raise ValueError('Найденный шов не охватывает весь меш. Выберите одну трубку без ответвлений.')
        from tube_resection import _newell, _centroid, _normalize, _dot, _sub, _dist
        boundary = next(r for r in (rings[0], rings[-1]) if set(r) == touch)
        points = [md.P[v] for v in boundary]
        normal = _normalize(_newell(points)); centre = _centroid(points)
        tolerance = max((_dist(p, centre) for p in points), default=1.) * 1e-4 + 1e-7
        if any(abs(_dot(_sub(md.P[v], centre), normal)) > tolerance for v in cap):
            raise ValueError('Шов не доходит до конца трубки, либо торец не плоский. Выберите полный продольный шов.')
        ends.remove(touch)
    if len(md.vertNbrs) != md.fn.numVertices:
        raise ValueError('Меш содержит отдельные вершины.')
    return rings, bands


def automatic_seam(mesh):
    """Return (edge ids, 'uv' or 'topology'); no selection or mesh mutations."""
    from tube_resection import _MeshData
    fn = _mesh_fn(mesh); md = _MeshData(fn.dagPath())
    if any(len(faces) > 2 for faces in md.edgeFaces.values()):
        raise ValueError('Reset Tube: найдены неманифолдные рёбра. Исправьте топологию меша.')
    if not md.vertNbrs: raise ValueError('Reset Tube: меш не содержит рёбер трубки.')
    connected = set(); stack = [next(iter(md.vertNbrs))]
    while stack:
        vertex = stack.pop()
        if vertex in connected: continue
        connected.add(vertex); stack.extend(md.vertNbrs[vertex] - connected)
    if len(connected) != fn.numVertices:
        raise ValueError('Reset Tube: выберите одну связную трубку. В меше есть отдельные оболочки или вершины.')
    pairs = [fn.getEdgeVertices(e) for e in range(fn.numEdges)]
    adjacent = [md.edge_faces(*pair) for pair in pairs]
    eligible = {e for e, faces in enumerate(adjacent)
                if len(faces) == 2 and all(len(md.faceVerts[f]) == 4 for f in faces)}
    incident = {}
    for e in eligible:
        for v in pairs[e]: incident.setdefault(v, []).append(e)

    def walk(start):
        chain = {start}
        for endpoint in pairs[start]:
            edge, vertex = start, endpoint
            while True:
                choices = [e for e in incident[vertex] if e != edge and not adjacent[e] & adjacent[edge]]
                if not choices: break
                if len(choices) != 1: return None, chain
                nxt = choices[0]
                if nxt in chain:
                    if nxt != start: return None, chain
                    return sorted(chain), chain
                chain.add(nxt)
                a, b = pairs[nxt]; vertex = b if a == vertex else a; edge = nxt
        return sorted(chain), chain

    # UV IDs, rather than coordinates, distinguish sewn and split corners,
    # including cuts whose UV coordinates happen to coincide.
    cuts = set()
    try:
        counts, ids = fn.getAssignedUVs(); corners = []; offset = 0
        for face, fv in enumerate(md.faceVerts):
            count = counts[face]
            corners.append(dict(zip(fv, ids[offset:offset + count])) if count == len(fv) else {})
            offset += count
        for e in eligible:
            f, g = adjacent[e]; a, b = pairs[e]
            if a in corners[f] and b in corners[f] and a in corners[g] and b in corners[g]:
                if any(corners[f][v] != corners[g][v] for v in (a, b)): cuts.add(e)
    except RuntimeError:
        pass
    visited = set(); candidates = []
    for edge in sorted(eligible):
        if edge in visited: continue
        chain, seen = walk(edge); visited.update(seen)
        if chain is None: continue
        degrees = {}
        length = 0.
        for e in chain:
            a, b = pairs[e]
            degrees[a] = degrees.get(a, 0) + 1; degrees[b] = degrees.get(b, 0) + 1
            length += (om.MPoint(md.P[a]) - om.MPoint(md.P[b])).length()
        uv = all(e in cuts for e in chain)
        candidates.append((uv, any(d == 1 for d in degrees.values()), length, chain))
    # A complete UV rail takes precedence. Open rails precede transverse loops;
    # for a closed tube the longer loop normally follows the tube's centreline.
    candidates.sort(key=lambda c: (-c[0], -c[1], -c[2], c[3][0]))
    for uv, _, _, chain in candidates:
        try: _checked_rings(md, chain)
        except (ValueError, RuntimeError): continue
        return chain, 'uv' if uv else 'topology'
    raise ValueError('Reset Tube: не удалось найти продольный шов. Нужна цельная трубка из рядов квадов '
                     'с одинаковым числом вершин в кольцах. Выберите шов вручную или исправьте топологию.')
