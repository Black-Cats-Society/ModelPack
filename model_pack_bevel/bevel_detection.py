"""Read-only bevel face detection, with history and geometry fallbacks.

History is used only at the actual mesh input, never by guessing old face IDs.
Without history, connected coplanar faces form one region: subdividing a parent
surface must not make it look like a collection of tiny bevel faces. Geometry
recognition is deliberately heuristic; a seed supplies intent for broad chamfers.
"""
from collections import defaultdict
from dataclasses import dataclass, replace
import heapq
import itertools
import json
import math
import time

from maya import cmds
from maya.api import OpenMaya as om
from slide_for_maya.core import _mesh_fn

from . import unbevel_faces


_PROGRESS=None


class _Cancelled(RuntimeError):pass


def _checkpoint(status):
    if _PROGRESS is not None:_PROGRESS.check(status)


def _advance(fraction,status):
    if _PROGRESS is not None:
        if hasattr(_PROGRESS,'advance'):_PROGRESS.advance(fraction,status)
        else:_PROGRESS.check(status)


def _create_progress_window(status):
    """Own a modeless Qt window; Maya's progressWindow also owns a busy cursor."""
    from PySide6 import QtCore, QtWidgets

    class ProgressWindow(QtWidgets.QDialog):
        def __init__(self,parent):
            super().__init__(parent,QtCore.Qt.Tool)
            self.cancelled=False;self.cancellable=True
            self.setObjectName('ModelPackBevelSearchProgress')
            self.setWindowTitle('Поиск фасок')
            self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
            self.setCursor(QtCore.Qt.ArrowCursor)
            layout=QtWidgets.QVBoxLayout(self)
            self.label=QtWidgets.QLabel(status,self);layout.addWidget(self.label)
            self.bar=QtWidgets.QProgressBar(self);self.bar.setRange(0,100)
            self.bar.setValue(0);layout.addWidget(self.bar)
            self.cancel=QtWidgets.QPushButton('Отмена',self)
            self.cancel.clicked.connect(self.reject);layout.addWidget(self.cancel)
            self.setMinimumWidth(330)

        def update_progress(self,percent,status,cancellable):
            self.cancellable=cancellable;self.cancel.setEnabled(cancellable)
            if self.label.text()!=status:self.label.setText(status)
            self.bar.setValue(percent)

        def reject(self):
            if self.cancellable:
                self.cancelled=True;self.hide()

        def closeEvent(self,event):
            if self.cancellable:
                self.cancelled=True;event.accept()
            else:event.ignore()

    parent=next((w for w in QtWidgets.QApplication.topLevelWidgets()
                 if w.objectName()=='MayaWindow'),None)
    return ProgressWindow(parent)


class _SearchProgress:
    def __init__(self,total):
        self.total=total;self.done=0;self.last=0.;self.previous=None
        self.start=0.;self.enabled=False
        self.percent=0;self.cancellable=True;self.window=None

    def advance(self,fraction,status):
        self.percent=max(self.percent,min(88,round(88*(self.done+fraction)/self.total)))
        self.check(status)

    def phase(self,percent,status):
        self.percent=max(self.percent,percent);self.check(status)

    def _paint(self):
        if self.window is None:return
        from shiboken6 import isValid
        if isValid(self.window):self.window.repaint()

    def __enter__(self):
        global _PROGRESS
        self.previous=_PROGRESS
        self.enabled=not cmds.about(batch=True);self.start=time.perf_counter()
        if self.enabled:_PROGRESS=self
        return self

    def check(self,status):
        now=time.perf_counter()
        if not self.enabled or now-self.start<.25 or now-self.last<.1:return
        self.last=now
        if self.window is None:
            self.window=_create_progress_window(status)
            self.window.show()
        self.window.update_progress(self.percent,status,self.cancellable)
        if self.cancellable:
            from PySide6.QtCore import QCoreApplication
            QCoreApplication.processEvents()
            if self.window.cancelled:raise _Cancelled('Поиск фасок отменён.')
        else:self._paint()

    def __exit__(self,exc_type,*_):
        global _PROGRESS
        _PROGRESS=self.previous
        if self.window is not None:
            from PySide6.QtCore import QCoreApplication,QEvent
            from shiboken6 import isValid
            if isValid(self.window):
                try:
                    if exc_type is None:
                        self.window.update_progress(100,'Готово',False);self._paint()
                finally:
                    # Destroy only our window before returning, even on errors.
                    # Do not drain Maya's idle queue or touch its cursor stack.
                    self.window.hide()
                    self.window.deleteLater()
                    QCoreApplication.sendPostedEvents(self.window,QEvent.DeferredDelete)


@dataclass(frozen=True)
class BevelGroup:
    mesh: str
    faces: tuple
    source: str
    ready: bool
    reason: str = ""


@dataclass(frozen=True)
class Detection:
    groups: tuple

    @property
    def faces(self):
        return tuple(sorted({f for group in self.groups for f in group.faces}))

    @property
    def ready_faces(self):
        return tuple(sorted({f for group in self.groups if group.ready for f in group.faces}))


def _components(items, neighbors):
    remaining = set(items)
    for first in sorted(remaining):
        if first not in remaining:
            continue
        stack = [first]
        patch = set()
        while stack:
            item = stack.pop()
            if item not in remaining:
                continue
            remaining.remove(item)
            patch.add(item)
            stack.extend(neighbors[item] & remaining)
        yield patch


def _normal(points, ids):
    # Calculate from positions: Maya 2027's iterator getNormal(kWorld) can
    # return the next polygon's cached normal on a newly evaluated bevel.
    origin = points[ids[0]]
    normal = om.MVector()
    for a, b in zip(ids[1:-1], ids[2:]):
        normal += (points[a] - origin) ^ (points[b] - origin)
    return normal.normal()


class _Mesh:
    def __init__(self, mesh, mesh_fn=None):
        self.fn = _mesh_fn(mesh) if mesh_fn is None else mesh_fn
        self.mesh = self.fn.fullPathName()
        self.points = self.fn.getPoints(om.MSpace.kWorld)
        self.faces = [None] * self.fn.numPolygons
        self.normals = [None] * self.fn.numPolygons
        self.areas = [None] * self.fn.numPolygons
        self.face_edges = [None] * self.fn.numPolygons
        self.edge_faces = defaultdict(list)
        self.neighbors = defaultdict(set)
        iterator = om.MItMeshPolygon(self.fn.dagPath())
        while not iterator.isDone():
            face = iterator.index()
            if face%128==0:_advance(.05+.15*face/max(self.fn.numPolygons,1),'Чтение меша')
            self.faces[face] = list(iterator.getVertices())
            self.normals[face] = _normal(self.points, self.faces[face])
            ids = self.faces[face]
            origin = self.points[ids[0]]
            area = om.MVector()
            for a, b in zip(ids[1:-1], ids[2:]):
                area += (self.points[a] - origin) ^ (self.points[b] - origin)
            self.areas[face] = area.length() * .5
            edges = list(iterator.getEdges())
            self.face_edges[face] = edges
            for edge in edges:
                self.edge_faces[edge].append(face)
            iterator.next()
        self.lengths = {}
        for edge, faces in self.edge_faces.items():
            a, b = self.fn.getEdgeVertices(edge)
            self.lengths[edge] = (self.points[b] - self.points[a]).length()
            for face in faces:
                self.neighbors[face].update(set(faces) - {face})
        self.scale = max((max((p[i] for p in self.points), default=0) -
                          min((p[i] for p in self.points), default=0)) for i in range(3))
        self.tolerance = max(self.scale * 1e-7, 1e-10)
        self.width_tolerance = max(self.scale * 1e-9, 1e-12)
        self.regions = []
        self.region_for = {}
        self.region_neighbors = defaultdict(set)
        self.widths = []
        self._regions()

    def _regions(self):
        # Compare against the starting plane, preventing gradual normal drift
        # from joining an entire gently curved surface into a flat region.
        remaining = set(range(len(self.faces)))
        for first in range(len(self.faces)):
            if first not in remaining:
                continue
            normal = self.normals[first]
            origin = self.points[self.faces[first][0]]
            stack = [first]
            patch = set()
            while stack:
                face = stack.pop()
                if face not in remaining:
                    continue
                if (normal - self.normals[face]).length() > 1e-5:
                    continue
                if any(abs((self.points[v] - origin) * normal) > self.tolerance
                       for v in self.faces[face]):
                    if face != first:
                        continue
                remaining.remove(face)
                patch.add(face)
                stack.extend(self.neighbors[face] & remaining)
            region = len(self.regions)
            self.regions.append(patch)
            self.region_for.update((face, region) for face in patch)
            self.widths.append(_patch_width(self, patch))
        for face, neighbors in self.neighbors.items():
            region = self.region_for[face]
            self.region_neighbors[region].update(self.region_for[f] for f in neighbors
                                                 if self.region_for[f] != region)


