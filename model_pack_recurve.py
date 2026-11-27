"""ReCurve's shared preview and shortcuts embedded below the Model Pack buttons."""
import os
from pathlib import Path
import runpy
import sys
import weakref
import importlib

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid
from maya import cmds
from maya.api import OpenMaya as om
import recurve_colors

ROOT = Path(__file__).resolve().parent
OWNER = None
ENGINE = None
PANEL_TYPE = None
PANEL_BASE = None
RUNTIME = ('recurve', 'recurve_strip', 'recurve_bulk', 'recurve_uv', 'recurve_colors',
           'recurve_normals', 'recurve_controls', 'recurve_faces', 'recurve_panel', 'recurve_saved_curves')


def icon(name):
    result = QtGui.QIcon(str(ROOT / 'icons' / (name+'.svg')))
    active = ROOT / 'icons' / (name+'_active.svg')
    if active.is_file():result.addFile(str(active), QtCore.QSize(), QtGui.QIcon.Normal, QtGui.QIcon.On)
    return result


def owner():
    window = OWNER() if OWNER is not None else None
    return window if window is not None and isValid(window) else None


def ensure_runtime():
    """Use the tested updater when an older or separately installed copy exists."""
    global ENGINE
    canonical = lambda path: os.path.normcase(os.path.realpath(path))
    ready = all(name in sys.modules and
                canonical(getattr(sys.modules[name], '__file__', '') or '.') ==
                canonical(ROOT / (name+'.py')) for name in RUNTIME)
    ready = ready and hasattr(sys.modules['recurve'], 'MENU_HOST')
    ready = ready and hasattr(sys.modules['recurve'], 'INPUT_GUARD')
    ready = ready and getattr(sys.modules['recurve'], 'MODEL_PACK_BUILD', None) == '0.1.3'
    ready = ready and callable(getattr(sys.modules['recurve'].Session, 'set_align_to_curve', None))
    if ready and not hasattr(sys.modules['recurve'].Session, 'set_cv_soft_selection'):
        reload_runtime()
    ready = ready and callable(getattr(sys.modules['recurve'].Session, 'save_curves', None))
    ready = ready and callable(getattr(sys.modules['recurve'].WheelListener, 'set_application_active', None))
    ready = ready and callable(getattr(sys.modules['recurve'], 'copy_mesh_data', None))
    if ready and (not getattr(sys.modules['recurve'].WheelListener, 'guards_preview_undo', False) or
                  not getattr(sys.modules['recurve'].Session, 'preserves_initial_preview', False) or
                  not hasattr(sys.modules['recurve_strip'], 'align_closed_rows') or
                  not isinstance(getattr(sys.modules['recurve'].Session, 'source', None), property) or
                  not callable(getattr(sys.modules['recurve'].WheelListener,'wheel_action',None)) or
                  not callable(getattr(sys.modules['recurve'], 'transfer_face_groups', None)) or
                  not getattr(sys.modules['recurve'],'VERTEX_COLOR_SUPPORT',False)):
        reload_runtime()
    if not ready or not cmds.pluginInfo('recurve', query=True, loaded=True):
        runpy.run_path(str(ROOT / 'install_recurve.py'), run_name='__main__')
    ENGINE = sys.modules['recurve']
    ENGINE.install(quiet=True,bind_hotkey=False)
    if owner() is not None:ENGINE.MENU_HOST=show_host;ENGINE.INPUT_GUARD=input_guard
    return ENGINE


def reload_runtime():
    """Upgrade known canonical runtime code without rewriting Maya preferences."""
    module=sys.modules['recurve']
    if module.SESSION is not None:raise RuntimeError('Завершите текущий ReCurve перед обновлением инструментов.')
    module.shutdown();importlib.reload(module)
    for name in RUNTIME[1:]:importlib.reload(sys.modules[name])


def resolve_view():
    for panel in (cmds.getPanel(withFocus=True), cmds.getPanel(underPointer=True)):
        if panel and cmds.getPanel(typeOf=panel) == 'modelPanel':
            return ENGINE.viewport_widget(panel)
    panels = cmds.getPanel(type='modelPanel') or []
    if panels:
        return ENGINE.viewport_widget(panels[0])
    raise ValueError('ReCurve: open a model viewport first.')


