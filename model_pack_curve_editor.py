"""Curve Editor panel and reversible, native-undo curve bevel sessions."""
import math
import uuid
import weakref
from maya import cmds
from maya.api import OpenMaya as om, OpenMayaUI as omui
from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid, wrapInstance
from model_pack_bevel.session import without_undo
from model_pack_curve_bevel_geometry import Curve, clean_geometry

CONTEXT = 'modelPackCurveBevelContext'
DEFAULT_CLEAN_TOLERANCE = .02
_HOST = None
MANIPULATORS = {QtCore.Qt.Key_Q: 'selectSuperContext', QtCore.Qt.Key_W: 'moveSuperContext',
                QtCore.Qt.Key_E: 'RotateSuperContext', QtCore.Qt.Key_R: 'scaleSuperContext'}


def selected_points():
    selection = om.MGlobal.getActiveSelectionList()
    found = {}
    objects = set()
    for i in range(selection.length()):
        path, component = selection.getComponent(i)
        if component.isNull():
            objects.add(path.fullPathName())
            continue
        if not component.hasFn(om.MFn.kCurveCVComponent):
            raise ValueError('Curve Bevel: выделите точки CV кривой.')
        if path.node().hasFn(om.MFn.kTransform):
            path.extendToShape()
        if not path.node().hasFn(om.MFn.kNurbsCurve):
            raise ValueError('Curve Bevel: выделите точки CV кривой.')
        found.setdefault(path.fullPathName(), set()).update(om.MFnSingleIndexedComponent(component).getElements())
    if len(found) != 1:
        raise ValueError('Curve Bevel: выделите точки одной кривой.')
    shape, ids = next(iter(found.items()))
    if objects - {shape, shape.rsplit('|', 1)[0]}:
        raise ValueError('Curve Bevel: выделите точки одной кривой.')
    return shape, ids


def write_curve(shape, geometry):
    points = geometry['points']
    knots = geometry['knots']
    cmds.setAttr(shape+'.create', geometry['degree'], len(points)-geometry['degree'],
                 geometry['form']-1, True, 3, tuple(knots), 0, len(points),
                 *[tuple(point) for point in points], type='nurbsCurve')


def _check_editable(shape, fn, tool, *, preview=False):
    if fn.isInstanced() or cmds.referenceQuery(shape, isNodeReferenced=True):
        raise ValueError(f'{tool}: сделайте кривую локальной и уникальной.')
    create = shape + '.create'
    if cmds.listConnections(create, source=True, destination=False):
        raise ValueError(f'{tool}: у кривой есть входящая история.')
    tweaks = [f'{shape}.controlPoints[{i}]' for i in cmds.getAttr(shape + '.controlPoints', multiIndices=True) or []]
    if (cmds.getAttr(create, lock=True) or
            any(cmds.getAttr(plug, lock=True) or cmds.listConnections(plug, source=True, destination=False)
                for plug in tweaks) or (preview and not cmds.getAttr(shape + '.visibility', settable=True))):
        raise ValueError(f'{tool}: атрибуты кривой заблокированы или подключены.')


def _apply_edits(edits, tool):
    if not cmds.undoInfo(query=True, state=True):
        raise ValueError(f'{tool}: включите Undo в Maya.')
    chunk = f'{tool} {uuid.uuid4().hex}'
    cmds.undoInfo(openChunk=True, chunkName=chunk)
    changed = False
    try:
        for shape, geometry in edits:
            write_curve(shape, geometry)
            changed = True
        cmds.select(list(dict.fromkeys(shape.rsplit('|', 1)[0] for shape, _ in edits)), replace=True)
    except Exception:
        cmds.undoInfo(closeChunk=True)
        if changed and cmds.undoInfo(query=True, undoName=True) == chunk:
            cmds.undo()
        raise
    else:
        cmds.undoInfo(closeChunk=True)


def clean_selected_curves(tolerance=DEFAULT_CLEAN_TOLERANCE):
    shapes = []
    selection = om.MGlobal.getActiveSelectionList()
    for i in range(selection.length()):
        try:
            path = selection.getDagPath(i)
        except (RuntimeError, TypeError):
            continue
        if path.node().hasFn(om.MFn.kNurbsCurve):
            if not om.MFnDagNode(path).isIntermediateObject:
                shapes.append(path.fullPathName())
        elif path.node().hasFn(om.MFn.kTransform):
            shapes.extend(cmds.listRelatives(path.fullPathName(), shapes=True, noIntermediate=True,
                                            type='nurbsCurve', fullPath=True) or [])
    shapes = list(dict.fromkeys(shapes))
    if not shapes:
        raise ValueError('Clean Points: выделите кривую или её CV.')

    edits = []
    removed = 0
    skipped = []
    for shape in shapes:
        fn = om.MFnNurbsCurve(om.MSelectionList().add(shape).getDagPath(0))
        try:
            _check_editable(shape, fn, 'Clean Points')
        except ValueError as error:
            skipped.append((shape, error))
            continue
        result = clean_geometry(fn, tolerance)
        if result is not None:
            edits.append((shape, result))
            removed += fn.numCVs - len(result['points'])
    if len(skipped) == len(shapes):
        raise skipped[0][1]
    if edits:
        _apply_edits(edits, 'Clean Points')
    for shape, error in skipped:
        cmds.warning(f'{shape} — {error}')
    return removed


