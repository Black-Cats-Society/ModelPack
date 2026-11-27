"""Transfer authored normal locks only; leave generated normals automatic."""
import maya.api.OpenMaya as om


def matching_corner(fn, source_points, points, vertex, face, a, b, weight):
    """An interpolated/repositioned corner does not inherit a normal lock."""
    corner = a if a == b or weight <= 1e-12 else b if weight >= 1-1e-12 else None
    if corner is None:
        return None
    original = fn.getPolygonVertices(face)[corner]
    if any(abs(source_points[original][i]-points[vertex][i]) > 1e-8 for i in range(3)):
        return None
    return corner


class NormalTransfer:
    def __init__(self, fn, points):
        self.fn = fn
        self.locked = [fn.isNormalLocked(i) for i in range(fn.numNormals)]
        self.enabled = any(self.locked)
        self.points = points
        self.source_points = fn.getPoints() if self.enabled else None
        self.source_normals = fn.getNormals() if self.enabled else None
        self.face_ids = {}
        self.normals, self.faces, self.vertices = [], [], []

    def add(self, target_face, vertex, face, a, b, weight):
        if not self.enabled:
            return
        corner = matching_corner(self.fn, self.source_points, self.points,
                                 vertex, face, a, b, weight)
        if corner is None:
            return
        if face not in self.face_ids:
            self.face_ids[face] = self.fn.getFaceNormalIds(face)
        normal = self.face_ids[face][corner]
        if self.locked[normal]:
            self.normals.append(om.MVector(self.source_normals[normal]))
            self.faces.append(target_face)
            self.vertices.append(vertex)

    def apply(self, out):
        # setFaceVertexNormals itself locks the specified normals in Maya.
        # Never pass automatic normals to it, even just to copy their values.
        if self.normals:
            out.setFaceVertexNormals(self.normals, self.faces, self.vertices)


def transfer_bulk(fn, out, points, pieces, outcounts, outconnections, splice, ints):
    """Compact corner mapping for large meshes, including dense imported locks."""
    import numpy as np
    locked = np.fromiter((fn.isNormalLocked(i) for i in range(fn.numNormals)),
                         dtype=np.bool_, count=fn.numNormals)
    if not np.any(locked):
        return  # Ordinary models keep Maya's automatic normals without any writes.
    normalcounts, normalids = fn.getNormalIds()
    source_points = fn.getPoints()
    absent = len(locked)
    replacements = {}
    for face, parts in pieces.items():
        source_ids = fn.getFaceNormalIds(face)
        records = []
        for part in parts:
            ids = []
            for vertex, a, b, weight in part:
                corner = matching_corner(fn, source_points, points,
                                         vertex, face, a, b, weight)
                ids.append(absent if corner is None else source_ids[corner])
            records.append(ids)
        replacements[face] = records
    _, ids = splice(ints(normalids), ints(normalcounts), replacements)
    keep = np.append(locked, False)[ids]
    selected = np.flatnonzero(keep)
    if not len(selected):
        return
    source = fn.getNormals()
    values = np.fromiter((x for n in source for x in (n.x, n.y, n.z)),
                         dtype=np.float32, count=len(source)*3).reshape(-1, 3)
    required = values[ids[selected]]
    faces = np.repeat(np.arange(len(outcounts), dtype=np.int32), outcounts)
    if len(selected) < 50000:
        out.setFaceVertexNormals(om.MVectorArray(required), faces[selected].tolist(),
                                 outconnections[selected].tolist())
        return
    # Avoid millions of Python vectors for densely locked imported meshes.
    # setNormals locks its entire array; immediately restore the corner mask
    # on detached data, before any geometry is displayed or committed.
    output_ids = ints(out.getNormalIds()[1])
    automatic = out.getNormals()
    target = np.fromiter((x for n in automatic for x in (n.x, n.y, n.z)),
                         dtype=np.float32, count=len(automatic)*3).reshape(-1, 3)
    target[output_ids[selected]] = required
    out.setNormals(om.MFloatVectorArray(target))
    conflict = np.flatnonzero(np.any(np.abs(target[output_ids[selected]]-required) > 1e-6, axis=1))
    if len(conflict):
        corners = selected[conflict]
        out.setFaceVertexNormals(om.MVectorArray(required[conflict]), faces[corners].tolist(),
                                 outconnections[corners].tolist())
    free = np.flatnonzero(~keep)
    if len(free):
        out.unlockFaceVertexNormals(faces[free].tolist(), outconnections[free].tolist())