class CountField(QtWidgets.QFrame):
    """Icon, minus, editable count and plus in one compact themed field."""
    def __init__(self, name, minimum, maximum, value, tooltip, parent):
        super().__init__(parent)
        self.setObjectName('recurveCountField')
        self.setStyleSheet('''
            QFrame#recurveCountField {background:#393d43;border:1px solid #464b53;border-radius:9px;}
            QFrame#recurveCountField QSpinBox {background:transparent;border:none;padding:0;min-height:0;font-size:14px;}
            QFrame#recurveCountField QToolButton {background:transparent;border:none;padding:0;min-height:0;border-radius:5px;}
            QFrame#recurveCountField QToolButton:hover {background:#454a51;}
        ''')
        self.setMinimumHeight(34)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        row = QtWidgets.QHBoxLayout(self);row.setContentsMargins(8, 2, 5, 2);row.setSpacing(3)
        mark = QtWidgets.QLabel(self);mark.setPixmap(icon(name).pixmap(20, 20));row.addWidget(mark)
        self.minus = QtWidgets.QToolButton(self);self.minus.setText('−')
        self.plus = QtWidgets.QToolButton(self);self.plus.setText('+')
        for button, text in ((self.minus, 'Уменьшить значение на один шаг.'), (self.plus, 'Увеличить значение на один шаг.')):
            button.setFixedSize(22, 26);button.setFocusPolicy(QtCore.Qt.NoFocus)
            button.setToolTip(text);button.setCursor(QtCore.Qt.PointingHandCursor)
        row.addWidget(self.minus)
        self.spin = QtWidgets.QSpinBox(self);self.spin.setRange(minimum, maximum);self.spin.setValue(value)
        self.spin.setAlignment(QtCore.Qt.AlignCenter)
        self.spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.spin.setKeyboardTracking(False);self.spin.setMinimumWidth(35)
        row.addWidget(self.spin, 1);row.addWidget(self.plus)
        self.minus.clicked.connect(self.spin.stepDown);self.plus.clicked.connect(self.spin.stepUp)
        self.setToolTip(tooltip);self.spin.setToolTip(tooltip)


class Feedback(QtWidgets.QLabel):
    def __init__(self, panel):
        super().__init__('', panel)
        self.panel = panel
        self.setWordWrap(True);self.hide()
        self.timer = QtCore.QTimer(self);self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.setText(''))

    def setText(self, text):
        self.timer.stop()
        if self.text() == text:return
        super().setText(text);self.setVisible(bool(text));self.panel.fit_owner()

    def show_notice(self, text):
        self.setText(text);self.timer.start(1500)


