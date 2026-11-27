"""Find nonplanar LP faces and triangulate them against HP reference surfaces."""
from math import dist, hypot
from maya import cmds
from maya.api import OpenMaya as om
from PySide6 import QtCore, QtWidgets

from model_pack_bevel.session import without_undo


TOOL_KIND = "nonplanar_hp"
THRESHOLD_SCALE = 10000


def selected_meshes():
    items = cmds.ls(selection=True, objectsOnly=True, long=True) or []
    shapes = set()
    for item in items:
        if cmds.nodeType(item) == "mesh":
            shapes.add(item)
        else:
            shapes.update(cmds.listRelatives(item, shapes=True, noIntermediate=True,
                                            fullPath=True, type="mesh") or [])
    if not shapes:
        raise ValueError("Выберите меш.")
    selection = om.MSelectionList()
    for shape in sorted(shapes):
        selection.add(shape)
    return [selection.getDagPath(i) for i in range(selection.length())]


def plane(points):
    # Center before taking cross products to avoid cancellation far from origin.
    local = [p - points[0] for p in points]
    normal = om.MVector()
    for i, point in enumerate(local):
        normal += point ^ local[(i + 1) % len(local)]
    return local, normal.normal()


def scan(path):
    points = om.MFnMesh(path).getPoints(om.MSpace.kWorld)
    it = om.MItMeshPolygon(path)
    scores = {}
    while not it.isDone():
        if it.polygonVertexCount() > 3:
            local, normal = plane([points[i] for i in it.getVertices()])
            heights = [p * normal for p in local]
            center = sum(heights) / len(heights)
            spans = [max(p[k] for p in local) - min(p[k] for p in local)
                     for k in range(3)]
            size = hypot(*spans)
            if size:
                score = 100 * max(abs(h - center) for h in heights) / size
                if score > 1e-10:
                    scores[it.index()] = score
        it.next()
    return scores


def world_surface(path):
    # Bake world positions into private mesh data for Euclidean distances
    # under nonuniform scale and shear. Keep the data alive during queries.
    holder = om.MFnMeshData().create()
    copy = om.MFnMesh()
    geometry = copy.copy(path.node(), holder)
    om.MFnMesh(geometry).setPoints(om.MFnMesh(path).getPoints(om.MSpace.kWorld))
    surface = om.MMeshIntersector()
    surface.create(geometry, om.MMatrix())
    return holder, surface


def diagonals(ids, points, surfaces):
    local, normal = plane([points[i] for i in ids])
    axis = max(range(3), key=lambda i: abs(normal[i]))
    if not normal.length():
        return None
    x, y = (axis + 1) % 3, (axis + 2) % 3
    xy = {i: (p[x], p[y]) for i, p in zip(ids, local)}
    orientation = 1 if normal[axis] > 0 else -1
    span = max(max(v[k] for v in xy.values()) - min(v[k] for v in xy.values())
               for k in (0, 1))
    eps = span * span * 1e-12

    def turn(i, j, k):
        a, b, c = xy[i], xy[j], xy[k]
        return orientation * ((b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0]))

    ring = list(ids)
    result = []
    while len(ring) > 3:
        candidates = []
        for index, b in enumerate(ring):
            a, c = ring[index-1], ring[(index+1) % len(ring)]
            if turn(a, b, c) <= eps:
                continue
            if any(min(turn(a,b,v), turn(b,c,v), turn(c,a,v)) >= -eps
                   for v in ring if v not in (a,b,c)):
                continue
            midpoint = points[a] + (points[c] - points[a]) * 0.5
            hits = (surface.getClosestPoint(midpoint).point for surface in surfaces)
            cost = min(dist((midpoint.x, midpoint.y, midpoint.z), (hit.x, hit.y, hit.z)) for hit in hits)
            candidates.append((cost, min(a,c), max(a,c), index))
        if not candidates:
            return None
        _, a, c, index = min(candidates)
        result.append((a,c))
        ring.pop(index)
    return result if turn(*ring) > eps else None


