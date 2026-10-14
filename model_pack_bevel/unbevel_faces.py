"""Recognize quad bevel strips and triangular fans at their ends."""
import math
import copy

from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn


class StripAnalysis:
    """Topology and section geometry shared by one immutable mesh snapshot."""
    def __init__(self, fn, points):
        self.fn, self.points = fn, points
        self.face_edges, self.edge_faces, self.pairs = {}, {}, {}
        self.vertices, self.normals, self.sections, self.widths = {}, {}, {}, {}
        self.pair_edges, self.arcs, self.surface_normals = {}, {}, {}
        self.prepared = {}
        self.polygon = om.MItMeshPolygon(fn.dagPath())
        self.edge = om.MItMeshEdge(fn.dagPath())

    def __getattr__(self, name):
        return getattr(self.fn, name)

    def getPolygonVertices(self, face):
        if face not in self.vertices:
            self.vertices[face] = tuple(self.fn.getPolygonVertices(face))
        return self.vertices[face]

    def getPolygonNormal(self, face, space):
        key = face, space
        if key not in self.normals:
            self.normals[key] = self.fn.getPolygonNormal(face, space)
        return om.MVector(self.normals[key])

    def topology(self, face):
        if face not in self.face_edges:
            self.polygon.setIndex(face)
            self.face_edges[face] = tuple(self.polygon.getEdges())
            for edge in self.face_edges[face]:
                if edge not in self.pairs:
                    self.pairs[edge] = self.fn.getEdgeVertices(edge)
                    self.pair_edges[frozenset(self.pairs[edge])] = edge
                    self.edge.setIndex(edge)
                    self.edge_faces[edge] = tuple(self.edge.getConnectedFaces())
        return self.face_edges[face]

    def width(self, row):
        key = tuple(row)
        if key not in self.widths:
            self.widths[key] = sum((self.points[b]-self.points[a]).length()
                                   for a, b in zip(row, row[1:]))
        return self.widths[key]

    def arc(self, row):
        key=tuple(row)
        if key not in self.arcs:self.arcs[key]=_arc_corner(row,self.points)
        result=self.arcs[key]
        return None if result is None else om.MPoint(result)

    def surface_normal(self, face):
        if face not in self.surface_normals:
            vs=self.getPolygonVertices(face);p=self.points[vs[0]]
            self.surface_normals[face]=sum(((self.points[x]-p)^(self.points[y]-p)
                                          for x,y in zip(vs[1:-1],vs[2:])),om.MVector()).normal()
        return om.MVector(self.surface_normals[face])

    def corner(self, row, supports, patch_vertices):
        # Only support vertices affect membership tests in the corner solvers.
        # A growing strip can reuse its interior sections, but a new support
        # or a newly blocked outgoing vertex must force a fresh calculation.
        faces = tuple(tuple(sorted(supports[side][vertex]))
                      for side, vertex in enumerate((row[0], row[-1])))
        support_vertices = {v for side in faces for f in side for v in self.getPolygonVertices(f)}
        key = tuple(row), faces, frozenset(support_vertices & patch_vertices)
        if key not in self.sections:
            try:
                self.sections[key] = _section_corner(row, supports, self, self.points, patch_vertices)
            except ValueError as error:
                self.sections[key] = str(error)
        result = self.sections[key]
        if isinstance(result, str):
            raise ValueError(result)
        return om.MPoint(result)


def _paths(edges, pairs, allow_closed=False):
    neighbors = {}
    for edge in edges:
        a, b = pairs[edge]
        neighbors.setdefault(a, set()).add(b)
        neighbors.setdefault(b, set()).add(a)
    if any(len(items) > 2 for items in neighbors.values()):
        raise ValueError("Выделение фаски содержит ветвление.")
    remaining = set(neighbors)
    paths = []
    ends = sorted((v for v in remaining if len(neighbors[v]) == 1),reverse=True)
    while remaining:
        while ends and ends[-1] not in remaining:
            ends.pop()
        closed = not ends
        if closed and not allow_closed:
            raise ValueError("Поперечное сечение фаски не должно замыкаться.")
        path, previous, vertex = [], None, min(remaining) if closed else ends.pop()
        while vertex is not None:
            if closed and path and vertex == path[0]:
                path.append(vertex)
                break
            path.append(vertex)
            remaining.remove(vertex)
            following = neighbors[vertex] - {previous}
            previous, vertex = vertex, min(following,default=None)
        paths.append(path)
    return paths