def panel_type():
    """Reuse ReCurve's controller; replace only its floating-window layout."""
    global PANEL_TYPE, PANEL_BASE
    recurve = ENGINE
    if PANEL_BASE is recurve.RecurveMenu:return PANEL_TYPE

    class EmbeddedPanel(recurve.RecurveMenu):
        def __init__(self, window, view):
            QtWidgets.QDialog.__init__(self, window, QtCore.Qt.Widget)
            self.setWindowFlags(QtCore.Qt.Widget)
            self.setObjectName('modelPackRecurveSettings')
            self.setStyleSheet('QDialog#modelPackRecurveSettings {background:transparent;border:none;}')
            self.window_ref = weakref.ref(window)
            self.view = view;self.section = 'segments'
            self.closed = False;self.control_request_generation = 0;self.control_wheel = 0
            self.segment_undo = [];self.segment_redo = [];self.undo_session = None;self.restoring_segments = False
            layout = QtWidgets.QVBoxLayout(self);layout.setContentsMargins(0, 5, 0, 0);layout.setSpacing(6)
            self.settings = QtWidgets.QHBoxLayout();self.settings.setSpacing(6);layout.addLayout(self.settings)
            self.segment_field = CountField('resegments', 1, 1000, 4,
                                           'Segments per selected chain · Shift+Wheel · Ctrl+Z: undo · Ctrl+Y: redo', self)
            self.curve_field = CountField('control_curve', 3, 32, 4,
                                         'Control points per curve · Ctrl+Shift+Wheel', self)
            self.count = self.segment_field.spin;self.control_points = self.curve_field.spin
            self.settings.addWidget(self.segment_field, 1);self.settings.addWidget(self.curve_field, 1)
            self.mode = QtWidgets.QComboBox(self)
            self.mode.addItem(icon('ngons'), 'N-gons');self.mode.addItem(icon('triangles'), 'Triangles')
            self.mode.setMinimumHeight(34);self.mode.setMinimumWidth(100)
            self.mode.setToolTip('Сохранять соседние N-gons или разбивать их на треугольники. Alt переключает режим.')
            self.settings.addWidget(self.mode, 1)
            self.align_curve = QtWidgets.QCheckBox('Align to Curve', self)
            self.align_curve.setObjectName('recurveAlignToCurve')
            self.align_curve.setChecked(False);self.align_curve.setEnabled(False)
            self.align_curve.setFocusPolicy(QtCore.Qt.NoFocus)
            self.align_curve.setToolTip('Притянуть исходные рёбра к управляющим кривым. Отключение возвращает исходные детали, сохраняя правки CV.')
            layout.addWidget(self.align_curve)
            self.cv_soft_panel = QtWidgets.QWidget(self)
            cv_row = QtWidgets.QHBoxLayout(self.cv_soft_panel)
            cv_row.setContentsMargins(0, 0, 0, 0)
            cv_label = QtWidgets.QLabel('CV Soft Select', self.cv_soft_panel)
            self.cv_soft_mode = QtWidgets.QComboBox(self.cv_soft_panel)
            self.cv_soft_mode.addItems(['Use Maya', 'Volume', 'Surface'])
            self.cv_soft_mode.setToolTip('Мягкое выделение CV кривой в Maya. Use Maya использует исходные настройки Maya.')
            self.cv_soft_radius = QtWidgets.QDoubleSpinBox(self.cv_soft_panel)
            self.cv_soft_radius.setRange(.001, 1e6)
            self.cv_soft_radius.setDecimals(3)
            self.cv_soft_radius.setSingleStep(.1)
            self.cv_soft_radius.setKeyboardTracking(False)
            self.cv_soft_radius.setToolTip('Радиус мягкого выделения точек кривой в единицах сцены.')
            cv_label.setBuddy(self.cv_soft_radius)
            cv_row.addWidget(cv_label)
            cv_row.addWidget(self.cv_soft_mode)
            cv_row.addWidget(self.cv_soft_radius)
            layout.addWidget(self.cv_soft_panel)
            self.cv_soft_mode.currentIndexChanged.connect(self.change_cv_soft_selection)
            self.cv_soft_radius.valueChanged.connect(self.change_cv_soft_selection)
            from model_pack_surface_fit_ui import Ramp
            self.influence_panel = QtWidgets.QWidget(self)
            influence_layout = QtWidgets.QVBoxLayout(self.influence_panel)
            influence_layout.setContentsMargins(0, 0, 0, 0)
            distance_row = QtWidgets.QHBoxLayout()
            self.influence_mode = QtWidgets.QComboBox(self.influence_panel)
            self.influence_mode.addItems(['Distance', 'Edge Count'])
            self.influence_mode.setToolTip('Выберите единицы ширины бокового влияния кривой.')
            self.influence_edges = QtWidgets.QSpinBox(self.influence_panel)
            self.influence_edges.setRange(0, 1000000)
            self.influence_edges.setKeyboardTracking(False)
            self.influence_edges.setToolTip('Число рядов соседних вершин, на которые влияет кривая с каждой стороны цепочки.')
            self.influence_edges.hide()
            distance_label = QtWidgets.QLabel('Influence', self.influence_panel)
            self.influence_distance = QtWidgets.QDoubleSpinBox(self.influence_panel)
            self.influence_distance.setRange(0., 1e9)
            self.influence_distance.setDecimals(4)
            self.influence_distance.setSingleStep(.1)
            self.influence_distance.setSuffix(' ' + cmds.currentUnit(query=True, linear=True))
            self.influence_distance.setKeyboardTracking(False)
            self.influence_distance.setToolTip('Расстояние влияния вдоль рёбер меша. При 0 деформируется только исходная цепочка.')
            self.influence_label = distance_label
            distance_label.setBuddy(self.influence_distance)
            distance_row.addWidget(distance_label)
            distance_row.addWidget(self.influence_mode)
            distance_row.addWidget(self.influence_distance)
            distance_row.addWidget(self.influence_edges)
            influence_layout.addLayout(distance_row)
            falloff_row = QtWidgets.QHBoxLayout()
            falloff_row.addWidget(QtWidgets.QLabel('Falloff', self.influence_panel))
            self.falloff_mode = QtWidgets.QComboBox(self.influence_panel)
            self.falloff_mode.addItems(['Linear', 'Smooth', 'Slow Start', 'Fast Start'])
            falloff_row.addWidget(self.falloff_mode)
            influence_layout.addLayout(falloff_row)
            self.influence_ramp = Ramp(self.influence_panel)
            influence_layout.addWidget(self.influence_ramp)
            layout.addWidget(self.influence_panel)
            self.influence_distance.valueChanged.connect(self.change_influence)
            self.influence_ramp.changed.connect(self.change_influence)
            self.influence_ramp.started.connect(self.begin_falloff_edit)
            self.influence_ramp.finished.connect(self.end_falloff_edit)
            self.falloff_undo = []
            self.falloff_redo = []
            self.falloff_session = None
            self.falloff_start = None
            self.influence_edges.valueChanged.connect(self.change_influence)
            self.influence_mode.currentIndexChanged.connect(self.change_influence)
            self.falloff_mode.currentIndexChanged.connect(self.change_falloff_mode)
            self.save_curves = QtWidgets.QPushButton('Save Curves', self)
            self.save_curves.setObjectName('recurveSaveCurves')
            self.save_curves.setMinimumHeight(32);self.save_curves.setAutoDefault(False)
            self.save_curves.setFocusPolicy(QtCore.Qt.NoFocus)
            self.save_curves.setToolTip('Сохранить управляющие кривые и редактирование меша после завершения инструмента и повторного открытия сцены.')
            layout.addWidget(self.save_curves)
            self.ngons = QtWidgets.QRadioButton(self);self.connect = QtWidgets.QRadioButton(self)
            self.ngons.hide();self.connect.hide()
            (self.connect if recurve.CONNECT_NEW else self.ngons).setChecked(True)
            self.mode.setCurrentIndex(1 if recurve.CONNECT_NEW else 0)
            actions = QtWidgets.QHBoxLayout();actions.setSpacing(6);layout.addLayout(actions)
            self.apply = QtWidgets.QPushButton(icon('apply'), 'Apply', self)
            self.apply.setProperty('primary', True)
            self.cancel = QtWidgets.QPushButton(icon('cancel'), 'Cancel', self)
            for button in (self.apply, self.cancel):
                button.setMinimumHeight(32);button.setAutoDefault(False);button.setFocusPolicy(QtCore.Qt.NoFocus)
                actions.addWidget(button)
            self.apply.setToolTip('Применить предпросмотр: Space или Enter.')
            self.cancel.setToolTip('Восстановить исходный меш: Esc.')
            self.feedback = Feedback(self);layout.addWidget(self.feedback)
            self.apply.clicked.connect(lambda: self.defer(lambda: recurve.handle_finish(True)))
            self.cancel.clicked.connect(lambda: self.defer(lambda: recurve.handle_finish(False)))
            self.count.valueChanged.connect(lambda value: self.defer(lambda: self.change_count(value)))
            self.control_points.valueChanged.connect(self.queue_control_points)
            self.align_curve.toggled.connect(lambda enabled: self.defer(lambda: self.change_curve_alignment(enabled)))
            self.save_curves.clicked.connect(lambda: self.defer(self.save_control_curves))
            self.connect.toggled.connect(self.mode_toggled)
            self.mode.currentIndexChanged.connect(lambda index: (self.connect if index else self.ngons).setChecked(True))
            self.set_section('segments')

        def defer(self, callback):
            generation = self.control_request_generation
            def run():
                if isValid(self) and recurve.MENU is self and self.isVisible() and generation == self.control_request_generation:
                    callback()
            recurve.queue_idle(run)

        def mode_toggled(self, connected):
            with QtCore.QSignalBlocker(self.mode):self.mode.setCurrentIndex(1 if connected else 0)
            self.defer(lambda: self.change_mode(connected))

        def sync(self, count, error=''):
            if recurve.SESSION:self.closed = recurve.SESSION.chain[0] == recurve.SESSION.chain[-1]
            with QtCore.QSignalBlocker(self.count):
                self.count.setMinimum(3 if self.closed else 1);self.count.setValue(count)
            self.feedback.setText(error);self.sync_controls()

        def sync_controls(self):
            session = recurve.SESSION
            if session is None:
                self.segment_undo.clear();self.segment_redo.clear();self.undo_session = None
                self.falloff_undo.clear();self.falloff_redo.clear();self.falloff_session = None
            self.apply.setEnabled(session is not None);self.cancel.setEnabled(session is not None)
            if session is not None and session.controls is not None:
                with QtCore.QSignalBlocker(self.control_points):self.control_points.setValue(session.controls.number)
            with QtCore.QSignalBlocker(self.align_curve):
                self.align_curve.setChecked(session is not None and session.align_to_curve)
            self.align_curve.setEnabled(session is not None and session.face_region is None and session.controls is not None)
            self.align_curve.setVisible(self.section == 'controls' and (session is None or session.face_region is None))
            self.cv_soft_panel.setVisible(self.section == 'controls')
            self.cv_soft_panel.setEnabled(session is not None and session.controls is not None)
            if session is not None and session.controls is not None:
                radius = session.cv_soft_radius
                if radius is None:
                    radius = cmds.softSelect(query=True, softSelectDistance=True)
                with QtCore.QSignalBlocker(self.cv_soft_mode), QtCore.QSignalBlocker(self.cv_soft_radius):
                    self.cv_soft_mode.setCurrentIndex(session.cv_soft_mode)
                    self.cv_soft_radius.setValue(radius)
                self.cv_soft_radius.setEnabled(bool(session.cv_soft_mode))
            self.influence_panel.setVisible(self.section == 'controls' and (session is None or session.face_region is None))
            self.influence_panel.setEnabled(session is not None and session.controls is not None)
            if session is not None and session.face_region is None:
                with QtCore.QSignalBlocker(self.influence_distance):
                    self.influence_distance.setSuffix(' ' + cmds.currentUnit(query=True, linear=True))
                    self.influence_distance.setValue(om.MDistance(session.influence_distance, om.MDistance.kCentimeters).asUnits(om.MDistance.uiUnit()))
                with QtCore.QSignalBlocker(self.influence_edges):
                    self.influence_edges.setValue(session.influence_edges)
                with QtCore.QSignalBlocker(self.influence_mode):
                    self.influence_mode.setCurrentIndex(int(session.influence_by_edges))
                with QtCore.QSignalBlocker(self.falloff_mode):
                    self.falloff_mode.setCurrentIndex(int(session.influence_smooth))
                self.sync_influence_display()
                if self.influence_ramp.grip is None:
                    self.influence_ramp.knots = [list(point) for point in session.influence_falloff]
                    self.influence_ramp.update()
            self.save_curves.setEnabled(session is not None and session.controls is not None)
            self.save_curves.setVisible(self.section == 'controls')
            window = self.window_ref()
            if window is not None and isValid(window):sync_buttons(window)

        def set_section(self, section):
            if recurve.SESSION is not None and recurve.SESSION.face_region is not None:
                if section == 'segments':self.feedback.setText('ReSegments: выделите рёбра. Для полигонов используйте Control Curve.')
                section = 'controls'
            if self.section!=section:self.control_request_generation+=1
            self.section = section
            self.segment_field.setVisible(section == 'segments')
            self.mode.setVisible(section == 'segments');self.curve_field.setVisible(section == 'controls')
            self.align_curve.setVisible(section == 'controls' and (recurve.SESSION is None or recurve.SESSION.face_region is None))
            self.save_curves.setVisible(section == 'controls')
            self.cv_soft_panel.setVisible(section == 'controls')
            self.influence_panel.setVisible(section == 'controls' and (recurve.SESSION is None or recurve.SESSION.face_region is None))
            self.fit_owner()
            window = self.window_ref()
            if window is not None and isValid(window):sync_buttons(window)

        def change_cv_soft_selection(self, *_):
            session = recurve.SESSION
            if session is None or session.controls is None:
                return
            mode = self.cv_soft_mode.currentIndex()
            session.set_cv_soft_selection(mode, self.cv_soft_radius.value())
            self.cv_soft_radius.setEnabled(bool(mode))

        def sync_influence_display(self):
            from recurve_controls import falloff_weight
            by_edges = self.influence_mode.currentIndex() == 1
            self.influence_distance.setVisible(not by_edges)
            self.influence_edges.setVisible(by_edges)
            self.influence_label.setBuddy(self.influence_edges if by_edges else self.influence_distance)
            profile = self.falloff_mode.currentIndex()
            self.influence_ramp.interpolator = (
                lambda x, knots: falloff_weight(x, knots, profile)
            ) if profile else None
            self.influence_ramp.update()

        def falloff_state(self, session):
            return session.influence_smooth, session.influence_falloff

        def begin_falloff_edit(self):
            session = recurve.SESSION
            if session is not self.falloff_session:
                self.falloff_session = session
                self.falloff_undo.clear()
                self.falloff_redo.clear()
            if session is not None and session.controls is not None:
                self.falloff_start = self.falloff_state(session)

        def end_falloff_edit(self):
            before, self.falloff_start = self.falloff_start, None
            session = recurve.SESSION
            if before is None or session is not self.falloff_session or session.controls is None:
                return
            after = self.falloff_state(session)
            if before != after:
                self.falloff_undo.append((before, after, session.controls.signature()))
                self.falloff_redo.clear()

        def change_falloff_mode(self):
            self.begin_falloff_edit()
            self.change_influence()
            self.end_falloff_edit()

        def restore_falloff(self, redo):
            session = recurve.SESSION
            stack = self.falloff_redo if redo else self.falloff_undo
            if session is not self.falloff_session or not stack:
                return
            step = stack[-1]
            profile, knots = step[1 if redo else 0]
            session.set_control_influence(
                session.influence_distance, knots, edges=session.influence_edges,
                by_edges=session.influence_by_edges, smooth=profile,
            )
            stack.pop()
            (self.falloff_undo if redo else self.falloff_redo).append(step)

        def change_influence(self, *_):
            session = recurve.SESSION
            if session is None or session.controls is None or session.face_region is not None:
                return
            distance = om.MDistance(self.influence_distance.value(), om.MDistance.uiUnit()).asCentimeters()
            try:
                self.sync_influence_display()
                session.set_control_influence(
                    distance, self.influence_ramp.knots,
                    edges=self.influence_edges.value(),
                    by_edges=self.influence_mode.currentIndex() == 1,
                    smooth=self.falloff_mode.currentIndex(),
                )
            except (ValueError, RuntimeError) as error:
                self.feedback.setText(str(error))

        def change_curve_alignment(self,enabled):
            if enabled != self.align_curve.isChecked():return
            session = recurve.SESSION
            if session is None or session.controls is None or session.face_region is not None:return
            try:
                session.set_align_to_curve(enabled)
            except Exception as exc:
                recurve.record_error('align_to_curve',exc)
                self.feedback.setText(str(exc))
                self.sync_controls()

        def save_control_curves(self):
            session=recurve.SESSION
            if session is None or session.controls is None:return
            try:session.save_curves()
            except Exception as exc:
                recurve.record_error('save_curves',exc)
                self.feedback.setText(str(exc));self.sync_controls()

        def adjust_control_points(self, delta, build=False):
            self.set_section('controls')
            super().adjust_control_points(delta, build)

        def toggle_mode(self):
            if self.section=='segments':super().toggle_mode()

        def change_count(self,value):
            if self.section=='segments':super().change_count(value)

        def change_mode(self,value):
            if self.section=='segments':super().change_mode(value)

        def fit_owner(self):
            window = self.window_ref()
            if window is not None and isValid(window) and not self.isHidden():
                self.layout().activate();window.layout().activate()
                window.resize(getattr(window, '_recurve_locked_width', window.width()), window.sizeHint().height())

        def segment_changed(self, session, before, after):
            if self.restoring_segments or before == after:return
            if self.undo_session is not session:
                self.segment_undo.clear();self.segment_redo.clear();self.undo_session = session
            signature = session.controls.signature() if session.controls is not None else None
            self.segment_undo.append((before, after, signature));self.segment_redo.clear()

        def undo_shortcut(self, obj, event):
            modifiers = event.modifiers()
            native_z = (self.section in ('segments', 'controls') and event.key() == QtCore.Qt.Key_Z
                        and modifiers in (QtCore.Qt.NoModifier, QtCore.Qt.ShiftModifier))
            if not native_z and not modifiers & QtCore.Qt.ControlModifier:
                return False
            if event.key() not in (QtCore.Qt.Key_Z, QtCore.Qt.Key_Y):
                return False
            focus = QtWidgets.QApplication.focusWidget()
            if any(self.owns(target) and isinstance(target, (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox)) for target in (obj, focus)):
                return False
            session = recurve.SESSION
            if session is None:return False
            redo = event.key() == QtCore.Qt.Key_Y or bool(event.modifiers() & QtCore.Qt.ShiftModifier)
            if self.section != 'segments' and session.controls is not None and session.controls.paths() is not None:
                stack = self.falloff_redo if redo else self.falloff_undo
                if session is not self.falloff_session or not stack or session.controls.signature() != stack[-1][2]:
                    return False
                if not event.isAutoRepeat():
                    self.defer(lambda: self.restore_falloff(redo))
                return True
            if native_z and self.section != 'segments':
                return False
            stack = self.segment_redo if redo else self.segment_undo
            if session.controls is not None:
                signature = session.controls.signature()
                if stack:
                    recorded = stack[-1][2]
                    if signature != recorded and not (recorded is None and not session.controls.deformed):return False
                elif signature is not None and session.controls.deformed:return False
            if not event.isAutoRepeat():self.defer(lambda: self.restore_segments(redo))
            return True

        def restore_segments(self, redo=False):
            session = recurve.SESSION
            if session is None:return
            stack = self.segment_redo if redo else self.segment_undo
            if not stack:
                if not redo:recurve.handle_finish(False)
                return
            step = stack[-1]
            self.restoring_segments = True
            try:
                session.rebuild_preview(step[1] if redo else step[0])
            finally:self.restoring_segments = False
            stack.pop()
            (self.segment_undo if redo else self.segment_redo).append(step)

        def closeEvent(self, event):
            self.hide();event.accept()

        def hideEvent(self, event):
            super().hideEvent(event)
            # Qt updates the hidden widget's layout minimum after hideEvent.
            QtCore.QTimer.singleShot(0, self.after_hide)

        def after_hide(self):
            if not isValid(self):return
            window = self.window_ref()
            if self.isHidden() and window is not None and isValid(window):
                window.layout().invalidate();window.layout().activate()
                size = getattr(window, '_recurve_compact_size', None)
                unlock_width(window)
                if size is not None:window.resize(size)
                if recurve.SESSION is None and window._active_tool == 'recurve':window._active_tool = None
                sync_buttons(window)

    PANEL_TYPE = EmbeddedPanel;PANEL_BASE = recurve.RecurveMenu
    return PANEL_TYPE