def _mesh_plug(node, attr):
    return om.MFnMesh(node.findPlug(attr, False).asMObject())


def _topology(fn):
    counts, vertices = fn.getVertices()
    return fn.numVertices, tuple(counts), tuple(vertices)


def _history_faces(data):
    """Return None when source history cannot give a reliable classification."""
    plug = om.MFnDependencyNode(data.fn.object()).findPlug("inMesh", False)
    inputs = plug.connectedTo(True, False)
    if len(inputs) != 1:
        return None
    node = om.MFnDependencyNode(inputs[0].node())
    if node.typeName not in ("modelPackGestureBevel", "polyBevel3"):
        return None
    output = om.MFnMesh(inputs[0].asMObject())
    if _topology(output) != _topology(data.fn):
        return None
    output_topology = _topology(output)
    # Typed mesh plugs can reuse their data buffer during upstream evaluation.
    # Own the output arrays before asking the node for its source mesh.
    output_faces = [list(output.getPolygonVertices(f)) for f in range(output.numPolygons)]
    output_points = output.getPoints()
    local_tolerance = max(max((max((p[i] for p in output_points), default=0) -
                               min((p[i] for p in output_points), default=0))
                              for i in range(3)) * 1e-7, 1e-10)
    if node.typeName == "modelPackGestureBevel":
        # The undeformed small-width template retains semantic face roles even
        # for a concave/custom profile and locally varying output width.
        source = _mesh_plug(node, "sourceMesh")
        matrix = om.MFnMatrixData(node.findPlug("meshMatrix", False).asMObject()).matrix()
        points = [om.MPoint(p) * matrix for p in source.getPoints()]
        selected = json.loads(node.findPlug("selectedEdges", False).asString())
        anchors = {v for e in selected for v in source.getEdgeVertices(e)}
        flat = _mesh_plug(node, "flatMesh")
        if _topology(flat) != output_topology:
            return None
        cell = max(node.findPlug("referenceWidth", False).asDouble() * 64, 1e-8)
        buckets = defaultdict(list)
        key = lambda p: tuple(math.floor(p[i] / cell) for i in range(3))
        for vertex, point in enumerate(points):
            buckets[key(point)].append(vertex)
        origins = []
        for point in (om.MPoint(p) * matrix for p in flat.getPoints()):
            center = key(point)
            candidates = [v for delta in itertools.product((-1, 0, 1), repeat=3)
                          for v in buckets.get(tuple(a + b for a, b in zip(center, delta)), ())]
            closest = min(candidates, key=lambda v: (point - points[v]).length(), default=None)
            origins.append(points[closest] if closest in anchors and
                           (point - points[closest]).length() < cell else point)
        found = set()
        for face in range(flat.numPolygons):
            ids = list(flat.getPolygonVertices(face))
            root = origins[ids[0]]
            area = om.MVector()
            for a, b in zip(ids[1:-1], ids[2:]):
                area += (origins[a] - root) ^ (origins[b] - root)
            if area.length() < max(cell * cell * 1e-8, 1e-18):
                found.add(face)
        return found, "history"
    # Native bevel preserves planar original surfaces. Compare full planes,
    # not just normals (an offset bevel plane may share a source normal).
    source = _mesh_plug(node, "inputPolymesh")
    points = source.getPoints()
    planes = defaultdict(list)
    def normal_key(normal):
        return tuple(math.floor(normal[i] / 1e-5) for i in range(3))
    def nearby(normal):
        key = normal_key(normal)
        return (plane for delta in itertools.product((-1,0,1),repeat=3)
                for plane in planes.get(tuple(a+b for a,b in zip(key,delta)),()))
    for face in range(source.numPolygons):
        ids = list(source.getPolygonVertices(face))
        normal = _normal(points, ids)
        origin = om.MPoint(points[ids[0]])
        if any(abs((points[v] - origin) * normal) > local_tolerance for v in ids):
            return None
        if not any((normal - n).length() < 1e-5 and abs((origin - p) * n) < local_tolerance
                   for n, p in nearby(normal)):
            planes[normal_key(normal)].append((normal, origin))
    found = set()
    for face, ids in enumerate(output_faces):
        output_normal = _normal(output_points, ids)
        if not any((output_normal - normal).length() < 1e-5 and
                   all(abs((output_points[v] - origin) * normal) < local_tolerance for v in ids)
                   for normal, origin in nearby(output_normal)):
            found.add(face)
    return found, "history"


def _cone(normal, normals, max_order=3):
    """A transition normal lies between two (or at a junction three) supports."""
    if len(normals) > 12:
        nearest = heapq.nlargest(4, normals, key=lambda n: n * normal)
        # Preserve the main directions as well as nearby curved supports. A
        # dense arc of side normals must not crowd a perpendicular top out.
        for axis in range(3):
            nearest.extend((min(normals,key=lambda n:n[axis]),max(normals,key=lambda n:n[axis])))
        normals = list({tuple(n): n for n in nearest}.values())
    for a, b in itertools.combinations(normals, 2):
        dot = a * b
        if abs(dot) > .985:
            continue
        ca = (normal * a - dot * (normal * b)) / (1 - dot * dot)
        cb = (normal * b - dot * (normal * a)) / (1 - dot * dot)
        if ca > 1e-5 and cb > 1e-5 and (normal - a * ca - b * cb).length() < (.35 if len(normals)==2 else .08):
            return 2
    if max_order >= 3:
        for a, b, c in itertools.combinations(normals, 3):
            det = a * (b ^ c)
            if abs(det) < 1e-5:
                continue
            coefficients = (normal * (b ^ c) / det, a * (normal ^ c) / det,
                            a * (b ^ normal) / det)
            if min(coefficients) > 1e-5:
                return 3
    return 0


def _supports(data, faces):
    normals = []
    seen = set()
    for face in sorted({f for face in faces for f in data.neighbors[face] if f not in faces}):
        normal = data.normals[face]
        key = tuple(round(normal[i], 5) for i in range(3))
        if key not in seen:
            normals.append(normal)
            seen.add(key)
    return normals