def _linear_corner(row, supports, fn, points, patch_vertices):
    """Intersect outgoing surface directions within the bevel section.

    A warped quad has no single supporting plane. Its edge leaving the strip
    still defines the local continuation of the surface at that section.
    """
    directions = []
    for side, vertex in enumerate((row[0], row[-1])):
        outgoing = set()
        for face in supports[side][vertex]:
            vertices = list(fn.getPolygonVertices(face))
            index = vertices.index(vertex)
            outgoing.update(v for v in (vertices[index-1],vertices[(index+1)%len(vertices)])
                            if v not in patch_vertices)
        if not outgoing:
            return None
        direction = sum(((points[v]-points[vertex]).normal() for v in sorted(outgoing)),om.MVector())
        if direction.length()<1e-8:
            return None
        directions.append(direction.normal())
    a,b = (points[v] for v in (row[0],row[-1]))
    na,nb = directions
    if len(row)>2:
        vectors=[points[v]-a for v in row[1:]]
        normal=max((x^y for i,x in enumerate(vectors) for y in vectors[i+1:]),
                   key=lambda n:n.length(),default=om.MVector())
        if normal.length()>1e-10:
            normal.normalize()
            if max(abs(v*normal) for v in vectors)<=max((b-a).length()*.02,1e-7):
                directions=[n-normal*(n*normal) for n in directions]
                if any(n.length()<1e-8 for n in directions):
                    return None
                na,nb=(n.normal() for n in directions)
    dot=na*nb; determinant=1-dot*dot
    if determinant<1e-6:
        raise ValueError("Продолжения соседних поверхностей параллельны.")
    span=b-a; width=span.length()
    ta=(span*na-dot*(span*nb))/determinant
    tb=(dot*(span*na)-span*nb)/determinant
    pa,pb=a+na*ta,b+nb*tb
    gap_limit=.25 if len(row)>2 else .15
    if max(ta,tb)>width*.1 or max(abs(ta),abs(tb))>width*8 or (pa-pb).length()>max(width*gap_limit,1e-6):
        raise ValueError("Продолжения соседних поверхностей не образуют локальный угол.")
    return om.MPoint(*((pa[i]+pb[i])*.5 for i in range(3)))


def _arc_corner(row, points):
    """Restore a circular fillet when another fillet obscures its supports."""
    if len(row)<4:
        return None
    origin=points[row[0]]
    vectors=[points[v]-origin for v in row]
    normal=max((a^b for i,a in enumerate(vectors) for b in vectors[i+1:]),
               key=lambda n:n.length(),default=om.MVector())
    length=sum((b-a).length() for a,b in zip(vectors,vectors[1:]))
    if length<1e-12:
        return None
    precision=max(max(abs(points[v][i]) for v in row for i in range(3))*1e-7,1e-9)
    if normal.length()<length*length*1e-4:
        return None
    normal.normalize()
    tolerance=max(length*.005,precision*2)
    if any(abs(v*normal)>tolerance for v in vectors):
        return None
    x=vectors[-1].normal();y=(normal^x).normal()
    coords=[(v*x,v*y) for v in vectors]
    # Solve a scaled circle fit; relative coordinates avoid cancellation on
    # millimetre fillets far from the world origin.
    coords=[(a/length,b/length) for a,b in coords]
    matrix=[om.MVector(*(sum((a,b,1)[i]*(a,b,1)[j] for a,b in coords) for j in range(3))) for i in range(3)]
    rhs=[sum((a,b,1)[i]*(a*a+b*b) for a,b in coords) for i in range(3)]
    determinant=matrix[0]*(matrix[1]^matrix[2])
    if abs(determinant)<1e-10:
        return None
    solution=((matrix[1]^matrix[2])*rhs[0]+(matrix[2]^matrix[0])*rhs[1]+(matrix[0]^matrix[1])*rhs[2])/determinant
    center=origin+x*(solution.x*length*.5)+y*(solution.y*length*.5)
    radii=[(points[v]-center).length() for v in row];radius=sum(radii)/len(radii)
    if max(abs(r-radius) for r in radii)>tolerance:
        return None
    a,b=points[row[0]],points[row[-1]]
    na=(normal^(a-center)).normal();nb=(normal^(b-center)).normal()
    dot=na*nb;denominator=1-dot*dot
    if denominator<.01:
        return None
    span=b-a;ta=(span*na-dot*(span*nb))/denominator
    target=a+na*ta
    if max((target-points[v]).length() for v in row)>length*2:
        return None
    return target


