"""Project and pick bevel markers on the displayed mesh."""
from collections import defaultdict
from dataclasses import dataclass
import math

from maya import cmds
from maya.api import OpenMaya as om, OpenMayaUI as omui
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid

from model_pack_linear_shift.overlay import Canvas, Overlay, Projection
from . import history


@dataclass(frozen=True)
class BakedBand:
    mesh: str
    faces: tuple[int, ...]
    joined: bool = False


def _faces(fn):
    counts, vertices = fn.getVertices()
    start = 0
    for count in counts:
        yield vertices[start:start + count]
        start += count


def _key(points, face):
    return frozenset(tuple(points[v])[:3] for v in face)


def visible_faces(fn, routes):
    """Find the saved regions in the actual output, including upstream bevels."""
    direct = history._input(fn.fullPathName() + '.inMesh').rsplit('.', 1)[0]
    owners = {}
    earlier = defaultdict(set)
    samples = defaultdict(list)
    for node, bands in routes.items():
        labels = history._face_owners(node, bands)
        if node == direct:
            owners.update(labels)
            continue
        source = history._mesh(node + '.outputMesh')
        points = source.getPoints()
        for face, band in labels.items():
            ring = source.getPolygonVertices(face)
            key = _key(points, ring)
            earlier[key].add(band)
            center = om.MPoint(*(sum(points[v][i] for v in ring) / len(ring) for i in range(3)))
            samples[key].append((band, center, source.getPolygonNormal(face)))
    if not earlier:
        return owners

    points = fn.getPoints()
    found = set()
    for face, ids in enumerate(_faces(fn)):
        if face not in owners:
            key = _key(points, ids)
            bands = earlier.get(key)
            if bands and len(bands) == 1:
                owners[face] = next(iter(bands))
                found.add(key)

    # A following bevel can trim an existing face. Associate any remaining
    # coplanar polygon at its surface and keep only the currently drawn edges.
    box = om.MFnDagNode(fn.dagPath()).boundingBox
    tolerance = max(box.width, box.height, box.depth, 1e-5) * 2e-5
    for key in earlier.keys() - found:
        for band, center, normal in samples[key]:
            hit = history._ray_hit(fn, center + normal * (4 * tolerance), -normal)
            if hit and hit[2] not in owners and (om.MPoint(hit[0]) - center).length() <= tolerance:
                if abs(fn.getPolygonNormal(hit[2]) * normal) > .999:
                    owners[hit[2]] = band
    return owners


def band_edges(fn, routes):
    """Use both rims of each region, omitting its internal segment edges."""
    return _outline_edges(fn, visible_faces(fn, routes))


def _outline_edges(fn, owners):
    edges = defaultdict(set)
    for face, band in owners.items():
        ring = list(fn.getPolygonVertices(face))
        for a, b in zip(ring, ring[1:] + ring[:1]):
            edge = (min(a, b), max(a, b))
            if edge in edges[band]:
                edges[band].remove(edge)
            else:
                edges[band].add(edge)
    return {band: sorted(members) for band, members in edges.items() if members}


def _layout_key(fn, routes):
    nodes = []
    for node, choices in routes.items():
        obj = om.MSelectionList().add(node).getDependNode(0)
        weld = getattr(om.MFnDependencyNode(obj).userNode(), '_welded_faces', None)
        nodes.append((om.MObjectHandle(obj).hashCode(), tuple(choices.items()),
                      tuple(map(tuple, weld[2])) if weld else ()))
    return (om.MObjectHandle(fn.object()).hashCode(), fn.numVertices, fn.numEdges, fn.numPolygons, tuple(nodes))


