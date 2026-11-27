"""Face-region contours and smooth interior displacement, without retopology."""
from collections import defaultdict
import numpy as np
from maya.api import OpenMaya as om


class FaceRegion:
    def __init__(self, fn, faces):
        self.faces = sorted(set(faces))
        if not self.faces:raise ValueError('Select polygon faces first.')
        polygons = {f: list(fn.getPolygonVertices(f)) for f in self.faces}
        edges = defaultdict(list)
        outgoing = {}
        for face, vertices in polygons.items():
            for a, b in zip(vertices, vertices[1:] + vertices[:1]):
                edges[tuple(sorted((a, b)))].append((face, a, b))
                outgoing[face, a] = b
        if any(len(uses) > 2 for uses in edges.values()):
            raise ValueError('Selected faces contain a non-manifold edge.')
        unused = {uses[0] for uses in edges.values() if len(uses) == 1}
        if not unused:
            raise ValueError('Selected faces have no contour. Select part of the surface.')

        def following(edge):
            face, _, vertex = edge
            visited = set()
            while face not in visited:
                visited.add(face)
                other = outgoing[face, vertex]
                uses = edges[tuple(sorted((vertex, other)))]
                if len(uses) == 1:return face, vertex, other
                opposite = next(use for use in uses if use[0] != face)
                if opposite[1:] != (other, vertex):
                    raise ValueError('Selected faces have inconsistent winding.')
                face = opposite[0]
            raise ValueError('Cannot trace the selected face contour.')

        self.rows = []
        while unused:
            first = edge = min(unused)
            row = []
            while True:
                if edge not in unused:raise ValueError('Cannot trace the selected face contour.')
                unused.remove(edge)
                row.append(edge[1])
                edge = following(edge)
                if edge == first:break
            if len(row) < 3:raise ValueError('Face contours need at least three vertices.')
            self.rows.append(row + row[:1])

        self.vertices = sorted({v for polygon in polygons.values() for v in polygon})
        # Authored normal locks are immutable during this binding. Inspect once
        # so an ordinary CV drag needn't query every selected face corner again.
        selected = set(self.vertices)
        iterator = om.MItMeshVertex(fn.object())
        touching = set()
        for vertex in self.vertices:
            iterator.setIndex(vertex);touching.update(iterator.getConnectedFaces())
        self.locked_vertices = set()
        locks = {}
        for face in touching:
            for vertex, normal in zip(fn.getPolygonVertices(face),fn.getFaceNormalIds(face)):
                if vertex not in selected:continue
                if normal not in locks:locks[normal] = fn.isNormalLocked(normal)
                if locks[normal]:self.locked_vertices.add(vertex)
        self.boundary = sorted({v for row in self.rows for v in row})
        border = set(self.boundary)
        adjacency = defaultdict(set)
        for a, b in edges:
            adjacency[a].add(b);adjacency[b].add(a)
        # Every separate selected island needs a contour to drive its vertices.
        unseen = set(self.vertices)
        while unseen:
            seed = min(unseen);unseen.remove(seed)
            pending = [seed];anchored = seed in border
            while pending:
                for other in adjacency[pending.pop()]:
                    if other in unseen:
                        unseen.remove(other);pending.append(other);anchored |= other in border
            if not anchored:raise ValueError('A selected closed island has no contour. Select part of its surface.')
        self.interior = np.array(sorted(set(self.vertices) - border), dtype=np.int32)
        lookup = {int(v): i for i, v in enumerate(self.interior)}
        diagonal = np.zeros(len(lookup))
        ii, ij, iw, bi, bj, bw = [], [], [], [], [], []
        points = fn.getPoints()
        for vertex, index in lookup.items():
            for other in sorted(adjacency[vertex]):
                weight = 1.0 / max((points[vertex] - points[other]).length(), 1e-10)
                diagonal[index] += weight
                if other in lookup:
                    ii.append(index);ij.append(lookup[other]);iw.append(weight)
                else:
                    bi.append(index);bj.append(other);bw.append(weight)
        self.diagonal = diagonal
        self.ii, self.ij = np.array(ii,dtype=np.int32), np.array(ij,dtype=np.int32)
        self.iw = np.array(iw)
        self.bi, self.bj = np.array(bi,dtype=np.int32), np.array(bj,dtype=np.int32)
        self.bw = np.array(bw)
        self.previous = np.zeros((len(lookup),3))

    def spread(self, delta):
        """Harmonic displacement inside the region; all outside vertices stay put."""
        size = len(self.interior)
        if not size:return delta

        def accumulate(rows, values):
            return np.column_stack([np.bincount(rows,weights=values[:,axis],minlength=size) for axis in range(3)])

        def multiply(values):
            return self.diagonal[:,None]*values - accumulate(self.ii,self.iw[:,None]*values[self.ij])

        rhs = accumulate(self.bi,self.bw[:,None]*delta[self.bj])
        result = self.previous.copy()
        residual = rhs - multiply(result)
        z = residual / self.diagonal[:,None]
        direction = z.copy();rz = np.sum(residual*z,axis=0)
        tolerance = np.maximum(np.sum(rhs*rhs,axis=0)*1e-18,1e-24)
        for _ in range(min(2048,max(32,size*2))):
            active = np.sum(residual*residual,axis=0) > tolerance
            if not np.any(active):break
            product = multiply(direction)
            denominator = np.sum(direction*product,axis=0)
            alpha = np.divide(rz,denominator,out=np.zeros(3),where=active & (denominator>0))
            result += direction*alpha
            residual -= product*alpha
            z = residual / self.diagonal[:,None]
            next_rz = np.sum(residual*z,axis=0)
            beta = np.divide(next_rz,rz,out=np.zeros(3),where=active & (rz>0))
            direction = z + direction*beta
            rz = next_rz
        self.previous = result
        delta[self.interior] = result
        return delta