def _planar_corner(row, supports, fn, points, patch_vertices):
    """Intersect original supporting planes when local edge directions are absent."""
    a,b=points[row[0]],points[row[-1]]
    normals = []
    constraints = []
    for side, vertex in enumerate((row[0], row[-1])):
        normal = om.MVector()
        for face in sorted(supports[side][vertex]):
            face_normal = fn.getPolygonNormal(face, om.MSpace.kWorld).normal()
            constraints.append((face_normal,points[vertex]))
            normal += face_normal
        if normal.length() < 1e-8:
            raise ValueError("Не удалось определить соседние поверхности фаски.")
        normals.append(normal.normal())
    midpoint = om.MPoint((a.x+b.x)/2, (a.y+b.y)/2, (a.z+b.z)/2)
    length = sum((points[y]-points[x]).length() for x, y in zip(row, row[1:]))
    # At a bend several original planes meet. Averaging them would move
    # the sharp corner inward; solve their intersection independently.
    matrix = [om.MVector(*(sum(n[i]*n[j] for n,_ in constraints) for j in range(3)))
              for i in range(3)]
    rhs = [sum(n[i]*((p-midpoint)*n) for n,p in constraints) for i in range(3)]
    determinant = matrix[0]*(matrix[1]^matrix[2])
    if determinant > 1e-8*(len(constraints)/3)**3:
        delta = ((matrix[1]^matrix[2])*rhs[0]+(matrix[2]^matrix[0])*rhs[1]
                 +(matrix[0]^matrix[1])*rhs[2])/determinant
        center = midpoint+delta
        if any(abs((center-p)*n)>max(length*1e-4,1e-7) for n,p in constraints):
            raise ValueError("Соседние поверхности не образуют согласованный угол.")
        return center
    section_vectors = [points[y]-points[x] for x,y in zip(row,row[1:])]
    crosses = [x ^ y for i,x in enumerate(section_vectors) for y in section_vectors[i+1:]]
    section_normal = max(crosses,key=lambda v:v.length(),default=om.MVector())
    if section_normal.length() < 1e-8:
        # One segment has only two points. Extend its section with the
        # boundary vertex's other edge on each supporting surface.
        for side,vertex in enumerate((row[0],row[-1])):
            for face in supports[side][vertex]:
                vertices = list(fn.getPolygonVertices(face))
                i = vertices.index(vertex)
                for other in (vertices[i-1],vertices[(i+1)%len(vertices)]):
                    if other not in patch_vertices:
                        section_vectors.append(points[other]-points[vertex])
        crosses = [x ^ y for i,x in enumerate(section_vectors) for y in section_vectors[i+1:]]
        section_normal = max(crosses,key=lambda v:v.length(),default=om.MVector())
    if section_normal.length() > 1e-8:
        section_normal.normalize()
        # A curved strip's end rail chord differs from its section tangent.
        # Project support normals into that section before intersecting;
        # otherwise the reconstructed endpoint slips along the strip.
        normals = [normal-section_normal*(normal*section_normal) for normal in normals]
        if any(normal.length()<1e-8 for normal in normals):
            raise ValueError("Не удалось определить направления соседних поверхностей.")
        normals = [normal.normal() for normal in normals]
    na, nb = normals
    dot = na * nb
    determinant = 1 - dot * dot
    if determinant < 1e-8:
        raise ValueError("Соседние поверхности параллельны: восстановить угол невозможно.")
    da, db = (a-midpoint)*na, (b-midpoint)*nb
    center = midpoint + na*((da-dot*db)/determinant) + nb*((db-dot*da)/determinant)
    # A section must lie across the intersection line, not along it.
    direction = (na ^ nb).normal()
    tolerance = max(length*1e-4, 1e-7)
    if any(abs((points[v]-midpoint)*direction) > tolerance for v in row):
        raise ValueError("Поперечные сечения фаски должны быть плоскими.")
    if not all(math.isfinite(center[i]) for i in range(3)) or length < 1e-8:
        raise ValueError("У фаски вырожденное сечение.")
    return center


def _section_corner(row, supports, fn, points, patch_vertices):
    if len(row)>=2:
        normals=[]
        for side,vertex in enumerate((row[0],row[-1])):
            n=om.MVector()
            for face in supports[side][vertex]:
                if isinstance(fn,StripAnalysis):n+=fn.surface_normal(face)
                else:
                    vs=list(fn.getPolygonVertices(face));p=points[vs[0]]
                    area=sum(((points[x]-p)^(points[y]-p) for x,y in zip(vs[1:-1],vs[2:])),om.MVector())
                    n+=area.normal()
            normals.append(n.normal())
        try:
            center = (fn.arc(row) if isinstance(fn,StripAnalysis) else _arc_corner(row,points)) if normals[0]*normals[1]<-.95 else None
            if center is None:center = _linear_corner(row,supports,fn,points,patch_vertices)
        except ValueError:
            center = fn.arc(row) if isinstance(fn,StripAnalysis) else _arc_corner(row,points)
            if center is None:raise
        if center is not None:return center
    try:
        return _planar_corner(row,supports,fn,points,patch_vertices)
    except ValueError:
        center=_linear_corner(row,supports,fn,points,patch_vertices)
        if center is None:raise
        return center