def sync_buttons(window):
    panel = getattr(window, '_recurve_panel', None)
    visible = panel is not None and isValid(panel) and not panel.isHidden()
    if hasattr(window, 'resegments_button'):
        window.resegments_button.setChecked(visible and panel.section == 'segments')
        window.control_curve_button.setChecked(bool(getattr(window, '_control_curve_editor', None)) or (visible and panel.section == 'controls'))
    for label, section in zip(getattr(window, 'recurve_shortcut_labels', ()), ('segments', 'controls')):
        label.setVisible(visible and panel.section == section)


def lock_width(window):
    if getattr(window, '_recurve_width_limits', None) is None:
        window._recurve_width_limits = (window.minimumWidth(), window.maximumWidth())
        window._recurve_locked_width = window.width()
    window.setFixedWidth(window._recurve_locked_width)


def unlock_width(window):
    limits = getattr(window, '_recurve_width_limits', None)
    if limits is not None:
        window.setMinimumWidth(limits[0]);window.setMaximumWidth(limits[1])
        window._recurve_width_limits = None


def open_panel(window, section='segments', view=None):
    if not isValid(window):return None
    editor = getattr(window, '_control_curve_editor', None)
    if section == 'controls':
        if editor is not None:
            return editor
        close_panel(window)
        window._prepare_tool('recurve')
        from model_pack_control_editor import Editor, selected_mesh
        try:
            editor = Editor(window, ENGINE, view or resolve_view())
        except (RuntimeError, ValueError) as error:
            window._active_tool = None
            cmds.warning('Control Curve: ' + str(error))
            return None
        window._control_curve_editor = editor
        window.layout().addWidget(editor, window._panel_row, 0, 1, 2)
        editor.show()
        _, component = selected_mesh()
        if component.hasFn(om.MFn.kMeshEdgeComponent):
            editor.add_curves()
        window.adjustSize()
        sync_buttons(window)
        return editor
    if editor is not None:
        editor.finish(True)
    window._prepare_tool('recurve')
    recurve = ENGINE
    lock_width(window)
    panel = getattr(window, '_recurve_panel', None)
    if panel is None or not isValid(panel):
        panel = panel_type()(window, view or resolve_view())
        window._recurve_panel = panel
        window.layout().addWidget(panel, window._panel_row, 0, 1, 2)
    elif view is not None:panel.view = view
    if recurve.MENU is not panel:
        if recurve.SESSION is not None:recurve.SESSION.finish(False)
        if recurve.MENU is not None:
            recurve.MENU.hide();recurve.MENU.deleteLater()
        recurve.MENU = panel
    if panel.isHidden():window._recurve_compact_size = QtCore.QSize(window.size())
    window.show();panel.show();window.raise_()
    panel.set_section(section)
    if recurve.SESSION is None:
        panel.refresh_selection();panel.begin_preview(controls=section == 'controls')
    else:panel.sync(recurve.SESSION.count)
    panel.set_section(section)
    if section == 'controls':panel.create_control_curves()
    panel.fit_owner();sync_buttons(window)
    return panel


