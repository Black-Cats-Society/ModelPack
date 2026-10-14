"""Recover planar parent surfaces around irregular bevels and junctions.

This fallback handles strips and networks around planar parent surfaces. It
does not assume a rectangular grid or need the bevel's construction history.
"""
import heapq
import itertools
import math
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn


def _intersection(planes, ids, origin, tolerance):
    constraints=[planes[i] for i in sorted(ids)]
    matrix=[om.MVector(*(sum(n[i]*n[j] for n,_ in constraints) for j in range(3))) for i in range(3)]
    rhs=[sum(n[i]*(d-n*om.MVector(origin)) for n,d in constraints) for i in range(3)]
    determinant=matrix[0]*(matrix[1]^matrix[2])
    if determinant<1e-8*(len(constraints)/3)**3:
        raise ValueError("Плоскости стыка не определяют один острый угол.")
    point=origin+((matrix[1]^matrix[2])*rhs[0]+(matrix[2]^matrix[0])*rhs[1]
                 +(matrix[0]^matrix[1])*rhs[2])/determinant
    if any(abs(n*om.MVector(point)-d)>tolerance for n,d in constraints):
        raise ValueError("Исходные поверхности стыка не пересекаются в одной точке.")
    return point


def _project(planes, pair, point):
    na,da=planes[pair[0]];nb,db=planes[pair[1]]
    dot=na*nb;determinant=1-dot*dot
    a,b=da-na*om.MVector(point),db-nb*om.MVector(point)
    return point+na*((a-dot*b)/determinant)+nb*((b-dot*a)/determinant)