def _candidate(colors, transverse, pairs, edge_faces, selected, fn, points, blocked):
    sections = _paths([e for e in colors if colors[e] == transverse], pairs)
    longitudinal = _paths([e for e in colors if colors[e] != transverse], pairs,allow_closed=True)
    closed = longitudinal[0][0] == longitudinal[0][-1]
    if any((path[0]==path[-1]) != closed for path in longitudinal):
        raise ValueError("Полоса содержит неполный замкнутый контур.")
    if len(sections) < 2 or len(longitudinal) < 2:
        raise ValueError("Выделите всю открытую полосу фаски.")
    if len(selected) != (len(sections)-int(not closed))*(len(longitudinal)-1):
        raise ValueError("У полосы фаски есть отверстия или неполные сечения.")
    row_for = {v: row for row, path in enumerate(sections) for v in path}
    column_for = {v: col for col, path in enumerate(longitudinal) for v in path}
    if set(row_for) != set(column_for):
        raise ValueError("Выделение не образует полную полосу фаски.")
    order = [row_for[v] for v in (longitudinal[0][:-1] if closed else longitudinal[0])]
    if len(set(order)) != len(sections) or len(order) != len(sections):
        raise ValueError("У полосы фаски есть отверстия или неполные сечения.")
    columns = None
    rows = []
    for row in order:
        path = sections[row]
        # Closed longitudinal paths start at their lowest vertex ID. The first
        # path can therefore be an interior rail rather than a border rail.
        # Orient sections by the first complete section's endpoint, not rail 0.
        if columns is not None and column_for[path[0]] != columns[0]:
            path = list(reversed(path))
        actual = [column_for[v] for v in path]
        if len(set(actual)) != len(longitudinal) or len(actual) != len(longitudinal):
            raise ValueError("У полосы фаски есть отверстия или неполные сечения.")
        if columns is None:
            columns = actual
        if actual != columns:
            raise ValueError("Сечения фаски имеют несогласованную топологию.")
        rows.append(path)
    return _grid_candidate(rows,closed,pairs,edge_faces,fn,points,blocked)


def _grid_candidate(rows, closed, pairs, edge_faces, fn, points, blocked):
    # At each side of a section, use the surface across the longitudinal
    # boundary. End-cap faces must never define the restored sharp corner.
    by_pair = fn.pair_edges if isinstance(fn,StripAnalysis) else {frozenset(pair): edge for edge, pair in pairs.items()}
    supports = [{}, {}]
    for side, column in enumerate((0, -1)):
        for first, second in zip(rows, rows[1:]+rows[:1] if closed else rows[1:]):
            a, b = first[column], second[column]
            edge = by_pair[frozenset((a, b))]
            outside = [f for f in edge_faces[edge] if f not in blocked]
            if len(outside) != 1:
                raise ValueError("С обеих сторон фаски должны быть соседние поверхности.")
            for vertex in (a, b):
                supports[side].setdefault(vertex, set()).add(outside[0])
    centers = []
    patch_vertices = {v for row in rows for v in row}
    for row in rows:
        centers.append(fn.corner(row,supports,patch_vertices) if isinstance(fn,StripAnalysis)
                       else _section_corner(row,supports,fn,points,patch_vertices))
    average_length = sum(fn.width(row) if isinstance(fn,StripAnalysis) else
                         sum((points[b]-points[a]).length() for a,b in zip(row,row[1:]))
                         for row in rows)/len(rows)
    return average_length, rows, centers, closed


def _weights(centers, closed):
    distances=[0.0]
    for a,b in zip(centers,centers[1:]):distances.append(distances[-1]+(b-a).length())
    total=distances[-1]+((centers[0]-centers[-1]).length() if closed else 0)
    if total<1e-8:raise ValueError("Полоса фаски имеет нулевую длину.")
    return [2*min(d/total,1-d/total) if closed else d/total for d in distances]


def extend_strip(analysis, patch, rows, boundary, incoming):
    """Validate an added quad row without recoloring the existing grid.

    Both possible directions still use the same supporting-surface and corner
    solvers as a complete strip. Irregular additions use the ordinary parser.
    """
    columns=len(rows[0])
    if (len(rows)<2 or any(len(r)!=columns for r in rows) or
            len(patch)!=(len(rows)-1)*(columns-1) or
            any(len(analysis.topology(f))!=4 for f in patch)):
        raise ValueError('Добавление требует регулярную открытую полосу.')
    vertices={v for r in rows for v in r};outgoing={}
    for face,a,b in zip(incoming,boundary,boundary[1:]):
        edges=analysis.topology(face)
        if len(edges)!=4 or any(len(analysis.edge_faces[e])>2 for e in edges):
            raise ValueError('Добавление содержит неоднозначную топологию.')
        vs=analysis.getPolygonVertices(face)
        for first,second in ((a,b),(b,a)):
            i=vs.index(first);neighbors={vs[i-1],vs[(i+1)%4]}
            if second not in neighbors:raise ValueError('Неполное добавленное сечение.')
            other=(neighbors-{second}).pop()
            if other in vertices or first in outgoing and outgoing[first]!=other:
                raise ValueError('Добавленное сечение пересекает полосу.')
            outgoing[first]=other
    added=[outgoing[v] for v in boundary]
    if len(set(added))!=columns:raise ValueError('Добавленное сечение замыкается.')
    grid=([added]+[list(r) for r in rows] if boundary is rows[0]
          else [list(r) for r in rows]+[added])
    selected=set(patch)|set(incoming)
    def canonical(grid):
        # Match _paths' lowest-endpoint ordering, including a new row whose
        # vertex IDs precede the old grid. This keeps widths and pivots exact.
        if min(grid[0]+grid[-1]) in grid[-1]:grid=list(reversed(grid))
        if grid[0][0]>grid[0][-1]:grid=[list(reversed(r)) for r in grid]
        return grid
    candidates=[]
    for candidate in (grid,[list(r) for r in zip(*grid)]):
        try:
            candidates.append(_grid_candidate(canonical(candidate),False,analysis.pairs,
                                               analysis.edge_faces,analysis,analysis.points,selected))
        except ValueError:pass
    if not candidates:raise ValueError('Не удалось распознать добавленное сечение.')
    candidates.sort(key=lambda value:value[0])
    if len(candidates)==2 and candidates[1][0]<candidates[0][0]*1.05:
        raise ValueError('Направление добавленной полосы неоднозначно.')
    _,new_rows,centers,closed=candidates[0]
    return centers,new_rows,_weights(centers,closed)