def apply(lp, hp, faces):
    if any(lp.node() == p.node() for p in hp):
        raise ValueError("Назначьте разные меши для LP и HP.")
    mesh = om.MFnMesh(lp)
    points = mesh.getPoints(om.MSpace.kWorld)
    references = [world_surface(p) for p in hp]
    surfaces = [surface for holder, surface in references]
    it = om.MItMeshPolygon(lp)
    vertex = om.MItMeshVertex(lp)
    cuts = []
    skipped = 0
    for face in faces:
        it.setIndex(face)
        ids = list(it.getVertices())
        plan = None
        if not it.isHoled() and len(ids) == len(set(ids)) and it.hasValidTriangulation():
            plan = diagonals(ids, points, surfaces)
        if plan:
            # The command takes vertex pairs. Reject ambiguous shared faces.
            for a, b in plan:
                vertex.setIndex(a)
                adjacent = set(vertex.getConnectedFaces())
                vertex.setIndex(b)
                if adjacent.intersection(vertex.getConnectedFaces()) != {face}:
                    plan = None
                    break
        if plan:
            cuts.extend(plan)
        else:
            skipped += 1

    name = lp.fullPathName()
    if cuts:
        if not cmds.undoInfo(query=True, state=True):
            raise ValueError("Для отмены триангуляции включите Undo в настройках Maya.")
        cmds.undoInfo(openChunk=True, chunkName="Nonplanar HP")
        completed = 0
        try:
            for a,b in cuts:
                cmds.polyConnectComponents(name + ".vtx[%d]" % a, name + ".vtx[%d]" % b,
                                           constructionHistory=False, insertWithEdgeFlow=False)
                completed += 1
        except Exception:
            cmds.undoInfo(closeChunk=True)
            if completed:
                cmds.undo()
            raise
        else:
            cmds.undoInfo(closeChunk=True)
    return len(cuts), skipped