def _surface_middle(triangles):
    weighted = om.MVector()
    area_sum = 0.
    for a, b, c in triangles:
        area = ((b - a) ^ (c - a)).length()
        weighted += (om.MVector(a) + om.MVector(b) + om.MVector(c)) * (area / 3.)
        area_sum += area
    if area_sum <= 0.:
        return None
    middle = om.MPoint(weighted / area_sum)
    best, distance = None, math.inf
    # The centroid can fall off a curved or concave strip. Search only
    # triangles belonging to this bevel, in world space.
    for a, b, c in triangles:
        ab, ac, delta = b - a, c - a, middle - a
        uu, uv, vv = ab * ab, ab * ac, ac * ac
        divisor = uu * vv - uv * uv
        candidates = []
        if divisor > 1e-20 * uu * vv:
            du, dv = delta * ab, delta * ac
            u, v = (du * vv - dv * uv) / divisor, (dv * uu - du * uv) / divisor
            if u >= 0. and v >= 0. and u + v <= 1.:
                candidates.append(a + ab * u + ac * v)
        if not candidates:
            for p, q in ((a, b), (b, c), (c, a)):
                edge = q - p
                squared = edge * edge
                t = max(0., min(1., ((middle - p) * edge) / squared)) if squared else 0.
                candidates.append(p + edge * t)
        for point in candidates:
            d = (point - middle).length()
            if d < distance:
                best, distance = point, d
    return best


class _Outline:
    def __init__(self, fn, edges, active, owners=None):
        path = fn.dagPath()
        self.shape = path.fullPathName()
        self.handle = om.MObjectHandle(fn.object())
        self.topology = (fn.numVertices, fn.numEdges, fn.numPolygons)
        self.edges = edges
        self.active = active
        # Capture one geometry snapshot. Draw callbacks do not query the DG.
        points = fn.getPoints()
        vertices = {v for members in edges.values() for edge in members for v in edge}
        self.points = {v: om.MPoint(points[v]) for v in vertices}
        self.matrix = path.inclusiveMatrix()
        self.inverse = self.matrix.inverse()
        self.world = {v: point * self.matrix for v, point in self.points.items()}
        triangles = defaultdict(list)
        counts, vertices = fn.getTriangles()
        start = 0
        for face, count in enumerate(counts):
            band = (owners or {}).get(face)
            end = start + 3 * count
            if band in edges:
                for i in range(start, end, 3):
                    triangles[band].append(tuple(points[vertices[j]] * self.matrix for j in range(i, i + 3)))
            start = end
        self.centers = {}
        for band, members in edges.items():
            center = _surface_middle(triangles[band])
            if center is None:
                a, b = members[0]
                center = self.world[a] + (self.world[b] - self.world[a]) * .5
            self.centers[band] = center
        edge_faces = {pair: [] for members in edges.values() for pair in members}
        normals = {}
        for face, ring in enumerate(_faces(fn)):
            ring = list(ring)
            for a, b in zip(ring, ring[1:] + ring[:1]):
                pair = (min(a, b), max(a, b))
                if pair in edge_faces:
                    if face not in normals:
                        normals[face] = fn.getPolygonNormal(face, om.MSpace.kWorld)
                    edge_faces[pair].append(normals[face])
        self.edge_normals = edge_faces
        box = om.MFnDagNode(path).boundingBox
        self.tolerance = max(box.width, box.height, box.depth, 1e-5) * 3e-5

    def live_mesh(self):
        # Preview rebuilds delete shapes and can reuse the same DAG name.
        # Verify the original node before resolving a fresh path.
        if not self.handle.isValid() or not self.handle.isAlive():
            return None
        if not cmds.ls(self.shape, long=True, visible=True):
            return None
        fn = history._mesh_fn(self.shape)
        if fn.object() != self.handle.object() or (fn.numVertices, fn.numEdges, fn.numPolygons) != self.topology:
            return None
        return fn

    def project(self, fn, projection, camera):
        ray = fn.autoUniformGridParams()
        eye = camera.eyePoint(om.MSpace.kWorld) * self.inverse
        direction = (camera.viewDirection(om.MSpace.kWorld) * self.inverse).normal() if camera.isOrtho() else None
        result = []
        for band, edges in self.edges.items():
            segments = []
            for a, b in edges:
                segment = projection.line(self.world[a], self.world[b])
                if segment is None:
                    continue
                # Sample visibility along long contours so another part of
                # the workpiece can partially hide them when orbiting.
                parts = min(12, max(1, math.ceil(math.dist(*segment) / 32)))
                first, delta = self.points[a], self.points[b] - self.points[a]
                for index in range(parts):
                    point = first + delta * ((index + .5) / parts)
                    vector = point - eye
                    distance = vector.length() if direction is None else vector * direction
                    if distance <= self.tolerance:
                        continue
                    angle = direction if direction is not None else vector / distance
                    origin = point - angle * distance if direction is not None else eye
                    hit = fn.anyIntersection(om.MFloatPoint(origin), om.MFloatVector(angle), om.MSpace.kObject,
                                             distance - self.tolerance, False, accelParams=ray)
                    if hit is None:
                        start = (first + delta * (index / parts)) * self.matrix
                        end = (first + delta * ((index + 1) / parts)) * self.matrix
                        clipped = projection.line(start, end) if parts > 1 else segment
                        if clipped is not None:
                            segments.append(clipped)
            selected = band in self.active if isinstance(self.active, tuple) else band == self.active
            result.append((selected, band, segments))
        return result

    def project_motion(self, projection, camera):
        """Project the cached outline during a render callback without a DG query."""
        toward_eye = -camera.viewDirection(om.MSpace.kWorld) if camera.isOrtho() else None
        eye = camera.eyePoint(om.MSpace.kWorld) if toward_eye is None else None
        result = []
        for band, members in self.edges.items():
            segments = []
            for a, b in members:
                side = self.edge_normals[a, b]
                facing = toward_eye if eye is None else eye - self.world[a]
                if len(side) == 2 and all(normal * facing < 0. for normal in side):
                    continue
                line = projection.line(self.world[a], self.world[b])
                if line is not None:
                    segments.append(line)
            selected = band in self.active if isinstance(self.active, tuple) else band == self.active
            result.append((selected, band, segments))
        return result