def _fan_arc(fn, faces):
    common = None
    pairs = {}
    for face in faces:
        vertices = set(fn.getPolygonVertices(face))
        common = vertices if common is None else common & vertices
    if len(faces)<2 or len(common)!=1:
        raise ValueError("Пока поддерживаются треугольные веера с одной общей вершиной и минимум двумя гранями.")
    root = common.pop()
    for face in faces:
        pair = [v for v in fn.getPolygonVertices(face) if v!=root]
        pairs[face] = pair
    paths = _paths(pairs,pairs)
    if len(paths)!=1:
        raise ValueError("Треугольники не образуют один открытый веер.")
    if len(paths[0])!=len(faces)+1:
        raise ValueError("У треугольного веера перекрывающиеся грани.")
    return root,paths[0]


def _fan_center(fn, points, root, arc, selected):
    edge_iterator = om.MItMeshVertex(fn.dagPath())
    edge_iterator.setIndex(root)
    endpoints = {arc[0],arc[-1]}
    supports = []
    edges = om.MItMeshEdge(fn.dagPath())
    for edge in edge_iterator.getConnectedEdges():
        pair = fn.getEdgeVertices(edge)
        if not endpoints.intersection(pair):
            continue
        edges.setIndex(edge)
        outside = [face for face in edges.getConnectedFaces() if face not in selected]
        if len(outside)!=1:
            raise ValueError("Веер должен примыкать к двум исходным поверхностям; сложные угловые стыки пока не поддерживаются.")
        supports.append(fn.getPolygonNormal(outside[0],om.MSpace.kWorld).normal())
    if len(supports)!=2:
        raise ValueError("Не удалось определить соседние поверхности треугольного веера.")
    direction = supports[0]^supports[1]
    if direction.length()<1e-6:
        raise ValueError("Соседние поверхности веера параллельны: восстановить угол невозможно.")
    direction.normalize()
    # The outer arc retains the end plane even when its tip was moved while
    # removing an adjacent strip. Using fan face normals would include that
    # deformation and infer an incorrect corner.
    origin = points[arc[0]]
    vectors = [points[v]-origin for v in arc[1:]]
    normal = max((a^b for i,a in enumerate(vectors) for b in vectors[i+1:]),
                 key=lambda v:v.length(),default=om.MVector())
    if normal.length()<1e-8:
        raise ValueError("Внешний контур веера вырожден.")
    normal.normalize()
    tolerance = max(max(v.length() for v in vectors)*1e-4,1e-7)
    if any(abs(v*normal)>tolerance for v in vectors):
        raise ValueError("Внешний контур веера должен лежать в одной плоскости.")
    denominator = normal*direction
    if abs(denominator)<1e-6:
        raise ValueError("Не удалось восстановить угол треугольного веера.")
    center = points[root]+direction*(((origin-points[root])*normal)/denominator)
    if not all(math.isfinite(center[i]) for i in range(3)):
        raise ValueError("У треугольного веера некорректный угол.")
    return center


