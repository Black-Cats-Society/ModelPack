"""Dissolve only planar tessellation introduced by a Fast Draft Boolean."""
from maya import cmds
from maya.api import OpenMaya as om


def removable_edges(result, source):
    from .core import path_for
    shape=cmds.listRelatives(result,shapes=True,noIntermediate=True,fullPath=True)[0]
    path=path_for(shape);mesh=om.MFnMesh(path);original=om.MFnMesh(path_for(source))
    points=mesh.getPoints(om.MSpace.kWorld)
    if not points:return []
    bounds=mesh.boundingBox
    span=(om.MVector(bounds.max-bounds.min)*path.inclusiveMatrix()).length()
    coordinate=max(abs(value) for point in points for value in (point.x,point.y,point.z))
    # Boolean intersections are stored at float precision, including in a
    # translated asset. Test planes as well as normals before merging faces.
    tolerance=max(span*1e-6,coordinate*3e-7,1e-6)
    # Carve can split an almost planar source N-gon into pieces which differ
    # slightly in plane after intersection. Accept that small planarization,
    # while the tight normal test below keeps actual corners and bevels.
    plane_tolerance=max(tolerance,span*1e-4)
    source_faces={};result_faces={}

    def face_points(face):
        if face not in result_faces:
            result_faces[face]=[points[v] for v in mesh.getPolygonVertices(face)]
        return result_faces[face]

    def on_segment(point,a,b):
        axis=b-a;length=axis.length()
        if length<=tolerance:return False
        t=((point-a)*axis)/(length*length)
        return (-tolerance/length<=t<=1+tolerance/length and
                (point-a-axis*t).length()<=tolerance)

    def original_edge(a,b):
        # A surviving source edge (or a trimmed portion) has its midpoint on
        # one of the source faces adjacent to that edge. Search that face's
        # boundary, rather than all edges of a potentially large scene mesh.
        midpoint=a+(b-a)*.5
        closest,face=original.getClosestPoint(midpoint,om.MSpace.kWorld)
        if (closest-midpoint).length()>tolerance:return False
        if face not in source_faces:
            vertices=list(original.getPolygonVertices(face))
            source_faces[face]=[original.getPoint(v,om.MSpace.kWorld) for v in vertices]
        boundary=source_faces[face]
        return any(on_segment(a,c,d) and on_segment(b,c,d)
                   for c,d in zip(boundary,boundary[1:]+boundary[:1]))

    edges=[];iterator=om.MItMeshEdge(path)
    while not iterator.isDone():
        faces=iterator.getConnectedFaces()
        if len(faces)==2:
            normal=mesh.getPolygonNormal(faces[0],om.MSpace.kWorld)
            other=mesh.getPolygonNormal(faces[1],om.MSpace.kWorld)
            if normal*other>=1-1e-6:
                anchor=face_points(faces[0])[0]
                if all(abs((point-anchor)*normal)<=plane_tolerance
                       for point in face_points(faces[0])+face_points(faces[1])):
                    a,b=(points[v] for v in mesh.getEdgeVertices(iterator.index()))
                    if not original_edge(a,b):edges.append(f'{shape}.e[{iterator.index()}]')
        iterator.next()
    return edges


def clean(result,source):
    edges=removable_edges(result,source)
    if edges:
        # Keep this after the Boolean in history, with native Undo/Redo and
        # UV/material handling. Maya supports a planar N-gon with inner loops;
        # artificial radial bridges need not remain as real mesh edges.
        cmds.polyDelEdge(edges,cleanVertices=True,constructionHistory=True)
    return edges