class BevelCanvas(Canvas):
    def __init__(self, parent, manager):
        super().__init__(parent, manager)
        self.setObjectName('modelPackBevelOverlay')

    def paint_frame(self, painter):
        painter.save()
        try:
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
            painter.fillRect(self.rect(), QtCore.Qt.transparent)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            origin = self.mapToGlobal(QtCore.QPoint())
            region = QtGui.QRegion(self.rect())
            for window in QtWidgets.QApplication.topLevelWidgets():
                if window in (self, self.viewport.window()) or not window.isVisible() or window.isMinimized():
                    continue
                if not window.windowFlags() & QtCore.Qt.WindowTransparentForInput:
                    region -= QtGui.QRegion(window.frameGeometry().translated(-origin))
            painter.setClipRegion(region)
            if self.manager.selection_box is not None:
                start, end = self.manager.selection_box
                rect = QtCore.QRectF(QtCore.QPointF(start[0], self.height() - 1 - start[1]),
                                     QtCore.QPointF(end[0], self.height() - 1 - end[1])).normalized()
                painter.setPen(QtGui.QPen(QtGui.QColor('#eeeeee'), 1, QtCore.Qt.DashLine))
                painter.setBrush(QtGui.QColor(255, 255, 255, 20))
                painter.drawRect(rect)
            for _, active, (x, y) in self.manager.markers(self.width(), self.height()):
                center = QtCore.QPointF(x, self.height() - 1 - y)
                painter.setPen(QtGui.QPen(QtGui.QColor(14, 19, 23, 215), 4))
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.drawEllipse(center, 6, 6)
                painter.setPen(QtGui.QPen(QtGui.QColor('#83ef79' if active else '#eeeeee'), 2))
                if active:
                    painter.setBrush(QtGui.QColor('#53d767'))
                painter.drawEllipse(center, 6, 6)
        except (RuntimeError, ValueError):
            pass
        finally:
            painter.restore()