def _prepare_strips(mesh, face_ids, *, details=None, mesh_fn=None, mesh_points=None, analysis=None):
    """Return ordered pivots, vertex chains and per-strip longitudinal weights.

    Each strip and terminal triangle fan is inferred independently. The
    optional details collect fan roots eligible for local merge cleanup.
    """
    fn = analysis if analysis is not None else (mesh_fn if mesh_fn is not None else _mesh_fn(mesh))
    points = analysis.points if analysis is not None else (mesh_points if mesh_points is not None else fn.getPoints(om.MSpace.kWorld))
    selected = set(face_ids)
    triangles = set()
    relations, pairs, edge_faces, face_edges = {}, {}, {}, {}
    iterator = om.MItMeshPolygon(fn.dagPath())
    edge_iterator = om.MItMeshEdge(fn.dagPath())
    for face in sorted(selected):
        if analysis is not None and len(face_edges)%128==0:
            getattr(analysis,'checkpoint',lambda *_:None)('Проверка полосы фаски')
        if analysis is not None:edges = list(analysis.topology(face))
        else:
            iterator.setIndex(face)
            edges = list(iterator.getEdges())
        if len(edges) == 3:
            for edge in edges:
                connected=analysis.edge_faces[edge] if analysis is not None else None
                if connected is None:
                    edge_iterator.setIndex(edge);connected=edge_iterator.getConnectedFaces()
                if len(connected)>2:
                    raise ValueError("UnBevel не поддерживает неманифолдный веер.")
            triangles.add(face)
            continue
        if len(edges) != 4:
            raise ValueError("Пока поддерживаются полосы из четырёхугольников и треугольные веера на торцах.")
        face_edges[face] = edges
        for i, edge in enumerate(edges):
            relations.setdefault(edge, []).extend(((edges[(i+1)%4], 1), (edges[(i+2)%4], 0)))
            if edge not in pairs:
                if analysis is not None:
                    pairs[edge] = analysis.pairs[edge]
                    edge_faces[edge] = analysis.edge_faces[edge]
                else:
                    pairs[edge] = fn.getEdgeVertices(edge)
                    edge_iterator.setIndex(edge)
                    edge_faces[edge] = list(edge_iterator.getConnectedFaces())
                if len(edge_faces[edge]) > 2:
                    raise ValueError("UnBevel не поддерживает неманифолдную полосу фаски.")
    remaining = set(relations)
    all_rows, all_centers, all_weights, seen_vertices = [], [], [], set()
    while remaining:
        first = min(remaining)
        colors, pending = {first: 0}, [first]
        while pending:
            edge = pending.pop()
            for neighbor, parity in relations[edge]:
                expected = colors[edge] ^ parity
                if neighbor in colors:
                    if colors[neighbor] != expected:
                        raise ValueError("Выделение не образует согласованную полосу фаски.")
                else:
                    colors[neighbor] = expected
                    pending.append(neighbor)
        remaining.difference_update(colors)
        patch = {f for f, edges in face_edges.items() if edges[0] in colors}
        candidates, failures = [], []
        for transverse in (0, 1):
            try:
                candidates.append(_candidate(colors, transverse, pairs, edge_faces, patch, fn, points,selected))
            except ValueError as exc:
                failures.append(str(exc))
        if not candidates:
            raise ValueError("Не удалось распознать полосу фаски. " + " ".join(dict.fromkeys(failures)))
        candidates.sort(key=lambda value: value[0])
        if len(candidates) == 2 and candidates[1][0] < candidates[0][0]*1.05:
            raise ValueError("Направление фаски неоднозначно. Выделите всю полосу или используйте поперечные рёбра.")
        _, rows, centers, closed = candidates[0]
        vertices = {v for row in rows for v in row}
        if vertices & seen_vertices:
            raise ValueError("Полосы фаски касаются в вершине: угловые стыки пока не поддерживаются.")
        seen_vertices.update(vertices)
        all_rows.extend(rows)
        all_centers.extend(centers)
        all_weights.extend(_weights(centers,closed))
    polygon = om.MItMeshPolygon(fn.dagPath())
    pending_triangles = set(triangles)
    fans = []
    while pending_triangles:
        group,stack = set(),[min(pending_triangles)]
        while stack:
            face = stack.pop()
            if face in group:
                continue
            group.add(face)
            pending_triangles.discard(face)
            polygon.setIndex(face)
            stack.extend(f for f in polygon.getConnectedFaces() if f in triangles and f not in group)
        if len(group)==1:
            vertices=set(fn.getPolygonVertices(next(iter(group))))
            matching=[row for row in all_rows if len(row)==2 and set(row)<=vertices
                      and len(vertices-set(row))==1 and not (vertices-set(row)) & seen_vertices]
            if len(matching)!=1:
                raise ValueError("Один торцевой треугольник должен примыкать к крайнему сечению полосы фаски.")
            arc=list(matching[0]);root=next(iter(vertices-set(arc)))
        else:
            root,arc = _fan_arc(fn,group)
        attached = any(set(arc)<=set(row) and root not in row for row in all_rows)
        if attached and root in seen_vertices:
            raise ValueError("Вершина веера входит в другую фаску: сложные угловые стыки пока не поддерживаются.")
        if not attached:
            if set(arc)&seen_vertices or root in seen_vertices:
                raise ValueError("Веер пересекается с другой фаской: сложные угловые стыки пока не поддерживаются.")
            center = _fan_center(fn,points,root,arc,selected)
            all_centers.append(center)
            all_rows.append(arc)
            all_weights.append(.5)
            seen_vertices.update(arc)
        fans.append(dict(root=root,arc=arc))
    if details is not None:
        details["fans"] = fans
    return all_centers, all_rows, all_weights


