"""Quad junction grids, with parity cuts propagated through neighboring strips."""
from collections import Counter, defaultdict
import math
import numpy as np
from maya.api import OpenMaya as om
from .geometry import source_anchors


def _sphere(points):
    p=np.array([tuple(v)[:3] for v in points]);origin=p.mean(axis=0);scale=np.linalg.norm(p-origin,axis=1).max()
    if scale<1e-12:return None
    q=(p-origin)/scale
    values,_,rank,_=np.linalg.lstsq(np.column_stack((2*q,np.ones(len(q)))),np.sum(q*q,axis=1),rcond=None)
    if rank<4:return None
    center=values[:3];r2=values[3]+center@center
    if r2<=0:return None
    radius=math.sqrt(r2)
    if np.abs(np.linalg.norm(q-center,axis=1)-radius).max()>radius*.005:return None
    return om.MPoint(*(origin+center*scale)),radius*scale


def _project(point,sphere):
    if sphere is None:return point
    center,radius=sphere;direction=point-center
    return center+direction.normal()*radius if direction.length()>1e-12 else point


def _grid(boundary,points):
    n=len(boundary)
    if n==4:return [],[[0,1,2,3]]
    if n==6:
        center=sum((om.MVector(points[v]) for v in boundary),om.MVector())/n
        return [om.MPoint(center)],[[6,i,(i+1)%6,(i+2)%6] for i in (0,2,4)]
    a=n//4;b=n//2-a
    perimeter=([(i,0) for i in range(a)]+[(a,j) for j in range(b)]+
               [(i,b) for i in range(a,0,-1)]+[(0,j) for j in range(b,0,-1)])
    values=np.zeros((a+1,b+1,3))
    for p,v in zip(perimeter,boundary):values[p]=tuple(points[v])[:3]
    interior=[(i,j) for j in range(1,b) for i in range(1,a)]
    if interior:
        rhs=values[:-2,1:-1]+values[2:,1:-1]+values[1:-1,:-2]+values[1:-1,2:]
        def multiply(x):
            y=4*x.copy();y[1:]-=x[:-1];y[:-1]-=x[1:];y[:,1:]-=x[:,:-1];y[:,:-1]-=x[:,1:];return y
        result=np.zeros_like(rhs);residual=rhs.copy();direction=residual.copy();squared=np.sum(residual*residual,axis=(0,1))
        tolerance=np.maximum(squared*1e-22,1e-30)
        for _ in range(min(1024,len(interior)+8)):
            if np.all(squared<=tolerance):break
            product=multiply(direction);den=np.sum(direction*product,axis=(0,1))
            alpha=np.divide(squared,den,out=np.zeros(3),where=den>1e-30)
            result+=direction*alpha;residual-=product*alpha
            new=np.sum(residual*residual,axis=(0,1));beta=np.divide(new,squared,out=np.zeros(3),where=squared>1e-30)
            direction=residual+direction*beta;squared=new
        values[1:-1,1:-1]=result
    keys=perimeter+interior;lookup={p:i for i,p in enumerate(keys)}
    faces=[[lookup[i,j],lookup[i+1,j],lookup[i+1,j+1],lookup[i,j+1]] for j in range(b) for i in range(a)]
    return [om.MPoint(*values[p]) for p in interior],faces


def _weights(point,polygon,points):
    p=np.array([tuple(points[v]-point) for v in polygon]);_,_,vectors=np.linalg.svd(p,full_matrices=False)
    q=p@vectors[:2].T;distances=np.linalg.norm(q,axis=1);scale=max(distances.max(),1e-12)
    nearest=distances.argmin()
    if distances[nearest]<scale*1e-9:return {polygon[int(nearest)]:1.}
    tangents=[]
    for i,(a,b) in enumerate(zip(q,np.roll(q,-1,axis=0))):
        cross=abs(a[0]*b[1]-a[1]*b[0]);dot=a@b
        if cross<scale*scale*1e-10 and dot<0:
            j=(i+1)%len(polygon);t=distances[i]/(distances[i]+distances[j])
            return {polygon[i]:1-t,polygon[j]:t}
        tangents.append(cross/max(distances[i]*distances[(i+1)%len(q)]+dot,scale*scale*1e-20))
    w=(np.array(tangents)+np.roll(tangents,1))/distances;w/=w.sum()
    return {v:float(t) for v,t in zip(polygon,w) if t>1e-12}


