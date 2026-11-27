"""Dock controls and preview lifecycle for Curve Mesh Loft."""
import gc
import itertools
import runpy
import json
from pathlib import Path

from maya import cmds
from maya.api import OpenMaya as om
from PySide6 import QtCore, QtGui, QtWidgets

from model_pack_offset import transient
from recurve_controls import ControlCurves


NODE = 'modelPackCurveMesh'
PLUGIN = 'model_pack_curve_mesh_node'


def selected_curves():
    result = []
    for item in cmds.ls(orderedSelection=True, objectsOnly=True, long=True) or []:
        shapes = [item] if cmds.nodeType(item) == 'nurbsCurve' else (
            cmds.listRelatives(item, shapes=True, noIntermediate=True, type='nurbsCurve', fullPath=True) or [])
        for shape in shapes:
            uid = cmds.ls(shape, uuid=True)[0]
            if uid not in result:
                result.append(uid)
    return result


def curve_path(uid):
    paths = cmds.ls(uid, long=True, type='nurbsCurve') or []
    if not paths:
        raise ValueError('A section curve was removed.')
    return paths[0]


def curve_fn(uid):
    return om.MFnNurbsCurve(om.MSelectionList().add(curve_path(uid)).getDagPath(0))


def auto_sections(sections, across):
    result = []
    previous = None
    for uid in sections:
        curve = curve_fn(uid)
        from model_pack_curve_mesh_node import sample_section
        closed = curve.form != curve.kOpen
        samples = sample_section(curve, across, closed, om.MSpace.kWorld)
        count = len(samples)
        reverse, shift = False, 0
        if previous is not None:
            if len(previous) != count:
                raise ValueError('Use all open or all closed sections.')
            best = float('inf')
            for flipped in (False, True):
                oriented = (samples[:1] + samples[:0:-1] if closed else samples[::-1]) if flipped else samples
                for offset in range(count if closed else 1):
                    score = sum((point - oriented[(i+offset) % count]).length()**2
                                for i, point in enumerate(previous))
                    if score < best:
                        best, reverse, shift = score, flipped, offset
        oriented = (samples[:1] + samples[:0:-1] if closed else samples[::-1]) if reverse else samples
        previous = oriented[shift:] + oriented[:shift]
        result.append([reverse, shift])
    return result



def existing_mesh():
    selected = cmds.ls(selection=True, objectsOnly=True, long=True) or []
    if len(selected) != 1:
        return None, None
    item = selected[0]
    shapes = [item] if cmds.nodeType(item) == 'mesh' else (
        cmds.listRelatives(item, shapes=True, noIntermediate=True, type='mesh', fullPath=True) or [])
    if len(shapes) == 1:
        node = cmds.listConnections(shapes[0] + '.inMesh', source=True, destination=False, type=NODE) or []
        if node:
            return node[0], cmds.listRelatives(shapes[0], parent=True, fullPath=True)[0]
    return None, None


def connect_sections(node, sections, options):
    for index in cmds.getAttr(node + '.sections', multiIndices=True) or []:
        dest = f'{node}.sections[{index}]'
        source = cmds.connectionInfo(dest, sourceFromDestination=True)
        if source:
            cmds.disconnectAttr(source, dest)
    for index, uid in enumerate(sections):
        cmds.connectAttr(curve_path(uid) + '.worldSpace[0]', f'{node}.sections[{index}]')
    cmds.setAttr(node + '.options', json.dumps(options), type='string')


def boundary_match(sections):
    endpoints = {}
    for uid in sections:
        curve = curve_fn(uid)
        if curve.form != curve.kOpen:
            raise ValueError('Boundary needs four open curves.')
        lo, hi = curve.knotDomain
        endpoints[uid] = [curve.getPointAtParam(t, om.MSpace.kWorld) for t in (lo, hi)]
    best = None
    for tail in itertools.permutations(sections[1:]):
        order = [sections[0], *tail]
        for flips in itertools.product((False, True), repeat=4):
            pairs = [endpoints[uid][::-1] if flip else endpoints[uid] for uid, flip in zip(order, flips)]
            score = sum((pairs[i][1] - pairs[(i+1) % 4][0]).length()**2 for i in range(4))
            if best is None or score < best[0]:
                best = (score, order, flips)
    return best[1], [[flip, 0] for flip in best[2]]