def request(window, section):
    window._recurve_request_generation = getattr(window, '_recurve_request_generation', 0)+1
    generation = window._recurve_request_generation
    def run():
        if not isValid(window) or generation != window._recurve_request_generation:return
        editor = getattr(window, '_control_curve_editor', None)
        if section == 'controls' and editor is not None:
            editor.finish(True)
            return
        panel = getattr(window, '_recurve_panel', None)
        if panel is not None and isValid(panel) and not panel.isHidden() and panel.section == section:
            close_panel(window)
            if window._active_tool == 'recurve':window._active_tool = None
        else:open_panel(window, section)
    ENGINE.queue_idle(run)


def close_panel(window):
    editor = getattr(window, '_control_curve_editor', None)
    if editor is not None:
        editor.finish(True)
    panel = getattr(window, '_recurve_panel', None)
    if panel is None or not isValid(panel):
        unlock_width(window);return
    window._recurve_request_generation = getattr(window, '_recurve_request_generation', 0)+1
    panel.control_request_generation += 1
    if ENGINE is not None and ENGINE.MENU is panel:
        ENGINE.IDLE_TASKS.clear()
        ENGINE.PENDING_WHEEL_VIEW, ENGINE.PENDING_WHEEL_DELTA = None, 0
        ENGINE.PENDING_CONTROL_VIEW, ENGINE.PENDING_CONTROL_DELTA = None, 0
        if ENGINE.LISTENER is not None:
            ENGINE.LISTENER.space_apply = False;ENGINE.LISTENER.alt_tap = False
        if ENGINE.SESSION is not None:ENGINE.SESSION.finish(False)
        ENGINE.MENU = None
    was_visible = not panel.isHidden()
    panel.hide();window.layout().activate()
    size = getattr(window, '_recurve_compact_size', None)
    if was_visible and size is not None:window.resize(size)
    sync_buttons(window)