def build(source,flat,rounded,polygons,selected,source_edges,reference,matrix,caps_override=None):
    original_polygons=polygons;polygons=[]
    for polygon in original_polygons:
        # polyBevel3 occasionally repeats the closing vertex on a residual
        # face. A newly created Maya mesh drops that invalid face entirely.
        clean=[v for i,v in enumerate(polygon) if i==0 or v!=polygon[i-1]]
        if len(clean)>1 and clean[-1]==clean[0]:clean.pop()
        polygons.append(clean)
    neighbors=defaultdict(set)
    for e in selected:
        a,b=source_edges[e];neighbors[a].add(b);neighbors[b].add(a)
    source_world=[p*matrix for p in source];flat_world=[p*matrix for p in flat]
    anchors=source_anchors(source_world,flat_world,neighbors,reference)
    caps={};spheres={}
    for face,polygon in enumerate(polygons):
        group={anchors[v] for v in polygon}
        if len(group)!=1 or None in group:continue
        anchor=next(iter(group));degree=len(neighbors[anchor])
        if degree<2:continue
        if degree==2:
            a,b=[(source_world[v]-source_world[anchor]).normal() for v in neighbors[anchor]]
            if abs(a*b)>1-1e-6:continue
        caps[face]=anchor;spheres[anchor]=_sphere([rounded[v] for v in polygon])
    if caps_override is not None:
        caps={face:face for face in caps_override};anchors=[None]*len(flat);spheres={}
        for face in caps:
            for v in polygons[face]:anchors[v]=face
            spheres[face]=_sphere([rounded[v] for v in polygons[face]])
    if polygons==original_polygons and not any(len(polygons[f])!=4 for f in caps):return None
    edge_faces=defaultdict(list)
    for face,polygon in enumerate(polygons):
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):edge_faces[tuple(sorted((a,b)))].append(face)
    candidates=[];seen=set()
    for cap in sorted(caps):
        polygon=polygons[cap]
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):
            edge=tuple(sorted((a,b)));path=[edge];previous=cap;crossed=[];visited=set()
            while True:
                choices=[f for f in edge_faces[edge] if f!=previous]
                if not choices:target=None;break
                if len(choices)!=1:raise ValueError('Bevel: non-manifold junction boundary.')
                face=choices[0]
                if face in caps:target=face;break
                if len(polygons[face])!=4:target=None;break
                if face in visited:target=cap;break
                visited.add(face);crossed.append(face);v=polygons[face]
                i=next(i for i in range(4) if tuple(sorted((v[i],v[(i+1)%4])))==edge)
                previous=face;edge=tuple(sorted((v[(i+2)%4],v[(i+3)%4])));path.append(edge)
            identity=frozenset(path)
            if target==cap or identity in seen:continue
            seen.add(identity);candidates.append((len(crossed),cap,target,path,crossed))
    parent={f:f for f in caps};parent[None]=None
    def root(v):
        while parent[v]!=v:v=parent[v]
        return v
    tree=defaultdict(list)
    for candidate in sorted(candidates,key=lambda p:(p[0],p[1],-1 if p[2] is None else p[2],p[3])):
        _,a,b,_,_=candidate;ra,rb=root(a),root(b)
        if ra==rb:continue
        parent[ra]=rb;tree[a].append((b,candidate));tree[b].append((a,candidate))
    odd={f:len(polygons[f])%2 for f in caps};splitting=[];visited=set()
    for start in [None]+sorted(caps):
        if start in visited:continue
        visited.add(start);order=[(start,None,None)]
        for node,prior,_ in order:
            for other,path in tree[node]:
                if other not in visited:visited.add(other);order.append((other,node,path))
        for node,prior,path in reversed(order[1:]):
            if odd.get(node,0):
                splitting.append(path);odd[node]=0
                if prior is not None:odd[prior]^=1
        if start is not None and odd[start]:raise ValueError('Bevel: unable to route an odd junction through neighboring quads.')
    flat=list(flat);rounded=list(rounded);defs={};splits={};divided={}
    def midpoint(edge):
        if edge not in splits:
            a,b=edge;splits[edge]=len(flat);flat.append(flat[a]+(flat[b]-flat[a])*.5)
            point=rounded[a]+(rounded[b]-rounded[a])*.5
            sphere=spheres.get(anchors[a]) if anchors[a]==anchors[b] else None
            rounded.append(_project(point,sphere));defs[splits[edge]]={a:.5,b:.5}
        return splits[edge]
    crossed_faces=set()
    for _,_,_,path,crossed in splitting:
        for edge in path:midpoint(edge)
        crossed_faces.update(crossed)
    for face in crossed_faces:
        polygon=polygons[face];edges=[tuple(sorted((a,b))) for a,b in zip(polygon,polygon[1:]+polygon[:1])]
        selected_edges=[i for i,e in enumerate(edges) if e in splits]
        if len(selected_edges)==4:
            center=len(flat);flat.append(om.MPoint(sum((om.MVector(flat[v]) for v in polygon),om.MVector())/4))
            rounded.append(om.MPoint(sum((om.MVector(rounded[v]) for v in polygon),om.MVector())/4));defs[center]={v:.25 for v in polygon}
            divided[face]=[[v,splits[edges[i]],center,splits[edges[i-1]]] for i,v in enumerate(polygon)]
        elif len(selected_edges)==2 and (selected_edges[0]+2)%4==selected_edges[1]:
            i=selected_edges[0];v=polygon[i:]+polygon[:i];a=splits[edges[i]];b=splits[edges[(i+2)%4]]
            divided[face]=[[v[0],a,b,v[3]],[a,v[1],v[2],b]]
        else:raise ValueError('Bevel: invalid neighboring quad cut.')
    faces=[];owners=[];records=[]
    for owner,polygon in enumerate(polygons):
        boundary=[]
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):
            boundary.append(a);edge=tuple(sorted((a,b)))
            if edge in splits:boundary.append(splits[edge])
        if owner in caps:
            if len(boundary)%2:raise ValueError('Bevel: junction parity was not resolved.')
            new,cells=_grid(boundary,flat);new_round,_=_grid(boundary,rounded);ids=list(boundary)
            for point,curved in zip(new,new_round):
                ids.append(len(flat));defs[len(flat)]=_weights(point,polygon,flat)
                flat.append(point);rounded.append(_project(curved,spheres.get(caps[owner])))
            pieces=[[ids[i] for i in face] for face in cells]
        else:pieces=divided.get(owner,[boundary])
        for piece in pieces:
            faces.append(piece);owners.append(owner)
            records.append([{original_polygons[owner].index(v):w for v,w in defs.get(vertex,{vertex:1.}).items()} for vertex in piece])
    return flat,rounded,faces,owners,records,splits

