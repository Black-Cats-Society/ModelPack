"""Recognize triangulated strips using a temporary mesh data object.

Recover coplanar quad cells for topology analysis only. The user's mesh, UVs,
triangles and construction history are never edited during recognition.
"""
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn


class _WorldMesh:
    def __init__(self,fn,data):
        self.fn=fn;self.data=data

    def dagPath(self):
        return self.fn.object()

    def getPoints(self,space):
        return self.fn.getPoints(om.MSpace.kObject)

    def getPolygonNormal(self,face,space):
        return self.fn.getPolygonNormal(face,om.MSpace.kObject)

    def __getattr__(self,name):
        return getattr(self.fn,name)


def _quad(first,second,points,normal):
    sides={}
    for face in (first,second):
        for a,b in zip(face,face[1:]+face[:1]):
            key=frozenset((a,b))
            if key in sides:del sides[key]
            else:sides[key]=(a,b)
    if len(sides)!=4:return None
    following=dict(sides.values());start=min(following);loop=[start]
    for _ in range(3):
        if loop[-1] not in following:return None
        loop.append(following[loop[-1]])
    if len(set(loop))!=4 or following.get(loop[-1])!=start:return None
    for i in range(4):
        a=points[loop[(i+1)%4]]-points[loop[i]]
        b=points[loop[(i+2)%4]]-points[loop[(i+1)%4]]
        if (a^b)*normal<=1e-12:return None
    return loop


def prepare(mesh,face_ids,*,details=None,mesh_fn=None,mesh_points=None):
    from .unbevel_faces import _prepare_strips
    source=mesh_fn if mesh_fn is not None else _mesh_fn(mesh)
    points=mesh_points if mesh_points is not None else source.getPoints(om.MSpace.kWorld);selected=set(face_ids)
    triangles={f for f in selected if len(source.getPolygonVertices(f))==3}
    if len(triangles)<2:
        raise ValueError("Не найдены парные треугольники для восстановления сечений.")
    faces=[list(source.getPolygonVertices(f)) for f in range(source.numPolygons)]
    normals={f:source.getPolygonNormal(f,om.MSpace.kWorld).normal() for f in triangles}
    edge=om.MItMeshEdge(source.dagPath());candidates=[]
    while not edge.isDone():
        neighbors=list(edge.getConnectedFaces())
        if len(neighbors)==2 and set(neighbors)<=triangles:
            a,b=neighbors
            if normals[a]*normals[b]>1-1e-6:
                quad=_quad(faces[a],faces[b],points,normals[a])
                if quad:
                    vertices=source.getEdgeVertices(edge.index())
                    length=(points[vertices[0]]-points[vertices[1]]).length()
                    candidates.append((-length,a,b,quad))
        edge.next()
    merged={};removed=set()
    for _,a,b,quad in sorted(candidates):
        if a in merged or b in merged or a in removed or b in removed:continue
        merged[a]=quad;removed.add(b)
    if not merged:
        raise ValueError("Треугольники не образуют восстанавливаемые сечения фаски.")
    virtual=[];selected_virtual=[]
    for face,vertices in enumerate(faces):
        if face in removed:continue
        if face in selected:selected_virtual.append(len(virtual))
        virtual.append(merged.get(face,vertices))
    data=om.MFnMeshData().create();fn=om.MFnMesh()
    fn.create(points,[len(face) for face in virtual],[v for face in virtual for v in face],parent=data)
    recovered=fn.getPoints()
    if len(recovered)!=len(points) or any((a-b).length()>1e-7 for a,b in zip(points,recovered)):
        raise ValueError("Не удалось сохранить соответствие вершин при распознавании треугольников.")
    try:
        result=_prepare_strips(mesh,selected_virtual,details=details,mesh_fn=_WorldMesh(fn,data))
    except RuntimeError as error:
        raise ValueError("Не удалось построить корректные сечения из выбранных треугольников.") from error
    if details is not None:
        details["regions"]=[dict(faces=sorted(selected),vertices=sorted({v for f in selected for v in faces[f]}))]
    return result