def show_host(view=None):
    window = owner()
    if window is None:return None
    panel = getattr(window, '_recurve_panel', None)
    section = panel.section if panel is not None and isValid(panel) and not panel.isHidden() else 'segments'
    return open_panel(window, section, view)


def input_guard(obj, event):
    window = owner()
    if window is not None and getattr(window, '_active_tool', None) not in (None, 'recurve'):return False
    if event.type() == QtCore.QEvent.Wheel and event.modifiers() & QtCore.Qt.ShiftModifier:
        panel = ENGINE.MENU
        if panel is None or not isValid(panel) or not panel.isVisible():return False
        # The listener consumes a Shift-only transition inside Control Curve
        # without handing it to hidden segment fields or Maya's wheel handlers.
    return True


def attach(window):
    global OWNER
    ensure_runtime()
    OWNER = weakref.ref(window)
    ENGINE.MENU_HOST = show_host;ENGINE.INPUT_GUARD = input_guard
    window._recurve_panel = None;window._recurve_request_generation = 0
    window.resegments_button.setFocusPolicy(QtCore.Qt.NoFocus)
    window.control_curve_button.setFocusPolicy(QtCore.Qt.NoFocus)
    sync_buttons(window)


def detach(window):
    global OWNER
    close_panel(window)
    if owner() is window:
        ENGINE.MENU_HOST = None;ENGINE.INPUT_GUARD = input_guard;OWNER = None