def _collapsed_caps(fn, selected, rows):
    """Terminal fill entirely covered by two collapsing bevel sections.

    Dissolving support edges can join an end fill directly to both strips.
    Such a face becomes an edge together with the bevel, while every other
    neighboring face must retain its area and orientation.
    """
    group_for={v:i for i,row in enumerate(rows) for v in row}
    affected=set();vertex=om.MItMeshVertex(fn.dagPath())
    for v in group_for:
        vertex.setIndex(v);affected.update(vertex.getConnectedFaces())
    caps=set()
    for face in affected-set(selected):
        vertices=list(fn.getPolygonVertices(face))
        if all(v in group_for for v in vertices) and len({group_for[v] for v in vertices})<=2:
            caps.add(face)
    return caps


def _prepare_with_caps(mesh, face_ids, *, details=None, analysis=None):
    """Separate selected planar end fill from the strip before coloring edges.

    Accept an omitted region only when all its vertices belong to restored
    sections and each face collapses to at most two points. Parent surfaces
    accidentally selected along with a bevel are therefore never discarded.
    """
    fn=analysis if analysis is not None else _mesh_fn(mesh)
    points=analysis.points if analysis is not None else fn.getPoints(om.MSpace.kWorld);selected=set(face_ids)
    polygon=om.MItMeshPolygon(fn.dagPath())
    normals={f:fn.getPolygonNormal(f,om.MSpace.kWorld).normal() for f in selected}
    candidates=set();neighbors={}
    for face in sorted(selected):
        if analysis is not None and len(neighbors)%128==0:
            getattr(analysis,'checkpoint',lambda *_:None)('Проверка полосы фаски')
        polygon.setIndex(face);neighbors[face]=set(polygon.getConnectedFaces())
        n=normals[face];origin=points[fn.getPolygonVertices(face)[0]]
        vertices=list(fn.getPolygonVertices(face))
        tolerance=max(max((points[v]-origin).length() for v in vertices)*1e-5,1e-7)
        if any(abs((points[v]-origin)*n)>tolerance for v in vertices):continue
        for other in neighbors[face]-selected:
            if (fn.getPolygonNormal(other,om.MSpace.kWorld).normal()-n).length()>1e-5:continue
            if all(abs((points[v]-origin)*n)<=tolerance for v in fn.getPolygonVertices(other)):
                candidates.add(face);break
    regions=[];remaining=set(candidates)
    while remaining:
        region=set();stack=[min(remaining)]
        while stack:
            f=stack.pop()
            if f in region:continue
            region.add(f);remaining.discard(f)
            stack.extend(g for g in neighbors[f]&candidates if g not in region and (normals[g]-normals[f]).length()<1e-5)
        regions.append(region)
    # Two separate end planes are common; cap the search on unrelated broad
    # selections and retain the ordinary recognizers' diagnostic on failure.
    trials=list(regions)
    if 1<len(regions)<=8:
        trials.extend(a|b for i,a in enumerate(regions) for b in regions[i+1:])
        trials.append(set().union(*regions))
    for omitted in trials:
        local={}
        try:result=_prepare_strips(mesh,selected-omitted,details=local,analysis=analysis)
        except ValueError:continue
        caps=_collapsed_caps(fn,selected-omitted,result[1])
        if not omitted<=caps:continue
        if details is not None:details.update(local)
        return result
    raise ValueError("Не удалось отделить торцевые полигоны от сечений фаски.")