def _patch_width(data, patch):
    edges = {e for f in patch for e in data.face_edges[f]}
    perimeter = sum(data.lengths[e] for e in edges
                    if sum(f in patch for f in data.edge_faces[e]) == 1)
    return 2 * sum(data.areas[f] for f in patch) / max(perimeter, data.width_tolerance)


def _closed_ribbons(data):
    """Quad rings followed through opposite edges, independent of world axes."""
    seen = set()
    rings = []
    for first, initial_edges in enumerate(data.face_edges):
        if len(initial_edges) != 4:
            continue
        for index in (0, 1):
            state = (first, frozenset((initial_edges[index], initial_edges[index+2])))
            if state in seen:
                continue
            face, incoming = first, initial_edges[index]
            states = set()
            while True:
                edges = data.face_edges[face]
                if len(edges) != 4 or incoming not in edges:
                    break
                outgoing = edges[(edges.index(incoming)+2) % 4]
                current = (face, frozenset((incoming, outgoing)))
                if current in states:
                    if current == state and len(states) >= 3:
                        rings.append({f for f, _ in states})
                    break
                if current in seen:
                    break
                states.add(current)
                adjacent = data.edge_faces[outgoing]
                if len(adjacent) != 2:
                    break
                face = next(f for f in adjacent if f != face)
                incoming = outgoing
            seen.update(states)
    return rings


def _annular_faces(data, sensitivity):
    """Recognize narrow closed transitions without flooding into conical walls.

    A conical support has a different normal in every radial sector. Treating
    each sector as a separate planar region can merge several concentric bands
    into one oversized candidate. Opposite-edge rings supply the missing strip
    direction; width and normals are then checked across each radial section.
    """
    ratio = {"strict": .45, "normal": .65, "extended": .9}[sensitivity]
    rings = _closed_ribbons(data)
    membership = defaultdict(set)
    for index, ring in enumerate(rings):
        for face in ring:
            membership[face].add(index)
    # A toroidal grid can close in both directions. It does not supply a unique
    # longitudinal direction, so leave it to the existing geometry/history path.
    ambiguous = {i for indices in membership.values() if len(indices) > 1 for i in indices}
    rings = [ring for i, ring in enumerate(rings) if i not in ambiguous]
    ring_for = {face: i for i, ring in enumerate(rings) for face in ring}
    neighbors = defaultdict(set)
    for face, i in ring_for.items():
        neighbors[i].update(ring_for[f] for f in data.neighbors[face]
                            if f in ring_for and ring_for[f] != i)
    found = set()
    for family in _components(range(len(rings)), neighbors):
        # Only simple chains have two unambiguous longitudinal borders.
        ends = [i for i in family if len(neighbors[i]) < 2]
        if any(len(neighbors[i]) > 2 for i in family) or not ends:
            continue
        chain = []
        previous, current = None, min(ends)
        while current is not None:
            chain.append(current)
            following = neighbors[current] - {previous}
            previous, current = current, next(iter(following), None)
        along = {e for i in chain for f in rings[i] for e in data.face_edges[f]
                 if sum(face in rings[i] for face in data.edge_faces[e]) == 2}
        across = {f: {n for e in data.face_edges[f] if e not in along
                      for n in data.edge_faces[e] if n != f}
                  for i in chain for f in rings[i]}
        for start in range(len(chain)):
            patch = set()
            for stop in range(start, len(chain)):
                patch.update(rings[chain[stop]])
                exterior = {n for f in patch for n in across[f] if n not in patch}
                borders = list(_components(exterior, data.neighbors))
                if len(borders) != 2:
                    continue
                widths = [_patch_width(data, border) for border in borders]
                width = _patch_width(data, patch)
                # The thinner support may legitimately be smaller than a bevel,
                # but a wide primary wall must not be mistaken for a transition.
                if width >= max(widths)*ratio or width >= min(widths)*1.5:
                    continue
                sections = list(_components(patch, across))
                valid = True
                for section in sections:
                    supports = {n for f in section for n in across[f] if n not in patch}
                    if len(supports) != 2:
                        valid = False
                        break
                    normals = [data.normals[f] for f in supports]
                    # Float mesh coordinates can make coplanar support rows
                    # differ by a few ten-thousandths in their normals.
                    if not all(min((data.normals[f]-n).length() for n in normals) > 1e-3
                               and _cone(data.normals[f], normals, 2) for f in section):
                        valid = False
                        break
                if valid:
                    found.update(patch)
    return found


def _geometric_faces(data, sensitivity):
    ratio = {"strict": .45, "normal": .65, "extended": .9}[sensitivity]
    ceilings = [0.] * len(data.regions)
    areas = [sum(data.areas[f] for f in region) for region in data.regions]
    heap = []
    for region, width in enumerate(data.widths):
        normal = data.normals[min(data.regions[region])]
        # A small wall meeting a broad flat cap at a sharp right angle is still
        # a wall. Actual chamfer segments make a gradual normal transition.
        limit = max((data.widths[r] * ratio for r in data.region_neighbors[region]
                     if normal * data.normals[min(data.regions[r])] > .2588 and
                     areas[region] < areas[r]), default=0)
        if width < limit and width > data.width_tolerance:
            ceilings[region] = limit
            heapq.heappush(heap, (-limit, region))
    # Propagate the local parent scale through every segment, without a fixed
    # segment count and without a global threshold for differently sized shells.
    while heap:
        negative, region = heapq.heappop(heap)
        limit = -negative
        if limit < ceilings[region]:
            continue
        for other in data.region_neighbors[region]:
            if data.width_tolerance < data.widths[other] < min(limit,data.widths[region]*2.5) and limit > ceilings[other]:
                ceilings[other] = limit
                heapq.heappush(heap, (-limit, other))
    candidates = {f for r, limit in enumerate(ceilings) if limit for f in data.regions[r]}
    # End caps and narrow parent walls can join the flood. Remove faces whose
    # normal cannot be a transition between the surrounding parent surfaces.
    for _ in range(4):
        cleaned = set()
        for patch in _components(candidates, data.neighbors):
            normals = _supports(data, patch)
            cleaned.update(f for f in patch if _cone(data.normals[f], normals))
        if cleaned == candidates:
            break
        candidates = cleaned
    confirmed = set()
    for patch in _components(candidates, data.neighbors):
        # Judge the complete cross-section, not its individual thin segments.
        # This also rejects the tiny triangular fans at a sphere's poles.
        width = _patch_width(data, patch)
        parents = {data.region_for[f] for face in patch for f in data.neighbors[face] if f not in patch}
        outside = {f for r in parents for f in data.regions[r]}
        support_width = max((_patch_width(data, p) for p in _components(outside, data.neighbors)), default=0)
        if width < support_width * ratio:
            confirmed.update(patch)
    return confirmed