class BevelOverlay(Overlay):
    canvas_class = BevelCanvas

    def __init__(self):
        super().__init__()
        self.outlines = []
        self.cache = {}
        self.frame = ()
        self.selection_box = None
        self._marker_positions = {}
        self._view_key = None
        self._frame_key = None
        self._queued = False
        self._closed = False
        self._settle = QtCore.QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(110)
        self._settle.timeout.connect(self._queue_projection)

    def rendered(self, panel, *_):
        canvas = self.canvas
        if (self._closed or self.painting or panel != self.panel or canvas is None
                or not isValid(canvas) or not canvas.isVisible()):
            return
        try:
            view = omui.M3dView.getM3dViewFromModelPanel(panel)
            if int(view.widget()) != self.pointer:
                return
            self.view = view
            self.painting = True
            projection, outlines, key = self._projection(canvas.size())
            if key != self._frame_key:
                self._frame_key = key
                self._view_key = None
                self.frame = ()
                if outlines:
                    camera = om.MFnCamera(view.getCamera())
                    self.frame = tuple(sorted((item for outline in outlines
                                               for item in outline.project_motion(projection, camera)),
                                              key=lambda item: item[0]))
                self._project_marker_centers(projection, outlines)
                canvas.repaint()
                self._settle.start()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            self._frame_key = self._view_key = None
        finally:
            self.painting = False

    def tick(self):
        canvas = self.canvas
        size = canvas.size() if canvas is not None and isValid(canvas) else None
        visible = canvas is not None and isValid(canvas) and canvas.isVisible()
        super().tick()
        if self.canvas is not None and self.canvas.isVisible():
            if (not visible or self.canvas is not canvas or self.canvas.size() != size
                    or self._view_key is None and not self._settle.isActive()):
                self._queue_projection()

    def _queue_projection(self):
        if not self._closed and not self._queued:
            self._queued = True
            # Qt can deliver a paint event inside Maya's refresh, including
            # after the preview was replaced. Resolve scene data at idle only.
            cmds.evalDeferred(self._update_projection)

    def _update_projection(self):
        self._queued = False
        canvas = self.canvas
        if (self._closed or self._settle.isActive() or canvas is None or not isValid(canvas) or not isValid(canvas.viewport)
                or not canvas.isVisible()):
            return
        try:
            frame = self.projected(canvas.size())
        except (RuntimeError, TypeError, ValueError):
            self._view_key = None
            frame = ()
        if frame is not self.frame and isValid(canvas):
            self.frame = frame
            canvas.update()

    def sync(self, choices, session):
        active = getattr(getattr(session, 'history_plan', None), 'selected_bands', ())
        active = getattr(session, '_colored_baked_band', active)
        session_mesh = history._shape(session.mesh).rsplit('|', 1)[0] if session else None
        by_mesh = defaultdict(list)
        for _, band in choices:
            by_mesh[band.mesh].append(band)
        self.outlines = []
        cache = {}
        for mesh, bands in by_mesh.items():
            try:
                display = (session._display_mesh or mesh) if session_mesh == mesh else mesh
                fn = history._mesh_fn(display)
                plan = getattr(session, 'history_plan', None)
                baked = [band for band in bands if isinstance(band, BakedBand)]
                routes = plan.routes(session) if plan and session_mesh == mesh else history._routes(
                    band for band in bands if not isinstance(band, BakedBand))
                key = _layout_key(fn, routes)
                if baked:
                    if (session_mesh == mesh and getattr(session, 'recovered', False)
                            and session.after_rows != session.rows):
                        original = om.MFnMesh(session.before)
                        chosen = active if isinstance(active, tuple) else (active,)
                        original_vertices = {band: {v for f in band.faces for v in original.getPolygonVertices(f)}
                                             for band in chosen if band in baked}
                        owners = {v: (index, band) for index, (old, new) in enumerate(zip(session.rows, session.after_rows))
                                  for band, vertices in original_vertices.items() if set(old) <= vertices for v in new}
                        current_faces = {}
                        for face, ring in enumerate(_faces(fn)):
                            if not all(v in owners for v in ring):
                                continue
                            rows = {owners[v][0] for v in ring}
                            face_bands = {owners[v][1] for v in ring}
                            if len(rows) == 2 and len(face_bands) == 1:
                                current_faces[face] = face_bands.pop()
                        key += (tuple(baked), active, tuple(current_faces.items()))
                    else:
                        current_faces = None
                        key += (tuple(baked),)
                layout = self.cache.get(key)
                if layout is None:
                    owners = visible_faces(fn, routes)
                    if baked:
                        owners.update({f: band for band in baked for f in band.faces}
                                      if current_faces is None else current_faces)
                    layout = _outline_edges(fn, owners), owners
                edges, owners = layout
                cache[key] = layout
                self.outlines.append(_Outline(fn, {band: members for band, members in edges.items() if band in bands},
                                              active, owners))
            except (ValueError, RuntimeError):
                continue
        self.cache = cache
        self._frame_key = self._view_key = None
        self.tick()
        self._queue_projection()

    def _project_marker_centers(self, projection, outlines):
        self._marker_positions = {band: projection.point(point) for outline in outlines
                                  for band, point in outline.centers.items()}

    def markers(self, width, height):
        markers = []
        for active, band, segments in self.frame:
            if not segments or band not in self._marker_positions:
                continue
            x, y, visible = self._marker_positions[band]
            if visible and 0 <= x <= width and 0 <= y <= height:
                markers.append((band, active, (x, y)))
        return markers

    def pick_marker(self, anchor):
        canvas = self.canvas
        if canvas is None or not isValid(canvas) or not canvas.isVisible():
            return None
        hits = [(math.dist(anchor, point), band) for band, _, point in self.markers(canvas.width(), canvas.height())]
        if hits:
            distance, band = min(hits, key=lambda hit: hit[0])
            if distance <= 9.:
                return band
        return None

    def draw_selection_box(self, start=None, end=None):
        self.selection_box = (start, end) if start is not None else None
        if self.canvas is not None and isValid(self.canvas):
            self.canvas.update()

    def markers_in_box(self, start, end):
        canvas = self.canvas
        if canvas is None or not isValid(canvas) or not canvas.isVisible():
            return ()
        left, right = sorted((start[0], end[0]))
        bottom, top = sorted((start[1], end[1]))
        return tuple(band for band, _, (x, y) in self.markers(canvas.width(), canvas.height())
                     if left <= x <= right and bottom <= y <= top)

    def _projection(self, size):
        view = self.view
        projection = Projection(view, size.width(), size.height())
        if not view.objectDisplay() & view.kDisplayMeshes:
            return projection, (), ()
        filtered = None
        if view.viewIsFiltered():
            selection = view.filteredObjectList()
            paths = []
            for index in range(selection.length()):
                try:
                    paths.append(selection.getDagPath(index).fullPathName())
                except (RuntimeError, TypeError):
                    continue
            filtered = tuple(paths)
        outlines = [outline for outline in self.outlines if outline.handle.isAlive() and (
            filtered is None or any(outline.shape == path or outline.shape.startswith(path + '|') for path in filtered))]
        matrix = tuple(projection.matrix) if projection.matrix is not None else None
        key = (matrix, size.width(), size.height(), tuple(o.shape for o in outlines))
        return projection, outlines, key

    def projected(self, size):
        projection, outlines, key = self._projection(size)
        self._frame_key = key
        if self._view_key != key:
            self._view_key = key
            self._project_marker_centers(projection, outlines)
            if not outlines:
                return ()
            camera = om.MFnCamera(self.view.getCamera())
            return tuple(sorted((item for outline in outlines if (fn := outline.live_mesh()) is not None
                                 for item in outline.project(fn, projection, camera)), key=lambda item: item[0]))
        return self.frame

    def close(self):
        self._closed = True
        self.timer.stop()
        self._settle.stop()
        self.clear_canvas()
        self.frame = ()
        self.outlines.clear()
        self.cache.clear()
        self.deleteLater()