def _edge_keys(ring):
    return [tuple(sorted((a, b))) for a, b in zip(ring, ring[1:] + ring[:1])]


def _corner_sections(faces, anchors, neighbors, candidates):
    caps, surroundings, sections = defaultdict(list), defaultdict(list), defaultdict(set)
    for face, ring in enumerate(faces):
        group = {anchors[v] for v in ring}
        for corner in candidates.intersection(group):
            surroundings[corner].append(face)
            other = group - {corner}
            if not other:
                caps[corner].append(face)
            elif len(other) == 1 and other <= neighbors[corner]:
                section = sections[corner, next(iter(other))]
                section.update(pair for pair in _edge_keys(ring) if all(anchors[v] == corner for v in pair))
    return caps, surroundings, sections


def _section_path(edges):
    links = defaultdict(set)
    for a, b in edges:
        links[a].add(b)
        links[b].add(a)
    ends = sorted(v for v, adjacent in links.items() if len(adjacent) == 1)
    if len(ends) != 2 or any(len(adjacent) > 2 for adjacent in links.values()):
        return []
    ring, seen = [ends[0]], {ends[0]}
    while following := links[ring[-1]] - seen:
        vertex, = following
        ring.append(vertex)
        seen.add(vertex)
    return ring if len(ring) == len(links) else []