class Session:
    def __init__(self):
        self.shape, selected = selected_points()
        self.path = om.MSelectionList().add(self.shape).getDagPath(0)
        fn = om.MFnNurbsCurve(self.path)
        _check_editable(self.shape, fn, 'Curve Bevel', preview=True)
        if not cmds.undoInfo(query=True, state=True):
            raise ValueError('Curve Bevel: включите Undo в Maya.')
        self.geometry = Curve(fn, selected)
        self.handle = om.MObjectHandle(fn.object())
        self.original = [tuple(p) for p in fn.cvPositions()]
        self.selection = om.MGlobal.getActiveSelectionList()
        self.visible = cmds.getAttr(self.shape+'.visibility')
        self.preview = None
        self.closed = False
        self.width = self.geometry.maximum*.3
        self.count = max(self.geometry.minimum_points, int(cmds.optionVar(query='ModelPack_CurveBevelPoints'))
                         if cmds.optionVar(exists='ModelPack_CurveBevelPoints') else 4)
        self.after = None
        try:
            with without_undo():
                self.preview = cmds.createNode('transform', name='curveBevelPreview#')
                cmds.xform(self.preview, worldSpace=True, matrix=list(self.geometry.matrix))
                self.preview_shape = cmds.createNode('nurbsCurve', parent=self.preview)
                cmds.setAttr(self.preview_shape+'.overrideEnabled', True)
                cmds.setAttr(self.preview_shape+'.overrideRGBColors', True)
                cmds.setAttr(self.preview_shape+'.overrideColorRGB', .25, .65, .72)
                cmds.setAttr(self.preview_shape+'.overrideDisplayType', 2)
                cmds.setAttr(self.preview_shape+'.dispCV', True)
                self.update()
                cmds.setAttr(self.shape+'.visibility', False)
                om.MGlobal.setActiveSelectionList(self.selection)
        except Exception:
            self.cancel()
            raise

    def valid(self):
        if not self.handle.isValid() or not self.handle.isAlive():
            raise ValueError('Curve Bevel: исходная кривая удалена.')
        self.shape = om.MFnDagNode(self.handle.object()).fullPathName()
        fn = om.MFnNurbsCurve(self.handle.object())
        if [tuple(p) for p in fn.cvPositions()] != self.original:
            raise ValueError('Curve Bevel: кривая изменилась во время предпросмотра.')

    def update(self, width=None, count=None):
        self.valid()
        width = self.width if width is None else max(self.geometry.maximum*1e-6, min(self.geometry.maximum, width))
        count = self.count if count is None else max(self.geometry.minimum_points, min(64, int(count)))
        result = self.geometry.build(width, count)
        with without_undo():
            write_curve(self.preview_shape, result)
        self.width, self.count, self.after = width, count, result

    def cleanup(self):
        with without_undo():
            if self.preview and cmds.objExists(self.preview):
                cmds.delete(self.preview)
            if self.handle.isValid() and self.handle.isAlive():
                self.shape = om.MFnDagNode(self.handle.object()).fullPathName()
                cmds.setAttr(self.shape+'.visibility', self.visible)
        self.preview = None
        self.closed = True

    def cancel(self):
        if not self.closed:
            self.cleanup()

    def commit(self):
        if self.closed:
            return
        self.valid()
        try:
            _apply_edits([(self.shape, self.after)], 'Curve Bevel')
        finally:
            self.cleanup()


def shutdown(apply=False):
    global _HOST
    host, _HOST = _HOST, None
    if host is not None and isValid(host):
        host.stop(apply)


def toggle(window):
    global _HOST
    if _HOST is not None and _HOST.window() is window:
        shutdown(apply=True)
        return
    shutdown()
    window._prepare_tool('curve_editor')
    _HOST = Host(window)
    _HOST.start()


def sync(window):
    checked = _HOST is not None and _HOST.window() is window
    if window.curve_editor_button.isChecked() != checked:
        window.curve_editor_button.setChecked(checked)