def _protect_surfaces(fn, selected, centers, rows, points=None):
    """Keep narrow surviving cap faces oriented while collapsing each section.

    Imported rounded caps may have an extra rim just outside the bevel. A
    slightly skew inferred corner can cross that rim. Move the sharp target
    locally within its section instead of folding the surviving polygon.
    """
    if points is None:points=fn.getPoints(om.MSpace.kWorld)
    group_for={v:i for i,row in enumerate(rows) for v in row}
    affected=set();vertex=om.MItMeshVertex(fn.dagPath())
    for v in group_for:
        vertex.setIndex(v);affected.update(f for f in vertex.getConnectedFaces() if f not in selected)
    faces={f:list(fn.getPolygonVertices(f)) for f in affected}
    def area(vertices,targets):
        positions=[targets[group_for[v]] if v in group_for else points[v] for v in vertices]
        origin=positions[0];normal=om.MVector()
        for a,b in zip(positions[1:-1],positions[2:]):normal+=(a-origin)^(b-origin)
        return normal*.5
    normals={};minimum={}
    original=[om.MPoint(p) for p in centers]
    section_normals=[];limits=[]
    for row in rows:
        vectors=[points[v]-points[row[0]] for v in row[1:]]
        normal=max((a^b for i,a in enumerate(vectors) for b in vectors[i+1:]),key=lambda n:n.length(),default=om.MVector())
        section_normals.append(normal.normal())
        limits.append(max(sum((points[b]-points[a]).length() for a,b in zip(row,row[1:])),1e-6)*.25)
    for f,vertices in faces.items():
        origin=points[vertices[0]];normal=om.MVector()
        for a,b in zip(vertices[1:-1],vertices[2:]):normal+=(points[a]-origin)^(points[b]-origin)
        normals[f]=normal.normal();minimum[f]=max(normal.length()*.005,1e-12)
    for _ in range(32):
        changed=False
        for f,vertices in sorted(faces.items()):
            value=area(vertices,centers)*normals[f]
            if value>=minimum[f]:continue
            candidates=[]
            for group in sorted({group_for[v] for v in vertices if v in group_for}):
                base=centers[group];gradient=[]
                for axis in range(3):
                    shifted=om.MPoint(base);shifted[axis]+=1
                    centers[group]=shifted
                    gradient.append(area(vertices,centers)*normals[f]-value)
                centers[group]=base
                direction=om.MVector(gradient);n=section_normals[group]
                if n.length()>1e-8:direction-=n*(direction*n)
                if direction.length()>1e-10:candidates.append((direction.length(),group,direction))
            if not candidates:
                raise ValueError("Восстановление угла вырождает соседнюю поверхность.")
            _,group,direction=max(candidates,key=lambda c:c[0])
            centers[group]+=direction*((minimum[f]-value)*1.02/(direction*direction))
            if (centers[group]-original[group]).length()>limits[group]:
                raise ValueError("Восстановленный угол слишком сильно пересекает соседнюю поверхность.")
            changed=True
        if not changed:return centers
    raise ValueError("Не удалось восстановить фаску без переворота соседних поверхностей.")


def prepare(mesh, face_ids, *, details=None, analysis=None):
    """Recognize each connected region with its suitable geometry method."""
    selected=set(face_ids);key=frozenset(selected)
    if analysis is not None and key in analysis.prepared:
        value=analysis.prepared[key]
        if isinstance(value,str):raise ValueError(value)
        result,scratch=value
        if details is not None:details.update(copy.deepcopy(scratch))
        return ([om.MPoint(p) for p in result[0]],[list(row) for row in result[1]],list(result[2]))
    try:
        result,scratch=_prepare(mesh,selected,analysis)
    except ValueError as error:
        if analysis is not None:analysis.prepared[key]=str(error)
        raise
    if analysis is not None:
        analysis.prepared[key]=(([om.MPoint(p) for p in result[0]],
                                 [list(row) for row in result[1]],list(result[2])),copy.deepcopy(scratch))
    if details is not None:details.update(scratch)
    return result


def _prepare(mesh, selected, analysis):
    fn=analysis if analysis is not None else _mesh_fn(mesh);remaining=set(selected)
    polygon=om.MItMeshPolygon(fn.dagPath());patches=[]
    while remaining:
        patch=set();stack=[min(remaining)]
        while stack:
            face=stack.pop()
            if face in patch:continue
            patch.add(face);remaining.discard(face)
            polygon.setIndex(face)
            stack.extend(f for f in polygon.getConnectedFaces() if f in selected and f not in patch)
        patches.append(patch)
    centers=[];rows=[];weights=[];used=set();scratch={"fans":[],"regions":[]}
    from .unbevel_regions import prepare as prepare_regions
    from .unbevel_triangles import prepare as prepare_triangles
    for patch in patches:
        local={};failures=[]
        for recognizer in (_prepare_strips,prepare_regions,prepare_triangles,_prepare_with_caps):
            try:
                if recognizer in (_prepare_strips,_prepare_with_caps):
                    result=recognizer(mesh,patch,details=local,analysis=analysis)
                else:
                    result=recognizer(mesh,patch,details=local,mesh_fn=fn,
                                      mesh_points=analysis.points if analysis is not None else None)
                break
            except ValueError as error:
                failures.append(str(error));local={}
        else:
            raise ValueError(" ".join(dict.fromkeys(failures)))
        vertices={v for row in result[1] for v in row}
        vertices.update(fan["root"] for fan in local.get("fans",()))
        if vertices & used:
            raise ValueError("Раздельные участки фаски касаются только в вершине.")
        used.update(vertices)
        centers.extend(result[0]);rows.extend(result[1]);weights.extend(result[2])
        for key in scratch:scratch[key].extend(local.get(key,()))
    caps=_collapsed_caps(fn,selected,rows)
    centers=_protect_surfaces(fn,selected|caps,centers,rows,analysis.points if analysis is not None else None)
    scratch['collapsed_caps']=sorted(caps)
    return (centers,rows,weights),scratch