def _ribbon_faces(data, sensitivity, bands_out=None, *, junctions=False, arc_bands_out=None, opposed_out=None, shallow_out=None, seeds=None, face_ids=None):
    """Follow both quad directions, then test complete transverse profiles.

    Subdivided curved parent walls cannot be classified by polygon size alone.
    Keep the normal transition and width checks local to each section instead
    of treating all surrounding normals as one global supporting cone.
    """
    states={}
    for face in (range(len(data.faces)) if face_ids is None else sorted(face_ids)):
        edges=data.face_edges[face]
        if len(edges)==4:
            for i in (0,1):
                states[(face,i)]=frozenset((edges[i],edges[i+2]))
    graph=defaultdict(set)
    for state,pair in states.items():
        face,_=state
        for edge in pair:
            adjacent=[f for f in data.edge_faces[edge] if f!=face]
            if len(adjacent)!=1 or len(data.face_edges[adjacent[0]])!=4:
                continue
            other=adjacent[0];i=data.face_edges[other].index(edge)%2
            if (other,i) not in states:continue
            graph[state].add((other,i))
    ratio={"strict":.45,"normal":.65,"extended":.9}[sensitivity]
    found=set()
    profiles={}
    circular_profiles=set()
    def circular(section, parent):
        if len(section)<4:return False
        edge=next((e for e in states[section[0]] if parent in data.edge_faces[e]),None)
        if edge is None:return False
        rows=[[v] for v in data.fn.getEdgeVertices(edge)]
        for state in section:
            face=state[0];outgoing=next(e for e in states[state] if e!=edge)
            pair=set(data.fn.getEdgeVertices(outgoing));vs=data.faces[face]
            for row in rows:
                i=vs.index(row[-1]);choices=pair & {vs[i-1],vs[(i+1)%4]}
                if len(choices)!=1:return False
                row.append(choices.pop())
            edge=outgoing
        return all(_analysis(data).arc(row) is not None for row in rows)
    for component in _components(states,graph):
        _checkpoint('Поиск полос фасок')
        if len({f for f,_ in component})!=len(component):continue
        ends=[s for s in component if len(graph[s])<2]
        closed=not ends
        chain=[];previous=None;current=min(ends or component)
        while current is not None:
            chain.append(current)
            following=graph[current]-{previous}
            previous,current=current,min(following,default=None)
            if current==chain[0]:break
        # The graph now follows a cross-section. Polygon width is measured
        # along that section, independently of tiny facets at a rounded bend.
        cell_width={s:sum(data.lengths[e] for e in data.face_edges[s[0]] if e not in states[s])*.5 for s in chain}
        support_width={s:max(cell_width[s],data.widths[data.region_for[s[0]]]) for s in chain}
        if not closed:
            for end in (0,-1):
                terminal=chain[end]
                boundary=[e for e in states[terminal] if not any((f,i) in graph[terminal]
                           for f in data.edge_faces[e] if f!=terminal[0] for i in (0,1))]
                parents=[f for e in boundary for f in data.edge_faces[e] if f!=terminal[0]]
                if len(parents)==1:
                    sentinel=(parents[0],-1)
                    cell_width[sentinel]=0.
                    support_width[sentinel]=data.widths[data.region_for[parents[0]]]
                    if end==0:chain.insert(0,sentinel)
                    else:chain.append(sentinel)
        n=len(chain)
        for start in range(len(chain)):
            if not closed and start==0:continue
            width=0.;section=[];minimum=math.inf;maximum=0.
            for length in range(1,n-1 if closed else n-start):
                s=chain[(start+length-1)%n];section.append(s);width+=cell_width[s]
                minimum=min(minimum,cell_width[s]);maximum=max(maximum,cell_width[s])
                if maximum>max(minimum*3,data.width_tolerance):break
                if length<2 and seeds is None:continue
                parents=[chain[(start-1)%n],chain[(start+length)%n]]
                if seeds is not None and not any(s[0] in seeds for s in section):continue
                normals=[data.normals[p[0]] for p in parents]
                arc=junctions and (abs(normals[0]*normals[1])>.95 if seeds is not None else normals[0]*normals[1]<-.95) and circular(section,parents[0][0])
                if not arc and normals[0]*normals[1]>math.cos(math.radians(8)):continue
                widths=[support_width[p] for p in parents]
                if seeds is None and not arc and width>=max(widths)*3:continue
                interior=[s for s in section if min((data.normals[s[0]]-v).length() for v in normals)>.02]
                collars=[[s for s in section if (data.normals[s[0]]-v).length()<=.02] for v in normals]
                widest=max((cell_width[s] for s in interior),default=0)
                narrowest=min((cell_width[s] for s in interior),default=0)
                turn=math.acos(max(-1.,min(1.,normals[0]*normals[1])))
                curvature=turn/max(width,data.width_tolerance)
                outside=[]
                for p,q in ((start-1,start-2),(start+length,start+length+1)):
                    if closed or 0<=q<n:
                        first,second=chain[p%n],chain[q%n]
                        angle=math.acos(max(-1.,min(1.,data.normals[first[0]]*data.normals[second[0]])))
                        outside.append(angle/max((cell_width[first]+cell_width[second])*.5,data.width_tolerance))
                repeated=sum((data.normals[a[0]]-data.normals[b[0]]).length()<.02 for a,b in zip(section,section[1:]))
                # A flat collar is at most one cell on each side. Repeated
                # coplanar cells belong to the parent surface, not the fillet.
                single=seeds is not None and length==1 and all(abs(n*data.normals[s[0]])<.95 for n in normals) and _cone(data.normals[s[0]],normals,2)
                seeded_profile=seeds is not None and length>=2 and len(interior)==length and all(
                    _cone(data.normals[s[0]],normals,2) for s in section)
                regular=(interior and repeated==0 and widest<max(widths)*ratio and
                    curvature>max(outside,default=0)*1.5 and
                    widest<max(narrowest*2.5,data.width_tolerance) and max(map(len,collars))<=1 and
                    all(cell_width[s]>widest*.7 and (junctions or cell_width[s]<widest*1.2 or
                        (cell_width[parents[i]] or support_width[parents[i]])>cell_width[s]*2.1)
                        for i,c in enumerate(collars) for s in c) and
                    all(cell_width[s]<widest*2.1 for c in collars for s in c) and
                    all(_cone(data.normals[s[0]],normals,2) for s in interior))
                if single or seeded_profile or arc or regular:
                    previous_face=parents[0][0]
                    incoming=[]
                    for s in section:
                        incoming.append(next(e for e in states[s] if previous_face in data.edge_faces[e]))
                        previous_face=s[0]
                    profiles[frozenset(section)]=(tuple(section),incoming,turn,width)
                    if arc:circular_profiles.add(frozenset(section))
    continuation=defaultdict(set)
    if seeds is not None:
        from .seeded_detection import walk_profile
        for section,incoming,_,_ in sorted(profiles.values(),key=lambda p:len(p[0]),reverse=True):
            patch=walk_profile(data,section,incoming)
            if bands_out is not None and patch not in bands_out:bands_out.append(patch)
        return set().union(*(bands_out or []))
    for key,(section,incoming,_,_) in profiles.items():
        for side in (1,3):
            following=[]
            for s,edge in zip(section,incoming):
                es=data.face_edges[s[0]];e=es[(es.index(edge)+side)%4]
                adjacent=[f for f in data.edge_faces[e] if f!=s[0]]
                if len(adjacent)!=1 or len(data.face_edges[adjacent[0]])!=4:break
                f=adjacent[0];following.append((f,1-data.face_edges[f].index(e)%2))
            else:
                other=frozenset(following)
                if other in profiles:
                    continuation[key].add(other);continuation[other].add(key)
    bands=[]
    arc_patches=[]
    for component in _components(profiles,continuation):
        if len(component)<3:continue
        patch={s[0] for key in component for s in key}
        bands.append((len(component),round(sum(profiles[k][2] for k in component)/len(component),2),len(patch),patch))
        if len(component & circular_profiles)>=len(component)*.8:arc_patches.append(patch)
    # A profile must repeat along the edge. Prefer the complete coherent band
    # over its nested subsets before testing whether UnBevel can restore it.
    accepted=[]
    ranking=(lambda item:(item[2]/item[0],item[0],item[1])) if junctions else (lambda item:item[:3])
    for _,turn,_,patch in sorted(bands,key=ranking,reverse=True):
        if (any(patch<=p for p in accepted) if junctions else bool(patch & found)):continue
        try:
            _strip(data,patch)
        except ValueError:continue
        accepted.append(patch)
        arc=patch in arc_patches
        if junctions:patch=_extend_band(data,patch,set(),junctions=True)
        if bands_out is not None and patch not in bands_out:bands_out.append(patch)
        if arc and arc_bands_out is not None and patch not in arc_bands_out:arc_bands_out.append(patch)
        if turn>2.8 and opposed_out is not None:opposed_out.append(patch)
        if turn<1. and shallow_out is not None:shallow_out.append(patch)
        found.update(patch)
    if bands_out is not None:
        bands_out[:]=[p for p in bands_out if not any(p<q for q in bands_out)]
    return found