class SectionList(QtWidgets.QListWidget):
    remove_requested = QtCore.Signal(int)
    reorder_requested = QtCore.Signal(int, int)

    def __init__(self):
        super().__init__()
        self.setViewMode(QtWidgets.QListView.IconMode)
        self.setFlow(QtWidgets.QListView.LeftToRight)
        self.setWrapping(True)
        self.setMovement(QtWidgets.QListView.Snap)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        # Reserve the vertical scrollbar width before computing the columns.
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        self.setSpacing(0)
        self.setUniformItemSizes(True)
        self.setTextElideMode(QtCore.Qt.ElideRight)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        size = QtCore.QSize(max(1, (self.viewport().width() - 24) // max(4, self.viewport().width() // 85)), 26)
        if self.gridSize() != size:
            self.setGridSize(size)

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if item and event.button() == QtCore.Qt.LeftButton:
            rect = self.visualItemRect(item)
            if event.position().x() <= rect.left() + 19:
                self.remove_requested.emit(self.row(item))
                return
        super().mousePressEvent(event)

    def dropEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        destination = self.row(item) if item else self.count() - 1
        source = self.currentRow()
        if source >= 0 and destination >= 0:
            self.reorder_requested.emit(source, destination)
            event.accept()
        else:
            event.ignore()

def make_mesh(sections, options, preview=False):
    if not cmds.pluginInfo(PLUGIN, query=True, loaded=True):
        cmds.loadPlugin(str(Path(__file__).with_name(PLUGIN + '.py')), quiet=True)
    if not preview and not cmds.about(batch=True) and not cmds.pluginInfo(PLUGIN, q=True, autoload=True):
        cmds.pluginInfo(PLUGIN, edit=True, autoload=True)
        cmds.pluginInfo(savePluginPrefs=True)
    node = cmds.createNode(NODE, skipSelect=True)
    transform = None
    try:
        transform = cmds.createNode('transform', name='curveMeshPreview#' if preview else 'curveMesh#', skipSelect=True)
        shape = cmds.createNode('mesh', parent=transform, skipSelect=True)
        connect_sections(node, sections, options)
        cmds.connectAttr(node + '.outputMesh', shape + '.inMesh')
        cmds.sets(shape, edit=True, forceElement='initialShadingGroup')
        if not cmds.polyEvaluate(shape, face=True):
            raise ValueError('No mesh generated. Check the sections.')
        return node, transform
    except Exception:
        cmds.delete([item for item in (node, transform) if item and cmds.objExists(item)])
        raise


class Panel(QtWidgets.QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.closed = False
        self.drag_x = None
        self.previous_soft_selection = None
        self.node, self.result = existing_mesh()
        saved = json.loads(cmds.getAttr(self.node + '.options')) if self.node else {}
        self.sections = selected_curves()
        self.orientations = {}
        if self.node:
            self.sections = []
            for index, orientation in enumerate(saved['sections']):
                source = cmds.connectionInfo(f'{self.node}.sections[{index}]', sourceFromDestination=True)
                if source:
                    uid = cmds.ls(source.split('.', 1)[0], uuid=True)[0]
                    self.sections.append(uid)
                    self.orientations[uid] = orientation
        self.influences = dict(zip(self.sections, (saved.get('influences') or [])))
        for config in self.influences.values():
            if config.get('mode') == 1:
                config['mode'] = 2
                config['percent'] = min(100., 100. * (config.get('edges', 4) + 1) / saved.get('between', 4))
        self.loading_influence = False
        self.result_hidden = False
        self.preview = ()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 0)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(['Loft', 'Boundary'])
        self.mode.setToolTip('Loft — сетка по двум или нескольким сечениям. Boundary — патч по четырём открытым границам. Разошедшиеся углы сетки сводятся посередине.')
        self.mode.setCurrentIndex(int(saved.get('mode') == 'boundary'))
        layout.addWidget(self.mode)
        self.list = SectionList()
        self.list.setMaximumHeight(112)
        self.list.itemClicked.connect(self.select_section)
        self.list.itemSelectionChanged.connect(self.sync_influence)
        self.list.remove_requested.connect(self.remove)
        self.list.reorder_requested.connect(self.reorder)
        layout.addWidget(self.list)
        row = QtWidgets.QHBoxLayout()
        for title, callback, tip in (
                ('Add', self.add, 'Добавить выделенные кривые в построение.'),
                ('Auto', self.auto_match, 'Подобрать направления сечений и положение швов. В Boundary также упорядочить стороны.'),
                ('Reverse', self.reverse, 'Развернуть направление выбранных кривых.')):
            button = QtWidgets.QPushButton(title)
            button.setMinimumWidth(0)
            button.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            button.setToolTip(tip)
            button.clicked.connect(callback)
            row.addWidget(button, 1)
        seam_label = QtWidgets.QLabel('Seam')
        seam_label.setToolTip('Сместить шов выбранных замкнутых сечений на один сегмент.')
        row.addWidget(seam_label)
        for title, delta in (('+', 1), ('−', -1)):
            button = QtWidgets.QPushButton(title)
            button.setFixedWidth(25)
            button.setToolTip('Сдвиг шва на один сегмент ' + ('вперёд.' if delta > 0 else 'назад.'))
            button.clicked.connect(lambda checked=False, step=delta: self.shift(step))
            row.addWidget(button)
        layout.addLayout(row)
        form = QtWidgets.QHBoxLayout()
        self.across = QtWidgets.QSpinBox()
        self.across.setRange(3, 128)
        self.across.setValue(saved.get('across', 16))
        self.between = QtWidgets.QSpinBox()
        self.between.setRange(1, 128)
        self.between.setValue(saved.get('between', 4))
        self.smooth = QtWidgets.QCheckBox('Smooth')
        self.smooth.setChecked(saved.get('smooth', False))
        for title, field, tip in (
                ('Across', self.across, 'Число сегментов вдоль каждого сечения. В Boundary — вдоль первой и третьей сторон.'),
                ('Between', self.between, 'Ряды между соседними сечениями. В Boundary — вдоль второй и четвёртой сторон.')):
            label = QtWidgets.QLabel(title)
            label.setBuddy(field)
            label.setToolTip(tip + ' Тяните подпись с зажатой ЛКМ для изменения.')
            field.setToolTip(tip)
            field.setMinimumWidth(42)
            field.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            field.setKeyboardTracking(False)
            form.addWidget(label)
            form.addWidget(field, 1)
        self.smooth.setToolTip('Плавный переход между сечениями Loft с сохранением сечений. Boundary использует патч Кунса.')
        form.addWidget(self.smooth)
        layout.addLayout(form)
        soft_row = QtWidgets.QHBoxLayout()
        soft_label = QtWidgets.QLabel('CV Soft Select')
        self.soft_mode = QtWidgets.QComboBox()
        self.soft_mode.addItems(['Use Maya', 'Volume', 'Surface'])
        self.soft_mode.setFixedWidth(90)
        self.soft_radius = QtWidgets.QDoubleSpinBox()
        self.soft_radius.setRange(.001, 1e6)
        self.soft_radius.setDecimals(3)
        self.soft_radius.setSingleStep(.1)
        self.soft_radius.setKeyboardTracking(False)
        self.soft_radius.setValue(cmds.softSelect(query=True, softSelectDistance=True))
        self.soft_radius.setEnabled(False)
        self.soft_radius.setMinimumWidth(50)
        self.soft_radius.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        soft_label.setBuddy(self.soft_radius)
        soft_label.setToolTip('Радиус мягкого выделения CV. Тяните подпись с зажатой ЛКМ. Shift — точнее, Ctrl — быстрее.')
        self.soft_mode.setToolTip('Use Maya — исходные настройки Maya. Volume — влияние по объёму. Surface — вдоль поверхности.')
        self.soft_radius.setToolTip('Радиус мягкого выделения точек кривой в единицах сцены.')
        for field in (soft_label, self.soft_mode, self.soft_radius):
            soft_row.addWidget(field)
        layout.addLayout(soft_row)
        self.soft_mode.currentIndexChanged.connect(self.change_soft_selection)
        self.soft_radius.valueChanged.connect(self.change_soft_selection)
        from model_pack_surface_fit_ui import Ramp
        self.influence_panel = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(self.influence_panel)
        grid.setContentsMargins(0, 0, 0, 0)
        self.influence_mode = QtWidgets.QComboBox()
        self.influence_mode.addItem('Distance', 0)
        self.influence_mode.addItem('Percent', 2)
        self.influence_mode.setFixedWidth(96)
        self.influence_value = QtWidgets.QDoubleSpinBox()
        self.influence_value.setRange(0, 1e6)
        self.influence_value.setKeyboardTracking(False)
        self.influence_value.setMinimumWidth(60)
        self.influence_value.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        influence_label = QtWidgets.QLabel('Influence')
        influence_label.setBuddy(self.influence_value)
        self.align_curve = QtWidgets.QCheckBox('Align to Curve')
        self.falloff_mode = QtWidgets.QComboBox()
        self.falloff_mode.addItems(['Linear', 'Smooth', 'Slow Start', 'Fast Start'])
        self.falloff_mode.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.falloff_mode.setMinimumWidth(90)
        self.ramp = Ramp(self)
        grid.addWidget(influence_label, 0, 0)
        grid.addWidget(self.influence_mode, 0, 1)
        grid.addWidget(self.influence_value, 0, 2)
        grid.addWidget(self.align_curve, 1, 0)
        grid.addWidget(self.falloff_mode, 1, 1, 1, 2)
        grid.addWidget(self.ramp, 2, 0, 1, 3)
        layout.addWidget(self.influence_panel)
        influence_label.setToolTip('Ширина влияния выбранных сечений Loft. Тяните подпись ЛКМ для изменения. Перекрытия смешиваются по нормализованным весам.')
        self.influence_mode.setToolTip('Distance — расстояние между сечениями. Percent — доля пролёта до соседнего сечения. Затухание меняет форму поперёк направления построения.')
        self.influence_value.setToolTip('Значение влияния выбранных кривых. Distance вводится в единицах сцены.')
        self.align_curve.setToolTip('Точно удерживать ряд исходного сечения на кривой. При отключении ряд участвует в смешивании соседних влияний.')
        self.falloff_mode.setToolTip('Профиль затухания: линейный, плавный, медленное начало, быстрое начало.')
        self.ramp.setToolTip('Слева — сечение, справа — граница влияния. ЛКМ — добавить или переместить точку. ПКМ — удалить внутреннюю точку.')
        self.influence_mode.currentIndexChanged.connect(self.change_influence_mode)
        self.influence_value.valueChanged.connect(self.write_influence)
        self.align_curve.toggled.connect(self.write_influence)
        self.falloff_mode.currentIndexChanged.connect(self.write_influence)
        self.ramp.changed.connect(self.write_influence)
        self.list.setToolTip('Клик — выделить CV. Ctrl — добавить кривую, Shift — диапазон. Перетаскивание ЛКМ — изменить порядок. Крестик слева — исключить кривую из построения. Ctrl+Shift+СКМ во вьюпорте — изменить число CV; колесо с теми же клавишами тоже работает. Del — исключить выделенные кривые.')
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QtWidgets.QHBoxLayout()
        self.apply_button = QtWidgets.QPushButton('Apply')
        self.bake_button = QtWidgets.QPushButton('Bake')
        cancel = QtWidgets.QPushButton('Cancel')
        self.apply_button.clicked.connect(lambda: self.commit(False))
        self.bake_button.clicked.connect(lambda: self.commit(True))
        cancel.clicked.connect(self.finish)
        for button in (self.apply_button, self.bake_button, cancel):
            row.addWidget(button)
        for button, tip in ((self.apply_button, 'Применить с сохранением связи с кривыми. Выберите меш и снова откройте Curve Mesh для перенастройки.'),
                            (self.bake_button, 'Запечь результат в обычный полигональный меш.'),
                            (cancel, 'Убрать превью и закрыть настройки.')):
            button.setToolTip(tip)
        layout.addLayout(row)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self.across.valueChanged.connect(self.refresh)
        self.between.valueChanged.connect(self.refresh)
        self.smooth.toggled.connect(self.refresh)
        self.mode.currentIndexChanged.connect(self.auto_match)
        self.refresh()

    def influence_defaults(self):
        return dict(mode=2, percent=100., edges=4, distance=1., align=True,
                    profile=0, falloff=[[0., 1.], [1., 0.]])

    def sync_influence(self):
        if not hasattr(self, 'influence_panel'):
            return
        selected = self.selected_sections()
        self.influence_panel.setEnabled(bool(selected) and self.mode.currentIndex() == 0)
        self.influence_panel.setToolTip('Индивидуальное влияние сечений доступно в Loft. Boundary определяется четырьмя границами.')
        if not selected or self.loading_influence:
            return
        self.loading_influence = True
        try:
            config = self.influences.get(selected[0], self.influence_defaults())
            self.influence_mode.setCurrentIndex(0 if config['mode'] == 0 else 1)
            self.configure_influence_value(config)
            self.align_curve.setChecked(config['align'])
            self.falloff_mode.setCurrentIndex(config['profile'])
            self.ramp.knots = [list(point) for point in config['falloff']]
            self.update_ramp()
        finally:
            self.loading_influence = False

    def configure_influence_value(self, config):
        mode = self.influence_mode.currentData()
        key = ('distance', 'edges', 'percent')[mode]
        self.influence_value.setDecimals(3 if mode == 0 else 0)
        self.influence_value.setMaximum(100 if mode == 2 else 1e6)
        value = config[key]
        if mode == 0:
            value = om.MDistance(value, om.MDistance.kCentimeters).asUnits(om.MDistance.uiUnit())
        self.influence_value.setValue(value)

    def update_ramp(self):
        from recurve_controls import falloff_weight
        profile = self.falloff_mode.currentIndex()
        self.ramp.interpolator = lambda x, knots: falloff_weight(x, knots, profile)
        self.ramp.update()

    def change_influence_mode(self, *_):
        if self.loading_influence:
            return
        selected = self.selected_sections()
        if not selected:
            return
        self.loading_influence = True
        try:
            self.configure_influence_value(self.influences.get(selected[0], self.influence_defaults()))
        finally:
            self.loading_influence = False
        self.write_influence()

    def write_influence(self, *_):
        if self.loading_influence:
            return
        mode = self.influence_mode.currentData()
        value = self.influence_value.value()
        if mode == 0:
            value = om.MDistance(value, om.MDistance.uiUnit()).asCentimeters()
        for uid in self.selected_sections():
            config = dict(self.influences.get(uid, self.influence_defaults()))
            config.update(mode=mode, align=self.align_curve.isChecked(), profile=self.falloff_mode.currentIndex(),
                          falloff=[list(point) for point in self.ramp.knots])
            config[('distance', 'edges', 'percent')[mode]] = value
            self.influences[uid] = config
        self.update_ramp()
        self.refresh()

    edit_soft_selection = staticmethod(ControlCurves.edit_soft_selection)
    restore_soft_selection = ControlCurves.restore_soft_selection

    def change_soft_selection(self, *_):
        mode = self.soft_mode.currentIndex()
        self.soft_radius.setEnabled(bool(mode))
        ControlCurves.set_soft_selection(self, mode, self.soft_radius.value())

    def selected_sections(self):
        return [self.sections[self.list.row(item)] for item in self.list.selectedItems()]

    def eventFilter(self, obj, event):
        if self.closed:
            return False
        kind = event.type()
        if kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Delete and obj is self.list:
            for index in sorted([self.list.row(item) for item in self.list.selectedItems()], reverse=True):
                self.orientations.pop(self.sections.pop(index), None)
            self.refresh()
            return True
        if kind == QtCore.QEvent.MouseButtonRelease and self.drag_x is not None and event.button() == QtCore.Qt.MiddleButton:
            self.drag_x = None
            cmds.undoInfo(closeChunk=True)
            return True
        if kind == QtCore.QEvent.MouseMove and self.drag_x is not None:
            steps = int((event.globalPosition().x() - self.drag_x) / 18)
            if steps:
                self.drag_x += steps * 18
                self.change_cvs(steps)
            return True
        if kind not in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.Wheel):
            return False
        if event.modifiers() != (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier):
            return False
        if isinstance(obj, QtWidgets.QWidget) and (obj is self.owner or self.owner.isAncestorOf(obj)):
            return False
        from recurve import event_viewport
        if event_viewport(obj, event) is None or not self.selected_sections():
            return False
        if kind == QtCore.QEvent.Wheel:
            self.change_cvs(int(event.angleDelta().y() / 120))
            return True
        if event.button() == QtCore.Qt.MiddleButton:
            self.drag_x = event.globalPosition().x()
            cmds.undoInfo(openChunk=True, chunkName='Curve Mesh CVs')
            return True
        return False

    def change_cvs(self, steps):
        if not steps:
            return
        cmds.undoInfo(openChunk=True, chunkName='Curve Mesh CVs')
        try:
            for uid in self.selected_sections():
                curve = curve_fn(uid)
                periodic = curve.form == curve.kPeriodic
                count = max(3, min(32, curve.numCVs - (curve.degree if periodic else 0) + steps))
                degree = min(3, count - 1)
                cmds.rebuildCurve(curve_path(uid), constructionHistory=False, replaceOriginal=True,
                                  rebuildType=0, keepRange=0, keepEndPoints=not periodic,
                                  degree=degree, spans=count if periodic else count-degree)
        finally:
            cmds.undoInfo(closeChunk=True)
        self.select_section()

    def select_section(self, *_):
        try:
            components = []
            hilite = om.MSelectionList()
            for uid in self.selected_sections():
                shape = curve_path(uid)
                curve = curve_fn(uid)
                count = curve.numCVs - (curve.degree if curve.form == curve.kPeriodic else 0)
                components.append(f'{shape}.cv[0:{count - 1}]')
                hilite.add(shape.rsplit('|', 1)[0])
            if not components:
                return
            om.MGlobal.setSelectionMode(om.MGlobal.kSelectComponentMode)
            om.MGlobal.setComponentSelectionMask(om.MSelectionMask.kSelectCVs)
            om.MGlobal.setHiliteList(hilite)
            cmds.select(components, replace=True)
            cmds.setToolTo('Move')
        except (ValueError, RuntimeError) as error:
            self.status.setText(str(error))

    def add(self):
        self.sections = list(dict.fromkeys(self.sections + selected_curves()))
        self.auto_match()

    def remove(self, index):
        if 0 <= index < len(self.sections):
            self.orientations.pop(self.sections.pop(index), None)
            self.refresh()

    def reorder(self, source, target):
        if source != target:
            self.sections.insert(target, self.sections.pop(source))
            self.refresh()
            self.list.setCurrentRow(target)

    def auto_match(self, *_):
        self.orientations.clear()
        self.refresh()

    def reverse(self):
        for uid in self.selected_sections():
            if uid in self.orientations:
                self.orientations[uid][0] = not self.orientations[uid][0]
        self.refresh()

    def shift(self, step):
        for uid in self.selected_sections():
            if uid in self.orientations and curve_fn(uid).form != om.MFnNurbsCurve.kOpen:
                self.orientations[uid][1] = (self.orientations[uid][1] + step) % self.across.value()
        self.refresh()

    def options(self):
        if self.mode.currentIndex() == 1 and len(self.sections) != 4:
            raise ValueError('Boundary needs four curves that meet at their ends.')
        if len(self.sections) < 2:
            raise ValueError('Select at least two curves, then click Add.')
        if (len(self.sections) - 1) * self.across.value() * self.between.value() > 250000:
            raise ValueError('Reduce subdivisions to stay within 250,000 quads.')
        return dict(mode='boundary' if self.mode.currentIndex() else 'loft', across=self.across.value(), between=self.between.value(),
                    smooth=self.smooth.isChecked(),
                    sections=[self.orientations[uid] for uid in self.sections],
                    influences=[self.influences.get(uid, self.influence_defaults()) for uid in self.sections] if self.influences else None)

    def remove_preview(self):
        with transient():
            for item in reversed(self.preview):
                if cmds.objExists(item):
                    cmds.delete(item)
        self.preview = ()
        if self.result_hidden and self.result and cmds.objExists(self.result):
            with transient():
                cmds.setAttr(self.result + '.visibility', True)
        self.result_hidden = False

    def refresh(self, *_):
        index = self.list.currentRow()
        selected = set(self.selected_sections())
        blocker = QtCore.QSignalBlocker(self.list)
        self.list.clear()
        self.remove_preview()
        self.apply_button.setEnabled(False)
        self.bake_button.setEnabled(False)
        try:
            for uid in self.sections:
                try:
                    label = curve_path(uid).rsplit('|', 1)[-1]
                except ValueError:
                    label = '[Missing section]'
                item = QtWidgets.QListWidgetItem("×  " + label)
                item.setToolTip(label)
                item.setTextAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
                self.list.addItem(item)
                if uid in selected:
                    item.setSelected(True)
            if self.sections and not selected:
                self.list.setCurrentRow(max(0, min(index, len(self.sections) - 1)))
            if len(self.orientations) != len(self.sections):
                if self.mode.currentIndex() == 1:
                    if len(self.sections) != 4:
                        raise ValueError('Boundary needs four curves that meet at their ends.')
                    self.sections, settings = boundary_match(self.sections)
                    self.orientations = dict(zip(self.sections, settings))
                    self.refresh()
                    return
                self.orientations = dict(zip(self.sections, auto_sections(self.sections, self.across.value())))
            with transient():
                self.preview = make_mesh(self.sections, self.options(), preview=True)
                if self.result and cmds.objExists(self.result):
                    matrix = cmds.xform(self.result, query=True, worldSpace=True, matrix=True)
                    cmds.xform(self.preview[1], worldSpace=True, matrix=matrix)
                if self.result and cmds.objExists(self.result) and cmds.getAttr(self.result + '.visibility', settable=True) and cmds.getAttr(self.result + '.visibility'):
                    cmds.setAttr(self.result + '.visibility', False)
                    self.result_hidden = True
            del blocker
            self.sync_influence()
            count = cmds.polyEvaluate(self.preview[1], face=True)
            self.status.setText(f'{count} quads')
            self.apply_button.setEnabled(True)
            self.bake_button.setEnabled(True)
        except (ValueError, RuntimeError) as error:
            self.status.setText(str(error))

    def commit(self, bake):
        self.remove_preview()
        cmds.undoInfo(openChunk=True, chunkName='Curve Mesh')
        try:
            if self.node:
                node, result = self.node, self.result
                connect_sections(node, self.sections, self.options())
            else:
                node, result = make_mesh(self.sections, self.options())
            if bake:
                cmds.delete(result, constructionHistory=True)
                if cmds.objExists(node):
                    cmds.delete(node)
            cmds.select(result, replace=True)
        except (ValueError, RuntimeError) as error:
            self.status.setText(str(error))
            return
        finally:
            cmds.undoInfo(closeChunk=True)
        self.finish()

    def finish(self):
        if self.closed:
            return
        self.closed = True
        QtWidgets.QApplication.instance().removeEventFilter(self)
        if self.drag_x is not None:
            self.drag_x = None
            cmds.undoInfo(closeChunk=True)
        self.restore_soft_selection()
        self.remove_preview()
        self.owner._curve_mesh_panel = None
        if self.owner._active_tool == 'curve_mesh':
            self.owner._active_tool = None
        self.hide()
        self.owner.layout().removeWidget(self)
        self.deleteLater()


def refresh_registered_nodes():
    if not cmds.pluginInfo(PLUGIN, query=True, loaded=True):
        return
    fresh = runpy.run_path(str(Path(__file__).with_name(PLUGIN + '.py')))['CurveMeshNode']
    for candidate in gc.get_objects():
        if (type(candidate) is type and candidate.__name__ == 'CurveMeshNode'
                and issubclass(candidate, om.MPxNode) and hasattr(candidate, 'output')):
            candidate.compute = fresh.compute
    for node in cmds.ls(type=NODE) or []:
        cmds.dgdirty(node)


def close(owner):
    panel = getattr(owner, '_curve_mesh_panel', None)
    if panel is not None:
        panel.finish()


def toggle(owner):
    if getattr(owner, '_curve_mesh_panel', None) is not None:
        close(owner)
        return
    owner._prepare_tool('curve_mesh')
    panel = Panel(owner)
    owner._curve_mesh_panel = panel
    owner.layout().addWidget(panel, owner._panel_row, 0, 1, 2)
    panel.show()