def prepare(mesh, face_ids, *, details=None, mesh_fn=None, mesh_points=None):
    fn=mesh_fn if mesh_fn is not None else _mesh_fn(mesh)
    points=mesh_points if mesh_points is not None else fn.getPoints(om.MSpace.kWorld)
    selected=set(face_ids)
    polygon=om.MItMeshPolygon(fn.dagPath());edge_iterator=om.MItMeshEdge(fn.dagPath())
    faces={};face_edges={};edge_faces={};neighbors={}
    for face in sorted(selected):
        polygon.setIndex(face)
        faces[face]=list(polygon.getVertices());face_edges[face]=list(polygon.getEdges())
        neighbors[face]=set(polygon.getConnectedFaces())
        if len(faces[face])<3 or polygon.getArea(om.MSpace.kWorld)<1e-12:
            raise ValueError("Выделение содержит вырожденные грани.")
        for edge in face_edges[face]:
            if edge not in edge_faces:
                edge_iterator.setIndex(edge);edge_faces[edge]=list(edge_iterator.getConnectedFaces())
                if len(edge_faces[edge])>2:
                    raise ValueError("Стык фасок имеет неманифолдные рёбра.")
    result_centers=[];result_rows=[];result_weights=[]
    remaining=set(selected);used=set();regions=[]
    while remaining:
        patch=set();stack=[min(remaining)]
        while stack:
            face=stack.pop()
            if face in patch:continue
            patch.add(face);remaining.discard(face)
            stack.extend(f for f in neighbors[face] if f in selected and f not in patch)
        vertices={v for f in patch for v in faces[f]}
        edges={e for f in patch for e in face_edges[f]}
        if vertices & used:
            raise ValueError("Раздельные участки фаски касаются только в вершине.")
        used.update(vertices)
        bound_points=[points[v] for v in vertices]
        scale=max((max(p[i] for p in bound_points)-min(p[i] for p in bound_points)) for i in range(3))
        tolerance=max(scale*1e-6,1e-7)
        planes=[];plane_for={};vertex_planes={}
        for edge in sorted(edges):
            outside=[f for f in edge_faces[edge] if f not in selected]
            if not outside:continue
            face=outside[0]
            if face not in plane_for:
                polygon.setIndex(face)
                normal=fn.getPolygonNormal(face,om.MSpace.kWorld).normal()
                if normal.length()<1e-8:
                    raise ValueError("У соседней поверхности нет корректной плоскости.")
                d=normal*om.MVector(points[polygon.getVertices()[0]])
                if any(abs(normal*om.MVector(points[v])-d)>tolerance*10 for v in polygon.getVertices()):
                    raise ValueError("Для этого стыка соседние исходные поверхности должны быть плоскими.")
                plane_for[face]=next((i for i,(n,old_d) in enumerate(planes)
                                     if (normal-n).length()<1e-5 and abs(d-old_d)<tolerance),len(planes))
                if plane_for[face]==len(planes):planes.append((normal,d))
            for vertex in fn.getEdgeVertices(edge):
                vertex_planes.setdefault(vertex,set()).add(plane_for[face])
        if len(planes)<2:
            raise ValueError("Не удалось определить исходные плоскости вокруг стыка.")
        pairs=[pair for pair in itertools.combinations(range(len(planes)),2)
               if abs(planes[pair[0]][0]*planes[pair[1]][0])<1-1e-6]
        # An isolated parent polygon is valid when all its surrounding edges
        # were beveled. A missing interior bevel face instead has a normal
        # between other parent normals; reject that incomplete selection.
        for face,index in plane_for.items():
            polygon.setIndex(face)
            if not set(polygon.getConnectedFaces())<=selected:continue
            normal=planes[index][0]
            for pair in pairs:
                if index in pair:continue
                na,nb=planes[pair[0]][0],planes[pair[1]][0];dot=na*nb
                ca=(normal*na-dot*(normal*nb))/(1-dot*dot)
                cb=(normal*nb-dot*(normal*na))/(1-dot*dot)
                if ca>1e-5 and cb>1e-5 and (normal-na*ca-nb*cb).length()<1e-4:
                    raise ValueError("В выделении фаски пропущена внутренняя грань.")
        labels={};eligible={}
        pair_data=[(pair,planes[pair[0]][0],planes[pair[1]][0],
                    planes[pair[0]][0]*planes[pair[1]][0]) for pair in pairs]
        for face in sorted(patch):
            if len(labels)%32==0:getattr(fn,'checkpoint',lambda *_:None)('Проверка угловых стыков')
            normal=fn.getPolygonNormal(face,om.MSpace.kWorld).normal()
            center=om.MPoint(*(sum(points[v][i] for v in faces[face])/len(faces[face]) for i in range(3)))
            key=tuple(normal)
            if key not in eligible:
                matches=[]
                for pair,na,nb,dot in pair_data:
                    ca=(normal*na-dot*(normal*nb))/(1-dot*dot)
                    cb=(normal*nb-dot*(normal*na))/(1-dot*dot)
                    # Variable width and diagonal cuts tilt a strip's triangles
                    # along its length. With two parent planes the edge is
                    # unambiguous; the normal need not be in their span.
                    if ca>1e-5 and cb>1e-5 and (len(planes)==2 or (normal-na*ca-nb*cb).length()<1e-4):
                        matches.append(pair)
                eligible[key]=matches
            candidates=[((_project(planes,pair,center)-center).length(),pair) for pair in eligible[key]]
            labels[face]=min(candidates)[1] if candidates else None
        memberships={v:set() for v in vertices}
        for face,pair in labels.items():
            if pair:
                for vertex in faces[face]:memberships[vertex].update(pair)
        junctions={f for f in patch if labels[f] is None}
        while junctions:
            group=set();stack=[min(junctions)];support=set()
            while stack:
                face=stack.pop()
                if face in group:continue
                group.add(face);junctions.discard(face)
                for neighbor in neighbors[face]&patch:
                    if labels[neighbor] is None:
                        if neighbor not in group:stack.append(neighbor)
                    else:support.update(labels[neighbor])
            if len(support)<3:
                raise ValueError("Не удалось отличить угловой стык от выбранной исходной поверхности.")
            local={v for f in group for v in faces[f]}
            origin=om.MPoint(*(sum(points[v][i] for v in local)/len(local) for i in range(3)))
            _intersection(planes,support,origin,tolerance*10)
            for vertex in local:memberships[vertex].update(support)
        targets={}
        for vertex,ids in memberships.items():
            ids.update(vertex_planes.get(vertex,()))
            if len(ids)<2:
                raise ValueError("Не удалось восстановить исходное ребро фаски.")
            targets[vertex]=(_project(planes,tuple(sorted(ids)),points[vertex]) if len(ids)==2
                             else _intersection(planes,ids,points[vertex],tolerance*10))
        # All vertices restored to one point share a width value, including
        # the grid at a three-way junction. No vertex is moved twice.
        centers=[];rows=[];buckets={};group_for={}
        for vertex,point in sorted(targets.items()):
            key=tuple(math.floor(point[i]/tolerance) for i in range(3))
            nearby=[g for delta in itertools.product((-1,0,1),repeat=3)
                    for g in buckets.get(tuple(a+b for a,b in zip(key,delta)),())
                    if (centers[g]-point).length()<tolerance]
            group=min(nearby) if nearby else len(rows)
            if group==len(rows):
                rows.append([]);centers.append(point);buckets.setdefault(key,[]).append(group)
            rows[group].append(vertex);group_for[vertex]=group
        graph={i:{} for i in range(len(rows))}
        for edge in edges:
            a,b=(group_for[v] for v in fn.getEdgeVertices(edge))
            if a!=b:
                length=(centers[a]-centers[b]).length()
                graph[a][b]=graph[b][a]=length
        start=next((i for i in graph if len(graph[i])==1),0)
        distances={start:0.0};queue=[(0.0,start)]
        while queue:
            distance,a=heapq.heappop(queue)
            if distance!=distances[a]:continue
            for b,length in graph[a].items():
                value=distance+length
                if value<distances.get(b,math.inf):
                    distances[b]=value;heapq.heappush(queue,(value,b))
        if len(distances)!=len(rows):
            raise ValueError("Восстановленные рёбра стыка не образуют один связный участок.")
        total=max(distances.values(),default=0)
        result_centers.extend(centers);result_rows.extend(rows)
        result_weights.extend(distances[i]/total if total>tolerance else .5 for i in graph)
        regions.append(dict(faces=sorted(patch),vertices=sorted(vertices)))
    if details is not None:
        details.update(fans=[],regions=regions)
    return result_centers,result_rows,result_weights