def _thin_strip_faces(data, sensitivity, face_ids=None):
    """Local quad transitions on warped supporting surfaces."""
    ratio = {"strict": .35,"normal": .5,"extended": .7}[sensitivity]
    candidates = set()
    for face in (range(len(data.faces)) if face_ids is None else sorted(face_ids)):
        edges=data.face_edges[face]
        if len(edges)!=4:
            continue
        index=min((0,1),key=lambda i:data.lengths[edges[i]]+data.lengths[edges[i+2]])
        widths=[data.lengths[edges[index]],data.lengths[edges[index+2]]]
        sides=[edges[(index+1)%4],edges[(index+3)%4]]
        if max(widths)>=min(data.lengths[e] for e in sides)*ratio:
            continue
        parents=[f for e in sides for f in data.edge_faces[e] if f!=face]
        if len(parents)!=2:
            continue
        if not any(any(abs((data.points[v]-data.points[data.faces[p][0]])*data.normals[p])>data.tolerance
                       for v in data.faces[p]) for p in parents):
            continue
        width=_patch_width(data,{face})
        parent_widths=[data.widths[data.region_for[f]] for f in parents]
        parent_ratio={"strict":.45,"normal":.65,"extended":.9}[sensitivity]
        if width>=max(parent_widths)*parent_ratio or width>=min(parent_widths)*1.5:
            continue
        normals=[data.normals[f] for f in parents]
        if min((data.normals[face]-n).length() for n in normals)<.01:
            continue
        if _cone(data.normals[face],normals,2):
            candidates.add(face)
    found=set()
    for patch in _components(candidates,data.neighbors):
        try:
            unbevel_faces._prepare_strips(data.mesh,patch)
        except ValueError:
            continue
        found.update(patch)
    return found


def _attach_fans_to_patch(data, faces):
    """Include cap triangles whose outer arc is a detected strip end section."""
    result = set(faces)
    try:
        _,rows,weights=unbevel_faces._prepare_strips(data.mesh,faces)
        ends=[set(rows[0]),set(rows[-1])] if abs(weights[-1]-1)<1e-8 else []
    except ValueError:
        ends=[]
    vertices = {v for f in faces for v in data.faces[f]}
    triangles = {f for face in faces for f in data.neighbors[face] if f not in faces and len(data.faces[f]) == 3
                 and len(set(data.faces[f]) & vertices) == 2}
    for group in _components(triangles, data.neighbors):
        if len(group)==1:
            face=next(iter(group));ids=set(data.faces[face])
            arc=ids & vertices
            neighboring={f for f in data.neighbors[face] if f in faces}
            try:
                patch=next(p for p in _components(faces,data.neighbors) if p & neighboring)
                _,rows,_=unbevel_faces._prepare_strips(data.mesh,patch)
            except (ValueError,StopIteration):
                continue
            if any(len(row)==2 and set(row)==arc for row in rows):
                result.add(face)
            continue
        try:
            root, arc = unbevel_faces._fan_arc(data.fn, group)
        except ValueError:
            continue
        if root not in vertices and any(set(arc)==row for row in ends):
            result.update(group)
    return result


def _attach_fans(data, faces):
    return {f for patch in _components(faces,data.neighbors)
            for f in _attach_fans_to_patch(data,patch)}


def _seed_band(data, seed):
    """Resolve broad chamfers using a few dominant surface normal pairs."""
    regions = sorted(range(len(data.regions)), key=lambda r: sum(data.areas[f] for f in data.regions[r]),
                     reverse=True)[:24]
    choices = []
    for a, b in itertools.combinations(regions, 2):
        na, nb = data.normals[min(data.regions[a])], data.normals[min(data.regions[b])]
        if not _cone(data.normals[seed], (na, nb), 2):
            continue
        strength = min(sum(data.areas[f] for f in data.regions[r]) for r in (a,b))
        choices.append((strength, -(na*nb), a, b))
    for _, _, a, b in sorted(choices, reverse=True):
        normals = [data.normals[min(data.regions[r])] for r in (a,b)]
        candidates = {f for f, normal in enumerate(data.normals) if _cone(normal, normals, 2)}
        patch = next((p for p in _components(candidates, data.neighbors) if seed in p), set())
        if not patch:
            continue
        supports = _supports(data, patch)
        if not all(any((n-s).length() < 1e-5 for s in supports) for n in normals):
            continue
        return patch
    return set()


def _group(data, faces, source):
    _checkpoint('Проверка найденной фаски')
    key=tuple(sorted(faces))
    cache=getattr(data,'_group_cache',None)
    if cache is None:cache=data._group_cache={}
    if key in cache:return replace(cache[key],source=source)
    try:
        mapped=[data.source_faces[f] for f in faces] if hasattr(data,'source_faces') else faces
        unbevel_faces.prepare(data.mesh, mapped,analysis=_analysis(getattr(data,'source_data',data)))
        ready, reason = True, ""
    except ValueError as error:
        ready, reason = False, str(error)
    group=BevelGroup(data.mesh,key,source,ready,reason)
    cache[key]=group
    return group


def _analysis(data):
    analysis=getattr(data,'_strip_analysis',None)
    if analysis is None:analysis=data._strip_analysis=unbevel_faces.StripAnalysis(data.fn,data.points)
    analysis.checkpoint=_checkpoint
    return analysis


def _strip(data, faces):
    """Reuse geometry checks only for this immutable detection snapshot."""
    key=frozenset(faces)
    cache=getattr(data,'_strip_cache',None)
    if cache is None:cache=data._strip_cache={}
    analysis=_analysis(data)
    if key not in cache:
        try:cache[key]=unbevel_faces._prepare_strips(data.mesh,key,analysis=analysis)
        except ValueError as error:cache[key]=str(error)
    result=cache[key]
    if isinstance(result,str):raise ValueError(result)
    return result


def _extend_band(data, patch, blocked, *, junctions=False):
    cache=getattr(data,'_extension_cache',None)
    if cache is None:cache=data._extension_cache={}
    options=frozenset(blocked),junctions
    key=frozenset(patch),options
    if key in cache:return set(cache[key])
    visited=[]
    result=_extend_band_walk(data,patch,blocked,junctions=junctions,visited=visited,cache=cache,options=options)
    frozen=frozenset(result)
    for prefix in visited:cache[prefix,options]=frozen
    return result


