"""Mesh-centric editor for persistent Control Curve bindings."""
import heapq
import json
import math

from maya import cmds
from maya.api import OpenMaya as om
from PySide6 import QtCore, QtWidgets

from model_pack_surface_fit_ui import Ramp
from recurve_controls import ControlCurves, DEFAULT_FALLOFF, falloff_weight, lateral_binding, parameters, relative_matrix
from recurve_saved_curves import NODE_TYPE, ensure_plugin, topology


def selected_mesh():
    selection = om.MGlobal.getActiveSelectionList()
    if selection.length() != 1:
        raise ValueError('Select one mesh or an edge selection on one mesh.')
    path, component = selection.getComponent(0)
    if path.node().hasFn(om.MFn.kTransform):
        shapes = cmds.listRelatives(path.fullPathName(), shapes=True, noIntermediate=True,
                                   type='mesh', fullPath=True) or []
        if len(shapes) != 1:
            raise ValueError('Select an object with one visible mesh shape.')
        path = om.MSelectionList().add(shapes[0]).getDagPath(0)
    if not path.node().hasFn(om.MFn.kMesh):
        raise ValueError('Select one polygon mesh.')
    return path, component


def percent_binding(data, row, other_rows, percent, matrix, bridge=None):
    """Normalize each vertex against the next guide or open boundary."""
    if percent <= 0:
        return []
    fn = om.MFnMesh(data)
    points = [point * matrix for point in fn.getPoints()]
    seeds = set(row)
    barriers = {vertex for other in other_rows for vertex in other} - seeds
    iterator = om.MItMeshVertex(data)
    graph = {}
    boundary = set()
    while not iterator.isDone():
        vertex = iterator.index()
        graph[vertex] = [(other, (points[other] - points[vertex]).length())
                         for other in iterator.getConnectedVertices()]
        if vertex not in seeds and iterator.onBoundary():
            boundary.add(vertex)
        iterator.next()

    def distances(starts, stops):
        best = {vertex: (0., vertex) for vertex in starts}
        queue = [(0., vertex, vertex) for vertex in starts]
        heapq.heapify(queue)
        while queue:
            distance, origin, vertex = heapq.heappop(queue)
            if best[vertex] != (distance, origin) or vertex in stops:
                continue
            for neighbor, length in graph[vertex]:
                candidate = distance + length, origin
                if neighbor not in starts and candidate < best.get(neighbor, (math.inf, origin)):
                    best[neighbor] = candidate
                    heapq.heappush(queue, (*candidate, neighbor))
        return best

    outward = distances(seeds, barriers)
    # Open end edges can be closer than the opposing guide even inside a
    # strip. Prefer the opposing guide in regions separated by the guides.
    guide_distance = distances(barriers, seeds)
    edge_distance = distances(boundary, seeds | barriers)
    corridor = set()
    remaining = set(outward) - seeds - barriers
    while remaining:
        start = remaining.pop()
        region = {start}
        todo = [start]
        touches_guide = False
        while todo:
            vertex = todo.pop()
            for neighbor, _ in graph[vertex]:
                if neighbor in barriers:
                    touches_guide = True
                elif neighbor in remaining:
                    remaining.remove(neighbor)
                    region.add(neighbor)
                    todo.append(neighbor)
        if touches_guide:
            corridor.update(region)
    fallback = max((distance for distance, _ in outward.values()), default=0.)
    result = []
    for vertex, (distance, seed) in outward.items():
        if vertex in seeds or vertex in barriers:
            continue
        inward = guide_distance if vertex in corridor else edge_distance
        span = distance + inward[vertex][0] if vertex in inward else fallback
        limit = span * percent / 100.
        if distance < limit:
            result.append((vertex, seed, distance / limit))
            if bridge is not None and percent == 100 and vertex in corridor and vertex in guide_distance:
                bridge.append((vertex, guide_distance[vertex][1]))
    return result