class Panel(QtWidgets.QGroupBox):
    def __init__(self, toolbox):
        super().__init__("Nonplanar HP", toolbox)
        self.setObjectName("modelPackNonplanarHP")
        self.lp = None
        self.hp = []
        self.scores = {}
        self.bindings = {}

        layout = QtWidgets.QGridLayout(self)
        for row, (role, help_text) in enumerate((
            ("LP", "Выделите один меш, в котором нужно найти неплоские грани."),
            ("HP", "Выделите один или несколько опорных мешей для выбора диагоналей."),
        )):
            button = QtWidgets.QPushButton("Set " + role, self)
            button.setToolTip(help_text)
            button.clicked.connect(lambda checked=False, role=role: self.run(self.bind, role))
            binding = QtWidgets.QLabel("—", self)
            binding.setTextFormat(QtCore.Qt.PlainText)
            binding.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            layout.addWidget(button, row, 0)
            layout.addWidget(binding, row, 1, 1, 2)
            self.bindings[role] = binding

        self.threshold = QtWidgets.QDoubleSpinBox(self)
        self.threshold.setDecimals(4)
        self.threshold.setRange(0, 100)
        self.threshold.setSingleStep(0.01)
        self.threshold.setSuffix(" %")
        self.threshold.setValue(0.1)
        self.threshold.setKeyboardTracking(False)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal, self)
        self.slider.setRange(0, 10 * THRESHOLD_SCALE)
        self.slider.setValue(round(self.threshold.value() * THRESHOLD_SCALE))
        self.slider.valueChanged.connect(self.slider_changed)
        self.threshold.valueChanged.connect(self.threshold_changed)
        label = QtWidgets.QLabel("Threshold", self)
        label.setBuddy(self.threshold)
        hint = "Допустимое отклонение от плоскости в процентах от размера грани. Превышающие порог грани выделяются в Maya."
        self.threshold.setToolTip(hint)
        self.slider.setToolTip(hint)
        layout.addWidget(label, 2, 0)
        layout.addWidget(self.slider, 2, 1)
        layout.addWidget(self.threshold, 2, 2)

        self.status = QtWidgets.QLabel("Выделите меш и нажмите Set LP.", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status, 3, 0, 1, 3)
        self.apply_button = QtWidgets.QPushButton("Triangulate by HP", self)
        self.apply_button.setProperty("primary", True)
        self.apply_button.setToolTip("Разбить найденные грани LP диагоналями, ближайшими к поверхности HP. Ctrl+Z — отменить.")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(lambda: self.run(self.triangulate))
        layout.addWidget(self.apply_button, 4, 0, 1, 3)
        layout.setColumnStretch(1, 1)

    def run(self, callback, *args):
        try:
            callback(*args)
        except (RuntimeError, ValueError) as error:
            cmds.warning(str(error))
            self.status.setText(str(error))

    def bind(self, role):
        paths = selected_meshes()
        if role == "LP" and len(paths) != 1:
            raise ValueError("Выберите один меш для LP.")
        lp = paths[0] if role == "LP" else self.lp
        hp = self.hp if role == "LP" else paths
        if lp and lp.isValid() and any(path.isValid() and path.node() == lp.node() for path in hp):
            raise ValueError("Назначьте разные меши для LP и HP.")
        self.lp, self.hp = lp, hp
        names = [path.partialPathName() for path in paths]
        self.bindings[role].setText(names[0] if len(names) == 1 else f"Мешей: {len(names)}")
        self.bindings[role].setToolTip("\n".join(names))
        if self.lp and self.lp.isValid():
            self.refresh()
        else:
            self.status.setText("Выделите меш и нажмите Set LP.")

    def slider_changed(self, value):
        self.threshold.setValue(value / THRESHOLD_SCALE)

    def threshold_changed(self, value):
        with QtCore.QSignalBlocker(self.slider):
            self.slider.setValue(round(value * THRESHOLD_SCALE))
        if self.lp is not None:
            self.run(self.preview)

    def require_lp(self):
        if not self.lp or not self.lp.isValid():
            raise ValueError("Назначьте LP.")

    def matches(self):
        threshold = self.threshold.value()
        return [face for face, score in self.scores.items() if score > threshold]

    def refresh(self):
        self.require_lp()
        self.scores = scan(self.lp)
        self.preview()

    def preview(self):
        self.require_lp()
        faces = self.matches()
        selection = om.MSelectionList()
        if faces:
            component = om.MFnSingleIndexedComponent()
            obj = component.create(om.MFn.kMeshPolygonComponent)
            component.addElements(faces)
            selection.add((self.lp, obj))
        with without_undo():
            cmds.hilite(self.lp.fullPathName().rsplit("|", 1)[0], replace=True)
            om.MGlobal.setActiveSelectionList(selection)
        self.show_matches(faces, "Выделено")

    def rescan_after_undo(self):
        if self.lp and self.lp.isValid():
            self.scores = scan(self.lp)
            self.show_matches(self.matches(), "Найдено")

    def show_matches(self, faces, action):
        has_hp = bool(self.hp) and all(path.isValid() for path in self.hp)
        self.apply_button.setEnabled(has_hp and bool(faces))
        message = f"{action} граней: {len(faces)}"
        if not has_hp:
            message += " · Назначьте HP."
        self.status.setText(message)

    def triangulate(self):
        self.require_lp()
        if not self.hp or not all(path.isValid() for path in self.hp):
            raise ValueError("Назначьте HP.")
        self.scores = scan(self.lp)
        edges, skipped = apply(self.lp, self.hp, self.matches())
        self.refresh()
        self.status.setText(f"Добавлено диагоналей: {edges} · Пропущено граней: {skipped}")


def toggle(toolbox):
    if getattr(toolbox, "_nonplanar_hp_panel", None) is not None:
        close(toolbox)
        return
    toolbox._prepare_tool(TOOL_KIND)
    panel = Panel(toolbox)
    toolbox._nonplanar_hp_panel = panel
    toolbox.layout().addWidget(panel, toolbox._panel_row, 0, 1, 2)
    toolbox.nonplanar_hp_button.setChecked(True)
    toolbox.layout().activate()
    toolbox.resize(toolbox.width(), toolbox.sizeHint().height())


def close(toolbox):
    panel = getattr(toolbox, "_nonplanar_hp_panel", None)
    if panel is None:
        return
    toolbox._nonplanar_hp_panel = None
    panel.hide()
    toolbox.layout().removeWidget(panel)
    panel.deleteLater()
    toolbox.nonplanar_hp_button.setChecked(False)
    if toolbox._active_tool == TOOL_KIND:
        toolbox._active_tool = None
    toolbox.layout().activate()
    toolbox.resize(toolbox.width(), toolbox.sizeHint().height())