def _extended_strip(data,patch,rows,row,incoming):
    candidate=frozenset(patch|set(incoming));cache=data._strip_cache
    if candidate not in cache:
        try:cache[candidate]=unbevel_faces.extend_strip(_analysis(data),patch,rows,row,incoming)
        except (ValueError,KeyError):return _strip(data,candidate)
    return _strip(data,candidate)


def _extend_band_walk(data, patch, blocked, *, junctions=False, visited, cache, options):
    """Continue complete quad sections up to a cap or another bevel.

    A later bevel trims an older band's endpoint. Preserve its transverse
    direction instead of flooding through the newer band at that junction.
    """
    patch=set(patch)
    while True:
        _checkpoint('Определение границ фаски')
        key=frozenset(patch)
        if (key,options) in cache:return set(cache[key,options])
        visited.append(key)
        try:_,rows,weights=_strip(data,patch)
        except ValueError:return patch
        if abs(weights[-1]-1)>1e-8:return patch
        expanded=False
        for row in (rows[0],rows[-1]):
            incoming=[]
            for a,b in zip(row,row[1:]):
                candidates=[f for f in data.neighbors[next(f for f in patch if a in data.faces[f] and b in data.faces[f])]
                            if f not in patch and a in data.faces[f] and b in data.faces[f]]
                if len(candidates)!=1 or candidates[0] in blocked or len(data.faces[candidates[0]])!=4:break
                incoming.append(candidates[0])
            else:
                if len(set(incoming))!=len(row)-1:continue
                # Flat parent subdivisions must not be treated as another
                # cross section of a curved bevel.
                if len(incoming)<2:continue
                normals=[data.normals[f] for f in incoming]
                if not junctions and max((a-b).length() for a in normals for b in normals)<.14:continue
                candidate=patch|set(incoming)
                try:_,new_rows,_=_extended_strip(data,patch,rows,row,incoming)
                except ValueError:continue
                if not junctions:
                    patch=candidate;expanded=True;break
                old_vertices={v for r in rows for v in r}
                added=[r for r in new_rows if any(v not in old_vertices for v in r)]
                width=lambda r:sum((data.points[b]-data.points[a]).length() for a,b in zip(r,r[1:]))
                if added and (len(added)!=1 or not .5<width(added[0])/max(width(row),data.width_tolerance)<2):
                    continue
                if added and len(rows)>2 and len(row)<=5 and all(_analysis(data).arc(r) is not None for r in (row,added[0])):
                    middle=lambda r:sum((om.MVector(data.points[v]) for v in r),om.MVector())/len(r)
                    previous=rows[1] if row is rows[0] else rows[-2]
                    incoming_direction=middle(row)-middle(previous)
                    next_direction=middle(added[0])-middle(row)
                    if incoming_direction.normal()*next_direction.normal()<.5:continue
                patch=candidate;expanded=True;break
        if not expanded:return patch


def _separate_touching_bands(data, patches, history, annular, ribbons=()):
    """Find each band's direction, retaining shared cells at intersections."""
    anchors=[(p,'geometry') for p in ribbons if _group(data,p,'geometry').ready]
    for faces,source in ((history[0] if history else set(),'history'),(annular,'geometry')):
        for patch in _components(faces,data.neighbors):
            group=_group(data,patch,source)
            if group.ready and not any(patch==p for p,_ in anchors):anchors.append((patch,source))
    if not anchors:return [(patch,None) for patch in patches]
    faces=set().union(*patches) if patches else set()
    for anchor,_ in anchors:
        if anchor & faces and not anchor<=faces:faces.update(anchor)
    patches=list(_components(faces,data.neighbors))
    result=[]
    for patch in patches:
        choices=[(p,s) for p,s in anchors if p<=patch]
        covered=set().union(*(p for p,_ in choices)) if choices else set()
        if covered==patch and len(choices)>1 and not _group(data,patch,'geometry').ready:
            for p,source in choices:
                complete=_extend_band(data,p,set(),junctions=True)
                result.append((complete if complete<=patch and _group(data,complete,source).ready else p,source))
            continue
        if _group(data,patch,'geometry').ready:
            result.append((patch,None));continue
        choices=[(p,s) for p,s in anchors if p<=patch and p!=patch]
        choices.sort(key=lambda entry:(entry[1]!='history',-len(entry[0])))
        if ribbons and choices:
            borders=set().union(*(p for p,_ in choices))
            remaining=list(_components(patch-borders,data.neighbors))
            bands=[_extend_band(data,p,borders,junctions=True) for p in remaining]
            if bands and all(_group(data,p,'geometry').ready for p in bands):
                union=set().union(*bands)
                if sum(map(len,bands))==len(union):
                    result.extend((_extend_band(data,p,union-p,junctions=True),'geometry') for p in bands)
                    result.extend(choices);continue
        for anchor,source in choices:
            bands=[_extend_band(data,p,anchor,junctions=bool(ribbons)) for p in _components(patch-anchor,data.neighbors)]
            union=set().union(*bands) if bands else set()
            if (not bands or sum(map(len,bands))!=len(union) or
                    not all(_group(data,p,'geometry').ready for p in bands)):continue
            # Separate the directions first, then continue each complete
            # transverse section into the common junction. A polygon may
            # belong to both the closed rim and a longitudinal fillet.
            result.extend((_extend_band(data,p,union-p,junctions=bool(ribbons)),'geometry') for p in bands)
            result.append((anchor,source));break
        else:result.append((patch,None))
    return result


def detect(mesh, *, seeds=None, sensitivity="normal", include_corners=True, use_history=True, _data=None):
    """Detect all bevel groups or the complete groups containing seed face IDs.

    Does not change selection, geometry, attributes or the undo queue. Result
    groups carry a separate UnBevel readiness check, including its failure reason.
    """
    if sensitivity not in ("strict", "normal", "extended"):
        raise ValueError("Неизвестная чувствительность поиска фасок.")
    if _data is None:
        from .search_cache import CACHE
        entry=CACHE.get(mesh)
        key=(None if seeds is None else frozenset(seeds),sensitivity,include_corners,use_history)
        if key in entry.results:return entry.results[key]
        cached=entry.seeded(key)
        if cached is not None:return cached
        version=entry.version;source=None
        try:
            if seeds is not None:
                from .seeded_detection import LocalMesh,detect_seeded
                source=LocalMesh(mesh)
                result=detect_seeded(mesh,set(seeds),include_corners=include_corners,_data=source)
            else:
                source=_Mesh(mesh)
                result=detect(mesh,sensitivity=sensitivity,include_corners=include_corners,
                              use_history=use_history,_data=source)
            if _PROGRESS is not None and version!=entry.version:
                raise _Cancelled('Меш изменился во время поиска.')
            if version==entry.version:
                analysis=_analysis(source)
                prepared={frozenset(g.faces):analysis.prepared[frozenset(g.faces)]
                          for g in result.groups if frozenset(g.faces) in analysis.prepared
                          and not isinstance(analysis.prepared[frozenset(g.faces)],str)}
                entry.remember(key,result,prepared,reusable=getattr(source,'reusable',()))
            return result
        finally:
            if source is not None and hasattr(source,'release'):source.release()
    data = _Mesh(mesh) if _data is None else _data
    key=(None if seeds is None else frozenset(seeds),sensitivity,include_corners,use_history)
    cache=getattr(data,'_detection_cache',None)
    if cache is None:cache=data._detection_cache={}
    if key not in cache:
        result=_detect_snapshot(data,seeds=seeds,sensitivity=sensitivity,
                                include_corners=include_corners,use_history=use_history)
        cache[key]=result,set(data._base_geometric)
    result,data._base_geometric=cache[key]
    return result