class Editor(QtWidgets.QWidget):
    def __init__(self, window, engine, view):
        super().__init__(window)
        path, _ = selected_mesh()
        self.mesh = om.MObjectHandle(path.node())
        self.window = window
        self.engine = engine
        self.view = view
        self.active = None
        self.closed = False
        self.updating = False
        self.graph_chunk = False
        self.previous_soft_selection = None
        self.original_selection = om.MGlobal.getActiveSelectionList().getSelectionStrings()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        self.curves = QtWidgets.QListWidget()
        self.curves.setMaximumHeight(110)
        self.curves.setViewMode(QtWidgets.QListView.IconMode)
        self.curves.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        self.curves.setSpacing(0)
        self.curves.setUniformItemSizes(True)
        self.curves.setTextElideMode(QtCore.Qt.ElideRight)
        self.curves.setFlow(QtWidgets.QListView.LeftToRight)
        self.curves.setWrapping(True)
        self.curves.setSpacing(0)
        self.curves.setResizeMode(QtWidgets.QListView.Adjust)
        self.curves.setMovement(QtWidgets.QListView.Static)
        self.curves.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.curves.itemSelectionChanged.connect(self.choose)
        self.curves.itemClicked.connect(self.choose)
        self.curves.itemDoubleClicked.connect(self.rename_curve)
        self.drag_x = None
        QtWidgets.QApplication.instance().installEventFilter(self)
        layout.addWidget(self.curves)
        add = QtWidgets.QPushButton('Add Curve from Edges')
        add.clicked.connect(self.add_curves)
        layout.addWidget(add)
        self.rebind = QtWidgets.QPushButton('Rebind to Selected Edges')
        self.rebind.clicked.connect(self.rebind_curve)
        self.rebind.hide()
        layout.addWidget(self.rebind)
        settings_grid = QtWidgets.QGridLayout()
        settings_grid.setContentsMargins(0, 0, 0, 0)
        settings_grid.setHorizontalSpacing(6)
        settings_grid.setColumnStretch(2, 1)
        self.soft_label = QtWidgets.QLabel('CV Soft Select')
        self.soft_label.setFixedWidth(96)

        self.soft_mode = QtWidgets.QComboBox()
        self.soft_mode.addItems(['Use Maya', 'Volume', 'Surface'])

        self.soft_radius = QtWidgets.QDoubleSpinBox()
        self.soft_radius.setRange(.001, 1e6)
        self.soft_radius.setValue(cmds.softSelect(query=True, softSelectDistance=True))
        self.soft_radius.setEnabled(False)

        self.soft_label.setBuddy(self.soft_radius)
        self.soft_radius.setKeyboardTracking(False)
        self.soft_radius.setSingleStep(.1)
        self.soft_mode.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.soft_mode.setMinimumContentsLength(7)
        self.soft_mode.setMinimumWidth(76)
        self.soft_mode.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.soft_radius.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)

        self.soft_mode.currentIndexChanged.connect(self.change_soft_selection)
        self.soft_radius.valueChanged.connect(self.change_soft_selection)
        self.form = QtWidgets.QWidget()
        form = QtWidgets.QVBoxLayout(self.form)
        form.setContentsMargins(0, 0, 0, 0)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(['Distance', 'Edge Count', 'Percent'])
        self.distance = QtWidgets.QDoubleSpinBox()
        self.distance.setRange(0, 1e6)
        self.distance.setDecimals(4)
        self.distance.setKeyboardTracking(False)
        self.edges = QtWidgets.QSpinBox()
        self.edges.setRange(0, 100000)
        self.edges.setKeyboardTracking(False)

        self.mode.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.mode.setMinimumContentsLength(8)
        self.mode.setFixedWidth(96)
        self.soft_mode.setFixedWidth(96)
        self.percent = QtWidgets.QSpinBox()
        self.percent.setRange(0, 100)
        self.percent.setSuffix(' %')
        self.percent.setKeyboardTracking(False)
        for field in (self.distance, self.edges, self.percent):
            field.setMinimumWidth(68)
            field.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

        self.influence_label = QtWidgets.QLabel('Influence')

        self.align = QtWidgets.QCheckBox('Align to Curve')
        self.profile = QtWidgets.QComboBox()
        self.profile.addItems(['Linear', 'Smooth', 'Slow Start', 'Fast Start'])

        self.profile.setFixedWidth(96)
        self.profile.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)

        self.ramp = Ramp(self)
        form.addWidget(self.ramp)
        self.cv_count = QtWidgets.QSpinBox(self)
        self.cv_count.setRange(3, 32)
        self.cv_count.setKeyboardTracking(False)
        self.cv_count.hide()
        self.cv_count.valueChanged.connect(self.change_count)
        for field in (self.distance, self.edges, self.percent):
            field.valueChanged.connect(self.write_settings)
        self.mode.currentIndexChanged.connect(self.change_influence_mode)
        self.align.toggled.connect(self.write_settings)
        self.profile.currentIndexChanged.connect(self.write_settings)
        self.ramp.started.connect(self.start_graph)
        self.ramp.changed.connect(self.write_settings)
        self.ramp.finished.connect(self.end_graph)
        layout.addWidget(self.form)
        self.blend = QtWidgets.QCheckBox('Blend Overlap')
        self.blend.setToolTip('Компенсировать перекрывающиеся веса Control Curve вместо сложения смещений.')
        self.blend.toggled.connect(self.change_blend)

        self.blend.setText('Blend')
        self.align.setFixedWidth(96)
        self.influence_label.setFixedWidth(96)
        self.soft_radius.setMinimumWidth(68)
        self.soft_radius.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        for widget, row, column in (
                (self.soft_label, 0, 0), (self.soft_mode, 0, 1), (self.soft_radius, 0, 2),
                (self.influence_label, 1, 0), (self.mode, 1, 1),
                (self.align, 2, 0), (self.profile, 2, 1), (self.blend, 2, 2)):
            settings_grid.addWidget(widget, row, column)
        self.influence_fields = QtWidgets.QStackedWidget()
        self.influence_fields.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        for field in (self.distance, self.edges, self.percent):
            self.influence_fields.addWidget(field)
        settings_grid.addWidget(self.influence_fields, 1, 2)
        layout.insertLayout(layout.indexOf(self.form), settings_grid)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QtWidgets.QHBoxLayout()
        for name, action in (('Apply', lambda: self.finish(True)),
                             ('Cancel', lambda: self.finish(False))):
            button = QtWidgets.QPushButton(name)
            button.clicked.connect(action)
            row.addWidget(button)
        layout.addLayout(row)
        tips = {
            self.curves: 'Клик — выбрать CV кривой. Ctrl — добавить к выделению, Shift — диапазон. Двойной клик — переименовать. Del — удалить. Список читается слева направо и сверху вниз.',
            add: 'Создать кривые из выделенных цепочек рёбер текущего меша.',
            self.rebind: 'Перепривязать выбранную кривую к одной выделенной цепочке рёбер.',
            self.soft_label: 'Радиус мягкого выделения CV. Тяните подпись с зажатой ЛКМ. Shift — точнее, Ctrl — быстрее.',
            self.soft_mode: 'Мягкое выделение CV. Use Maya — настройки Maya, Volume — по объёму, Surface — вдоль поверхности.',
            self.soft_radius: 'Радиус мягкого выделения точек кривой в единицах сцены.',
            self.influence_label: 'Ширина влияния кривой на меш. Тяните подпись с зажатой ЛКМ для изменения значения.',
            self.mode: 'Distance — расстояние вдоль сетки. Edge Count — число шагов по рёбрам. Percent — доля пролёта до другой кривой или края.',
            self.distance: 'Дистанция влияния на соседние вершины вдоль рёбер меша.',
            self.edges: 'Количество рядов соседних вершин, затронутых кривой.',
            self.percent: 'Процент пролёта до соседней кривой или открытого края. 100% — полный пролёт.',
            self.align: 'Поместить вершины исходной цепочки точно на кривую и защитить их от смещений других кривых.',
            self.profile: 'Профиль затухания. Linear — линейный, Smooth — плавный, Slow Start — медленное начало, Fast Start — быстрое начало.',
            self.ramp: 'Затухание от кривой до границы влияния. ЛКМ — добавить или переместить точку. ПКМ — удалить внутреннюю точку.',
            self.blend: 'Blend Overlap — смешивать пересекающиеся влияния кривых, компенсируя суммирование смещений.',
        }
        for widget, text in tips.items():
            widget.setToolTip(text)
        for button in self.findChildren(QtWidgets.QPushButton):
            if button.text() == 'Apply':
                button.setToolTip('Сохранить редактируемые связи. Весь сеанс можно отменить одним Undo.')
            elif button.text() == 'Cancel':
                button.setToolTip('Отменить изменения текущего сеанса, включая движения CV и настройки кривых.')
        ensure_plugin()
        if not cmds.undoInfo(query=True, state=True):
            raise ValueError('Enable Maya Undo before editing Control Curves.')
        cmds.undoInfo(openChunk=True, chunkName='Control Curve Editor')
        self.callbacks = [om.MEventMessage.addEventCallback(event, self.schedule_refresh)
                          for event in ('SelectionChanged', 'Undo', 'Redo')]
        self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = self.curves.viewport().width()
        columns = max(4, width // 85)
        self.curves.setGridSize(QtCore.QSize(max(1, (width - 24) // columns), 26))

    def eventFilter(self, obj, event):
        if self.closed:
            return False
        kind = event.type()
        if isinstance(obj, QtWidgets.QWidget) and kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Delete:
            if obj is self.curves or self.curves.isAncestorOf(obj):
                self.delete_curves()
                return True
        if kind == QtCore.QEvent.MouseButtonRelease and self.drag_x is not None and event.button() == QtCore.Qt.MiddleButton:
            self.drag_x = None
            return True
        if kind not in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseMove, QtCore.QEvent.Wheel):
            return False
        modifiers = QtCore.Qt.ShiftModifier | QtCore.Qt.ControlModifier
        if not self.active or not self.form.isEnabled() or not self.cv_count.isEnabled():
            return False
        if kind == QtCore.QEvent.MouseMove and self.drag_x is not None:
            x = event.globalPosition().x()
            steps = int((x - self.drag_x) / 18)
            if steps:
                self.drag_x += steps * 18
                self.cv_count.setValue(self.cv_count.value() + steps)
            return True
        if event.modifiers() != modifiers:
            return False
        if isinstance(obj, QtWidgets.QWidget) and (obj is self or self.isAncestorOf(obj)):
            return False
        in_view = isinstance(obj, QtWidgets.QWidget) and (obj is self.view or self.view.isAncestorOf(obj))
        if not in_view and self.engine.event_viewport(obj, event) is None:
            return False
        if kind == QtCore.QEvent.MouseButtonPress and event.button() == QtCore.Qt.MiddleButton and event.modifiers() == modifiers:
            self.drag_x = event.globalPosition().x()
            return True
        if kind == QtCore.QEvent.Wheel and event.modifiers() == modifiers:
            self.cv_count.setValue(self.cv_count.value() + int(event.angleDelta().y() / 120))
            return True
        return False

    def delete_curves(self):
        shapes = [item.data(QtCore.Qt.UserRole)[2] for item in self.curves.selectedItems()]
        if not shapes:
            return
        cmds.undoInfo(openChunk=True, chunkName='Delete Control Curves')
        try:
            cmds.delete(list(dict.fromkeys(shape.rsplit('|', 1)[0] for shape in shapes)))
            self.update_percent_bindings()
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh()

    edit_soft_selection = staticmethod(ControlCurves.edit_soft_selection)
    restore_soft_selection = ControlCurves.restore_soft_selection

    def change_soft_selection(self, *_):
        mode = self.soft_mode.currentIndex()
        self.soft_radius.setEnabled(bool(mode))
        ControlCurves.set_soft_selection(self, mode, self.soft_radius.value())

    def mesh_path(self):
        if not self.mesh.isAlive() or not self.mesh.isValid():
            raise ValueError('The edited mesh was removed.')
        return om.MDagPath.getAPathTo(self.mesh.object())

    def schedule_refresh(self, *_):
        QtCore.QTimer.singleShot(0, self.refresh)

    def blend_node(self):
        shape = self.mesh_path().fullPathName()
        source = cmds.listConnections(shape + '.inMesh', source=True, destination=False) or []
        if source and cmds.nodeType(source[0]) == NODE_TYPE:
            return source[0]
        return None

    def change_blend(self, enabled):
        node = self.blend_node()
        if node is None:
            return
        state = json.loads(cmds.getAttr(node + '.binding'))
        if state.get('region') is not None:
            return
        cmds.undoInfo(openChunk=True, chunkName='Blend Control Curve Overlap')
        try:
            if enabled:
                self.update_percent_bindings()
                state = json.loads(cmds.getAttr(node + '.binding'))
            state['blend_overlap'] = enabled
            cmds.setAttr(node + '.binding', json.dumps(state, separators=(',', ':')), type='string')
            upstream = cmds.listConnections(node + '.inputMesh', source=True, destination=False) or []
            while upstream and cmds.nodeType(upstream[0]) == NODE_TYPE:
                prior = upstream[0]
                previous = json.loads(cmds.getAttr(prior + '.binding'))
                if previous.get('region') is not None or previous['topology'] != state['topology']:
                    break
                if previous.pop('blend_overlap', False):
                    cmds.setAttr(prior + '.binding', json.dumps(previous, separators=(',', ':')), type='string')
                upstream = cmds.listConnections(prior + '.inputMesh', source=True, destination=False) or []
        finally:
            cmds.undoInfo(closeChunk=True)

    def refresh(self):
        if self.closed or self.updating or self.graph_chunk:
            return
        try:
            mesh = self.mesh_path()
        except ValueError:
            self.finish(True)
            return
        blend_node = self.blend_node()
        blend_state = json.loads(cmds.getAttr(blend_node + '.binding')) if blend_node else {}
        with QtCore.QSignalBlocker(self.blend):
            self.blend.setEnabled(bool(blend_state) and blend_state.get('region') is None)
            self.blend.setChecked(bool(blend_state.get('blend_overlap')))
        current_topology = topology(om.MFnMesh(mesh))
        selection = {name.split('.', 1)[0] for name in cmds.ls(selection=True, long=True) or []}
        selected = []
        entries = []
        for node in reversed(cmds.listHistory(mesh.fullPathName()) or []):
            if cmds.nodeType(node) != NODE_TYPE:
                continue
            state = json.loads(cmds.getAttr(node + '.binding'))
            valid = state['topology'] == current_topology
            for index in range(len(state['rows'])):
                shapes = cmds.listConnections(f'{node}.controlCurve[{index}]', source=True,
                                              destination=False, shapes=True) or []
                if not shapes:
                    continue
                shape = cmds.ls(shapes[0], long=True)[0]
                transform = shape.rsplit('|', 1)[0]
                key = (node, index, shape)
                if shape in selection or transform in selection:
                    selected.append(key)
                label = transform.rsplit('|', 1)[-1]
                if label.startswith('recurveControl'):
                    label = f'Curve{len(entries) + 1:02d}'
                entries.append((key, label, valid))
        desired = selected[0] if len(selected) == 1 else self.active if not selected else None
        with QtCore.QSignalBlocker(self.curves):
            self.curves.clear()
            for key, label, valid in entries:
                item = QtWidgets.QListWidgetItem(label if valid else label + ' · Needs Rebind')
                item.setData(QtCore.Qt.UserRole, key)
                self.curves.addItem(item)
                if tuple(desired or ()) == key:
                    self.curves.setCurrentItem(item)
                if key in selected:
                    item.setSelected(True)
            if not selected and self.curves.currentItem() is None and entries:
                self.curves.setCurrentRow(0)
        items = self.curves.selectedItems()
        self.load_item(items[0] if len(items) == 1 else None)
        self.status.setText('Select one curve to edit its settings.' if len(selected) > 1 else
                            '' if entries else 'Select mesh edges, then add a curve.')

    def rename_curve(self, item):
        node, index, shape = item.data(QtCore.Qt.UserRole)
        label = item.text().removesuffix(' · Needs Rebind')
        name, accepted = QtWidgets.QInputDialog.getText(self, 'Rename Curve', 'Name', text=label)
        if not accepted or not name.strip() or name.strip() == label:
            return
        try:
            transform = cmds.rename(shape.rsplit('|', 1)[0], name.strip())
            new_shape = cmds.listRelatives(transform, shapes=True, fullPath=True)[0]
            self.active = (node, index, new_shape)
            self.refresh()
        except RuntimeError as error:
            self.status.setText(str(error))

    def choose(self):
        items = self.curves.selectedItems()
        self.load_item(items[0] if len(items) == 1 else None)
        if not items:
            return
        components = []
        hilite = om.MSelectionList()
        for item in items:
            shape = item.data(QtCore.Qt.UserRole)[2]
            path = om.MSelectionList().add(shape).getDagPath(0)
            curve = om.MFnNurbsCurve(path)
            count = curve.numCVs - (curve.degree if curve.form == curve.kPeriodic else 0)
            hilite.add(shape.rsplit('|', 1)[0])
            components.append(f'{shape}.cv[0:{count - 1}]')
        om.MGlobal.setSelectionMode(om.MGlobal.kSelectComponentMode)
        om.MGlobal.setComponentSelectionMask(om.MSelectionMask.kSelectCVs)
        om.MGlobal.setHiliteList(hilite)
        cmds.select(components, replace=True)
        cmds.setToolTo('Move')

    def load_item(self, item):
        self.active = tuple(item.data(QtCore.Qt.UserRole)) if item else None
        invalid = bool(item) and item.text().endswith('Needs Rebind')
        self.form.setEnabled(bool(item) and not invalid)
        self.rebind.setVisible(invalid)
        if not item:
            self.update_display()
            return
        node, index, shape = self.active
        state = json.loads(cmds.getAttr(node + '.binding'))
        settings = state.get('guides', {}).get(str(index), state)
        influence = settings.get('influence', state.get('influence', {}))
        self.updating = True
        try:
            distance = om.MDistance(influence.get('distance', 0), om.MDistance.kCentimeters)
            self.distance.setValue(distance.asUnits(om.MDistance.uiUnit()))
            self.edges.setValue(influence.get('edges', 0))
            self.percent.setValue(influence.get('percent', 100))
            self.mode.setCurrentIndex(influence.get('mode', int(influence.get('by_edges', False))))
            self.profile.setCurrentIndex(int(settings.get('smooth', state.get('smooth', 0))))
            self.align.setChecked(settings.get('align', state['align']))
            self.ramp.knots = [list(point) for point in settings.get('falloff', state.get('falloff', DEFAULT_FALLOFF))]
            curve = om.MFnNurbsCurve(om.MSelectionList().add(shape).getDagPath(0))
            self.cv_count.setValue(curve.numCVs - (curve.degree if curve.form == curve.kPeriodic else 0))
            self.cv_count.setEnabled(True)
            self.update_display()
            if state.get('region') is not None:
                self.form.setEnabled(False)
        finally:
            self.updating = False

    def update_display(self):
        fields = (self.distance, self.edges, self.percent)
        current = fields[self.mode.currentIndex()]
        self.influence_fields.setCurrentIndex(self.mode.currentIndex())
        for field in fields:
            field.setVisible(field is current)
        self.influence_label.setBuddy(current)
        profile = self.profile.currentIndex()
        self.ramp.interpolator = lambda x, knots: falloff_weight(x, knots, profile)
        self.ramp.update()

    def influence_binding(self, data, row, node, index, mesh, stored=None):
        mode = self.mode.currentIndex()
        distance = om.MDistance(self.distance.value(), om.MDistance.uiUnit()).asCentimeters()
        edges, percent = self.edges.value(), self.percent.value()
        if stored is not None:
            mode = stored['mode']
            distance, edges, percent = stored['distance'], stored['edges'], stored['percent']
        matrix = mesh.inclusiveMatrix()
        bridge = []
        if mode == 2:
            other_rows = []
            expected = topology(om.MFnMesh(data))
            for binding in cmds.listHistory(mesh.fullPathName()) or []:
                if cmds.nodeType(binding) != NODE_TYPE:
                    continue
                state = json.loads(cmds.getAttr(binding + '.binding'))
                if state['topology'] != expected:
                    continue
                for i, other in enumerate(state['rows']):
                    if (binding, i) != (node, index) and cmds.connectionInfo(
                            f'{binding}.controlCurve[{i}]', isDestination=True):
                        other_rows.append(other)
            # Use the undistorted input of the consecutive curve stack.
            rest = data
            plug = om.MFnDependencyNode(om.MSelectionList().add(node).getDependNode(0)).findPlug('inputMesh', False)
            while not plug.source().isNull:
                upstream = om.MFnDependencyNode(plug.source().node())
                if upstream.typeName != NODE_TYPE:
                    break
                plug = upstream.findPlug('inputMesh', False)
                candidate = plug.asMObject()
                if candidate.isNull() or topology(om.MFnMesh(candidate)) != expected:
                    break
                rest = candidate
            weights = percent_binding(rest, row, other_rows, percent, matrix, bridge)
        else:
            weights = lateral_binding(data, [row], edges if mode == 1 else distance, matrix, mode == 1)
        return weights, dict(mode=mode, by_edges=mode == 1, distance=distance,
                             edges=edges, percent=percent, bridge=bridge)

    def update_percent_bindings(self):
        mesh = self.mesh_path()
        for node in cmds.listHistory(mesh.fullPathName()) or []:
            if cmds.nodeType(node) != NODE_TYPE:
                continue
            state = json.loads(cmds.getAttr(node + '.binding'))
            guides = [(int(index), settings) for index, settings in state.get('guides', {}).items()
                      if settings.get('influence', {}).get('mode') == 2]
            if not guides:
                continue
            fn = om.MFnDependencyNode(om.MSelectionList().add(node).getDependNode(0))
            data = fn.findPlug('inputMesh', False).asMObject()
            if data.isNull() or topology(om.MFnMesh(data)) != state['topology']:
                continue
            for index, settings in guides:
                settings['lateral'], settings['influence'] = self.influence_binding(
                    data, state['rows'][index], node, index, mesh, settings['influence'])
            cmds.setAttr(node + '.binding', json.dumps(state, separators=(',', ':')), type='string')

    def change_influence_mode(self, mode):
        self.update_display()
        self.write_settings()
        if not self.updating and self.active is not None and mode == 2 and self.blend.isEnabled():
            self.blend.setChecked(True)

    def write_settings(self, *_):
        if self.updating or self.active is None:
            return
        self.update_display()
        node, index, _ = self.active
        state = json.loads(cmds.getAttr(node + '.binding'))
        mesh = self.mesh_path()
        data = om.MFnDependencyNode(om.MSelectionList().add(node).getDependNode(0)).findPlug('inputMesh', False).asMObject()
        if topology(om.MFnMesh(data)) != state['topology']:
            return
        binding, influence = self.influence_binding(data, state['rows'][index], node, index, mesh)
        state.setdefault('guides', {})[str(index)] = dict(
            align=self.align.isChecked(), smooth=self.profile.currentIndex(),
            falloff=self.ramp.knots, lateral=binding,
            influence=influence,
        )
        cmds.setAttr(node + '.binding', json.dumps(state, separators=(',', ':')), type='string')

    def change_count(self, count):
        if self.updating or not self.active:
            return
        shape = self.active[2]
        degree = min(3, count - 1)
        curve = om.MFnNurbsCurve(om.MSelectionList().add(shape).getDagPath(0))
        periodic = curve.form == curve.kPeriodic
        cmds.rebuildCurve(shape, constructionHistory=False, replaceOriginal=True,
                          rebuildType=0, keepRange=0, keepEndPoints=not periodic,
                          degree=degree, spans=count if periodic else count - degree)
        self.schedule_refresh()

    def start_graph(self):
        if not self.graph_chunk:
            cmds.undoInfo(openChunk=True, chunkName='Control Curve Falloff')
            self.graph_chunk = True

    def end_graph(self):
        if self.graph_chunk:
            cmds.undoInfo(closeChunk=True)
            self.graph_chunk = False

    def rebind_curve(self):
        if not self.active:
            return
        try:
            node, index, shape = self.active
            state = json.loads(cmds.getAttr(node + '.binding'))
            if len(state['rows']) != 1 or state.get('region') is not None:
                raise ValueError('Rebind supports a single edge guide per binding.')
            path, component = selected_mesh()
            if path.node() != self.mesh_path().node() or not component.hasFn(om.MFn.kMeshEdgeComponent):
                raise ValueError('Select a replacement edge chain on the edited mesh.')
            node_fn = om.MFnDependencyNode(om.MSelectionList().add(node).getDependNode(0))
            data = node_fn.findPlug('inputMesh', False).asMObject()
            source = om.MFnMesh(data)
            if topology(source) != topology(om.MFnMesh(path)):
                raise ValueError('The mesh topology changes after this binding. Rebind requires matching input and output topology.')
            chains = self.engine.selected_chains(source, om.MFnSingleIndexedComponent(component).getElements())
            if len(chains) != 1:
                raise ValueError('Select one connected replacement chain.')
            curve_path = om.MSelectionList().add(shape).getDagPath(0)
            curve = om.MFnNurbsCurve(curve_path)
            matrix = relative_matrix(curve_path, path)
            row = chains[0]
            state.update(topology=topology(source), rows=[row], offsets=None, align=False,
                         parameters=[parameters([tuple(source.getPoint(v))[:3] for v in row])],
                         baselines=[dict(cvs=[tuple(p * matrix)[:3] for p in curve.cvPositions()],
                                         knots=list(curve.knots()), degree=curve.degree,
                                         closed=curve.form == curve.kPeriodic)])
            binding, influence = self.influence_binding(data, row, node, index, path)
            state['guides'] = {str(index): dict(
                align=False, smooth=self.profile.currentIndex(), falloff=self.ramp.knots,
                lateral=binding, influence=influence,
            )}
            cmds.setAttr(node + '.binding', json.dumps(state, separators=(',', ':')), type='string')
            self.refresh()
        except (RuntimeError, ValueError) as error:
            self.status.setText(str(error))

    def add_curves(self):
        try:
            path, component = selected_mesh()
            if path.node() != self.mesh_path().node() or not component.hasFn(om.MFn.kMeshEdgeComponent):
                raise ValueError('Select edges on the mesh being edited.')
            blend_enabled = self.blend.isChecked()
            context = cmds.currentCtx()
            selection = om.MGlobal.getActiveSelectionList()
            hilite = om.MGlobal.getHiliteList()
            mode = om.MGlobal.selectionMode()
            mask = om.MGlobal.componentSelectionMask()
            chains = self.engine.selected_chains(om.MFnMesh(path), om.MFnSingleIndexedComponent(component).getElements())
            try:
                for chain in chains:
                    session = self.engine.Session(path.fullPathName(), chain, self.view)
                    try:
                        session.create_controls(4)
                        transform = session.controls.paths()[0].fullPathName().rsplit('|', 1)[0]
                        number = self.curves.count() + 1
                        while cmds.objExists(f'Curve{number:02d}'):
                            number += 1
                        cmds.rename(transform, f'Curve{number:02d}')
                        session.save_curves()
                        if blend_enabled:
                            self.change_blend(True)
                        self.update_percent_bindings()
                        self.refresh()
                    except Exception:
                        session.finish(False)
                        raise
                self.refresh()
            finally:
                if cmds.currentCtx() != context:
                    cmds.setToolTo(context)
                om.MGlobal.setSelectionMode(mode)
                om.MGlobal.setComponentSelectionMask(mask)
                om.MGlobal.setHiliteList(hilite)
                om.MGlobal.setActiveSelectionList(selection)
        except (RuntimeError, ValueError) as error:
            self.status.setText(str(error))

    def finish(self, apply):
        if self.closed:
            return
        self.closed = True
        QtWidgets.QApplication.instance().removeEventFilter(self)
        for callback in self.callbacks:
            om.MMessage.removeCallback(callback)
        self.end_graph()
        self.restore_soft_selection()
        cmds.undoInfo(closeChunk=True)
        if not apply and cmds.undoInfo(query=True, undoName=True) == 'Control Curve Editor':
            cmds.undo()
            selection = [name for name in self.original_selection if cmds.objExists(name)]
            cmds.select(selection, replace=True) if selection else cmds.select(clear=True)
        self.window._control_curve_editor = None
        if self.window._active_tool == 'recurve':
            self.window._active_tool = None
        self.window.control_curve_button.setChecked(False)
        self.hide()
        self.window.layout().removeWidget(self)
        self.window.adjustSize()
        self.deleteLater()