def join_sharp_rows(source, radial, automatic, polygons, natural, selected_pairs, reference, matrix):
    """Keep the radial cells at branch corners and close simple two-edge miters."""
    neighbors = defaultdict(set)
    for a, b in selected_pairs:
        neighbors[a].add(b)
        neighbors[b].add(a)
    candidates = {v for v, next_vertices in neighbors.items() if len(next_vertices) == 2}
    if not candidates:
        return None
    original = [p * matrix for p in source]
    first = [p * matrix for p in radial]
    second = [p * matrix for p in automatic]
    radial_anchors = source_anchors(original, first, neighbors, reference)
    auto_anchors = source_anchors(original, second, neighbors, reference)
    caps, around, rows = _corner_sections(polygons, radial_anchors, neighbors, candidates)
    plain, _, auto_rows = _corner_sections(natural, auto_anchors, neighbors, candidates)
    chosen = sorted(caps.keys() - plain.keys())
    if not chosen:
        return None
    edge_count = Counter(edge for ring in polygons for edge in _edge_keys(ring))
    faces = [list(ring) for ring in polygons]
    records = [list(range(len(ring))) for ring in polygons]
    replace, adjusted = {}, {}
    for corner in chosen:
        forward, rear = sorted(neighbors[corner])
        cut = [_section_path(rows[corner, v]) for v in (forward, rear)]
        native = [_section_path(auto_rows[corner, v]) for v in (forward, rear)]
        if not all(cut + native) or len({len(path) for path in cut + native}) != 1:
            continue
        if set(native[0]) != set(native[1]):
            continue
        mapping = {}
        for path, guide in zip(cut, native):
            ordered = min((guide, guide[::-1]),
                          key=lambda seq: sum((first[a] - second[b]).length() ** 2 for a, b in zip(path, seq)))
            if any(a in mapping and mapping[a] != b for a, b in zip(path, ordered)):
                break
            mapping.update(zip(path, ordered))
        else:
            boundary = Counter(edge for face in caps[corner] for edge in _edge_keys(polygons[face]))
            extras = {v for edge, uses in boundary.items() if uses == 1 for v in edge} - mapping.keys()
            # The residual source rail can end in a separate vertex in the
            # radial grid. Join it only when it meets the same open section.
            spacing = min((second[a] - second[b]).length()
                          for a, b in zip(native[0], native[0][1:]))
            for v in extras:
                closest = min(native[0], key=lambda b: (first[v] - second[b]).length())
                if (first[v] - second[closest]).length() > spacing * .6:
                    break
                mapping[v] = closest
            else:
                representative = {}
                weld = {v: representative.setdefault(b, v) for v, b in mapping.items()}
                deleted = set(caps[corner])
                trial = {}
                delta = Counter()
                for face in around[corner]:
                    old = faces[face]
                    delta.subtract(_edge_keys(old))
                    ring = []
                    local = []
                    if face not in deleted:
                        joined = [weld.get(v, v) for v in old]
                        for i, v in enumerate(joined):
                            if v != joined[i - 1]:
                                ring.append(v)
                                local.append(records[face][i])
                        if len(ring) < 3 or len(set(ring)) != len(ring):
                            break
                        delta.update(_edge_keys(ring))
                    trial[face] = ring, local
                else:
                    inherited = Counter()
                    for edge in delta:
                        if edge_count[edge] != 1:
                            continue
                        a, b = (weld.get(v, v) for v in edge)
                        if a != b:
                            inherited[tuple(sorted((a, b)))] += 1
                    # A simple miter can terminate on an open rim. Keep its
                    # existing boundary edges after the remap, and still
                    # reject newly opened seams or merged borders.
                    if any(edge_count[e] + delta[e] not in ((1,) if e in inherited else (0, 2))
                           for e in delta.keys() | inherited.keys()) or any(n != 1 for n in inherited.values()):
                        continue
                    for face, (ring, local) in trial.items():
                        faces[face], records[face] = ring, local
                    edge_count.update(delta)
                    replace.update(weld)
                    adjusted.update({v: automatic[b] for b, v in representative.items()})
    if not replace:
        return None
    kept = sorted({v for ring in faces for v in ring})
    indices = {vertex: i for i, vertex in enumerate(kept)}
    points = [adjusted.get(v, radial[v]) for v in kept]
    owners = [face for face, ring in enumerate(faces) if ring]
    result = [[indices[v] for v in faces[face]] for face in owners]
    records = [[{corner: 1.} for corner in records[face]] for face in owners]
    remap = {v: indices[replace.get(v, v)] for v in range(len(radial)) if replace.get(v, v) in indices}
    return points, result, owners, records, remap