def _detect_snapshot(data, *, seeds=None, sensitivity="normal", include_corners=True, use_history=True):
    mesh=data.mesh
    seeds = None if seeds is None else set(seeds)
    if seeds is not None and (not seeds or not seeds <= set(range(len(data.faces)))):
        raise ValueError("Выберите существующий полигон фаски.")
    if any(len(faces) > 2 for faces in data.edge_faces.values()):
        raise ValueError("Поиск фасок требует меш без неманифолдных рёбер.")
    history = _history_faces(data) if use_history else None
    _advance(.25,'Поиск фасок')
    effective_sensitivity = "extended" if seeds else sensitivity
    annular=_annular_faces(data,effective_sensitivity)
    _advance(.35,'Проверка профилей')
    if seeds and annular:
        independent=detect(mesh,sensitivity=sensitivity,include_corners=include_corners,use_history=use_history,_data=data)
        matches=[g for g in independent.groups if g.ready and set(g.faces)&seeds]
        def edges(group):return {e for f in group.faces for e in data.face_edges[f]}
        if matches and any(edges(g)&edges(other) for g in matches for other in independent.groups if g!=other):
            return Detection(tuple(matches))
    geometric = (_geometric_faces(data, effective_sensitivity) | annular)
    base_geometric=set(geometric)
    data._base_geometric=base_geometric
    _advance(.45,'Поиск полос фасок')
    ribbon_bands=[];opposed=[];shallow=[]
    ribbons=_ribbon_faces(data,effective_sensitivity,ribbon_bands,opposed_out=opposed,shallow_out=shallow)
    if ribbons:
        ribbon_vertices={v for f in ribbons for v in data.faces[f]}
        retained=set()
        for patch in _components(geometric,data.neighbors):
            vertices={v for f in patch for v in data.faces[f]}
            outside={n for f in patch for n in data.neighbors[f] if n not in patch}
            if vertices & ribbon_vertices:
                planar=all(all(abs((data.points[v]-data.points[data.faces[f][0]])*data.normals[f])<data.tolerance
                               for v in data.faces[f]) for f in outside)
                if planar and _group(data,patch,'geometry').ready:
                    try:_strip(data,patch)
                    except ValueError:retained.update(patch)
                continue
            if len(patch)<3 and effective_sensitivity!='extended':continue
            if max((min(((data.normals[f]-data.normals[n]).length() for n in outside),default=0) for f in patch),default=0)<.26:continue
            if _group(data,patch,'geometry').ready:retained.update(patch)
        geometric=ribbons|retained
    thin = _thin_strip_faces(data, effective_sensitivity)
    _advance(.65,'Проверка границ фасок')
    if ribbons and effective_sensitivity!='extended':
        thin={f for p in _components(thin,data.neighbors) if len(p)>=3
              and not ({v for face in p for v in data.faces[face]} & ribbon_vertices)
              for f in p}
    # A strip on a warped support can end at a triangular corner shared with
    # another strip. With a seed, preserve the strip direction before adding
    # its end caps, rather than growing across that junction into other bevels.
    seeded_thin = []
    if seeds and not seeds & base_geometric:
        thin_patches = list(_components(thin,data.neighbors))
        for seed in seeds:
            matches=[p for p in thin_patches if seed in p or
                     (len(data.faces[seed])==3 and data.neighbors[seed] & p)]
            if matches:
                patch=min(matches,key=lambda p:_patch_width(data,p))
                seeded_thin.append(_attach_fans(data,patch) if include_corners else patch)
    geometric |= thin
    faces = geometric | (history[0] if history is not None else set())
    faces = {f for f in faces if data.areas[f] > data.width_tolerance ** 2}
    source = "history" if history is not None else "geometry"
    if include_corners:
        faces = _attach_fans(data, faces)
    patches = list(_components(faces, data.neighbors))
    if seeds:
        patches = seeded_thin or [p for p in patches if p & seeds]
        matched = {f for p in patches for f in p}
        # A deliberately broad one-segment chamfer can have the same scale as
        # its parent surfaces. A seed removes that width ambiguity.
        for seed in sorted(seeds - matched):
            if seed in matched or data.neighbors[seed] & faces:
                continue
            patch = data.regions[data.region_for[seed]]
            normals = _supports(data, patch)
            # A parent face may lie in the cone of its adjacent end caps.
            # Broad-seed fallback needs two genuinely different side supports,
            # with a substantial normal turn on BOTH sides of the seed.
            usable = [n for n in normals if abs(n * data.normals[seed]) < .95]
            if all(_cone(data.normals[f], usable) for f in patch):
                candidate = _group(data, patch, "seed")
                if candidate.ready:
                    patches.append(patch)
                    matched.update(patch)
                    continue
            patch = _seed_band(data, seed)
            if patch:
                patches.append(_attach_fans(data, patch) if include_corners else patch)
                matched.update(patch)
    if not include_corners:
        bands = set()
        for patch in patches:
            normals = _supports(data, patch)
            bands.update(f for f in patch if _cone(data.normals[f], normals, 2))
        patches = list(_components(bands, data.neighbors))
    patches = list(_components({f for p in patches for f in p}, data.neighbors))
    broken=any(not _group(data,p,'geometry').ready for p in patches)
    _advance(.75,'Проверка восстановления')
    original_separated=None
    if broken:
        original_separated=_separate_touching_bands(data,patches,history,annular,ribbon_bands)
        broken=any(not _group(data,p,'geometry').ready for p,_ in original_separated)
    if shallow or opposed:
        expanded=[];arcs=[]
        _ribbon_faces(data,effective_sensitivity,expanded,junctions=True,arc_bands_out=arcs)
        # A much longer coherent strip completes fragments interrupted at a
        # newer fillet. Small transverse additions must retain the old border.
        columns={}
        def column_count(p):
            key=frozenset(p)
            if key not in columns:
                columns[key]=min(map(len,_strip(data,p)[1]))
            return columns[key]
        larger=[p for p in expanded if any(q<=p and len(p)>len(q)*2 and
                0<=column_count(p)-column_count(q)<=2 for q in shallow)]
        if larger:
            vertices={v for p in larger for f in p for v in data.faces[f]}
            larger+= [p for p in expanded if p not in larger and
                       len({v for f in p for v in data.faces[f]} & vertices)>2 and
                       any(q<=p and len(p)>len(q)*2 and
                           0<=column_count(p)-column_count(q)<=2 for q in ribbon_bands)]
        replacements=[p for p in arcs if any(len(p&q)>min(len(p),len(q))*.5 for q in opposed)]
        larger=list({frozenset(p):p for p in larger+replacements}.values())
        if larger:
            broken=True
            ribbon_bands=[q for q in ribbon_bands if not any(q<=p or
                    (q in opposed and p in replacements and len(p&q)>min(len(p),len(q))*.5) for p in larger)]+larger
            vertices={v for p in larger for f in p for v in data.faces[f]}
            patches=[p for p in patches if not ({v for f in p for v in data.faces[f]} & vertices)]
            patches.extend(larger)
    separated=(original_separated if original_separated is not None and not broken and not opposed else
               _separate_touching_bands(data,patches,history,annular,ribbon_bands if broken or opposed else ()))
    if seeds:separated=[(p,s) for p,s in separated if p & seeds]
    groups = tuple(_group(data, patch, origin or (source if history is None or patch <= history[0]
                          else "geometry+history" if patch & history[0] else "geometry")) for patch,origin in separated)
    memberships = defaultdict(set)
    for index, group in enumerate(groups):
        for vertex in {v for face in group.faces for v in data.faces[face]}:
            memberships[vertex].add(index)
    # Bands separated at a shared EDGE can be removed in sequence. Pure
    # vertex contacts remain ambiguous and retain their readiness warning.
    shared_edges=defaultdict(set)
    for index,group in enumerate(groups):
        for edge in {e for f in group.faces for e in data.face_edges[f]}:shared_edges[edge].add(index)
    connected={frozenset(ids) for ids in shared_edges.values() if len(ids)==2}
    touching={i for ids in memberships.values() if len(ids)>1
              for i in ids if any(frozenset((i,j)) not in connected for j in ids if j!=i)}
    groups = tuple(replace(g,ready=False,reason="Раздельные участки фаски касаются только в вершине.")
                   if i in touching else g for i,g in enumerate(groups))
    return Detection(groups)