class Host(QtCore.QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window_ref = weakref.ref(window)
        self.session = None
        self.panel = None
        self.stopped = False
        self.switching = False
        self.wheel_delta = 0
        self.callbacks = []
        self.compact_size = QtCore.QSize(window.size())

    def window(self):
        window = self.window_ref()
        return window if window is not None and isValid(window) else None

    def start(self):
        window = self.window()
        self.panel = QtWidgets.QGroupBox('Curve Editor', window)
        layout = QtWidgets.QVBoxLayout(self.panel)
        button = QtWidgets.QPushButton('Bevel', self.panel)
        button.setAutoDefault(False)
        button.clicked.connect(self.begin)
        self.clean_button = QtWidgets.QPushButton('Clean Points', self.panel)
        self.clean_button.setAutoDefault(False)
        self.clean_button.setToolTip('Удалить избыточные CV выбранных кривых с сохранением формы в пределах допуска.')
        self.clean_button.clicked.connect(self.clean_points)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(button)
        actions.addWidget(self.clean_button)
        layout.addLayout(actions)
        row = QtWidgets.QFormLayout()
        self.width = QtWidgets.QDoubleSpinBox(self.panel)
        self.width.setDecimals(3)
        self.width.setKeyboardTracking(False)
        self.width.setRange(.001, 1e9)
        self.width.valueChanged.connect(self.change_width)
        row.addRow('Width', self.width)
        self.points = QtWidgets.QSpinBox(self.panel)
        self.points.setRange(3, 64)
        self.points.setValue(4)
        self.points.valueChanged.connect(self.change_points)
        row.addRow('Points', self.points)
        self.clean_tolerance = QtWidgets.QDoubleSpinBox(self.panel)
        self.clean_tolerance.setDecimals(3)
        self.clean_tolerance.setRange(0., 100.)
        self.clean_tolerance.setSingleStep(.005)
        self.clean_tolerance.setSuffix('%')
        self.clean_tolerance.setValue(DEFAULT_CLEAN_TOLERANCE)
        self.clean_tolerance.setToolTip('Максимальное отклонение при очистке в процентах от наибольшего размера кривой. 0% — точная очистка.')
        row.addRow('Clean tolerance', self.clean_tolerance)
        layout.addLayout(row)
        self.hint = QtWidgets.QLabel('Select curve CVs, then Bevel.\nDrag LMB: width · Shift + Wheel: points', self.panel)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        row = QtWidgets.QHBoxLayout()
        self.apply = QtWidgets.QPushButton('Apply', self.panel)
        self.cancel = QtWidgets.QPushButton('Cancel', self.panel)
        for control in (self.apply, self.cancel):
            control.setAutoDefault(False)
            row.addWidget(control)
        self.apply.clicked.connect(lambda: self.finish(True))
        self.cancel.clicked.connect(lambda: self.finish(False))
        layout.addLayout(row)
        window.layout().addWidget(self.panel, window._panel_row, 0, 1, 2)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self.callbacks = [om.MEventMessage.addEventCallback('ToolChanged', self.tool_changed)]
        self.callbacks.extend(om.MSceneMessage.addCallback(event, lambda *_: shutdown())
                              for event in (om.MSceneMessage.kBeforeNew, om.MSceneMessage.kBeforeOpen))
        self.sync_fields()
        window.curve_editor_button.setChecked(True)
        window.layout().activate()
        window.resize(window.width(), window.sizeHint().height())

    def sync_fields(self):
        enabled = self.session is not None
        self.clean_button.setEnabled(not enabled)
        self.clean_tolerance.setEnabled(not enabled)
        for control in (self.width, self.points, self.apply, self.cancel):
            control.setEnabled(enabled)
        if enabled:
            session = self.session
            with QtCore.QSignalBlocker(self.width), QtCore.QSignalBlocker(self.points):
                scale = om.MDistance(1., om.MDistance.kCentimeters).asUnits(om.MDistance.uiUnit())
                self.width.setRange(max(session.geometry.maximum*scale*1e-6, 1e-9), session.geometry.maximum*scale)
                self.width.setValue(session.width*scale)
                self.points.setMinimum(session.geometry.minimum_points)
                self.points.setValue(session.count)

    def clean_points(self):
        try:
            count = clean_selected_curves(self.clean_tolerance.value())
        except (ValueError, RuntimeError) as error:
            cmds.warning(str(error))
            return
        message = f'CVs removed: {count}' if count else 'No redundant CVs found.'
        cmds.inViewMessage(statusMessage=message, pos='midCenterTop', fade=True)

    def begin(self):
        try:
            if self.session is not None:
                self.finish(True)
                return
            self.session = Session()
            self.switching = True
            try:
                options = dict(pressCommand=self.press, dragCommand=self.drag,
                               space='screen', cursor='crossHair', undoMode='none')
                if cmds.draggerContext(CONTEXT, exists=True):
                    cmds.draggerContext(CONTEXT, edit=True, **options)
                else:
                    cmds.draggerContext(CONTEXT, **options)
                cmds.setToolTo(CONTEXT)
            finally:
                self.switching = False
            self.wheel_delta = 0
            self.sync_fields()
        except Exception as error:
            self.finish(False)
            cmds.warning(str(error))

    def change_width(self, value):
        scale = om.MDistance(1., om.MDistance.uiUnit()).asCentimeters()
        self.update(width=value*scale)

    def change_points(self, value):
        self.update(count=value)

    def update(self, **kwargs):
        if self.session is None:
            return
        try:
            self.session.update(**kwargs)
            cmds.optionVar(intValue=('ModelPack_CurveBevelPoints', self.session.count))
        except Exception as error:
            cmds.warning(str(error))
        self.sync_fields()
        # Maya defers viewport redraws inside a draggerContext callback.
        # Present the new curve while the mouse button is still held.
        cmds.refresh(currentView=True)

    def press(self):
        self.anchor = cmds.draggerContext(CONTEXT, query=True, anchorPoint=True)[0]
        self.anchor_width = self.session.width

    def drag(self):
        if self.session is not None:
            x = cmds.draggerContext(CONTEXT, query=True, dragPoint=True)[0]
            self.update(width=self.anchor_width+(x-self.anchor)*self.session.geometry.maximum/200.)

    def finish(self, apply):
        session, self.session = self.session, None
        if session is not None:
            try:
                session.commit() if apply else session.cancel()
            except Exception as error:
                session.cancel()
                cmds.warning(str(error))
        if cmds.currentCtx() == CONTEXT:
            self.switching = True
            try:
                cmds.setToolTo('selectSuperContext')
            finally:
                self.switching = False
        self.sync_fields()

    def tool_changed(self, *_):
        if not self.switching and self.session is not None and cmds.currentCtx() != CONTEXT:
            self.finish(True)

    def owns(self, target):
        if not isinstance(target, QtWidgets.QWidget):
            return False
        if target is self.panel or self.panel.isAncestorOf(target):
            return True
        for panel in cmds.getPanel(type='modelPanel') or []:
            try:
                widget = wrapInstance(int(omui.M3dView.getM3dViewFromModelPanel(panel).widget()), QtWidgets.QWidget)
                if target is widget or widget.isAncestorOf(target):
                    return True
            except (RuntimeError, ValueError):
                pass
        return False

    def eventFilter(self, target, event):
        if self.stopped or self.session is None or not self.owns(target):
            return False
        inputs = (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit)
        if isinstance(target, inputs) or isinstance(QtWidgets.QApplication.instance().focusWidget(), inputs):
            return False
        if event.type() == QtCore.QEvent.Wheel and event.modifiers() == QtCore.Qt.ShiftModifier:
            self.wheel_delta += event.angleDelta().y()
            steps = math.trunc(self.wheel_delta/120)
            self.wheel_delta -= steps*120
            if steps:
                self.update(count=self.session.count+steps)
            event.accept()
            return True
        if event.type() not in (QtCore.QEvent.KeyPress, QtCore.QEvent.ShortcutOverride):
            return False
        key = event.key()
        if event.modifiers() != QtCore.Qt.NoModifier or key not in (
                QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_Escape, *MANIPULATORS):
            return False
        if event.type() == QtCore.QEvent.KeyPress and not event.isAutoRepeat():
            self.finish(key != QtCore.Qt.Key_Escape)
            if key in MANIPULATORS:
                cmds.setToolTo(MANIPULATORS[key])
        event.accept()
        return True

    def stop(self, apply=False):
        if self.stopped:
            return
        self.stopped = True
        self.finish(apply)
        if cmds.draggerContext(CONTEXT, exists=True):
            cmds.deleteUI(CONTEXT, toolContext=True)
        QtWidgets.QApplication.instance().removeEventFilter(self)
        for callback in self.callbacks:
            om.MMessage.removeCallback(callback)
        self.callbacks = []
        window = self.window()
        if self.panel is not None and isValid(self.panel):
            self.panel.hide()
            if window is not None:
                window.layout().removeWidget(self.panel)
            self.panel.deleteLater()
        if window is not None:
            if window._active_tool == 'curve_editor':
                window._active_tool = None
            window.curve_editor_button.setChecked(False)
            window.layout().activate()
            window.resize(self.compact_size)
        self.deleteLater()