def _targets(selection, seeded):
    meshes = defaultdict(set)
    items = om.MSelectionList()
    for name in selection:
        items.add(name)
    iterator = om.MItSelectionList(items)
    while not iterator.isDone():
        try:
            path, component = iterator.getComponent()
        except RuntimeError:
            iterator.next()
            continue
        if path.node().hasFn(om.MFn.kTransform):
            # All visible mesh children, rather than silently choosing one shape.
            dag = om.MFnDagNode(path)
            paths = []
            for index in range(dag.childCount()):
                child = dag.child(index)
                if child.hasFn(om.MFn.kMesh) and not om.MFnDagNode(child).isIntermediateObject:
                    shape = om.MDagPath(path); shape.push(child); paths.append(shape)
        else:
            paths = [path] if path.node().hasFn(om.MFn.kMesh) else []
        for path in paths:
            name = path.fullPathName()
            if seeded:
                if component.isNull() or not component.hasFn(om.MFn.kMeshPolygonComponent):
                    raise ValueError("Для расширения выберите полигоны фаски.")
                meshes[name].update(om.MFnSingleIndexedComponent(component).getElements())
            else:
                meshes[name]
        iterator.next()
    if not meshes:
        raise ValueError("Выберите меш или его полигоны.")
    return meshes


def _face_mask(groups):
    result=defaultdict(set)
    for group in groups:result[group.mesh].update(group.faces)
    return dict(result)


def _selected_face_mask():
    result=defaultdict(set);iterator=om.MItSelectionList(om.MGlobal.getActiveSelectionList())
    while not iterator.isDone():
        try:
            path,component=iterator.getComponent()
            if component.isNull() or not component.hasFn(om.MFn.kMeshPolygonComponent):return None
            if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
            result[path.fullPathName()].update(om.MFnSingleIndexedComponent(component).getElements())
        except RuntimeError:return None
        iterator.next()
    return dict(result)


def _face_components(mask):
    """Compact native ranges avoid reparsing a long name for every polygon."""
    result=[]
    for mesh,faces in mask.items():
        ordered=sorted(faces)
        if not ordered:continue
        start=end=ordered[0]
        for face in ordered[1:]+[None]:
            if face is not None and face==end+1:end=face;continue
            result.append(f'{mesh}.f[{start}]' if start==end else f'{mesh}.f[{start}:{end}]')
            start=end=face
    return result


def _selection_matches(mask, parents):
    if _selected_face_mask()!=mask:return False
    component_mode=cmds.selectMode(query=True,component=True)
    face_mode=cmds.selectType(query=True,polymeshFace=True)
    # Standalone has no UI mask state and returns None for selectType.
    if face_mode is not None and (not component_mode or not face_mode):return False
    hilite=om.MGlobal.getHiliteList()
    return {hilite.getDagPath(i).fullPathName() for i in range(hilite.length())}==set(parents)


def select_bevels(*, seeded=None, sensitivity="normal", include_corners=True, ready_only=False, _on_result=None):
    """Toolbox entry: analyze all targets before replacing selection in one undo."""
    try:
        # Finish the outgoing tool first: Bevel may still own a live preview.
        # Read selection afterwards so obsolete preview IDs never leak through.
        if cmds.currentCtx()!='selectSuperContext':cmds.setToolTo("selectSuperContext")
        selected=cmds.ls(selection=True,long=True) or []
        if seeded is None:seeded=bool(cmds.filterExpand(selected,selectionMask=34))
        targets = _targets(selected, seeded)
        results=[]
        with _SearchProgress(len(targets)) as progress:
            for mesh,seeds in targets.items():
                _advance(.02,'Подготовка поиска')
                results.append(detect(mesh,seeds=seeds if seeded else None,sensitivity=sensitivity,
                                      include_corners=include_corners))
                _advance(1,'Результат поиска готов')
                progress.done+=1
            groups = [group for result in results for group in result.groups]
            chosen = [g for g in groups if g.ready or not ready_only]
            mask=_face_mask(chosen)
            changed=False
            progress.phase(90,'Подготовка выделения')
            if mask:
                parents=[]
                for mesh in targets:
                    path=_mesh_fn(mesh).dagPath();path.pop();parents.append(path.fullPathName())
                # An unchanged repeat must not wake scene callbacks, rebuild
                # viewport selection or add another native undo command.
                progress.cancellable=False
                progress.phase(92,'Выделение полигонов')
                if not _selection_matches(mask,parents):
                    cmds.undoInfo(openChunk=True, chunkName="Find bevel faces")
                    try:
                        cmds.selectMode(component=True)
                        cmds.selectType(allComponents=False)
                        cmds.selectType(polymeshFace=True)
                        cmds.hilite(parents,replace=True)
                        cmds.select(_face_components(mask),replace=True)
                        changed=True
                    finally:cmds.undoInfo(closeChunk=True)
            progress.cancellable=False
            progress.phase(96,'Обновление списка фасок')
            if _on_result is not None:_on_result(tuple(groups))
            progress.phase(98,'Обновление вида')
            if changed and not cmds.about(batch=True):cmds.refresh(currentView=True)
            progress.phase(100,'Готово')
        if not mask:
            cmds.warning("Поиск фасок: подходящие фаски не найдены. Для широкой фаски выберите "
                         "её полигон и используйте поиск от выделения.")
            return tuple(groups)
        unready = sum(not g.ready for g in chosen)
        message = f"Поиск фасок: {len(chosen)} участков, {sum(map(len,mask.values()))} полигонов."
        if unready:
            message += f" Для {unready} участков текущий UnBevel не смог подтвердить восстановление."
        print(message)
        if unready:
            cmds.warning(message)
            for group in (g for g in chosen if not g.ready):
                print(f"{group.mesh}.f[{group.faces[0]}]: {group.reason}")
        return tuple(groups)
    except _Cancelled:
        print('Поиск фасок отменён.');return ()
    except (ValueError, RuntimeError) as error:
        cmds.warning("Поиск фасок: " + str(error))
        return ()
